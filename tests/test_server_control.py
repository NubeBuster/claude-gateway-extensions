"""Tests for claude_cache_bump_gateway.server.control."""

from __future__ import annotations

import gzip
import json

import pytest
from aiohttp import web

from conftest import ServerHarness

#: A deadline far enough ahead that the scheduler never reaches it mid-test
#: (2100-01-01), for the tests that run against the real clock.
_FUTURE_UNTIL = 4102444800


async def test_health(harness: ServerHarness) -> None:
    resp = await harness.client.get("/ccbg/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert isinstance(data["version"], str)
    assert data["sessions"] == 0


async def test_get_unknown_session_404(harness: ServerHarness) -> None:
    resp = await harness.client.get("/ccbg/sessions/does-not-exist")
    assert resp.status == 404
    assert await resp.json() == {"error": "unknown_session"}


async def test_get_invalid_session_id_400(harness: ServerHarness) -> None:
    resp = await harness.client.get("/ccbg/sessions/bad$id")
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_session_id"


async def test_put_missing_enabled_400(harness: ServerHarness) -> None:
    resp = await harness.client.put("/ccbg/sessions/sess-x", json={"interval_s": 60})
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_body"


async def test_put_wrong_interval_type_400(harness: ServerHarness) -> None:
    resp = await harness.client.put(
        "/ccbg/sessions/sess-x", json={"enabled": True, "interval_s": "60"}
    )
    assert resp.status == 400


async def test_put_invalid_session_id_400(harness: ServerHarness) -> None:
    resp = await harness.client.put("/ccbg/sessions/bad$id", json={"enabled": True})
    assert resp.status == 400


async def test_put_creates_and_lists_session(harness: ServerHarness) -> None:
    resp = await harness.client.put(
        "/ccbg/sessions/sess-y", json={"enabled": True, "interval_s": 60}
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["session_id"] == "sess-y"
    assert data["enabled"] is True
    assert data["interval_s"] == 60

    list_resp = await harness.client.get("/ccbg/sessions")
    assert list_resp.status == 200
    listed = await list_resp.json()
    assert any(s["session_id"] == "sess-y" for s in listed["sessions"])

    health_resp = await harness.client.get("/ccbg/health")
    assert (await health_resp.json())["sessions"] == 1


async def test_bump_no_snapshot_404(harness: ServerHarness) -> None:
    await harness.client.put("/ccbg/sessions/sess-empty", json={"enabled": True})
    resp = await harness.client.post("/ccbg/sessions/sess-empty/bump")
    assert resp.status == 404
    assert await resp.json() == {"error": "no_snapshot"}


async def test_bump_invalid_session_id_400(harness: ServerHarness) -> None:
    resp = await harness.client.post("/ccbg/sessions/bad$id/bump")
    assert resp.status == 400


async def test_delete_session_204_then_unknown(harness: ServerHarness) -> None:
    await harness.client.put("/ccbg/sessions/sess-z", json={"enabled": True})
    resp = await harness.client.delete("/ccbg/sessions/sess-z")
    assert resp.status == 204

    get_resp = await harness.client.get("/ccbg/sessions/sess-z")
    assert get_resp.status == 404


async def test_delete_unknown_session_still_204(harness: ServerHarness) -> None:
    resp = await harness.client.delete("/ccbg/sessions/never-seen")
    assert resp.status == 204


async def test_delete_invalid_session_id_400(harness: ServerHarness) -> None:
    resp = await harness.client.delete("/ccbg/sessions/bad$id")
    assert resp.status == 400


async def test_bump_rejected_outcome_returns_409(harness: ServerHarness) -> None:
    async def messages_handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 5}})

    harness.fake.script(messages_handler)
    body = b'{"model": "claude-x", "max_tokens": 10, "messages": []}'
    resp = await harness.client.post(
        "/v1/messages",
        data=body,
        headers={"x-claude-code-session-id": "sess-rej", "authorization": "Bearer a"},
    )
    assert resp.status == 200

    async def reject_handler(request: web.Request) -> web.Response:
        return web.json_response({"error": {"type": "invalid_request_error"}}, status=400)

    harness.fake.script(reject_handler)
    bump_resp = await harness.client.post("/ccbg/sessions/sess-rej/bump")
    assert bump_resp.status == 409
    data = await bump_resp.json()
    assert data["outcome"] == "rejected"


