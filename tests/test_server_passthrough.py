"""Tests for claude_gateway_extensions.server.passthrough via a real fake upstream."""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_gateway_extensions.server.app import make_app
from claude_gateway_extensions.server.keys import STORE_KEY
from conftest import ServerHarness, make_config


@pytest.mark.invariant(decision="Byte-transparent forwarding")
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


@pytest.mark.invariant(decision="Every non-/cgx/ path is tunnelled verbatim")
async def test_arbitrary_path_tunnelled_verbatim_and_never_snapshotted(
    harness: ServerHarness,
) -> None:
    """A request to an unfamiliar path -- e.g. a chained upstream's own control call --

    reaches the fake upstream with its method, path, query and body intact,
    and is never captured as a snapshot: only /v1/messages is tee'd.
    """

    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    harness.fake.script(handler)

    payload = b'{"weird": "shape", "n": 1}'
    resp = await harness.client.post(
        "/_some-other-proxy/control/reload?scope=all",
        data=payload,
        headers={"x-claude-code-session-id": "sess-tunnel"},
    )
    assert resp.status == 200

    recorded = harness.fake.requests[0]
    assert recorded.method == "POST"
    assert recorded.path == "/_some-other-proxy/control/reload"
    assert recorded.query == "scope=all"
    assert recorded.body == payload

    store = harness.app[STORE_KEY]
    assert store.get_snapshots("sess-tunnel") == []


@pytest.mark.invariant(decision="Byte-transparent forwarding")
async def test_forwards_repeated_header_without_collapsing(harness: ServerHarness) -> None:
    """A repeated request header must reach upstream with every value intact."""
    seen: list[list[str]] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append(request.headers.getall("cookie"))
        return web.json_response({"ok": True})

    harness.fake.script(handler)

    async with harness.client.get(
        "/v1/models",
        headers=(("cookie", "a=1"), ("cookie", "b=2")),
    ) as resp:
        assert resp.status == 200

    assert seen == [["a=1", "b=2"]]


@pytest.mark.invariant(decision="Byte-transparent forwarding")
async def test_forwards_body_unchanged(harness: ServerHarness) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=201, body=b"created")

    harness.fake.script(handler)

    payload = b'{"foo": "bar", "n": 1}'
    resp = await harness.client.post("/v1/other", data=payload)
    assert resp.status == 201
    assert await resp.read() == b"created"
    assert harness.fake.requests[0].body == payload


@pytest.mark.invariant(decision="Byte-transparent forwarding")
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


@pytest.mark.invariant(decision="Byte-transparent forwarding")
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


@pytest.mark.invariant(decision="Tee only the private copy")
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
    assert snap.is_subagent is None


async def test_capture_reads_is_subagent_from_the_attribution_block(
    harness: ServerHarness,
) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 5}})

    harness.fake.script(handler)

    body = json.dumps(
        {
            "model": "claude-x",
            "max_tokens": 10,
            "system": [
                {"type": "text", "text": "x-anthropic-billing-header: cc_is_subagent=true;x=1"}
            ],
            "messages": [],
        }
    ).encode()
    resp = await harness.client.post(
        "/v1/messages", data=body, headers={"x-claude-code-session-id": "sess-sub"}
    )
    assert resp.status == 200

    store = harness.app[STORE_KEY]
    snaps = store.get_snapshots("sess-sub")
    assert len(snaps) == 1
    assert snaps[0].is_subagent is True


@pytest.mark.invariant(decision="Tee only the private copy")
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


@pytest.mark.invariant(decision="Session identity is x-claude-code-session-id")
async def test_no_snapshot_without_session_header(harness: ServerHarness) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 1}})

    harness.fake.script(handler)

    body = b'{"model": "claude-z", "max_tokens": 10, "messages": []}'
    resp = await harness.client.post("/v1/messages", data=body)
    assert resp.status == 200

    store = harness.app[STORE_KEY]
    assert store.list_sessions() == []


@pytest.mark.invariant(decision="Tee only the private copy")
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


@pytest.mark.invariant(decision="Byte-transparent forwarding")
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

    put_resp = await harness.client.put("/cgx/sessions/sess-bump", json={"enabled": True})
    assert put_resp.status == 200
    assert (await put_resp.json())["enabled"] is True

    async def bump_handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 5, "cache_read_input_tokens": 100}})

    harness.fake.script(bump_handler)

    bump_resp = await harness.client.post("/cgx/sessions/sess-bump/bump")
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


