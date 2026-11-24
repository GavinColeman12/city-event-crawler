"""Eventbrite collector (Barcelona and any city with a Who's-On-First place id).

How it works
------------
Eventbrite's HTML listing pages (``/d/spain--barcelona/...``) sit behind an AWS
WAF captcha and are NOT used.  The site's own front-end instead calls a JSON
endpoint::

    POST https://www.eventbrite.com/api/v3/destination/search/

which requires the anonymous ``csrftoken`` cookie that every browser receives
from a plain ``GET https://www.eventbrite.com/`` (echoed back in the
``X-CSRFToken`` header).  No login, no API key.

``event_search.places`` takes a Who's-On-First locality id (Barcelona =
101748323) and ``date_range`` an inclusive ``{"from", "to"}`` pair of dates.
Each result is expanded with venue, organizer (incl. ``num_followers``),
ticket availability / price, sales status and urgency signals.
"""
from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from backend.digest.collectors import CollectorError
from backend.digest.models import DigestEvent

log = logging.getLogger(__name__)

SOURCE = "eventbrite"
CITY_PARAMS = {
    # place_id = Who's-On-First locality id used by Eventbrite's "places" filter.
    "barcelona": {"place_id": "101748323", "slug": "spain--barcelona", "radius_km": 25.0},
}

BASE = "https://www.eventbrite.com"
SEARCH_URL = BASE + "/api/v3/destination/search/"
PAGE_SIZE = 50
MAX_PAGES = 12          # ~2x a busy weekend (today ~6 pages of 50); more is logged as truncated
EXTRA_PASSES = 2        # extra sequential passes to pick up stragglers when short of object_count
# Eventbrite's own pages come back a row or two short (49/44/49 of 50), so about
# 3% of object_count is never returned on any pass.  Only a gap beyond that
# baseline is treated as missing data: it triggers the extra passes and, if it
# persists, the warning.
SHORT_TOLERANCE = 0.05
TIME_BUDGET_S = 12.0    # do not start another extra pass after this many seconds
SORT = "distance"       # "date" is accepted too but is LESS stable; anything else is a 400
EXPAND = [
    "event_sales_status", "image", "primary_venue", "saves",
    "ticket_availability", "primary_organizer",
]
_CURRENCY = {"EUR": "€", "USD": "$", "GBP": "£"}
_URGENCY_LABELS = {
    "goingFast": "Going fast",
    "fewTickets": "Few tickets left",
    "salesEndSoon": "Sales end soon",
    "popular": "Popular",
    "sellingFast": "Selling fast",
    "almostFull": "Almost full",
}
_AREA_TYPES = ("neighbourhood", "neighborhood", "macrohood", "borough")


# ───────────────────────── pure parsing ─────────────────────────

def _money(node: Optional[dict]) -> Optional[float]:
    try:
        return float(node["major_value"])
    except (TypeError, KeyError, ValueError):
        return None


def _fmt_amount(value: float) -> str:
    return ("%d" % value) if float(value).is_integer() else ("%.2f" % value)


def _price(avail: dict) -> Optional[str]:
    if avail.get("is_free"):
        return "Free"
    lo = _money(avail.get("minimum_ticket_price"))
    hi = _money(avail.get("maximum_ticket_price"))
    if lo is None and hi is None:
        return None
    cur = ((avail.get("minimum_ticket_price") or avail.get("maximum_ticket_price")) or {}).get("currency", "")
    sym = _CURRENCY.get(cur, cur + " " if cur else "")
    if lo is None or hi is None or lo == hi:
        return "%s%s" % (sym, _fmt_amount(lo if lo is not None else hi))
    return "%s%s–%s" % (sym, _fmt_amount(lo), _fmt_amount(hi))


