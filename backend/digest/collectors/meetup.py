"""Meetup collector — in-person Meetup events around a city.

How it works
------------
meetup.com's own front end talks to one GraphQL endpoint, ``https://www.meetup.com/gql2``.
It accepts plain (non-persisted) queries without login, cookies or tokens.  We
use the ``recommendedEvents`` field — the same one behind the public
``/find/?source=EVENTS`` page — because it works with an empty keyword, which
the ``eventSearch`` field does not (it returns nothing without a keyword).

* filter: ``lat``/``lon``/``radius`` (miles) + ``startDateRange``/``endDateRange``
  + ``eventType: PHYSICAL`` (in-person only).  Dates are Java-style zoned
  datetimes: ``2026-11-12T00:00:00+01:00[Europe/Madrid]``.
* pagination: ``first`` (≤100 honoured) + ``after`` cursor, until
  ``hasNextPage`` is false.
* Meetup caps what one query returns (relevance-cut), and a wide window loses
  ~10-20 % of events.  So the window is cut per local day and any slice that
  comes back big is bisected and re-queried until slices are small.  Results
  are de-duplicated by event id.
* Every number comes in the same response: ``rsvps.yesCount`` (going),
  ``rsvps.waitlistCount``, ``maxTickets`` (RSVP limit, 0 = none),
  ``group.memberships.totalCount`` (group members).  No per-event requests.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

from backend.digest.collectors import CollectorError
from backend.digest.models import DigestEvent

SOURCE = "meetup"

CITY_PARAMS = {
    # radius is in miles (Meetup's unit).  10 mi keeps the city and the towns the
    # metro reaches (L'Hospitalet, Badalona, Sant Adrià) and leaves out places
    # that are a train ride away (Sabadell, Sitges, Terrassa).
    "barcelona": {"lat": 41.3851, "lon": 2.1734, "radius": 10},
}

GQL_URL = "https://www.meetup.com/gql2"
PAGE_SIZE = 100
MAX_CONCURRENCY = 5
SPLIT_THRESHOLD = 40          # a slice returning >= this many events is bisected
MIN_SLICE = timedelta(minutes=15)
MAX_REQUESTS = 160            # hard stop on total HTTP requests per run (a normal run uses ~47)
RETRY_PAUSE = 1.0             # seconds before retrying a day whose first request failed
MAX_EVENTS = 500
DESCRIPTION_CHARS = 1200

# Event statuses that are not a real, happening event.
_DEAD_STATUSES = {
    "CANCELLED", "CANCELLED_PERM", "AUTOSCHED_CANCELLED", "DRAFT", "AUTOSCHED_DRAFT",
    "BLOCKED", "TEMPLATE", "PROPOSED", "PENDING",
}

_QUERY = """
query($filter: RecommendedEventsFilter!, $first: Int, $after: String) {
  recommendedEvents(filter: $filter, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id title dateTime endTime eventType isOnline status eventUrl maxTickets description
      rsvps { totalCount yesCount waitlistCount }
      feeSettings { amount currency required }
      group { id name urlname memberships { totalCount } }
      venue { name address city lat lon }
      topics { edges { node { name } } }
      featuredEventPhoto { highResUrl }
    } }
  }
}
"""

log = logging.getLogger(__name__)


# ─────────────────────────────── pure parsing ───────────────────────────────

def _to_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 else None


def _dict(value: Any) -> dict:
    """The value if it is a dict, else {} — malformed sub-objects lose just that field."""
    return value if isinstance(value, dict) else {}


def _describe(exc: BaseException) -> str:
    """'TypeName: short message' — httpx timeouts have an empty str(), so always name the type."""
    msg = " ".join(str(exc).split())[:120]
    return "%s: %s" % (type(exc).__name__, msg) if msg else type(exc).__name__


def _plain_text(text: Any) -> Optional[str]:
    """Meetup descriptions are markdown; flatten to readable plain text."""
    if not text or not isinstance(text, str):
        return None
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    if len(text) > DESCRIPTION_CHARS:
        text = text[:DESCRIPTION_CHARS].rsplit(" ", 1)[0] + "…"
    return text or None


def _price(fee: Any) -> Tuple[Optional[str], Optional[bool]]:
    """No fee settings → "Free to RSVP" with is_free None.

    Meetup shows such events as free, but organisers often charge at the door or
    through an external ticket link, so we cannot claim "Free".
    """
    fee = _dict(fee)
    amount = fee.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
        return "Free to RSVP", None
    cur = fee.get("currency")
    cur = cur.upper() if isinstance(cur, str) else ""
    symbol = {"EUR": "€", "USD": "$", "GBP": "£"}.get(cur)
    number = ("%g" % amount) if float(amount).is_integer() or round(amount, 2) == amount else "%.2f" % amount
    if symbol:
        return "%s%s" % (symbol, number), False
    return ("%s %s" % (number, cur)).strip(), False


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _address(venue: dict) -> Optional[str]:
    addr = _str(venue.get("address")).rstrip(",").strip()
    city = _str(venue.get("city"))
    if addr and city and city.lower() not in addr.lower():
        return "%s, %s" % (addr, city)
    return addr or city or None


def _parse_node(node: Any, city, include_online: bool) -> Optional[DigestEvent]:
    node = _dict(node)
    if (node.get("status") or "ACTIVE") in _DEAD_STATUSES:
        return None
    online = bool(node.get("isOnline")) or node.get("eventType") == "ONLINE"
    if online and not include_online:
        return None
    raw_start = node.get("dateTime")
    title = _str(node.get("title"))
    url = node.get("eventUrl")
    if not isinstance(raw_start, str) or not raw_start or not title or not isinstance(url, str) or not url.startswith("http"):
        return None
    try:
        start = datetime.fromisoformat(raw_start).astimezone(city.zone)
        raw_end = node.get("endTime")
        end = datetime.fromisoformat(raw_end).astimezone(city.zone) if isinstance(raw_end, str) and raw_end else None
    except ValueError:
        return None

    rsvps = _dict(node.get("rsvps"))
    going = _to_int(rsvps.get("yesCount"))      # yesCount only: totalCount may include other RSVP states
    limit = _to_int(node.get("maxTickets")) or None      # 0 means "no limit"
    waitlist = _to_int(rsvps.get("waitlistCount"))

    group = _dict(node.get("group"))
    members = _to_int(_dict(group.get("memberships")).get("totalCount"))
    venue = _dict(node.get("venue"))
    price, is_free = _price(node.get("feeSettings"))
    topic_edges = _dict(node.get("topics")).get("edges")
    topics = [
        _dict(_dict(e).get("node")).get("name")
        for e in (topic_edges if isinstance(topic_edges, list) else [])
    ]
    node_id = node.get("id")

    return DigestEvent(
        source=SOURCE,
        title=title,
        start=start,
        end=end,
        url=url,
        venue=_str(venue.get("name")) or None,
        address=_address(venue),
        is_online=online,
        description=_plain_text(node.get("description")),
        organizer=_str(group.get("name")) or None,
        tags=[t for t in topics if isinstance(t, str) and t],
        price=price,
        is_free=is_free,
        sold_out=(True if (limit and going is not None and going >= limit) else None),
        attending=going,
        attending_label="going" if going is not None else None,
        capacity=limit,
        organizer_followers=members,
        extra_stats=({"waitlist": waitlist} if waitlist is not None else {}),
        image_url=(_str(_dict(node.get("featuredEventPhoto")).get("highResUrl")) or None),
        source_id=str(node_id) if node_id is not None else None,
    )


def _iter_nodes(payload: Any) -> Iterable[Any]:
    pages = payload if isinstance(payload, list) else [payload]
    for page in pages:
        conn = _dict(_dict(_dict(page).get("data")).get("recommendedEvents"))
        edges = conn.get("edges")
        for edge in edges if isinstance(edges, list) else []:
            node = _dict(edge).get("node")
            if node:
                yield node


def parse_events(payload: Any, city, start: datetime, end: datetime,
                 include_online: bool = False) -> List[DigestEvent]:
    """GraphQL response (or a list of them) → events starting in ``[start, end)``.

    Online-only events are dropped unless ``include_online`` (then they come
    back with ``is_online=True``).  Duplicate event ids are collapsed.  A
    malformed node is skipped (one warning per call), never fatal.
    """
    out: List[DigestEvent] = []
    seen = set()
    bad = 0
    first_bad: Optional[BaseException] = None
    for node in _iter_nodes(payload):
        try:
            ev = _parse_node(node, city, include_online)
        except Exception as exc:  # noqa: BLE001 — one bad node must not kill the run
            bad += 1
            first_bad = first_bad or exc
            continue
        if ev is None or not (start <= ev.start < end):
            continue
        if ev.source_id in seen:
            continue
        seen.add(ev.source_id)
        out.append(ev)
    if bad:
        log.warning("meetup: skipped %d malformed event(s) (%s)", bad, _describe(first_bad))
    out.sort(key=lambda e: (e.start, e.title))
    return out


# ─────────────────────────────── fetching ───────────────────────────────

def _zoned(dt: datetime, tz: str) -> str:
    return "%s[%s]" % (dt.isoformat(timespec="seconds"), tz)


def day_slices(start: datetime, end: datetime) -> List[Tuple[datetime, datetime]]:
    """Cut [start, end) at local midnights."""
    out = []
    cur = start
    while cur < end:
        nxt = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        out.append((cur, min(nxt, end)))
        cur = nxt
    return out


class _BudgetExhausted(CollectorError):
    """The per-run request budget is used up (degrades results, never discards them)."""


class _Budget:
    def __init__(self) -> None:
        self.requests = 0
        self.sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self.exhausted_logged = False

    def exhausted(self) -> bool:
        return self.requests >= MAX_REQUESTS

    def warn_exhausted(self) -> None:
        if not self.exhausted_logged:
            self.exhausted_logged = True
            log.warning("meetup: request budget (%d) exhausted — results may be incomplete", MAX_REQUESTS)


def _slice_label(s: datetime) -> str:
    return "%s %d %s" % (s.strftime("%a"), s.day, s.strftime("%b"))


async def _post(client: httpx.AsyncClient, budget: _Budget, variables: dict) -> dict:
    if budget.exhausted():
        budget.warn_exhausted()
        raise _BudgetExhausted("meetup: request budget (%d) exhausted" % MAX_REQUESTS)
    budget.requests += 1
    async with budget.sem:
        resp = await client.post(
            GQL_URL,
            json={"query": _QUERY, "variables": variables},
            headers={"Origin": "https://www.meetup.com", "Referer": "https://www.meetup.com/find/"},
        )
    if resp.status_code != 200:
        raise CollectorError("meetup: HTTP %d from the GraphQL endpoint (%s)" % (resp.status_code, resp.text[:120].replace("\n", " ")))
    try:
        data = resp.json()
    except ValueError:
        raise CollectorError("meetup: non-JSON response from GraphQL endpoint: %s" % resp.text[:120].replace("\n", " "))
    if not isinstance(data, dict):
        raise CollectorError("meetup: unexpected GraphQL response shape")
    errors = data.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) else errors
        msg = first.get("message") if isinstance(first, dict) else first
        raise CollectorError("meetup: GraphQL error: %s" % str(msg)[:200])
    if not isinstance(_dict(data.get("data")).get("recommendedEvents"), dict):
        raise CollectorError("meetup: GraphQL response has no recommendedEvents")
    return data


async def _fetch_slice(client, budget, params, city, s: datetime, e: datetime) -> List[dict]:
    """All pages for one [s, e] slice → list of raw GraphQL responses.

    Raises only if the FIRST request fails; a later failing page (or an
    exhausted budget) returns the pages already fetched, with a warning.
    """
    filt = {
        "lat": params["lat"], "lon": params["lon"], "radius": params["radius"],
        "eventType": "PHYSICAL",
        "startDateRange": _zoned(s, city.tz), "endDateRange": _zoned(e, city.tz),
    }
    pages: List[dict] = []
    after = None
    total = 0
    for _ in range(12):
        try:
            data = await _post(client, budget, {"filter": filt, "first": PAGE_SIZE, "after": after})
        except _BudgetExhausted:
            if not pages:
                raise
            break                                   # already warned once by _post
        except Exception as exc:  # noqa: BLE001
            if not pages:
                raise
            log.warning("meetup: a later page for %s failed (%s) — that slice may be incomplete",
                        _slice_label(s), _describe(exc))
            break
        pages.append(data)
        conn = data["data"]["recommendedEvents"]
        edges = conn.get("edges")
        total += len(edges) if isinstance(edges, list) else 0
        info = _dict(conn.get("pageInfo"))
        if not info.get("hasNextPage") or not info.get("endCursor") or total >= MAX_EVENTS:
            break
        after = info["endCursor"]
    return pages


def _count(pages: List[dict]) -> int:
    n = 0
    for p in pages:
        edges = _dict(p["data"]["recommendedEvents"]).get("edges")
        n += len(edges) if isinstance(edges, list) else 0
    return n


async def _collect_slice(client, budget, params, city, s, e, depth=0) -> List[dict]:
    """Pages for [s, e], bisected while the slice is big.

    Raises only if the slice's own first request fails.  Once pages are in hand
    they are never discarded: a failing half just means that bisect is skipped.
    """
    pages = await _fetch_slice(client, budget, params, city, s, e)
    if _count(pages) >= SPLIT_THRESHOLD and (e - s) > MIN_SLICE * 2 and depth < 6:
        mid = s + (e - s) / 2
        mid = mid.replace(second=0, microsecond=0)
        mid = mid - timedelta(minutes=mid.minute % 15)      # snap to a quarter hour
        if s < mid < e:
            if budget.exhausted():
                budget.warn_exhausted()
                return pages
            halves = await asyncio.gather(
                _collect_slice(client, budget, params, city, s, mid, depth + 1),
                _collect_slice(client, budget, params, city, mid, e, depth + 1),
                return_exceptions=True,
            )
            out = list(pages)
            for res in halves:
                if isinstance(res, asyncio.CancelledError):
                    raise res
                if isinstance(res, _BudgetExhausted):
                    budget.warn_exhausted()
                elif isinstance(res, BaseException):
                    log.warning("meetup: could not split %s further (%s) — keeping the pages already fetched",
                                _slice_label(s), _describe(res))
                else:
                    out.extend(res)
            return out
    return pages


async def _collect_day(client, budget, params, city, s, e) -> Tuple[List[dict], Optional[BaseException]]:
    """One top-level day → (pages, error).  Retries once if the very first request fails."""
    err: Optional[BaseException] = None
    for attempt in range(2):
        try:
            return await _collect_slice(client, budget, params, city, s, e), None
        except asyncio.CancelledError:
            raise
        except _BudgetExhausted as exc:
            budget.warn_exhausted()
            return [], exc
        except Exception as exc:  # noqa: BLE001
            err = exc
            if attempt == 0:
                await asyncio.sleep(RETRY_PAUSE)
    log.warning("meetup: %s could not be fetched (%s) — that day is missing", _slice_label(s), _describe(err))
    return [], err


async def collect(city, start, end, client) -> List[DigestEvent]:
    params = CITY_PARAMS.get(city.key)
    if params is None:
        raise CollectorError("meetup: no CITY_PARAMS for %r" % city.key)
    budget = _Budget()
    slices = day_slices(start.astimezone(city.zone), end.astimezone(city.zone))
    # _collect_day never raises (bar cancellation) and gather awaits every task,
    # so nothing is still running when collect() returns.
    results = await asyncio.gather(
        *[_collect_day(client, budget, params, city, s, e) for s, e in slices],
        return_exceptions=True,
    )
    pages: List[dict] = []
    errors: List[BaseException] = []
    for res in results:
        if isinstance(res, BaseException):
            errors.append(res)
        elif res[1] is not None:
            errors.append(res[1])
        else:
            pages.extend(res[0])
    if slices and len(errors) == len(slices):
        raise CollectorError("meetup: every request failed (%s)" % _describe(errors[0]))
    events = parse_events(pages, city, start, end)
    log.info("meetup: %d events from %d requests", len(events), budget.requests)
    return events


if __name__ == "__main__":
    from backend.digest.collectors._smoke import main
    raise SystemExit(main(__import__("sys").modules[__name__]))
