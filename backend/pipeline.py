"""Single-function entry point to the v2 pipeline.

Runs DISCOVER → TRIAGE → SCRAPE (posts + stories) → EXTRACT → SCORE → CURATE,
returning a dict that matches the SearchResponse schema. Caches scrape
results in Postgres and writes a cost_log row at the end.

Designed so the same code path serves:
  - the FastAPI handler in backend.main (HTTP)
  - the Streamlit dashboard (in-process, no HTTP boundary)
"""

from __future__ import annotations

import logging
import time
import traceback
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Optional

import asyncpg

from .config import CITY_COORDINATES, get_settings
from .db import cost as cost_db
from .extraction import compose_guide, parse_events, rate_events
from .extraction.score import composite_score
from .instagram import discover_accounts, scrape_account_content, triage_accounts
from .models import (
    Event,
    EventVibe,
    SearchRequest,
    SearchResponse,
)
from .utils.helpers import calculate_distance

logger = logging.getLogger(__name__)


def _resolve_city(request: SearchRequest) -> tuple[float, float, str]:
    if request.latitude is not None and request.longitude is not None:
        return request.latitude, request.longitude, request.city.strip().title()
    city_key = request.city.strip().lower()
    data = CITY_COORDINATES.get(city_key)
    if data is None:
        raise ValueError(
            f"City '{request.city}' is not in the supported list. "
            "Provide explicit latitude/longitude or call list_cities()."
        )
    return data["lat"], data["lon"], request.city.strip().title()


def _normalize_title(title: str) -> str:
    import re
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", title.lower())).strip()


def _dedupe_events(events: list[Event]) -> list[Event]:
    if not events:
        return []
    by_day: dict[str, list[Event]] = {}
    for ev in events:
        day = ev.date.strftime("%Y-%m-%d") if ev.date else "unknown"
        by_day.setdefault(day, []).append(ev)
    kept: list[Event] = []
    for group in by_day.values():
        clusters: list[list[Event]] = []
        for ev in group:
            t = _normalize_title(ev.title)
            placed = False
            for cluster in clusters:
                rep_t = _normalize_title(cluster[0].title)
                if SequenceMatcher(None, t, rep_t).ratio() >= 0.65:
                    cluster.append(ev)
                    placed = True
                    break
            if not placed:
                clusters.append([ev])
        for cluster in clusters:
            kept.append(max(cluster, key=lambda e: (
                bool(e.description), bool(e.venue_name), bool(e.image_url),
                e.likes or 0, e.comments or 0,
            )))
    return kept


async def _open_pool(database_url: str) -> Optional[asyncpg.Pool]:
    """Open a transient pool scoped to one run_search call.

    Owning the pool inside run_search (instead of as a module global)
    eliminates the cross-event-loop bug that hits when Streamlit calls
    asyncio.run() repeatedly: each call gets a fresh loop AND a fresh
    pool, so no state leaks between calls.
    """
    if not database_url:
        return None
    try:
        return await asyncpg.create_pool(
            dsn=database_url, min_size=1, max_size=4, command_timeout=30,
        )
    except Exception as exc:
        logger.warning("DB pool creation failed (continuing without DB): %s", exc)
        return None


async def run_search(request: SearchRequest) -> SearchResponse:
    """Execute the v2 pipeline end-to-end.

    Owns its own DB pool for the duration of the call so it is safe to
    run repeatedly from a sync context like Streamlit (where each click
    spins up a fresh asyncio loop via ``asyncio.run``).
    """
    settings = get_settings()
    t0 = time.monotonic()

    latitude, longitude, city_name = _resolve_city(request)
    pool = await _open_pool(settings.DATABASE_URL)
    try:
        return await _run_with_pool(
            request=request,
            pool=pool,
            settings=settings,
            t0=t0,
            latitude=latitude,
            longitude=longitude,
            city_name=city_name,
        )
    finally:
        if pool is not None:
            try:
                await pool.close()
            except Exception as exc:
                logger.warning("Pool close failed (ignored): %s", exc)