def _clean_url(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def _split_camel(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", s).capitalize()


def _parse_dt(date_s: Optional[str], time_s: Optional[str], src_tz: ZoneInfo, city_tz: ZoneInfo) -> Optional[datetime]:
    if not date_s:
        return None
    try:
        naive = datetime.strptime("%s %s" % (date_s, time_s or "00:00"), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=src_tz).astimezone(city_tz)


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 12742.0 * math.asin(math.sqrt(a))


def _in_city(raw: dict, place_id: str, center: Optional[tuple]) -> bool:
    """Eventbrite's ``places`` filter leaks the wider region (Girona, Lleida, ...).

    Keep an event if its venue lies within the radius of the city centre; with no
    coordinates, require the city's locality id in the event's location chain.
    """
    addr = (raw.get("primary_venue") or {}).get("address") or {}
    try:
        lat, lon = float(addr["latitude"]), float(addr["longitude"])
        if center is not None:
            return _km(center[0], center[1], lat, lon) <= center[2]
    except (KeyError, TypeError, ValueError):
        pass
    return any(l.get("id") == place_id and l.get("type") == "locality" for l in raw.get("locations") or [])


def parse_event(raw: dict, city_tz: ZoneInfo) -> Optional[DigestEvent]:
    """One ``destination_event`` object -> DigestEvent (None if unusable / online)."""
    if raw.get("is_online_event") or raw.get("is_cancelled"):
        return None
    name = (raw.get("name") or "").strip()
    url = raw.get("url")
    if not name or not url:
        return None
    try:
        src_tz = ZoneInfo(raw.get("timezone") or city_tz.key)
    except Exception:  # noqa: BLE001
        src_tz = city_tz
    start = _parse_dt(raw.get("start_date"), raw.get("start_time"), src_tz, city_tz)
    if start is None:
        return None
    end = _parse_dt(raw.get("end_date"), raw.get("end_time"), src_tz, city_tz)

    venue = raw.get("primary_venue") or {}
    addr = venue.get("address") or {}
    organizer = raw.get("primary_organizer") or {}
    avail = raw.get("ticket_availability") or {}
    sales = raw.get("event_sales_status") or {}

    area = None
    for loc in raw.get("locations") or []:
        if loc.get("type") in _AREA_TYPES:
            area = loc.get("name")

    tags: List[str] = []
    for t in raw.get("tags") or []:
        if t.get("prefix") in ("EventbriteCategory", "EventbriteSubCategory", "EventbriteFormat"):
            label = t.get("display_name")
            if label and label not in tags:
                tags.append(label)
    signals = (raw.get("urgency_signals") or {}).get("messages") or []
    for m in signals:
        label = _URGENCY_LABELS.get(m) or _split_camel(m)
        if label not in tags:
            tags.append(label)

    # "sales_ended" also covers events that have simply started, so it is surfaced as a
    # tag only; sold_out is True strictly when Eventbrite reports the status "sold_out".
    sold_out: Optional[bool] = None
    status = sales.get("sales_status")
    if status == "sold_out":
        sold_out = True
    elif status == "on_sale":
        sold_out = False
    elif status == "sales_ended" and "Sales ended" not in tags:
        tags.append("Sales ended")

    followers = organizer.get("num_followers")
    if isinstance(followers, bool) or not isinstance(followers, int) or followers < 0:
        followers = None

    img = raw.get("image") or {}
    image_url = (img.get("original") or {}).get("url") or img.get("url")

    return DigestEvent(
        source=SOURCE,
        title=name,
        start=start,
        end=end,
        url=_clean_url(url),
        venue=venue.get("name") or None,
        address=addr.get("localized_address_display") or None,
        area=area,
        is_online=False,
        description=(raw.get("summary") or None),
        organizer=organizer.get("name") or None,
        tags=tags,
        price=_price(avail) if avail else None,
        is_free=avail.get("is_free") if "is_free" in avail else None,
        sold_out=sold_out,
        organizer_followers=followers,
        image_url=image_url,
        source_id=str(raw.get("id") or raw.get("eid") or "") or None,
    )


def parse_search(payload: dict, city_tz: ZoneInfo, start: datetime, end: datetime,
                 place_id: str = "101748323", center: Optional[tuple] = None) -> List[DigestEvent]:
    """Parse one search response page; keep in-person events in the city whose start is in [start, end).

    ``center`` = (lat, lon, radius_km) enables the distance filter.
    """
    out: List[DigestEvent] = []
    events = (payload.get("events") or {})
    for raw in (events.get("results") or []) + (events.get("promoted_results") or []):
        try:
            if not _in_city(raw, place_id, center):
                continue
            ev = parse_event(raw, city_tz)
        except Exception as exc:  # noqa: BLE001 — one malformed row must not kill the collector
            log.debug("eventbrite: skipped a malformed result (%s)", type(exc).__name__)
            continue
        if ev is not None and start <= ev.start < end:
            out.append(ev)
    return out


# ───────────────────────── fetching ─────────────────────────

def _body(place_id: str, start: datetime, end: datetime, page: int) -> Dict[str, Any]:
    # date_range is inclusive of whole local days; the 06:00 cut is applied when parsing.
    return {
        "event_search": {
            "dates": "current_future",
            "date_range": {"from": start.date().isoformat(), "to": end.date().isoformat()},
            "places": [place_id],
            "sort": SORT,
            "online_events_only": False,
            "page": page,
            "page_size": PAGE_SIZE,
        },
        "expand.destination_event": EXPAND,
    }


def _row_key(raw: Any) -> Optional[str]:
    if not isinstance(raw, dict):
        return None
    key = raw.get("id") or raw.get("eid") or raw.get("url")
    return str(key) if key else None


async def collect(city, start, end, client) -> List[DigestEvent]:
    params = CITY_PARAMS.get(city.key)
    if not params:
        raise CollectorError("eventbrite: no place id configured for %s" % city.name)

    try:
        home = await client.get(BASE + "/")
    except Exception as exc:  # noqa: BLE001
        raise CollectorError("eventbrite: homepage unreachable (%s)" % exc)
    token = client.cookies.get("csrftoken")
    if home.status_code != 200 or not token:
        raise CollectorError(
            "eventbrite: could not obtain anonymous CSRF cookie (HTTP %s, waf=%s)"
            % (home.status_code, home.headers.get("x-amzn-waf-action")))
    headers = {
        "Referer": BASE + "/",          # the page that actually issued the CSRF cookie
        "Origin": BASE,
        "X-CSRFToken": token,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json",
    }
    tz = city.zone
    center = (city.lat, city.lon, float(params.get("radius_km", 25.0)))

    async def fetch(page: int) -> dict:
        resp = await client.post(SEARCH_URL, json=_body(params["place_id"], start, end, page), headers=headers)
        if resp.status_code != 200:
            raise CollectorError("eventbrite: search page %d returned HTTP %s %s"
                                 % (page, resp.status_code, resp.text[:120].replace("\n", " ")))
        try:
            return resp.json()
        except ValueError:
            raise CollectorError("eventbrite: search page %d was not JSON (blocked?)" % page)

    rows: Dict[str, dict] = {}

    def absorb(payload: dict) -> int:
        """Merge one page's raw rows into ``rows`` (de-duplicated by id); return how many were new."""
        added = 0
        events = payload.get("events") or {}
        for raw in (events.get("results") or []) + (events.get("promoted_results") or []):
            key = _row_key(raw)
            if key is None:
                # unusable id: keep it under a unique key so parse_search can still try it
                key = "anon-%d" % len(rows)
            if key not in rows:
                rows[key] = raw
                added += 1
        return added

    t0 = time.monotonic()
    first = await fetch(1)          # a failure here is a CollectorError for the whole source
    pag = (first.get("events") or {}).get("pagination") or {}
    try:
        object_count = int(pag.get("object_count") or 0)
        page_count = int(pag.get("page_count") or 1)
    except (TypeError, ValueError):
        object_count, page_count = 0, 1
    if page_count > MAX_PAGES:
        log.warning("eventbrite: results truncated — %d pages available, only the first %d fetched "
                    "(%d pages skipped)", page_count, MAX_PAGES, page_count - MAX_PAGES)
        page_count = MAX_PAGES
    absorb(first)

    async def sweep(first_page: int) -> int:
        added = 0
        for page in range(first_page, page_count + 1):
            try:
                added += absorb(await fetch(page))
            except Exception as exc:  # noqa: BLE001 — partial failure: log, keep going
                log.warning("eventbrite: page %d failed (%s) — some events may be missing",
                            page, type(exc).__name__)
        return added

    await sweep(2)
    # Eventbrite's page order is not perfectly stable, so a pass can miss rows another returns.
    for _ in range(EXTRA_PASSES):
        enough = len(rows) >= object_count * (1 - SHORT_TOLERANCE)
        if not object_count or enough or time.monotonic() - t0 > TIME_BUDGET_S:
            break
        added = await sweep(1)
        if not added:
            break                    # converged: another pass would only repeat itself

    log.info("eventbrite: fetched %d unique of %d reported results in %.1fs",
             len(rows), object_count, time.monotonic() - t0)
    if object_count and len(rows) < object_count * (1 - SHORT_TOLERANCE):
        log.warning("eventbrite: got %d of %d results — some events are missing", len(rows), object_count)

    events = parse_search({"events": {"results": list(rows.values())}}, tz, start, end,
                          params["place_id"], center)
    events.sort(key=lambda e: (e.start, e.title))
    return events


if __name__ == "__main__":
    from backend.digest.collectors._smoke import main
    raise SystemExit(main(__import__("sys").modules[__name__]))
