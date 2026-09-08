"""Tests for claude_gateway_extensions.core.status_format."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from claude_gateway_extensions.core.status_format import (
    format_clock,
    format_countdown,
    format_deadline,
    format_status_times,
)

#: A fixed, non-UTC zone so a test failure can't be masked by the host
#: machine happening to run in UTC.
_TZ = timezone(timedelta(hours=9, minutes=30))

#: 2023-11-14T20:13:21+09:30
_NOW = 1_700_000_001.0

_AMS = ZoneInfo("Europe/Amsterdam")
_NY = ZoneInfo("America/New_York")


def _epoch(year: int, month: int, day: int, hour: int, minute: int, tz: ZoneInfo) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=tz).timestamp()


def test_format_countdown_minutes_and_seconds() -> None:
    assert format_countdown(3319) == "55:19"


def test_format_countdown_pads_single_digit_minutes_and_seconds() -> None:
    assert format_countdown(65) == "01:05"


def test_format_countdown_switches_to_hours_at_one_hour() -> None:
    assert format_countdown(3600) == "1:00:00"
    assert format_countdown(3661) == "1:01:01"


def test_format_countdown_rounds() -> None:
    assert format_countdown(59.6) == "01:00"


def test_format_countdown_negative_clamps_to_zero() -> None:
    assert format_countdown(-42) == "00:00"


def test_format_clock_uses_given_zone() -> None:
    assert format_clock(_NOW + 3319, tz=_TZ) == "08:38"


def test_format_status_times_full() -> None:
    result = format_status_times(
        cache_expires_at=_NOW + 3319,
        next_bump_at=_NOW + 3019,
        now=_NOW,
        tz=_TZ,
    )
    assert result == {
        "ttl_countdown": "55:19",
        "expires_at": "08:38",
        "next_bump_countdown": "50:19",
        "next_bump_at": "08:33",
    }


def test_format_status_times_no_cache_expiry() -> None:
    result = format_status_times(cache_expires_at=None, next_bump_at=_NOW + 60, now=_NOW, tz=_TZ)
    assert result["ttl_countdown"] is None
    assert result["expires_at"] is None
    assert result["next_bump_countdown"] == "01:00"
    assert result["next_bump_at"] is not None


def test_format_status_times_no_bump_scheduled() -> None:
    """A disabled session with a snapshot still gets a TTL countdown, no bump fields."""
    result = format_status_times(cache_expires_at=_NOW + 300, next_bump_at=None, now=_NOW, tz=_TZ)
    assert result["ttl_countdown"] == "05:00"
    assert result["expires_at"] is not None
    assert result["next_bump_countdown"] is None
    assert result["next_bump_at"] is None


def test_format_status_times_elapsed_ttl_clamps_to_zero() -> None:
    result = format_status_times(cache_expires_at=_NOW - 10, next_bump_at=None, now=_NOW, tz=_TZ)
    assert result["ttl_countdown"] == "00:00"


# -- format_deadline: local-calendar-day classification ----------------------


def test_format_deadline_same_local_day() -> None:
    now = _epoch(2026, 9, 8, 18, 45, _AMS)
    until = _epoch(2026, 9, 8, 21, 0, _AMS)
    assert format_deadline(until, now, tz=_AMS) == "21:00 — 2h 15m from now"


def test_format_deadline_next_local_day() -> None:
    now = _epoch(2026, 9, 8, 15, 0, _AMS)
    until = _epoch(2026, 9, 9, 4, 20, _AMS)
    assert format_deadline(until, now, tz=_AMS) == "tomorrow at 04:20 — 13h 20m from now"


def test_format_deadline_later_uses_full_iso_with_offset() -> None:
    now = _epoch(2026, 9, 8, 21, 0, _AMS)
    until = _epoch(2026, 9, 12, 9, 0, _AMS)
    assert format_deadline(until, now, tz=_AMS) == "2026-09-12T09:00+02:00 — 3d 12h from now"


def test_format_deadline_classifies_by_calendar_day_not_24h_window() -> None:
    """23:50 -> 00:10 is only 20 minutes away but crosses local midnight into 'tomorrow'."""
    now = _epoch(2026, 9, 8, 23, 50, _AMS)
    until = _epoch(2026, 9, 9, 0, 10, _AMS)
    assert format_deadline(until, now, tz=_AMS) == "tomorrow at 00:10 — 20m from now"


def test_format_deadline_just_before_midnight_stays_same_day() -> None:
    now = _epoch(2026, 9, 8, 20, 0, _AMS)
    until = _epoch(2026, 9, 8, 23, 59, _AMS)
    assert format_deadline(until, now, tz=_AMS) == "23:59 — 3h 59m from now"


# -- format_deadline: DST day -------------------------------------------------


def test_format_deadline_across_amsterdam_spring_forward() -> None:
    """Amsterdam springs forward 02:00->03:00 on 2026-03-29; deadline is later that day."""
    now = _epoch(2026, 3, 29, 1, 0, _AMS)
    until = _epoch(2026, 3, 29, 9, 0, _AMS)
    assert format_deadline(until, now, tz=_AMS) == "09:00 — 7h from now"


def test_format_deadline_across_new_york_fall_back() -> None:
    """New York falls back 02:00->01:00 on 2026-11-01; deadline is the next local day."""
    now = _epoch(2026, 10, 31, 20, 0, _NY)
    until = _epoch(2026, 11, 1, 6, 0, _NY)
    assert format_deadline(until, now, tz=_NY) == "tomorrow at 06:00 — 11h from now"


# -- format_deadline: remaining-time formatting -------------------------------


@pytest.mark.parametrize(
    ("minutes", "expected_tail"),
    [(45, "45m"), (3 * 1440 + 5, "3d 5m"), (3 * 1440 + 4 * 60 + 5, "3d 4h 5m")],
)
def test_format_deadline_remaining_omits_zero_units(minutes: int, expected_tail: str) -> None:
    now = _epoch(2026, 1, 1, 0, 0, _AMS)
    until = now + minutes * 60
    assert format_deadline(until, now, tz=_AMS).endswith(expected_tail + " from now")


def test_format_deadline_elapsed_clamps_to_zero_minutes() -> None:
    now = _epoch(2026, 1, 1, 12, 0, _AMS)
    until = now - 30
    assert format_deadline(until, now, tz=_AMS).endswith("0m from now")
