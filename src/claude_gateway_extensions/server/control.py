"""Control API routes served locally under ``/cgx/``.

Full request/response contract: ``docs/API.md``. Design record: the
"Control API on ``/cgx/``..." decision in ``docs/DESIGN.md`` "Decisions".
These routes must be registered before
:func:`claude_gateway_extensions.server.passthrough.register`'s catch-all so
``/cgx/*`` is served locally rather than forwarded.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from aiohttp import web

from claude_gateway_extensions import __version__
from claude_gateway_extensions.config import SHADOW_FORK_ENV_VARS, Config, ShadowForkSettings
from claude_gateway_extensions.core.clock import Clock
from claude_gateway_extensions.core.scheduler import NoSnapshot, Scheduler
from claude_gateway_extensions.core.status_format import format_local_datetime
from claude_gateway_extensions.core.store import SESSION_ID_RE, SnapshotStore
from claude_gateway_extensions.server.keys import (
    CLOCK_KEY,
    CONFIG_KEY,
    DRAIN_STATE_KEY,
    GIT_PROVENANCE_KEY,
    SCHEDULER_KEY,
    STARTED_AT_KEY,
    STORE_KEY,
)


def _invalid_session_id() -> web.Response:
    return web.json_response({"error": "invalid_session_id"}, status=400)


def _unknown_session() -> web.Response:
    return web.json_response({"error": "unknown_session"}, status=404)


def _invalid_body() -> web.Response:
    return web.json_response({"error": "invalid_body"}, status=400)


async def health(request: web.Request) -> web.Response:
    """``GET /cgx/health``: liveness, version, upstream target and session count.

    Answers ``503 {"status": "draining"}`` instead once a shutdown signal has
    put the daemon into its drain window (see
    :data:`~claude_gateway_extensions.server.keys.DRAIN_STATE_KEY`): it is
    still finishing in-flight requests, but must not be treated as a healthy
    target for new traffic (e.g. by ``cgx serve --handover``'s own probe of
    the daemon it is about to replace).
    """
    drain_state = request.app[DRAIN_STATE_KEY]
    if drain_state.draining:
        return web.json_response({"status": "draining"}, status=503)
    store: SnapshotStore = request.app[STORE_KEY]
    config: Config = request.app[CONFIG_KEY]
    clock: Clock = request.app[CLOCK_KEY]
    started_at = request.app[STARTED_AT_KEY]
    now = clock.now()
    provenance = request.app[GIT_PROVENANCE_KEY]
    body: dict[str, Any] = {
        "ok": True,
        "version": __version__,
        "upstream": config.upstream,
        "sessions": len(store.list_sessions()),
        "started_at": started_at,
        "started_local": format_local_datetime(started_at),
        "uptime_seconds": max(0.0, now - started_at),
    }
    if provenance is not None:
        body["commit"] = provenance["commit"]
        body["branch"] = provenance["branch"]
        body["dirty"] = provenance["dirty"]
    return web.json_response(body)


#: Maps each :class:`ShadowForkSettings` field name to the ``Config`` field
#: holding its env/default-resolved value, for :func:`get_config`.
_SHADOW_FORK_CONFIG_FIELDS: dict[str, str] = {
    "max_tokens": "shadow_fork_max_tokens",
    "max_per_min": "shadow_fork_max_per_min",
    "thinking_guard": "shadow_fork_thinking_guard",
    "model": "shadow_fork_model",
}


def _shadow_fork_report(config: Config, file_settings: ShadowForkSettings) -> dict[str, Any]:
    """Build the ``shadow_fork`` object for ``GET /cgx/config``: each key's value and source.

    ``source`` is ``"file"`` when the tuning file sets that key, else
    ``"env"`` when the corresponding ``CGX_SHADOW_FORK_*`` variable was
    actually set in the environment (per ``Config.configured_env_vars``),
    else ``"default"``.
    """
    report: dict[str, Any] = {}
    for key, config_field in _SHADOW_FORK_CONFIG_FIELDS.items():
        file_value = getattr(file_settings, key)
        env_var = SHADOW_FORK_ENV_VARS[key]
        if file_value is not None:
            report[key] = {"value": file_value, "source": "file"}
        elif env_var in config.configured_env_vars:
            report[key] = {"value": getattr(config, config_field), "source": "env"}
        else:
            report[key] = {"value": getattr(config, config_field), "source": "default"}
    return report


async def get_config(request: web.Request) -> web.Response:
    """``GET /cgx/config``: the Shadow Fork tuning file's resolved path and effective values.

    Reports the resolved ``CGX_CONFIG_FILE`` path, whether it is present,
    its parse error if the last attempt to load it failed, and each of the
    four Shadow Fork keys' effective value with its source (``file``,
    ``env`` or ``default``) -- see the "Shadow Fork tuning is hot
    reloaded..." decision in ``docs/DESIGN.md``.
    """
    config: Config = request.app[CONFIG_KEY]
    scheduler: Scheduler = request.app[SCHEDULER_KEY]
    state = scheduler.config_file_state()
    return web.json_response(
        {
            "path": str(state.path),
            "present": state.present,
            "parse_error": state.parse_error,
            "shadow_fork": _shadow_fork_report(config, state.settings),
        }
    )


async def list_sessions(request: web.Request) -> web.Response:
    """``GET /cgx/sessions``: status dicts for every known session."""
    scheduler: Scheduler = request.app[SCHEDULER_KEY]
    return web.json_response({"sessions": scheduler.status_all()})


async def get_session(request: web.Request) -> web.Response:
    """``GET /cgx/sessions/{id}``: one session's status dict, or 404."""
    session_id = request.match_info["id"]
    if not SESSION_ID_RE.match(session_id):
        return _invalid_session_id()
    scheduler: Scheduler = request.app[SCHEDULER_KEY]
    try:
        status = scheduler.status(session_id)
    except KeyError:
        return _unknown_session()
    return web.json_response(status)


