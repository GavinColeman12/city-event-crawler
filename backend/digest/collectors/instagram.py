"""Instagram posts for the city digest (optional, Apify-backed).

Instagram posts are not structured events, so this module does NOT implement
``collect()``.  It returns compact post dicts that the digest's ranking step
(Claude) reads to spot events announced on Instagram::

    {"handle", "url", "caption", "posted_at" (ISO, tz-aware), "likes",
     "comments", "is_video", "image_url"}

Public surface::

    SOURCE = "instagram"
    CITY_HANDLES = {"barcelona": [...]}
    async def collect_posts(city, client, token, handles=None,
                            per_account=3, max_age_days=5) -> list[dict]
    def parse_posts(items, now, max_age_days) -> list[dict]      # pure

How it scrapes
--------------
ONE run of the Apify actor ``apify/instagram-api-scraper`` (the same actor and
input shape ``backend/instagram/scraper.py`` uses) through Apify's REST API
(``run-sync-get-dataset-items``), plain httpx, token in an ``Authorization``
header only.  Input::

    {"directUrls": [profile urls], "resultsType": "posts",
     "resultsLimit": per_account,          # applies per profile URL
     "onlyPostsNewerThan": "YYYY-MM-DD"}   # cuts stale/pinned posts server side

Cost (pay-per-event, FREE-tier prices as published on the actor page and
matching ``APIFY_POSTS_USD_PER_1K = 2.30`` in backend/config.py):
    $0.0010 actor start + $0.0013 date filter + $0.0023 per post returned.
So one run is about $0.0023 + $0.0023 * posts.  20 handles x 3 posts (the
ceiling, 60 posts) is about $0.14; the date filter usually makes it far less.
3 handles x 2 posts is under $0.02.

Failures raise ``CollectorError`` with a short reason (no token, 401 bad token,
402 / "usage limit" account cap, 403 blocked account (e.g. unpaid invoices), actor timeout, other HTTP errors).  The
account has a monthly hard cap, so callers must treat this source as optional.

Python 3.9 compatible; stdlib + httpx only.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import httpx

from backend.digest.collectors import CollectorError

logger = logging.getLogger(__name__)

SOURCE = "instagram"

APIFY_ACTOR = "apify/instagram-api-scraper"
APIFY_RUN_URL = "https://api.apify.com/v2/acts/apify~instagram-api-scraper/run-sync-get-dataset-items"
APIFY_TIMEOUT_SECS = 120          # server-side actor run limit
USD_PER_POST = 0.0023             # FREE-tier price; see module docstring

# Default list: ONLY handles confirmed from two independent sources (the
# organisation's own site plus a second page naming the same exact handle).
# Lowercase, no "@", no duplicates (a test enforces this).  Activity could not
# be checked directly: Instagram blocks anonymous page loads.
CITY_HANDLES: Dict[str, List[str]] = {
    "barcelona": [
        "razzmatazzclubs",               # Razzmatazz club (site + guidebook listing)
        "sala_apolo",                    # Sala Apolo / Nitsa (site + Wikidata)
        "newinbarcelona",                # New in Barcelona expat community (site + Meetup page)
        "barcelonainternationalsocial",  # Barcelona International Social (site + Meetup/search listing)
        "planesenbarcelona",             # Planes en Barcelona city guide, ~236K followers (added at Gavin's request)
        "barcelonasecreta",              # Barcelona Secreta city guide, ~995K followers (added at Gavin's request)
    ],
}

# NOT used by default.  Each is linked from the organisation's own website
# (exact spelling seen there) but has only that one source and no confirmed
# recent activity.  Promote to CITY_HANDLES after a successful live run shows
# recent posts from the handle.
CANDIDATE_HANDLES: Dict[str, List[str]] = {
    "barcelona": [
        "timeoutbcn", "barcelonametropolitan", "thisisbarcelona_official",
        "ottozutzclub", "moog_barcelona", "opiumbarcelona", "shokobarcelona", "laterrrazza",
        "brunchbcn", "pobleespanyol",
    ],
}


def _profile_url(handle: str) -> str:
    return "https://www.instagram.com/%s/" % handle.strip().lstrip("@").lower()


def _to_int(value: Any) -> Optional[int]:
    """Apify reports -1 when likes are hidden; that and junk become None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 else None


