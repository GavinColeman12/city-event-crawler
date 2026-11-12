"""Command line for the city digest.

    python -m backend.digest collect --out candidates.json      # scrape + shortlist
    python -m backend.digest render --candidates candidates.json --picks picks.json --out message.txt
    python -m backend.digest send --file message.txt            # WhatsApp
    python -m backend.digest auto [--send]                      # all of it, heuristic picks, no model
    python -m backend.digest check                              # what is configured / reachable

Exit codes: 0 ok · 1 bad input · 2 nothing collected · 3 WhatsApp not configured · 4 WhatsApp send failed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import List, Optional

from backend.digest import engine, render, whatsapp
from backend.digest.cities import get_city
from backend.digest.collectors import REGISTRY
from backend.digest.http import make_client

EXIT_OK, EXIT_BAD_INPUT, EXIT_NOTHING, EXIT_NOT_CONFIGURED, EXIT_SEND_FAILED = 0, 1, 2, 3, 4


def _load_dotenv() -> None:
    """Local convenience: pick up WHATSAPP_* / Apify settings from a .env in the working directory.

    Only keys this package uses are read, and real environment variables win.
    """
    wanted = {"WHATSAPP_PROVIDER", "WHATSAPP_PHONE", "WHATSAPP_APIKEY", engine.APIFY_TOKEN_ENV}
    try:
        with open(".env", encoding="utf-8") as handle:
            for line in handle:
                key, sep, value = line.strip().partition("=")
                if sep and key in wanted and key not in os.environ:
                    os.environ[key] = value.strip().strip("'\"")
    except OSError:
        pass


def _parse_now(text: Optional[str], city) -> Optional[datetime]:
    if not text:
        return None
    moment = datetime.fromisoformat(text)
    return moment.replace(tzinfo=city.zone) if moment.tzinfo is None else moment


def _print_summary(payload: dict) -> None:
    print("%s digest candidates · generated %s" % (payload["city"]["name"], payload["generated_at"]))
    for name, status in payload["sources"].items():
        if status.get("ok"):
            count = status.get("events", status.get("posts", 0))
            unit = "posts" if name == "instagram" else "events"
            line = "  ok    %-11s %4d %s  (%.1fs)" % (name, count, unit, status["seconds"])
            for warning in status.get("warnings") or []:
                line += "\n          ⚠ PARTIAL: %s" % warning
        elif status.get("skipped"):
            line = "  skip  %-11s %s" % (name, status["error"])
        else:
            line = "  FAIL  %-11s %s" % (name, status["error"])
        print(line)
    if payload["blocked_hosts"]:
        print("  Blocked by the sandbox network policy: %s" % ", ".join(payload["blocked_hosts"]))
    totals = payload["totals"]
    print("  %d collected → %d after merging duplicates · %d removed by the content filter"
          % (totals["collected"], totals["after_dedupe"], totals["removed_by_content_filter"]))
    for day in ("today", "tomorrow"):
        block = payload[day]
        print("  %-8s %s: %d live events, %d shortlisted" % (day, block["label"], block["found"], len(block["candidates"])))


def _collect(args) -> dict:
    city = get_city(args.city)
    sources = [s.strip() for s in args.sources.split(",")] if args.sources else None
    return asyncio.run(engine.collect_payload(
        city, now=_parse_now(args.now, city), sources=sources,
        with_instagram=not args.no_instagram, max_per_day=args.max_per_day))


def _nothing_collected(payload: dict) -> bool:
    return not any(s.get("ok") and s.get("events") for n, s in payload["sources"].items() if n != "instagram")


def cmd_collect(args) -> int:
    payload = _collect(args)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    _print_summary(payload)
    print("Wrote %s" % args.out)
    if args.brief:
        with open(args.brief, "w", encoding="utf-8") as handle:
            handle.write(render.brief_text(payload))
        print("Wrote %s (the readable shortlist — pick from this)" % args.brief)
    return EXIT_NOTHING if _nothing_collected(payload) else EXIT_OK


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def cmd_render(args) -> int:
    payload, picks = _read_json(args.candidates), _read_json(args.picks)
    problems = render.validate_picks(payload, picks)
    if problems:
        print("The picks file needs fixing:", file=sys.stderr)
        for problem in problems:
            print("  - %s" % problem, file=sys.stderr)
        return EXIT_BAD_INPUT
    message = render.render_message(payload, picks)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(message + "\n")
        print("Wrote %s (%d characters, %d WhatsApp messages)"
              % (args.out, len(message), len(whatsapp.split_message(message))))
    else:
        print(message)
    return EXIT_OK


def _echo_undelivered(message: str) -> None:
    """Put an undelivered digest in the run's output, so the work is never lost with the sandbox."""
    print("----- THE MESSAGE THAT WAS NOT DELIVERED -----")
    print(message.strip())
    print("----- END OF MESSAGE -----")


