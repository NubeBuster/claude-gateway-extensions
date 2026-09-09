"""Contract tests: the CLI's ``ControlClient`` against the real aiohttp app.

Every other client test answers from a hand-written fake server, so it can
only prove the client is self-consistent. These drive the blocking
:class:`~claude_gateway_extensions.cli.client.ControlClient` (in a worker
thread, since it is synchronous ``urllib``) against the actual application
built by ``server.app.make_app`` on an ephemeral port, so a divergence
between the client's assumptions and ``docs/API.md`` fails here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, TypeVar

import pytest
from aiohttp import web

from claude_gateway_extensions.cli.client import ApiError, ControlClient
from conftest import ServerHarness

T = TypeVar("T")

#: The status-dict keys ``docs/API.md`` documents for GET/PUT /cgx/sessions[/{id}].
STATUS_KEYS = {
    "session_id",
    "enabled",
    "interval_s",
    "max_duration_s",
    "until",
    "last_request_at",
    "last_bump_at",
    "bump_count",
    "replays_seen",
    "stopped_reason",
    "next_bump_at",
    "cache_expires_at",
    "streams",
    "last_result",
    "rewarm",
    "rewarm_write",
    "formatted",
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


#: The response keys ``docs/API.md`` documents for POST /cgx/sessions/{id}/fork.
FORK_KEYS = {
    "verdict",
    "reason",
    "mutation",
    "usage",
    "paid_write",
    "model",
    "stream_key",
    "subagent_snapshot",
}


async def test_fork_invalid_session_id_rejected(harness: ServerHarness) -> None:
    resp = await harness.client.post(
        "/cgx/sessions/bad*id/fork", json={"prompt": "review", "hook_event": "x", "payload": {}}
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_session_id"


async def test_fork_missing_prompt_rejected(harness: ServerHarness) -> None:
    resp = await harness.client.post(
        "/cgx/sessions/sess-f/fork", json={"hook_event": "x", "payload": {}}
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_body"


async def test_fork_non_str_prompt_rejected(harness: ServerHarness) -> None:
    resp = await harness.client.post(
        "/cgx/sessions/sess-f/fork", json={"prompt": 5, "hook_event": "x", "payload": {}}
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_body"


async def test_fork_non_positive_max_tokens_rejected(harness: ServerHarness) -> None:
    resp = await harness.client.post(
        "/cgx/sessions/sess-f/fork",
        json={"prompt": "review", "hook_event": "x", "payload": {}, "max_tokens": 0},
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_body"


async def test_fork_empty_string_model_rejected(harness: ServerHarness) -> None:
    resp = await harness.client.post(
        "/cgx/sessions/sess-f/fork",
        json={"prompt": "review", "hook_event": "x", "payload": {}, "model": ""},
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_body"


async def test_fork_non_str_model_rejected(harness: ServerHarness) -> None:
    resp = await harness.client.post(
        "/cgx/sessions/sess-f/fork",
        json={"prompt": "review", "hook_event": "x", "payload": {}, "model": 5},
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_body"


async def test_fork_without_snapshot_is_200_ok_no_snapshot(harness: ServerHarness) -> None:
    resp = await harness.client.post(
        "/cgx/sessions/sess-empty2/fork",
        json={"prompt": "review", "hook_event": "x", "payload": {}},
    )
    assert resp.status == 200
    body = await resp.json()
    assert set(body) == FORK_KEYS
    assert body == {
        "verdict": "ok",
        "reason": "no_snapshot",
        "mutation": None,
        "usage": None,
        "paid_write": False,
        "model": None,
        "stream_key": None,
        "subagent_snapshot": False,
    }


async def test_fork_returns_the_verdict_shape(harness: ServerHarness) -> None:
    async def messages_handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 5}})

    harness.fake.script(messages_handler)
    # A system[0] attribution block without the subagent marker: this is
    # main-conversation traffic, so the fork must not need the fallback
    # path (see the "never a subagent snapshot" selection preference).
    request_body = json.dumps(
        {
            "model": "claude-x",
            "max_tokens": 10,
            "system": [
                {"type": "text", "text": "x-anthropic-billing-header: cc_is_subagent=false"}
            ],
            "messages": [],
        }
    ).encode()
    resp = await harness.client.post(
        "/v1/messages",
        data=request_body,
        headers={"x-claude-code-session-id": "sess-fork", "authorization": "Bearer a"},
    )
    assert resp.status == 200

    async def fork_handler(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "content": [
                    {"type": "text", "text": json.dumps({"verdict": "ok", "reason": "fine"})}
                ],
                "usage": {"input_tokens": 3, "cache_read_input_tokens": 4096},
            }
        )

    harness.fake.script(fork_handler)
    resp = await harness.client.post(
        "/cgx/sessions/sess-fork/fork",
        json={"prompt": "review this", "hook_event": "PreToolUse", "payload": {"tool": "Bash"}},
    )
    assert resp.status == 200
    body = await resp.json()
    assert set(body) == FORK_KEYS
    assert body["verdict"] == "ok"
    assert body["reason"] == "fine"
    assert body["mutation"] is None
    assert body["paid_write"] is False
    assert body["usage"]["cache_read_input_tokens"] == 4096
    assert body["model"] == "claude-x"
    assert body["stream_key"] is not None
    assert body["subagent_snapshot"] is False
