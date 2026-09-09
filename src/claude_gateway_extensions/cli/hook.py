"""``UserPromptSubmit`` hook handler for the ``/cache`` fall-through command.

Per the "Skill as pure fall-through, hook does the work" decision in
``docs/DESIGN.md`` "Decisions": a hook is used instead of relying on the
skill because it is the only deterministic path that avoids a model turn.
``run_user_prompt_submit`` reads the hook JSON from stdin, and for a
matching prompt prints a single ``{"decision": "block", "reason": ...}``
JSON line to stdout so Claude Code shows the reason without invoking the
model. The ``/cache`` grammar itself (durations, clock times, dates, the
fixed ``status``/``bump``/``off`` keywords) is parsed by
:func:`~claude_gateway_extensions.cli.when.parse_when`; this module only
dispatches the parsed result to the control API and formats the reply.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, TextIO

from claude_gateway_extensions.cli.client import ApiError, ControlClient, DaemonUnreachable
from claude_gateway_extensions.cli.when import (
    At,
    Bump,
    Off,
    Status,
    WhenError,
    local_now,
    parse_when,
)
from claude_gateway_extensions.config import Config
from claude_gateway_extensions.core.status_format import format_deadline

_COMMAND_PREFIX = "/cache"


def _minutes_until(epoch: float | None, now: float) -> int | None:
    """Return whole minutes from ``now`` until ``epoch``, or ``None`` if ``epoch`` is unset."""
    if epoch is None:
        return None
    return max(0, round((epoch - now) / 60))


def _interval_minutes(session: Mapping[str, Any]) -> int | None:
    """Reconstruct the effective bump interval, in whole minutes, from a status payload.

    The status payload has no direct "interval" field once a session is
    running (``interval_s`` is only the override, ``null`` when the config
    default applies); the effective interval is instead the distance from
    ``next_bump_at`` back to whichever of ``last_request_at``/``last_bump_at``
    it was scheduled from.
    """
    next_bump_at = session.get("next_bump_at")
    if not isinstance(next_bump_at, int | float):
        return None
    last_request_at = session.get("last_request_at")
    last_bump_at = session.get("last_bump_at")
    anchor = max(
        last_request_at if isinstance(last_request_at, int | float) else 0.0,
        last_bump_at if isinstance(last_bump_at, int | float) else 0.0,
    )
    if anchor <= 0:
        return None
    return max(0, round((next_bump_at - anchor) / 60))


def _format_on_reason(session: Mapping[str, Any], now: float) -> str:
    """Format the reason line for an enabled ``status`` (no new deadline this call)."""
    until = session.get("until")
    minutes = _minutes_until(session.get("next_bump_at"), now)
    if isinstance(until, int | float):
        head = f"cache: on until {format_deadline(until, now)}"
    else:
        head = "cache: on for this session"
    if minutes is None:
        return head
    return f"{head}, next bump in {minutes}m"


def _format_until_reason(session: Mapping[str, Any], now: float) -> str:
    """Format the reason line for freshly enabling bumping with an absolute deadline."""
    until = session.get("until")
    if not isinstance(until, int | float):
        return "cache: on for this session"
    minutes = _interval_minutes(session)
    suffix = f", bump every {minutes}m" if minutes is not None else ""
    if session.get("rewarm"):
        suffix += ", rewarm armed (next cache write is tolerated)"
    return f"cache: bumping until {format_deadline(until, now)}{suffix}"


def _format_now_reason(result: Mapping[str, Any]) -> str:
    """Format the reason line for the ``bump`` command from a bump result payload."""
    outcome = result.get("outcome")
    usage = result.get("usage") or {}
    if outcome == "ok":
        tokens = usage.get("cache_read_input_tokens", 0)
        return f"cache: bumped, {tokens} tokens read from cache"
    if outcome == "cache_write":
        tokens = usage.get("cache_creation_input_tokens", 0)
        return (
            f"cache: bumped, but the cache had already expired (paid a full write, {tokens} tokens)"
        )
    if outcome == "unauthorized":
        return "cache: bump failed, credential rejected (session stopped)"
    if outcome == "rejected":
        return "cache: bump failed, upstream rejected the replay (session stopped)"
    if outcome == "transient":
        return "cache: bump failed, transient upstream error, try again shortly"
    error = result.get("error")
    if error:
        return f"cache: bump not possible right now ({error})"
    return "cache: bump failed, unrecognised response from daemon"


def _handle_command(client: ControlClient, session_id: str, argument: str, now_dt: datetime) -> str:
    """Parse the ``/cache`` argument and dispatch it, returning the reason line."""
    try:
        deadline = parse_when(argument, now_dt)
    except WhenError as exc:
        return f"cache: {exc}"

    if isinstance(deadline, Off):
        client.set_session(session_id, False)
        return "cache: off"

    if isinstance(deadline, Bump):
        result = client.bump(session_id)
        return _format_now_reason(result)

    if isinstance(deadline, At):
        session = client.set_session(
            session_id, True, until=deadline.when.timestamp(), rewarm=deadline.rewarm
        )
        return _format_until_reason(session, now_dt.timestamp())

    assert isinstance(deadline, Status)
    try:
        session = client.get_session(session_id)
    except ApiError as exc:
        if exc.status == 404:
            return "cache: off"
        raise
    if session.get("enabled"):
        return _format_on_reason(session, now_dt.timestamp())
    stopped_reason = session.get("stopped_reason")
    if stopped_reason:
        return f"cache: off, stopped: {stopped_reason}"
    return "cache: off"


def run_user_prompt_submit(stdin: TextIO, stdout: TextIO, environ: Mapping[str, str]) -> int:
    """Read the ``UserPromptSubmit`` hook JSON from ``stdin`` and handle ``/cache``.

    Returns 0 unconditionally once a ``/cache`` prompt has been recognised
    (so the user sees a reason instead of a hook failure), and 0 silently
    for any prompt that does not match or for unparseable input. A match
    requires ``/cache`` to be followed by end-of-string or whitespace, so
    ``/cachefoo`` (and the old ``/cache-bump`` spelling) are left alone.
    """
    raw = stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        return 0
    stripped = prompt.strip()
    if not stripped.startswith(_COMMAND_PREFIX):
        return 0
    rest = stripped[len(_COMMAND_PREFIX) :]
    if rest and not rest[0].isspace():
        return 0
    argument = rest.strip()
    session_id = payload.get("session_id")

    config = Config.from_env(environ)
    now_dt = local_now()
    reason: str
    if not isinstance(session_id, str) or not session_id:
        reason = "cache: no session id in hook payload"
    else:
        client = ControlClient(config.url)
        try:
            reason = _handle_command(client, session_id, argument, now_dt)
        except DaemonUnreachable:
            reason = f"cache: daemon not reachable at {config.url} (is cgx serve running?)"
        except ApiError as exc:
            reason = f"cache: daemon error ({exc.status})"
        except Exception as exc:
            reason = f"cache: unexpected error ({exc})"

    stdout.write(json.dumps({"decision": "block", "reason": reason}))
    stdout.write("\n")
    return 0


__all__ = ["run_user_prompt_submit"]
