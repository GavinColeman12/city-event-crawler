"""Offline tests for the Resident Advisor digest collector (fixture = real RA GraphQL response)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.digest.cities import get_city
from backend.digest.collectors import CollectorError, ra
from backend.digest.models import validate_event

FIXTURE = Path(__file__).parent / "fixtures" / "digest_ra.json"
MADRID = ZoneInfo("Europe/Madrid")
CITY = get_city("barcelona")
START = datetime(2026, 11, 12, 0, 0, tzinfo=MADRID)
END = datetime(2026, 11, 14, 6, 0, tzinfo=MADRID)


@pytest.fixture(scope="module")
def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def events(payload):
    return ra.parse_listings(payload, CITY, START, END)


def by_id(events, source_id):
    return next(e for e in events if e.source_id == source_id)


def test_every_event_passes_contract(events):
    assert len(events) >= 6
    for ev in events:
        validate_event(ev)
        assert ev.source == "ra"
        assert ev.url == "https://ra.co/events/%s" % ev.source_id


def test_times_are_madrid_aware(events):
    for ev in events:
        assert ev.start.tzinfo is not None
        assert ev.start.utcoffset() == timedelta(hours=1)  # CET in November
        assert ev.start.tzinfo.key == "Europe/Madrid"
        if ev.end is not None:
            assert ev.end.utcoffset() == timedelta(hours=1)
            assert ev.end > ev.start


def test_window_filter(payload, events):
    assert all(START <= e.start < END for e in events)
    ids = {e.source_id for e in events}
    # listed on 14 Nov afternoon/evening in the raw payload, outside the window
    assert not ids & {"2480238", "2571943", "2506339"}
    raw_ids = {r["event"]["id"] for r in payload["data"]["eventListings"]["data"]}
    assert {"2480238", "2571943", "2506339"} <= raw_ids
    # tighten the window to 13 Nov only
    narrow = ra.parse_listings(payload, CITY, datetime(2026, 11, 13, tzinfo=MADRID), datetime(2026, 11, 14, tzinfo=MADRID))
    assert narrow and all(e.start.date().isoformat() == "2026-11-13" for e in narrow)


def test_duplicate_rows_collapsed(events):
    ids = [e.source_id for e in events]
    assert len(ids) == len(set(ids))


def test_named_event_stats_venue_lineup(events):
    ev = by_id(events, "2534142")
    assert ev.title == "Enrenou pres. Polygonia + Tunik"
    assert ev.attending == 145
    assert ev.attending_label == "interested"
    assert ev.venue == "Les Enfants Brillants"
    assert ev.address == "Carrer de Guàrdia, 3, 08001 Barcelona"
    assert ev.lineup == ["Polygonia", "Tunik"]
    assert ev.organizer == "Les Enfants Brillants"
    assert ev.tags == ["Techno", "Electro", "RA Pick"]
    assert ev.price == "€20"
    assert ev.start == datetime(2026, 11, 13, 23, 59, tzinfo=MADRID)
    assert ev.end == datetime(2026, 11, 14, 6, 0, tzinfo=MADRID)  # club night runs past midnight
    assert ev.image_url == "https://images.ra.co/0fe86d09f3c56bc03001cb7b56c6cad5c1ffa1a4.jpg"
    assert ev.extra_stats == {"minimum_age": 21}
    assert ev.sold_out is None  # RA does not expose it


def test_big_event_attending(events):
    ev = by_id(events, "2554207")
    assert ev.title == "POLENTA"
    assert ev.venue == "La Paloma"
    assert ev.attending == 520


def test_pick_blurb_used_when_no_description(events):
    ev = by_id(events, "2580965")
    assert "RA Pick" in ev.tags
    assert ev.lineup == [] and ev.organizer is None
    assert ev.description.startswith("Abysmo & Drakis presentan")


@pytest.mark.parametrize("cost,expected", [
    ("", (None, None)), ("€", (None, None)), ("0", ("Free", True)), ("0,00", ("Free", True)),
    ("10€", ("10€", None)), ("20", ("€20", False)),
])
def test_price_cleanup(cost, expected):
    # these cases do not depend on isTicketed; zero amounts need it (see below)
    if cost in ("0", "0,00"):
        expected = (None, None)
    assert ra._clean_price(cost) == expected


@pytest.mark.parametrize("cost", ["0", "0,00", "0 €", "€0"])
def test_zero_cost_depends_on_is_ticketed(cost):
    assert ra._clean_price(cost, True) == (None, None)     # price probably not entered
    assert ra._clean_price(cost, None) == (None, None)     # unknown -> claim nothing
    assert ra._clean_price(cost, False) == ("Free", True)
    assert ra._clean_price("Free", True) == ("Free", True)  # explicit word is still trusted


@pytest.mark.parametrize("cost,expected", [
    ("0/12", "€0–12"), ("22 / 25 €", "€22–25"), ("22/25€", "€22–25"),
    ("€10 / €15", "€10–15"), ("10 / 12,5", "€10–12,5"),
])
def test_slash_range_becomes_euro_range(cost, expected):
    assert ra._clean_price(cost, True) == (expected, None)


def test_other_free_text_is_left_as_written():
    assert ra._clean_price("10€ (puerta 12€)", True) == ("10€ (puerta 12€)", None)
    assert ra._clean_price("10/12/15", True) == ("10/12/15", None)


def test_bad_payload_raises():
    with pytest.raises(CollectorError):
        ra.parse_listings({"errors": [{"message": "x"}]}, CITY, START, END)


def test_unknown_city_raises():
    import asyncio

    class Fake:
        key = "atlantis"

    with pytest.raises(CollectorError):
        asyncio.run(ra.collect(Fake(), START, END, None))


# ───────────────────────── tolerant parsing + paging (MockTransport) ─────────────────────────

import asyncio
import logging

import httpx


def _row(eid, **over):
    ev = {
        "id": str(eid), "title": "Event %s" % eid,
        "startTime": "2026-11-13T23:00:00.000", "endTime": None,
        "isTicketed": True, "cost": "", "minimumAge": None, "pick": None,
        "images": [], "venue": {"name": "V", "address": "A", "area": {"name": "Barcelona"}},
        "artists": [], "promoters": [], "genres": [], "attending": 3, "interestedCount": 3,
    }
    ev.update(over)
    return {"id": "L%s" % eid, "listingDate": "2026-11-13", "event": ev}


def test_none_entries_in_lists_are_tolerated(caplog):
    rows = [_row(1, genres=[None, {"name": "Techno"}], artists=[{"name": "A"}, None],
                 promoters=[None, {"name": "P"}], images=[None, {"filename": "f.jpg", "type": "FLYERFRONT"}]),
            _row(2, venue=None, pick="oops"),
            _row(3, title=123)]       # bad row: title not a string
    with caplog.at_level(logging.WARNING, logger="backend.digest.collectors.ra"):
        out = ra.parse_listings({"rows": rows}, CITY, START, END)
    assert [e.source_id for e in out] == ["1", "2"]
    first = out[0]
    assert first.tags == ["Techno"] and first.lineup == ["A"] and first.organizer == "P"
    assert first.image_url == "f.jpg"
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def _gql(rows, total):
    return {"data": {"eventListings": {"totalResults": total, "data": rows}}}


def _collect(handler):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ra.collect(CITY, START, END, client)
    return asyncio.run(go())


def _paged_handler(second):
    calls = []

    def handler(request):
        page = json.loads(request.content)["variables"]["page"]
        calls.append(page)
        if page == 1:
            return httpx.Response(200, json=_gql([_row(i) for i in range(ra.PAGE_SIZE)], ra.PAGE_SIZE * 2))
        return second(page)
    handler.calls = calls
    return handler


@pytest.mark.parametrize("second", [
    lambda page: httpx.Response(200, text="<html>blocked</html>"),
    lambda page: httpx.Response(200, json={"errors": [{"message": "boom"}], "data": None}),
])
def test_page_two_failure_keeps_page_one_and_warns(second, caplog):
    h = _paged_handler(second)
    with caplog.at_level(logging.WARNING, logger="backend.digest.collectors.ra"):
        out = _collect(h)
    assert h.calls == [1, 2]
    assert len(out) == ra.PAGE_SIZE
    assert any("page 2" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


def test_page_one_failure_is_collector_error():
    with pytest.raises(CollectorError):
        _collect(lambda request: httpx.Response(200, text="not json"))
    with pytest.raises(CollectorError):
        _collect(lambda request: httpx.Response(200, json={"errors": [{"message": "boom"}]}))
