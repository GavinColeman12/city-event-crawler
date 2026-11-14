"""Turn a candidates payload + a picks file into the WhatsApp message.

Division of labour: whoever ranks (the scheduled Claude routine, or the
``auto`` heuristic) only chooses event ids and writes one "why" line each.
Every fact in the message — title, time, venue, price, headcounts, link — is
rendered here straight from the collected data, so a number in the digest is
always a number a source actually published.

Picks file::

    {"today":    [{"id": "meetup-1a2b3c4d", "why": "..."}, ... up to 5],
     "tomorrow": [...],
     "note": "optional one-line heads-up shown under the header"}

A pick may instead carry a ``custom`` block for something found outside the
collectors (web search, an Instagram post)::

    {"custom": {"title": "...", "when": "Tonight 21:00", "where": "Palau Sant Jordi",
                "url": "https://...", "price": "From €45", "stat": "17,000 capacity, sold out"},
     "why": "..."}
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from backend.digest.cities import NIGHT_ROLLOVER_HOUR
from backend.digest.guardrails import blocked_term

MAX_PICKS_PER_DAY = 5
MAX_WHY_CHARS = 170
MAX_TITLE_CHARS = 80

SOURCE_LABEL = {
    "meetup": "Meetup", "ra": "RA", "dice": "DICE", "xceed": "Xceed",
    "eventbrite": "Eventbrite", "luma": "Luma", "instagram": "Instagram",
}


def _label(source: str) -> str:
    return SOURCE_LABEL.get(source, source.title())


def _num(value: int) -> str:
    return "{:,}".format(value)


def _plain(text: Optional[str], limit: Optional[int] = None) -> str:
    """One line, no WhatsApp formatting characters that would break *bold* / _italic_."""
    flat = str(text or "").replace("*", "·").replace("_", " ").replace("~", "-").replace("`", "'")
    flat = re.sub(r"\s+", " ", flat).strip()
    if limit and len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    return flat


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


# ───────────────────────── facts → text ─────────────────────────

def when_text(candidate: dict, now: datetime, zone: ZoneInfo) -> str:
    start = datetime.fromisoformat(candidate["start"]).astimezone(zone)
    end = datetime.fromisoformat(candidate["end"]).astimezone(zone) if candidate.get("end") else None
    has_end = end is not None and end > start
    clock = start.strftime("%H:%M")
    after_midnight = start.hour < NIGHT_ROLLOVER_HOUR
    if candidate.get("day") == "today":
        if start <= now:
            return "On now · until %s" % end.strftime("%H:%M") if has_end else "On now"
        text = ("Tonight %s" if (start.hour >= 18 or after_midnight) else "Today %s") % clock
    else:
        if after_midnight:
            # 01:00 on Saturday is, to anyone going out, still Friday night.
            night = datetime.fromisoformat(candidate["start"]).astimezone(zone).toordinal() - 1
            text = "%s night %s" % (datetime.fromordinal(night).strftime("%a"), clock)
        else:
            text = "%s %s" % (start.strftime("%a"), clock)
    if has_end:
        text += " → %s" % end.strftime("%H:%M")
    return text


def where_text(candidate: dict) -> str:
    venue = _plain(candidate.get("venue"), 48)
    area = _plain(candidate.get("area"), 30)
    if venue and area and area.lower() not in venue.lower():
        return "%s, %s" % (venue, area)
    return venue or area or "venue shown on the event page"


def price_text(candidate: dict) -> Optional[str]:
    price = _plain(candidate.get("price"), 40)
    if candidate.get("sold_out"):
        closed = "FULL — waitlist only" if candidate.get("source") == "meetup" else "SOLD OUT"
        return ("%s · %s" % (price, closed)) if price else closed
    if price:
        return price
    if candidate.get("is_free"):
        return "Free"
    return None


def stats_text(candidate: dict) -> str:
    """The popularity line.  Only numbers a source published; says so when there are none."""
    source = candidate["source"]
    others = candidate.get("also_on") or []
    extra = candidate.get("extra_stats") or {}
    parts: List[str] = []

    same_source = [candidate.get("attending")] + [o.get("attending") for o in others if o.get("source") == source]
    same_source = [n for n in same_source if _is_count(n)]
    if same_source:
        text = "%s %s on %s" % (_num(sum(same_source)), candidate.get("attending_label") or "attending", _label(source))
        if len(same_source) > 1:
            text += " across %d listings" % len(same_source)
        elif _is_count(candidate.get("capacity")):
            text += " (limit %s)" % _num(candidate["capacity"])
        parts.append(text)
    for other in others:
        if other.get("source") != source and _is_count(other.get("attending")):
            parts.append("%s %s on %s" % (_num(other["attending"]), other.get("attending_label") or "attending",
                                         _label(other["source"])))
    if _is_count(extra.get("waitlist")):
        parts.append("%s on the waitlist" % _num(extra["waitlist"]))

    tags = " ".join(str(t) for t in candidate.get("tags") or []).lower()
    if "ra pick" in tags:
        parts.append("RA Pick")
    if _is_count(extra.get("xceed_popular_rank")) and _is_count(extra.get("xceed_popular_pool")):
        parts.append("#%d of %d on Xceed by popularity" % (extra["xceed_popular_rank"], extra["xceed_popular_pool"]))

    # Follower counts are per-platform numbers, so always say whose platform.
    followers = candidate.get("organizer_followers")
    if source == "meetup" and _is_count(followers):
        parts.append("group has %s members" % _num(followers))
    elif _is_count(extra.get("venue_followers")):
        parts.append("venue has %s followers on Xceed" % _num(extra["venue_followers"]))
    elif _is_count(followers):
        parts.append("organiser has %s followers on %s" % (_num(followers), _label(source)))

    elsewhere = sorted({_label(o["source"]) for o in others if o.get("source") != source})
    if elsewhere and len(parts) < 3:
        parts.append("also on %s" % ", ".join(elsewhere))
    if not parts:
        return "no public headcount (%s doesn't publish one)" % _label(source)
    return " · ".join(parts[:4])


# ───────────────────────── picks ─────────────────────────

def _index(payload: dict) -> Dict[str, Dict[str, dict]]:
    return {day: {c["id"]: c for c in payload[day]["candidates"]} for day in ("today", "tomorrow")}


def validate_picks(payload: dict, picks: dict) -> List[str]:
    """Every problem with a picks file, as plain sentences.  Empty list = good."""
    problems: List[str] = []
    index = _index(payload)
    if not isinstance(picks, dict):
        return ["picks must be a JSON object with 'today' and 'tomorrow' lists"]
    for day in ("today", "tomorrow"):
        entries = picks.get(day, [])
        if not isinstance(entries, list):
            problems.append("'%s' must be a list" % day)
            continue
        if len(entries) > MAX_PICKS_PER_DAY:
            problems.append("'%s' has %d picks; the maximum is %d" % (day, len(entries), MAX_PICKS_PER_DAY))
        seen = set()
        for position, entry in enumerate(entries, 1):
            where = "%s #%d" % (day, position)
            if not isinstance(entry, dict):
                problems.append("%s must be an object" % where)
                continue
            why = entry.get("why")
            if not isinstance(why, str) or not why.strip():
                problems.append("%s has no 'why' line" % where)
            elif len(why.strip()) > MAX_WHY_CHARS:
                problems.append("%s: 'why' is %d characters; keep it under %d" % (where, len(why.strip()), MAX_WHY_CHARS))
            custom = entry.get("custom")
            if custom is not None:
                if not isinstance(custom, dict):
                    problems.append("%s: 'custom' must be an object" % where)
                    continue
                for key in ("title", "when", "where", "url"):
                    if not str(custom.get(key) or "").strip():
                        problems.append("%s: custom pick is missing '%s'" % (where, key))
                if custom.get("url") and not str(custom["url"]).startswith("http"):
                    problems.append("%s: custom url must start with http" % where)
                term = blocked_term(*(str(v) for v in custom.values()), why if isinstance(why, str) else "")
                if term:
                    problems.append("%s: custom pick trips the content filter (%r) — leave it out" % (where, term))
                continue
            event_id = entry.get("id")
            if not event_id:
                problems.append("%s needs either an 'id' or a 'custom' block" % where)
            elif event_id in seen:
                problems.append("%s repeats id %s" % (where, event_id))
            elif event_id not in index[day]:
                other = "tomorrow" if day == "today" else "today"
                hint = " (that id is in '%s')" % other if event_id in index[other] else ""
                problems.append("%s: id %s is not among the %s candidates%s" % (where, event_id, day, hint))
            seen.add(event_id)
    return problems


def auto_picks(payload: dict, per_day: int = MAX_PICKS_PER_DAY) -> dict:
    """Heuristic picks for runs without a ranking model: best score, with some variety."""
    picks: dict = {}
    for day in ("today", "tomorrow"):
        chosen: List[dict] = []
        per_source: Dict[str, int] = {}
        organisers = set()
        for candidate in sorted(payload[day]["candidates"], key=lambda c: -c.get("score", 0)):
            organiser = (candidate.get("organizer") or "").strip().lower()
            if candidate.get("sold_out"):
                continue  # nothing to act on
            if per_source.get(candidate["source"], 0) >= 3 or (organiser and organiser in organisers):
                continue
            per_source[candidate["source"]] = per_source.get(candidate["source"], 0) + 1
            if organiser:
                organisers.add(organiser)
            reasons = [s for s in candidate.get("signals", []) if not s.startswith("penalty")]
            why = "; ".join(reasons[1:4]) or "one of the busiest listings for this day"
            chosen.append({"id": candidate["id"], "why": why[:1].upper() + why[1:]})
            if len(chosen) == per_day:
                break
        picks[day] = chosen
    return picks


# ───────────────────────── brief (what the ranker reads) ─────────────────────────

# The heuristic's keyword groups (candidates._BOOSTS), surfaced as a rough "type"
# so the ranker can balance a day's list without re-deriving it from each blurb.
_KIND_SIGNALS = ("international crowd", "social", "nightlife / music", "founders / tech", "active / outdoors")

def _status_line(payload: dict) -> str:
    parts = []
    for name, status in payload.get("sources", {}).items():
        if status.get("ok"):
            partial = " — PARTIAL" if status.get("warnings") else ""
            parts.append("%s ok (%d%s)" % (name, status.get("events", status.get("posts", 0)), partial))
        elif status.get("skipped"):
            parts.append("%s skipped (%s)" % (name, status.get("error")))
        else:
            parts.append("%s FAILED (%s)" % (name, status.get("error")))
    return " · ".join(parts)


def brief_text(payload: dict, about_chars: int = 160, caption_chars: int = 400) -> str:
    """The shortlist as compact text for the ranker — the same facts the message will print."""
    zone = ZoneInfo(payload["city"]["tz"])
    now = datetime.fromisoformat(payload["generated_at"]).astimezone(zone)
    out: List[str] = [
        "%s digest candidates — generated %s local time" % (payload["city"]["name"], now.strftime("%a %d %b %H:%M")),
        "Sources: %s" % _status_line(payload),
    ]
    for name, status in payload.get("sources", {}).items():
        for warning in status.get("warnings") or []:
            out.append("Warning from %s: %s" % (name, warning))
    if payload.get("blocked_hosts"):
        out.append("Blocked by the sandbox network policy: %s" % ", ".join(payload["blocked_hosts"]))
    out.append("Removed by the content filter: %d" % payload["totals"]["removed_by_content_filter"])

    for day in ("today", "tomorrow"):
        block = payload[day]
        out.append("")
        out.append("===== %s · %s — %d live events found, best %d listed (heuristic score, high to low) ====="
                   % (day.upper(), block["label"], block["found"], len(block["candidates"])))
        for c in block["candidates"]:
            out.append("[%s] score %d" % (c["id"], round(c.get("score", 0))))
            out.append("  %s" % _plain(c["title"], 110))
            out.append("  when: %s | where: %s" % (when_text(c, now, zone), where_text(c)))
            out.append("  stats: %s" % stats_text(c))
            meta = []
            price = price_text(c)
            if price:
                meta.append("price: %s" % price)
            if c.get("organizer"):
                meta.append("by: %s" % _plain(c["organizer"], 50))
            meta.append("listing language: %s" % c.get("language", "unknown"))
            out.append("  %s" % " | ".join(meta))
            signals = c.get("signals") or []
            kinds = [s for s in signals if s in _KIND_SIGNALS]
            flags = [s[len("penalty: "):] for s in signals if s.startswith("penalty: ")]
            if kinds or flags:
                out.append("  looks like: %s%s" % (", ".join(kinds) or "unclear",
                                                  " | flagged: %s" % ", ".join(flags) if flags else ""))
            if c.get("lineup"):
                out.append("  lineup: %s" % ", ".join(_plain(a, 30) for a in c["lineup"][:6]))
            if c.get("tags"):
                out.append("  tags: %s" % ", ".join(_plain(t, 24) for t in c["tags"][:6]))
            if c.get("description"):
                out.append("  about: %s" % _plain(c["description"], about_chars))

    posts = payload.get("instagram_posts") or []
    if posts:
        out.append("")
        out.append("===== INSTAGRAM — %d recent posts from Barcelona event accounts (unstructured: read for "
                   "events happening today or tomorrow) =====" % len(posts))
        for post in posts:
            likes = post.get("likes")
            likes_text = "%s likes" % _num(likes) if _is_count(likes) else "likes hidden"
            out.append("@%s · posted %s · %s · %s" % (post.get("handle"), str(post.get("posted_at"))[:10],
                                                      likes_text, post.get("url")))
            out.append("  %s" % _plain(post.get("caption"), caption_chars))
    stories = payload.get("instagram_stories") or []
    if stories:
        out.append("")
        out.append("===== INSTAGRAM STORIES — %d live stories (posted in the last 24h; the text is INSIDE the "
                   "image, so open each image file and read it) =====" % len(stories))
        for story in stories:
            out.append("@%s · posted %s%s · %s" % (story.get("handle"), str(story.get("posted_at"))[:16].replace("T", " "),
                                                   " · video (cover frame only)" if story.get("is_video") else "",
                                                   story.get("url")))
            out.append("  image: %s" % (story.get("image_path") or "not downloaded — %s" % story.get("image_url")))
            if story.get("caption"):
                out.append("  text: %s" % _plain(story.get("caption"), caption_chars))
    return "\n".join(out) + "\n"


# ───────────────────────── message ─────────────────────────

def _event_block(position: int, candidate: dict, why: str, now: datetime, zone: ZoneInfo) -> str:
    lines = [
        "*%d. %s*" % (position, _plain(candidate["title"], MAX_TITLE_CHARS)),
        "🕘 %s · 📍 %s" % (when_text(candidate, now, zone), where_text(candidate)),
        "📊 %s" % stats_text(candidate),
    ]
    price = price_text(candidate)
    if price:
        lines.append("💶 %s" % price)
    lines.append("💬 %s" % _plain(why, MAX_WHY_CHARS))
    lines.append(candidate["url"])
    return "\n".join(lines)


def _custom_block(position: int, custom: dict, why: str) -> str:
    lines = [
        "*%d. %s*" % (position, _plain(custom["title"], MAX_TITLE_CHARS)),
        "🕘 %s · 📍 %s" % (_plain(custom["when"], 40), _plain(custom["where"], 60)),
    ]
    if custom.get("stat"):
        lines.append("📊 %s" % _plain(custom["stat"], 120))
    if custom.get("price"):
        lines.append("💶 %s" % _plain(custom["price"], 40))
    lines.append("💬 %s" % _plain(why, MAX_WHY_CHARS))
    lines.append(str(custom["url"]).strip())
    return "\n".join(lines)


def _short_reason(error: Optional[str]) -> str:
    """The gist of a failure, short enough for a message footer."""
    text = re.sub(r"^[a-z_]+:\s*", "", " ".join(str(error or "").split()))   # drop a leading "source: "
    head = text.split(" (")[0]
    text = head if len(head) >= 10 else text
    return _plain(text, 45)


def _coverage_line(payload: dict) -> str:
    """Honest coverage: what was scanned, what was only partly scanned, what was not."""
    counted, missing = [], []
    for name, status in payload.get("sources", {}).items():
        if status.get("skipped"):
            continue    # a source that was never switched on is not news
        partial = " (partial)" if status.get("warnings") else ""
        if name == "instagram" and status.get("ok"):
            counted.append("Instagram %d posts + %d stories%s" % (status.get("posts", 0), status.get("stories", 0), partial))
        elif status.get("ok") and status.get("events"):
            counted.append("%s %d%s" % (_label(name), status["events"], partial))
        else:
            reason = _short_reason(status.get("error")) if not status.get("ok") else "no events returned"
            missing.append("%s (%s)" % (_label(name), reason) if reason else _label(name))
    line = "Scanned: %s." % " · ".join(counted) if counted else "No source returned events."
    if missing:
        line += " Not available today: %s." % ", ".join(missing)
    if payload.get("blocked_hosts"):
        line += " The cloud environment's network settings blocked %d site(s)." % len(payload["blocked_hosts"])
    return line


def render_message(payload: dict, picks: dict) -> str:
    """The digest text.  Sections are separated by blank lines so it can be split safely."""
    problems = validate_picks(payload, picks)
    if problems:
        raise ValueError("picks file is not usable:\n- " + "\n- ".join(problems))

    zone = ZoneInfo(payload["city"]["tz"])
    now = datetime.fromisoformat(payload["generated_at"]).astimezone(zone)
    index = _index(payload)

    sections: List[str] = []
    header = "🎉 *%s · %s*" % (payload["city"]["name"], payload["today"]["label"])
    note = _plain(picks.get("note"), 200)
    sections.append(header + ("\n_%s_" % note if note else ""))

    titles = {"today": "TODAY · %s" % payload["today"]["label"], "tomorrow": "TOMORROW · %s" % payload["tomorrow"]["label"]}
    for day in ("today", "tomorrow"):
        entries = picks.get(day) or []
        sections.append("━━ *%s* ━━" % titles[day])
        if not entries:
            sections.append("Nothing worth your evening turned up for this day.")
            continue
        for position, entry in enumerate(entries, 1):
            if entry.get("custom"):
                sections.append(_custom_block(position, entry["custom"], entry["why"]))
            else:
                sections.append(_event_block(position, index[day][entry["id"]], entry["why"], now, zone))

    sections.append("_%s_" % _coverage_line(payload))
    return "\n\n".join(sections)


# ───────────────────────── plain text (iMessage, SMS) ─────────────────────────

_BOLD_LINE = re.compile(r"^(🎉 )?\*(.+)\*$")
_ITALIC_LINE = re.compile(r"^_(.+)_$")


def _strip_markup(line: str) -> str:
    """Remove WhatsApp's *bold* / _italic_ markers, which other apps print literally.

    Only markers that wrap a whole line are touched — that is the only place
    ``render_message`` puts them — so an underscore inside a link survives.
    """
    text = line.strip()
    if text.startswith("━━"):
        return text.strip("━ ").strip("*").strip()
    for pattern in (_BOLD_LINE, _ITALIC_LINE):
        match = pattern.match(text)
        if match:
            return "".join(group or "" for group in match.groups())
    return line


def plain_messages(message: str) -> List[str]:
    """The digest as plain text, in two parts: today (with the header) and tomorrow (with the footer)."""
    parts: List[str] = []
    current: List[str] = []
    for block in message.strip().split("\n\n"):
        if block.startswith("━━") and "TOMORROW" in block and current:
            parts.append("\n\n".join(current))
            current = []
        current.append("\n".join(_strip_markup(line) for line in block.split("\n")))
    if current:
        parts.append("\n\n".join(current))
    return parts
