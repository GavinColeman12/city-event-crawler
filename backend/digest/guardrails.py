"""Deterministic content guardrail: kink / fetish / swinger events never reach the digest.

The ranking model is told to leave these out as well, but a prompt is a request
and this is a guarantee — an event that matches is removed before anything
ranks it.  Terms are deliberately unambiguous.  Wording that also has an
innocent meaning ("sensual bachata", "swing dance", "tantra", "burlesque",
"darkroom" photography, a "pornstar martini") is NOT matched here and is left
to the ranker's judgment.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Optional, Tuple

from backend.digest.models import DigestEvent

_TERMS = (
    r"kink(?:y|s|ster|sters)?",
    r"fetish(?:es|ist|ists)?",
    r"fetich(?:e|es|ista|istas|ismo)",
    r"bdsm",
    r"bondage",
    r"shibari",
    r"kinbaku",
    r"dominatrix",
    r"femdom",
    r"swingers?",
    r"intercambio\s+de\s+parejas",
    r"play\s*part(?:y|ies)",
    r"sex[\s-]*positiv\w*",
    r"sex\s+(?:party|parties|club)",
    r"org(?:y|ies|ia|ias)",
    r"(?:fiesta|club|local|ambiente|pareja|sala)s?\s+liberal(?:es)?",
    r"liberal(?:es)?\s+(?:party|club|fiesta)",
    r"erotic\w*",
    r"glory\s*hole",
    r"cruising\s+(?:bar|club|night|party|area|zone)",
    r"zona\s+cruising",
    r"(?:naked|nude)\s+(?:party|night|rave|disco)",
    r"nudist\w*",
    r"rope\s+(?:jam|bondage|play)",
    r"striptease",
    r"strip\s+club",
    r"lap\s*dance",
    r"torture\s+garden",
    r"killing\s+kittens",
    r"skirt\s+club",
    r"pornceptual",
    r"kit\s*kat\s*club",
)

_BLOCKED = re.compile(r"\b(?:%s)\b" % "|".join(_TERMS))


def fold(text: Optional[str]) -> str:
    """Lower-case and strip accents, so 'Erótico' and 'orgía' match plain patterns."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def blocked_term(*texts: Optional[str]) -> Optional[str]:
    """Return the first blocked term found in any of ``texts``, else None."""
    for text in texts:
        if not text:
            continue
        match = _BLOCKED.search(fold(text))
        if match:
            return re.sub(r"\s+", " ", match.group(0))
    return None


def event_blocked_term(ev: DigestEvent) -> Optional[str]:
    return blocked_term(
        ev.title,
        ev.description,
        ev.organizer,
        ev.venue,
        " ".join(str(t) for t in ev.tags),
        " ".join(str(a) for a in ev.lineup),
    )


def filter_events(events: Iterable[DigestEvent]) -> Tuple[List[DigestEvent], List[dict]]:
    """Split events into (kept, removed).  ``removed`` records why, for auditing."""
    kept: List[DigestEvent] = []
    removed: List[dict] = []
    for ev in events:
        term = event_blocked_term(ev)
        if term:
            removed.append({"title": ev.title, "source": ev.source, "url": ev.url, "term": term})
        else:
            kept.append(ev)
    return kept, removed
