"""Grammar for the ``/cache`` argument: parses text into a :class:`Deadline`.

``parse_when`` is the single place that understands the whole ``/cache``
grammar (case-insensitive, whitespace-tolerant): the empty string or
``status``; ``bump``; ``off``/``0``; a relative duration (``2h``, ``2d 4h``);
a bare clock time (``9PM``, ``21:00``, ``noon``); ``today``/``tomorrow``
plus an optional clock time; a weekday name or abbreviation (``wed``,
``wednesday``) plus an optional clock time; or an ISO
``YYYY-MM-DD[ HH:MM[:SS]]`` date.
Everything that resolves to an absolute deadline comes back as an ``At``
with a timezone-aware, minute-truncated :class:`datetime.datetime`; the hook
and the CLI both call this instead of hand-rolling the grammar twice.

Callers pass ``now`` explicitly (see :func:`local_now`) so this module stays
a pure function with no wall-clock dependency of its own, which is what
makes it exhaustively testable with fixed times and fixed zones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time

_HINT = "try 2h, 9PM, tomorrow 10AM, fri 9AM, 2026-12-31 08:00, off, bump"

_DURATION_TOKEN_RE = re.compile(r"(\d+)\s*(day|d|hr|h|min|m)")
_DURATION_UNIT_SECONDS = {"d": 86400, "day": 86400, "h": 3600, "hr": 3600, "m": 60, "min": 60}

_CLOCK_RE = re.compile(r"^(\d{1,2})(?::(\d{2})(?::\d{2})?)?\s*(am|pm)?$")
_NAMED_CLOCKS = {"noon": dt_time(12, 0), "midnight": dt_time(0, 0), "teatime": dt_time(16, 0)}

#: ``date.weekday()`` numbering: Monday=0 .. Sunday=6. Three spellings, all
#: case-insensitive (the caller already lowercases): two-letter, three-letter
#: (plus the non-truncating ``tues``/``thur``/``thurs``), and full names.
_WEEKDAY_NAMES = {
    "mo": 0,
    "tu": 1,
    "we": 2,
    "th": 3,
    "fr": 4,
    "sa": 5,
    "su": 6,
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
    "tues": 1,
    "thur": 3,
    "thurs": 3,
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_ISO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[ t](\d{2}:\d{2})(?::\d{2})?)?$")
_UNSUPPORTED_DATE_RE = re.compile(r"^\d{1,4}[/.]\d{1,2}[/.]\d{1,4}$")
_BARE_NUMBER_RE = re.compile(r"^\d+$")


class WhenError(ValueError):
    """Raised by :func:`parse_when` with a one-line, user-facing reason."""


@dataclass(frozen=True)
class Status:
    """``/cache`` or ``/cache status``: report the current state."""


@dataclass(frozen=True)
class Bump:
    """``/cache bump``: bump immediately, outside the normal schedule."""


@dataclass(frozen=True)
class Off:
    """``/cache off`` or ``/cache 0``: disable bumping."""


@dataclass(frozen=True)
class At:
    """Enable bumping with an absolute, aware, minute-truncated deadline."""

    when: datetime


#: Every shape a parsed ``/cache`` argument can take.
Deadline = Status | Bump | Off | At


def local_now() -> datetime:
    """Return the current time in the process's local zone, honouring ``TZ``."""
    return datetime.now().astimezone()


def parse_when(text: str, now: datetime) -> Deadline:
    """Parse a ``/cache`` argument into a :class:`Deadline`.

    ``now`` must be an aware ``datetime`` (see :func:`local_now`); every
    relative and "next occurrence" calculation is anchored to it and done in
    its zone, so DST transitions resolve correctly. Raises :class:`WhenError`
    with a one-line reason on anything unparseable or already in the past.
    """
    stripped = text.strip()
    lowered = stripped.lower()
    if lowered in ("", "status"):
        return Status()
    if lowered == "bump":
        return Bump()
    if lowered in ("off", "0"):
        return Off()
    return At(when=_truncate_to_minute(_parse_deadline(stripped, lowered, now)))


def _truncate_to_minute(when: datetime) -> datetime:
    """Drop seconds and microseconds, per the documented minute-resolution deadline."""
    return when.replace(second=0, microsecond=0)


def _in_the_past(original: str) -> WhenError:
    return WhenError(f"{original} is in the past")


def _parse_deadline(original: str, lowered: str, now: datetime) -> datetime:
    """Parse everything except the fixed keywords already handled by :func:`parse_when`."""
    if lowered == "yesterday":
        raise _in_the_past(original)

    if lowered == "tomorrow":
        return now + timedelta(hours=24)
    if lowered.startswith("tomorrow"):
        rest = lowered[len("tomorrow") :].strip()
        clock = _require_clock(rest)
        return _combine(_add_days(now.date(), 1), clock, now)

    if lowered.startswith("today"):
        rest = lowered[len("today") :].strip()
        clock = _require_clock(rest)
        candidate = _combine(now.date(), clock, now)
        if candidate <= now:
            raise _in_the_past(original)
        return candidate

    weekday_match = _try_parse_weekday(lowered)
    if weekday_match is not None:
        weekday, rest = weekday_match
        clock = None
        if rest:
            clock = _try_parse_clock(rest)
            if clock is None:
                raise WhenError(f"{rest!r}: {_HINT}")
        return _resolve_weekday(weekday, clock, now)

    iso = _try_parse_iso(lowered, now)
    if iso is not None:
        if iso <= now:
            raise _in_the_past(original)
        return iso

    if _UNSUPPORTED_DATE_RE.match(lowered):
        raise WhenError(f"{original}: unsupported date format, use YYYY-MM-DD")

    if _BARE_NUMBER_RE.match(lowered):
        # A bare number reads as a duration to some people and as a clock
        # hour to others (`/cache 12`), so neither is guessed: only `0` has
        # a fixed meaning, and parse_when already took it as "off".
        raise WhenError(f"{original!r}: {_HINT}")

    duration = _try_parse_duration(lowered)
    if duration is not None:
        return now + duration

    clock = _try_parse_clock(lowered)
    if clock is not None:
        candidate = _combine(now.date(), clock, now)
        if candidate <= now:
            candidate = _combine(_add_days(now.date(), 1), clock, now)
        return candidate

    raise WhenError(f"{original!r}: {_HINT}")


