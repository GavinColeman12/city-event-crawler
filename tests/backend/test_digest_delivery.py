"""Offline tests for the digest's orchestration, WhatsApp delivery and command line.

Network is replaced by httpx.MockTransport; collectors by in-memory fakes.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from backend.digest import __main__ as cli
from backend.digest import engine, render, whatsapp
from backend.digest.cities import get_city
from backend.digest.collectors import CollectorError
from backend.digest.http import make_client
from backend.digest.models import DigestEvent

MADRID = ZoneInfo("Europe/Madrid")
CITY = get_city("barcelona")
NOW = datetime(2026, 11, 12, 15, 0, tzinfo=MADRID)


def event(title: str, day: int = 12, hour: int = 20, **kw) -> DigestEvent:
    kw.setdefault("url", "https://example.com/e/%s" % title.replace(" ", "-"))
    return DigestEvent(source=kw.pop("source", "meetup"), title=title,
                       start=datetime(2026, 11, day, hour, 0, tzinfo=MADRID), **kw)


# ───────────── WhatsApp configuration ─────────────

def test_config_names_exactly_what_is_missing():
    with pytest.raises(whatsapp.NotConfigured, match="missing WHATSAPP_PHONE and WHATSAPP_APIKEY"):
        whatsapp.config_from_env({})
    with pytest.raises(whatsapp.NotConfigured, match="missing WHATSAPP_APIKEY$"):
        whatsapp.config_from_env({"WHATSAPP_PHONE": "+34600000000"})


def test_config_normalises_the_phone_number_and_defaults_the_provider():
    cfg = whatsapp.config_from_env({"WHATSAPP_PHONE": " +34 600-000 (000) ", "WHATSAPP_APIKEY": " 123456 "})
    assert cfg == whatsapp.WhatsAppConfig("callmebot", "+34600000000", "123456")
    assert whatsapp.config_from_env({"WHATSAPP_PHONE": "34600000000", "WHATSAPP_APIKEY": "k"}).phone == "+34600000000"


@pytest.mark.parametrize("env, message", [
    ({"WHATSAPP_PROVIDER": "pigeon", "WHATSAPP_PHONE": "+34600000000", "WHATSAPP_APIKEY": "k"}, "not supported"),
    ({"WHATSAPP_PHONE": "call-me-maybe", "WHATSAPP_APIKEY": "k"}, "international format"),
])
def test_config_rejects_nonsense(env, message):
    with pytest.raises(whatsapp.NotConfigured, match=message):
        whatsapp.config_from_env(env)


# ───────────── splitting ─────────────

def test_split_never_cuts_an_event_block_and_loses_nothing():
    blocks = ["header"] + ["*%d. Event*\n🕘 Tonight 21:00 · 📍 Venue\n💬 %s\nhttps://example.com/%d" % (i, "x" * 150, i)
                           for i in range(1, 11)] + ["_footer_"]
    text = "\n\n".join(blocks)
    chunks = whatsapp.split_message(text, limit=700)
    assert len(chunks) > 1 and all(len(c) <= 700 for c in chunks)
    assert "\n\n".join(chunks) == text                       # nothing dropped or reordered
    for chunk in chunks:
        for block in chunk.split("\n\n"):
            assert block in blocks                           # every piece is a whole block


def test_split_hard_wraps_a_single_oversized_block_on_line_breaks():
    block = "\n".join("line %02d %s" % (i, "y" * 40) for i in range(40))
    chunks = whatsapp.split_message(block, limit=300)
    assert all(len(c) <= 300 for c in chunks)
    assert "\n".join(chunks).split("\n") == block.split("\n")


def test_short_message_is_one_chunk():
    assert whatsapp.split_message("hello\n\nworld") == ["hello\n\nworld"]
    assert whatsapp.split_message("  \n\n ") == []


def test_a_full_digest_goes_out_as_today_then_tomorrow():
    def block(i: int) -> str:
        return ("*%d. Some event with a fairly long title*\n🕘 Tonight 21:00 → 02:00 · 📍 Venue, Area\n"
                "📊 120 going on Meetup · group has 40,000 members\n💶 Free\n💬 %s\nhttps://example.com/e/%d"
                % (i, "A reason that runs to about a hundred characters or so, give or take a few words here.", i))

    text = "\n\n".join(["🎉 *Barcelona · Thu 12 Nov*", "━━ *TODAY · Thu 12 Nov* ━━"] + [block(i) for i in range(1, 6)]
                       + ["━━ *TOMORROW · Fri 13 Nov* ━━"] + [block(i) for i in range(1, 6)] + ["_Scanned: Meetup 363._"])
    assert len(text) > whatsapp.CHUNK_CHARS
    chunks = whatsapp.split_message(text)
    assert len(chunks) == 2 and all(len(c) <= whatsapp.CHUNK_CHARS for c in chunks)
    assert chunks[0].startswith("🎉 *Barcelona") and chunks[0].count("https://") == 5
    assert chunks[1].startswith("━━ *TOMORROW") and chunks[1].endswith("_Scanned: Meetup 363._")
    assert "\n\n".join(chunks) == text


def test_chunk_size_can_be_lowered_from_the_environment():
    base = {"WHATSAPP_PHONE": "+34600000000", "WHATSAPP_APIKEY": "k"}
    assert whatsapp.config_from_env(dict(base, WHATSAPP_CHUNK_CHARS="900")).chunk_chars == 900
    assert whatsapp.config_from_env(base).chunk_chars == whatsapp.CHUNK_CHARS
    with pytest.raises(whatsapp.NotConfigured, match="between 300 and 4000"):
        whatsapp.config_from_env(dict(base, WHATSAPP_CHUNK_CHARS="12"))


# ───────────── sending ─────────────

def _client(handler) -> httpx.AsyncClient:
    return make_client(transport=httpx.MockTransport(handler))


async def test_callmebot_request_shape_and_success():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="<p>Message queued. You will receive it in a few seconds.</p>")

    cfg = whatsapp.WhatsAppConfig("callmebot", "+34600000000", "123456")
    async with _client(handler) as client:
        results = await whatsapp.send_message("🎉 *Barcelona*\nline two & more", cfg, client, pause=0)
    assert [r.ok for r in results] == [True]
    request = seen[0]
    assert request.method == "GET" and request.url.host == "api.callmebot.com" and request.url.path == "/whatsapp.php"
    assert dict(request.url.params) == {"phone": "+34600000000", "text": "🎉 *Barcelona*\nline two & more", "apikey": "123456"}
    raw = request.url.raw_path.decode()
    assert "phone=%2B34600000000" in raw and "%0A" in raw     # '+' and the newline are percent-encoded
    assert "queued" in results[0].detail and "<p>" not in results[0].detail


async def test_textmebot_uses_its_own_host_and_parameter_name():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="Success")

    cfg = whatsapp.WhatsAppConfig("textmebot", "+34600000000", "k")
    async with _client(handler) as client:
        await whatsapp.send_message("hi", cfg, client, pause=0)
    assert seen[0].url.host == "api.textmebot.com" and seen[0].url.path == "/send.php"
    assert set(dict(seen[0].url.params)) == {"recipient", "apikey", "text"}


async def test_a_200_that_reports_an_error_is_a_failure_and_stops_the_run():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, text="ERROR: APIKey is invalid.")

    cfg = whatsapp.WhatsAppConfig("callmebot", "+34600000000", "bad")
    long_text = "\n\n".join("block %d %s" % (i, "z" * 900) for i in range(4))
    async with _client(handler) as client:
        results = await whatsapp.send_message(long_text, cfg, client, pause=0)
    assert len(calls) == 1 and len(results) == 1 and results[0].ok is False
    assert "invalid" in results[0].detail.lower()


async def test_sandbox_block_and_network_errors_are_reported_without_leaking_the_key():
    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"x-deny-reason": "host_not_allowed"}, text="blocked")

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("could not reach %s" % request.url, request=request)

    cfg = whatsapp.WhatsAppConfig("callmebot", "+34600000000", "SECRETKEY")
    async with _client(denied) as client:
        blocked = (await whatsapp.send_message("hi", cfg, client, pause=0))[0]
    async with _client(broken) as client:
        failed = (await whatsapp.send_message("hi", cfg, client, pause=0))[0]
    assert not blocked.ok and "sandbox network policy" in blocked.detail and "api.callmebot.com" in blocked.detail
    assert not failed.ok and failed.detail == "network error: ConnectError"
    assert "SECRETKEY" not in blocked.detail + failed.detail and "34600000000" not in failed.detail


# ───────────── orchestration ─────────────

def _fake_sources(monkeypatch, modules: dict) -> None:
    monkeypatch.setattr(engine, "load", lambda name: modules[name])


async def test_one_dead_source_never_sinks_the_rest(monkeypatch):
    async def good(city, start, end, client):
        return [event("Rooftop social"),
                event("Next week", day=20),                                     # outside the window
                DigestEvent(source="meetup", title="Naive", start=datetime(2026, 11, 12, 20), url="https://x.y")]

    async def blocked(city, start, end, client):
        raise CollectorError("HTTP 403 from the listing")

    async def crashes(city, start, end, client):
        return {}["boom"]

    async def hangs(city, start, end, client):
        await asyncio.sleep(5)

    async def empty(city, start, end, client):
        return []

    monkeypatch.setattr(engine, "COLLECTOR_TIMEOUT_SECS", 0.05)
    _fake_sources(monkeypatch, {name: SimpleNamespace(collect=fn) for name, fn in
                                [("meetup", good), ("ra", blocked), ("dice", crashes), ("xceed", hangs), ("luma", empty)]})
    events, statuses, posts, denied = await engine.collect_all(
        CITY, NOW, sources=["meetup", "ra", "dice", "xceed", "luma"], env={})

    assert [ev.title for ev in events] == ["Rooftop social"]
    assert statuses["meetup"]["ok"] and statuses["meetup"]["events"] == 1
    assert statuses["ra"] == dict(statuses["ra"], ok=False, error="HTTP 403 from the listing")
    assert statuses["dice"]["ok"] is False and statuses["dice"]["error"].startswith("KeyError")
    assert statuses["xceed"]["ok"] is False and "timed out" in statuses["xceed"]["error"]
    assert statuses["luma"]["ok"] is True and "0 events" in statuses["luma"]["warnings"][0]
    assert statuses["meetup"]["warnings"] == ["1 event(s) dropped for breaking the data contract"]
    assert posts == [] and denied == []


async def test_what_a_collector_logs_as_a_warning_marks_the_source_partial(monkeypatch):
    import logging

    async def partial(city, start, end, client):
        log = logging.getLogger("backend.digest.collectors.meetup")
        log.warning("meetup: Fri 13 Nov could not be fetched (%s) — that day is missing", "ReadTimeout")
        log.warning("meetup: Fri 13 Nov could not be fetched (%s) — that day is missing", "ReadTimeout")  # no repeats
        log.info("meetup: 1 events from 3 requests")                                                      # not a warning
        return [event("Rooftop social")]

    async def quiet(city, start, end, client):
        return [event("Club night", source="ra")]

    _fake_sources(monkeypatch, {"meetup": SimpleNamespace(collect=partial), "ra": SimpleNamespace(collect=quiet)})
    events, statuses, posts, denied = await engine.collect_all(CITY, NOW, sources=["meetup", "ra"], env={})
    assert statuses["meetup"]["warnings"] == ["meetup: Fri 13 Nov could not be fetched (ReadTimeout) — that day is missing"]
    assert statuses["ra"]["warnings"] == []
    payload = engine.build_payload(CITY, NOW, events, statuses, posts, denied)
    assert "Meetup 1 (partial) · RA 1" in render.render_message(payload, {"today": [], "tomorrow": []})
    # the handler is removed again, so nothing leaks into the next run
    assert not [h for h in logging.getLogger("backend.digest.collectors").handlers
                if isinstance(h, engine._CollectorWarnings)]


async def test_sandbox_denials_are_named_so_nobody_blames_the_source(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"x-deny-reason": "host_not_allowed"})

    async def fetches(city, start, end, client):
        response = await client.get("https://www.meetup.com/gql2")
        raise CollectorError("HTTP %d" % response.status_code)

    monkeypatch.setattr(engine, "make_client", lambda **kw: make_client(transport=httpx.MockTransport(handler), **kw))
    _fake_sources(monkeypatch, {"meetup": SimpleNamespace(collect=fetches)})
    events, statuses, posts, denied = await engine.collect_all(CITY, NOW, sources=["meetup"], env={})
    assert denied == ["www.meetup.com"] and statuses["meetup"]["error"] == "HTTP 403"
    payload = engine.build_payload(CITY, NOW, events, statuses, posts, denied)
    assert "Blocked by the sandbox network policy: www.meetup.com" in render.brief_text(payload)


async def test_instagram_is_skipped_without_a_token_and_filtered_with_one(monkeypatch):
    async def nothing(city, start, end, client):
        return [event("Rooftop social")]

    async def posts(city, client, token):
        assert token == "tok"
        return [{"handle": "sala_apolo", "caption": "Nitsa this Friday", "url": "https://instagram.com/p/1"},
                {"handle": "somewhere", "caption": "Fetish night this Friday", "url": "https://instagram.com/p/2"}]

    modules = {"meetup": SimpleNamespace(collect=nothing), "instagram": SimpleNamespace(collect_posts=posts)}
    _fake_sources(monkeypatch, modules)

    _, statuses, found, _ = await engine.collect_all(CITY, NOW, sources=None, env={})
    assert found == [] and statuses["instagram"]["skipped"] is True

    monkeypatch.setattr(engine, "REGISTRY", ("meetup",))
    _, statuses, found, _ = await engine.collect_all(CITY, NOW, env={engine.APIFY_TOKEN_ENV: "tok"})
    assert [p["handle"] for p in found] == ["sala_apolo"]
    assert statuses["instagram"] == dict(statuses["instagram"], ok=True, posts=1)


# ───────────── command line ─────────────

@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Run the CLI where there is no .env, with WhatsApp unset."""
    monkeypatch.chdir(tmp_path)
    for name in ("WHATSAPP_PROVIDER", "WHATSAPP_PHONE", "WHATSAPP_APIKEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _write_payload(workdir) -> dict:
    statuses = {"meetup": {"ok": True, "events": 2, "seconds": 1.0, "error": None}}
    payload = engine.build_payload(CITY, NOW, [event("Rooftop social", attending=40, attending_label="going"),
                                               event("Friday club", day=13, hour=23)], statuses)
    (workdir / "candidates.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


def test_cli_render_then_dry_run_send(workdir, capsys):
    payload = _write_payload(workdir)
    picks = {"today": [{"id": payload["today"]["candidates"][0]["id"], "why": "Most RSVPs tonight."}], "tomorrow": []}
    (workdir / "picks.json").write_text(json.dumps(picks), encoding="utf-8")

    assert cli.main(["render", "--candidates", "candidates.json", "--picks", "picks.json", "--out", "message.txt"]) == 0
    message = (workdir / "message.txt").read_text(encoding="utf-8")
    assert "*1. Rooftop social*" in message and "📊 40 going on Meetup" in message

    assert cli.main(["send", "--file", "message.txt", "--dry-run"]) == 0
    assert "DRY RUN — 1 message(s) would be sent" in capsys.readouterr().out


def test_cli_render_rejects_bad_picks_with_exit_code_1(workdir, capsys):
    _write_payload(workdir)
    (workdir / "picks.json").write_text(json.dumps({"today": [{"id": "meetup-00000000", "why": "x"}]}), encoding="utf-8")
    assert cli.main(["render", "--candidates", "candidates.json", "--picks", "picks.json"]) == 1
    assert "is not among the today candidates" in capsys.readouterr().err


def test_cli_send_without_configuration_exits_3_and_sends_nothing(workdir, capsys):
    (workdir / "message.txt").write_text("🎉 *Barcelona*\n\n*1. Rooftop social*", encoding="utf-8")
    assert cli.main(["send", "--file", "message.txt"]) == 3
    captured = capsys.readouterr()
    assert "WHATSAPP NOT CONFIGURED: missing WHATSAPP_PHONE and WHATSAPP_APIKEY" in captured.err
    # the digest still ends up in the run's output — the cloud sandbox is thrown away afterwards
    assert "----- THE MESSAGE THAT WAS NOT DELIVERED -----\n🎉 *Barcelona*\n\n*1. Rooftop social*\n----- END OF MESSAGE -----" in captured.out


def test_cli_send_failure_exits_4_and_still_prints_the_message(workdir, capsys, monkeypatch):
    monkeypatch.setenv("WHATSAPP_PHONE", "+34600000000")
    monkeypatch.setenv("WHATSAPP_APIKEY", "bad-key")

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ERROR: APIKey is invalid.")

    monkeypatch.setattr(cli, "make_client", lambda **kw: make_client(transport=httpx.MockTransport(refuse), **kw))
    (workdir / "message.txt").write_text("the digest", encoding="utf-8")
    assert cli.main(["send", "--file", "message.txt"]) == 4
    captured = capsys.readouterr()
    assert "WHATSAPP SEND FAILED after 0 of 1 message(s)." in captured.err
    assert "message 1/1: FAILED (HTTP 200) ERROR: APIKey is invalid." in captured.out
    assert "THE MESSAGE THAT WAS NOT DELIVERED -----\nthe digest\n" in captured.out
    assert "bad-key" not in captured.out + captured.err


def test_cli_send_success_reports_sent(workdir, capsys, monkeypatch):
    monkeypatch.setenv("WHATSAPP_PHONE", "+34600000000")
    monkeypatch.setenv("WHATSAPP_APIKEY", "good-key")
    monkeypatch.setattr(whatsapp, "PAUSE_BETWEEN_CHUNKS_SECS", 0)

    def accept(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Message queued. You will receive it in a few seconds.")

    monkeypatch.setattr(cli, "make_client", lambda **kw: make_client(transport=httpx.MockTransport(accept), **kw))
    (workdir / "message.txt").write_text("the digest", encoding="utf-8")
    assert cli.main(["send", "--file", "message.txt"]) == 0
    captured = capsys.readouterr()
    assert "WHATSAPP SENT: 1 message(s) accepted by callmebot." in captured.out
    assert "NOT DELIVERED" not in captured.out and "good-key" not in captured.out + captured.err


def test_cli_send_refuses_an_empty_message(workdir):
    (workdir / "message.txt").write_text("   \n", encoding="utf-8")
    assert cli.main(["send", "--file", "message.txt"]) == 1


def test_cli_reads_whatsapp_settings_from_a_local_dotenv_but_never_other_keys(workdir, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (workdir / ".env").write_text("WHATSAPP_PHONE=+34600000000\nWHATSAPP_APIKEY='abc'\nANTHROPIC_API_KEY=nope\n", encoding="utf-8")
    cli._load_dotenv()
    import os
    assert whatsapp.config_from_env().apikey == "abc"
    assert "ANTHROPIC_API_KEY" not in os.environ
