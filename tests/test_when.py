"""Exhaustive tests for claude_cache_bump_gateway.cli.when.parse_when."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from claude_cache_bump_gateway.cli.when import At, Bump, Off, Status, WhenError, parse_when

_AMS = ZoneInfo("Europe/Amsterdam")
_NY = ZoneInfo("America/New_York")


def _at(year: int, month: int, day: int, hour: int, minute: int, tz: ZoneInfo = _AMS) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=tz)


# -- fixed keywords ----------------------------------------------------------


@pytest.mark.parametrize("text", ["", "status", "STATUS", "  Status  "])
def test_status(text: str) -> None:
    now = _at(2026, 9, 8, 12, 0)
    assert parse_when(text, now) == Status()


@pytest.mark.parametrize("text", ["bump", "BUMP", " Bump "])
def test_bump(text: str) -> None:
    now = _at(2026, 9, 8, 12, 0)
    assert parse_when(text, now) == Bump()


@pytest.mark.parametrize("text", ["off", "OFF", "0", " off "])
def test_off(text: str) -> None:
    now = _at(2026, 9, 8, 12, 0)
    assert parse_when(text, now) == Off()


# -- durations ----------------------------------------------------------------


def test_duration_minutes() -> None:
    now = _at(2026, 9, 8, 12, 0)
    result = parse_when("5m", now)
    assert result == At(now + timedelta(minutes=5))


def test_duration_hours() -> None:
    now = _at(2026, 9, 8, 12, 0)
    assert parse_when("2h", now) == At(now + timedelta(hours=2))


def test_duration_90_minutes() -> None:
    now = _at(2026, 9, 8, 12, 0)
    assert parse_when("90m", now) == At(now + timedelta(minutes=90))


def test_duration_combined_with_space() -> None:
    now = _at(2026, 9, 8, 12, 0)
    result = parse_when("2d 4h", now)
    assert result == At(now + timedelta(hours=52))


def test_duration_combined_joined() -> None:
    now = _at(2026, 9, 8, 12, 0)
    result = parse_when("2d4h", now)
    assert result == At(now + timedelta(hours=52))


@pytest.mark.parametrize("text", ["3day", "1hr", "10min"])
def test_duration_long_unit_spellings(text: str) -> None:
    now = _at(2026, 9, 8, 12, 0)
    result = parse_when(text, now)
    assert isinstance(result, At)
    assert result.when > now


# -- bare clock times -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "hour", "minute"),
    [
        ("9PM", 21, 0),
        ("9 pm", 21, 0),
        ("9:30PM", 21, 30),
        ("21:00", 21, 0),
        ("noon", 12, 0),
        ("teatime", 16, 0),
    ],
)
def test_bare_clock_resolves_to_today_when_still_future(text: str, hour: int, minute: int) -> None:
    now = _at(2026, 9, 8, 8, 0)
    result = parse_when(text, now)
    assert result == At(_at(2026, 9, 8, hour, minute))


@pytest.mark.parametrize(
    ("text", "hour", "minute"),
    [
        ("00:15:16", 0, 15),
        ("midnight", 0, 0),
    ],
)
def test_bare_clock_before_now_rolls_to_tomorrow(text: str, hour: int, minute: int) -> None:
    now = _at(2026, 9, 8, 8, 0)
    result = parse_when(text, now)
    assert result == At(_at(2026, 9, 9, hour, minute))


def test_clock_rolls_to_tomorrow_when_already_past() -> None:
    """The documented 21:00 -> 1AM rollover: typed at 21:00, ``1AM`` means tomorrow."""
    now = _at(2026, 9, 8, 21, 0)
    result = parse_when("1AM", now)
    assert result == At(_at(2026, 9, 9, 1, 0))


def test_clock_at_or_before_now_rolls_forward_not_errors() -> None:
    now = _at(2026, 9, 8, 21, 30)
    result = parse_when("21:00", now)
    assert result == At(_at(2026, 9, 9, 21, 0))


# -- today / tomorrow -------------------------------------------------------


def test_today_with_future_clock() -> None:
    now = _at(2026, 9, 8, 8, 0)
    result = parse_when("today 9pm", now)
    assert result == At(_at(2026, 9, 8, 21, 0))


def test_today_with_past_clock_is_an_error() -> None:
    now = _at(2026, 9, 8, 22, 0)
    with pytest.raises(WhenError, match="in the past"):
        parse_when("today 9pm", now)


def test_today_without_clock_is_an_error() -> None:
    now = _at(2026, 9, 8, 8, 0)
    with pytest.raises(WhenError):
        parse_when("today", now)


def test_tomorrow_alone_is_24_hours_from_now() -> None:
    now = _at(2026, 9, 8, 13, 47)
    result = parse_when("tomorrow", now)
    assert result == At(now + timedelta(hours=24))


def test_tomorrow_with_clock() -> None:
    now = _at(2026, 9, 8, 8, 0)
    result = parse_when("tomorrow 10AM", now)
    assert result == At(_at(2026, 9, 9, 10, 0))


def test_tomorrow_with_clock_already_passed_today_still_means_tomorrow() -> None:
    now = _at(2026, 9, 8, 23, 0)
    result = parse_when("tomorrow 6am", now)
    assert result == At(_at(2026, 9, 9, 6, 0))


# -- ISO dates ----------------------------------------------------------------


def test_iso_date_only_is_midnight_local() -> None:
    now = _at(2026, 9, 8, 8, 0)
    result = parse_when("2026-12-31", now)
    assert result == At(_at(2026, 12, 31, 0, 0))


def test_iso_date_with_space_time() -> None:
    now = _at(2026, 9, 8, 8, 0)
    result = parse_when("2026-12-31 08:00", now)
    assert result == At(_at(2026, 12, 31, 8, 0))


def test_iso_date_with_t_separator() -> None:
    now = _at(2026, 9, 8, 8, 0)
    result = parse_when("2026-12-31T08:00", now)
    assert result == At(_at(2026, 12, 31, 8, 0))


def test_iso_far_future_date_has_no_cap() -> None:
    now = _at(2026, 9, 8, 8, 0)
    result = parse_when("2100-01-01", now)
    assert result == At(_at(2100, 1, 1, 0, 0))


def test_iso_seconds_are_truncated() -> None:
    now = _at(2026, 9, 8, 8, 0)
    result = parse_when("2026-12-31 08:00:59", now)
    assert result == At(_at(2026, 12, 31, 8, 0))


# -- weekday names -------------------------------------------------------


@pytest.mark.parametrize("text", ["we", "wed", "wednesday", "WE", "Wed", "WEDNESDAY"])
def test_weekday_spellings_are_equivalent(text: str) -> None:
    """2026-09-08 is a Tuesday; every spelling of Wednesday resolves the same."""
    now = _at(2026, 9, 8, 8, 0)
    result = parse_when(text, now)
    assert result == At(_at(2026, 9, 9, 8, 0))


@pytest.mark.parametrize(
    ("text", "weekday"),
    [
        ("mo", 0), ("tu", 1), ("we", 2), ("th", 3), ("fr", 4), ("sa", 5), ("su", 6),
        ("mon", 0), ("tue", 1), ("wed", 2), ("thu", 3), ("fri", 4), ("sat", 5), ("sun", 6),
        ("tues", 1), ("thur", 3), ("thurs", 3),
        ("monday", 0), ("tuesday", 1), ("wednesday", 2), ("thursday", 3),
        ("friday", 4), ("saturday", 5), ("sunday", 6),
    ],
)  # fmt: skip
def test_every_weekday_spelling_resolves_to_its_weekday(text: str, weekday: int) -> None:
    now = _at(2026, 9, 8, 8, 0)  # a Tuesday
    result = parse_when(text, now)
    assert isinstance(result, At)
    assert result.when.weekday() == weekday
    assert now < result.when <= now + timedelta(days=7)


def test_weekday_alone_uses_current_time_of_day() -> None:
    now = _at(2026, 9, 8, 14, 30)  # Tuesday
    result = parse_when("friday", now)
    assert result == At(_at(2026, 9, 11, 14, 30))


def test_weekday_with_named_clock() -> None:
    now = _at(2026, 9, 8, 8, 0)  # Tuesday
    result = parse_when("wed noon", now)
    assert result == At(_at(2026, 9, 9, 12, 0))


def test_weekday_with_numeric_clock() -> None:
    now = _at(2026, 9, 8, 8, 0)  # Tuesday
    result = parse_when("friday 9AM", now)
    assert result == At(_at(2026, 9, 11, 9, 0))


def test_weekday_two_letter_with_24h_clock() -> None:
    now = _at(2026, 9, 8, 8, 0)  # Tuesday
    result = parse_when("tu 21:00", now)
    assert result == At(_at(2026, 9, 8, 21, 0))


def test_weekday_today_with_clock_still_ahead_resolves_today() -> None:
    now = _at(2026, 9, 8, 8, 0)  # Tuesday
    result = parse_when("tuesday 9PM", now)
    assert result == At(_at(2026, 9, 8, 21, 0))


def test_weekday_today_with_clock_already_passed_rolls_to_next_week() -> None:
    now = _at(2026, 9, 8, 22, 0)  # Tuesday
    result = parse_when("tuesday 9PM", now)
    assert result == At(_at(2026, 9, 15, 21, 0))


def test_weekday_today_no_clock_at_exact_now_rolls_to_next_week() -> None:
    """No clock means 'now', and now is never still ahead of itself."""
    now = _at(2026, 9, 8, 10, 0)  # Tuesday
    result = parse_when("tu", now)
    assert result == At(_at(2026, 9, 15, 10, 0))


def test_weekday_other_day_rolls_to_this_week() -> None:
    now = _at(2026, 9, 8, 8, 0)  # Tuesday
    result = parse_when("thursday", now)
    assert result == At(_at(2026, 9, 10, 8, 0))


def test_weekday_week_rollover_past_sunday() -> None:
    now = _at(2026, 9, 12, 8, 0)  # Saturday
    result = parse_when("mo", now)
    assert result == At(_at(2026, 9, 14, 8, 0))


def test_weekday_across_amsterdam_dst_week() -> None:
    """2026-03-29 is the Amsterdam spring-forward Sunday; typed the Monday before."""
    now = _at(2026, 3, 23, 8, 0, tz=_AMS)  # Monday, before the DST week
    assert now.utcoffset() == timedelta(hours=1)
    result = parse_when("su 09:00", now)
    assert isinstance(result, At)
    assert result.when == _at(2026, 3, 29, 9, 0, tz=_AMS)
    assert result.when.utcoffset() == timedelta(hours=2)


def test_weekday_invalid_clock_gives_a_hint() -> None:
    now = _at(2026, 9, 8, 8, 0)
    with pytest.raises(WhenError, match="try 2h, 9PM, tomorrow 10AM"):
        parse_when("wed dinner", now)


@pytest.mark.parametrize("text", ["morning", "dinner", "evening"])
def test_period_and_meal_words_are_rejected(text: str) -> None:
    now = _at(2026, 9, 8, 8, 0)
    with pytest.raises(WhenError, match="try 2h, 9PM, tomorrow 10AM"):
        parse_when(text, now)


# -- DST transitions ---------------------------------------------------------


def test_clock_resolution_across_amsterdam_spring_forward() -> None:
    """Europe/Amsterdam springs forward 02:00->03:00 on 2026-03-29; 09:00 is CEST."""
    now = _at(2026, 3, 29, 1, 30, tz=_AMS)
    assert now.utcoffset() == timedelta(hours=1)
    result = parse_when("today 09:00", now)
    assert isinstance(result, At)
    assert result.when == _at(2026, 3, 29, 9, 0, tz=_AMS)
    assert result.when.utcoffset() == timedelta(hours=2)


def test_clock_resolution_across_new_york_fall_back() -> None:
    """America/New_York falls back 02:00->01:00 on 2026-11-01; 06:00 is EST."""
    now = _at(2026, 10, 31, 20, 0, tz=_NY)
    assert now.utcoffset() == timedelta(hours=-4)
    result = parse_when("tomorrow 06:00", now)
    assert isinstance(result, At)
    assert result.when == _at(2026, 11, 1, 6, 0, tz=_NY)
    assert result.when.utcoffset() == timedelta(hours=-5)


# -- errors -------------------------------------------------------------------


def test_yesterday_is_recognised_as_in_the_past() -> None:
    now = _at(2026, 9, 8, 8, 0)
    with pytest.raises(WhenError, match="in the past"):
        parse_when("yesterday", now)


def test_past_iso_date_is_an_error() -> None:
    now = _at(2026, 9, 8, 8, 0)
    with pytest.raises(WhenError, match="in the past"):
        parse_when("2020-01-01", now)


@pytest.mark.parametrize("text", ["4/20/2027", "20.04.2027"])
def test_unsupported_date_formats_hint_iso(text: str) -> None:
    now = _at(2026, 9, 8, 8, 0)
    with pytest.raises(WhenError, match="YYYY-MM-DD"):
        parse_when(text, now)


@pytest.mark.parametrize("text", ["banana", "on", "on 2h", "next tuesday"])
def test_unparseable_input_gives_a_hint(text: str) -> None:
    now = _at(2026, 9, 8, 8, 0)
    with pytest.raises(WhenError, match="try 2h, 9PM, tomorrow 10AM"):
        parse_when(text, now)


@pytest.mark.parametrize("text", ["12", "30", "2", "0900"])
def test_bare_number_other_than_zero_is_rejected(text: str) -> None:
    """A unitless number is neither a duration nor a clock time; only ``0`` means off."""
    now = _at(2026, 9, 8, 8, 0)
    with pytest.raises(WhenError, match="try 2h, 9PM, tomorrow 10AM"):
        parse_when(text, now)


def test_bare_zero_is_still_off() -> None:
    assert parse_when("0", _at(2026, 9, 8, 8, 0)) == Off()
