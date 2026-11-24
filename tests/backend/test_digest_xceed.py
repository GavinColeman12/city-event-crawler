"""Offline tests for the Xceed digest collector.

The fixture is a real capture (2026-11-12) of Xceed's Barcelona listing, per-event
detail, offers and venue payloads — see tests/backend/fixtures/digest_xceed.json.
No network is touched.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.digest.collectors import CollectorError, xceed
from backend.digest.models import validate_event

FIXTURE = Path(__file__).parent / "fixtures" / "digest_xceed.json"
MADRID = ZoneInfo("Europe/Madrid")

COHETE = 251319        # "COHETE X MELT: G SPOT: SRXMAS ..." — club night running past midnight
TIKITAKA = 251189      # free guest list + paid tickets, City Hall
NAVIDER = 250296       # Slow Club, free entrance option + paid, ends 06:00


@pytest.fixture(scope="module")
def fx():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def window(fx):
    return tuple(datetime.fromisoformat(x) for x in fx["window"])


def _build_all(fx, start, end):
    items, _total = xceed.parse_listing(fx["listing"])
    pool = fx["popular_ids"]
    events = []
    for item in items:
        if not xceed.in_window(item, MADRID, start, end):
            continue
        rank = (pool.index(item["id"]) + 1, len(pool)) if item["id"] in pool else None
        ev = xceed.build_event(
            item, MADRID, "barcelona",
            fx["details"].get(str(item["legacyId"])),
            fx["offers"].get(item["id"]),
            fx["venues"].get(item["venue"]["slug"]),
            rank,
        )
        if ev is not None:
            events.append(ev)
    return events


@pytest.fixture(scope="module")
def events(fx, window):
    return _build_all(fx, *window)


def _by_id(events, legacy_id):
    return next(e for e in events if e.source_id == str(legacy_id))


def test_fixture_is_big_enough_and_small_enough():
    assert FIXTURE.stat().st_size < 80 * 1024
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    items, total = xceed.parse_listing(data["listing"])
    assert len(items) >= 6
    assert total == len(items)


def test_every_event_passes_validate_event(events):
    assert len(events) == 20
    for ev in events:
        validate_event(ev)
        assert ev.source == "xceed"
        assert ev.url.startswith("https://xceed.me/en/barcelona/event/")
        assert ev.url.endswith("/" + ev.source_id)
        assert ev.attending is None            # Xceed publishes no attendance number


def test_times_are_tz_aware_madrid(events):
    for ev in events:
        assert ev.start.utcoffset() == timedelta(hours=1)   # CET on 12–13 Nov 2026
        assert ev.start.tzinfo.key == "Europe/Madrid"
        if ev.end is not None:
            assert ev.end.tzinfo.key == "Europe/Madrid"
            assert ev.end > ev.start
    cohete = _by_id(events, COHETE)
    assert cohete.start == datetime(2026, 11, 12, 23, 30, tzinfo=MADRID)
    assert cohete.end == datetime(2026, 11, 13, 5, 30, tzinfo=MADRID)    # ends next morning


def test_window_filter(fx, window):
    start, end = window
    items, _ = xceed.parse_listing(fx["listing"])
    assert len(items) == 24                    # 20 in window + 4 captured past the window end
    events = _build_all(fx, start, end)
    assert len(events) == 20
    assert all(start <= ev.start < end for ev in events)
    # the 4 outside events (e.g. "BUS DE MODA", 14 Nov 12:00) are dropped...
    assert "BUS DE MODA" not in {e.title for e in events}
    # ...and narrowing the window to Thursday only drops Friday's events
    thursday_end = datetime(2026, 11, 13, 6, 0, tzinfo=MADRID)
    thursday = _build_all(fx, start, thursday_end)
    assert 0 < len(thursday) < len(events)
    assert all(ev.start < thursday_end for ev in thursday)


def test_named_event_cohete(events):
    ev = _by_id(events, COHETE)
    assert ev.title == "COHETE X MELT: G SPOT: SRXMAS / DISCO BAR: LOS DOS MIL (BRUNO NOT BRUNO)"
    assert ev.venue == "Cohete: G Spot / Disco Bar"
    assert ev.address == "Av. Diagonal, 593, 08014 Barcelona"
    assert ev.organizer == "Cohete: G Spot / Disco Bar"
    assert ev.lineup == ["SRXMAS", "Bruno Castillo"]
    assert ev.tags[:2] == ["House", "90s - 00s"]
    assert ev.price == "€15–25"            # tickets €15 and €25; €250 bottle-service tables excluded
    assert ev.is_free is False
    assert ev.sold_out is False
    assert ev.url == ("https://xceed.me/en/barcelona/event/"
                      "cohete-x-melt-g-spot-srxmas-disco-bar-los-dos-mil-bruno-not-bruno/251319")
    assert ev.extra_stats["min_age"] == 18
    assert ev.extra_stats["venue_followers"] == 18
    assert ev.organizer_followers is None  # venue followers are never attributed to the organiser
    assert ev.extra_stats["xceed_popular_rank"] == 1
    assert ev.extra_stats["xceed_popular_pool"] == 20
    assert ev.extra_stats["ticket_stock"] == 47 + 246
    assert "tickets_left" not in ev.extra_stats


def test_named_event_free_guest_list(events):
    ev = _by_id(events, TIKITAKA)
    assert ev.title == "Tikitaka Club w// Zé Pequeña"
    assert ev.venue == "City Hall"
    assert ev.address == "Rambla de Catalunya, 4"
    assert ev.lineup == ["ZÉ PEQUEÑA"]
    assert ev.tags[:4] == ["Reggaeton", "Trap", "Cumbia", "Urban"]
    assert "free-guest-list" in ev.tags
    assert ev.price == "Free guest list · paid €12–15"
    assert ev.is_free is False
    assert ev.organizer_followers is None
    assert ev.extra_stats["venue_followers"] == 567


def test_named_event_free_entry_and_decimal_price(events):
    ev = _by_id(events, NAVIDER)
    assert ev.venue == "Slow Club"
    assert ev.price == "Free entry · paid €10.50"
    assert ev.end == datetime(2026, 11, 14, 6, 0, tzinfo=MADRID)
    secret = _by_id(events, 250486)
    assert secret.price == "€5.50–12.50"


def test_tba_lineup_placeholder_is_dropped(fx):
    items, _ = xceed.parse_listing(fx["listing"])
    hope = next(i for i in items if i["legacyId"] == 249778)
    assert [a["name"] for a in hope["lineup"]] == ["TBA"]          # raw payload has the placeholder
    ev = xceed.build_event(hope, MADRID)
    assert ev.lineup == []
    assert ev.price is None and ev.is_free is None                  # no offers payload → nothing invented


def _offer(kind, status, amount, quantity=None, threshold=None, name="Ticket"):
    return {"_k": kind, "status": status, "name": name, "quantity": quantity,
            "ticketsLeftThreshold": threshold, "price": {"amount": amount, "currency": "EUR"},
            "salesStatus": {"isSoldOut": status == "sold_out", "isSalesClosed": False}}


def test_summarize_offers_states():
    only_free = xceed.summarize_offers({"guestList": [_offer("g", "on_sale", 0, name="Guest List")]})
    assert (only_free["price"], only_free["is_free"], only_free["sold_out"]) == ("Free guest list", True, False)

    sold = xceed.summarize_offers({"ticket": [_offer("t", "sold_out", 12, 0)],
                                   "bottleService": [_offer("b", "on_sale", 250)]})
    assert sold["sold_out"] is True                    # bottle-service availability does not matter

    stock = xceed.summarize_offers({"ticket": [_offer("t", "on_sale", 15, quantity=7, threshold=10)]})
    assert stock["ticket_stock"] == 7 and stock["price"] == "€15"
    assert "last_tickets" not in stock

    empty = xceed.summarize_offers({"ticket": [], "guestList": [], "bottleService": []})
    assert empty["price"] is None and empty["sold_out"] is None
    assert xceed.summarize_offers(None)["price"] is None


def test_low_threshold_never_produces_last_tickets_tag(fx):
    items, _ = xceed.parse_listing(fx["listing"])
    item = next(i for i in items if i["legacyId"] == COHETE)
    offers = {"ticket": [_offer("t", "on_sale", 15, quantity=3, threshold=10)]}
    ev = xceed.build_event(item, MADRID, "barcelona", None, offers)
    assert "last-tickets" not in ev.tags
    assert ev.extra_stats["ticket_stock"] == 3


def test_no_company_means_no_organizer_and_no_organizer_followers(fx):
    items, _ = xceed.parse_listing(fx["listing"])
    item = next(i for i in items if i["legacyId"] == COHETE)
    venue_info = {"followersCount": 4321}
    for detail in (None, {"venue": {"address": "x"}}, {"company": None}):
        ev = xceed.build_event(item, MADRID, "barcelona", detail, None, venue_info)
        assert ev.organizer is None
        assert ev.organizer_followers is None
        assert ev.extra_stats["venue_followers"] == 4321


def test_guest_list_offers_excluded_from_ticket_stock():
    offers = {
        "ticket": [_offer("t", "on_sale", 15, quantity=40)],
        "guestList": [_offer("g", "on_sale", 0, quantity=500, name="Guest List")],
    }
    summary = xceed.summarize_offers(offers)
    assert summary["ticket_stock"] == 40
    # a ticket-kind offer that is named as a guest list is excluded too
    offers["ticket"].append(_offer("t", "on_sale", 0, quantity=100, name="Free Guest list"))
    assert xceed.summarize_offers(offers)["ticket_stock"] == 40
    only_guest = xceed.summarize_offers({"guestList": [_offer("g", "on_sale", 0, quantity=9, name="Guest List")]})
    assert only_guest["ticket_stock"] is None


def test_explicitly_cancelled_event_is_skipped(fx):
    items, _ = xceed.parse_listing(fx["listing"])
    item = dict(next(i for i in items if i["legacyId"] == COHETE))
    assert xceed.build_event(item, MADRID) is not None
    assert xceed.build_event(dict(item, status="cancelled"), MADRID) is None
    assert xceed.build_event(item, MADRID, "barcelona", {"isPostponed": True}) is None


def test_parse_listing_rejects_garbage():
    with pytest.raises(CollectorError):
        xceed.parse_listing({"success": False, "error": {"message": "nope"}})
    with pytest.raises(CollectorError):
        xceed.parse_listing([])
