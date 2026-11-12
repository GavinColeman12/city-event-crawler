"""Run every collector and assemble the candidates payload a digest is picked from."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from backend.digest import candidates as cand
from backend.digest.cities import City, collection_window
from backend.digest.collectors import REGISTRY, CollectorError, load
from backend.digest.guardrails import blocked_term, filter_events
from backend.digest.http import make_client
from backend.digest.models import DigestEvent, validate_event

logger = logging.getLogger(__name__)

COLLECTOR_TIMEOUT_SECS = 90.0
INSTAGRAM_TIMEOUT_SECS = 240.0   # one Apify actor run for every handle
APIFY_TOKEN_ENV = "INSTAGRAM_APIFY_TOKEN"


class _DeniedHosts:
    """Remembers hosts a cloud sandbox's network policy refused.

    Anthropic's cloud environments answer a blocked request with HTTP 403 and an
    ``x-deny-reason`` header.  Without this, that looks like the source itself
    blocking us, which sends whoever reads the report after the wrong fix.
    """

    def __init__(self) -> None:
        self.hosts: set = set()

    async def __call__(self, response) -> None:
        if response.headers.get("x-deny-reason"):
            self.hosts.add(response.request.url.host)


class _CollectorWarnings(logging.Handler):
    """Gathers what collectors log at WARNING or above, per source.

    A collector that could only fetch part of its data says so with
    ``logger.warning``.  Capturing those lines is what lets the report mark a
    source "partial" instead of presenting a short list as the whole picture.
    """

    _PREFIX = "backend.digest.collectors."
    _PER_SOURCE = 5

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.by_source: Dict[str, List[str]] = {}

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith(self._PREFIX):
            return
        try:
            message = " ".join(record.getMessage().split())[:200]
        except Exception:  # noqa: BLE001 — a bad log call must not break the run
            return
        seen = self.by_source.setdefault(record.name[len(self._PREFIX):].split(".")[0], [])
        if message and message not in seen and len(seen) < self._PER_SOURCE:
            seen.append(message)


async def _run_collector(name: str, city: City, start: datetime, end: datetime, client) -> Tuple[List[DigestEvent], dict]:
    status: dict = {"ok": False, "events": 0, "seconds": 0.0, "error": None, "warnings": []}
    began = time.monotonic()
    events: List[DigestEvent] = []
    try:
        module = load(name)
        raw = await asyncio.wait_for(module.collect(city, start, end, client), COLLECTOR_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        status["error"] = "timed out after %ds" % COLLECTOR_TIMEOUT_SECS
    except CollectorError as exc:
        status["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — one broken source must never sink the digest
        logger.exception("collector %s crashed", name)
        status["error"] = "%s: %s" % (type(exc).__name__, exc)
    else:
        invalid = 0
        for ev in raw:
            try:
                validate_event(ev)
            except ValueError as exc:
                invalid += 1
                logger.warning("dropping invalid %s event: %s", name, exc)
                continue
            if start <= ev.start < end:
                events.append(ev)
        status["ok"] = True
        status["events"] = len(events)
        if invalid:
            status["warnings"].append("%d event(s) dropped for breaking the data contract" % invalid)
        if not events:
            # A healthy source with nothing on for two days is unlikely in a big
            # city; an empty result more often means the site changed shape.
            status["warnings"].append("returned 0 events — possibly a parser break")
    status["seconds"] = round(time.monotonic() - began, 1)
    return events, status


async def _run_instagram(city: City, client, token: str) -> Tuple[List[dict], dict]:
    status: dict = {"ok": False, "posts": 0, "seconds": 0.0, "error": None}
    began = time.monotonic()
    posts: List[dict] = []
    try:
        module = load("instagram")
        posts = await asyncio.wait_for(module.collect_posts(city, client, token), INSTAGRAM_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        status["error"] = "timed out after %ds" % INSTAGRAM_TIMEOUT_SECS
    except CollectorError as exc:
        status["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("instagram collector crashed")
        status["error"] = "%s: %s" % (type(exc).__name__, exc)
    else:
        # The content guardrail applies to captions too.
        posts = [p for p in posts if not blocked_term(p.get("caption"), p.get("handle"))]
        status["ok"] = True
        status["posts"] = len(posts)
    status["seconds"] = round(time.monotonic() - began, 1)
    return posts, status


async def collect_all(
    city: City,
    now: datetime,
    sources: Optional[Sequence[str]] = None,
    with_instagram: bool = True,
    env: Optional[dict] = None,
) -> Tuple[List[DigestEvent], Dict[str, dict], List[dict], List[str]]:
    """Returns (events, per-source status, instagram posts, hosts blocked by the sandbox)."""
    env = os.environ if env is None else env
    names = [n for n in (sources or REGISTRY) if n != "instagram"]
    start, end = collection_window(city, now)
    denied = _DeniedHosts()
    warnings = _CollectorWarnings()
    collector_log = logging.getLogger("backend.digest.collectors")
    collector_log.addHandler(warnings)
    try:
        async with make_client(event_hooks={"response": [denied]}) as client:
            jobs = [_run_collector(name, city, start, end, client) for name in names]
            token = (env.get(APIFY_TOKEN_ENV) or "").strip()
            run_instagram = with_instagram and bool(token) and (sources is None or "instagram" in sources)
            if run_instagram:
                jobs.append(_run_instagram(city, client, token))
            results = await asyncio.gather(*jobs)
    finally:
        collector_log.removeHandler(warnings)

    events: List[DigestEvent] = []
    statuses: Dict[str, dict] = {}
    for name, (found, status) in zip(names, results):
        events.extend(found)
        # What the collector itself logged comes first: it explains a short count.
        status["warnings"] = warnings.by_source.get(name, []) + status["warnings"]
        statuses[name] = status
    posts: List[dict] = []
    if run_instagram:
        posts, statuses["instagram"] = results[-1]
    elif with_instagram:
        statuses["instagram"] = {"ok": False, "posts": 0, "seconds": 0.0, "skipped": True,
                                 "error": "not configured (%s is not set)" % APIFY_TOKEN_ENV}
    return events, statuses, posts, sorted(denied.hosts)


def _day_block(day, found: List[cand.Candidate], limit: int) -> dict:
    return {
        "date": day.isoformat(),
        "label": day.strftime("%a %d %b").replace(" 0", " "),
        "found": len(found),
        "candidates": [c.to_dict() for c in cand.shortlist(found, limit)],
    }


def build_payload(
    city: City,
    now: datetime,
    events: List[DigestEvent],
    statuses: Dict[str, dict],
    instagram_posts: Optional[List[dict]] = None,
    blocked_hosts: Optional[List[str]] = None,
    max_per_day: int = 26,
) -> dict:
    """Guardrail → merge duplicates → bucket → score → shortlist.  Pure: no network."""
    now = now.astimezone(city.zone)
    kept, removed = filter_events(events)
    merged = cand.merge_duplicates(kept)
    today, tomorrow = cand.bucket(merged, city, now)
    for candidate in today + tomorrow:
        cand.score(candidate)
    today_date = cand.digest_day(now, city)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "city": {"key": city.key, "name": city.name, "tz": city.tz},
        "today": _day_block(today_date, today, max_per_day),
        "tomorrow": _day_block(today_date + timedelta(days=1), tomorrow, max_per_day),
        "sources": statuses,
        "blocked_hosts": blocked_hosts or [],
        "totals": {
            "collected": len(events),
            "after_dedupe": len(merged),
            "removed_by_content_filter": len(removed),
        },
        "content_filter_removed": removed,
        "instagram_posts": instagram_posts or [],
    }


async def collect_payload(
    city: City,
    now: Optional[datetime] = None,
    sources: Optional[Sequence[str]] = None,
    with_instagram: bool = True,
    max_per_day: int = 26,
) -> dict:
    now = (now or datetime.now(city.zone)).astimezone(city.zone)
    events, statuses, posts, blocked = await collect_all(city, now, sources, with_instagram)
    return build_payload(city, now, events, statuses, posts, blocked, max_per_day)
