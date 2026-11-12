"""Offline tests for the digest core: window, guardrail, duplicates, bucketing, scoring,
payload assembly, picks validation and message rendering.  No network."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backend.digest import candidates as cand
from backend.digest import engine, render
from backend.digest.cities import collection_window, get_city
from backend.digest.guardrails import blocked_term, filter_events
from backend.digest.models import DigestEvent, validate_event

MADRID = ZoneInfo("Europe/Madrid")
CITY = get_city("barcelona")
NOW = datetime(2026, 11, 12, 15, 0, tzinfo=MADRID)  # a Thursday, 3 pm


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=MADRID)


def make(source="meetup", title="Thursday Language Exchange", start=None, **kw) -> DigestEvent:
    kw.setdefault("url", "https://example.com/%s/%s" % (source, abs(hash((title, str(start), kw.get("organizer")))) % 10**8))
    return DigestEvent(source=source, title=title, start=start or at(12, 19), **kw)


# ───────────── model + window ─────────────

def test_validate_rejects_naive_start_and_stat_without_label():
    with pytest.raises(ValueError, match="timezone-aware"):
        validate_event(make(start=datetime(2026, 11, 12, 19, 0)))
    with pytest.raises(ValueError, match="attending_label"):
        validate_event(make(attending=10))
    validate_event(make(attending=10, attending_label="going"))


def test_event_id_is_stable_and_round_trips():
    ev = make(source_id="317105736", attending=44, attending_label="going", end=at(12, 22))
    again = DigestEvent.from_dict(ev.to_dict())
    assert again.event_id == ev.event_id == "meetup-" + ev.event_id.split("-")[1]
    assert again.start == ev.start and again.end == ev.end and again.attending == 44


def test_collection_window_runs_midnight_today_to_6am_two_days_on():
    start, end = collection_window(CITY, NOW)
    assert start == at(12, 0) and end == at(14, 6)


def test_unknown_city_is_an_error():
    with pytest.raises(ValueError, match="unknown city"):
        get_city("atlantis")


# ───────────── content guardrail ─────────────

@pytest.mark.parametrize("text", [
    "Kinky Films 1st Edition / Electro Party / Sex Positive",
    "Fetish Friday at the dungeon",
    "Fiesta liberal para parejas",
    "Noche Erótica en el Raval",
    "Swingers meet & greet",
    "BDSM 101 workshop",
    "Shibari rope jam",
    "Orgía de sabores? No: una orgía.",
])
def test_blocked_terms_are_caught(text):
    assert blocked_term(text) is not None


@pytest.mark.parametrize("text", [
    "Swing dance social — lindy hop for beginners",
    "Bachata sensual bootcamp",
    "Deep Tech House all night",
    "2-for-1 Pornstar Martinis at the rooftop",
    "Darkroom photography workshop",
    "Liberal arts book club",
    "Organic wine tasting",
    "Burlesque & cabaret night",
])
def test_innocent_wording_is_not_blocked(text):
    assert blocked_term(text) is None


def test_filter_events_checks_description_tags_and_records_why():
    kept, removed = filter_events([
        make(title="Rooftop afterwork"),
        make(title="Late night special", description="A sex-positive play party for open minds"),
        make(title="Club night", tags=["techno", "Fetish"]),
    ])
    assert [ev.title for ev in kept] == ["Rooftop afterwork"]
    assert {r["term"] for r in removed} == {"sex-positive", "fetish"}


# ───────────── duplicates ─────────────

def test_cross_posted_meetup_and_ticket_listing_merge_and_headcount_sums():
    events = [
        make(title="The Thursday Language Exchange", venue="Space Cowboy", organizer="Barcelona Language Exchange",
             attending=44, attending_label="going"),
        make(title="The Thursday Language Exchange", venue="Space Cowboy", organizer="BCN Languages and Friends",
             attending=15, attending_label="going"),
        make(source="eventbrite", title="The Thursday Language Exchange", venue="Space Cowboy Barcelona", price="Free"),
    ]
    merged = cand.merge_duplicates(events)
    assert len(merged) == 1
    one = merged[0]
    assert one.event.attending == 44 and one.same_source_attending() == 59
    assert sorted(o["source"] for o in one.also_on) == ["eventbrite", "meetup"]
    assert one.event.price == "Free"  # descriptive gap filled from the other listing


def test_decorated_ticket_titles_still_merge():
    events = [
        make(source="ra", title="HOPE at Noxe (26th floor W Barcelona)", venue="Noxe Barcelona", start=at(12, 22),
             attending=80, attending_label="interested"),
        make(source="dice", title="FREE GUEST LIST HOPE 26th FLOOR W BARCELONA", venue="Noxe", start=at(12, 22)),
    ]
    assert len(cand.merge_duplicates(events)) == 1


def test_same_venue_same_time_different_events_stay_apart():
    # Real false merge from the first live run: both titles end in "Barcelona".
    events = [
        make(source="eventbrite", title="NETWORKING - ÉXITO COMPARTIDO BARCELONA", venue="Cervecería Ogham | Barcelona",
             organizer="Barcelona international Social"),
        make(source="eventbrite", title="ROCK BINGO - CRAFT BARCELONA", venue="Craft Barcelona", organizer="CRAFT BAR"),
    ]
    assert len(cand.merge_duplicates(events)) == 2


def test_one_organiser_listing_twice_on_one_site_is_two_events():
    events = [
        make(title="Mixed English speaking Football - Improver", venue="Barcelona", organizer="Keep Calm Barcelona",
             attending=17, attending_label="going"),
        make(title="Mixed English speaking Football - Intermediate", venue="Barcelona", organizer="Keep Calm Barcelona",
             attending=16, attending_label="going"),
    ]
    merged = cand.merge_duplicates(events)
    assert len(merged) == 2 and all(c.same_source_attending() < 20 for c in merged)


def test_numbers_and_neighbourhoods_are_never_borrowed_from_another_listing():
    events = [
        make(source="ra", title="Throwback", venue="La Terrrazza", organizer="Throwback Barcelona",
             attending=580, attending_label="interested"),
        make(source="xceed", title="Throwback", venue="La Terrrazza", organizer_followers=392, capacity=900,
             area="Barri Gotic", address="Av. Francesc Ferrer i Guàrdia"),
    ]
    one = cand.merge_duplicates(events)[0]
    assert one.event.source == "ra"
    assert one.event.organizer_followers is None and one.event.capacity is None
    assert one.event.area is None                       # another site's geocoding stays with that site
    assert one.event.address == "Av. Francesc Ferrer i Guàrdia"   # a plain descriptive gap is still filled


# ───────────── today / tomorrow ─────────────

def test_night_rollover_puts_after_midnight_sets_with_the_evening_before():
    assert cand.digest_day(at(13, 1, 30), CITY) == at(12, 0).date()
    assert cand.digest_day(at(13, 6, 0), CITY) == at(13, 0).date()


def test_bucket_drops_finished_online_and_other_days():
    events = {
        "tonight": make(title="Tonight club", start=at(12, 23, 59)),
        "after midnight": make(title="Late set", start=at(13, 1)),            # still "today"
        "tomorrow": make(title="Friday rooftop", start=at(13, 20)),
        "tomorrow night": make(title="Friday late", start=at(14, 2)),         # still "tomorrow"
        "over": make(title="Morning yoga", start=at(12, 9), end=at(12, 10)),
        "no end, long ago": make(title="Lunch meetup", start=at(12, 13)),
        "all day": make(title="Design market", start=at(12, 11), end=at(12, 21)),
        "ending soon": make(title="Vermouth hour", start=at(12, 13), end=at(12, 16)),
        "online": make(title="Webinar", start=at(12, 19), is_online=True),
        "last night": make(title="Wednesday afters", start=at(12, 3)),
    }
    today, tomorrow = cand.bucket([cand.Candidate(ev) for ev in events.values()], CITY, NOW)
    assert sorted(c.event.title for c in today) == ["Design market", "Late set", "Tonight club"]
    assert sorted(c.event.title for c in tomorrow) == ["Friday late", "Friday rooftop"]
    in_progress = {c.event.title: c.in_progress for c in today}
    assert in_progress == {"Design market": True, "Late set": False, "Tonight club": False}


# ───────────── scoring ─────────────

def _scored(**kw) -> cand.Candidate:
    candidate = cand.Candidate(make(**kw))
    cand.score(candidate)
    return candidate


def test_bigger_real_headcount_scores_higher():
    small = _scored(title="Board games", attending=8, attending_label="going")
    big = _scored(title="Board games", attending=90, attending_label="going")
    assert big.score > small.score + 15


def test_a_500_person_club_night_outranks_a_tiny_keyword_rich_meetup():
    club = _scored(source="ra", title="POLENTA", description="Sala principal", attending=520, attending_label="interested")
    tiny = _scored(title="International expat social mixer language exchange", attending=6, attending_label="going",
                   description="Join us and meet new people from all over the world for drinks and networking")
    assert club.score > tiny.score


def test_sold_out_costs_points_and_says_so():
    open_ = _scored(title="Spanish conversation", attending=17, attending_label="going", capacity=30)
    full = _scored(title="Spanish conversation", attending=17, attending_label="going", capacity=17, sold_out=True)
    assert full.score < open_.score
    assert "sold out / full" in full.signals


def test_tech_house_is_not_a_founder_event_but_a_startup_meetup_is():
    rave = _scored(source="ra", title="Deep, Tech, House: Hermes", description="Tech house and AI-free vibes", tags=["Tech House"])
    founders = _scored(title="Startup founders drinks", description="Meet founders and investors")
    assert "founders / tech" not in rave.signals
    assert "founders / tech" in founders.signals


def test_sales_seminars_and_kids_events_are_penalised():
    pitch = _scored(title="Investment - Real estate in Barcelona", attending=41, attending_label="going")
    kids = _scored(title="Taller infantil de pintura", attending=41, attending_label="going")
    normal = _scored(title="Friday drinks", attending=41, attending_label="going")
    assert pitch.score < normal.score - 15 and kids.score < normal.score - 15
    assert any(s.startswith("penalty") for s in pitch.signals)


def test_language_detection():
    assert cand.detect_language("Join us for free drinks and meet new people") == "en"
    assert cand.detect_language("Ven a la fiesta con todos los amigos, entrada gratis") == "local"
    assert cand.detect_language("POLENTA") == "unknown"


def test_shortlist_keeps_each_sources_best_even_when_outscored():
    pool = [_scored(title="Meetup %d" % i, attending=100 - i, attending_label="going",
                    start=at(12, 19, i)) for i in range(30)]
    pool.append(_scored(source="dice", title="Small gig", start=at(12, 21)))
    chosen = cand.shortlist(pool, limit=10)
    assert len(chosen) == 11 and any(c.event.source == "dice" for c in chosen)
    assert [c.score for c in chosen] == sorted((c.score for c in chosen), reverse=True)


# ───────────── payload + picks + message ─────────────

def _payload() -> dict:
    events = [
        make(title="The Thursday Language Exchange", venue="Space Cowboy", area="Ciutat Vella", start=at(12, 19),
             end=at(12, 22), organizer="Barcelona Language Exchange", attending=44, attending_label="going",
             organizer_followers=62984, price="Free", is_free=True, source_id="1"),
        make(title="The Thursday Language Exchange", venue="Space Cowboy", start=at(12, 19),
             organizer="BCN Languages and Friends", attending=15, attending_label="going", source_id="2"),
        make(source="ra", title="POLENTA", venue="La Paloma", start=at(13, 23, 59), end=at(14, 6),
             attending=520, attending_label="interested", price="€20", tags=["House", "RA Pick"], source_id="3"),
        make(source="meetup", title="Speak Spanish: café y juegos", venue="Vivari", start=at(13, 18, 30),
             attending=17, attending_label="going", capacity=17, sold_out=True, price="Free", source_id="4"),
        make(source="dice", title="Late *set* _b2b_", venue="Slow Club", start=at(13, 1), price="€12", source_id="5"),
        make(source="xceed", title="Viernes Íntimos", description="Club swinger", start=at(13, 23), source_id="6"),
        make(title="Morning run", start=at(12, 8), end=at(12, 9), source_id="7"),
    ]
    statuses = {
        "meetup": {"ok": True, "events": 4, "seconds": 1.0, "error": None},
        "ra": {"ok": True, "events": 1, "seconds": 1.0, "error": None},
        "dice": {"ok": True, "events": 1, "seconds": 1.0, "error": None},
        "xceed": {"ok": False, "events": 0, "seconds": 1.0, "error": "HTTP 503"},
        "instagram": {"ok": False, "posts": 0, "seconds": 0.0, "skipped": True, "error": "not configured"},
    }
    return engine.build_payload(CITY, NOW, events, statuses)


def _id(payload: dict, day: str, title_start: str) -> str:
    return next(c["id"] for c in payload[day]["candidates"] if c["title"].startswith(title_start))


def test_build_payload_filters_merges_buckets():
    payload = _payload()
    assert payload["totals"] == {"collected": 7, "after_dedupe": 5, "removed_by_content_filter": 1}
    assert payload["content_filter_removed"][0]["term"] == "swinger"
    assert [c["title"] for c in payload["today"]["candidates"]] == ["The Thursday Language Exchange", "Late *set* _b2b_"]
    assert {c["title"] for c in payload["tomorrow"]["candidates"]} == {"POLENTA", "Speak Spanish: café y juegos"}
    assert payload["today"]["label"] == "Thu 12 Nov" and payload["tomorrow"]["label"] == "Fri 13 Nov"


def test_stats_line_only_states_published_numbers():
    payload = _payload()
    exchange = payload["today"]["candidates"][0]
    assert render.stats_text(exchange) == "59 going on Meetup across 2 listings · group has 62,984 members"
    polenta = next(c for c in payload["tomorrow"]["candidates"] if c["title"] == "POLENTA")
    assert render.stats_text(polenta) == "520 interested on RA · RA Pick"
    late = next(c for c in payload["today"]["candidates"] if c["source"] == "dice")
    assert render.stats_text(late) == "no public headcount (DICE doesn't publish one)"


def test_when_text_reads_like_a_person_would_say_it():
    payload = _payload()
    by_title = {c["title"]: c for day in ("today", "tomorrow") for c in payload[day]["candidates"]}
    assert render.when_text(by_title["The Thursday Language Exchange"], NOW, MADRID) == "Tonight 19:00 → 22:00"
    assert render.when_text(by_title["Late *set* _b2b_"], NOW, MADRID) == "Tonight 01:00"
    assert render.when_text(by_title["POLENTA"], NOW, MADRID) == "Fri 23:59 → 06:00"
    after_midnight = dict(by_title["POLENTA"], start=at(14, 1).isoformat(), end=None)
    assert render.when_text(after_midnight, NOW, MADRID) == "Fri night 01:00"
    running = dict(by_title["The Thursday Language Exchange"], start=at(12, 11).isoformat(), end=at(12, 21).isoformat())
    assert render.when_text(running, NOW, MADRID) == "On now · until 21:00"


def test_validate_picks_explains_every_problem():
    payload = _payload()
    polenta = _id(payload, "tomorrow", "POLENTA")
    problems = render.validate_picks(payload, {
        "today": [
            {"id": polenta, "why": "wrong day"},
            {"id": "meetup-deadbeef", "why": "does not exist"},
            {"id": _id(payload, "today", "The Thursday"), "why": ""},
            {"custom": {"title": "Fetish ball", "when": "Tonight", "where": "x", "url": "https://x.y"}, "why": "no"},
            {"custom": {"title": "Concert"}, "why": "missing fields"},
        ],
        "tomorrow": [{"id": polenta, "why": "w" * 200}] + [{"id": polenta, "why": "dup"}] * 5,
    })
    text = "\n".join(problems)
    assert "that id is in 'tomorrow'" in text
    assert "meetup-deadbeef is not among the today candidates" in text
    assert "today #3 has no 'why' line" in text
    assert "trips the content filter" in text
    assert "missing 'when'" in text and "missing 'url'" in text
    assert "'tomorrow' has 6 picks" in text and "keep it under" in text and "repeats id" in text
    with pytest.raises(ValueError, match="not usable"):
        render.render_message(payload, {"today": [{"id": "nope", "why": "x"}], "tomorrow": []})


def test_render_message_prints_facts_from_data_and_survives_formatting_characters():
    payload = _payload()
    picks = {
        "note": "Quiet Thursday, big Friday.",
        "today": [
            {"id": _id(payload, "today", "The Thursday"), "why": "Biggest English-friendly social tonight."},
            {"id": _id(payload, "today", "Late"), "why": "Small room, good sound."},
        ],
        "tomorrow": [
            {"id": _id(payload, "tomorrow", "POLENTA"), "why": "The busiest club night on RA."},
            {"id": _id(payload, "tomorrow", "Speak Spanish"), "why": "Full, but worth the waitlist."},
            {"custom": {"title": "Rosalía", "when": "Fri 21:00", "where": "Palau Sant Jordi", "url": "https://example.com/r",
                        "price": "From €60", "stat": "17,000 capacity"}, "why": "The big show in town."},
        ],
    }
    message = render.render_message(payload, picks)
    assert message.startswith("🎉 *Barcelona · Thu 12 Nov*\n_Quiet Thursday, big Friday._")
    assert "━━ *TODAY · Thu 12 Nov* ━━" in message and "━━ *TOMORROW · Fri 13 Nov* ━━" in message
    assert "*1. The Thursday Language Exchange*\n🕘 Tonight 19:00 → 22:00 · 📍 Space Cowboy, Ciutat Vella\n" \
           "📊 59 going on Meetup across 2 listings · group has 62,984 members\n💶 Free\n" \
           "💬 Biggest English-friendly social tonight.\nhttps://example.com/meetup/" in message
    assert "*2. Late ·set· b2b*" in message          # stray * and _ cannot break the bold title
    assert "💶 Free · FULL — waitlist only" in message
    assert "*3. Rosalía*\n🕘 Fri 21:00 · 📍 Palau Sant Jordi\n📊 17,000 capacity\n💶 From €60" in message
    assert message.rstrip().endswith("_Scanned: Meetup 4 · RA 1 · DICE 1. Not available today: Xceed (HTTP 503)._")


def test_footer_is_honest_about_partial_blocked_and_failed_sources():
    payload = _payload()
    payload["sources"]["meetup"]["warnings"] = ["meetup: Fri 13 Nov could not be fetched (ReadTimeout) — that day is missing"]
    payload["sources"]["instagram"] = {"ok": False, "posts": 0, "seconds": 0.5,
                                       "error": "Apify account blocked (platform-feature-disabled Too many outstanding invoices)"}
    payload["sources"]["luma"] = {"ok": True, "events": 0, "seconds": 0.2, "error": None,
                                  "warnings": ["returned 0 events — possibly a parser break"]}
    payload["blocked_hosts"] = ["dice.fm", "ra.co"]
    footer = render.render_message(payload, {"today": [], "tomorrow": []}).split("\n\n")[-1]
    assert footer == ("_Scanned: Meetup 4 (partial) · RA 1 · DICE 1. Not available today: Xceed (HTTP 503), "
                      "Instagram (Apify account blocked), Luma (no events returned). "
                      "The cloud environment's network settings blocked 2 site(s)._")
    brief = render.brief_text(payload)
    assert "meetup ok (4 — PARTIAL)" in brief
    assert "Warning from meetup: meetup: Fri 13 Nov could not be fetched" in brief


def test_a_source_that_was_never_switched_on_is_not_reported_as_missing():
    footer = render.render_message(_payload(), {"today": [], "tomorrow": []}).split("\n\n")[-1]
    assert "Instagram" not in footer


def test_rsvp_only_price_gives_way_to_a_real_ticket_price():
    events = [
        make(title="Singles Night at Gaby's", venue="Gaby's Club", attending=36, attending_label="going",
             price="Free to RSVP", is_free=None),
        make(source="eventbrite", title="Singles Night at Gaby's", venue="Gaby's Club", price="€12", is_free=False),
    ]
    one = cand.merge_duplicates(events)[0]
    assert one.event.source == "meetup" and one.event.price == "€12" and one.event.is_free is False
    alone = cand.merge_duplicates(events[:1])[0]
    assert alone.event.price == "Free to RSVP"


def test_placeholder_listings_sink():
    real = _scored(title="English speaking Ping Pong", attending=19, attending_label="going",
                   description="Join us for a friendly game, all levels welcome")
    stub = _scored(title="English speaking Ping Pong", attending=19, attending_label="going",
                   description="PLEASE NOTE: THIS IS A PLACEHOLDER EVENT ONLY. Sessions are organised in our chat group.")
    assert stub.score <= real.score - 30 and "penalty: placeholder listing" in stub.signals


def test_empty_day_says_so_instead_of_padding():
    payload = _payload()
    message = render.render_message(payload, {"today": [], "tomorrow": []})
    assert message.count("Nothing worth your evening turned up for this day.") == 2


def test_auto_picks_are_valid_varied_and_skip_full_events():
    payload = _payload()
    picks = render.auto_picks(payload)
    assert render.validate_picks(payload, picks) == []
    chosen_tomorrow = {p["id"] for p in picks["tomorrow"]}
    assert _id(payload, "tomorrow", "Speak Spanish") not in chosen_tomorrow
    assert _id(payload, "tomorrow", "POLENTA") in chosen_tomorrow
    assert all(p["why"] for day in ("today", "tomorrow") for p in picks[day])


def test_brief_lists_every_shortlisted_candidate_with_its_id_and_source_status():
    payload = _payload()
    brief = render.brief_text(payload)
    for day in ("today", "tomorrow"):
        for c in payload[day]["candidates"]:
            assert "[%s]" % c["id"] in brief
    assert "xceed FAILED (HTTP 503)" in brief and "instagram skipped (not configured)" in brief
    assert "===== TODAY · Thu 12 Nov — 2 live events found" in brief
    assert "stats: 520 interested on RA · RA Pick" in brief
