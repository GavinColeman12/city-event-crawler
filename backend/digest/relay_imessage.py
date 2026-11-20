#!/usr/bin/env python3
"""Mac-side relay: deliver the day's digest by iMessage.

The cloud run cannot send an iMessage — only the Messages app on a Mac can — so
it publishes each finished digest to the ``claude/digest-outbox`` branch (see
outbox.py).  This script fetches that branch and, when it holds a digest that is
fresh and has not been delivered yet, hands it to Messages.  launchd runs it
every ten minutes; before the delivery window it exits without touching the
network, and each digest is delivered exactly once.

Standalone on purpose — standard library only, nothing imported from this
package — so the installed copy keeps working whatever happens to the checkout.

    python3 relay_imessage.py install --recipient +34600000000 --remote-url <git url>
    python3 relay_imessage.py run [--dry-run] [--force]
    python3 relay_imessage.py status
    python3 relay_imessage.py uninstall

Installed files (all under the user's own Library, nothing system-wide):
    ~/Library/Application Support/BarcelonaDigest/   script copy, config, state, a tiny git mirror
    ~/Library/LaunchAgents/com.barcelona-digest.relay.plist
    ~/Library/Logs/BarcelonaDigest/relay.log
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from zoneinfo import ZoneInfo

LABEL = "com.barcelona-digest.relay"
OUTBOX_BRANCH = "claude/digest-outbox"
OUTBOX_PATH = "outbox/latest.json"
LOCAL_REF = "refs/heads/outbox"
DEFAULT_TZ = "Europe/Madrid"

CHECK_EVERY_SECS = 600
WINDOW_OPENS = (14, 45)           # in the digest's own timezone; the cloud run starts at 14:55
MAX_AGE = timedelta(hours=9)      # a 3 pm digest is still useful at 11 pm, not the next morning
MAX_ATTEMPTS = 5
PAUSE_BETWEEN_MESSAGES_SECS = 1.5

GIT = "/usr/bin/git"              # absolute: launchd gives jobs a minimal PATH
OSASCRIPT = "/usr/bin/osascript"
PYTHON = "/usr/bin/python3"

# Kept ASCII-only so it reads the same whatever encoding osascript assumes.
# The text travels in a file because it is long, multi-line and full of emoji.
_SEND_SCRIPT = """
on run argv
    set theRecipient to item 1 of argv
    set theText to do shell script "/bin/cat " & quoted form of (item 2 of argv) without altering line endings
    tell application "Messages"
        set theAccount to 1st account whose service type = iMessage
        send theText to participant theRecipient of theAccount
    end tell
