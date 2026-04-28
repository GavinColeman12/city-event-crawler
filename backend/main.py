"""
City Event Crawler v2 — FastAPI HTTP wrapper around backend.pipeline.

The actual pipeline (DISCOVER → TRIAGE → SCRAPE → EXTRACT → SCORE → CURATE)
lives in backend.pipeline and can be called directly from any async context
(Streamlit, scripts, notebooks). FastAPI is kept as an optional HTTP entry
point — useful for local dev or when you want a public API surface.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .config import CITY_COORDINATES, get_settings
from .db import close_pool
from .models import (
    CityInfo,
    EventVibe,
    SearchRequest,
    SearchResponse,
)
from .pipeline import run_search

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
    version="2.2.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/search", response_model=SearchResponse)
async def search_events(request: SearchRequest) -> SearchResponse:
    """Run the v2 pipeline and return events + curated guide + cost telemetry."""
    try:
        return await run_search(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
        "version": "2.2.0",
        "model": settings.CLAUDE_MODEL,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True, log_level="info")
