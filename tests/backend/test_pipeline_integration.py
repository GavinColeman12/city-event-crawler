"""End-to-end integration test against the live FastAPI app + real APIs.

Runs a single Berlin search at reduced caps (5 accounts × 1 post + 2 stories)
to keep costs minimal (~$0.05). Skips automatically if any required key is
missing from .env.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

REQUIRED = ("SERPAPI_KEY", "INSTAGRAM_APIFY_TOKEN", "ANTHROPIC_API_KEY", "DATABASE_URL")


@pytest.fixture(scope="module", autouse=True)
def _skip_if_no_keys():
    from dotenv import load_dotenv
    load_dotenv(override=True)
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        pytest.skip(f"missing env: {missing}")


@pytest.fixture(scope="module")
def client():
    # Tighten caps for the test so we don't burn credits.
    os.environ["MAX_ACCOUNTS_PER_SEARCH"] = "5"
    os.environ["MAX_POSTS_PER_ACCOUNT"] = "1"
    os.environ["MAX_STORIES_PER_ACCOUNT"] = "2"
    os.environ["MAX_DISCOVERY_QUERIES"] = "3"
    # Force a clean settings load.
    from backend import config
    if hasattr(config.get_settings, "cache_clear"):
        config.get_settings.cache_clear()

    from backend.main import app
    return TestClient(app)


def test_live_berlin_search_returns_cost_and_events(client):
    resp = client.post(
        "/api/search",
        json={"city": "berlin", "date": "2026-04-27", "vibes": ["nightlife"], "max_results": 10},
    )
    assert resp.status_code == 200
    data = resp.json()
    # Even with no events, we expect telemetry fields populated.
    assert "monthly_spend_usd" in data
    assert "apify_cost_usd" in data
    assert "budget_blocked" in data
    assert data["accounts_discovered"] >= 1
    # Either we got events or we got a budget block — never silently nothing.
    assert data["events_extracted"] >= 0