async def test_bump_detects_a_cache_write_in_a_gzipped_response(harness: ServerHarness) -> None:
    """The app's client session does not auto-decompress; the bump path must decode anyway.

    Without decoding, a gzipped 200 is unparseable, ``usage`` reads as
    ``None`` and every bump classifies as ``ok`` — silently disabling the
    stop-on-cache-write safety valve that keeps a stale session from paying
    a full cache write on every interval.
    """

    async def messages_handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 5}})

    harness.fake.script(messages_handler)
    resp = await harness.client.post(
        "/v1/messages",
        data=b'{"model": "claude-x", "max_tokens": 10, "messages": []}',
        headers={
            "x-claude-code-session-id": "sess-gz",
            "authorization": "Bearer a",
            "accept-encoding": "br, zstd",
        },
    )
    assert resp.status == 200

    async def gzipped_cache_write(request: web.Request) -> web.Response:
        # The replay must have asked for an encoding the daemon can decode.
        assert request.headers["accept-encoding"] == "gzip"
        payload = json.dumps(
            {"usage": {"input_tokens": 1, "cache_creation_input_tokens": 4096}}
        ).encode()
        return web.Response(
            body=gzip.compress(payload),
            headers={"content-encoding": "gzip", "content-type": "application/json"},
        )

    harness.fake.script(gzipped_cache_write)
    bump_resp = await harness.client.post("/ccbg/sessions/sess-gz/bump")
    assert bump_resp.status == 409
    data = await bump_resp.json()
    assert data["outcome"] == "cache_write"
    assert data["usage"]["cache_creation_input_tokens"] == 4096

    status = await (await harness.client.get("/ccbg/sessions/sess-gz")).json()
    assert status["enabled"] is False
    assert status["stopped_reason"] == "cache_write"


@pytest.mark.parametrize("value", [0, -1])
@pytest.mark.parametrize("field", ["interval_s", "max_duration_s"])
async def test_put_rejects_non_positive_durations(
    harness: ServerHarness, field: str, value: int
) -> None:
    """A zero interval would bump on every tick; reject it instead of spending on it."""
    resp = await harness.client.put(
        "/ccbg/sessions/sess-interval", json={"enabled": True, field: value}
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_body"

    # And nothing was created for the rejected session.
    assert (await harness.client.get("/ccbg/sessions/sess-interval")).status == 404


async def test_put_accepts_until_and_reports_it_back(harness: ServerHarness) -> None:
    """A future deadline survives the round trip; this harness runs on the real clock."""
    resp = await harness.client.put(
        "/ccbg/sessions/sess-until", json={"enabled": True, "until": _FUTURE_UNTIL}
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["until"] == _FUTURE_UNTIL

    fetched = await harness.client.get("/ccbg/sessions/sess-until")
    assert (await fetched.json())["until"] == _FUTURE_UNTIL


async def test_put_rejects_non_numeric_until(harness: ServerHarness) -> None:
    resp = await harness.client.put(
        "/ccbg/sessions/sess-until-bad", json={"enabled": True, "until": "soon"}
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_body"


async def test_put_accepts_a_past_until_unlike_max_duration(harness: ServerHarness) -> None:
    """Unlike ``interval_s``/``max_duration_s``, ``until`` is not clamped to the future."""
    resp = await harness.client.put(
        "/ccbg/sessions/sess-until-past", json={"enabled": True, "until": 1.0}
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["until"] == 1.0


async def test_put_disabling_clears_until(harness: ServerHarness) -> None:
    """Disabling drops the deadline; re-enabling without one does not resurrect it."""
    session = "/ccbg/sessions/sess-until-cleared"
    await harness.client.put(session, json={"enabled": True, "until": 1700006000})

    disabled = await harness.client.put(session, json={"enabled": False})
    assert disabled.status == 200
    assert (await disabled.json())["until"] is None

    reenabled = await harness.client.put(session, json={"enabled": True})
    assert (await reenabled.json())["until"] is None
