"""Cities the digest knows about, and the time window a digest run covers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

# A club night that starts at 01:00 belongs to the evening before it, so the
# digest's "day" runs 06:00 → 06:00 local time rather than midnight → midnight.
NIGHT_ROLLOVER_HOUR = 6


@dataclass(frozen=True)
class City:
    key: str        # lookup key, e.g. "barcelona"
    name: str       # display name
    country: str    # ISO-3166 alpha-2
    tz: str         # IANA timezone
    lat: float
    lon: float

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


CITIES = {
    "barcelona": City("barcelona", "Barcelona", "ES", "Europe/Madrid", 41.3851, 2.1734),
}


def get_city(key: str) -> City:
    try:
        return CITIES[key.strip().lower()]
    except KeyError:
        raise ValueError("unknown city %r (known: %s)" % (key, ", ".join(sorted(CITIES))))


def collection_window(city: City, now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """Window collectors must cover: [today 00:00, day-after-tomorrow 06:00) local.

    It starts at midnight rather than "now" so that things already under way
    (an all-day market, a festival) are still collected; what has finished is
    dropped later, when events are bucketed into today / tomorrow.
    """
    local_now = (now or datetime.now(city.zone)).astimezone(city.zone)
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=2)).replace(hour=NIGHT_ROLLOVER_HOUR)
    return start, end
