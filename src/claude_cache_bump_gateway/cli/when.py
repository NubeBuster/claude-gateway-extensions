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
_NAMED_CLOCKS = {
    "morning": dt_time(9, 0),
    "noon": dt_time(12, 0),
    "teatime": dt_time(16, 0),
    "dinner": dt_time(18, 0),
    "nighttime": dt_time(22, 0),
    "midnight": dt_time(0, 0),
}

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
class _ParsedClock:
    """A parsed clock token: its time, plus a 12h-ambiguous alternate if any.

    ``alt`` is set only for a token with minutes but no am/pm whose hour is
    written without a leading zero and falls in 1..12 (``4:20``, ``12:30``):
    such a token could mean either ``time`` or ``time`` shifted by 12 hours,
    and callers resolve the ambiguity by picking whichever candidate is the
    nearest upcoming one. Zero-padded hours (``04:20``), hours 13..23, and
    any am/pm-qualified token are unambiguous and leave ``alt`` unset.
    """

    time: dt_time
    alt: dt_time | None = None


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
        return _resolve_on_day(_add_days(now.date(), 1), clock, now, original)

    if lowered.startswith("today"):
        rest = lowered[len("today") :].strip()
        clock = _require_clock(rest)
        return _resolve_on_day(now.date(), clock, now, original)

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
        return _resolve_nearest_upcoming(clock, now)

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


def _require_clock(rest: str) -> _ParsedClock:
    """Parse a clock time that must be present (``today``/``tomorrow <clock>``)."""
    if not rest:
        raise WhenError(f"today requires a time, e.g. 'today 9pm' ({_HINT})")
    clock = _try_parse_clock(rest)
    if clock is None:
        raise WhenError(f"{rest!r}: {_HINT}")
    return clock


def _candidate_times(clock: _ParsedClock) -> list[dt_time]:
    """The one or two wall-clock times a parsed clock token could mean."""
    return [clock.time] if clock.alt is None else [clock.time, clock.alt]


def _resolve_nearest_upcoming(clock: _ParsedClock, now: datetime) -> datetime:
    """Resolve a bare clock token (no explicit day) to the nearest upcoming candidate.

    Each candidate time is independently rolled to tomorrow if today's
    occurrence has already passed (the pre-existing single-candidate rule),
    then the earliest of the resulting datetimes wins. With a single
    (unambiguous) candidate this reduces to the original behaviour.
    """
    resolved = []
    for candidate_time in _candidate_times(clock):
        candidate = _combine(now.date(), candidate_time, now)
        if candidate <= now:
            candidate = _combine(_add_days(now.date(), 1), candidate_time, now)
        resolved.append(candidate)
    return min(resolved)


def _resolve_on_day(day: date, clock: _ParsedClock, now: datetime, original: str) -> datetime:
    """Pick the earliest of a clock token's candidate(s) on ``day`` that is still ahead of ``now``.

    Used for the explicit-day forms (``today``/``tomorrow <clock>``): the day
    is fixed, so (unlike :func:`_resolve_nearest_upcoming`) there is no
    rolling forward — if none of the candidates on ``day`` are still ahead of
    ``now``, that is an error.
    """
    candidates = [_combine(day, candidate_time, now) for candidate_time in _candidate_times(clock)]
    future = [candidate for candidate in candidates if candidate > now]
    if not future:
        raise _in_the_past(original)
    return min(future)


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


def _resolve_weekday(weekday: int, clock: _ParsedClock | None, now: datetime) -> datetime:
    """Resolve a weekday (plus optional clock) to its next local occurrence.

    Today counts only if ``clock`` (or, lacking one, the current time of
    day) is still ahead of ``now``; otherwise it rolls to the same weekday
    next week, matching the bare-clock/``today`` rollover rules above. When
    ``clock`` is 12h-ambiguous, both candidates are tried on each day and the
    earliest one still ahead of ``now`` wins (the "explicit day" rule), with
    the whole day rolling forward a week, not erroring, if neither is.
    """
    days_ahead = (weekday - now.weekday()) % 7
    if clock is None:
        target_clock = dt_time(now.hour, now.minute)
        candidate = _combine(_add_days(now.date(), days_ahead), target_clock, now)
        if candidate <= now:
            candidate = _combine(_add_days(now.date(), days_ahead + 7), target_clock, now)
        return candidate
    candidate_times = _candidate_times(clock)
    day = _add_days(now.date(), days_ahead)
    candidates = [_combine(day, candidate_time, now) for candidate_time in candidate_times]
    future = [candidate for candidate in candidates if candidate > now]
    if future:
        return min(future)
    day = _add_days(now.date(), days_ahead + 7)
    return min(_combine(day, candidate_time, now) for candidate_time in candidate_times)


def _try_parse_clock(lowered: str) -> _ParsedClock | None:
    """Parse a bare clock time (named or numeric); ``None`` if it doesn't look like one.

    A numeric token with minutes but no am/pm, whose hour is written without
    a leading zero and falls in 1..12, is 12h-ambiguous: the returned
    :class:`_ParsedClock` carries both the literal hour and the hour+12
    (wrapping 12 to 0) as ``alt``, leaving resolution to the caller.
    """
    stripped = lowered.strip()
    named = _NAMED_CLOCKS.get(stripped)
    if named is not None:
        return _ParsedClock(named)
    match = _CLOCK_RE.match(stripped)
    if match is None:
        return None
    hour_str, minute_str, ampm = match.groups()
    hour = int(hour_str)
    minute = int(minute_str) if minute_str else 0
    if not 0 <= minute <= 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        return _ParsedClock(dt_time(hour, minute))
    if not 0 <= hour <= 23:
        return None
    alt = None
    if minute_str is not None and not hour_str.startswith("0") and 1 <= hour <= 12:
        alt = dt_time((hour + 12) % 24, minute)
    return _ParsedClock(dt_time(hour, minute), alt)


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