def _parse_ts(value: Any) -> Optional[datetime]:
    """ISO string or epoch seconds -> tz-aware UTC datetime, else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        # Python 3.9 fromisoformat() rejects "+0100" offsets and odd fractions.
        text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
        text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def _is_error_row(item: Dict[str, Any]) -> bool:
    return bool(item.get("error")) or item.get("errorDescription") is not None


def _owner(item: Dict[str, Any]) -> str:
    owner = item.get("ownerUsername") or (item.get("owner") or {}).get("username") or ""
    return str(owner).lower()


def _is_video(item: Dict[str, Any]) -> bool:
    kind = str(item.get("type") or item.get("productType") or "").lower()
    return kind in ("video", "clips", "reel") or bool(item.get("videoUrl"))


def _image_url(item: Dict[str, Any]) -> Optional[str]:
    for key in ("displayUrl", "imageUrl", "thumbnailUrl"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def _post_url(item: Dict[str, Any]) -> Optional[str]:
    url = item.get("url")
    if isinstance(url, str) and url.startswith("http"):
        return url
    code = item.get("shortCode")
    if code:
        return "https://www.instagram.com/p/%s/" % code
    return None


def parse_posts(items: Iterable[Dict[str, Any]], now: datetime, max_age_days: int = 5) -> List[dict]:
    """Pure: raw Apify items -> compact post dicts, newest first.

    Drops error rows, rows with no usable date / url / owner, and posts older
    than ``max_age_days`` (or dated more than a day in the future).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    out: List[dict] = []
    seen = set()
    for item in items:
        if not isinstance(item, dict) or _is_error_row(item):
            continue
        posted = _parse_ts(item.get("timestamp") or item.get("takenAtTimestamp"))
        url = _post_url(item)
        handle = _owner(item)
        if posted is None or not url or not handle:
            continue
        if posted < cutoff or posted > now + timedelta(days=1):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({
            "handle": handle,
            "url": url,
            "caption": (item.get("caption") or "").strip(),
            "posted_at": posted.isoformat(),
            "likes": _to_int(item.get("likesCount")),
            "comments": _to_int(item.get("commentsCount")),
            "is_video": _is_video(item),
            "image_url": _image_url(item),
        })
    out.sort(key=lambda p: p["posted_at"], reverse=True)
    return out


def _raise_for_status(resp: httpx.Response) -> None:
    code = resp.status_code
    if code < 400:
        return
    try:
        body = resp.json().get("error", {})
        message = "%s %s" % (body.get("type", ""), body.get("message", ""))
    except Exception:
        message = resp.text[:200]
    low = message.lower()
    if code == 401:
        raise CollectorError("Apify rejected the token (401)")
    if "invoice" in low or code == 403 and "platform-feature-disabled" in low:
        raise CollectorError("Apify account blocked (%s)" % " ".join(message.split())[:80])
    if code == 402 or ("usage" in low and "limit" in low) or "hard limit" in low:
        raise CollectorError("Apify monthly usage limit reached (%d)" % code)
    if code == 408 or "timed out" in low or "timeout" in low:
        raise CollectorError("Apify actor run timed out")
    raise CollectorError("Apify HTTP %d: %s" % (code, " ".join(message.split())[:120]))


async def collect_posts(
    city: Any,
    client: httpx.AsyncClient,
    token: str,
    handles: Optional[List[str]] = None,
    per_account: int = 3,
    max_age_days: int = 5,
) -> List[dict]:
    """Run the posts actor once for all ``handles`` and return parsed posts."""
    if not token or not token.strip():
        raise CollectorError("no Apify token configured")
    key = getattr(city, "key", city)
    if handles is None:
        if key not in CITY_HANDLES:
            raise CollectorError("instagram: no handles configured for %s" % key)
        handles = CITY_HANDLES[key]
    handles = [h.strip().lstrip("@").lower() for h in handles if h and h.strip()]
    if not handles:
        return []

    now = datetime.now(timezone.utc)
    run_input = {
        "directUrls": [_profile_url(h) for h in handles],
        "resultsType": "posts",
        "resultsLimit": per_account,
        "onlyPostsNewerThan": (now - timedelta(days=max_age_days)).strftime("%Y-%m-%d"),
    }
    try:
        resp = await client.post(
            APIFY_RUN_URL,
            params={"timeout": APIFY_TIMEOUT_SECS},
            json=run_input,
            headers={"Authorization": "Bearer %s" % token.strip()},
            timeout=httpx.Timeout(APIFY_TIMEOUT_SECS + 30.0, connect=10.0),
        )
    except httpx.TimeoutException:
        raise CollectorError("Apify actor run timed out")
    except httpx.HTTPError as exc:
        raise CollectorError("Apify request failed: %s" % type(exc).__name__)
    _raise_for_status(resp)
    try:
        items = resp.json()
    except ValueError:
        raise CollectorError("Apify returned a non-JSON response")
    if not isinstance(items, list):
        raise CollectorError("Apify returned an unexpected payload")
    posts = parse_posts(items, now, max_age_days)
    logger.info("instagram: %d items -> %d recent posts from %d handles", len(items), len(posts), len(handles))
    return posts


def _load_token() -> str:
    token = os.environ.get("INSTAGRAM_APIFY_TOKEN") or os.environ.get("APIFY_TOKEN") or ""
    if token:
        return token
    for path in (".env", os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    name, _, value = line.strip().partition("=")
                    if name.strip() == "INSTAGRAM_APIFY_TOKEN":
                        return value.strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def main() -> int:
    import asyncio

    from backend.digest.cities import get_city
    from backend.digest.http import make_client

    token = _load_token()
    handles = CITY_HANDLES["barcelona"][:3]

    async def run() -> List[dict]:
        async with make_client() as client:
            return await collect_posts(get_city("barcelona"), client, token, handles=handles, per_account=2)

    try:
        posts = asyncio.run(run())
    except CollectorError as exc:
        print("instagram: FAILED - %s" % exc)
        return 1
    print("instagram: %d posts from %s (token configured: %s)" % (len(posts), ", ".join(handles), bool(token)))
    for p in posts:
        print("  %s  @%s  likes=%s  %s" % (p["posted_at"][:16], p["handle"], p["likes"], p["caption"][:70].replace("\n", " ")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
