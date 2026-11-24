"""Offline tests for the DICE digest collector (parses a saved dice.fm payload)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.digest.cities import get_city
from backend.digest.collectors import dice
from backend.digest.models import validate_event

FIXTURE = Path(__file__).parent / "fixtures" / "digest_dice.json"
MADRID = ZoneInfo("Europe/Madrid")
CITY = get_city("barcelona")
START = datetime(2026, 11, 12, 0, 0, tzinfo=MADRID)
END = datetime(2026, 11, 14, 6, 0, tzinfo=MADRID)


@pytest.fixture(scope="module")
def fx():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def listings(fx):
    out = []
    for page in fx["listing_pages"].values():
        out += dice.parse_listing_page(page)["events"]
    return out


@pytest.fixture(scope="module")
def details(fx):
    return {i: dice.parse_detail_page(p) for i, p in fx["details"].items()}


@pytest.fixture(scope="module")
def events(listings, details):
    return dice.parse_events(listings, details, CITY, START, END)


def by_title(events, title):
    matches = [e for e in events if e.title == title]
    assert len(matches) == 1, title
    return matches[0]


def test_fixture_is_big_enough(listings):
    assert len(listings) >= 6


def test_every_event_passes_contract(events):
    assert len(events) >= 6
    for ev in events:
        validate_event(ev)
        assert ev.source == "dice"
        assert ev.start.tzinfo is not None
        assert ev.start.utcoffset() == START.utcoffset()
        assert ev.start.tzinfo.key == "Europe/Madrid"
        assert ev.url.startswith("https://dice.fm/event/")


def test_window_filter(listings, details, events):
    assert all(START <= e.start < END for e in events)
    # The fixture also holds listings that start after the window; they must be dropped.
    outside = [l for l in listings if not dice.in_window(l, MADRID, START, END)]
    assert outside, "fixture should contain out-of-window listings"
    assert len(events) == len(listings) - len(outside)
    # Narrowing the window to just Thursday removes Friday's events.
    thursday = dice.parse_events(listings, details, CITY, START, datetime(2026, 11, 13, 0, 0, tzinfo=MADRID))
    assert thursday and all(e.start.day == 12 for e in thursday)
    assert len(thursday) < len(events)


def test_dedupes_repeated_listings(listings, details):
    doubled = dice.parse_events(listings + listings, details, CITY, START, END)
    assert len(doubled) == len(dice.parse_events(listings, details, CITY, START, END))


def test_other_city_listings_are_dropped(listings, details):
    foreign = json.loads(json.dumps(listings))
    for item in foreign:
        for v in item["venues"]:
            v["city"]["id"] = "000000000000000000000000"
    assert dice.parse_events(foreign, details, CITY, START, END) == []


def test_lineup_venue_price_for_chez_perros(events):
    ev = by_title(events, "Chez Perros")
    assert ev.venue == "Village Underground Barcelona"
    assert "Hospitalet" in ev.address or "Barcelona" in ev.address
    assert ev.price == "€18"
    assert ev.is_free is False
    assert ev.sold_out is False
    assert ev.lineup == ["VIKK", "Perro Jimbo", "Sohrab"]  # section headers are skipped
    assert ev.organizer == "Village Underground Barcelona"
    assert ev.tags == ["DJ"]
    assert ev.start == datetime(2026, 11, 13, 22, 0, tzinfo=MADRID)
    assert ev.url == ("https://dice.fm/event/ww9r5v-chez-perros-13th-nov-"
                      "village-underground-barcelona-lhospitalet-de-llobregat-tickets")
    assert ev.source_id == "6ac7b623399cf10001080ebb"


def test_sold_out_event(events):
    ev = by_title(events, "oskar med k · Bofirax · Isa Rojas")
    assert ev.sold_out is True
    assert ev.price == "€31"
    assert ev.venue == "SALA APOLO"
    assert ev.lineup == ["oskar med k", "Bofirax", "Isa Rojas"]
    assert ev.organizer == "NITSA CLUB"
    assert "Waiting list" in ev.tags


def test_free_and_from_prices(events):
    free = by_title(events, "Deep, Tech, House: Hermes Disco Eterno")
    assert (free.price, free.is_free) == ("Free", True)
    frm = by_title(events, "Greetings From Barcelona: Sheer Mag, Powerplant...")
    assert (frm.price, frm.is_free) == ("From €14.56", False)
    assert frm.venue == "Sala Upload"
    assert frm.tags == ["Gigs"]


def test_no_invented_popularity(events):
    for ev in events:
        assert ev.attending is None
        assert ev.attending_label is None


def test_format_price_edge_cases():
    assert dice.format_price(None) == (None, None)
    assert dice.format_price({"currency": "EUR", "amount": 0}) == ("Free", True)
    assert dice.format_price({"currency": "EUR", "amount": 1500}) == ("€15", False)
    assert dice.format_price({"currency": "EUR", "amount": 1118}) == ("€11.18", False)
    assert dice.format_price({"currency": "GBP", "amount": None, "amount_from": 1200}) == ("From £12", False)
    assert dice.format_price({"currency": "EUR", "amount": None, "amount_from": None}) == (None, None)


def test_event_without_detail_still_valid(listings):
    item = next(l for l in listings if dice.in_window(l, MADRID, START, END))
    ev = dice.build_event(item, None, MADRID)
    validate_event(ev)
    assert ev.organizer is None and ev.lineup == []
    assert ev.url == "https://dice.fm/event/" + item["id"]


def test_extract_next_data_roundtrip():
    html = '<html><script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"events": []}}}</script></html>'
    assert dice.parse_listing_page(dice.extract_next_data(html))["events"] == []
    with pytest.raises(ValueError):
        dice.extract_next_data("<html></html>")


# ───────────────────────── fetch-path behaviour (MockTransport) ─────────────────────────

import asyncio
import logging

import httpx

PARAMS = dice.CITY_PARAMS["barcelona"]
SLUG = PARAMS["slug"]


def _event(eid, start_iso, **extra):
    ev = {
        "id": eid, "name": "Event %s" % eid, "status": "on-sale",
        "dates": {"event_start_date": start_iso, "event_end_date": None},
        "venues": [{"name": "V", "address": "A", "city": {"id": PARAMS["city_id"], "name": "Barcelona"}}],
        "price": {"currency": "EUR", "amount": 1000, "amount_from": None},
        "images": {},
    }
    ev.update(extra)
    return ev


def _page_html(events, cursor=None, sub_links=None):
    pp = {
        "city": {"id": PARAMS["city_id"], "name": "Barcelona"},
        "events": events,
        "nextCursor": cursor,
        "secondaryFilters": [{"activateLink": "/browse/%s/%s" % (SLUG, p)} for p in (sub_links or [])],
    }
    blob = json.dumps({"props": {"pageProps": pp}})
    return '<html><script id="__NEXT_DATA__" type="application/json">%s</script></html>' % blob


def _run(handler, window_end=END):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await dice.collect(CITY, START, window_end, client)
    return asyncio.run(go())


def _detail_404_or_listing(listing_for):
    """Handler: listing pages come from ``listing_for(path)``; detail pages 404."""
    seen = []

    def handler(request):
        path = request.url.path
        seen.append(path)
        if path.startswith("/event/"):
            return httpx.Response(404)
        prefix = "/browse/%s" % SLUG
        rel = path[len(prefix):].strip("/")
        return httpx.Response(200, text=listing_for(rel))
    handler.seen = seen
    return handler


def test_capped_category_fetches_every_subgenre_page(caplog):
    subs = ["music/gig/sub%d" % i for i in range(20)]   # more than the old cap of 12
    early = _event("e0", "2026-11-12T20:00:00+01:00")

    def listing_for(rel):
        if rel == "music/gig":
            return _page_html([early], cursor="CUR", sub_links=subs)
        if rel in subs:
            return _page_html([_event("s_" + rel.split("/")[-1], "2026-11-13T20:00:00+01:00")])
        return _page_html([])

    h = _detail_404_or_listing(listing_for)
    with caplog.at_level(logging.WARNING, logger="backend.digest.collectors.dice"):
        events = _run(h)
    fetched_subs = [p for p in h.seen if "/music/gig/sub" in p]
    assert len(fetched_subs) == 20
    assert {"s_sub%d" % i for i in range(20)} <= {e.source_id for e in events}


def test_request_ceiling_logs_warning(caplog, monkeypatch):
    subs = ["music/gig/sub%d" % i for i in range(20)]
    monkeypatch.setattr(dice, "MAX_LISTING_REQUESTS", len(dice.CATEGORIES) + 5)

    def listing_for(rel):
        if rel == "music/gig":
            return _page_html([_event("e0", "2026-11-12T20:00:00+01:00")], cursor="CUR", sub_links=subs)
        return _page_html([])

    h = _detail_404_or_listing(listing_for)
    with caplog.at_level(logging.WARNING, logger="backend.digest.collectors.dice"):
        _run(h)
    assert len([p for p in h.seen if "/music/gig/sub" in p]) == 5
    assert any("ceiling" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_capped_subgenre_and_failed_page_warn(caplog):
    subs = ["music/gig/a", "music/gig/b"]

    def listing_for(rel):
        if rel == "music/gig":
            return _page_html([_event("e0", "2026-11-12T20:00:00+01:00")], cursor="C", sub_links=subs)
        if rel == "music/gig/a":   # itself capped before the window end
            return _page_html([_event("a0", "2026-11-12T21:00:00+01:00")], cursor="C2")
        raise AssertionError(rel)

    def handler(request):
        rel = request.url.path[len("/browse/%s" % SLUG):].strip("/")
        if request.url.path.startswith("/event/") or rel == "music/gig/b":
            return httpx.Response(500)
        return httpx.Response(200, text=listing_for(rel) if rel in ("music/gig", "music/gig/a") else _page_html([]))

    with caplog.at_level(logging.WARNING, logger="backend.digest.collectors.dice"):
        _run(handler)
    msgs = " | ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "still capped" in msgs
    assert "sub-genre pages failed" in msgs


def test_malformed_row_skipped_not_fatal(caplog, listings, details):
    good = next(l for l in listings if dice.in_window(l, MADRID, START, END))
    bad_rows = [
        {"id": "bad1", "name": "No dates", "venues": [], "dates": None},
        {"id": "bad2", "name": "Bad price", "dates": good["dates"], "venues": good["venues"],
         "price": {"currency": "EUR", "amount": "oops"}},
    ]
    with caplog.at_level(logging.WARNING, logger="backend.digest.collectors.dice"):
        out = dice.parse_events(bad_rows + [good], details, CITY, START, END)
    assert [e.source_id for e in out] == [good["id"]]
    assert any("malformed" in r.getMessage() for r in caplog.records)


def test_collect_survives_row_without_dates_key():
    # Row lacks "dates" entirely on a capped page: used to raise KeyError in collect().
    odd = {"id": "odd", "name": "Odd", "venues": []}

    def listing_for(rel):
        if rel == "music/gig":
            return _page_html([odd, _event("ok", "2026-11-12T20:00:00+01:00")], cursor="C", sub_links=[])
        return _page_html([])

    events = _run(_detail_404_or_listing(listing_for))
    assert [e.source_id for e in events] == ["ok"]


def test_cancelled_and_postponed_are_skipped():
    rows = [
        _event("c1", "2026-11-12T20:00:00+01:00", status="cancelled"),
        _event("p1", "2026-11-12T21:00:00+01:00", status="postponed"),
        _event("ok", "2026-11-12T22:00:00+01:00", status="sold-out"),
    ]
    out = dice.parse_events(rows, {}, CITY, START, END)
    assert [e.source_id for e in out] == ["ok"]
    assert out[0].sold_out is True
