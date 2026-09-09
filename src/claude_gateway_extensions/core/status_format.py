"""Pure formatting of session status times for humans.

Kept separate from any I/O or clock so every caller — the control API's
per-request status dict (:meth:`~.scheduler.Scheduler.status`) and the CLI
statusline segment (:mod:`claude_gateway_extensions.cli.statusline`) —
renders the exact same strings for the exact same inputs. Every function
here takes its "now" and timezone as plain arguments rather than reading
the clock or the host's local zone itself, so tests can pin both.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, tzinfo

#: Boundary, in seconds, at which a countdown switches from ``MM:SS`` to
#: ``H:MM:SS``.
_ONE_HOUR_S = 3600


def format_countdown(seconds: float) -> str:
    """Format a duration in seconds as ``MM:SS``, or ``H:MM:SS`` at one hour or more.

    Already-elapsed durations (negative ``seconds``) clamp to ``00:00``
    rather than going negative.
    """
    total = max(0, round(seconds))
    hours, remainder = divmod(total, _ONE_HOUR_S)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_clock(epoch: float, tz: tzinfo | None = None) -> str:
    """Format an epoch timestamp as a local ``HH:MM`` clock value.

    ``tz`` overrides the zone used for the conversion (tests pin a fixed
    zone); defaults to the host's local timezone.
    """
    local = datetime.fromtimestamp(epoch, tz=UTC).astimezone(tz)
    return local.strftime("%H:%M")


def format_rewarm_write(
    rewarm_write: Mapping[str, object] | None, tz: tzinfo | None = None
) -> str | None:
    """Format a ``rewarm_write`` record (``{"at": epoch, "usage": {...}}``) for humans.

    Returns ``None`` when ``rewarm_write`` is ``None`` (no tolerated write
    has happened yet for this session). See the "Rewarm" decision in
    ``docs/DESIGN.md``.
    """
    if rewarm_write is None:
        return None
    usage = rewarm_write.get("usage")
    tokens = usage.get("cache_creation_input_tokens", 0) if isinstance(usage, Mapping) else 0
    at = rewarm_write.get("at")
    clock = format_clock(at, tz) if isinstance(at, int | float) else "?"
    return f"{tokens} cache-write tokens at {clock}"


def format_status_times(
    *,
    cache_expires_at: float | None,
    next_bump_at: float | None,
    now: float,
    tz: tzinfo | None = None,
    rewarm_write: Mapping[str, object] | None = None,
) -> dict[str, str | None]:
    """Build the ``formatted`` block of a session status dict.

    ``ttl_countdown``/``expires_at`` are present whenever ``cache_expires_at``
    is known, even when bumping is disabled, so a statusline can always show
    the cache's time to live. ``next_bump_countdown``/``next_bump_at`` are
    ``None`` when no bump is scheduled. ``rewarm_write`` renders the
    session's ``rewarm_write`` record (see :func:`format_rewarm_write`), or
    ``None`` when the session has never had a rewarm-tolerated write.
    """
    ttl_countdown: str | None = None
    expires_at: str | None = None
    if cache_expires_at is not None:
        ttl_countdown = format_countdown(cache_expires_at - now)
        expires_at = format_clock(cache_expires_at, tz)

    next_bump_countdown: str | None = None
    next_bump_at_formatted: str | None = None
    if next_bump_at is not None:
        next_bump_countdown = format_countdown(next_bump_at - now)
        next_bump_at_formatted = format_clock(next_bump_at, tz)

    return {
        "ttl_countdown": ttl_countdown,
        "expires_at": expires_at,
        "next_bump_countdown": next_bump_countdown,
        "next_bump_at": next_bump_at_formatted,
        "rewarm_write": format_rewarm_write(rewarm_write, tz),
    }


def _format_remaining(seconds: float) -> str:
    """Format a duration as minute-precision ``Nd Nh Nm``, omitting any zero-valued unit.

    Each of ``d``/``h``/``m`` is dropped when it is zero, in either order
    (``3d 12h`` for an exact multiple of days, ``3d 5m`` when only the hour
    is zero); at least one unit is always shown, falling back to ``0m`` for
    an already-elapsed (or exactly zero) duration, which also clamps rather
    than going negative.
    """
    total_minutes = max(0, round(seconds / 60))
    days, remainder = divmod(total_minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def format_deadline(until: float, now: float, tz: tzinfo | None = None) -> str:
    """Format an absolute deadline relative to ``now``, classified by local calendar day.

    Same local calendar day as ``now``: ``HH:MM — Nd Nh Nm from now``. The
    next local calendar day: ``tomorrow at HH:MM — Nd Nh Nm from now``. Any
    other day (including one already in the past): the full ISO 8601
    timestamp with UTC offset instead of a bare clock time, e.g.
    ``2026-09-12T09:00+02:00 — 3d 12h from now``. The classification uses
    calendar dates in ``tz`` (or the host's local zone), not 24-hour
    windows, so it agrees with how a person reads "today"/"tomorrow".

    Callers prepend their own leading word (``"until "``, ``"on "``) — this
    returns everything from the clock/date onward, so the same string works
    whether embedded in a sentence or a ``key=value`` status field.
    """
    local_until = datetime.fromtimestamp(until, tz=UTC).astimezone(tz)
    local_now = datetime.fromtimestamp(now, tz=UTC).astimezone(tz)
    day_delta = (local_until.date() - local_now.date()).days
    if day_delta == 0:
        head = local_until.strftime("%H:%M")
    elif day_delta == 1:
        head = f"tomorrow at {local_until.strftime('%H:%M')}"
    else:
        head = local_until.isoformat(timespec="minutes")
    return f"{head} — {_format_remaining(until - now)} from now"


__all__ = [
    "format_clock",
    "format_countdown",
    "format_deadline",
    "format_rewarm_write",
    "format_status_times",
]
