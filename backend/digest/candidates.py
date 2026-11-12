"""From raw collector output to the ranked candidate list a digest is picked from.

Steps: merge duplicates → bucket into today / tomorrow (dropping what is already
over) → score every candidate with a transparent heuristic.

The heuristic is a pre-sort, not the final word.  It decides which candidates
the ranking model gets to read and it powers the no-model ``auto`` mode.  Every
point it awards is written to ``signals`` so a pick can always be explained.
"""
from __future__ import annotations

import dataclasses
import difflib
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from backend.digest.cities import NIGHT_ROLLOVER_HOUR, City
from backend.digest.guardrails import fold
from backend.digest.models import DigestEvent

# When two listings are the same event, the one with the bigger attendance
# number becomes the primary; this order breaks ties.
SOURCE_PRIORITY = ("meetup", "ra", "luma", "xceed", "dice", "eventbrite")


@dataclass
class Candidate:
    event: DigestEvent
    also_on: List[dict] = field(default_factory=list)  # other listings of the same event
    day: str = ""                 # "today" | "tomorrow"
    in_progress: bool = False     # already started but still worth going to
    score: float = 0.0
    signals: List[str] = field(default_factory=list)
    language: str = "unknown"     # "en" | "local" | "unknown"

    @property
    def id(self) -> str:
        return self.event.event_id

    def same_source_attending(self) -> Optional[int]:
        """Attendance summed over every listing of this event on the primary source.

        Organisers cross-post one meetup into several groups; each listing has
        its own RSVP list, so the headcount for the night is the sum.
        """
        numbers = [self.event.attending] + [
            other.get("attending") for other in self.also_on if other.get("source") == self.event.source
        ]
        numbers = [n for n in numbers if isinstance(n, int)]
        return sum(numbers) if numbers else None

    def to_dict(self, description_chars: int = 400) -> dict:
        data = self.event.to_dict()
        data.pop("image_url", None)
        data.pop("source_id", None)
        data["description"] = _trim(data.get("description"), description_chars)
        data.update(
            day=self.day,
            in_progress=self.in_progress,
            also_on=self.also_on,
            score=round(self.score, 1),
            signals=self.signals,
            language=self.language,
        )
        return data


def _trim(text: Optional[str], limit: int) -> Optional[str]:
    if not text:
        return None
    flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
    if len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0].rstrip(",.;:-") + "…"


# ───────────────────────── duplicates ─────────────────────────

# Words that say nothing about which event or venue this is.  Leaving them in
# makes unrelated listings look alike ("… Barcelona" vs "… Barcelona").
_STOP_TOKENS = frozenset(
    "barcelona bcn spain the at en el la de del with presents pres presenta and by amb con in of a i y".split()
)
_GENERIC_VENUES = frozenset(["", "tba", "tbc", "secret location", "online", "catalunya", "city center", "city centre"])


