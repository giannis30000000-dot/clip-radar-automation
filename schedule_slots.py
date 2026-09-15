"""Timezone-aware production slot helpers.

Discovery remains hourly.  A future publishing invocation can ask for the
next available slot without selecting the whole day's content in advance.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Europe/Athens"
DEFAULT_SLOTS = ("12:00", "15:30", "19:00", "22:00")


def configured_slots() -> tuple[str, ...]:
    raw = os.getenv("PRODUCTION_SLOTS", ",".join(DEFAULT_SLOTS))
    slots = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not slots:
        return DEFAULT_SLOTS
    for value in slots:
        hour, minute = value.split(":", 1)
        if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
            raise ValueError(f"invalid production slot: {value}")
    return slots


def next_production_slot(
    now: datetime | None = None,
    timezone_name: str | None = None,
    slots: tuple[str, ...] | None = None,
) -> datetime:
    """Return the next future slot in the configured local timezone."""

    timezone_name = timezone_name or os.getenv("PRODUCTION_TIMEZONE", DEFAULT_TIMEZONE)
    zone = ZoneInfo(timezone_name)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_now = current.astimezone(zone)
    for day_offset in range(0, 3):
        local_date: date = local_now.date() + timedelta(days=day_offset)
        for value in slots or configured_slots():
            hour, minute = (int(part) for part in value.split(":", 1))
            candidate = datetime.combine(local_date, time(hour, minute), tzinfo=zone)
            if candidate > local_now:
                return candidate
    raise RuntimeError("could not find a future production slot")
