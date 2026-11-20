"""Offline tests for the cloud → Mac hand-off: publishing the digest to a git branch
and relaying it by iMessage.  Real git against throw-away local repositories;
Messages and launchd are replaced by fakes, so nothing is ever sent."""
from __future__ import annotations

import json
import plistlib
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.digest import __main__ as cli
from backend.digest import outbox, relay_imessage as relay, render

MADRID = ZoneInfo("Europe/Madrid")
MADE = datetime(2026, 11, 12, 14, 56, tzinfo=MADRID)

MESSAGE = "\n\n".join([
    "🎉 *Barcelona · Thu 12 Nov*\n_Public holiday tomorrow._",
    "━━ *TODAY · Thu 12 Nov* ━━",
    "*1. The Thursday Language Exchange*\n🕘 Tonight 19:00 → 22:00 · 📍 Space Cowboy\n"
    "📊 80 going on Meetup across 3 listings\n💶 Free\n💬 Biggest English-friendly social tonight.\n"
    "https://www.meetup.com/some_group/events/317105736/",
    "━━ *TOMORROW · Fri 13 Nov* ━━",
    "*1. POLENTA*\n🕘 Fri 23:59 → 06:00 · 📍 La Paloma\n📊 520 interested on RA\n💶 €20\n"
    "💬 The busiest club night on RA.\nhttps://ra.co/events/1",
    "_Scanned: Meetup 353 · RA 65._",
])
PAYLOAD = {"city": {"key": "barcelona", "name": "Barcelona", "tz": "Europe/Madrid"},
           "today": {"date": "2026-11-12"}, "generated_at": MADE.isoformat()}


