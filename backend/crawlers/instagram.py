"""Instagram crawler - two-phase approach:

1. Researcher Agent discovers relevant Instagram accounts (clubs, promoters, venues)
2. This crawler uses Apify to scrape those accounts' recent posts
3. Posts are parsed for event details (dates, times, venues, lineups)

Supports both:
- Profile scraping (primary - scrapes discovered accounts)
- Hashtag scraping (secondary - searches city event hashtags)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from ..config import get_settings
from ..models import Event, EventSource, EventVibe
from ..utils.helpers import clean_text, generate_event_id, parse_date
from .base import BaseCrawler

logger = logging.getLogger(__name__)

# Apify actor - the main instagram-scraper handles profiles, hashtags, and posts
INSTAGRAM_SCRAPER = "apify~instagram-scraper"

CITY_HASHTAGS = {
    "budapest": ["budapestevents", "budapestnightlife", "budapestparty", "budapesttonight"],
    "berlin": ["berlinevents", "berlinnightlife", "berlintechno", "berlinparty", "berlinrave"],
    "prague": ["pragueevents", "praguenightlife", "pragueparty", "praguetonight"],
    "barcelona": ["barcelonaevents", "barcelonanightlife", "barcelonaparty"],
    "amsterdam": ["amsterdamevents", "amsterdamnightlife", "amsterdamparty"],
    "lisbon": ["lisbonevents", "lisbonnightlife", "lisbonparty"],
    "vienna": ["viennaevents", "viennanightlife", "viennaparty"],
    "warsaw": ["warsawevents", "warsawnightlife", "warsawparty"],
    "london": ["londonevents", "londonnightlife", "londonparty", "londontonight"],
    "paris": ["parisevents", "parisnightlife", "parisparty"],
}


class InstagramCrawler(BaseCrawler):
    source = EventSource.INSTAGRAM
    name = "Instagram"

    _research_context: dict | None = None

    def set_research_context(self, context: dict):
        """Receive research data from the Researcher Agent."""
        self._research_context = context

    async def crawl(self, city, date, lat, lon, radius_km, vibes=None, **kw):
        settings = get_settings()
        if not settings.INSTAGRAM_APIFY_TOKEN:
            self._log_warning("INSTAGRAM_APIFY_TOKEN not configured — skipping")
            return []

        city_lower = city.lower().strip()
        events, seen = [], set()

        # Phase 1: Scrape discovered Instagram accounts (from Researcher Agent)
        accounts = self._get_accounts_to_scrape(city_lower)
        if accounts:
            self._log_info("Scraping %d Instagram accounts for %s", len(accounts), city)
            profile_events = await self._scrape_profiles(accounts, city, date, settings.INSTAGRAM_APIFY_TOKEN, seen)
            events.extend(profile_events)

        # Phase 2: Search hashtags as supplementary
        city_tag = city_lower.replace(" ", "").replace("-", "")
        hashtags = CITY_HASHTAGS.get(city_lower, [f"{city_tag}events", f"{city_tag}nightlife"])
        hashtag_events = await self._search_hashtags(hashtags[:4], city, date, settings.INSTAGRAM_APIFY_TOKEN, seen)
        events.extend(hashtag_events)

        self._log_info("Found %d events from Instagram for %s", len(events), city)
        return self._filter_by_vibes(events, vibes)

    def _get_accounts_to_scrape(self, city: str) -> list[str]:
        """Get list of Instagram accounts from research context."""
        accounts = []

        # From Researcher Agent
        if self._research_context:
            research = self._research_context.get("research", {})
            accounts = list(research.get("instagram_accounts", []))

        # If no research data, fall back to city seeds
        if not accounts:
            from ..agents.researcher import CITY_SEED_DATA
            seed = CITY_SEED_DATA.get(city, {})
            accounts = list(seed.get("instagram_seeds", []))

        return accounts[:50]  # Cap at 50 accounts per search

    async def _scrape_profiles(self, accounts, city, date, token, seen):
        """Use Apify instagram-scraper to get recent posts from discovered accounts."""
        # Batch accounts to stay within free tier limits
        batch_size = 10
        all_posts = []

        for i in range(0, min(len(accounts), 30), batch_size):  # Cap at 30 accounts
            batch = accounts[i:i + batch_size]
            urls = [f"https://www.instagram.com/{a}/" for a in batch]

            resp = await self._post(
                f"https://api.apify.com/v2/acts/{INSTAGRAM_SCRAPER}/run-sync-get-dataset-items",
                params={"token": token},
                json={
                    "directUrls": urls,
                    "resultsLimit": 5,  # Last 5 posts per account
                    "resultsType": "posts",
                    "searchType": "user",
                },
            )
            if not resp:
                continue

            try:
                posts = resp.json()
                if isinstance(posts, list):
                    all_posts.extend(posts)
            except Exception:
                continue

        self._log_info("Got %d posts from %d accounts", len(all_posts), len(accounts))

        # Parse posts for events
        results = []
        for post in all_posts:
          try:
            caption = post.get("caption", "") or ""
            if not self._looks_like_event(caption):
                continue

            shortcode = post.get("shortCode", post.get("shortcode", post.get("id", "")))
            eid = generate_event_id(self.source.value, shortcode)
            if eid in seen:
                continue
            seen.add(eid)

            event_title = self._extract_title(caption)
            event_date = self._extract_date(caption, date)
            location = post.get("locationName") or post.get("location", "")
            if isinstance(location, dict):
                location = location.get("name", "")
            owner = post.get("ownerUsername", "")
            if not owner and isinstance(post.get("owner"), dict):
                owner = post["owner"].get("username", "")
            def _safe_count(val):
                if val is None:
                    return None
                try:
                    n = int(val)
                    return n if n >= 0 else None
                except (ValueError, TypeError):
                    return None
            likes = _safe_count(post.get("likesCount")) or _safe_count(post.get("likes"))
            comments = _safe_count(post.get("commentsCount")) or _safe_count(post.get("comments"))

            results.append(Event(
                id=eid,
                title=event_title,
                description=clean_text(caption[:1500]),
                date=event_date or parse_date(date),
                source=self.source,
                source_url=f"https://www.instagram.com/p/{shortcode}/" if shortcode else f"https://www.instagram.com/{owner}/",
                venue_name=location if location else None,
                image_url=post.get("displayUrl") or post.get("imageUrl") or post.get("url"),
                likes=likes,
                comments=comments,
                vibes=self.classify_vibes(event_title, caption),
                tags=[f"@{owner}"] if owner else [],
                organizer=f"@{owner}" if owner else None,
                raw_data={
                    "shortcode": shortcode,
                    "owner": owner,
                    "timestamp": post.get("timestamp"),
                    "type": "profile_scrape",
                },
            ))
          except Exception as exc:
            self._log_warning("Failed to parse Instagram post: %s", exc)
            continue

        return results

    async def _search_hashtags(self, hashtags, city, date, token, seen):
        """Use Apify Instagram Hashtag Scraper for supplementary discovery."""
        tag_urls = [f"https://www.instagram.com/explore/tags/{tag}/" for tag in hashtags]
        resp = await self._post(
            f"https://api.apify.com/v2/acts/{INSTAGRAM_SCRAPER}/run-sync-get-dataset-items",
            params={"token": token},
            json={
                "directUrls": tag_urls,
                "resultsLimit": 20,
                "resultsType": "posts",
                "searchType": "hashtag",
            },
        )
        if not resp:
            return []

        try:
            posts = resp.json()
        except Exception:
            return []

        if not isinstance(posts, list):
            return []

        results = []
        for post in posts:
          try:
            caption = post.get("caption", "") or ""
            if not self._looks_like_event(caption):
                continue

            shortcode = post.get("shortCode", post.get("id", ""))
            eid = generate_event_id(self.source.value, shortcode)
            if eid in seen:
                continue
            seen.add(eid)

            event_title = self._extract_title(caption)
            location = post.get("locationName") or ""
            if isinstance(post.get("location"), dict):
                location = post["location"].get("name", "")
            owner = post.get("ownerUsername", "")

            def _safe(val):
                try:
                    n = int(val) if val is not None else None
                    return n if n is not None and n >= 0 else None
                except (ValueError, TypeError):
                    return None

            results.append(Event(
                id=eid,
                title=event_title,
                description=clean_text(caption[:1500]),
                date=self._extract_date(caption, date) or parse_date(date),
                source=self.source,
                source_url=f"https://www.instagram.com/p/{shortcode}/" if shortcode else "https://www.instagram.com",
                venue_name=location if location else None,
                image_url=post.get("displayUrl") or post.get("imageUrl"),
                likes=_safe(post.get("likesCount")),
                comments=_safe(post.get("commentsCount")),
                vibes=self.classify_vibes(event_title, caption),
                tags=[f"@{owner}"] if owner else [],
                organizer=f"@{owner}" if owner else None,
                raw_data={"shortcode": shortcode, "type": "hashtag_scrape"},
            ))
          except Exception as exc:
            self._log_warning("Failed to parse hashtag post: %s", exc)
            continue

        return results

    def _looks_like_event(self, caption: str) -> bool:
        """Check if an Instagram caption promotes an event."""
        indicators = [
            r'\b\d{1,2}[./]\d{1,2}\b',             # Date: 13.04 or 13/04
            r'\b\d{1,2}:\d{2}\b',                    # Time: 22:00
            r'tickets?\b', r'entry\b',                # Tickets
            r'doors?\s*(open|at|from)',               # Doors open
            r'tonight\b', r'this\s+(fri|sat|sun|weekend)',  # Timing
            r'lineup\b', r'line[\s-]?up\b',          # Lineup
            r'featuring\b', r'feat\.?\b',             # Artists
            r'rsvp\b', r'free\s+entry', r'guestlist', # Entry
            r'join\s+us', r'come\s+(join|party|dance)', # CTAs
            r'event\b', r'party\b', r'club\s+night',  # Event words
            r'dj\s+set', r'live\s+music', r'concert',  # Music
            r'link\s+in\s+bio', r'swipe\s+up',        # IG CTAs
            r'limited\s+(spots|tickets|capacity)',     # Scarcity
            r'presale\b', r'early\s+bird',             # Sales
        ]
        caption_lower = caption.lower()
        matches = sum(1 for p in indicators if re.search(p, caption_lower))
        return matches >= 2

    def _extract_title(self, caption: str) -> str:
        """Extract event title from caption (usually the first meaningful line)."""
        lines = caption.strip().split("\n")
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("@"):
                continue
            # Remove leading emojis
            title = re.sub(r'^[\U0001F000-\U0001FFFF\U00002600-\U000027BF\s]+', '', line)
            if title and len(title) > 3:
                # Clean trailing hashtags
                title = re.split(r'\s*#', title)[0].strip()
                return title[:150]
        return caption[:100]

    def _extract_date(self, caption: str, fallback_date: str) -> datetime | None:
        """Try to extract an event date from the caption text."""
        # Common date patterns in event posts
        patterns = [
            # "13.04.2026" or "13/04/2026"
            (r'(\d{1,2})[./](\d{1,2})[./](20\d{2})', lambda m: f"{m.group(3)}-{m.group(2)}-{m.group(1)}"),
            # "April 13" or "13 April"
            (r'(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*', lambda m: None),
            (r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+(\d{1,2})', lambda m: None),
            # "13.04" (no year)
            (r'(\d{1,2})[./](\d{1,2})(?!\d)', lambda m: None),
        ]

        for pattern, formatter in patterns:
            match = re.search(pattern, caption.lower())
            if match:
                if formatter and formatter(match):
                    dt = parse_date(formatter(match))
                    if dt:
                        return dt
                # Fall back to dateutil for complex formats
                try:
                    full_match = match.group(0)
                    dt = parse_date(full_match)
                    if dt:
                        return dt
                except Exception:
                    pass

        return parse_date(fallback_date)
