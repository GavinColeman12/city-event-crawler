"""Data contract shared by every digest collector.

Stdlib only, on purpose: the digest has to run in a bare cloud sandbox after
nothing more than ``pip install httpx``.  Must stay Python 3.9 compatible.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, Optional


@dataclass
class DigestEvent:
    """One event, as a source describes it.  Facts only — no scoring here."""

    # ── identity ──
    source: str                      # collector id: "meetup", "ra", "dice", ...
    title: str
    start: datetime                  # MUST be timezone-aware
    url: str                         # canonical public event page

    # ── when / where ──
    end: Optional[datetime] = None   # timezone-aware when known
    venue: Optional[str] = None
    address: Optional[str] = None
    area: Optional[str] = None       # neighbourhood / district, when the source gives one
    is_online: bool = False

    # ── what ──
    description: Optional[str] = None    # plain text; may be long, trimmed downstream
    organizer: Optional[str] = None      # promoter / Meetup group / host
    lineup: list = field(default_factory=list)   # artist / speaker names
    tags: list = field(default_factory=list)     # source-native genres / categories / topics

    # ── cost ──
    price: Optional[str] = None      # human-readable: "Free", "€15", "€12–20", "From €10"
    is_free: Optional[bool] = None
    sold_out: Optional[bool] = None

    # ── popularity: raw source-native numbers, None when the source has none ──
    attending: Optional[int] = None          # RSVPs / "going" / "interested" / guests
    attending_label: Optional[str] = None    # the source's own word: "going", "interested", "guests"
    capacity: Optional[int] = None           # RSVP limit / venue capacity, when published
    organizer_followers: Optional[int] = None    # group members / promoter followers
    extra_stats: dict = field(default_factory=dict)  # other numbers worth showing, e.g. {"waitlist": 40}

    # ── misc ──
    image_url: Optional[str] = None
    source_id: Optional[str] = None  # the source's own event id

    @property
    def event_id(self) -> str:
        """Short stable id used to refer to this event in picks files."""
        raw = "%s:%s" % (self.source, self.source_id or self.url)
        return "%s-%s" % (self.source, hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8])

    def to_dict(self) -> dict:
        out: dict = {"id": self.event_id}
        for f in fields(self):
            value: Any = getattr(self, f.name)
            if isinstance(value, datetime):
                value = value.isoformat()
            out[f.name] = value
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "DigestEvent":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        for key in ("start", "end"):
            if isinstance(kwargs.get(key), str):
                kwargs[key] = datetime.fromisoformat(kwargs[key])
        return cls(**kwargs)


def validate_event(ev: DigestEvent) -> None:
    """Raise ``ValueError`` if ``ev`` breaks the collector contract.

    Every collector test runs each parsed event through this.
    """
    if not ev.source:
        raise ValueError("event has no source")
    if not ev.title or not ev.title.strip():
        raise ValueError("%s event has an empty title" % ev.source)
    if not isinstance(ev.start, datetime) or ev.start.tzinfo is None or ev.start.utcoffset() is None:
        raise ValueError("%s event %r: start must be a timezone-aware datetime" % (ev.source, ev.title))
    if ev.end is not None and (not isinstance(ev.end, datetime) or ev.end.utcoffset() is None):
        raise ValueError("%s event %r: end must be timezone-aware when set" % (ev.source, ev.title))
    if not isinstance(ev.url, str) or not ev.url.startswith("http"):
        raise ValueError("%s event %r: url must be absolute, got %r" % (ev.source, ev.title, ev.url))
    for name in ("attending", "capacity", "organizer_followers"):
        value = getattr(ev, name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError("%s event %r: %s must be a non-negative int, got %r" % (ev.source, ev.title, name, value))
    if ev.attending is not None and not ev.attending_label:
        raise ValueError("%s event %r: attending is set but attending_label is not" % (ev.source, ev.title))