async def _run_with_pool(
    *,
    request: SearchRequest,
    pool: Optional[asyncpg.Pool],
    settings,
    t0: float,
    latitude: float,
    longitude: float,
    city_name: str,
) -> SearchResponse:
    errors: list[dict[str, Any]] = []

    accounts: list[str] = []
    triaged: list[str] = []
    posts_count = 0
    stories_count = 0
    accounts_cache_hit = 0
    apify_cost = 0.0
    items: list[dict] = []
    events: list[Event] = []
    guide = None

    monthly_spent = await cost_db.monthly_spend_usd(pool)
    budget_blocked = monthly_spent >= settings.MONTHLY_BUDGET_USD

    # DISCOVER
    try:
        accounts = await discover_accounts(
            city=city_name,
            serpapi_key=settings.SERPAPI_KEY,
            vibes=request.vibes,
            max_queries=settings.MAX_DISCOVERY_QUERIES,
        )
    except Exception as exc:
        logger.error("DISCOVER failed: %s", exc)
        errors.append({"stage": "discover", "error": str(exc), "traceback": traceback.format_exc()})

    # TRIAGE
    if accounts:
        try:
            triaged = await triage_accounts(
                city=city_name,
                handles=accounts,
                vibes=request.vibes,
                max_keep=settings.MAX_ACCOUNTS_PER_SEARCH,
            )
        except Exception as exc:
            logger.error("TRIAGE failed: %s", exc)
            errors.append({"stage": "triage", "error": str(exc), "traceback": traceback.format_exc()})
            triaged = accounts[: settings.MAX_ACCOUNTS_PER_SEARCH]

    # SCRAPE — cache-first; if budget blocked, only read cache.
    if triaged:
        try:
            if budget_blocked:
                from .db import cache as cache_db
                cached_posts = await cache_db.read_scrape_cache(pool, triaged, "posts")
                cached_stories = await cache_db.read_scrape_cache(pool, triaged, "stories")
                for posts_list in cached_posts.values():
                    for p in posts_list:
                        p.setdefault("_origin", "profile")
                        items.append(p)
                for stories_list in cached_stories.values():
                    for s in stories_list:
                        s.setdefault("_origin", "story")
                        items.append(s)
                accounts_cache_hit = len(cached_posts) + len(cached_stories)
                posts_count = sum(len(v) for v in cached_posts.values())
                stories_count = sum(len(v) for v in cached_stories.values())
            else:
                items, summary = await scrape_account_content(triaged, pool=pool)
                posts_count = sum(1 for i in items if i.get("_origin") == "profile")
                stories_count = sum(1 for i in items if i.get("_origin") == "story")
                accounts_cache_hit = summary["posts_cache_hit"] + summary["stories_cache_hit"]
                apify_cost = cost_db.compute_apify_cost(
                    summary["posts_billed"],
                    summary["stories_billed"],
                    posts_per_1k=settings.APIFY_POSTS_USD_PER_1K,
                    stories_per_1k=settings.APIFY_STORIES_USD_PER_1K,
                )
        except Exception as exc:
            logger.error("SCRAPE failed: %s", exc)
            errors.append({"stage": "scrape", "error": str(exc), "traceback": traceback.format_exc()})

    # EXTRACT
    if items:
        try:
            events = await parse_events(items, reference_date=request.date)
        except Exception as exc:
            logger.error("EXTRACT failed: %s", exc)
            errors.append({"stage": "extract", "error": str(exc), "traceback": traceback.format_exc()})

    # Dedupe + distance + vibe filter
    if events:
        events = _dedupe_events(events)
        for ev in events:
            if ev.latitude is not None and ev.longitude is not None:
                ev.distance_km = calculate_distance(latitude, longitude, ev.latitude, ev.longitude)
        if request.vibes:
            requested = set(request.vibes)
            events = [ev for ev in events if not ev.vibes or set(ev.vibes) & requested]

    # SCORE
    if events:
        try:
            events = await rate_events(events, vibes=request.vibes)
        except Exception as exc:
            logger.error("SCORE failed: %s", exc)
            errors.append({"stage": "score", "error": str(exc), "traceback": traceback.format_exc()})
        events.sort(key=lambda e: (composite_score(e), e.engagement_score), reverse=True)
        events = events[: request.max_results]

    # CURATE
    if events:
        try:
            guide = await compose_guide(events=events, city=city_name, vibes=request.vibes)
        except Exception as exc:
            logger.error("CURATE failed: %s", exc)
            errors.append({"stage": "curate", "error": str(exc), "traceback": traceback.format_exc()})

    elapsed = round(time.monotonic() - t0, 3)

    await cost_db.record_run(
        pool,
        {
            "city": city_name,
            "search_date": request.date,
            "vibes": [v.value for v in (request.vibes or [])],
            "accounts_discovered": len(accounts),
            "accounts_triaged": len(triaged),
            "accounts_cache_hit": accounts_cache_hit,
            "accounts_scraped": max(0, len(triaged) - accounts_cache_hit),
            "posts_scraped": posts_count,
            "stories_scraped": stories_count,
            "events_extracted": len(events),
            "apify_results_billed": posts_count + stories_count,
            "apify_cost_usd": apify_cost,
            "claude_input_tokens": 0,
            "claude_output_tokens": 0,
            "duration_seconds": elapsed,
            "budget_blocked": budget_blocked,
            "errors": errors,
        },
    )

    new_monthly_spent = await cost_db.monthly_spend_usd(pool)

    logger.info(
        "Search %s/%s done: discovered=%d triaged=%d posts=%d stories=%d events=%d cost=$%.4f in %.2fs",
        city_name, request.date,
        len(accounts), len(triaged), posts_count, stories_count, len(events), apify_cost, elapsed,
    )

    return SearchResponse(
        events=events,
        curated_guide=guide,
        total_count=len(events),
        city=city_name,
        date=request.date,
        search_duration_seconds=elapsed,
        accounts_discovered=len(accounts),
        accounts_triaged=len(triaged),
        accounts_cache_hit=accounts_cache_hit,
        posts_scraped=posts_count,
        stories_scraped=stories_count,
        events_extracted=len(events),
        apify_cost_usd=apify_cost,
        monthly_spend_usd=new_monthly_spent,
        monthly_budget_usd=settings.MONTHLY_BUDGET_USD,
        budget_blocked=budget_blocked,
        errors=errors,
    )


__all__ = ["run_search"]
