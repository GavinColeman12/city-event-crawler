"""Luma (lu.ma / luma.com) collector.

Endpoints (public, anonymous - the same calls luma.com/barcelona makes in a browser):

* ``GET https://api.lu.ma/discover/get-paginated-events?discover_place_api_id=<id>&pagination_limit=50[&pagination_cursor=..]``
  - the city's discover feed, ascending by start time, ``has_more`` / ``next_cursor`` paging.
* ``GET https://api.lu.ma/event/get?event_api_id=<evt-id>`` - detail for one event
  (description + categories); fetched only for events inside the window.
* Fallback if the API call fails: the city page ``https://lu.ma/<slug>`` embeds the
  first page of the same feed in ``__NEXT_DATA__``.

No key or login is involved.  Luma's discover feed for Barcelona is a curated list
(tens of events, not hundreds), so a small result is normal.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from backend.digest.collectors import CollectorError
from backend.digest.models import DigestEvent

log = logging.getLogger(__name__)

SOURCE = "luma"
CITY_PARAMS = {
    "barcelona": {"slug": "barcelona", "place_api_id": "discplace-WcS4REeayDPXV4n"},
}

API = "https://api.lu.ma"
SITE = "https://luma.com"
PAGE_LIMIT = 50
MAX_PAGES = 10
CONCURRENCY = 5
_CURRENCY = {"eur": "€", "usd": "$", "gbp": "£"}


# ───────────────────────── pure parsing ─────────────────────────

def _amount(cents: Optional[int]) -> str:
    v = cents / 100.0
    return ("%d" % v) if float(v).is_integer() else ("%.2f" % v)


def _price(info: dict) -> Optional[str]:
    if info.get("is_free"):
        return "Free"
    p = info.get("price") or {}
    if not p or p.get("cents") is None:
        return None
    cur = (p.get("currency") or "").lower()
    sym = _CURRENCY.get(cur, (cur.upper() + " ") if cur else "")
    lo = p.get("min_cents") if p.get("min_cents") is not None else p["cents"]
    hi = p["cents"]
    mx = info.get("max_price")
    if isinstance(mx, dict) and mx.get("cents") is not None:
        hi = max(hi, mx["cents"])
    if lo == hi:
        return "%s%s" % (sym, _amount(hi))
    return "%s%s–%s" % (sym, _amount(min(lo, hi)), _amount(max(lo, hi)))


def _description(mirror: Any) -> Optional[str]:
    """Flatten a ProseMirror doc (``description_mirror``) to plain text."""
    paras: List[str] = []

    def walk(node: Any, buf: List[str]) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text":
                buf.append(node.get("text", ""))
            elif node.get("type") == "hard_break":
                buf.append("\n")
            for child in node.get("content") or []:
                walk(child, buf)
                if child.get("type") in ("paragraph", "heading", "list_item", "bullet_list", "ordered_list"):
                    buf.append("\n")

    if not mirror:
        return None
    buf: List[str] = []
    walk(mirror, buf)
    text = re.sub(r"\n{2,}", "\n", "".join(buf)).strip()
    return text or None


_CALLED_OFF = ("cancelled", "canceled", "postponed")


def called_off(*payloads: Any) -> bool:
    """True when Luma explicitly marks the event cancelled / postponed.

    No such field exists in the feed or ``event/get`` payloads as observed
    (``registration_availability`` is only open / sold-out / waitlist), so this is a
    defensive check on ``status`` / ``registration_availability`` values.
    """
    for p in payloads:
        if not isinstance(p, dict):
            continue
        for key in ("status", "registration_availability"):
            v = p.get(key)
            if isinstance(v, str) and v.strip().lower() in _CALLED_OFF:
                return True
    return False


def _why(exc: BaseException) -> str:
    text = str(exc).strip()
    return "%s: %s" % (type(exc).__name__, text) if text else type(exc).__name__


def parse_entry(entry: dict, city_tz: ZoneInfo, detail: Optional[dict] = None) -> Optional[DigestEvent]:
    """One discover-feed entry (optionally merged with its ``event/get`` detail) -> DigestEvent."""
    ev = entry.get("event") or {}
    if called_off(entry, ev, detail):
        return None
    if ev.get("location_type") not in (None, "offline"):
        return None                         # online / virtual
    name = (ev.get("name") or "").strip()
    slug = ev.get("url")
    start_s = ev.get("start_at") or entry.get("start_at")
    if not name or not slug or not start_s:
        return None
    try:
        start = datetime.fromisoformat(start_s.replace("Z", "+00:00")).astimezone(city_tz)
        end = (datetime.fromisoformat(ev["end_at"].replace("Z", "+00:00")).astimezone(city_tz)
               if ev.get("end_at") else None)
    except ValueError:
        return None

    geo = ev.get("geo_address_info") or {}
    shown = geo.get("mode") == "shown" and ev.get("geo_address_visibility") == "public"
    venue = (geo.get("address") or None) if shown else None
    address = (geo.get("full_address") or geo.get("short_address") or None) if shown else None
    area = geo.get("sublocality") or None
    if not shown and not area:
        area = geo.get("city") or None

    cal = entry.get("calendar") or {}
    hosts = [h.get("name") for h in entry.get("hosts") or [] if h.get("name")]
    cal_name = cal.get("name")
    if cal_name and cal_name != "Personal":
        organizer = cal_name
    elif hosts:
        organizer = ", ".join(hosts[:3])
    else:
        organizer = cal_name or None

    info = entry.get("ticket_info") or {}
    tags: List[str] = []
    for c in (detail or {}).get("categories") or []:
        label = c.get("name") if isinstance(c, dict) else None
        if label and label not in tags:
            tags.append(label)

    sold_out: Optional[bool] = None
    if info or entry.get("registration_availability"):
        sold_out = bool(info.get("is_sold_out")) or entry.get("registration_availability") == "sold-out"
    waitlist = bool(entry.get("waitlist_active")) or entry.get("registration_availability") == "waitlist"
    if waitlist:
        tags.append("Waitlist")
    if info.get("is_near_capacity") and not sold_out:
        tags.append("Near capacity")
    if info.get("require_approval"):
        tags.append("Approval required")

    # guest_count is reported as 0 for events that hide their guest list, so a bare 0
    # on such an event is "unknown", not "nobody".
    count = entry.get("guest_count")
    attending: Optional[int] = None
    if isinstance(count, int) and not isinstance(count, bool):
        if count > 0 or (ev.get("show_guest_list") and not ev.get("hide_rsvp")):
            attending = count

    extra: Dict[str, int] = {}
    if isinstance(info.get("spots_remaining"), int):
        extra["spots_remaining"] = info["spots_remaining"]

    return DigestEvent(
        source=SOURCE,
        title=name,
        start=start,
        end=end,
        url="%s/%s" % (SITE, slug),
        venue=venue,
        address=address,
        area=area,
        is_online=False,
        description=_description((detail or {}).get("description_mirror")),
        organizer=organizer,
        tags=tags,
        price=_price(info) if info else None,
        is_free=info.get("is_free") if "is_free" in info else None,
        sold_out=sold_out,
        attending=attending,
        attending_label="guests" if attending is not None else None,
        extra_stats=extra,
        image_url=ev.get("cover_url") or None,
        source_id=ev.get("api_id") or entry.get("api_id"),
    )


def parse_feed(payload: dict, city_tz: ZoneInfo, start: datetime, end: datetime) -> List[DigestEvent]:
    """Parse a get-paginated-events payload; keep in-person events whose start is in [start, end)."""
    out = []
    for entry in payload.get("entries") or []:
        ev = parse_entry(entry, city_tz)
        if ev is not None and start <= ev.start < end:
            out.append(ev)
    return out


def parse_next_data(html: str) -> dict:
    """Extract ``{"place": ..., "entries": [...]}`` from a luma.com/<city> page."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("no __NEXT_DATA__ in page")
    data = json.loads(m.group(1))["props"]["pageProps"]["initialData"]["data"]
    return {"place": data.get("place") or {}, "entries": data.get("events") or []}