async def test_client_disconnect_mid_stream_logs_one_line_no_traceback(
    harness: ServerHarness, caplog
) -> None:
    """A client cancelling mid-SSE must log one INFO line, never a traceback.

    ``handler_cancellation`` (enabled by aiohttp's test harness, and possible
    in some real deployments) delivers ``asyncio.CancelledError`` to the
    streaming loop when the client disconnects. That must be logged as an
    ordinary disconnect, not swallowed, and not left to surface as an
    unhandled exception traceback.
    """
    hang = asyncio.Event()
    chunk_seen = asyncio.Event()

    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b'data: {"type": "message_start", "message": {"usage": {}}}\n\n')
        await hang.wait()
        return resp

    harness.fake.script(handler)

    async def do_request() -> None:
        async with harness.client.request(
            "POST",
            "/v1/messages",
            headers={"x-claude-code-session-id": "sess-cancel"},
            data=b'{"model": "claude-x", "max_tokens": 10, "messages": []}',
        ) as resp:
            first = await resp.content.readany()
            assert b"message_start" in first
            chunk_seen.set()
            await asyncio.Event().wait()

    caplog.set_level(logging.INFO, logger="claude_gateway_extensions.server.passthrough")
    task = asyncio.ensure_future(do_request())
    await asyncio.wait_for(chunk_seen.wait(), timeout=2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    # Give the server-side handler task a moment to observe the disconnect.
    await asyncio.sleep(0.2)

    records = [
        r for r in caplog.records if r.name == "claude_gateway_extensions.server.passthrough"
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert "client disconnected mid-stream" in records[0].getMessage()
    assert "sess-cancel" in records[0].getMessage()
    assert records[0].exc_info is None

    # The server itself must stay healthy: a fresh request still works.
    async def ok_handler(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    harness.fake.script(ok_handler)
    resp2 = await harness.client.get("/v1/models")
    assert resp2.status == 200


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
        assert data["error"]["type"] == "cgx_upstream_unreachable"
    finally:
        await client.close()


@pytest.mark.invariant(decision="Tee only the private copy")
async def test_capture_reads_usage_from_a_gzipped_response(harness: ServerHarness) -> None:
    """A compressed /v1/messages response must still be forwarded verbatim and captured.

    Without decoding the tee'd copy, json.loads raised UnicodeDecodeError
    inside _capture, so no snapshot was stored at all and the session had
    nothing to bump.
    """
    payload = json.dumps({"usage": {"input_tokens": 11, "cache_read_input_tokens": 4096}}).encode()
    compressed = gzip.compress(payload)

    async def gzipped(request: web.Request) -> web.Response:
        return web.Response(
            body=compressed,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
        )

    harness.fake.script(gzipped)
    resp = await harness.client.post(
        "/v1/messages",
        data=b'{"model": "claude-x", "max_tokens": 10, "messages": []}',
        headers={"x-claude-code-session-id": "sess-cap", "accept-encoding": "gzip"},
        auto_decompress=False,
    )
    assert resp.status == 200
    assert await resp.read() == compressed  # forwarded byte-for-byte, still compressed

    store = harness.app[STORE_KEY]
    snapshots = store.get_snapshots("sess-cap")
    assert len(snapshots) == 1
    assert snapshots[0].usage.cache_read_input_tokens == 4096
    assert snapshots[0].usage.total_input == 4107


@pytest.mark.invariant(decision="Tee only the private copy")
async def test_gzipped_request_body_captured_same_as_plain(harness: ServerHarness) -> None:
    """A gzip-encoded request body must still be captured, and forwarded unchanged."""

    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 7}})

    harness.fake.script(handler)

    plain_body = b'{"model": "claude-x", "max_tokens": 10, "messages": []}'
    compressed_body = gzip.compress(plain_body)
    resp = await harness.client.post(
        "/v1/messages",
        data=compressed_body,
        headers={
            "x-claude-code-session-id": "sess-gzip-req",
            "content-encoding": "gzip",
        },
    )
    assert resp.status == 200

    # Forwarded to upstream exactly as received, still compressed.
    assert harness.fake.requests[0].body == compressed_body
    assert harness.fake.requests[0].headers.get("content-encoding") == "gzip"

    store = harness.app[STORE_KEY]
    snapshots = store.get_snapshots("sess-gzip-req")
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.body == plain_body
    assert snapshot.model == "claude-x"
    # The stored copy is plain JSON now, so a stale content-encoding header
    # must not follow it into the snapshot (a future bump replay would try
    # to gunzip a body that was never gzipped again).
    assert "content-encoding" not in snapshot.headers


@pytest.mark.invariant(decision="Tee only the private copy")
async def test_malformed_gzip_request_body_warns_once_per_session(
    harness: ServerHarness, caplog: pytest.LogCaptureFixture
) -> None:
    """A body that claims gzip but is not valid gzip logs one WARNING per session."""

    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 1}})

    harness.fake.script(handler)
    harness.fake.script(handler)
    caplog.set_level(logging.WARNING, logger="claude_gateway_extensions.server.passthrough")

    for _ in range(2):
        resp = await harness.client.post(
            "/v1/messages",
            data=b"not actually gzip",
            headers={
                "x-claude-code-session-id": "sess-bad-gzip",
                "content-encoding": "gzip",
            },
        )
        assert resp.status == 200

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "sess-bad-gzip" in message
    assert "gzip" in message
    assert "not actually gzip" not in message

    store = harness.app[STORE_KEY]
    assert store.get_snapshots("sess-bad-gzip") == []


@pytest.mark.invariant(decision="Bodies never reach a log")
async def test_no_request_or_response_body_reaches_any_log_record(
    harness: ServerHarness, caplog: pytest.LogCaptureFixture
) -> None:
    """Request and response bodies must never appear in a log record, at any level.

    The daemon sits in the request path permanently and sees whole
    conversations plus the credential. Body content is therefore logged
    nowhere, not even behind DEBUG, so turning logging up can never turn the
    daemon into a transcript recorder.
    """
    request_marker = "REQUEST-BODY-MARKER-3f9a"
    response_marker = "RESPONSE-BODY-MARKER-7c1b"

    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 1}, "note": response_marker})

    harness.fake.script(handler)
    caplog.set_level(logging.DEBUG)

    body = json.dumps(
        {
            "model": "claude-x",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": request_marker}],
        }
    ).encode()
    resp = await harness.client.post(
        "/v1/messages",
        data=body,
        headers={"x-claude-code-session-id": "sess-log", "authorization": "Bearer sk-secret"},
    )
    assert resp.status == 200
    assert response_marker in (await resp.text())  # the client still gets the body

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert caplog.records, "nothing was logged at all; the assertions below would be vacuous"
    assert request_marker not in logged
    assert response_marker not in logged
    assert "sk-secret" not in logged
