"""DICE (dice.fm) collector.

How it works
------------
dice.fm is a Next.js site that server-renders its data into a
``<script id="__NEXT_DATA__">`` JSON blob.  No API key, login or cookie is used.

1. LISTING  ``https://dice.fm/browse/<city-slug>/<group>/<category>`` for every
   primary category (gigs, DJ, party, film, art, ...).  Each category page
   carries up to ~25 events sorted by start date, so the first page covers the
   near future.  (The un-filtered ``/browse/<slug>`` page is a "popular" mix
   that is NOT date ordered, so it is not used.)  The listing has title, start/end, venue, price, status
   and image but no lineup / promoter / tags / canonical URL.
2. DETAIL   ``https://dice.fm/event/<id>`` (redirects to the canonical slug URL)
   for each event inside the window only, giving lineup, promoter
   (``billing_promoter``), genre tags, description and the canonical
   ``perm_name`` used to build the public URL.

Pagination: category pages are capped (~25 events).  DICE's own "load more"
calls ``POST api.dice.fm/unified_search`` with the page's ``nextCursor``; that
works, but the API answered 403 after a short burst of requests, so it is NOT
used here.  (The ``_next/data`` route 404s and a ``?cursor=`` query on the page
is ignored.)  Instead, a capped category is widened by fetching ALL of its
sub-genre pages, bounded by ``MAX_LISTING_REQUESTS`` and reported with WARNING
records when coverage cannot be guaranteed.

Cancelled / postponed: the only statuses observed are ``on-sale`` and
``sold-out``.  Events whose ``status`` is cancelled/postponed are skipped (a
postponed event's listed date is no longer valid); no status field was seen for
anything else.

Popularity: DICE exposes NO attendance / interested / saved counts on public
pages, so ``attending`` stays ``None``.  The only demand signal is the event
status (``sold-out``), reported through ``sold_out``.

Fragility: the city slug embeds DICE's city id (``CITY_PARAMS``).  If a page
fails to report that same city id the collector raises rather than return
events from some other place.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

from backend.digest.collectors import CollectorError
from backend.digest.models import DigestEvent

log = logging.getLogger(__name__)

SOURCE = "dice"

CITY_PARAMS = {
    "barcelona": {
        "slug": "barcelona-5d8cefe1e3e6e374e99e8cbe",
        "city_id": "5d8cefe1e3e6e374e99e8cbe",
        "city_name": "Barcelona",
    },
}

BASE = "https://dice.fm"
# Primary browse categories as DICE publishes them (pageProps.primaryFilters).
CATEGORIES = (
    "music/gig", "music/dj", "music/party",
    "culture/film", "culture/art", "culture/podcast",
    "culture/theatre", "culture/social", "culture/talks",
)
MAX_CONCURRENCY = 6
# Hard ceiling on listing-page requests (categories + sub-genres) per run.
MAX_LISTING_REQUESTS = 150
# Statuses DICE could publish for an event that is not happening as listed.
# Only "on-sale" and "sold-out" were observed live; these are matched defensively.
_CANCELLED_STATUSES = {"cancelled", "canceled"}
_POSTPONED_STATUSES = {"postponed", "rescheduled"}

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
_CURRENCY_SYMBOLS = {"EUR": "€", "GBP": "£", "USD": "$"}


# ───────────────────────── pure parsing ─────────────────────────

def extract_next_data(html: str) -> dict:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise ValueError("__NEXT_DATA__ not found")
    return json.loads(m.group(1))


def parse_listing_page(next_data: dict) -> dict:
    """Return ``{"city": {...}, "events": [...], "next_cursor": str|None, "sub_links": [...]}``."""
    pp = (next_data.get("props") or {}).get("pageProps") or {}
    sub_links = [f.get("activateLink") for f in (pp.get("secondaryFilters") or []) if f.get("activateLink")]
    return {
        "city": pp.get("city") or {},
        "events": pp.get("events") or [],
        "next_cursor": pp.get("nextCursor"),
        "sub_links": sub_links,
    }


def parse_detail_page(next_data: dict) -> dict:
    """Return the detail event dict with the lineup attached as ``_lineup``."""
    pp = (next_data.get("props") or {}).get("pageProps") or {}
    state = pp.get("initialState")
    if isinstance(state, str):
        state = json.loads(state)
    block = (state or {}).get("event") or {}
    event = block.get("event")
    if not event:
        raise ValueError("no event in initialState")
    out = dict(event)
    out["_lineup"] = block.get("lineup") or []
    return out


def _parse_dt(value: Any, zone) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=zone)
    return dt.astimezone(zone)


def _money(minor: Any, currency: str) -> str:
    amount = minor / 100.0
    text = ("%d" % amount) if float(amount).is_integer() else ("%.2f" % amount)
    symbol = _CURRENCY_SYMBOLS.get(currency)
    return ("%s%s" % (symbol, text)) if symbol else ("%s %s" % (text, currency))


def format_price(price: Optional[dict]) -> Tuple[Optional[str], Optional[bool]]:
    """DICE stores minor units.  ``amount`` = single price, ``amount_from`` = cheapest of several."""
    if not price:
        return None, None
    currency = price.get("currency") or "EUR"
    amount, amount_from = price.get("amount"), price.get("amount_from")
    if amount is not None:
        return ("Free", True) if amount == 0 else (_money(amount, currency), False)
    if amount_from is not None:
        return ("Free", True) if amount_from == 0 else ("From %s" % _money(amount_from, currency), False)
    return None, None


def _lineup_names(detail: Optional[dict]) -> List[str]:
    if not detail:
        return []
    names: List[str] = []
    for entry in detail.get("_lineup") or []:
        name = (entry.get("name") or "").strip() if isinstance(entry, dict) else ""
        if name and name not in names:
            names.append(name)
    if not names:
        for entry in (detail.get("summary_lineup") or {}).get("top_artists") or []:
            name = (entry.get("name") or "").strip()
            if name and name not in names:
                names.append(name)
    return names


def _image(images: Any) -> Optional[str]:
    if isinstance(images, dict):
        for key in ("landscape", "square", "portrait"):
            if images.get(key):
                return images[key]
    return None


def build_event(listing: dict, detail: Optional[dict], zone) -> Optional[DigestEvent]:
    """Merge a listing entry with its (optional) detail payload into a DigestEvent."""
    dates = listing.get("dates") or {}
    start = _parse_dt(dates.get("event_start_date"), zone)
    title = (listing.get("name") or "").strip()
    if start is None or not title:
        return None
    detail = detail or {}
    end = _parse_dt(dates.get("event_end_date"), zone)
    venues = detail.get("venues") or listing.get("venues") or []
    venue = venues[0] if venues else {}
    price, is_free = format_price(detail.get("price") or listing.get("price"))

    status = detail.get("status") or listing.get("status")
    if is_called_off(listing, detail):
        return None
    sold_out = (status == "sold-out") if status else None
    tags = [t.get("title") or t.get("name") for t in (detail.get("tags_types") or []) if isinstance(t, dict)]
    tags = [t for t in tags if t]
    if sold_out and detail.get("secondary_status") == "waiting-list":
        tags.append("Waiting list")

    perm = detail.get("perm_name")
    url = "%s/event/%s" % (BASE, perm or listing.get("id"))
    promoter = (detail.get("billing_promoter") or {}).get("name")
    description = ((detail.get("about") or {}).get("description") or "").strip() or None

    return DigestEvent(
        source=SOURCE,
        title=title,
        start=start,
        end=end,
        url=url,
        venue=venue.get("name"),
        address=venue.get("address"),
        description=description,
        organizer=promoter,
        lineup=_lineup_names(detail),
        tags=tags,
        price=price,
        is_free=is_free,
        sold_out=sold_out,
        image_url=_image(detail.get("images")) or _image(listing.get("images")),
        source_id=str(listing.get("id")) if listing.get("id") else None,
    )


def is_called_off(listing: dict, detail: Optional[dict] = None) -> bool:
    """True when DICE marks the event cancelled or postponed (either payload)."""
    for payload in (listing, detail or {}):
        status = payload.get("status") if isinstance(payload, dict) else None
        if isinstance(status, str) and status.strip().lower() in (_CANCELLED_STATUSES | _POSTPONED_STATUSES):
            return True
    return False


def _listing_start(listing: Any, zone) -> Optional[datetime]:
    """Start of a listing row, or None for anything malformed (never raises)."""
    try:
        return _parse_dt((listing.get("dates") or {}).get("event_start_date"), zone)
    except Exception:
        return None


def in_window(listing: dict, zone, start: datetime, end: datetime) -> bool:
    dt = _listing_start(listing, zone)
    return dt is not None and start <= dt < end


def _is_city(listing: dict, city_id: str) -> bool:
    try:
        venues = listing.get("venues") or []
        return any(((v.get("city") or {}).get("id") == city_id) for v in venues)
    except Exception:
        return False


def parse_events(listings: List[dict], details: Dict[str, dict], city, start: datetime,
                 end: datetime) -> List[DigestEvent]:
    """Pure: de-dupe listing entries, filter to the window, merge details, sort by start."""
    params = CITY_PARAMS[city.key]
    seen: Dict[str, dict] = {}
    skipped = 0
    for item in listings:
        try:
            if item.get("id") and item["id"] not in seen:
                seen[item["id"]] = item
        except Exception:
            skipped += 1
    out: List[DigestEvent] = []
    for item in seen.values():
        try:
            if not in_window(item, city.zone, start, end) or not _is_city(item, params["city_id"]):
                continue
            ev = build_event(item, details.get(item["id"]), city.zone)
        except Exception as exc:
            skipped += 1
            log.warning("dice: skipped malformed event %s (%s)", item.get("id"), type(exc).__name__)
            continue
        if ev is not None and start <= ev.start < end:
            out.append(ev)
    if skipped:
        log.warning("dice: skipped %d malformed listing row(s)", skipped)
    out.sort(key=lambda e: (e.start, e.title))
    return out


# ───────────────────────── fetching ─────────────────────────

async def _get_next_data(client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        resp = await client.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
    if resp.status_code != 200:
        raise CollectorError("DICE returned HTTP %s for %s" % (resp.status_code, url))
    try:
        return extract_next_data(resp.text)
    except ValueError as exc:
        raise CollectorError("DICE page has no parsable data (%s): %s" % (exc, url))


def _why(exc: BaseException) -> str:
    """Short, non-empty description of an exception (httpx timeouts have an empty str())."""
    text = str(exc).strip()
    return "%s: %s" % (type(exc).__name__, text) if text else type(exc).__name__


async def collect(city, start: datetime, end: datetime, client: httpx.AsyncClient) -> List[DigestEvent]:
    params = CITY_PARAMS.get(city.key)
    if not params:
        raise CollectorError("DICE: no city config for %r" % city.key)
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    browse = "%s/browse/%s" % (BASE, params["slug"])

    async def listing(path: str) -> dict:
        page = parse_listing_page(await _get_next_data(client, "%s/%s" % (browse, path.lstrip("/")), sem))
        got = page["city"].get("id")
        if got != params["city_id"]:
            raise CollectorError("DICE page %s reports city %r, expected %s" % (path, page["city"].get("name"), params["city_name"]))
        return page

    def capped_before_end(page: dict) -> bool:
        """A full page (nextCursor set) whose last event still starts before the window end."""
        if not page["next_cursor"] or not page["events"]:
            return False
        last = max((_listing_start(e, city.zone) or start) for e in page["events"])
        return last < end

    results = await asyncio.gather(*(listing(p) for p in CATEGORIES), return_exceptions=True)
    pages = [r for r in results if isinstance(r, dict)]
    failed = [(path, r) for path, r in zip(CATEGORIES, results) if isinstance(r, Exception)]
    for path, r in failed:
        log.warning("dice: category %s failed (%s): results may be incomplete", path, _why(r))
    if not pages:
        raise CollectorError("DICE: every listing page failed (%s)" % (_why(failed[0][1]) if failed else "no data"))

    # A page is date-sorted but capped (~25 events).  If it is capped before the
    # window end, widen with ALL of its sub-genre pages so nothing in the window is lost.
    extra_paths: List[str] = []
    capped_cats: List[str] = []
    for path, r in zip(CATEGORIES, results):
        if isinstance(r, dict) and capped_before_end(r):
            capped_cats.append(path)
            for l in r["sub_links"]:
                if params["slug"] in l:
                    sub = l.split(params["slug"], 1)[-1]
                    if sub not in extra_paths and sub.strip("/") not in CATEGORIES:
                        extra_paths.append(sub)
    if capped_cats:
        budget = max(0, MAX_LISTING_REQUESTS - len(CATEGORIES))
        if len(extra_paths) > budget:
            log.warning("dice: %d sub-genre pages needed but request ceiling allows %d: results may be incomplete",
                        len(extra_paths), budget)
            extra_paths = extra_paths[:budget]
        extra = await asyncio.gather(*(listing(p) for p in extra_paths), return_exceptions=True)
        sub_failed = [(p, r) for p, r in zip(extra_paths, extra) if isinstance(r, Exception)]
        if sub_failed:
            log.warning("dice: %d of %d sub-genre pages failed (e.g. %s: %s): results may be incomplete",
                        len(sub_failed), len(extra_paths), sub_failed[0][0], _why(sub_failed[0][1]))
        ok = [(p, r) for p, r in zip(extra_paths, extra) if isinstance(r, dict)]
        pages += [r for _, r in ok]
        still_capped = [p for p, r in ok if capped_before_end(r)]
        if still_capped:
            log.warning("dice: %d sub-genre page(s) are still capped before the window end (e.g. %s): "
                        "some events may be missing", len(still_capped), still_capped[0])

    listings = [e for p in pages for e in p["events"]]
    wanted = {}
    for e in listings:
        if e.get("id") and e["id"] not in wanted and in_window(e, city.zone, start, end) and _is_city(e, params["city_id"]):
            wanted[e["id"]] = e

    async def detail(event_id: str) -> Tuple[str, Optional[dict]]:
        try:
            return event_id, parse_detail_page(await _get_next_data(client, "%s/event/%s" % (BASE, event_id), sem))
        except (CollectorError, ValueError, httpx.HTTPError) as exc:
            detail_errors.append((event_id, exc))
            return event_id, None

    detail_errors: List[Tuple[str, Exception]] = []
    details = {eid: d for eid, d in await asyncio.gather(*(detail(i) for i in wanted)) if d}
    if detail_errors:
        log.warning("dice: %d of %d event detail pages failed (%s): lineup/promoter missing for those",
                    len(detail_errors), len(wanted), _why(detail_errors[0][1]))
    return parse_events(listings, details, city, start, end)


if __name__ == "__main__":
    from backend.digest.collectors._smoke import main
    raise SystemExit(main(__import__("sys").modules[__name__]))
