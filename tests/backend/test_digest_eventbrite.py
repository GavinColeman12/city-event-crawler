"""Offline tests for the Eventbrite digest collector (fixture = real destination/search payload)."""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.digest.collectors import eventbrite as eb
from backend.digest.models import validate_event

TZ = ZoneInfo("Europe/Madrid")
FIXTURE = Path(__file__).parent / "fixtures" / "digest_eventbrite.json"
START = datetime(2026, 11, 12, 0, 0, tzinfo=TZ)
END = datetime(2026, 11, 14, 6, 0, tzinfo=TZ)
CENTER = (41.3851, 2.1734, 25.0)


@pytest.fixture(scope="module")
def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def events(payload):
    return eb.parse_search(payload, TZ, START, END, "101748323", CENTER)


def by_title(events, prefix):
    matches = [e for e in events if e.title.startswith(prefix)]
    assert len(matches) == 1, prefix
    return matches[0]


def test_fixture_is_real_and_big_enough(payload):
    assert len(payload["events"]["results"]) >= 6


def test_every_event_is_contract_valid(events):
    assert len(events) >= 6
    for ev in events:
        validate_event(ev)
        assert ev.source == "eventbrite"
        assert not ev.is_online
        assert "?" not in ev.url


def test_starts_are_tz_aware_madrid(events):
    for ev in events:
        assert ev.start.utcoffset() is not None
        assert ev.start.tzinfo.key == "Europe/Madrid"
    ev = by_title(events, "Singles Night")
    assert ev.start == datetime(2026, 11, 12, 20, 30, tzinfo=TZ)


def test_window_filter(payload, events):
    for ev in events:
        assert START <= ev.start < END
    # "ART LLOBET" (14 Nov 18:30) is in the payload but after the window
    assert not any(e.title.startswith("ART LLOBET") for e in events)
    # shrinking the window drops tomorrow's events
    today_only = eb.parse_search(payload, TZ, START, datetime(2026, 11, 13, 0, 0, tzinfo=TZ),
                                 "101748323", CENTER)
    assert 0 < len(today_only) < len(events)
    assert all(e.start.day == 12 for e in today_only)


def test_online_events_skipped(payload):
    online = payload["events"]["results"][-1]
    assert online["is_online_event"] is True
    assert eb.parse_event(online, TZ) is None
    assert not any(e.source_id == online["id"] for e in eb.parse_search(payload, TZ, START, END))


def test_out_of_area_events_dropped(payload, events):
    # the places filter leaks Girona / Sitges; those are in the payload but must not survive
    titles = " ".join(e.title for e in events)
    assert "adornos navide" not in titles          # Girona venue
    assert "Mediterranean Soul" not in titles      # Sitges venue
    assert any("adornos navide" in r["name"] for r in payload["events"]["results"])


def test_singles_night_fields(events):
    ev = by_title(events, "Singles Night")
    assert ev.venue == "Gaby's Club"
    assert ev.address == "24 Carrer del Topazi, 08012 Barcelona"
    assert ev.organizer == "BCN Afterwork Friends and Fun"
    assert ev.organizer_followers == 484
    assert ev.price == "€12.87–16.63"
    assert ev.is_free is False
    assert ev.sold_out is False
    assert "Going fast" in ev.tags and "Party or Social Gathering" in ev.tags
    assert ev.attending is None          # Eventbrite exposes no attendee count
    assert ev.url.startswith("https://www.eventbrite.") and "tickets-" in ev.url
    assert ev.image_url and ev.image_url.startswith("https://img.evbuc.com/")


def test_free_event(events):
    ev = by_title(events, "Pitch Competition")
    assert ev.price == "Free" and ev.is_free is True
    assert ev.organizer == "Aticco" and ev.organizer_followers == 3619
    assert "Few tickets left" in ev.tags


