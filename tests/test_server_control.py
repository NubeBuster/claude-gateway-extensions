"""Tests for claude_gateway_extensions.server.control."""

from __future__ import annotations

import gzip
import json
import os

import pytest
from aiohttp import web

from claude_gateway_extensions.server.keys import CONFIG_KEY, DRAIN_STATE_KEY
from conftest import ServerHarness

#: A deadline far enough ahead that the scheduler never reaches it mid-test
#: (2100-01-01), for the tests that run against the real clock.
_FUTURE_UNTIL = 4102444800


async def test_health(harness: ServerHarness) -> None:
    resp = await harness.client.get("/cgx/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert isinstance(data["version"], str)
    assert data["sessions"] == 0


async def test_health_returns_503_while_draining(harness: ServerHarness) -> None:
    harness.app[DRAIN_STATE_KEY].draining = True
    resp = await harness.client.get("/cgx/health")
    assert resp.status == 503
    assert await resp.json() == {"status": "draining"}


async def test_get_config_reports_missing_file_as_defaults(harness: ServerHarness) -> None:
    resp = await harness.client.get("/cgx/config")
    assert resp.status == 200
    data = await resp.json()
    config = harness.app[CONFIG_KEY]
    assert data["path"] == str(config.config_file)
    assert data["present"] is False
    assert data["parse_error"] is None
    assert data["shadow_fork"]["max_tokens"] == {
        "value": config.shadow_fork_max_tokens,
        "source": "default",
    }
    assert data["shadow_fork"]["max_per_min"] == {
        "value": config.shadow_fork_max_per_min,
        "source": "default",
    }
    assert data["shadow_fork"]["thinking_guard"] == {
        "value": config.shadow_fork_thinking_guard,
        "source": "default",
    }
    assert data["shadow_fork"]["model"] == {"value": None, "source": "default"}


@pytest.mark.invariant(
    decision="Shadow Fork tuning is hot reloaded from one config file; "
    "the file wins over the environment for exactly the keys it holds."
)
async def test_get_config_reports_file_source_and_reloads_on_edit(
    harness: ServerHarness,
) -> None:
    config = harness.app[CONFIG_KEY]
    config.config_file.parent.mkdir(parents=True, exist_ok=True)
    config.config_file.write_text("[shadow_fork]\nmax_tokens = 999\n")

    resp = await harness.client.get("/cgx/config")
    data = await resp.json()
    assert data["present"] is True
    assert data["parse_error"] is None
    assert data["shadow_fork"]["max_tokens"] == {"value": 999, "source": "file"}
    assert data["shadow_fork"]["max_per_min"]["source"] == "default"

    mtime = os.stat(config.config_file).st_mtime + 5
    os.utime(config.config_file, (mtime, mtime))
    config.config_file.write_text("[shadow_fork]\nmax_tokens = 111\n")

    resp2 = await harness.client.get("/cgx/config")
    data2 = await resp2.json()
    assert data2["shadow_fork"]["max_tokens"] == {"value": 111, "source": "file"}


async def test_get_config_reports_parse_error_and_keeps_last_good(
    harness: ServerHarness,
) -> None:
    config = harness.app[CONFIG_KEY]
    config.config_file.parent.mkdir(parents=True, exist_ok=True)
    config.config_file.write_text("[shadow_fork]\nmax_tokens = 100\n")
    resp = await harness.client.get("/cgx/config")
    assert (await resp.json())["shadow_fork"]["max_tokens"]["value"] == 100

    mtime = os.stat(config.config_file).st_mtime + 5
    os.utime(config.config_file, (mtime, mtime))
    config.config_file.write_text("[shadow_fork]\nmax_tokens = not valid toml\n")

    resp2 = await harness.client.get("/cgx/config")
    data2 = await resp2.json()
    assert data2["present"] is True
    assert data2["parse_error"] is not None
    assert data2["shadow_fork"]["max_tokens"]["value"] == 100  # kept the last good value


async def test_get_unknown_session_404(harness: ServerHarness) -> None:
    resp = await harness.client.get("/cgx/sessions/does-not-exist")
    assert resp.status == 404
    assert await resp.json() == {"error": "unknown_session"}


async def test_get_invalid_session_id_400(harness: ServerHarness) -> None:
    resp = await harness.client.get("/cgx/sessions/bad$id")
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_session_id"


async def test_put_missing_enabled_400(harness: ServerHarness) -> None:
    resp = await harness.client.put("/cgx/sessions/sess-x", json={"interval_s": 60})
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_body"


async def test_put_wrong_interval_type_400(harness: ServerHarness) -> None:
    resp = await harness.client.put(
        "/cgx/sessions/sess-x", json={"enabled": True, "interval_s": "60"}
    )
    assert resp.status == 400


async def test_put_invalid_session_id_400(harness: ServerHarness) -> None:
    resp = await harness.client.put("/cgx/sessions/bad$id", json={"enabled": True})
    assert resp.status == 400


async def test_put_creates_and_lists_session(harness: ServerHarness) -> None:
    resp = await harness.client.put(
        "/cgx/sessions/sess-y", json={"enabled": True, "interval_s": 60}
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["session_id"] == "sess-y"
    assert data["enabled"] is True
    assert data["interval_s"] == 60

    list_resp = await harness.client.get("/cgx/sessions")
    assert list_resp.status == 200
    listed = await list_resp.json()
    assert any(s["session_id"] == "sess-y" for s in listed["sessions"])

    health_resp = await harness.client.get("/cgx/health")
    assert (await health_resp.json())["sessions"] == 1


async def test_bump_no_snapshot_404(harness: ServerHarness) -> None:
    await harness.client.put("/cgx/sessions/sess-empty", json={"enabled": True})
    resp = await harness.client.post("/cgx/sessions/sess-empty/bump")
    assert resp.status == 404
    assert await resp.json() == {"error": "no_snapshot"}


async def test_bump_invalid_session_id_400(harness: ServerHarness) -> None:
    resp = await harness.client.post("/cgx/sessions/bad$id/bump")
    assert resp.status == 400


async def test_delete_session_204_then_unknown(harness: ServerHarness) -> None:
    await harness.client.put("/cgx/sessions/sess-z", json={"enabled": True})
    resp = await harness.client.delete("/cgx/sessions/sess-z")
    assert resp.status == 204

    get_resp = await harness.client.get("/cgx/sessions/sess-z")
    assert get_resp.status == 404


async def test_delete_unknown_session_still_204(harness: ServerHarness) -> None:
    resp = await harness.client.delete("/cgx/sessions/never-seen")
    assert resp.status == 204


async def test_delete_invalid_session_id_400(harness: ServerHarness) -> None:
    resp = await harness.client.delete("/cgx/sessions/bad$id")
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
    bump_resp = await harness.client.post("/cgx/sessions/sess-rej/bump")
    assert bump_resp.status == 409
    data = await bump_resp.json()
    assert data["outcome"] == "rejected"


@pytest.mark.invariant(decision="Fail-closed bump classification")
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
    bump_resp = await harness.client.post("/cgx/sessions/sess-gz/bump")
    assert bump_resp.status == 409
    data = await bump_resp.json()
    assert data["outcome"] == "cache_write"
    assert data["usage"]["cache_creation_input_tokens"] == 4096

    status = await (await harness.client.get("/cgx/sessions/sess-gz")).json()
    assert status["enabled"] is False
    assert status["stopped_reason"] == "cache_write"


@pytest.mark.parametrize("value", [0, -1])
@pytest.mark.parametrize("field", ["interval_s", "max_duration_s"])
@pytest.mark.invariant(decision="Absolute deadline, not an open-ended toggle")
async def test_put_rejects_non_positive_durations(
    harness: ServerHarness, field: str, value: int
) -> None:
    """A zero interval would bump on every tick; reject it instead of spending on it."""
    resp = await harness.client.put(
        "/cgx/sessions/sess-interval", json={"enabled": True, field: value}
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_body"

    # And nothing was created for the rejected session.
    assert (await harness.client.get("/cgx/sessions/sess-interval")).status == 404


async def test_put_accepts_until_and_reports_it_back(harness: ServerHarness) -> None:
    """A future deadline survives the round trip; this harness runs on the real clock."""
    resp = await harness.client.put(
        "/cgx/sessions/sess-until", json={"enabled": True, "until": _FUTURE_UNTIL}
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["until"] == _FUTURE_UNTIL

    fetched = await harness.client.get("/cgx/sessions/sess-until")
    assert (await fetched.json())["until"] == _FUTURE_UNTIL


async def test_put_rejects_non_numeric_until(harness: ServerHarness) -> None:
    resp = await harness.client.put(
        "/cgx/sessions/sess-until-bad", json={"enabled": True, "until": "soon"}
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_body"


async def test_put_accepts_a_past_until_unlike_max_duration(harness: ServerHarness) -> None:
    """Unlike ``interval_s``/``max_duration_s``, ``until`` is not clamped to the future."""
    resp = await harness.client.put(
        "/cgx/sessions/sess-until-past", json={"enabled": True, "until": 1.0}
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["until"] == 1.0


async def test_status_has_no_cache_expires_at_without_a_snapshot(
    harness: ServerHarness,
) -> None:
    resp = await harness.client.put("/cgx/sessions/sess-empty2", json={"enabled": True})
    data = await resp.json()
    assert data["cache_expires_at"] is None
    assert data["formatted"] == {
        "ttl_countdown": None,
        "expires_at": None,
        "next_bump_countdown": None,
        "next_bump_at": None,
        "rewarm_write": None,
        "cache": None,
    }


async def test_status_cache_expires_at_present_after_a_real_request(
    harness: ServerHarness,
) -> None:
    async def messages_handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 5}})

    harness.fake.script(messages_handler)
    body = b'{"model": "claude-x", "max_tokens": 10, "messages": []}'
    resp = await harness.client.post(
        "/v1/messages",
        data=body,
        headers={"x-claude-code-session-id": "sess-ttl", "authorization": "Bearer a"},
    )
    assert resp.status == 200

    status_resp = await harness.client.get("/cgx/sessions/sess-ttl")
    data = await status_resp.json()
    assert data["enabled"] is False
    assert isinstance(data["cache_expires_at"], int | float)
    assert data["formatted"]["ttl_countdown"] is not None
    assert data["formatted"]["expires_at"] is not None
    assert data["formatted"]["next_bump_countdown"] is None
    assert data["formatted"]["next_bump_at"] is None


@pytest.mark.invariant(decision="Rewarm: an explicit, opt-in return from a cold cache")
async def test_put_rewarm_tolerates_one_cache_write(harness: ServerHarness) -> None:
    """A rewarm-armed session survives a cache-write bump and stays enabled."""

    async def messages_handler(request: web.Request) -> web.Response:
        return web.json_response({"usage": {"input_tokens": 5}})

    harness.fake.script(messages_handler)
    body = b'{"model": "claude-x", "max_tokens": 10, "messages": []}'
    resp = await harness.client.post(
        "/v1/messages",
        data=body,
        headers={"x-claude-code-session-id": "sess-rewarm", "authorization": "Bearer a"},
    )
    assert resp.status == 200

    put_resp = await harness.client.put(
        "/cgx/sessions/sess-rewarm", json={"enabled": True, "rewarm": True}
    )
    assert put_resp.status == 200
    assert (await put_resp.json())["rewarm"] is True

    async def cache_write_handler(request: web.Request) -> web.Response:
        return web.json_response(
            {"usage": {"input_tokens": 1, "cache_creation_input_tokens": 4096}}
        )

    harness.fake.script(cache_write_handler)
    bump_resp = await harness.client.post("/cgx/sessions/sess-rewarm/bump")
    assert bump_resp.status == 409
    data = await bump_resp.json()
    assert data["outcome"] == "cache_write"

    status = await (await harness.client.get("/cgx/sessions/sess-rewarm")).json()
    assert status["enabled"] is True
    assert status["stopped_reason"] is None
    assert status["rewarm"] is False
    assert status["rewarm_write"]["usage"]["cache_creation_input_tokens"] == 4096
    assert status["formatted"]["rewarm_write"] is not None

    # The flag is spent: a second cache write stops the session as usual.
    harness.fake.script(cache_write_handler)
    second_bump = await harness.client.post("/cgx/sessions/sess-rewarm/bump")
    assert second_bump.status == 409
    status_after = await (await harness.client.get("/cgx/sessions/sess-rewarm")).json()
    assert status_after["enabled"] is False
    assert status_after["stopped_reason"] == "cache_write"


@pytest.mark.invariant(decision="Absolute deadline, not an open-ended toggle")
async def test_put_disabling_clears_until(harness: ServerHarness) -> None:
    """Disabling drops the deadline; re-enabling without one does not resurrect it."""
    session = "/cgx/sessions/sess-until-cleared"
    await harness.client.put(session, json={"enabled": True, "until": 1700006000})

    disabled = await harness.client.put(session, json={"enabled": False})
    assert disabled.status == 200
    assert (await disabled.json())["until"] is None

    reenabled = await harness.client.put(session, json={"enabled": True})
    assert (await reenabled.json())["until"] is None
