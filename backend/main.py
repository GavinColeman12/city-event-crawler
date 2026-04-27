"""
City Event Crawler v2 — Instagram-only deep discovery + Claude curation.

Pipeline:  DISCOVER → TRIAGE → SCRAPE → EXTRACT → SCORE → CURATE
"""

from __future__ import annotations

import logging
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from difflib import SequenceMatcher

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .config import CITY_COORDINATES, get_settings
from .db import close_pool, get_pool
from .db import cost as cost_db
from .extraction import compose_guide, parse_events, rate_events
from .extraction.score import composite_score
from .instagram import discover_accounts, scrape_account_content, triage_accounts
from .models import (
    CityInfo,
    Event,
    EventVibe,
    SearchRequest,
    SearchResponse,
)
from .utils.helpers import calculate_distance

logger = logging.getLogger("city_event_crawler")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info(
        "Startup: serpapi=%s apify=%s anthropic=%s db=%s model=%s",
        bool(settings.SERPAPI_KEY),
        bool(settings.INSTAGRAM_APIFY_TOKEN),
        bool(settings.ANTHROPIC_API_KEY),
        bool(settings.DATABASE_URL),
        settings.CLAUDE_MODEL,
    )
    yield
    await close_pool()
    logger.info("Shutdown.")


app = FastAPI(
    title="City Event Crawler v2",
    description="Instagram deep discovery with Claude-powered curation",
    version="2.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _resolve_city(request: SearchRequest) -> tuple[float, float, str]:
    if request.latitude is not None and request.longitude is not None:
        return request.latitude, request.longitude, request.city.strip().title()
    city_key = request.city.strip().lower()
    data = CITY_COORDINATES.get(city_key)
    if data is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"City '{request.city}' not in supported list. "
                "Provide explicit latitude/longitude or call GET /api/cities."
            ),
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


@app.post("/api/search", response_model=SearchResponse)
async def search_events(request: SearchRequest) -> SearchResponse:
    """Run the v2 pipeline and return events + curated guide + cost telemetry."""
    settings = get_settings()
    t0 = time.monotonic()

    latitude, longitude, city_name = _resolve_city(request)
    pool = await get_pool()
    errors: list[dict] = []

    accounts: list[str] = []
    triaged: list[str] = []
    posts_count = 0
    stories_count = 0
    accounts_cache_hit = 0
    apify_cost = 0.0
    items: list[dict] = []
    events: list[Event] = []
    guide = None

    # --- Budget check ---
    monthly_spent = await cost_db.monthly_spend_usd(pool)
    budget_blocked = monthly_spent >= settings.MONTHLY_BUDGET_USD

    # --- DISCOVER ---
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

    # --- TRIAGE ---
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

    # --- SCRAPE (cache-first; bypass actor entirely if budget blocked) ---
    if triaged:
        try:
            if budget_blocked:
                # Cache-only: read what we have, do not call Apify.
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
                # Count items by origin (covers both cache hits and freshly scraped).
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

    # --- EXTRACT ---
    if items:
        try:
            events = await parse_events(items, reference_date=request.date)
        except Exception as exc:
            logger.error("EXTRACT failed: %s", exc)
            errors.append({"stage": "extract", "error": str(exc), "traceback": traceback.format_exc()})

    # --- Dedupe / distance / vibe filter ---
    if events:
        events = _dedupe_events(events)
        for ev in events:
            if ev.latitude is not None and ev.longitude is not None:
                ev.distance_km = calculate_distance(latitude, longitude, ev.latitude, ev.longitude)
        if request.vibes:
            requested = set(request.vibes)
            events = [ev for ev in events if not ev.vibes or set(ev.vibes) & requested]

    # --- SCORE ---
    if events:
        try:
            events = await rate_events(events, vibes=request.vibes)
        except Exception as exc:
            logger.error("SCORE failed: %s", exc)
            errors.append({"stage": "score", "error": str(exc), "traceback": traceback.format_exc()})

        events.sort(key=lambda e: (composite_score(e), e.engagement_score), reverse=True)
        events = events[: request.max_results]

    # --- CURATE ---
    if events:
        try:
            guide = await compose_guide(events=events, city=city_name, vibes=request.vibes)
        except Exception as exc:
            logger.error("CURATE failed: %s", exc)
            errors.append({"stage": "curate", "error": str(exc), "traceback": traceback.format_exc()})

    elapsed = round(time.monotonic() - t0, 3)

    # --- Persist run ---
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


@app.get("/api/cities", response_model=list[CityInfo])
async def list_cities() -> list[CityInfo]:
    return [
        CityInfo(
            name=key.title(),
            country=data["country"],
            latitude=data["lat"],
            longitude=data["lon"],
            timezone=data["tz"],
        )
        for key, data in sorted(CITY_COORDINATES.items())
    ]


@app.get("/api/vibes")
async def list_vibes() -> list[dict[str, str]]:
    return [
        {"value": vibe.value, "label": vibe.name.replace("_", " ").title()}
        for vibe in EventVibe
    ]


@app.get("/api/health")
async def health_check() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "service": "city-event-crawler",
        "version": "2.1.0",
        "model": settings.CLAUDE_MODEL,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True, log_level="info")
