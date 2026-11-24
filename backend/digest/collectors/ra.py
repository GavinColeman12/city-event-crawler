"""Resident Advisor (ra.co) collector.

RA's HTML pages sit behind DataDome (plain HTTP gets a 765-byte "enable JS"
stub with HTTP 403), but the public GraphQL endpoint the site itself uses,
``POST https://ra.co/graphql``, answers browser-like requests without any
login or key.  One ``eventListings`` query per page of 50 returns everything
the digest needs.

Barcelona area id = 20, proven by ``{ area(id: 20) { id name urlName country
{ urlCode } } }`` -> ``Barcelona / barcelona / ES``.

``startTime`` / ``endTime`` come back as naive local timestamps
("2026-11-12T23:59:00.000", no offset) -> interpreted in the city's timezone.
RA's ``attending`` and ``interestedCount`` fields were identical on every event
observed; both are the same "interested" counter, so it is reported once.
RA does not expose a sold-out flag or a numeric price here (``tickets`` is empty
for public callers and ``cost`` is free text), so ``sold_out`` stays None and
``price`` is the promoter's own free-text cost, tidied.  A numeric cost of 0
means "Free" only when ``isTicketed`` is false; on a ticketed event it more
likely means "price not entered", so nothing is claimed.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

from backend.digest.collectors import CollectorError

logger = logging.getLogger(__name__)

SOURCE = "ra"

CITY_PARAMS = {
    "barcelona": {"area_id": 20, "url_path": "es/barcelona"},
}

GRAPHQL_URL = "https://ra.co/graphql"
PAGE_SIZE = 50
MAX_PAGES = 8  # 400 events; Barcelona has ~115 across three listing days

LISTINGS_QUERY = """
query GET_EVENT_LISTINGS($filters: FilterInputDtoInput, $pageSize: Int, $page: Int) {
  eventListings(filters: $filters, pageSize: $pageSize, page: $page) {
    totalResults
    data {
      id
      listingDate
      event {
        id
        title
        startTime
        endTime
        contentUrl
        content
        attending
        interestedCount
        isTicketed
        minimumAge
        cost
        pick { id blurb }
        images { filename type }
        venue { id name address area { name } }
        artists { id name }
        promoters { id name }
        genres { name }
      }
    }
  }
}
"""


# ── pure parsing ─────────────────────────────────────────────────────────────

def _parse_local(value: Optional[str], zone: ZoneInfo) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=zone)
    return dt.astimezone(zone)


_NUM = r"\d+(?:[.,]\d+)?"
_NUMERIC = re.compile(r"^%s$" % _NUM)
_ZERO_NUM = re.compile(r"^(?:0+(?:[.,]0+)?\s*€?|€\s*0+(?:[.,]0+)?)$")
_FREE_WORD = re.compile(r"^(?:free|gratis|gratuito|entrada libre|free entry)$", re.I)
_RANGE = re.compile(r"^€?\s*(%s)\s*€?\s*/\s*€?\s*(%s)\s*€?$" % (_NUM, _NUM))


def _clean_price(cost: Optional[str], is_ticketed: Optional[bool] = None):
    """RA ``cost`` is free text from the promoter: '', '€', '0', '10€', '22 / 25 €'.

    Returns (price, is_free).  Anything that carries no information -> (None, None).
    A zero amount is "Free" only when the event is explicitly NOT ticketed; on a
    ticketed (or unknown) event it probably means the price was never entered.
    """
    text = (cost if isinstance(cost, str) else "").strip()
    if not text or text in ("€", "$", "£") or not re.search(r"[\d]|free|gratis|libre", text, re.I):
        return None, None
    if _FREE_WORD.match(text):
        return "Free", True
    if _ZERO_NUM.match(text):
        return ("Free", True) if is_ticketed is False else (None, None)
    if _NUMERIC.match(text):
        return "€%s" % text, False
    m = _RANGE.match(text)
    if m:
        return "€%s–%s" % (m.group(1), m.group(2)), None
    return text, None


def _best_image(images: List[dict]) -> Optional[str]:
    for kind in ("FLYERFRONT", None):
        for img in images or []:
            if (kind is None or img.get("type") == kind) and img.get("filename"):
                return img["filename"]
    return None


def _clean_address(address: Optional[str]) -> Optional[str]:
    if not address:
        return None
    parts = [p.strip() for p in address.split(";") if p.strip()]
    return ", ".join(parts) or None


def _dicts(seq: Any, skipped: List[int]) -> List[dict]:
    """The dict entries of a list; anything else (None, strings) is skipped and counted."""
    out: List[dict] = []
    for item in seq if isinstance(seq, list) else []:
        if isinstance(item, dict):
            out.append(item)
        else:
            skipped[0] += 1
    return out


def _row_to_event(row: Any, zone, start: datetime, end: datetime, seen: set, skipped: List[int]):
    from backend.digest.models import DigestEvent

    ev = row.get("event") if isinstance(row, dict) else None
    if not ev or not ev.get("id") or not (ev.get("title") or "").strip():
        return None
    eid = str(ev["id"])
    if eid in seen:
        return None
    starts = _parse_local(ev.get("startTime"), zone)
    if starts is None or not (start <= starts < end):
        return None
    ends = _parse_local(ev.get("endTime"), zone)
    if ends is not None and ends < starts:
        ends = None

    venue = ev.get("venue") if isinstance(ev.get("venue"), dict) else {}
    tags = [g["name"] for g in _dicts(ev.get("genres"), skipped) if g.get("name")]
    pick = ev.get("pick") if isinstance(ev.get("pick"), dict) else None
    if pick:
        tags.append("RA Pick")
    price, is_free = _clean_price(ev.get("cost"), ev.get("isTicketed"))
    description = (ev.get("content") or "").strip() or None
    if not description and pick:
        description = (pick.get("blurb") or "").strip() or None
    attending = ev.get("attending")
    if attending is None:
        attending = ev.get("interestedCount")
    if isinstance(attending, bool) or not isinstance(attending, int) or attending < 0:
        attending = None
    extra: Dict[str, Any] = {}
    if ev.get("minimumAge"):
        extra["minimum_age"] = ev["minimumAge"]

    result = DigestEvent(
        source=SOURCE,
        source_id=eid,
        title=ev["title"].strip(),
        start=starts,
        end=ends,
        url="https://ra.co/events/%s" % eid,
        venue=(venue.get("name") or None),
        address=_clean_address(venue.get("address")),
        description=description,
        organizer=", ".join(p["name"] for p in _dicts(ev.get("promoters"), skipped) if p.get("name")) or None,
        lineup=[a["name"] for a in _dicts(ev.get("artists"), skipped) if a.get("name")],
        tags=tags,
        price=price,
        is_free=is_free,
        sold_out=None,
        attending=attending,
        attending_label="interested" if attending is not None else None,
        image_url=_best_image(_dicts(ev.get("images"), skipped)),
        extra_stats=extra,
    )
    seen.add(eid)
    return result


def parse_listings(payload: Dict[str, Any], city, start: datetime, end: datetime) -> List["DigestEvent"]:
    """Turn one GraphQL response (or an already-merged list of its rows) into events.

    Keeps only events whose start falls in ``[start, end)``; de-duplicates by
    RA event id (multi-day events repeat under every listing date).
    """
    from backend.digest.models import DigestEvent

    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        rows = ((data.get("eventListings") or {}).get("data")) or []
    elif isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        rows = payload["rows"]
    else:
        raise CollectorError("RA response has no data.eventListings")

    zone = city.zone
    seen = set()
    out: List[DigestEvent] = []
    skipped = [0]   # malformed rows / list entries, reported once below
    for row in rows:
        try:
            ev = _row_to_event(row, zone, start, end, seen, skipped)
        except Exception as exc:  # noqa: BLE001 — one odd row must not kill the collector
            skipped[0] += 1
            logger.warning("RA: skipped a malformed event row (%s)", type(exc).__name__)
            continue
        if ev is not None:
            out.append(ev)
    if skipped[0]:
        logger.warning("RA: skipped %d malformed entr%s in the listing data",
                       skipped[0], "y" if skipped[0] == 1 else "ies")
    out.sort(key=lambda e: e.start)
    return out


# ── fetching ─────────────────────────────────────────────────────────────────

def _why(exc: BaseException) -> str:
    text = str(exc).strip()
    return "%s: %s" % (type(exc).__name__, text) if text else type(exc).__name__


async def collect(city, start: datetime, end: datetime, client: httpx.AsyncClient):
    params = CITY_PARAMS.get(city.key)
    if params is None:
        raise CollectorError("RA: no area id configured for %s" % city.key)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://ra.co",
        "Referer": "https://ra.co/events/%s" % params["url_path"],
    }
    variables = {
        "filters": {
            "areas": {"eq": params["area_id"]},
            # listingDate is the evening's calendar date, so the end day is
            # needed too: a 02:00 start the morning after belongs to it.
            "listingDate": {
                "gte": "%sT00:00:00.000Z" % start.date().isoformat(),
                "lte": "%sT00:00:00.000Z" % end.date().isoformat(),
            },
        },
        "pageSize": PAGE_SIZE,
        "page": 1,
    }

    rows: List[dict] = []
    total = None
    for page in range(1, MAX_PAGES + 1):
        variables["page"] = page
        try:
            resp = await client.post(
                GRAPHQL_URL, headers=headers,
                json={"operationName": "GET_EVENT_LISTINGS", "query": LISTINGS_QUERY, "variables": variables},
            )
        except httpx.HTTPError as exc:
            if page == 1:
                raise CollectorError("RA GraphQL request failed: %s" % _why(exc))
            logger.warning("RA page %d failed (%s): results may be incomplete", page, _why(exc))
            break
        if resp.status_code != 200:
            if page == 1:
                hint = " (DataDome bot block)" if resp.headers.get("x-datadome") or "datadome" in resp.text[:400].lower() else ""
                raise CollectorError("RA GraphQL returned HTTP %d%s" % (resp.status_code, hint))
            logger.warning("RA page %d returned HTTP %d: results may be incomplete", page, resp.status_code)
            break
        try:
            body = resp.json()
        except ValueError:
            if page == 1:
                raise CollectorError("RA GraphQL returned non-JSON: %s" % resp.text[:120].replace("\n", " "))
            logger.warning("RA page %d returned non-JSON: results may be incomplete", page)
            break
        if not isinstance(body, dict):
            if page == 1:
                raise CollectorError("RA GraphQL returned an unexpected response shape")
            logger.warning("RA page %d returned an unexpected response shape: results may be incomplete", page)
            break
        if body.get("errors") and not (body.get("data") or {}).get("eventListings"):
            first = body["errors"][0] if isinstance(body["errors"], list) and body["errors"] else {}
            msg = str(first.get("message") if isinstance(first, dict) else first)[:160]
            if page == 1:
                raise CollectorError("RA GraphQL error: %s" % msg)
            logger.warning("RA page %d GraphQL error (%s): results may be incomplete", page, msg)
            break
        listings = (body.get("data") or {}).get("eventListings") or {}
        page_rows = listings.get("data") or []
        total = listings.get("totalResults", total)
        rows.extend(page_rows)
        if len(page_rows) < PAGE_SIZE or (total is not None and len(rows) >= total):
            break

    return parse_listings({"rows": rows}, city, start, end)


if __name__ == "__main__":
    from backend.digest.collectors._smoke import main
    raise SystemExit(main(__import__("sys").modules[__name__]))
