"""Live-smoke runner shared by every collector.

    backend/.venv/bin/python -m backend.digest.collectors.meetup [city] [--json] [--limit N]

Hits the real source for the standard digest window, checks every event
against the contract, and prints a summary.  Exit code 0 = at least one valid
event and no contract violations.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from backend.digest.cities import collection_window, get_city
from backend.digest.http import make_client
from backend.digest.models import validate_event


def main(module) -> int:
    parser = argparse.ArgumentParser(description="Live smoke for the %s collector" % module.SOURCE)
    parser.add_argument("city", nargs="?", default="barcelona")
    parser.add_argument("--json", action="store_true", help="dump every event as JSON instead of a summary")
    parser.add_argument("--limit", type=int, default=8, help="events to show in the summary")
    args = parser.parse_args()

    city = get_city(args.city)
    start, end = collection_window(city)

    async def _run():
        async with make_client() as client:
            return await module.collect(city, start, end, client)

    started = time.time()
    try:
        events = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 — a smoke run should report, not traceback
        print("FAILED %s: %s: %s" % (module.SOURCE, type(exc).__name__, exc), file=sys.stderr)
        return 2
    elapsed = time.time() - started

    violations = 0
    for ev in events:
        try:
            validate_event(ev)
            if not (start <= ev.start < end):
                raise ValueError("%r starts %s, outside the window" % (ev.title, ev.start.isoformat()))
        except ValueError as exc:
            violations += 1
            print("CONTRACT VIOLATION: %s" % exc, file=sys.stderr)

    if args.json:
        print(json.dumps([ev.to_dict() for ev in events], ensure_ascii=False, indent=2))
        return 0 if events and not violations else 1

    with_stat = [ev for ev in events if ev.attending is not None]
    print("%s · %s · window %s → %s" % (module.SOURCE, city.name, start.isoformat(), end.isoformat()))
    print("%d events in %.1fs · %d with an attendance stat · %d contract violations"
          % (len(events), elapsed, len(with_stat), violations))
    ranked = sorted(events, key=lambda ev: (-(ev.attending or 0), ev.start))
    for ev in ranked[: args.limit]:
        stat = "%s %s" % (ev.attending, ev.attending_label) if ev.attending is not None else "no stat"
        print("  %s | %s | %s | %s | %s" % (
            ev.start.strftime("%a %d %H:%M"), ev.title[:60], ev.venue or "?", ev.price or "?", stat))
        print("      %s" % ev.url)
    return 0 if events and not violations else 1