def _add_days(day: date, count: int) -> date:
    return day + timedelta(days=count)


def _combine(day: date, clock: dt_time, now: datetime) -> datetime:
    """Build an aware datetime for ``day`` at ``clock`` in ``now``'s zone.

    Constructing directly from local wall-clock fields (rather than adding a
    fixed number of seconds to a naive value) lets ``now.tzinfo`` resolve the
    correct UTC offset for that specific date, so a DST transition between
    ``now`` and the result cannot skew the wall-clock time.
    """
    return datetime(day.year, day.month, day.day, clock.hour, clock.minute, tzinfo=now.tzinfo)


def _require_clock(rest: str) -> dt_time:
    """Parse a clock time that must be present (``today``/``tomorrow <clock>``)."""
    if not rest:
        raise WhenError(f"today requires a time, e.g. 'today 9pm' ({_HINT})")
    clock = _try_parse_clock(rest)
    if clock is None:
        raise WhenError(f"{rest!r}: {_HINT}")
    return clock


def _try_parse_duration(lowered: str) -> timedelta | None:
    """Parse one or more space-separated-or-joined ``<n><unit>`` tokens, or return ``None``."""
    if not lowered:
        return None
    total_seconds = 0
    matched_any = False
    i = 0
    n = len(lowered)
    while i < n:
        while i < n and lowered[i].isspace():
            i += 1
        if i >= n:
            break
        match = _DURATION_TOKEN_RE.match(lowered, i)
        if match is None:
            return None
        matched_any = True
        value = int(match.group(1))
        unit = match.group(2)
        total_seconds += value * _DURATION_UNIT_SECONDS[unit]
        i = match.end()
    if not matched_any:
        return None
    return timedelta(seconds=total_seconds)


def _try_parse_weekday(lowered: str) -> tuple[int, str] | None:
    """Parse a leading weekday name/abbreviation and return ``(weekday, rest)``.

    ``weekday`` follows :meth:`datetime.date.weekday` numbering (Monday=0).
    ``rest`` is whatever followed the weekday token, unparsed and possibly
    empty; ``None`` if the first token isn't a weekday name at all.
    """
    first, _, rest = lowered.partition(" ")
    weekday = _WEEKDAY_NAMES.get(first)
    if weekday is None:
        return None
    return weekday, rest.strip()


def _resolve_weekday(weekday: int, clock: dt_time | None, now: datetime) -> datetime:
    """Resolve a weekday (plus optional clock) to its next local occurrence.

    Today counts only if ``clock`` (or, lacking one, the current time of
    day) is still ahead of ``now``; otherwise it rolls to the same weekday
    next week, matching the bare-clock/``today`` rollover rules above.
    """
    days_ahead = (weekday - now.weekday()) % 7
    target_clock = clock if clock is not None else dt_time(now.hour, now.minute)
    candidate = _combine(_add_days(now.date(), days_ahead), target_clock, now)
    if candidate <= now:
        candidate = _combine(_add_days(now.date(), days_ahead + 7), target_clock, now)
    return candidate


def _try_parse_clock(lowered: str) -> dt_time | None:
    """Parse a bare clock time (named or numeric); ``None`` if it doesn't look like one."""
    stripped = lowered.strip()
    named = _NAMED_CLOCKS.get(stripped)
    if named is not None:
        return named
    match = _CLOCK_RE.match(stripped)
    if match is None:
        return None
    hour_str, minute_str, ampm = match.groups()
    hour = int(hour_str)
    minute = int(minute_str) if minute_str else 0
    if ampm:
        if not 1 <= hour <= 12:
            return None
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    elif not 0 <= hour <= 23:
        return None
    if not 0 <= minute <= 59:
        return None
    return dt_time(hour, minute)


def _try_parse_iso(lowered: str, now: datetime) -> datetime | None:
    """Parse ``YYYY-MM-DD``, ``YYYY-MM-DD HH:MM`` or ``YYYY-MM-DDTHH:MM``, or return ``None``."""
    match = _ISO_RE.match(lowered)
    if match is None:
        return None
    date_str, time_str = match.groups()
    try:
        parsed_date = date.fromisoformat(date_str)
    except ValueError:
        return None
    hour, minute = 0, 0
    if time_str is not None:
        hour, minute = (int(part) for part in time_str.split(":"))
    return datetime(
        parsed_date.year,
        parsed_date.month,
        parsed_date.day,
        hour,
        minute,
        tzinfo=now.tzinfo,
    )


__all__ = ["At", "Bump", "Deadline", "Off", "Status", "WhenError", "local_now", "parse_when"]
