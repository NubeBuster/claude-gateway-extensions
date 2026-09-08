"""``ccbg statusline`` segment for a user's own Claude Code statusline command.

See ``docs/DESIGN.md`` "Claude Code integration". Reads the statusline JSON
from stdin, queries the control API with a short timeout, and prints one
short segment. Prints nothing (and still exits 0) when the daemon is
unreachable or the session is unknown, since a statusline segment must never
block or clutter the line for an optional integration.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import TextIO

from claude_cache_bump_gateway.cli.client import ApiError, ControlClient, DaemonUnreachable
from claude_cache_bump_gateway.config import Config

_TIMEOUT_S = 0.5


def _format_segment(session: Mapping[str, object], now: float) -> str:
    """Format the one-line statusline segment for a known session's status payload."""
    if not session.get("enabled"):
        stopped_reason = session.get("stopped_reason")
        if stopped_reason:
            return f"cache-bump: off, stopped: {stopped_reason}"
        return "cache-bump: off"
    next_bump_at = session.get("next_bump_at")
    if isinstance(next_bump_at, int | float):
        minutes = max(0, round((next_bump_at - now) / 60))
        return f"cache-bump: on, next in {minutes}m"
    return "cache-bump: on"


def run_statusline(stdin: TextIO, stdout: TextIO, environ: Mapping[str, str]) -> int:
    """Read the statusline JSON from ``stdin`` and print the cache-bump segment.

    Always returns 0. Prints nothing when the input is unparseable, the
    daemon is unreachable, or the session id is unknown to the daemon.
    """
    raw = stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return 0

    config = Config.from_env(environ)
    client = ControlClient(config.url, timeout=_TIMEOUT_S)
    try:
        session = client.get_session(session_id)
    except (DaemonUnreachable, ApiError, TimeoutError):
        return 0

    stdout.write(_format_segment(session, time.time()))
    stdout.write("\n")
    return 0


__all__ = ["run_statusline"]
