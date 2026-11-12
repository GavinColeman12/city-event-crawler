"""Offline tests for the Luma digest collector (fixture = real discover-feed payload, trimmed)."""
import copy
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.digest.collectors import luma
from backend.digest.models import validate_event

TZ = ZoneInfo("Europe/Madrid")
FIXTURE = Path(__file__).parent / "fixtures" / "digest_luma.json"
START = datetime(2026, 11, 12, 0, 0, tzinfo=TZ)
END = datetime(2026, 11, 14, 6, 0, tzinfo=TZ)


@pytest.fixture(scope="module")
def data():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def entries(data):
    return {e["event"]["name"]: e for e in data["feed"]["entries"]}


@pytest.fixture(scope="module")
def events(data):
    out = []
    for entry in data["feed"]["entries"]:
        det = data["details"].get(entry["api_id"])
        ev = luma.parse_entry(entry, TZ, det)
        if ev is not None and START <= ev.start < END:
            out.append(ev)
    return out


def by_title(events, prefix):
    matches = [e for e in events if e.title.startswith(prefix)]
    assert len(matches) == 1, prefix
    return matches[0]


def test_fixture_size(data):
    assert len(data["feed"]["entries"]) >= 6


def test_events_valid_and_tz_aware(events):
    assert len(events) == 7
    for ev in events:
        validate_event(ev)
        assert ev.source == "luma"
        assert ev.start.tzinfo.key == "Europe/Madrid"
        assert ev.url.startswith("https://luma.com/")
        assert ev.attending_label in (None, "guests")


def test_utc_converted_to_madrid(events):
    ev = by_title(events, "Karaoke Thursdays")
    assert ev.start == datetime(2026, 11, 12, 19, 0, tzinfo=TZ)      # 18:00Z
    assert ev.end == datetime(2026, 11, 13, 0, 0, tzinfo=TZ)         # 23:00Z


def test_window_filter(data):
    feed = luma.parse_feed(data["feed"], TZ, START, END)
    assert len(feed) == 7
    assert all(START <= e.start < END for e in feed)
    assert not any(e.title == "The Long Run" for e in feed)          # 15 Nov, outside window
    today = luma.parse_feed(data["feed"], TZ, START, datetime(2026, 11, 13, 0, 0, tzinfo=TZ))
    assert len(today) == 6


def test_online_events_skipped(entries):
    online = copy.deepcopy(entries["Karaoke Thursdays"])
    online["event"]["location_type"] = "online"       # variant of a real entry
    assert luma.parse_entry(online, TZ) is None
    assert luma.parse_entry(entries["Karaoke Thursdays"], TZ) is not None


def test_karaoke_fields(events):
    ev = by_title(events, "Karaoke Thursdays")
    assert ev.venue == "Nomad House"
    assert ev.address == "Nomad House, Carrer del Notariat, 1, Ciutat Vella, 08001 Barcelona, Spain"
    assert ev.area == "El Raval"
    assert ev.organizer == "Nomad House"
    assert ev.attending == 76 and ev.attending_label == "guests"
    assert ev.price == "Free" and ev.is_free is True and ev.sold_out is False
    assert ev.description and ev.description.startswith("Apparently giving people a microphone")
    assert ev.image_url and "lumacdn.com" in ev.image_url


def test_paid_event_and_extra_stats(events):
    ev = by_title(events, "Bingo de ARAU")
    assert ev.price == "€7.60" and ev.is_free is False
    assert ev.attending == 14
    assert ev.extra_stats == {"spots_remaining": 5}
    assert "tickets" not in ev.extra_stats
    assert ev.organizer == "Bingueros de ARAU"
    assert "Near capacity" in ev.tags
    assert any(t == "Games" for t in ev.tags)


def test_sold_out_and_waitlist(events):
    sold = by_title(events, "PROVIDING THE JUICE")
    assert sold.sold_out is True and sold.attending == 94
    assert sold.tags[:2] == ["Fitness", "Running"]
    assert sold.organizer == "Gravity Run Club"          # calendar is "Personal" -> host name
    wl = by_title(events, "VSPC Zine Club")
    assert "Waitlist" in wl.tags and wl.attending == 15


def test_hidden_guest_list_is_unknown_not_zero(events):
    ev = by_title(events, "Lucky Friday")
    assert ev.attending is None and ev.attending_label is None
    assert ev.price == "€25" and ev.organizer == "We Met HERE"


def test_obfuscated_address_uses_area_only(entries):
    entry = next(e for n, e in entries.items() if n.startswith("Afterwork AperiBarcelona"))
    ev = luma.parse_entry(entry, TZ)
    assert ev.venue is None and ev.address is None
    assert ev.area == "Sant Antoni"
    assert ev.attending == 49


def test_next_data_fallback_parser():
    html = ('<html><script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"initialData":{"data":{"place":{"name":"Barcelona"},"events":[{"api_id":"x"}]}}}}}'
            '</script></html>')
    parsed = luma.parse_next_data(html)
    assert parsed["place"]["name"] == "Barcelona" and parsed["entries"] == [{"api_id": "x"}]


def test_tickets_stat_never_present(events):
    for ev in events:
        assert "tickets" not in ev.extra_stats


def test_cancelled_event_skipped(entries):
    gone = copy.deepcopy(entries["Karaoke Thursdays"])
    gone["registration_availability"] = "cancelled"
    assert luma.parse_entry(gone, TZ) is None
    assert luma.parse_entry(entries["Karaoke Thursdays"], TZ, {"status": "postponed"}) is None


def test_non_200_detail_logs_warning(data, caplog):
    import asyncio
    import logging

    import httpx

    from backend.digest.cities import get_city

    feed = copy.deepcopy(data["feed"])

    def handler(request):
        if request.url.path == "/discover/get-paginated-events":
            return httpx.Response(200, json=dict(feed, has_more=False, next_cursor=None))
        return httpx.Response(503)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await luma.collect(get_city("barcelona"), START, END, client)

    with caplog.at_level(logging.WARNING, logger="backend.digest.collectors.luma"):
        events = asyncio.run(go())
    assert len(events) == 7                        # events survive without their detail enrichment
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    # one summary line, not one per event — the orchestrator shows these to the reader
    assert len(msgs) == 1 and "7 of 7" in msgs[0] and "HTTP 503" in msgs[0]


def test_a_single_failed_detail_is_not_worth_a_warning(data, caplog):
    """Losing one event's description must not mark the whole source 'partial'."""
    import asyncio
    import logging

    import httpx

    from backend.digest.cities import get_city

    feed = copy.deepcopy(data["feed"])
    seen = []

    def handler(request):
        if request.url.path == "/discover/get-paginated-events":
            return httpx.Response(200, json=dict(feed, has_more=False, next_cursor=None))
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(503)              # exactly one detail request fails
        return httpx.Response(200, json={})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await luma.collect(get_city("barcelona"), START, END, client)

    with caplog.at_level(logging.INFO, logger="backend.digest.collectors.luma"):
        events = asyncio.run(go())
    assert len(events) == 7
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("1 of 7 event detail requests failed" in r.getMessage() for r in caplog.records)
