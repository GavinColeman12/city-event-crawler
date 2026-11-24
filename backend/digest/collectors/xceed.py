"""Xceed (xceed.me) collector — Barcelona's main club / nightlife ticketing platform.

How it works (verified live 2026-11-12 by reading what xceed.me itself loads)
----------------------------------------------------------------------------
The public site is a Next.js app whose browser code calls these anonymous JSON
endpoints.  No login, cookie, token or API key is needed — only the browser-like
headers every collector already sends:

* LISTING  ``GET https://event-b2c.xceed.me/v2/events``
  params: ``cities[]=barcelona  limit=100  offset=N  startTime=<epoch>
  orderBy=date  sort=ASC``  →  ``{"success", "data": {"items": [...], "meta":
  {"total"}}}``.  ``startTime`` hides events that are already over; ``offset``
  paginates (max limit tried: 100).  Items carry the epoch ``startingTime`` /
  ``endingTime`` (UTC seconds), venue, lineup and music genres, but NO price.
  We page in date order and stop at the first event past the window end.
* DETAIL   ``GET https://event-b2c.xceed.me/v1/events/<legacyId>?lang=en``
  → description, venue address, promoter/company, age policy.
* OFFERS   ``GET https://offer-b2c.xceed.me/v1/events/<event uuid>/offers?lang=en``
  → ticket / guest-list / bottle-service offers: price, quantity, sold-out
  state, per-offer quantity.  (Needs the UUID, not the numeric id.)
* VENUE    ``GET https://event-b2c.xceed.me/v1/venues/<slug>`` → ``followersCount``.
* POPULAR  the listing again with ``orderBy=popular sort=DESC`` — Xceed's own
  popularity ranking, used to record each event's rank.

Xceed publishes NO attendance / RSVP / interested count, so ``attending`` is
always ``None``.  The popularity signals it does expose: the venue's follower count
(kept only in ``extra_stats["venue_followers"]``, never attributed to the organiser),
the summed ticket ``quantity`` (``extra_stats["ticket_stock"]`` — not established to
mean REMAINING stock, so it is never turned into a "last tickets" claim) and its own
popularity rank.

Cancelled / postponed: no such field exists in the listing, detail or offers
payloads (offer ``status`` is only ``on_sale`` / ``sold_out``).  ``called_off`` below
checks defensively for ``status`` / ``isCancelled`` / ``isPostponed`` keys in case
Xceed adds them; none were ever observed.

Fragility: these hosts are Xceed's private frontend API, not a documented public
API — parameter names or response shapes can change without notice.  There was no
bot protection or rate limiting at ~50 requests per run.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from backend.digest.collectors import CollectorError
from backend.digest.models import DigestEvent

log = logging.getLogger(__name__)

SOURCE = "xceed"

CITY_PARAMS = {
    "barcelona": {"slug": "barcelona"},
}

EVENT_API = "https://event-b2c.xceed.me"
OFFER_API = "https://offer-b2c.xceed.me"
SITE = "https://xceed.me"

PAGE_LIMIT = 100
MAX_PAGES = 6          # 600 events — far beyond any 2-day window
CONCURRENCY = 6
API_HEADERS = {"Accept": "application/json", "Accept-Language": "en", "Referer": SITE + "/"}

_CURRENCY_SYMBOLS = {"EUR": "€", "GBP": "£", "USD": "$"}


# ───────────────────────── pure parsing ─────────────────────────

def _epoch(value: Any, zone) -> Optional[datetime]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value, tz=zone)


def _money(amount: Any, currency: str) -> str:
    sym = _CURRENCY_SYMBOLS.get(currency or "EUR", (currency or "EUR") + " ")
    if float(amount) == int(amount):
        return "%s%d" % (sym, int(amount))
    return "%s%.2f" % (sym, amount)


def parse_listing(payload: Any) -> Tuple[List[dict], Optional[int]]:
    """Return ``(items, total)`` from a ``/v2/events`` response."""
    try:
        data = payload["data"]
        items = data["items"]
    except (KeyError, TypeError):
        raise CollectorError("Xceed listing had an unexpected shape")
    if not isinstance(items, list):
        raise CollectorError("Xceed listing had an unexpected shape")
    total = (data.get("meta") or {}).get("total")
    return items, total if isinstance(total, int) else None


def _ticket_offers(offers: Optional[dict]) -> List[dict]:
    """Door-entry offers (tickets + guest lists); bottle service / tables are excluded."""
    if not isinstance(offers, dict):
        return []
    out: List[dict] = []
    for kind in ("ticket", "guestList"):
        for offer in offers.get(kind) or []:
            if isinstance(offer, dict):
                out.append(dict(offer, _kind=kind))
    return out


def _is_guest_offer(offer: dict) -> bool:
    return offer.get("_kind") == "guestList" or "guest" in (offer.get("name") or "").lower()


def called_off(*payloads: Any) -> bool:
    """True when a payload explicitly says the event is cancelled / postponed (none seen live)."""
    for p in payloads:
        if not isinstance(p, dict):
            continue
        status = p.get("status")
        if isinstance(status, str) and status.strip().lower() in ("cancelled", "canceled", "postponed"):
            return True
        if p.get("isCancelled") is True or p.get("isPostponed") is True:
            return True
    return False


def summarize_offers(offers: Optional[dict]) -> dict:
    """Price text, free flag, sold-out state and stock facts from an offers payload."""
    result: dict = {"price": None, "is_free": None, "sold_out": None,
                    "free_option": False, "ticket_stock": None}
    ticket_offers = _ticket_offers(offers)
    if not ticket_offers:
        return result

    on_sale = [o for o in ticket_offers if o.get("status") == "on_sale"]
    sold_out = [o for o in ticket_offers if o.get("status") == "sold_out"
                or (o.get("salesStatus") or {}).get("isSoldOut")]
    if on_sale:
        result["sold_out"] = False
    elif sold_out:
        result["sold_out"] = True

    priced = on_sale or ticket_offers
    amounts: List[Tuple[float, str, bool]] = []
    for o in priced:
        price = o.get("price") or {}
        if isinstance(price.get("amount"), (int, float)):
            is_guest = _is_guest_offer(o)
            amounts.append((float(price["amount"]), price.get("currency") or "EUR", is_guest))
    if amounts:
        currency = amounts[0][1]
        free = [a for a in amounts if a[0] == 0]
        paid = sorted(a[0] for a in amounts if a[0] > 0)
        result["free_option"] = bool(free)
        free_label = "Free guest list" if any(a[2] for a in free) else "Free entry"
        if free and not paid:
            result["price"], result["is_free"] = free_label, True
        else:
            result["is_free"] = False
            if paid[0] == paid[-1]:
                paid_text = _money(paid[0], currency)
            else:
                paid_text = "%s–%s" % (_money(paid[0], currency), _money(paid[-1], currency).lstrip("€£$"))
            result["price"] = "%s · paid %s" % (free_label, paid_text) if free else paid_text

    # Sum of ``quantity`` over on-sale PAID-TICKET offers only (guest lists are excluded).
    # Whether ``quantity`` is remaining stock is not established, so this is reported
    # as "ticket_stock" and never used to claim scarcity.
    stock_offers = [o for o in on_sale if not _is_guest_offer(o)]
    quantities = [o.get("quantity") for o in stock_offers]
    if quantities and all(isinstance(q, int) and not isinstance(q, bool) for q in quantities):
        result["ticket_stock"] = sum(quantities)
    return result


def build_event(item: dict, zone, city_slug: str = "barcelona", detail: Optional[dict] = None,
                offers: Optional[dict] = None, venue_info: Optional[dict] = None,
                popular_rank: Optional[Tuple[int, int]] = None) -> Optional[DigestEvent]:
    """One listing item (+ optional detail / offers / venue payloads) → ``DigestEvent``."""
    detail = detail or {}
    if called_off(item, detail):
        return None
    start = _epoch(item.get("startingTime"), zone)
    title = (item.get("name") or "").strip()
    legacy_id = item.get("legacyId")
    if start is None or not title or legacy_id is None:
        return None
    settings = detail.get("settings") or {}
    end = None if settings.get("hideEndingTime") else _epoch(item.get("endingTime"), zone)
    if end is not None and end <= start:
        end = None

    venue = item.get("venue") or {}
    venue_name = venue.get("name")
    secret = bool(item.get("venueIsSecret"))
    address = None if secret else ((detail.get("venue") or {}).get("address") or None)

    # Organiser comes from the company only; never fall back to the venue.
    company = detail.get("company") or {}
    organizer = company.get("commercialName") or company.get("name") or None

    lineup: List[str] = []
    for artist in item.get("lineup") or []:
        name = (artist.get("name") or "").strip() if isinstance(artist, dict) else ""
        if name and name.lower() != "tba" and name not in lineup:
            lineup.append(name)

    tags: List[str] = []
    for genre in item.get("musicGenres") or []:
        name = (genre.get("name") or "").strip() if isinstance(genre, dict) else ""
        if name and name not in tags:
            tags.append(name)
    for vibe in detail.get("vibes") or []:
        name = (vibe.get("name") or "").strip() if isinstance(vibe, dict) else ""
        if name and name not in tags:
            tags.append(name)

    summary = summarize_offers(offers)
    if summary["free_option"] and summary["is_free"] is False:
        tags.append("free-guest-list" if "guest list" in (summary["price"] or "") else "free-entry")
    if secret:
        tags.append("secret-venue")
    if item.get("hasActiveResaleListings"):
        tags.append("resale-tickets")
    if detail.get("isFestival"):
        tags.append("festival")

    extra: dict = {}
    min_age = (detail.get("agePolicy") or {}).get("minimumAge")
    if isinstance(min_age, int) and not isinstance(min_age, bool):
        extra["min_age"] = min_age
    if summary["ticket_stock"] is not None:
        extra["ticket_stock"] = summary["ticket_stock"]
    followers = (venue_info or {}).get("followersCount")
    if isinstance(followers, int) and not isinstance(followers, bool) and followers >= 0:
        extra["venue_followers"] = followers
    if popular_rank:
        extra["xceed_popular_rank"], extra["xceed_popular_pool"] = popular_rank

    description = (detail.get("about") or "").strip() or None
    image = detail.get("coverUrl") or item.get("coverUrl")
    return DigestEvent(
        source=SOURCE,
        title=title,
        start=start,
        end=end,
        url="%s/en/%s/event/%s/%s" % (SITE, city_slug, item.get("slug") or "", legacy_id),
        venue=venue_name,
        address=address,
        description=description,
        organizer=organizer,
        lineup=lineup,
        tags=tags,
        price=summary["price"],
        is_free=summary["is_free"],
        sold_out=summary["sold_out"],
        attending=None,
        extra_stats=extra,
        image_url=image,
        source_id=str(legacy_id),
    )


def in_window(item: dict, zone, start: datetime, end: datetime) -> bool:
    ts = _epoch(item.get("startingTime"), zone)
    return ts is not None and start <= ts < end


# ───────────────────────── fetching ─────────────────────────

def _why(exc: BaseException) -> str:
    text = str(exc).strip()
    return "%s: %s" % (type(exc).__name__, text) if text else type(exc).__name__


async def _get_json(client, url: str, params: Any = None) -> Any:
    resp = await client.get(url, params=params, headers=API_HEADERS)
    if resp.status_code != 200:
        raise CollectorError("Xceed %s answered HTTP %d" % (url.split("//")[1].split("?")[0], resp.status_code))
    try:
        return resp.json()
    except ValueError:
        raise CollectorError("Xceed %s did not return JSON" % url)


async def _fetch_listing(client, slug: str, start: datetime, end: datetime, zone) -> List[dict]:
    collected: List[dict] = []
    for page in range(MAX_PAGES):
        params = {"cities[]": slug, "limit": PAGE_LIMIT, "offset": page * PAGE_LIMIT,
                  "startTime": int(start.timestamp()), "orderBy": "date", "sort": "ASC"}
        try:
            payload = await _get_json(client, EVENT_API + "/v2/events", params)
            items, _total = parse_listing(payload)
        except CollectorError as exc:
            if page == 0:
                raise
            log.warning("xceed: listing page %d failed (%s): results may be incomplete", page + 1, _why(exc))
            break
        collected.extend(items)
        past_end = any((_epoch(i.get("startingTime"), zone) or start) >= end for i in items)
        if len(items) < PAGE_LIMIT or past_end:
            break
    return [i for i in collected if in_window(i, zone, start, end)]


async def collect(city, start, end, client) -> List[DigestEvent]:
    params = CITY_PARAMS.get(city.key)
    if not params:
        raise CollectorError("Xceed has no configuration for city %r" % city.key)
    slug = params["slug"]
    zone = city.zone

    try:
        items = await _fetch_listing(client, slug, start, end, zone)
    except CollectorError:
        raise
    except Exception as exc:  # noqa: BLE001 — network errors on the listing are fatal for this source
        raise CollectorError("Xceed listing request failed: %s" % _why(exc))
    if not items:
        return []

    sem = asyncio.Semaphore(CONCURRENCY)
    enrich_failures: List[str] = []

    async def guarded(label, coro_factory):
        async with sem:
            try:
                return await coro_factory()
            except Exception as exc:  # noqa: BLE001 — partial failure: skip this piece, report below
                enrich_failures.append("%s (%s)" % (label, _why(exc)))
                return None

    async def detail(item):
        payload = await guarded("detail %s" % item["legacyId"], lambda: _get_json(client, "%s/v1/events/%s" % (EVENT_API, item["legacyId"]), {"lang": "en"}))
        return (payload or {}).get("data")

    async def offers(item):
        payload = await guarded("offers %s" % item["legacyId"], lambda: _get_json(client, "%s/v1/events/%s/offers" % (OFFER_API, item["id"]), {"lang": "en"}))
        return (payload or {}).get("data")

    async def venue(venue_slug):
        payload = await guarded("venue %s" % venue_slug, lambda: _get_json(client, "%s/v1/venues/%s" % (EVENT_API, venue_slug)))
        return (payload or {}).get("data")

    async def popular():
        payload = await guarded("popularity ranking", lambda: _get_json(client, EVENT_API + "/v2/events", {
            "cities[]": slug, "limit": PAGE_LIMIT, "startTime": int(start.timestamp()),
            "endTime": int(end.timestamp()), "orderBy": "popular", "sort": "DESC"}))
        if not payload:
            return []
        try:
            return [i.get("id") for i in parse_listing(payload)[0]]
        except CollectorError:
            return []

    venue_slugs = sorted({(i.get("venue") or {}).get("slug") for i in items if (i.get("venue") or {}).get("slug")})
    details, offer_payloads, venue_payloads, popular_ids = await asyncio.gather(
        asyncio.gather(*[detail(i) for i in items]),
        asyncio.gather(*[offers(i) for i in items]),
        asyncio.gather(*[venue(s) for s in venue_slugs]),
        popular(),
    )
    if enrich_failures:
        # Enrichment only — every listed event is still returned.  It becomes a
        # warning (which marks the source "partial") only when a quarter or more
        # of the lookups failed.
        attempted = 2 * len(items) + len(venue_slugs) + 1
        level = logging.WARNING if len(enrich_failures) * 4 >= attempted else logging.INFO
        log.log(level, "xceed: %d of %d detail lookups failed (e.g. %s): price, promoter or ranking missing for those",
                len(enrich_failures), attempted, enrich_failures[0])
    venues = dict(zip(venue_slugs, venue_payloads))
    rank_pool = len(popular_ids)

    events: List[DigestEvent] = []
    for item, det, off in zip(items, details, offer_payloads):
        rank = None
        if item.get("id") in popular_ids:
            rank = (popular_ids.index(item["id"]) + 1, rank_pool)
        ev = build_event(item, zone, slug, det, off, venues.get((item.get("venue") or {}).get("slug")), rank)
        if ev is not None:
            events.append(ev)
    events.sort(key=lambda e: e.start)
    return events


if __name__ == "__main__":
    from backend.digest.collectors._smoke import main
    raise SystemExit(main(__import__("sys").modules[__name__]))