async def put_session(request: web.Request) -> web.Response:
    """``PUT /cgx/sessions/{id}``: set enabled/interval/max_duration/until, return status."""
    session_id = request.match_info["id"]
    if not SESSION_ID_RE.match(session_id):
        return _invalid_session_id()

    body = await _read_json_object(request)
    if body is None or "enabled" not in body or not isinstance(body["enabled"], bool):
        return _invalid_body()

    interval_s = body.get("interval_s")
    if not _is_optional_positive_int(interval_s):
        return _invalid_body()
    max_duration_s = body.get("max_duration_s")
    if not _is_optional_positive_int(max_duration_s):
        return _invalid_body()
    until = body.get("until")
    if not _is_optional_number(until):
        return _invalid_body()
    rewarm = body.get("rewarm", False)
    if not isinstance(rewarm, bool):
        return _invalid_body()

    scheduler: Scheduler = request.app[SCHEDULER_KEY]
    scheduler.set_enabled(session_id, body["enabled"], interval_s, max_duration_s, until, rewarm)
    return web.json_response(scheduler.status(session_id))


async def bump_session(request: web.Request) -> web.Response:
    """``POST /cgx/sessions/{id}/bump``: replay the session's snapshot once now."""
    session_id = request.match_info["id"]
    if not SESSION_ID_RE.match(session_id):
        return _invalid_session_id()

    scheduler: Scheduler = request.app[SCHEDULER_KEY]
    try:
        result = await scheduler.bump_now(session_id)
    except NoSnapshot:
        return web.json_response({"error": "no_snapshot"}, status=404)
    status_code = 200 if result.outcome == "ok" else 409
    return web.json_response(dataclasses.asdict(result), status=status_code)