def _send(message: str, dry_run: bool) -> int:
    if dry_run:
        chunks = whatsapp.split_message(message)
        print("DRY RUN — %d message(s) would be sent:" % len(chunks))
        for chunk in chunks:
            print("\n----- %d characters -----\n%s" % (len(chunk), chunk))
        return EXIT_OK
    try:
        cfg = whatsapp.config_from_env()
    except whatsapp.NotConfigured as exc:
        print("WHATSAPP NOT CONFIGURED: %s. Nothing was sent." % exc, file=sys.stderr)
        _echo_undelivered(message)
        return EXIT_NOT_CONFIGURED
    chunks = whatsapp.split_message(message, cfg.chunk_chars)

    async def _go() -> List[whatsapp.SendResult]:
        async with make_client() as client:
            return await whatsapp.send_message(message, cfg, client)

    results = asyncio.run(_go())
    for position, result in enumerate(results, 1):
        print("message %d/%d: %s (HTTP %s) %s"
              % (position, len(chunks), "sent" if result.ok else "FAILED", result.status_code, result.detail))
    if len(results) == len(chunks) and all(r.ok for r in results):
        print("WHATSAPP SENT: %d message(s) accepted by %s." % (len(chunks), cfg.provider))
        return EXIT_OK
    print("WHATSAPP SEND FAILED after %d of %d message(s)." % (sum(r.ok for r in results), len(chunks)), file=sys.stderr)
    _echo_undelivered(message)
    return EXIT_SEND_FAILED


def cmd_send(args) -> int:
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            message = handle.read()
    else:
        message = sys.stdin.read()
    if not message.strip():
        print("Nothing to send: the message is empty.", file=sys.stderr)
        return EXIT_BAD_INPUT
    return _send(message, args.dry_run)


def cmd_auto(args) -> int:
    payload = _collect(args)
    _print_summary(payload)
    if _nothing_collected(payload):
        return EXIT_NOTHING
    message = render.render_message(payload, render.auto_picks(payload))
    if args.send:
        return _send(message, dry_run=False)
    print("\n" + message)
    return EXIT_OK


def cmd_check(args) -> int:
    try:
        cfg = whatsapp.config_from_env()
        print("WhatsApp: configured (provider %s, number ending …%s)" % (cfg.provider, cfg.phone[-3:]))
    except whatsapp.NotConfigured as exc:
        print("WhatsApp: NOT configured — %s" % exc)
    print("Instagram: %s" % ("Apify token present" if os.environ.get(engine.APIFY_TOKEN_ENV)
                             else "not configured (%s is not set)" % engine.APIFY_TOKEN_ENV))
    provider = (os.environ.get("WHATSAPP_PROVIDER") or "callmebot").strip().lower()
    hosts = ["www.meetup.com", "ra.co", "dice.fm", "event-b2c.xceed.me", "offer-b2c.xceed.me",
             "www.eventbrite.com", "api.lu.ma",
             "api.textmebot.com" if provider == "textmebot" else "api.callmebot.com", "api.apify.com"]

    async def _probe_one(client, host: str) -> str:
        # Only the network path is being tested.  A site answering its front
        # page with 403 or 404 is still reachable — the collectors call other URLs.
        try:
            response = await client.get("https://%s/" % host)
        except Exception as exc:  # noqa: BLE001
            return "UNREACHABLE (%s)" % type(exc).__name__
        if response.headers.get("x-deny-reason"):
            return "BLOCKED by the sandbox network policy"
        return "open"

    async def _probe() -> List[str]:
        async with make_client(timeout=10.0) as client:
            return await asyncio.gather(*(_probe_one(client, host) for host in hosts))

    print("Network (open = the sandbox lets requests through; it says nothing about the site's own blocking):")
    for host, verdict in zip(hosts, asyncio.run(_probe())):
        print("  %-22s %s" % (host, verdict))
    return EXIT_OK


def _add_collect_options(parser) -> None:
    parser.add_argument("--city", default="barcelona")
    parser.add_argument("--sources", help="comma-separated subset of: %s, instagram" % ", ".join(REGISTRY))
    parser.add_argument("--no-instagram", action="store_true")
    parser.add_argument("--max-per-day", type=int, default=26, help="shortlist size per day (default 26)")
    parser.add_argument("--now", help="pretend it is this local time (ISO), for testing")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.digest", description=__doc__.split("\n")[0])
    parser.add_argument("-v", "--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="scrape every source and write the candidates file")
    _add_collect_options(collect)
    collect.add_argument("--out", required=True, help="candidates JSON (the source of truth for render)")
    collect.add_argument("--brief", help="also write the shortlist as compact readable text")
    collect.set_defaults(run=cmd_collect)

    rendering = commands.add_parser("render", help="candidates + picks → WhatsApp message text")
    rendering.add_argument("--candidates", required=True)
    rendering.add_argument("--picks", required=True)
    rendering.add_argument("--out")
    rendering.set_defaults(run=cmd_render)

    sending = commands.add_parser("send", help="send a message file (or stdin) to WhatsApp")
    sending.add_argument("--file")
    sending.add_argument("--dry-run", action="store_true", help="show the messages instead of sending")
    sending.set_defaults(run=cmd_send)

    auto = commands.add_parser("auto", help="collect, pick by heuristic, render (and --send)")
    _add_collect_options(auto)
    auto.add_argument("--send", action="store_true")
    auto.set_defaults(run=cmd_auto)

    check = commands.add_parser("check", help="show configuration and which hosts are reachable")
    check.set_defaults(run=cmd_check)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO; the WhatsApp URL carries the key.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _load_dotenv()
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
