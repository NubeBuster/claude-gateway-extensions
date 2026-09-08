"""Tests for claude_cache_bump_gateway.server.passthrough via a real fake upstream."""

from __future__ import annotations

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_cache_bump_gateway.server.app import make_app
from claude_cache_bump_gateway.server.keys import STORE_KEY
from conftest import ServerHarness, make_config


async def test_forwards_method_path_query_and_headers(harness: ServerHarness) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    harness.fake.script(handler)

    resp = await harness.client.get(
        "/v1/models?limit=5",
        headers={
            "authorization": "Bearer secret",
            "anthropic-beta": "prompt-caching-2024-07-31",
            "x-custom": "value",
        },
    )
    assert resp.status == 200
    assert await resp.json() == {"ok": True}

    recorded = harness.fake.requests[0]
    assert recorded.method == "GET"
    assert recorded.path == "/v1/models"
    assert recorded.query == "limit=5"
    assert recorded.headers["authorization"] == "Bearer secret"
    assert recorded.headers["anthropic-beta"] == "prompt-caching-2024-07-31"
    assert recorded.headers["x-custom"] == "value"
    assert "connection" not in recorded.headers


async def test_forwards_body_unchanged(harness: ServerHarness) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=201, body=b"created")

    harness.fake.script(handler)

    payload = b'{"foo": "bar", "n": 1}'
    resp = await harness.client.post("/v1/other", data=payload)
    assert resp.status == 201
    assert await resp.read() == b"created"
    assert harness.fake.requests[0].body == payload


async def test_upstream_error_status_and_body_relayed(harness: ServerHarness) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {"error": {"type": "not_found_error", "message": "nope"}}, status=404
        )

    harness.fake.script(handler)

    resp = await harness.client.get("/v1/models/nonexistent")
    assert resp.status == 404
    data = await resp.json()
    assert data["error"]["type"] == "not_found_error"


async def test_sse_streamed_incrementally(harness: ServerHarness) -> None:
    release = asyncio.Event()

    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b'data: {"type": "message_start", "message": {"usage": {}}}\n\n')
        await release.wait()
        await resp.write(b'data: {"type": "message_delta", "usage": {}}\n\n')
        await resp.write_eof()
        return resp

    harness.fake.script(handler)

    async with harness.client.request(
        "POST",
        "/v1/messages",
        headers={"x-claude-code-session-id": "sess-sse"},
        data=b'{"model": "claude-x", "max_tokens": 10, "messages": []}',
    ) as resp:
        first = await asyncio.wait_for(resp.content.readany(), timeout=2)
        assert b"message_start" in first
        release.set()
        rest = await asyncio.wait_for(resp.content.read(), timeout=2)
        assert b"message_delta" in rest
        assert resp.status == 200


async def test_tee_extracts_usage_from_sse_and_snapshots(harness: ServerHarness) -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(
            b'data: {"type": "message_start", "message": {"usage": '
            b'{"input_tokens": 100, "cache_read_input_tokens": 50}}}\n\n'
        )
        await resp.write(b'data: {"type": "message_delta", "usage": {"output_tokens": 20}}\n\n')
        await resp.write_eof()
        return resp

    harness.fake.script(handler)

    body = b'{"model": "claude-x", "max_tokens": 10, "messages": []}'
    resp = await harness.client.post(
        "/v1/messages",
        data=body,
        headers={"x-claude-code-session-id": "sess-a", "authorization": "Bearer tok"},
    )
    assert resp.status == 200
    await resp.read()

    store = harness.app[STORE_KEY]
    snaps = store.get_snapshots("sess-a")
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap.model == "claude-x"
    assert snap.usage.input_tokens == 100
    assert snap.usage.cache_read_input_tokens == 50
    assert snap.usage.output_tokens == 20
    assert snap.body == body
    assert snap.headers.get("authorization") == "Bearer tok"


async def test_tee_extracts_usage_from_json(harness: ServerHarness) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "id": "msg_1",
                "usage": {
                    "input_tokens": 30,
                    "output_tokens": 15,
                    "cache_creation_input_tokens": 5,
                },
            }
        )

    harness.fake.script(handler)

    body = b'{"model": "claude-y", "max_tokens": 10, "messages": []}'
    resp = await harness.client.post(
        "/v1/messages", data=body, headers={"x-claude-code-session-id": "sess-b"}
    )
    assert resp.status == 200

    store = harness.app[STORE_KEY]
    snaps = store.get_snapshots("sess-b")
    assert len(snaps) == 1
    assert snaps[0].usage.input_tokens == 30
    assert snaps[0].usage.output_tokens == 15
    assert snaps[0].usage.cache_creation_input_tokens == 5


async def test_no_snapshot_without_session_header(harness: ServerHarness) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 1}})

    harness.fake.script(handler)

    body = b'{"model": "claude-z", "max_tokens": 10, "messages": []}'
    resp = await harness.client.post("/v1/messages", data=body)
    assert resp.status == 200

    store = harness.app[STORE_KEY]
    assert store.list_sessions() == []


async def test_non_messages_path_never_snapshotted(harness: ServerHarness) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 1}})

    harness.fake.script(handler)

    resp = await harness.client.post(
        "/v1/complete",
        data=b'{"model": "claude-z"}',
        headers={"x-claude-code-session-id": "sess-c"},
    )
    assert resp.status == 200

    store = harness.app[STORE_KEY]
    assert store.get_state("sess-c") is None
    assert store.get_snapshots("sess-c") == []


async def test_bump_now_hits_fake_with_max_tokens_zero(harness: ServerHarness) -> None:
    async def capture_handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 5}})

    harness.fake.script(capture_handler)

    body = b'{"model": "claude-x", "max_tokens": 999, "messages": [], "stream": true}'
    resp = await harness.client.post(
        "/v1/messages",
        data=body,
        headers={
            "x-claude-code-session-id": "sess-bump",
            "authorization": "Bearer initial",
            "anthropic-beta": "prompt-caching-2024-07-31",
        },
    )
    assert resp.status == 200

    put_resp = await harness.client.put("/ccbg/sessions/sess-bump", json={"enabled": True})
    assert put_resp.status == 200
    assert (await put_resp.json())["enabled"] is True

    async def bump_handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 5, "cache_read_input_tokens": 100}})

    harness.fake.script(bump_handler)

    bump_resp = await harness.client.post("/ccbg/sessions/sess-bump/bump")
    assert bump_resp.status == 200
    assert (await bump_resp.json())["outcome"] == "ok"

    assert len(harness.fake.requests) == 2
    replay = harness.fake.requests[1]
    assert replay.path == "/v1/messages"
    replay_body = json.loads(replay.body)
    assert replay_body["max_tokens"] == 0
    assert replay_body["stream"] is False
    assert replay.headers["authorization"] == "Bearer initial"
    assert replay.headers["anthropic-beta"] == "prompt-caching-2024-07-31"
    assert "connection" not in replay.headers


async def test_upstream_unreachable_returns_502(tmp_path) -> None:
    dead_server = TestServer(web.Application())
    await dead_server.start_server()
    dead_url = str(dead_server.make_url(""))
    await dead_server.close()

    config = make_config(tmp_path, dead_url)
    app = make_app(config)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/v1/models")
        assert resp.status == 502
        data = await resp.json()
        assert data["error"]["type"] == "ccbg_upstream_unreachable"
    finally:
        await client.close()