end run
"""

Runner = Callable[..., "subprocess.CompletedProcess"]


class RelayError(Exception):
    """Something the relay could not do.  The message is safe to log."""


class Paths:
    def __init__(self, home: Optional[Path] = None) -> None:
        home = Path(home) if home else Path.home()
        self.app = home / "Library" / "Application Support" / "BarcelonaDigest"
        self.config = self.app / "config.json"
        self.state = self.app / "state.json"
        self.script = self.app / "relay_imessage.py"
        self.mirror = self.app / "outbox.git"
        self.logs = home / "Library" / "Logs" / "BarcelonaDigest"
        self.log = self.logs / "relay.log"
        self.plist = home / "Library" / "LaunchAgents" / (LABEL + ".plist")


# ───────────────────────── small helpers ─────────────────────────

def _load(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(default)


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".tmp")
    scratch.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    scratch.replace(path)


def _log(paths: Paths, text: str) -> None:
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text)
    print(line)
    try:
        paths.logs.mkdir(parents=True, exist_ok=True)
        with paths.log.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def valid_recipient(text: str) -> str:
    """An iMessage address: a phone number in international format, or an Apple ID email."""
    value = (text or "").strip()
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        return value
    digits = re.sub(r"[\s().-]", "", value)
    if re.fullmatch(r"\+\d{8,15}", digits):
        return digits
    raise ValueError("recipient must be a phone number with country code (e.g. +34600000000) or an Apple ID email")


def mask(recipient: str) -> str:
    """Enough to recognise the address in a log, not enough to read it."""
    if "@" in recipient:
        name, _, domain = recipient.partition("@")
        return "%s…@%s" % (name[:1], domain)
    return "…%s" % recipient[-3:]


def _git(run: Runner, *args: str, git_dir: Optional[Path] = None) -> str:
    command = [GIT] + (["--git-dir", str(git_dir)] if git_dir else []) + list(args)
    try:
        done = run(command, capture_output=True, timeout=90,
                   env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))   # never hang on a credential prompt
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RelayError("git %s failed: %s" % (args[0], type(exc).__name__))
    out = (done.stdout or b"").decode("utf-8", "replace")
    if done.returncode != 0:
        err = (done.stderr or b"").decode("utf-8", "replace").strip() or out.strip()
        err = re.sub(r"://[^/@\s]+@", "://", err)[-300:]            # no embedded credentials in a log
        raise RelayError("git %s failed: %s" % (args[0], err or "exit %d" % done.returncode))
    return out


# ───────────────────────── fetch / decide / send ─────────────────────────

def fetch_record(paths: Paths, cfg: dict, run: Runner = subprocess.run) -> Optional[dict]:
    """The latest published digest, or None when nothing has been published yet."""
    branch = cfg.get("branch", OUTBOX_BRANCH)
    if not _git(run, "ls-remote", "--heads", cfg["remote_url"], branch).strip():
        return None
    _git(run, "fetch", "--quiet", "--depth", "1", cfg["remote_url"],
         "+refs/heads/%s:%s" % (branch, LOCAL_REF), git_dir=paths.mirror)
    raw = _git(run, "show", "%s:%s" % (LOCAL_REF, OUTBOX_PATH), git_dir=paths.mirror)
    try:
        record = json.loads(raw)
    except ValueError:
        raise RelayError("the published digest is not valid JSON")
    if not isinstance(record, dict):
        raise RelayError("the published digest has an unexpected shape")
    return record


def decide(record: Optional[dict], state: dict, now: datetime) -> Tuple[bool, str]:
    """Should this digest be delivered now?  Returns (yes/no, the reason in plain words)."""
    if not record or not record.get("messages"):
        return False, "no digest has been published yet"
    key = str(record.get("generated_at") or "")
    try:
        made = datetime.fromisoformat(key)
    except ValueError:
        made = None
    if made is None or made.utcoffset() is None:
        return False, "the published digest has no usable timestamp"
    if key in state.get("delivered", []):
        return False, "the digest for %s was already delivered" % record.get("date")
    age = now - made
    if age > MAX_AGE:
        return False, "the digest for %s is stale (%d hours old)" % (record.get("date"), age.total_seconds() // 3600)
    if age < -timedelta(minutes=10):
        return False, "the published digest is dated in the future"
    if state.get("attempts", {}).get(key, 0) >= MAX_ATTEMPTS:
        return False, "gave up on the digest for %s after %d failed attempts" % (record.get("date"), MAX_ATTEMPTS)
    return True, "fresh digest for %s" % record.get("date")


def send_imessage(recipient: str, text: str, run: Runner = subprocess.run) -> None:
    """Hand one message to the Messages app.  Raises RelayError if Messages refuses it.

    A clean return means Messages accepted the message, not that it was
    delivered — Messages reports delivery only in its own window.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as handle:
        handle.write(text.strip())
        path = handle.name
    try:
        done = run([OSASCRIPT, "-", recipient, path], input=_SEND_SCRIPT.encode("ascii"),
                   capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RelayError("could not run Messages: %s" % type(exc).__name__)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if done.returncode != 0:
        detail = (done.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        if "-1743" in detail:
            detail += " — macOS has not given this job permission to control Messages"
        raise RelayError("Messages refused the message: %s" % (detail or "exit %d" % done.returncode))


def run_once(paths: Paths, now: Optional[datetime] = None, dry_run: bool = False, force: bool = False,
             run: Runner = subprocess.run, sender: Callable[..., None] = send_imessage,
             pause: float = PAUSE_BETWEEN_MESSAGES_SECS) -> int:
    cfg = _load(paths.config, {})
    if not cfg.get("recipient") or not cfg.get("remote_url"):
        print("The relay is not installed. Run: relay_imessage.py install --recipient <number> --remote-url <git url>")
        return 3
    now = now or datetime.now(ZoneInfo(cfg.get("tz", DEFAULT_TZ)))
    if not force and (now.hour, now.minute) < WINDOW_OPENS:
        return 0    # too early for today's digest to exist: stay silent and offline

    state = _load(paths.state, {})
    try:
        record = fetch_record(paths, cfg, run)
    except RelayError as exc:
        _note(paths, state, "could not fetch the outbox: %s" % exc)
        return 1

    wanted, reason = decide(record, state, now)
    if force and record and record.get("messages"):
        wanted, reason = True, "forced"
    if not wanted:
        _note(paths, state, reason)
        return 0

    key = str(record["generated_at"])
    messages: List[str] = [str(m) for m in record["messages"] if str(m).strip()]
    if dry_run:
        print("DRY RUN — would send %d iMessage(s) to %s (%s):" % (len(messages), mask(cfg["recipient"]), reason))
        for text in messages:
            print("\n----- %d characters -----\n%s" % (len(text), text))
        return 0

    # Count the attempt before sending, so a crash mid-send cannot loop forever,
    # and remember how far we got, so a retry never repeats a message.
    state.setdefault("attempts", {})[key] = state.get("attempts", {}).get(key, 0) + 1
    sent_so_far = 0 if force else state.get("progress", {}).get(key, 0)
    _save(paths.state, state)
    try:
        for index in range(sent_so_far, len(messages)):
            if index > sent_so_far:
                time.sleep(pause)
            sender(cfg["recipient"], messages[index], run)
            state.setdefault("progress", {})[key] = index + 1
            _save(paths.state, state)
    except RelayError as exc:
        _note(paths, state, "sending failed (attempt %d of %d): %s" % (state["attempts"][key], MAX_ATTEMPTS, exc), always=True)
        return 1

    state["delivered"] = (state.get("delivered", []) + [key])[-30:]
    state["attempts"] = {key: state["attempts"][key]}
    state["progress"] = {}
    _note(paths, state, "handed the digest for %s to Messages for %s (%d message(s))"
          % (record.get("date"), mask(cfg["recipient"]), len(messages)), always=True)
    return 0


def _note(paths: Paths, state: dict, text: str, always: bool = False) -> None:
    """Log a line — but not the same line every ten minutes."""
    if always or state.get("last_note") != text:
        _log(paths, text)
    state["last_note"] = text
    _save(paths.state, state)


# ───────────────────────── install / status ─────────────────────────

def _launchctl(run: Runner, *args: str) -> "subprocess.CompletedProcess":
    return run(["/bin/launchctl"] + list(args), capture_output=True, timeout=30)


def install(paths: Paths, recipient: str, remote_url: str, tz: str = DEFAULT_TZ,
            run: Runner = subprocess.run, source: Optional[Path] = None) -> int:
    try:
        recipient = valid_recipient(recipient)
        ZoneInfo(tz)
    except (ValueError, KeyError) as exc:
        print("Cannot install: %s" % exc)
        return 1
    paths.app.mkdir(parents=True, exist_ok=True)
    paths.logs.mkdir(parents=True, exist_ok=True)
    source = Path(source or __file__).resolve()
    if source != paths.script.resolve():
        shutil.copyfile(str(source), str(paths.script))
    if not paths.mirror.exists():
        _git(run, "init", "--quiet", "--bare", str(paths.mirror))
    _save(paths.config, {"recipient": recipient, "remote_url": remote_url, "branch": OUTBOX_BRANCH, "tz": tz})

    paths.plist.parent.mkdir(parents=True, exist_ok=True)
    with paths.plist.open("wb") as handle:
        plistlib.dump({
            "Label": LABEL,
            "ProgramArguments": [PYTHON, str(paths.script), "run"],
            "StartInterval": CHECK_EVERY_SECS,   # after sleep, launchd runs the missed check once on wake
            "RunAtLoad": True,
            "ProcessType": "Background",
            "StandardOutPath": str(paths.logs / "launchd.out.log"),
            "StandardErrorPath": str(paths.logs / "launchd.err.log"),
        }, handle)

    domain = "gui/%d" % os.getuid()
    _launchctl(run, "bootout", "%s/%s" % (domain, LABEL))          # replace an earlier install; fine if absent
    started = _launchctl(run, "bootstrap", domain, str(paths.plist))
    if started.returncode != 0:
        print("Installed the files, but launchd would not start the job: %s"
              % (started.stderr or b"").decode("utf-8", "replace").strip())
        return 1
    print("Installed. Every %d minutes from %02d:%02d (%s) the relay checks for a new digest and sends it to %s."
          % (CHECK_EVERY_SECS // 60, WINDOW_OPENS[0], WINDOW_OPENS[1], tz, mask(recipient)))
    return 0


def uninstall(paths: Paths, run: Runner = subprocess.run) -> int:
    _launchctl(run, "bootout", "gui/%d/%s" % (os.getuid(), LABEL))
    try:
        paths.plist.unlink()
    except OSError:
        pass
    print("Stopped and removed the launchd job. Config, state and logs were left in:\n  %s\n  %s" % (paths.app, paths.logs))
    return 0


def status(paths: Paths, run: Runner = subprocess.run) -> int:
    cfg = _load(paths.config, {})
    state = _load(paths.state, {})
    loaded = _launchctl(run, "print", "gui/%d/%s" % (os.getuid(), LABEL)).returncode == 0
    print("launchd job:   %s" % ("loaded" if loaded else "NOT loaded"))
    print("recipient:     %s" % (mask(cfg["recipient"]) if cfg.get("recipient") else "not set"))
    print("last note:     %s" % state.get("last_note", "—"))
    print("delivered:     %d digest(s), latest %s" % (len(state.get("delivered", [])), (state.get("delivered") or ["—"])[-1]))
    print("log:           %s" % paths.log)
    return 0 if loaded and cfg.get("recipient") else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Deliver the daily digest by iMessage from this Mac.")
    commands = parser.add_subparsers(dest="command", required=True)
    running = commands.add_parser("run", help="one pass: fetch, decide, send")
    running.add_argument("--dry-run", action="store_true", help="print what would be sent instead of sending")
    running.add_argument("--force", action="store_true", help="send the latest digest even if stale or already delivered")
    setup = commands.add_parser("install", help="write the config, copy this script, start the launchd job")
    setup.add_argument("--recipient", required=True, help="your iMessage phone number (+countrycode…) or Apple ID email")
    setup.add_argument("--remote-url", required=True, help="git URL of the repository holding the outbox branch")
    setup.add_argument("--tz", default=DEFAULT_TZ)
    commands.add_parser("status", help="is it installed, and what did it last do")
    commands.add_parser("uninstall", help="stop and remove the launchd job")
    args = parser.parse_args(argv)

    paths = Paths()
    if args.command == "run":
        return run_once(paths, dry_run=args.dry_run, force=args.force)
    if args.command == "install":
        return install(paths, args.recipient, args.remote_url, args.tz)
    if args.command == "status":
        return status(paths)
    return uninstall(paths)


if __name__ == "__main__":
    raise SystemExit(main())
