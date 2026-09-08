"""``UserPromptSubmit`` hook handler for the ``/cache-bump`` fall-through command.

See ``docs/DESIGN.md`` "Claude Code integration": a hook is used instead of
relying on the skill because it is the only deterministic path that avoids a
model turn. ``run_user_prompt_submit`` reads the hook JSON from stdin, and
for a matching prompt prints a single ``{"decision": "block", "reason": ...}``
JSON line to stdout so Claude Code shows the reason without invoking the
model.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from typing import Any, TextIO

from claude_cache_bump_gateway.cli.client import ApiError, ControlClient, DaemonUnreachable
from claude_cache_bump_gateway.config import Config

_COMMAND_PREFIX = "/cache-bump"
_DURATION_RE = re.compile(r"^(\d+)([hm])$")


def _parse_duration(token: str) -> int | None:
    """Parse an ``Nh``/``Nm`` duration token into seconds, or ``None`` if malformed."""
    match = _DURATION_RE.match(token)
    if match is None:
        return None
    value, unit = match.groups()
    seconds = int(value) * (3600 if unit == "h" else 60)
    return seconds


def _minutes_until(epoch: float | None, now: float) -> int | None:
    """Return whole minutes from ``now`` until ``epoch``, or ``None`` if ``epoch`` is unset."""
    if epoch is None:
        return None
    return max(0, round((epoch - now) / 60))


def _format_on_reason(session: Mapping[str, Any], now: float) -> str:
    """Format the reason line for ``on`` and an enabled ``status``."""
    minutes = _minutes_until(session.get("next_bump_at"), now)
    if minutes is None:
        return "cache-bump: on for this session"
    return f"cache-bump: on for this session, next bump in {minutes}m"


def _format_now_reason(result: Mapping[str, Any]) -> str:
    """Format the reason line for the ``now`` command from a bump result payload."""
    outcome = result.get("outcome")
    usage = result.get("usage") or {}
    if outcome == "ok":
        tokens = usage.get("cache_read_input_tokens", 0)
        return f"cache-bump: bumped, {tokens} tokens read from cache"
    if outcome == "cache_write":
        return "cache-bump: bumped, but the cache had already expired (paid a full write)"
    if outcome == "unauthorized":
        return "cache-bump: bump failed, credential rejected (session stopped)"
    if outcome == "rejected":
        return "cache-bump: bump failed, upstream rejected the replay (session stopped)"
    if outcome == "transient":
        return "cache-bump: bump failed, transient upstream error, try again shortly"
    error = result.get("error")
    if error:
        return f"cache-bump: bump not possible right now ({error})"
    return "cache-bump: bump failed, unrecognised response from daemon"


def _handle_command(client: ControlClient, session_id: str, argument: str, now: float) -> str:
    """Dispatch the parsed ``/cache-bump`` argument and return the reason line."""
    parts = argument.split()
    action = parts[0].lower() if parts else "status"
    if action == "on":
        max_duration_s = _parse_duration(parts[1]) if len(parts) > 1 else None
        session = client.set_session(session_id, True, max_duration_s=max_duration_s)
        return _format_on_reason(session, now)
    if action == "off":
        client.set_session(session_id, False)
        return "cache-bump: off"
    if action == "now":
        result = client.bump(session_id)
        return _format_now_reason(result)
    if action == "status":
        try:
            session = client.get_session(session_id)
        except ApiError as exc:
            if exc.status == 404:
                return "cache-bump: off"
            raise
        if session.get("enabled"):
            return _format_on_reason(session, now)
        stopped_reason = session.get("stopped_reason")
        if stopped_reason:
            return f"cache-bump: off, stopped: {stopped_reason}"
        return "cache-bump: off"
    return f"cache-bump: unknown command {action!r} (use on, off, status or now)"


def run_user_prompt_submit(stdin: TextIO, stdout: TextIO, environ: Mapping[str, str]) -> int:
    """Read the ``UserPromptSubmit`` hook JSON from ``stdin`` and handle ``/cache-bump``.

    Returns 0 unconditionally once a ``/cache-bump`` prompt has been
    recognised (so the user sees a reason instead of a hook failure), and 0
    silently for any prompt that does not match or for unparseable input.
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
    argument = stripped[len(_COMMAND_PREFIX) :].strip()
    session_id = payload.get("session_id")

    config = Config.from_env(environ)
    reason: str
    if not isinstance(session_id, str) or not session_id:
        reason = "cache-bump: no session id in hook payload"
    else:
        client = ControlClient(config.url)
        try:
            reason = _handle_command(client, session_id, argument, time.time())
        except DaemonUnreachable:
            reason = f"cache-bump: daemon not reachable at {config.url} (is ccbg serve running?)"
        except ApiError as exc:
            reason = f"cache-bump: daemon error ({exc.status})"
        except Exception as exc:
            reason = f"cache-bump: unexpected error ({exc})"

    stdout.write(json.dumps({"decision": "block", "reason": reason}))
    stdout.write("\n")
    return 0


__all__ = ["run_user_prompt_submit"]
