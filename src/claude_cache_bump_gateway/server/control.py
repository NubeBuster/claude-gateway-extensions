"""Control API routes served locally under ``/ccbg/``.

Full request/response contract: ``docs/API.md``. Design record: the
"Control API on ``/ccbg/``..." decision in ``docs/DESIGN.md`` "Decisions".
These routes must be registered before
:func:`claude_cache_bump_gateway.server.passthrough.register`'s catch-all so
``/ccbg/*`` is served locally rather than forwarded.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from aiohttp import web

from claude_cache_bump_gateway import __version__
from claude_cache_bump_gateway.config import Config
from claude_cache_bump_gateway.core.scheduler import NoSnapshot, Scheduler
from claude_cache_bump_gateway.core.store import SESSION_ID_RE, SnapshotStore
from claude_cache_bump_gateway.server.keys import CONFIG_KEY, SCHEDULER_KEY, STORE_KEY


def _invalid_session_id() -> web.Response:
    return web.json_response({"error": "invalid_session_id"}, status=400)


def _unknown_session() -> web.Response:
    return web.json_response({"error": "unknown_session"}, status=404)


async def health(request: web.Request) -> web.Response:
    """``GET /ccbg/health``: liveness, version, upstream target and session count."""
    store: SnapshotStore = request.app[STORE_KEY]
    config: Config = request.app[CONFIG_KEY]
    return web.json_response(
        {
            "ok": True,
            "version": __version__,
            "upstream": config.upstream,
            "sessions": len(store.list_sessions()),
        }
    )


async def list_sessions(request: web.Request) -> web.Response:
    """``GET /ccbg/sessions``: status dicts for every known session."""
    scheduler: Scheduler = request.app[SCHEDULER_KEY]
    return web.json_response({"sessions": scheduler.status_all()})


async def get_session(request: web.Request) -> web.Response:
    """``GET /ccbg/sessions/{id}``: one session's status dict, or 404."""
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
    """``PUT /ccbg/sessions/{id}``: set enabled/interval/max_duration/until, return status."""
    session_id = request.match_info["id"]
    if not SESSION_ID_RE.match(session_id):
        return _invalid_session_id()

    body = await _read_json_object(request)
    if body is None or "enabled" not in body or not isinstance(body["enabled"], bool):
        return web.json_response({"error": "invalid_body"}, status=400)

    interval_s = body.get("interval_s")
    if not _is_optional_positive_int(interval_s):
        return web.json_response({"error": "invalid_body"}, status=400)
    max_duration_s = body.get("max_duration_s")
    if not _is_optional_positive_int(max_duration_s):
        return web.json_response({"error": "invalid_body"}, status=400)
    until = body.get("until")
    if not _is_optional_number(until):
        return web.json_response({"error": "invalid_body"}, status=400)

    scheduler: Scheduler = request.app[SCHEDULER_KEY]
    scheduler.set_enabled(session_id, body["enabled"], interval_s, max_duration_s, until)
    return web.json_response(scheduler.status(session_id))


async def bump_session(request: web.Request) -> web.Response:
    """``POST /ccbg/sessions/{id}/bump``: replay the session's snapshot once now."""
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


async def delete_session(request: web.Request) -> web.Response:
    """``DELETE /ccbg/sessions/{id}``: forget the session's state and snapshots."""
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
    """Register every ``/ccbg/*`` route on ``app``."""
    app.router.add_get("/ccbg/health", health)
    app.router.add_get("/ccbg/sessions", list_sessions)
    app.router.add_get("/ccbg/sessions/{id}", get_session)
    app.router.add_put("/ccbg/sessions/{id}", put_session)
    app.router.add_post("/ccbg/sessions/{id}/bump", bump_session)
    app.router.add_delete("/ccbg/sessions/{id}", delete_session)
