"""Offline tests for the Meetup digest collector (parse a saved real GraphQL payload)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from backend.digest.cities import get_city
from backend.digest.collectors import CollectorError
from backend.digest.collectors import meetup
from backend.digest.models import validate_event

FIXTURE = Path(__file__).parent / "fixtures" / "digest_meetup.json"
MADRID = ZoneInfo("Europe/Madrid")
CITY = get_city("barcelona")
START = datetime(2026, 11, 12, 0, 0, tzinfo=MADRID)
END = datetime(2026, 11, 14, 6, 0, tzinfo=MADRID)


@pytest.fixture(scope="module")
def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def events(payload):
    return meetup.parse_events(payload, CITY, START, END)


def by_id(evs, source_id):
    return next(e for e in evs if e.source_id == source_id)


def test_fixture_is_substantial(payload):
    nodes = list(meetup._iter_nodes(payload))
    assert len(nodes) >= 6
    assert any(n["isOnline"] for n in nodes)
    assert sum(1 for n in nodes if (n["rsvps"] or {}).get("yesCount")) >= 6


def test_every_event_passes_contract(events):
    assert len(events) >= 30
    for ev in events:
        validate_event(ev)
        assert ev.source == "meetup"


def test_starts_are_tz_aware_madrid(events):
    for ev in events:
        assert ev.start.tzinfo is not None
        assert ev.start.utcoffset() == ev.start.astimezone(MADRID).utcoffset()
        assert str(ev.start.tzinfo) == "Europe/Madrid"
        if ev.end is not None:
            assert str(ev.end.tzinfo) == "Europe/Madrid"


def test_window_filter_drops_out_of_window(payload, events):
    ids = {e.source_id for e in events}
    # real events in the payload dated 27 Nov and 21 Nov
    assert "316992295" not in ids and "317313083" not in ids
    assert all(START <= e.start < END for e in events)
    # a narrower window keeps only what starts inside it
    narrow = meetup.parse_events(payload, CITY, datetime(2026, 11, 13, 19, 0, tzinfo=MADRID),
                                 datetime(2026, 11, 13, 20, 0, tzinfo=MADRID))
    assert narrow and all(datetime(2026, 11, 13, 19, 0, tzinfo=MADRID) <= e.start < datetime(2026, 11, 13, 20, 0, tzinfo=MADRID) for e in narrow)
    assert len(narrow) < len(events)
    # window start is inclusive, end exclusive
    exact = meetup.parse_events(payload, CITY, datetime(2026, 11, 13, 20, 0, tzinfo=MADRID),
                                datetime(2026, 11, 13, 20, 15, tzinfo=MADRID))
    assert exact and all(e.start == datetime(2026, 11, 13, 20, 0, tzinfo=MADRID) for e in exact)
    assert meetup.parse_events(payload, CITY, datetime(2026, 11, 27, 0, 0, tzinfo=MADRID),
                               datetime(2026, 11, 28, 0, 0, tzinfo=MADRID))  # the 27 Nov event is found when asked for


def test_named_event_stats(events):
    ev = by_id(events, "316811206")
    assert ev.title.startswith("FRIDAY DJ SESSION PARTY on a ROOFTOP HOTEL")
    assert ev.attending == 106 and ev.attending_label == "going"
    assert ev.organizer == "Language Exchange & Party / Barcelona International Social"
    assert ev.organizer_followers == 49730
    assert ev.capacity == 250
    assert ev.extra_stats == {"waitlist": 0}
    assert ev.venue == "Labtwentytwo Barcelona"
    assert ev.address == "Carrer del Perú, 102, Barcelona"
    assert ev.start == datetime(2026, 11, 13, 20, 0, tzinfo=MADRID)
    assert ev.end == datetime(2026, 11, 14, 6, 0, tzinfo=MADRID)
    assert ev.url == "https://www.meetup.com/language-exchange-barcelona-meetup/events/316811206/"
    assert ev.is_online is False
    assert ev.image_url.startswith("https://secure.meetupstatic.com/photos/")


def test_waitlist_and_sold_out(events):
    ev = by_id(events, "317282756")        # Dark Matter: 30 going, limit 26, waitlist 26
    assert ev.attending == 30 and ev.capacity == 26
    assert ev.extra_stats["waitlist"] == 26
    assert ev.organizer_followers == 4638
    assert ev.sold_out is True
    open_ev = by_id(events, "316811206")   # 106 of 250
    assert open_ev.sold_out is None


def test_zero_limit_means_no_capacity(events):
    ev = by_id(events, "316811205")        # maxTickets == 0
    assert ev.capacity is None
    assert ev.attending == 58
    assert ev.organizer_followers == 49730


def test_fee_and_free(events):
    paid = by_id(events, "317046238")      # Café y Tarot: 5 EUR
    assert paid.price == "€5" and paid.is_free is False
    free = by_id(events, "317283523")      # no fee settings
    # no fee settings: Meetup shows "free" but organisers may charge elsewhere, so we do not claim Free
    assert free.price == "Free to RSVP" and free.is_free is None


def test_topics_become_tags_and_missing_venue_is_none(events):
    ev = by_id(events, "317023970")
    assert "Board Games" in ev.tags and ev.organizer.startswith("KLEFF")
    no_venue = by_id(events, "316747516")  # badminton, no venue published
    assert no_venue.venue is None and no_venue.address is None
    assert no_venue.image_url is None


def test_online_events_skipped_by_default(payload, events):
    ids = {e.source_id for e in events}
    assert not ids & {"317218334", "317145920", "317328202"}
    assert all(not e.is_online for e in events)


def test_online_events_flagged_when_requested(payload):
    evs = meetup.parse_events(payload, CITY, START, END, include_online=True)
    online = [e for e in evs if e.is_online]
    assert {e.source_id for e in online} >= {"317218334", "317145920", "317328202"}
    for e in online:
        validate_event(e)
    assert len(evs) == len(online) + len([e for e in evs if not e.is_online])


def test_dedup_by_event_id(payload):
    doubled = [payload, payload]
    assert len(meetup.parse_events(doubled, CITY, START, END)) == len(meetup.parse_events(payload, CITY, START, END))


def test_cancelled_events_dropped(payload):
    data = json.loads(json.dumps(payload))
    data["data"]["recommendedEvents"]["edges"][0]["node"]["status"] = "CANCELLED"
    cancelled_id = data["data"]["recommendedEvents"]["edges"][0]["node"]["id"]
    assert cancelled_id not in {e.source_id for e in meetup.parse_events(data, CITY, START, END)}


def test_description_is_plain_text(events):
    for ev in events:
        if ev.description:
            assert "**" not in ev.description and "](" not in ev.description


def test_day_slices_cut_at_local_midnight():
    slices = meetup.day_slices(START, END)
    assert [(s.day, s.hour, e.day, e.hour) for s, e in slices] == [(12, 0, 13, 0), (13, 0, 14, 0), (14, 0, 14, 6)]


def test_unknown_city_raises():
    class FakeCity:
        key = "atlantis"
    with pytest.raises(CollectorError):
        asyncio.run(meetup.collect(FakeCity(), START, END, client=None))


# ───────────────────────── synthetic GraphQL + mocked transport ─────────────────────────

def make_node(node_id, start, **over):
    node = {
        "id": str(node_id), "title": "Event %s" % node_id, "dateTime": start.isoformat(),
        "endTime": None, "eventType": "PHYSICAL", "isOnline": False, "status": "ACTIVE",
        "eventUrl": "https://www.meetup.com/g/events/%s/" % node_id, "maxTickets": 0,
        "description": "hello", "rsvps": {"totalCount": 9, "yesCount": 7, "waitlistCount": 0},
        "feeSettings": None,
        "group": {"id": "1", "name": "A Group", "memberships": {"totalCount": 100}},
        "venue": {"name": "Venue", "address": "Carrer 1", "city": "Barcelona"},
        "topics": {"edges": [{"node": {"name": "Music"}}]},
        "featuredEventPhoto": None,
    }
    node.update(over)
    return node


def gql(nodes, has_next=False, cursor=None):
    return {"data": {"recommendedEvents": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "edges": [{"node": n} for n in nodes],
    }}}


def _bounds(request):
    body = json.loads(request.content)
    f = body["variables"]["filter"]
    parse = lambda z: datetime.fromisoformat(z.split("[")[0])  # noqa: E731
    return parse(f["startDateRange"]), parse(f["endDateRange"])


def big_slice(s, e, n=45):
    """n events (>= SPLIT_THRESHOLD) inside [s, e), ids unique per slice."""
    tag = s.strftime("%m%d%H%M") + e.strftime("%H%M")
    return [make_node("%s-%d" % (tag, i), s + timedelta(minutes=i)) for i in range(n)]


def run_collect(handler, start=START, end=datetime(2026, 11, 14, 0, 0, tzinfo=MADRID)):
    leftover = []

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            try:
                return await meetup.collect(CITY, start, end, client)
            finally:
                await asyncio.sleep(0)
                leftover.extend(t for t in asyncio.all_tasks() if t is not asyncio.current_task())

    result = asyncio.run(_run())
    assert not leftover, "collect() left tasks running: %r" % leftover
    return result


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch):
    monkeypatch.setattr(meetup, "RETRY_PAUSE", 0)


def meetup_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and r.name == meetup.__name__]


def test_budget_exhausted_keeps_every_day_and_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(meetup, "MAX_REQUESTS", 10)

    def handler(request):
        s, e = _bounds(request)
        return httpx.Response(200, json=gql(big_slice(s, e)))

    with caplog.at_level(logging.WARNING, logger=meetup.__name__):
        events = run_collect(handler)
    days = {ev.start.day for ev in events}
    assert days == {12, 13}, days
    budget_warnings = [m for m in meetup_warnings(caplog) if "budget" in m]
    assert len(budget_warnings) == 1 and "incomplete" in budget_warnings[0]


def test_failing_subslice_keeps_parent_pages(caplog):
    def handler(request):
        s, e = _bounds(request)
        if e - s >= timedelta(hours=24):
            return httpx.Response(200, json=gql(big_slice(s, e)))
        if s.hour < 12:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=gql(big_slice(s, e, n=5)))

    with caplog.at_level(logging.WARNING, logger=meetup.__name__):
        events = run_collect(handler)
    per_day = {d: sum(1 for ev in events if ev.start.day == d) for d in (12, 13)}
    assert all(n >= 45 for n in per_day.values()), per_day      # the parent's 45 survive
    assert any("could not split" in m and "CollectorError" in m for m in meetup_warnings(caplog))


def test_day_failing_twice_warns_with_day_and_keeps_other_day(caplog):
    calls = {"d12": 0}

    def handler(request):
        s, e = _bounds(request)
        if s.day == 12:
            calls["d12"] += 1
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=gql(big_slice(s, e, n=5)))

    with caplog.at_level(logging.WARNING, logger=meetup.__name__):
        events = run_collect(handler)
    assert calls["d12"] == 2                                    # one retry
    assert {ev.start.day for ev in events} == {13} and len(events) == 5
    label = meetup._slice_label(START)
    assert label == "Thu 12 Nov"
    assert any(label in m and "missing" in m and "CollectorError" in m for m in meetup_warnings(caplog))


def test_day_recovers_on_retry():
    seen = {"n": 0}

    def handler(request):
        s, e = _bounds(request)
        if s.day == 12:
            seen["n"] += 1
            if seen["n"] == 1:
                return httpx.Response(503, text="try later")
        return httpx.Response(200, json=gql(big_slice(s, e, n=3)))

    events = run_collect(handler)
    assert {ev.start.day for ev in events} == {12, 13}


def test_every_request_failing_raises():
    def handler(request):
        return httpx.Response(500, text="down")

    with pytest.raises(CollectorError):
        run_collect(handler)


def test_timeouts_are_named_in_the_warning(caplog):
    def handler(request):
        s, e = _bounds(request)
        if s.day == 12:
            raise httpx.ReadTimeout("")
        return httpx.Response(200, json=gql(big_slice(s, e, n=2)))

    with caplog.at_level(logging.WARNING, logger=meetup.__name__):
        events = run_collect(handler)
    assert len(events) == 2
    assert any("ReadTimeout" in m for m in meetup_warnings(caplog))


def test_genuinely_empty_source_returns_empty_list():
    assert run_collect(lambda request: httpx.Response(200, json=gql([]))) == []


def test_malformed_nodes_are_tolerated(monkeypatch, caplog):
    t = datetime(2026, 11, 13, 20, 0, tzinfo=MADRID)
    nodes = [
        make_node(1, t),
        make_node(2, t, venue="oops", group=None, rsvps=[], feeSettings="x",
                  topics="t", featuredEventPhoto=[], description=5),
        "not-a-dict",
        make_node(3, t, title=123),
        make_node(4, t, venue=["a"], group="g", rsvps="r", topics={"edges": "zz"}),
        make_node(5, t),
    ]
    payload = gql(nodes)
    evs = meetup.parse_events(payload, CITY, START, END)
    ids = {e.source_id for e in evs}
    assert {"1", "2", "4", "5"} <= ids and "3" not in ids
    bad = by_id(evs, "2")
    assert bad.venue is None and bad.organizer is None and bad.attending is None
    assert bad.attending_label is None and bad.tags == [] and bad.description is None
    assert bad.price == "Free to RSVP" and bad.is_free is None
    for ev in evs:
        validate_event(ev)

    # even an unexpected exception inside one node's parse only skips that node
    real = meetup._parse_node

    def flaky(node, city, include_online):
        if isinstance(node, dict) and node.get("id") == "2":
            raise RuntimeError("boom")
        return real(node, city, include_online)

    monkeypatch.setattr(meetup, "_parse_node", flaky)
    with caplog.at_level(logging.WARNING, logger=meetup.__name__):
        evs = meetup.parse_events(payload, CITY, START, END)
    assert {e.source_id for e in evs} >= {"1", "5"} and "2" not in {e.source_id for e in evs}
    assert any("RuntimeError" in m for m in meetup_warnings(caplog))


def test_no_fee_is_free_to_rsvp_not_free():
    t = datetime(2026, 11, 13, 20, 0, tzinfo=MADRID)
    zero = make_node(1, t, feeSettings={"amount": 0, "currency": "EUR"})
    none = make_node(2, t, feeSettings=None)
    paid = make_node(3, t, feeSettings={"amount": 12.5, "currency": "EUR"})
    evs = {e.source_id: e for e in meetup.parse_events(gql([zero, none, paid]), CITY, START, END)}
    for sid in ("1", "2"):
        assert evs[sid].price == "Free to RSVP" and evs[sid].is_free is None
    assert evs["3"].price == "€12.5" and evs["3"].is_free is False


def test_attending_uses_only_yes_count():
    t = datetime(2026, 11, 13, 20, 0, tzinfo=MADRID)
    missing = make_node(1, t, rsvps={"totalCount": 40, "waitlistCount": 2})
    present = make_node(2, t, rsvps={"totalCount": 40, "yesCount": 31})
    evs = {e.source_id: e for e in meetup.parse_events(gql([missing, present]), CITY, START, END)}
    assert evs["1"].attending is None and evs["1"].attending_label is None
    assert evs["2"].attending == 31 and evs["2"].attending_label == "going"
