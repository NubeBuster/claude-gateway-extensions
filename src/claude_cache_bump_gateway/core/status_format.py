"""Pure formatting of session status times for humans.

Kept separate from any I/O or clock so every caller — the control API's
per-request status dict (:meth:`~.scheduler.Scheduler.status`) and the CLI
statusline segment (:mod:`claude_cache_bump_gateway.cli.statusline`) —
renders the exact same strings for the exact same inputs. Every function
here takes its "now" and timezone as plain arguments rather than reading
the clock or the host's local zone itself, so tests can pin both.
"""

from __future__ import annotations

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


def format_status_times(
    *,
    cache_expires_at: float | None,
    next_bump_at: float | None,
    now: float,
    tz: tzinfo | None = None,
) -> dict[str, str | None]:
    """Build the ``formatted`` block of a session status dict.

    ``ttl_countdown``/``expires_at`` are present whenever ``cache_expires_at``
    is known, even when bumping is disabled, so a statusline can always show
    the cache's time to live. ``next_bump_countdown``/``next_bump_at`` are
    ``None`` when no bump is scheduled.
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
    }


__all__ = ["format_clock", "format_countdown", "format_status_times"]
