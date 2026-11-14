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

Stories
-------
``collect_stories`` runs a second actor, ``muhammetakkurtt/instagram-scraper``
(pay-per-result, the one ``backend/instagram/scraper.py`` uses for stories),
with ``{"usernames": [...], "resultsType": "stories"}``.  Stories expire after
24 hours and carry their text INSIDE the image, so each story dict has the
image (or video cover) URL; ``download_story_images`` saves those to disk so
the ranking step can look at them::

    {"kind": "story", "handle", "url", "caption", "posted_at", "likes": None,
     "comments": None, "is_video", "image_url", "image_path" (after download)}

The story row shape was not verified against a live run (no token was
available when this was written), so ``parse_stories`` accepts the field names
used by this actor's rows elsewhere in the repo plus Instagram's raw API names.

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

STORIES_ACTOR = "muhammetakkurtt/instagram-scraper"
STORIES_RUN_URL = "https://api.apify.com/v2/acts/muhammetakkurtt~instagram-scraper/run-sync-get-dataset-items"
MAX_STORIES_PER_ACCOUNT = 5
STORY_MAX_AGE_HOURS = 30          # stories live 24h; a little slack for clock skew
MAX_STORY_IMAGE_BYTES = 4_000_000

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


async def _run_actor(client: httpx.AsyncClient, run_url: str, run_input: dict, token: str) -> List[Any]:
    """One synchronous Apify actor run -> its dataset items."""
    try:
        resp = await client.post(
            run_url,
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
    return items


def _resolve_handles(city: Any, handles: Optional[List[str]]) -> List[str]:
    key = getattr(city, "key", city)
    if handles is None:
        if key not in CITY_HANDLES:
            raise CollectorError("instagram: no handles configured for %s" % key)
        handles = CITY_HANDLES[key]
    return [h.strip().lstrip("@").lower() for h in handles if h and h.strip()]


def _story_owner(item: Dict[str, Any]) -> str:
    owner = (item.get("username") or item.get("ownerUsername")
             or (item.get("user") or {}).get("username")
             or (item.get("owner") or {}).get("username") or "")
    return str(owner).lower()


def _story_image(item: Dict[str, Any]) -> Optional[str]:
    for key in ("displayUrl", "imageUrl", "image_url", "thumbnailUrl", "thumbnail_url", "display_url"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    versions = (item.get("image_versions2") or {}).get("candidates") or item.get("images") or []
    for cand in versions if isinstance(versions, list) else []:
        value = cand.get("url") if isinstance(cand, dict) else cand
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def _story_is_video(item: Dict[str, Any]) -> bool:
    kind = str(item.get("type") or item.get("mediaType") or "").lower()
    return (kind in ("video", "2") or item.get("media_type") == 2
            or bool(item.get("videoUrl") or item.get("video_url") or item.get("video_versions")))


def _story_text(item: Dict[str, Any]) -> str:
    caption = item.get("caption")
    if isinstance(caption, dict):
        caption = caption.get("text")
    parts = [caption, item.get("text"), item.get("accessibilityCaption") or item.get("accessibility_caption")]
    for mention in item.get("mentions") or []:
        if isinstance(mention, str):
            parts.append("@" + mention.lstrip("@"))
    return " · ".join(str(p).strip() for p in parts if isinstance(p, str) and p.strip())


def parse_stories(items: Iterable[Dict[str, Any]], now: datetime,
                  per_account: int = MAX_STORIES_PER_ACCOUNT) -> List[dict]:
    """Pure: raw stories-actor items -> compact story dicts, newest first.

    Drops error rows, rows with no owner or no image, stories older than
    ``STORY_MAX_AGE_HOURS`` and everything past ``per_account`` per handle.  A
    story with no readable timestamp is kept (it is live by definition) and
    dated ``now``.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=STORY_MAX_AGE_HOURS)
    rows: List[dict] = []
    seen = set()
    for item in items:
        if not isinstance(item, dict) or _is_error_row(item):
            continue
        handle = _story_owner(item)
        image = _story_image(item)
        if not handle or not image:
            continue
        posted = None
        for key in ("timestamp", "takenAt", "taken_at", "takenAtTimestamp", "taken_at_timestamp"):
            posted = _parse_ts(item.get(key))
            if posted is not None:
                break
        if posted is not None and (posted < cutoff or posted > now + timedelta(days=1)):
            continue
        story_id = str(item.get("id") or item.get("pk") or item.get("shortCode") or "").split("_")[0]
        ident = (handle, story_id or image)
        if ident in seen:
            continue
        seen.add(ident)
        url = "https://www.instagram.com/stories/%s/" % handle
        if story_id:
            url += "%s/" % story_id
        rows.append({
            "kind": "story",
            "handle": handle,
            "url": url,
            "caption": _story_text(item),
            "posted_at": (posted or now).isoformat(),
            "likes": None,
            "comments": None,
            "is_video": _story_is_video(item),
            "image_url": image,
        })
    rows.sort(key=lambda r: r["posted_at"], reverse=True)
    out: List[dict] = []
    per_owner: Dict[str, int] = {}
    for row in rows:
        if per_owner.get(row["handle"], 0) >= per_account:
            continue
        per_owner[row["handle"]] = per_owner.get(row["handle"], 0) + 1
        out.append(row)
    return out


async def collect_stories(
    city: Any,
    client: httpx.AsyncClient,
    token: str,
    handles: Optional[List[str]] = None,
    per_account: int = MAX_STORIES_PER_ACCOUNT,
) -> List[dict]:
    """Run the stories actor once for all ``handles`` and return live stories."""
    if not token or not token.strip():
        raise CollectorError("no Apify token configured")
    handles = _resolve_handles(city, handles)
    if not handles:
        return []
    items = await _run_actor(client, STORIES_RUN_URL, {"usernames": handles, "resultsType": "stories"}, token)
    stories = parse_stories(items, datetime.now(timezone.utc), per_account)
    logger.info("instagram: %d items -> %d live stories from %d handles", len(items), len(stories), len(handles))
    return stories


async def download_story_images(stories: List[dict], client: httpx.AsyncClient, directory: str) -> int:
    """Save each story's image under ``directory`` and set ``image_path``.

    Best effort: a story whose image cannot be fetched keeps ``image_url`` only.
    Returns how many were saved.
    """
    os.makedirs(directory, exist_ok=True)
    saved = 0
    for index, story in enumerate(stories, 1):
        url = story.get("image_url")
        if not url:
            continue
        try:
            resp = await client.get(url, timeout=httpx.Timeout(20.0, connect=10.0))
        except httpx.HTTPError:
            continue
        kind = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if resp.status_code != 200 or not kind.startswith("image/") or len(resp.content) > MAX_STORY_IMAGE_BYTES:
            continue
        ext = {"image/png": "png", "image/webp": "webp"}.get(kind, "jpg")
        name = "%02d_%s.%s" % (index, re.sub(r"[^a-z0-9_.]", "", story.get("handle", "")) or "story", ext)
        path = os.path.join(directory, name)
        with open(path, "wb") as fh:
            fh.write(resp.content)
        story["image_path"] = path
        saved += 1
    return saved


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
    handles = _resolve_handles(city, handles)
    if not handles:
        return []

    now = datetime.now(timezone.utc)
    run_input = {
        "directUrls": [_profile_url(h) for h in handles],
        "resultsType": "posts",
        "resultsLimit": per_account,
        "onlyPostsNewerThan": (now - timedelta(days=max_age_days)).strftime("%Y-%m-%d"),
    }
    items = await _run_actor(client, APIFY_RUN_URL, run_input, token)
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
