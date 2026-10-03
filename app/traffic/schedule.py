"""Provider closure schedules, kept separate from notice lifecycle timestamps."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class ClosurePeriod:
    from_day: str = ""
    to_day: str = ""
    start_time: str = ""
    finish_time: str = ""
    timezone: str = ""
    impact: str = ""
    direction: str = ""

    def describe(self) -> str:
        days = self.from_day or "days not supplied"
        if self.to_day:
            days += f" to {self.to_day}"
        hours = self.start_time or "start time not supplied"
        if self.finish_time:
            hours += f" to {self.finish_time}"
        return f"{days} {hours} ({self.timezone or 'timezone not supplied'})"


_DAYS = {day.casefold(): index for index, day in enumerate(
    ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"))}


def _days(period):
    value = period.from_day.casefold().strip()
    # Range order in the published guide is ambiguous. Preserve ranges without
    # declaring a current window until the provider establishes that ordering.
    if period.to_day and period.to_day.casefold().strip() != value:
        return None
    if value in {"every day", "daily"}:
        return set(range(7))
    if value == "weekdays":
        return set(range(5))
    if value == "weekends":
        return {5, 6}
    return {_DAYS[value]} if value in _DAYS else None


def _minutes(value):
    match = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*", value, re.I)
    if not match:
        return None
    hour, minute = int(match[1]), int(match[2] or 0)
    meridiem = (match[3] or "").casefold()
    if minute > 59 or (meridiem and not 1 <= hour <= 12) or (not meridiem and hour > 23):
        return None
    if meridiem:
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    return hour * 60 + minute


def window_state(periods: tuple[ClosurePeriod, ...], now: float) -> tuple[str, str]:
    """Return scheduled/unscheduled/unknown; never assert actual closure."""
    if not periods:
        return "unknown", "No closure schedule supplied"
    unknown = False
    for period in periods:
        days = _days(period)
        all_day = period.start_time.casefold().strip() == "all day" and not period.finish_time
        if days == set(range(7)) and all_day:
            return "scheduled", "Within an all-day daily schedule; actual closure not confirmed"
        if days is None or not period.timezone:
            unknown = True
            continue
        try:
            local = datetime.fromtimestamp(now, ZoneInfo(period.timezone))
        except (ZoneInfoNotFoundError, ValueError, OverflowError):
            unknown = True
            continue
        if all_day:
            if local.weekday() in days:
                return "scheduled", "Within the supplied daily schedule; actual closure not confirmed"
            continue
        start, finish = _minutes(period.start_time), _minutes(period.finish_time)
        if start is None or finish is None or start == finish:
            unknown = True
            continue
        minute = local.hour * 60 + local.minute
        if finish > start:
            inside = local.weekday() in days and start <= minute < finish
        else:
            inside = ((local.weekday() in days and minute >= start) or
                      ((local - timedelta(days=1)).weekday() in days and minute < finish))
        if inside:
            return "scheduled", "Within the supplied closure schedule; actual closure not confirmed"
    if unknown:
        return "unknown", "Current closure window unconfirmed: schedule timezone, days or hours not established"
    return "unscheduled", "Outside the supplied closure schedule; notice remains current"