# ───────────────────────── fetching ─────────────────────────

async def _feed_from_api(client, place_id: str, end: datetime) -> List[dict]:
    entries: List[dict] = []
    cursor = None
    for _ in range(MAX_PAGES):
        params = {"discover_place_api_id": place_id, "pagination_limit": PAGE_LIMIT}
        if cursor:
            params["pagination_cursor"] = cursor
        resp = await client.get(API + "/discover/get-paginated-events", params=params)
        if resp.status_code != 200:
            raise CollectorError("luma: discover API returned HTTP %s" % resp.status_code)
        data = resp.json()
        batch = data.get("entries") or []
        entries.extend(batch)
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor or not batch:
            break
        last = (batch[-1].get("event") or {}).get("start_at") or ""
        try:
            if datetime.fromisoformat(last.replace("Z", "+00:00")) >= end:
                break                          # feed is ascending: nothing later can be in window
        except ValueError:
            pass
    return entries


async def collect(city, start, end, client) -> List[DigestEvent]:
    params = CITY_PARAMS.get(city.key)
    if not params:
        raise CollectorError("luma: no discover place configured for %s" % city.name)
    tz = city.zone

    try:
        entries = await _feed_from_api(client, params["place_api_id"], end)
    except Exception as exc:  # noqa: BLE001 — fall back to the city page
        log.warning("luma API failed (%s); falling back to the city page (first page only, results may be incomplete)",
                    _why(exc))
        try:
            page = await client.get("https://lu.ma/%s" % params["slug"])
            if page.status_code != 200:
                raise CollectorError("luma: city page returned HTTP %s" % page.status_code)
            entries = parse_next_data(page.text)["entries"]
        except CollectorError:
            raise
        except Exception as exc2:  # noqa: BLE001
            raise CollectorError("luma: discover API and city page both failed (%s / %s)" % (_why(exc), _why(exc2)))

    chosen = []
    for entry in entries:
        ev = parse_entry(entry, tz)
        if ev is not None and start <= ev.start < end:
            chosen.append(entry)

    sem = asyncio.Semaphore(CONCURRENCY)

    async def detail(entry: dict) -> Optional[dict]:
        async with sem:
            try:
                r = await client.get(API + "/event/get", params={"event_api_id": entry["api_id"]})
                if r.status_code != 200:
                    log.warning("luma detail %s returned HTTP %d: description and categories missing",
                                entry.get("api_id"), r.status_code)
                    return None
                return r.json()
            except Exception as exc:  # noqa: BLE001 — partial failure: skip the enrichment
                log.warning("luma detail %s failed (%s): description and categories missing",
                            entry.get("api_id"), _why(exc))
                return None

    details = await asyncio.gather(*[detail(e) for e in chosen])
    events: List[DigestEvent] = []
    for entry, det in zip(chosen, details):
        ev = parse_entry(entry, tz, det)
        if ev is not None:
            events.append(ev)
    events.sort(key=lambda e: (e.start, e.title))
    return events


if __name__ == "__main__":
    from backend.digest.collectors._smoke import main
    raise SystemExit(main(__import__("sys").modules[__name__]))