async def fork_session(request: web.Request) -> web.Response:
    """``POST /cgx/sessions/{id}/shadow-fork``: run one shadow fork now.

    Always answers ``200`` with the verdict shape, including for the
    fail-open degradations (``no_snapshot``, ``rate_limited``,
    ``unparseable_verdict``, ``fork_failed``): a non-200 there would push
    fail-open logic into every client. ``hook_event`` is accepted for
    forward compatibility and ignored server-side: the handler never reads
    it, so it reaches neither the logs nor the engine.
    """
    session_id = request.match_info["id"]
    if not SESSION_ID_RE.match(session_id):
        return _invalid_session_id()

    body = await _read_json_object(request)
    if body is None:
        return _invalid_body()
    prompt = body.get("prompt")
    if not isinstance(prompt, str):
        return _invalid_body()
    max_tokens = body.get("max_tokens")
    if not _is_optional_positive_int(max_tokens):
        return _invalid_body()
    model = body.get("model")
    if not _is_optional_nonempty_str(model):
        return _invalid_body()
    payload = body.get("payload")

    scheduler: Scheduler = request.app[SCHEDULER_KEY]
    verdict = await scheduler.fork_now(session_id, prompt, payload, max_tokens, model)
    return web.json_response(
        {
            "verdict": verdict.verdict,
            "reason": verdict.reason,
            "mutation": verdict.mutation,
            "usage": dataclasses.asdict(verdict.usage) if verdict.usage is not None else None,
            "paid_write": verdict.paid_write,
            "model": verdict.model,
            "stream_key": verdict.stream_key,
            "subagent_snapshot": verdict.subagent_snapshot,
        }
    )


async def delete_session(request: web.Request) -> web.Response:
    """``DELETE /cgx/sessions/{id}``: forget the session's state and snapshots."""
    session_id = request.match_info["id"]
    if not SESSION_ID_RE.match(session_id):
        return _invalid_session_id()

    scheduler: Scheduler = request.app[SCHEDULER_KEY]
    scheduler.forget(session_id)
    return web.Response(status=204)


async def _read_json_object(request: web.Request) -> dict[str, Any] | None:
    """Return the request body parsed as a JSON object, or ``None`` if invalid."""
    raw = await request.read()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _is_optional_nonempty_str(value: object) -> bool:
    """Return ``True`` if ``value`` is ``None`` or a non-empty ``str``.

    Used for the fork request's optional ``model`` override: an empty
    string is never a model id a caller meant to send.
    """
    return value is None or (isinstance(value, str) and value != "")


def _is_optional_positive_int(value: object) -> bool:
    """Return ``True`` if ``value`` is ``None`` or a plain positive ``int`` (not ``bool``).

    Zero and negatives are rejected rather than clamped: an ``interval_s`` of
    ``0`` makes the scheduler bump on every tick, so a typo in a control-API
    call would spend a cache read per second for as long as the session lives.
    """
    if value is None:
        return True
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_optional_number(value: object) -> bool:
    """Return ``True`` if ``value`` is ``None`` or a plain ``int``/``float`` (not ``bool``).

    Used for ``until``, an absolute Unix epoch timestamp: unlike
    ``interval_s``/``max_duration_s`` it is not a duration, so zero or a
    past value is not rejected here — a deadline already in the past just
    stops the session on its next scheduler tick, which is a normal (if
    immediate) outcome, not a client error.
    """
    if value is None:
        return True
    return isinstance(value, int | float) and not isinstance(value, bool)


def register(app: web.Application) -> None:
    """Register every ``/cgx/*`` route on ``app``."""
    app.router.add_get("/cgx/health", health)
    app.router.add_get("/cgx/config", get_config)
    app.router.add_get("/cgx/sessions", list_sessions)
    app.router.add_get("/cgx/sessions/{id}", get_session)
    app.router.add_put("/cgx/sessions/{id}", put_session)
    app.router.add_post("/cgx/sessions/{id}/bump", bump_session)
    app.router.add_post("/cgx/sessions/{id}/shadow-fork", fork_session)
    app.router.add_delete("/cgx/sessions/{id}", delete_session)
