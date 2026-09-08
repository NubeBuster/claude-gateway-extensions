"""Contract tests: the CLI's ``ControlClient`` against the real aiohttp app.

Every other client test answers from a hand-written fake server, so it can
only prove the client is self-consistent. These drive the blocking
:class:`~claude_cache_bump_gateway.cli.client.ControlClient` (in a worker
thread, since it is synchronous ``urllib``) against the actual application
built by ``server.app.make_app`` on an ephemeral port, so a divergence
between the client's assumptions and ``docs/API.md`` fails here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

import pytest
from aiohttp import web

from claude_cache_bump_gateway.cli.client import ApiError, ControlClient
from conftest import ServerHarness

T = TypeVar("T")

#: The status-dict keys ``docs/API.md`` documents for GET/PUT /ccbg/sessions[/{id}].
STATUS_KEYS = {
    "session_id",
    "enabled",
    "interval_s",
    "max_duration_s",
    "until",
    "last_request_at",
    "last_bump_at",
    "bump_count",
    "stopped_reason",
    "next_bump_at",
    "streams",
    "last_result",
}


async def _off_loop(call: Callable[[], T]) -> T:
    """Run a blocking ``ControlClient`` call in a worker thread."""
    return await asyncio.get_running_loop().run_in_executor(None, call)


def _client(harness: ServerHarness) -> ControlClient:
    """Build a :class:`ControlClient` pointed at the harness's real listener."""
    return ControlClient(str(harness.client.make_url("")), timeout=5.0)


async def test_health_and_empty_list(harness: ServerHarness) -> None:
    client = _client(harness)
    health = await _off_loop(client.health)
    assert health["ok"] is True
    assert health["sessions"] == 0
    assert await _off_loop(client.list_sessions) == []


async def test_set_get_and_list_round_trip(harness: ServerHarness) -> None:
    client = _client(harness)
    created = await _off_loop(
        lambda: client.set_session("sess-a", True, interval_s=60, max_duration_s=7200)
    )
    assert set(created) == STATUS_KEYS
    assert created["session_id"] == "sess-a"
    assert created["enabled"] is True
    assert created["interval_s"] == 60
    assert created["max_duration_s"] == 7200

    fetched = await _off_loop(lambda: client.get_session("sess-a"))
    assert set(fetched) == STATUS_KEYS
    assert fetched["session_id"] == "sess-a"
    assert fetched["interval_s"] == 60

    listed = await _off_loop(client.list_sessions)
    assert [s["session_id"] for s in listed] == ["sess-a"]
    assert set(listed[0]) == STATUS_KEYS

    disabled = await _off_loop(lambda: client.set_session("sess-a", False))
    assert disabled["enabled"] is False


async def test_get_unknown_session_raises_404(harness: ServerHarness) -> None:
    client = _client(harness)
    with pytest.raises(ApiError) as exc_info:
        await _off_loop(lambda: client.get_session("never-seen"))
    assert exc_info.value.status == 404


async def test_bump_without_snapshot_is_a_normal_outcome(harness: ServerHarness) -> None:
    client = _client(harness)
    await _off_loop(lambda: client.set_session("sess-empty", True))
    result = await _off_loop(lambda: client.bump("sess-empty"))
    assert result == {"error": "no_snapshot"}


async def test_bump_returns_a_bump_result(harness: ServerHarness) -> None:
    async def messages_handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 5}})

    harness.fake.script(messages_handler)
    resp = await harness.client.post(
        "/v1/messages",
        data=b'{"model": "claude-x", "max_tokens": 10, "messages": []}',
        headers={"x-claude-code-session-id": "sess-b", "authorization": "Bearer a"},
    )
    assert resp.status == 200

    async def bump_handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 1, "cache_read_input_tokens": 4096}})

    harness.fake.script(bump_handler)
    client = _client(harness)
    result: dict[str, Any] = await _off_loop(lambda: client.bump("sess-b"))
    assert result["outcome"] == "ok"
    assert result["usage"]["cache_read_input_tokens"] == 4096


async def test_forget_removes_the_session(harness: ServerHarness) -> None:
    client = _client(harness)
    await _off_loop(lambda: client.set_session("sess-c", True))
    await _off_loop(lambda: client.forget("sess-c"))
    assert await _off_loop(client.list_sessions) == []