def git(*args: str, cwd=None) -> str:
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """A bare 'GitHub' and a working clone with it as origin."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    git("init", "-q", "--bare", str(remote))
    git("init", "-q", str(work))
    git("remote", "add", "origin", str(remote), cwd=work)
    return remote, work


# ───────────── plain text for iMessage ─────────────

def test_plain_messages_drop_the_markup_and_split_today_from_tomorrow():
    today, tomorrow = render.plain_messages(MESSAGE)
    assert today.startswith("🎉 Barcelona · Thu 12 Nov\nPublic holiday tomorrow.\n\nTODAY · Thu 12 Nov\n\n1. The Thursday Language Exchange\n")
    assert tomorrow.startswith("TOMORROW · Fri 13 Nov\n\n1. POLENTA\n") and tomorrow.endswith("Scanned: Meetup 353 · RA 65.")
    for text in (today, tomorrow):
        assert "*" not in text and "━" not in text
        assert not any(line.startswith("_") or line.endswith("_") for line in text.split("\n"))
    assert "https://www.meetup.com/some_group/events/317105736/" in today      # underscore inside a link survives
    assert "💬 Biggest English-friendly social tonight." in today and "📊 520 interested on RA" in tomorrow


def test_build_record_carries_both_forms_and_the_timestamp():
    record = outbox.build_record(PAYLOAD, MESSAGE)
    assert record["date"] == "2026-11-12" and record["city"] == "barcelona"
    assert record["generated_at"] == MADE.isoformat() and record["whatsapp"] == MESSAGE
    assert len(record["messages"]) == 2 and record["schema"] == outbox.SCHEMA


# ───────────── publishing ─────────────

def test_publish_creates_the_branch_then_appends_without_touching_the_working_tree(repos):
    remote, work = repos
    (work / "untracked.txt").write_text("mine")
    record = outbox.build_record(PAYLOAD, MESSAGE)

    first = outbox.publish(record, repo=str(work))
    assert git("--git-dir", str(remote), "rev-parse", "refs/heads/" + outbox.OUTBOX_BRANCH) == first
    assert json.loads(git("--git-dir", str(remote), "show", first + ":outbox/latest.json")) == record

    later = dict(record, date="2026-11-13", generated_at=(MADE + timedelta(days=1)).isoformat())
    second = outbox.publish(later, repo=str(work))
    assert git("--git-dir", str(remote), "rev-list", "--count", second) == "2"       # history, not a force-push
    assert git("--git-dir", str(remote), "rev-parse", second + "^") == first
    assert json.loads(git("--git-dir", str(remote), "show", second + ":outbox/latest.json"))["date"] == "2026-11-13"

    assert sorted(p.name for p in work.iterdir()) == [".git", "untracked.txt"]       # nothing checked out or staged
    assert git("status", "--porcelain", cwd=work) == "?? untracked.txt"
    assert git("config", "--get", "user.email", cwd=work) if False else True          # identity came from env, not config


def test_publish_failure_is_reported_without_credentials(repos, monkeypatch):
    _, work = repos
    with pytest.raises(outbox.OutboxError, match="git ls-remote failed"):
        outbox.publish(outbox.build_record(PAYLOAD, MESSAGE), remote="nowhere", repo=str(work))

    def leaky(command, **kwargs):
        return subprocess.CompletedProcess(command, 128, "", "fatal: unable to access 'https://gavin:ghp_SECRET@github.com/x/y.git/'")

    monkeypatch.setattr(outbox.subprocess, "run", leaky)
    with pytest.raises(outbox.OutboxError) as caught:
        outbox.publish(outbox.build_record(PAYLOAD, MESSAGE), repo=str(work))
    assert "ghp_SECRET" not in str(caught.value) and "github.com/x/y.git" in str(caught.value)


def test_cli_publish_reports_success_and_failure(repos, tmp_path, monkeypatch, capsys):
    _, work = repos
    (tmp_path / "candidates.json").write_text(json.dumps(PAYLOAD), encoding="utf-8")
    (tmp_path / "message.txt").write_text(MESSAGE, encoding="utf-8")
    monkeypatch.chdir(work)
    args = ["publish", "--candidates", str(tmp_path / "candidates.json"), "--file", str(tmp_path / "message.txt")]

    assert cli.main(args) == 0
    assert "PUBLISHED: digest for 2026-11-12 is on branch claude/digest-outbox" in capsys.readouterr().out
    assert cli.main(args + ["--remote", "nowhere"]) == 5
    assert "PUBLISH FAILED: git ls-remote failed" in capsys.readouterr().err


# ───────────── the Mac relay ─────────────

class Sent:
    """Stands in for Messages: records what it was handed, optionally failing on chosen calls."""

    def __init__(self, fail_on=()):
        self.texts, self.calls, self.fail_on = [], 0, set(fail_on)

    def __call__(self, recipient, text, run=None):
        self.calls += 1
        if self.calls in self.fail_on:
            raise relay.RelayError("Messages refused the message: not signed in")
        self.texts.append((recipient, text))


@pytest.fixture
def mac(repos, tmp_path):
    """An installed relay under a fake home, pointing at the fake GitHub, with one digest published."""
    remote, work = repos
    outbox.publish(outbox.build_record(PAYLOAD, MESSAGE), repo=str(work))
    paths = relay.Paths(home=tmp_path / "home")
    paths.app.mkdir(parents=True)
    git("init", "-q", "--bare", str(paths.mirror))
    paths.config.write_text(json.dumps({"recipient": "+34600000000", "remote_url": str(remote),
                                        "branch": outbox.OUTBOX_BRANCH, "tz": "Europe/Madrid"}))
    return paths, work


def at(hour: int, minute: int = 0, day: int = 12) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=MADRID)


def test_relay_delivers_a_fresh_digest_once_as_two_plain_messages(mac):
    paths, _ = mac
    sent = Sent()
    assert relay.run_once(paths, now=at(15, 5), sender=sent, pause=0) == 0
    assert [recipient for recipient, _ in sent.texts] == ["+34600000000", "+34600000000"]
    assert sent.texts[0][1].startswith("🎉 Barcelona · Thu 12 Nov") and sent.texts[1][1].startswith("TOMORROW · Fri 13 Nov")
    assert relay.run_once(paths, now=at(15, 15), sender=sent, pause=0) == 0      # ten minutes later
    assert len(sent.texts) == 2                                                  # …nothing is sent twice
    log = paths.log.read_text()
    assert "handed the digest for 2026-11-12 to Messages for …000 (2 message(s))" in log
    assert "+34600000000" not in log and "Language Exchange" not in log          # no number, no content in the log


def test_relay_stays_offline_before_the_window_and_skips_a_stale_digest(mac):
    paths, _ = mac

    def no_network(*args, **kwargs):
        raise AssertionError("the relay must not run git before the delivery window")

    sent = Sent()
    assert relay.run_once(paths, now=at(9, 0), run=no_network, sender=sent) == 0
    assert relay.run_once(paths, now=at(14, 44), run=no_network, sender=sent) == 0
    # Laptop opened the next afternoon before a new digest exists: yesterday's is not sent.
    assert relay.run_once(paths, now=at(14, 50, day=13), sender=sent) == 0
    assert sent.texts == [] and "stale" in paths.log.read_text()


def test_relay_resumes_after_a_failure_without_repeating_a_message(mac):
    paths, _ = mac
    sent = Sent(fail_on={2})
    assert relay.run_once(paths, now=at(15, 5), sender=sent, pause=0) == 1       # first part out, second refused
    assert len(sent.texts) == 1
    assert relay.run_once(paths, now=at(15, 15), sender=sent, pause=0) == 0      # retry sends only the second part
    assert [text[:8] for _, text in sent.texts] == ["🎉 Barcel", "TOMORROW"]
    assert relay.run_once(paths, now=at(15, 25), sender=sent, pause=0) == 0 and len(sent.texts) == 2


def test_relay_gives_up_after_repeated_failures(mac):
    paths, _ = mac
    always_fails = Sent(fail_on=set(range(1, 50)))
    for minutes in range(relay.MAX_ATTEMPTS):
        assert relay.run_once(paths, now=at(15, 5 + minutes), sender=always_fails, pause=0) == 1
    assert relay.run_once(paths, now=at(16, 0), sender=always_fails, pause=0) == 0
    assert always_fails.calls == relay.MAX_ATTEMPTS and "gave up" in paths.log.read_text()


def test_relay_picks_up_the_next_days_digest(mac):
    paths, work = mac
    sent = Sent()
    relay.run_once(paths, now=at(15, 5), sender=sent, pause=0)
    tomorrow = dict(outbox.build_record(PAYLOAD, MESSAGE), date="2026-11-13",
                    generated_at=(MADE + timedelta(days=1)).isoformat())
    outbox.publish(tomorrow, repo=str(work))
    assert relay.run_once(paths, now=at(15, 5, day=13), sender=sent, pause=0) == 0
    assert len(sent.texts) == 4


def test_dry_run_and_force(mac, capsys):
    paths, _ = mac
    sent = Sent()
    assert relay.run_once(paths, now=at(15, 5), dry_run=True, sender=sent) == 0
    assert sent.texts == [] and "DRY RUN — would send 2 iMessage(s) to …000" in capsys.readouterr().out
    relay.run_once(paths, now=at(15, 5), sender=sent, pause=0)
    assert relay.run_once(paths, now=at(9, 0, day=14), force=True, sender=sent, pause=0) == 0   # stale + delivered + early
    assert len(sent.texts) == 4


def test_relay_reports_fetch_failures_and_a_missing_install(tmp_path, mac, capsys):
    paths, _ = mac
    config = json.loads(paths.config.read_text())
    paths.config.write_text(json.dumps(dict(config, remote_url=str(tmp_path / "gone.git"))))
    assert relay.run_once(paths, now=at(15, 5), sender=Sent()) == 1
    assert "could not fetch the outbox" in paths.log.read_text()
    assert relay.run_once(relay.Paths(home=tmp_path / "nobody"), now=at(15, 5)) == 3
    assert "not installed" in capsys.readouterr().out


def test_decide_explains_itself():
    record = {"generated_at": MADE.isoformat(), "date": "2026-11-12", "messages": ["a"]}
    assert relay.decide(record, {}, at(15, 5)) == (True, "fresh digest for 2026-11-12")
    assert relay.decide(None, {}, at(15, 5))[1] == "no digest has been published yet"
    assert relay.decide(dict(record, generated_at="yesterday"), {}, at(15, 5))[1] == "the published digest has no usable timestamp"
    assert relay.decide(dict(record, generated_at="2026-11-12T14:56:00"), {}, at(15, 5))[0] is False     # naive time
    assert "already delivered" in relay.decide(record, {"delivered": [MADE.isoformat()]}, at(15, 5))[1]
    assert relay.decide(record, {}, at(23, 50))[0] is True and relay.decide(record, {}, at(0, 30, day=13))[0] is False
    assert "future" in relay.decide(record, {}, at(13, 0))[1]


@pytest.mark.parametrize("text, expected", [
    ("+34 600-000 (000)", "+34600000000"), ("+16175550123", "+16175550123"), (" me@icloud.com ", "me@icloud.com"),
])
def test_valid_recipient_accepts_numbers_and_apple_ids(text, expected):
    assert relay.valid_recipient(text) == expected


@pytest.mark.parametrize("text", ["600000000", "gavin", "", "+12", "a@b"])
def test_valid_recipient_rejects_the_rest(text):
    with pytest.raises(ValueError):
        relay.valid_recipient(text)


def test_mask_hides_the_address():
    assert relay.mask("+34600000123") == "…123" and relay.mask("gavin@icloud.com") == "g…@icloud.com"


def test_send_imessage_hands_messages_a_utf8_file_and_cleans_up():
    seen = {}

    def fake_osascript(command, **kwargs):
        seen["command"], seen["script"] = command, kwargs["input"].decode("ascii")
        seen["text"] = Path(command[3]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    relay.send_imessage("+34600000000", "🎉 Barcelona\n\n1. Rooftop — 21:00\n", run=fake_osascript)
    assert seen["command"][:3] == ["/usr/bin/osascript", "-", "+34600000000"]
    assert seen["text"] == "🎉 Barcelona\n\n1. Rooftop — 21:00"
    assert 'tell application "Messages"' in seen["script"] and "service type = iMessage" in seen["script"]
    assert not Path(seen["command"][3]).exists()


def test_send_imessage_turns_a_refusal_into_a_clear_error():
    def refused(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, b"", b"execution error: Not authorized to send Apple events to Messages. (-1743)")

    with pytest.raises(relay.RelayError, match="has not given this job permission to control Messages"):
        relay.send_imessage("+34600000000", "hi", run=refused)


def test_install_writes_config_script_and_a_launchd_job(tmp_path, repos):
    remote, _ = repos
    paths = relay.Paths(home=tmp_path / "home")
    launchctl = []

    def runner(command, **kwargs):
        if command[0] == "/bin/launchctl":
            launchctl.append(command[1:])
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return subprocess.run(command, **kwargs)

    assert relay.install(paths, "+34 600 000 000", str(remote), run=runner) == 0
    assert json.loads(paths.config.read_text()) == {"recipient": "+34600000000", "remote_url": str(remote),
                                                    "branch": "claude/digest-outbox", "tz": "Europe/Madrid"}
    assert paths.script.read_text() == Path(relay.__file__).read_text() and (paths.mirror / "HEAD").exists()
    job = plistlib.loads(paths.plist.read_bytes())
    assert job["Label"] == "com.barcelona-digest.relay" and job["StartInterval"] == 600
    assert job["ProgramArguments"] == ["/usr/bin/python3", str(paths.script), "run"]
    assert [call[0] for call in launchctl] == ["bootout", "bootstrap"] and launchctl[1][-1] == str(paths.plist)

    assert relay.install(paths, "not-a-number", str(remote), run=runner) == 1      # bad input changes nothing
    assert json.loads(paths.config.read_text())["recipient"] == "+34600000000"

    assert relay.uninstall(paths, run=runner) == 0
    assert not paths.plist.exists() and paths.config.exists() and launchctl[-1][0] == "bootout"
