"""Digest collectors — one module per event source.

THE CONTRACT (every module in this package must follow it)
----------------------------------------------------------
Each collector module exposes exactly this public surface::

    SOURCE = "meetup"                     # short id, equals the module name
    CITY_PARAMS = {"barcelona": {...}}    # whatever this source needs per city (slug, area id, ...)

    async def collect(city, start, end, client) -> list[DigestEvent]

* ``city``   — ``backend.digest.cities.City`` (key, name, tz, lat, lon).
* ``start``/``end`` — timezone-aware datetimes in the city's timezone.  Return
  only events whose START falls inside ``[start, end)``.
* ``client`` — a shared ``httpx.AsyncClient`` from ``backend.digest.http.make_client``
  (browser-like headers, redirects on, 25 s timeout).  Add per-request headers
  if the source needs them; do not create another client.

Rules:

1. Facts only.  Fill ``DigestEvent`` with what the source actually publishes.
   Never invent a number: a stat the source does not expose stays ``None``.
2. ``start`` (and ``end`` when set) must be timezone-aware, converted to the
   city's timezone.
3. Keep fetching and parsing separate: pure ``parse_*`` functions that take the
   already-downloaded payload, so tests run offline against a saved fixture.
4. Total failure (blocked, HTTP 4xx/5xx on the listing, unparseable payload)
   → raise ``CollectorError`` with a short human-readable reason.  A partial
   failure (one page or one detail request of several) is logged and skipped.
   The orchestrator catches errors per source, so one dead source never kills
   the digest.
5. A city missing from ``CITY_PARAMS`` → raise ``CollectorError``.
6. Be polite: bounded concurrency (≤ 6 parallel requests), no retry storms,
   stop paginating once past ``end``.
7. Python 3.9 compatible (``from __future__ import annotations``, ``zoneinfo``).
   No dependency beyond the stdlib and ``httpx``.

Each module ends with::

    if __name__ == "__main__":
        from backend.digest.collectors._smoke import main
        raise SystemExit(main(__import__("sys").modules[__name__]))

so ``python -m backend.digest.collectors.<name>`` runs a live smoke.
"""
from __future__ import annotations

import importlib
from types import ModuleType

# Order here is only the order sources are listed in reports.
REGISTRY = ("meetup", "ra", "dice", "xceed", "eventbrite", "luma")


class CollectorError(Exception):
    """A source could not be collected at all.  The message is shown to the reader."""


def load(name: str) -> ModuleType:
    return importlib.import_module("backend.digest.collectors.%s" % name)