def _key(text: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", fold(text)).strip()


def _distinctive(text: Optional[str]) -> str:
    return " ".join(t for t in _key(text).split() if t not in _STOP_TOKENS)


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


class _Fingerprint:
    __slots__ = ("event", "title", "tokens", "venue", "organizer")

    def __init__(self, event: DigestEvent):
        self.event = event
        self.title = _distinctive(event.title)
        self.tokens = frozenset(t for t in self.title.split() if len(t) > 1)
        venue = _distinctive(event.venue)
        self.venue = "" if (venue in _GENERIC_VENUES or venue.startswith("tba ")) else venue
        self.organizer = _key(event.organizer)


def _titles_match(a: _Fingerprint, b: _Fingerprint) -> bool:
    if not a.title or not b.title:
        return False
    if _ratio(a.title, b.title) >= 0.8:
        return True
    # Ticketing sites decorate titles ("FREE GUEST LIST - …"), so also accept
    # one title's distinctive words being mostly contained in the other's.
    shared = len(a.tokens & b.tokens)
    smaller = min(len(a.tokens), len(b.tokens))
    return shared >= 2 and shared / float(smaller) >= 0.6


def _venues_match(a: _Fingerprint, b: _Fingerprint) -> bool:
    if len(a.venue) >= 4 and len(b.venue) >= 4 and (a.venue in b.venue or b.venue in a.venue):
        return True
    return _ratio(a.venue, b.venue) >= 0.8


def _same_event(a: _Fingerprint, b: _Fingerprint) -> bool:
    gap_minutes = abs((a.event.start - b.event.start).total_seconds()) / 60.0
    if gap_minutes > 180:
        return False
    if a.event.source == b.event.source:
        if a.event.url == b.event.url:
            return True
        if a.organizer and a.organizer == b.organizer:
            # One organiser listing twice on one site means two events (two
            # pitches, two levels, two rooms) — never add their headcounts up.
            return False
    if a.venue and b.venue:
        return gap_minutes <= 120 and _venues_match(a, b) and _titles_match(a, b)
    # Venue unknown on at least one side: only a near-identical title and time will do.
    return gap_minutes <= 30 and _ratio(a.title, b.title) >= 0.9


def _priority(source: str) -> int:
    return SOURCE_PRIORITY.index(source) if source in SOURCE_PRIORITY else len(SOURCE_PRIORITY)


def _is_rsvp_only_price(ev: DigestEvent) -> bool:
    return ev.is_free is None and (ev.price or "").lower().startswith("free to rsvp")


def merge_duplicates(events: List[DigestEvent]) -> List[Candidate]:
    """Collapse listings of the same event (across sources, or cross-posted on one)."""
    prints = sorted((_Fingerprint(ev) for ev in events), key=lambda fp: fp.event.start)
    parent = list(range(len(prints)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(prints):
        for j in range(i + 1, len(prints)):
            b = prints[j]
            if (b.event.start - a.event.start).total_seconds() > 180 * 60:
                break
            if find(i) != find(j) and _same_event(a, b):
                parent[find(j)] = find(i)

    groups: Dict[int, List[DigestEvent]] = {}
    for index, fp in enumerate(prints):
        groups.setdefault(find(index), []).append(fp.event)

    merged: List[Candidate] = []
    for group in groups.values():
        group.sort(key=lambda ev: (-(ev.attending if ev.attending is not None else -1), _priority(ev.source)))
        primary = group[0]
        fill: dict = {}
        for other in group[1:]:
            # Descriptive gaps may be filled from another listing.  Numbers may
            # not: a capacity or follower count belongs to the site that
            # published it, and would be misattributed on the primary.  Nor may
            # the neighbourhood: it is each site's own geocoding, and one of
            # them files the W Hotel on the beach under "Barri Gòtic".
            for name in ("end", "venue", "address", "price", "is_free", "sold_out",
                         "description", "organizer"):
                if getattr(primary, name) in (None, "") and name not in fill and getattr(other, name) not in (None, ""):
                    fill[name] = getattr(other, name)
            if not primary.lineup and other.lineup and "lineup" not in fill:
                fill["lineup"] = list(other.lineup)
        # "Free to RSVP" only says the RSVP costs nothing.  When the same event
        # is also listed with a real ticket price, that price is the fact.
        if _is_rsvp_only_price(primary):
            for other in group[1:]:
                if other.price and not _is_rsvp_only_price(other):
                    fill["price"], fill["is_free"] = other.price, other.is_free
                    break
        tags: List[str] = []
        for ev in group:
            for tag in ev.tags:
                if tag and str(tag) not in tags:
                    tags.append(str(tag))
        fill["tags"] = tags[:12]
        candidate = Candidate(event=dataclasses.replace(primary, **fill))
        for other in group[1:]:
            candidate.also_on.append({
                "source": other.source,
                "url": other.url,
                "attending": other.attending,
                "attending_label": other.attending_label,
                "organizer": other.organizer,
                "price": other.price,
            })
        merged.append(candidate)
    return merged


# ───────────────────────── today / tomorrow ─────────────────────────

def digest_day(moment: datetime, city: City) -> date:
    """The evening an instant belongs to: anything before 06:00 counts as the day before."""
    return (moment.astimezone(city.zone) - timedelta(hours=NIGHT_ROLLOVER_HOUR)).date()


def still_worth_going(ev: DigestEvent, now: datetime) -> bool:
    if ev.start >= now:
        return True
    if ev.end is not None and ev.end > ev.start:
        return ev.end - now >= timedelta(minutes=90)
    return now - ev.start <= timedelta(minutes=45)


def bucket(candidates: List[Candidate], city: City, now: datetime) -> Tuple[List[Candidate], List[Candidate]]:
    """Split into (today, tomorrow); drop online events, other days, and what is already over."""
    today = digest_day(now, city)
    tomorrow = today + timedelta(days=1)
    today_list: List[Candidate] = []
    tomorrow_list: List[Candidate] = []
    for candidate in candidates:
        ev = candidate.event
        if ev.is_online:
            continue
        day = digest_day(ev.start, city)
        if day == today:
            if not still_worth_going(ev, now):
                continue
            candidate.day = "today"
            candidate.in_progress = ev.start < now
            today_list.append(candidate)
        elif day == tomorrow:
            candidate.day = "tomorrow"
            tomorrow_list.append(candidate)
    return today_list, tomorrow_list


# ───────────────────────── scoring ─────────────────────────

# Attendance that counts as "packed" differs by source: 150 RSVPs is a huge
# meetup, while 150 "interested" on RA is an ordinary club night.
_POPULARITY_SCALE = {"meetup": 150.0, "luma": 250.0, "ra": 600.0}
_DEFAULT_SCALE = 400.0
_POPULARITY_MAX = 60.0
_SOURCE_PRIOR = {"meetup": 8, "ra": 8, "xceed": 7, "dice": 7, "luma": 6, "eventbrite": 5}

_BOOSTS = (
    ("international crowd", 8, r"international|expats?|erasmus|foreigners?|newcomers?|new in (?:town|barcelona)"
     r"|english[- ]speaking|in english|language exchange|intercambio de idiomas|intercanvi d.idiomes"
     r"|multilingual|polyglot|digital nomads?|couchsurfing|internations"),
    ("social", 5, r"social|mixer|meet new people|make (?:new )?friends|after ?work|networking|pub crawl"
     r"|bar crawl|speed (?:dating|friending)|game night|board ?games?|quiz|trivia|karaoke|open mic"
     r"|stand[- ]?up|comedy|improv"),
    ("nightlife / music", 5, r"dj|techno|house|disco|club night|rave|party|fiesta|live music|concert"
     r"|concierto|festival|jazz|reggaeton|salsa|bachata|rooftop|boat party|sunset|terraza|open[- ]air"
     r"|afro(?:beats?|house)?|hip[- ]hop"),
    # Bare "tech" and "ai" are deliberately absent: "tech house" is a genre.
    ("founders / tech", 5, r"start-?ups?|founders?|entrepreneurs?|hackathon|investors?|venture capital"
     r"|developers?|artificial intelligence|machine learning|saas"
     r"|tech (?:meetup|talks?|community|networking|founders|scene|event)"
     r"|ai (?:networking|meetup|agents?|builders|founders|engineers?|community|summit|night|tools)"),
    ("active / outdoors", 3, r"hik(?:e|es|ing)|run(?:ning)? club|volley(?:ball)?|padel|paddle|climbing"
     r"|surf(?:ing)?|yoga|bike ride|cycling"),
)
_BOOST_CAP = 14

# Penalties look at the title and tags only — descriptions mention "online
# tickets" or "kids welcome" far too casually to be evidence.
_PENALTIES = (
    ("online", -40, r"online|webinar|zoom|virtual|livestream"),
    ("kids / family", -25, r"kids|children|ninos|nens|infantil|toddlers?|family[- ]friendly|familiar(?:es)?"),
    ("course / seminar", -18, r"curso|course|certification|bootcamp|masterclass|seminar(?:io)?|trading"
     r"|forex|network marketing|mlm"),
    ("sales pitch", -20, r"real estate|inmobiliari[ao]s?|investment|invest(?:ing)? in|inversion(?:es)?"
     r"|passive income|financial freedom"),
    ("religious", -25, r"church|bible|worship|misa|prayer|iglesia"),
    ("support group", -20, r"support group|grief|recovery meeting"),
)
_HOT_TAGS = re.compile(r"selling fast|going fast|few tickets|last[- ]tickets|almost sold out|almost full")

_BOOSTS = tuple((name, pts, re.compile(r"\b(?:%s)\b" % pattern)) for name, pts, pattern in _BOOSTS)
_PENALTIES = tuple((name, pts, re.compile(r"\b(?:%s)\b" % pattern)) for name, pts, pattern in _PENALTIES)

_ENGLISH = frozenset(
    "the and with for you your our join from this that are will meet people night free drinks live "
    "music all new we us come bring have get who what every more about just can".split()
)
_LOCAL = frozenset(
    "el la los las del con para por una que es se su tu nuestro nuestra fiesta noche gratis entrada "
    "musica amb per els les dels nit festa gratuit gratuita esta este todos todas mas muy como cada "
    "vine ven tenemos som".split()
)


def detect_language(*texts: Optional[str]) -> str:
    """Rough guess: is the listing written in English?  ('en' | 'local' | 'unknown')"""
    words = re.findall(r"[a-z]+", fold(" ".join(t for t in texts if t)))
    english = sum(1 for w in words if w in _ENGLISH)
    local = sum(1 for w in words if w in _LOCAL)
    if english >= 2 and english > local * 1.2:
        return "en"
    if local >= 2 and local > english:
        return "local"
    return "unknown"


def _popularity_points(count: int, source: str) -> float:
    scale = _POPULARITY_SCALE.get(source, _DEFAULT_SCALE)
    return _POPULARITY_MAX * min(1.0, math.log1p(count) / math.log1p(scale))


def score(candidate: Candidate) -> None:
    """Fill ``candidate.score`` / ``signals`` / ``language``."""
    ev = candidate.event
    signals: List[str] = []

    # 1. Popularity: the best real headcount any listing of this event has.
    points = 0.0
    headcounts: List[Tuple[str, int, Optional[str]]] = []
    own = candidate.same_source_attending()
    if own:
        headcounts.append((ev.source, own, ev.attending_label))
    for other in candidate.also_on:
        if other.get("source") != ev.source and isinstance(other.get("attending"), int) and other["attending"]:
            headcounts.append((other["source"], other["attending"], other.get("attending_label")))
    if headcounts:
        source, count, label = max(headcounts, key=lambda h: _popularity_points(h[1], h[0]))
        points = _popularity_points(count, source)
        signals.append("%d %s (%s)" % (count, label or "attending", source))
    elif ev.organizer_followers:
        points = 0.35 * _POPULARITY_MAX * min(1.0, math.log1p(ev.organizer_followers) / math.log1p(20000.0))
        signals.append("organiser has %d followers" % ev.organizer_followers)
    tag_text = fold(" ".join(str(t) for t in ev.tags))
    if _HOT_TAGS.search(tag_text):
        points += 5
        signals.append("selling fast")
    if "ra pick" in tag_text:
        points += 6
        signals.append("RA Pick")
    if ev.capacity and own and 0.9 * ev.capacity <= own < ev.capacity:
        points += 3
        signals.append("nearly full")
    total = points

    # Popular enough to sell out is a compliment — but an event nobody can get
    # into any more is a poor recommendation, so it costs more than it earns.
    if ev.sold_out:
        total -= 12
        signals.append("sold out / full")

    # 2. Written in English → a fair sign internationals are the audience.
    candidate.language = detect_language(ev.title, ev.description)
    if candidate.language == "en":
        total += 6
        signals.append("listing in English")
    elif candidate.language == "local":
        total -= 2

    # 3. What kind of event it is.
    body = fold(" ".join(filter(None, [ev.title, ev.description, ev.organizer, " ".join(str(t) for t in ev.tags)])))
    boost = 0
    for name, pts, pattern in _BOOSTS:
        if pattern.search(body):
            boost += pts
            signals.append(name)
    total += min(boost, _BOOST_CAP)

    headline = fold(" ".join([ev.title, " ".join(str(t) for t in ev.tags)]))
    for name, pts, pattern in _PENALTIES:
        if pattern.search(headline):
            total += pts
            signals.append("penalty: %s" % name)
    # Some groups keep a standing listing only to point at their chat group;
    # they say so themselves, and there is nothing to turn up to.
    if "placeholder event" in fold(ev.description):
        total -= 30
        signals.append("penalty: placeholder listing")

    # 4. Where it was listed.
    total += _SOURCE_PRIOR.get(ev.source, 4)

    candidate.score = total
    candidate.signals = signals


def shortlist(candidates: List[Candidate], limit: int, per_source_floor: int = 3) -> List[Candidate]:
    """Best ``limit`` by score, but always keep each source's own top few."""
    ranked = sorted(candidates, key=lambda c: (-c.score, c.event.start))
    keep = {c.id: c for c in ranked[:limit]}
    seen: Dict[str, int] = {}
    for c in ranked:
        seen[c.event.source] = seen.get(c.event.source, 0) + 1
        if seen[c.event.source] <= per_source_floor:
            keep.setdefault(c.id, c)
    return sorted(keep.values(), key=lambda c: (-c.score, c.event.start))