def test_sold_out_vs_sales_ended(events):
    sold = by_title(events, "#BAUMASTER")
    assert sold.sold_out is True and sold.organizer_followers == 2094 and sold.price == "€5"
    ended = by_title(events, "The Spanglish Show")
    assert ended.sold_out is None and "Sales ended" in ended.tags
    assert ended.price == "€9.10–11.32" and ended.organizer_followers == 1918


def test_non_euro_price(events):
    ev = by_title(events, "Barcelona Bash")
    assert ev.price == "$25"
    assert ev.organizer_followers == 9391


def test_neighbourhood_area(events):
    assert by_title(events, "Singles Night").area == "la Vila de Gracia"


# ───────────────────────── fetch path (mocked transport, no network) ─────────────────────────

import asyncio
import logging

import httpx

from backend.digest.cities import get_city
from backend.digest.collectors import CollectorError


def _raw(i):
    """A minimal destination_event row shaped like the real endpoint's, inside the window."""
    return {
        "id": "ev%03d" % i,
        "name": "Event %03d" % i,
        "url": "https://www.eventbrite.com/e/event-%03d-tickets-%03d?aff=x" % (i, i),
        "timezone": "Europe/Madrid",
        "start_date": "2026-11-12",
        "start_time": "%02d:%02d" % (10 + (i // 60) % 12, i % 60),
        "is_online_event": False,
        "primary_venue": {"name": "Venue", "address": {"latitude": "41.39", "longitude": "2.17",
                                                       "localized_address_display": "Carrer 1, Barcelona"}},
        "tags": [],
    }


class FakeEventbrite:
    """MockTransport handler: GET / hands out the csrftoken cookie, POST search serves ``pages``."""

    def __init__(self, pages, object_count=None, page_count=None, cookie=True, fail_pages=(), per_call=None):
        self.pages = pages                    # list of lists of raw rows (page 1 first)
        self.object_count = object_count if object_count is not None else sum(len(p) for p in pages)
        self.page_count = page_count if page_count is not None else len(pages)
        self.cookie = cookie
        self.fail_pages = set(fail_pages)
        self.per_call = per_call              # optional fn(page, call_no) -> rows, overrides pages
        self.calls = []                       # page numbers requested, in order
        self.bodies = []
        self.headers = []

    def __call__(self, request):
        if request.method == "GET":
            hdrs = {"set-cookie": "csrftoken=tok123; Path=/"} if self.cookie else {}
            return httpx.Response(200, headers=hdrs, text="<html></html>")
        body = json.loads(request.content)
        page = body["event_search"]["page"]
        self.bodies.append(body)
        self.headers.append(dict(request.headers))
        self.calls.append(page)
        if page in self.fail_pages:
            return httpx.Response(500, text="boom")
        if self.per_call:
            rows = self.per_call(page, self.calls.count(page))
        else:
            rows = self.pages[page - 1] if page - 1 < len(self.pages) else []
        return httpx.Response(200, json={"events": {
            "results": rows, "promoted_results": [],
            "pagination": {"object_count": self.object_count, "page_count": self.page_count,
                           "page_number": page, "page_size": 50}}})


def _collect(fake):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
            return await eb.collect(get_city("barcelona"), START, END, client)
    return asyncio.run(run())


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_duplicates_across_pages_are_deduplicated(caplog):
    caplog.set_level(logging.DEBUG, logger=eb.log.name)
    pages = [[_raw(i) for i in range(0, 5)], [_raw(i) for i in range(3, 8)], [_raw(i) for i in range(6, 10)]]
    fake = FakeEventbrite(pages, object_count=10)
    got = _collect(fake)
    assert sorted(e.source_id for e in got) == ["ev%03d" % i for i in range(10)]
    assert fake.calls[:3] == [1, 2, 3]                     # sequential, in order
    assert not _warnings(caplog)


def test_request_body_and_headers():
    fake = FakeEventbrite([[_raw(1)]])
    _collect(fake)
    body = fake.bodies[0]["event_search"]
    assert body["sort"] == "distance" and body["page"] == 1
    assert fake.headers[0]["referer"] == "https://www.eventbrite.com/"
    assert fake.headers[0]["x-csrftoken"] == "tok123"


def test_persistent_shortfall_warns(caplog):
    caplog.set_level(logging.DEBUG, logger=eb.log.name)
    pages = [[_raw(i) for i in range(0, 10)], [_raw(i) for i in range(10, 20)]]
    fake = FakeEventbrite(pages, object_count=40)          # the endpoint claims 40, only 20 exist
    got = _collect(fake)
    assert len(got) == 20
    msgs = _warnings(caplog)
    assert any("got 20 of 40" in m and "missing" in m for m in msgs), msgs


def test_small_structural_gap_is_not_warned(caplog):
    caplog.set_level(logging.DEBUG, logger=eb.log.name)
    pages = [[_raw(i) for i in range(0, 98)]]
    fake = FakeEventbrite(pages, object_count=100)         # 2% short is within tolerance
    _collect(fake)
    assert not _warnings(caplog)


def test_shortfall_fixed_by_extra_pass_does_not_warn(caplog):
    caplog.set_level(logging.DEBUG, logger=eb.log.name)
    first = [_raw(i) for i in range(0, 10)]
    second = [_raw(i) for i in range(10, 20)]

    def per_call(page, call_no):
        if page == 1:
            return first
        # the first time page 2 is asked for, the server drops 5 rows; the second time it is whole
        return second[:5] if call_no == 1 else second

    fake = FakeEventbrite([], object_count=20, page_count=2, per_call=per_call)
    got = _collect(fake)
    assert len(got) == 20
    assert fake.calls == [1, 2, 1, 2]                      # exactly one extra pass was needed
    assert not _warnings(caplog)


def test_failing_middle_page_keeps_other_pages(caplog):
    caplog.set_level(logging.DEBUG, logger=eb.log.name)
    pages = [[_raw(i) for i in range(0, 5)], [_raw(i) for i in range(5, 10)], [_raw(i) for i in range(10, 15)]]
    fake = FakeEventbrite(pages, fail_pages=(2,))
    got = _collect(fake)
    assert sorted(e.source_id for e in got) == ["ev%03d" % i for i in list(range(0, 5)) + list(range(10, 15))]
    msgs = _warnings(caplog)
    assert any("page 2 failed" in m and "CollectorError" in m for m in msgs), msgs
    assert 3 in fake.calls                                  # carried on past the failure


def test_page_one_failure_is_collector_error():
    fake = FakeEventbrite([[_raw(1)]], fail_pages=(1,))
    with pytest.raises(CollectorError):
        _collect(fake)


def test_page_count_above_cap_warns_truncation(caplog):
    caplog.set_level(logging.DEBUG, logger=eb.log.name)
    total = eb.MAX_PAGES + 3
    fake = FakeEventbrite([[_raw(p)] for p in range(total)], object_count=total)
    got = _collect(fake)
    assert len(got) == eb.MAX_PAGES
    assert max(fake.calls) == eb.MAX_PAGES
    msgs = _warnings(caplog)
    assert any("truncated" in m and "3 pages skipped" in m for m in msgs), msgs


def test_no_csrf_cookie_is_collector_error():
    fake = FakeEventbrite([[_raw(1)]], cookie=False)
    with pytest.raises(CollectorError):
        _collect(fake)
    assert fake.calls == []


def test_malformed_row_is_skipped_neighbours_parse(caplog):
    caplog.set_level(logging.DEBUG, logger=eb.log.name)
    bad_tag = _raw(2)
    bad_tag["tags"] = ["not-a-dict"]
    bad_venue = _raw(3)
    bad_venue["primary_venue"] = "nope"
    not_a_row = "garbage"
    page = [_raw(1), bad_tag, bad_venue, not_a_row, _raw(4)]
    fake = FakeEventbrite([page], object_count=5)
    got = _collect(fake)
    ids = [e.source_id for e in got]
    assert "ev001" in ids and "ev004" in ids
    assert "ev002" not in ids
