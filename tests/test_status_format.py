"""Tests for claude_cache_bump_gateway.core.status_format."""

from __future__ import annotations

from datetime import timedelta, timezone

from claude_cache_bump_gateway.core.status_format import (
    format_clock,
    format_countdown,
    format_status_times,
)

#: A fixed, non-UTC zone so a test failure can't be masked by the host
#: machine happening to run in UTC.
_TZ = timezone(timedelta(hours=9, minutes=30))

#: 2023-11-14T20:13:21+09:30
_NOW = 1_700_000_001.0


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


def test_format_countdown_zero() -> None:
    assert format_countdown(0) == "00:00"


def test_format_clock_uses_given_zone() -> None:
    assert format_clock(_NOW + 3319, tz=_TZ) == "08:38"


def test_format_clock_defaults_to_local_zone_without_error() -> None:
    # Just exercise the no-tz path; the exact string depends on the host zone.
    assert len(format_clock(_NOW)) == 5


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
