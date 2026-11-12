"""Offline tests for the digest Instagram collector.

FIXTURE NOTE: tests/backend/fixtures/digest_instagram.json is SYNTHETIC.  The
Apify account refused the live run (HTTP 403 "Too many outstanding invoices"),
so the items were hand-built in the shape the apify/instagram-api-scraper rows
have in this repo (ownerUsername, shortCode, url, caption, timestamp,
likesCount, commentsCount, type, displayUrl, error/errorDescription).
"""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from backend.digest.collectors import CollectorError
from backend.digest.collectors import instagram as ig

FIXTURE = Path(__file__).parent / "fixtures" / "digest_instagram.json"
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def items():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_parse_posts_shape_and_order(items):
    posts = ig.parse_posts(items, NOW, max_age_days=5)
    assert [p["url"].split("/")[-2] for p in posts] == ["SYN004", "SYN001", "SYN005", "SYN002", "SYN006"]
    first = posts[0]
    assert set(first) == {"handle", "url", "caption", "posted_at", "likes", "comments", "is_video", "image_url"}
    assert first["handle"] == "newinbarcelona"
    assert first["likes"] is None            # -1 (hidden) -> None
    assert first["comments"] == 5
    assert datetime.fromisoformat(first["posted_at"]).utcoffset() is not None
    assert posts[3]["is_video"] is True and posts[3]["url"].endswith("SYN002/")
    assert posts[-1]["handle"] == "sala_apolo"   # "+0100" offset parsed
    assert posts[1]["image_url"].startswith("https://")
    stamps = [p["posted_at"] for p in posts]
    assert stamps == sorted(stamps, reverse=True)


def test_age_cutoff(items):
    short = ig.parse_posts(items, NOW, max_age_days=2)
    assert {p["url"].split("/")[-2] for p in short} == {"SYN004", "SYN001", "SYN005"}
    assert all("SYN003" not in p["url"] for p in ig.parse_posts(items, NOW, max_age_days=5))
    assert any("SYN003" in p["url"] for p in ig.parse_posts(items, NOW, max_age_days=30))


def test_error_and_undated_rows_dropped(items):
    posts = ig.parse_posts(items, NOW, max_age_days=365)
    urls = " ".join(p["url"] for p in posts)
    assert "SYN007" not in urls                       # no timestamp
    assert all(p["handle"] and p["caption"] is not None for p in posts)
    assert len(posts) == 6


def test_empty_token_raises():
    async def run(token):
        async with httpx.AsyncClient() as client:
            await ig.collect_posts("barcelona", client, token)
    for token in ("", "   ", None):
        with pytest.raises(CollectorError, match="no Apify token configured"):
            asyncio.run(run(token))


def test_http_errors_map_to_collector_error():
    cases = [
        (401, {"error": {"type": "user-or-token-not-found", "message": "x"}}, "token"),
        (402, {"error": {"type": "payment-required", "message": "x"}}, "usage limit"),
        (403, {"error": {"type": "platform-feature-disabled", "message": "Too many outstanding invoices"}}, "blocked"),
    ]
    for status, body, word in cases:
        def handler(request, status=status, body=body):
            assert request.headers["authorization"] == "Bearer tok"
            assert "tok" not in str(request.url)
            return httpx.Response(status, json=body)

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await ig.collect_posts("barcelona", client, "tok", handles=["sala_apolo"])
        with pytest.raises(CollectorError, match=word):
            asyncio.run(run())


def test_collect_posts_with_mock_transport(items):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=items)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ig.collect_posts("barcelona", client, "tok", handles=["@Sala_Apolo"], per_account=2, max_age_days=3650)
    posts = asyncio.run(run())
    assert seen["body"]["resultsLimit"] == 2
    assert seen["body"]["directUrls"] == ["https://www.instagram.com/sala_apolo/"]
    assert seen["body"]["resultsType"] == "posts"
    assert len(posts) == 6


def test_barcelona_handles_clean():
    handles = ig.CITY_HANDLES["barcelona"]
    assert handles
    for h in handles:
        assert h == h.lower() and "@" not in h and h.strip() == h and " " not in h
    assert len(handles) == len(set(handles))
    assert not set(handles) & set(ig.CANDIDATE_HANDLES["barcelona"])
