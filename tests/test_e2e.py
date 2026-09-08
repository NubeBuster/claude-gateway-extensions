"""End-to-end test: Claude Code -> proxy -> upstream, capture, bump, restart.

Everything here runs against real sockets and the real application built by
:func:`claude_cache_bump_gateway.server.app.make_app`: a real aiohttp client
plays Claude Code, a real in-process aiohttp server plays the Anthropic API,
and the daemon is driven through its own control API. Nothing is mocked and
no test here reaches the network beyond loopback.

The fake upstream answers by *mode* rather than from a queue, because a
scheduled bump can fire at any moment once bumping is on; a queue would run
dry and turn timing into flakiness.
"""

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from claude_cache_bump_gateway.cli import main
from claude_cache_bump_gateway.cli.hook import run_user_prompt_submit
from claude_cache_bump_gateway.server.app import make_app
from claude_cache_bump_gateway.server.keys import STORE_KEY
from conftest import make_config

SESSION_ID = "e2e-session-1"
CREDENTIAL = "Bearer sk-ant-oat-e2e"
BETA_HEADER = "prompt-caching-2024-07-31,fine-grained-tool-streaming-2025-05-14"

#: A realistic Claude Code request body: 1 h TTL cache_control, system, tools.
REQUEST_BODY = json.dumps(
    {
        "model": "claude-sonnet-4-5-20250929",
        "max_tokens": 8192,
        "stream": True,
        "system": [
            {
                "type": "text",
                "text": "You are Claude Code.",
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ],
        "tools": [{"name": "Bash", "description": "run a command"}],
        "messages": [{"role": "user", "content": "hello"}],
    }
).encode("utf-8")

SSE_BODY = (
    b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
    b'{"input_tokens":10,"cache_read_input_tokens":0,'
    b'"cache_creation_input_tokens":2048}}}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


@dataclass
class Recorded:
    """One request the fake upstream saw."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    @property
    def json_body(self) -> dict[str, Any]:
        """Return the request body parsed as JSON."""
        parsed: dict[str, Any] = json.loads(self.body)
        return parsed

    @property
    def is_replay(self) -> bool:
        """Return whether this request is a bump replay rather than a real turn."""
        try:
            return self.json_body.get("max_tokens") == 0
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False


@dataclass
class FakeAnthropic:
    """Fake ``/v1/messages`` endpoint answering per :attr:`bump_mode`."""

    requests: list[Recorded] = field(default_factory=list)
    #: ``ok`` answers a replay with a pure cache read; ``cache_write`` answers
    #: with a paid cache write, which must stop the session.
    bump_mode: str = "ok"

    def build_app(self) -> web.Application:
        """Return an application recording every request and streaming SSE for real turns."""
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        return app

    @property
    def replays(self) -> list[Recorded]:
        """Return only the bump replays received so far."""
        return [r for r in self.requests if r.is_replay]

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        recorded = Recorded(
            method=request.method,
            path=request.path,
            headers={k.lower(): v for k, v in request.headers.items()},
            body=body,
        )
        self.requests.append(recorded)

        if recorded.is_replay:
            usage = {"input_tokens": 3, "cache_read_input_tokens": 2048}
            if self.bump_mode == "cache_write":
                usage = {"input_tokens": 3, "cache_creation_input_tokens": 2048}
            return web.json_response({"type": "message", "usage": usage})

        response = web.StreamResponse(
            status=200, headers={"content-type": "text/event-stream", "cache-control": "no-cache"}
        )
        await response.prepare(request)
        for chunk in SSE_BODY.split(b"\n\n"):
            if chunk:
                await response.write(chunk + b"\n\n")
        await response.write_eof()
        return response


async def _wait_for(predicate: Any, timeout: float = 10.0) -> bool:
    """Poll ``predicate`` every 50 ms until it is true or ``timeout`` elapses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """A sandboxed state directory, asserted to be inside pytest's tmp_path."""
    path = tmp_path / "state"
    resolved = path.resolve()
    assert resolved.is_relative_to(tmp_path.resolve()), (
        f"state dir {resolved} escaped the test sandbox {tmp_path}"
    )
    return path


async def test_full_lifecycle(state_dir: Path, tmp_path: Path) -> None:
    """Capture, bump, stop-on-cache-write and reload, end to end over real sockets."""
    fake = FakeAnthropic()
    upstream_server = TestServer(fake.build_app())
    await upstream_server.start_server()

    config = make_config(state_dir, str(upstream_server.make_url("")), persist=True)
    assert config.state_dir.resolve().is_relative_to(tmp_path.resolve()), (
        "the daemon would write outside the test sandbox; refusing to run"
    )

    app = make_app(config)
    proxy = TestServer(app)
    await proxy.start_server()
    base = str(proxy.make_url("")).rstrip("/")

    try:
        # --- 1. a real Claude Code turn passes through byte-for-byte ---------
        async with (
            aiohttp.ClientSession() as client,
            client.post(
                f"{base}/v1/messages",
                data=REQUEST_BODY,
                headers={
                    "content-type": "application/json",
                    "authorization": CREDENTIAL,
                    "anthropic-beta": BETA_HEADER,
                    "anthropic-version": "2023-06-01",
                    "x-claude-code-session-id": SESSION_ID,
                    "accept": "text/event-stream",
                },
            ) as response,
        ):
            assert response.status == 200
            assert response.headers["content-type"] == "text/event-stream"
            received = await response.read()

        assert received == SSE_BODY, "response was not forwarded byte-for-byte"
        assert len(fake.requests) == 1
        forwarded = fake.requests[0]
        assert forwarded.path == "/v1/messages"
        assert forwarded.body == REQUEST_BODY, "request was not forwarded byte-for-byte"
        assert forwarded.headers["authorization"] == CREDENTIAL
        assert forwarded.headers["anthropic-beta"] == BETA_HEADER
        # Hop-by-hop headers are regenerated by the transport, not relayed:
        # the upstream must see its own Host, never the proxy's.
        assert forwarded.headers["host"] == upstream_server.make_url("").authority

        # The snapshot's usage came out of the SSE stream, merged across events.
        store = app[STORE_KEY]
        snapshots = store.get_snapshots(SESSION_ID)
        assert len(snapshots) == 1
        assert snapshots[0].usage.total_input == 2058

        # --- 2. turn bumping on through the control API ---------------------
        async with aiohttp.ClientSession() as client:
            async with client.put(
                f"{base}/ccbg/sessions/{SESSION_ID}", json={"enabled": True, "interval_s": 1}
            ) as response:
                assert response.status == 200
                enabled_status = await response.json()
            assert enabled_status["enabled"] is True
            first_next_bump_at = enabled_status["next_bump_at"]
            assert first_next_bump_at is not None

            # --- 3. the scheduled bump fires and replays the snapshot -------
            assert await _wait_for(lambda: fake.replays), "no bump replay arrived"
            replay = fake.replays[0]
            replay_body = replay.json_body
            original = json.loads(REQUEST_BODY)
            assert replay_body["max_tokens"] == 0
            assert replay_body["stream"] is False
            assert replay_body["messages"] == original["messages"]
            assert replay_body["system"] == original["system"]
            assert replay_body["tools"] == original["tools"]
            assert replay_body["model"] == original["model"]
            assert replay.headers["authorization"] == CREDENTIAL
            assert replay.headers["anthropic-beta"] == BETA_HEADER

            async with client.get(f"{base}/ccbg/sessions/{SESSION_ID}") as response:
                after_bump = await response.json()
            assert after_bump["bump_count"] == 1
            assert after_bump["last_result"]["outcome"] == "ok"
            assert after_bump["next_bump_at"] > first_next_bump_at

            # --- 4. a bump that pays a cache write stops the session --------
            fake.bump_mode = "cache_write"

            async def stopped() -> bool:
                async with client.get(f"{base}/ccbg/sessions/{SESSION_ID}") as resp:
                    return bool((await resp.json())["stopped_reason"])

            deadline_ok = False
            for _ in range(200):
                if await stopped():
                    deadline_ok = True
                    break
                await asyncio.sleep(0.05)
            assert deadline_ok, "session kept bumping after paying a cache write"

            async with client.get(f"{base}/ccbg/sessions/{SESSION_ID}") as response:
                final = await response.json()
            assert final["enabled"] is False
            assert final["stopped_reason"] == "cache_write"
            assert final["last_result"]["outcome"] == "cache_write"

            # --- 5. the hook answers /cache status without a model turn -----
            hook_out = io.StringIO()
            payload = json.dumps({"prompt": "/cache status", "session_id": SESSION_ID})
            code = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: run_user_prompt_submit(io.StringIO(payload), hook_out, {"CCBG_URL": base}),
            )
            assert code == 0
            decision = json.loads(hook_out.getvalue())
            assert decision["decision"] == "block"
            assert "cache: off" in decision["reason"]
            assert "cache_write" in decision["reason"]
    finally:
        await proxy.close()
        await upstream_server.close()

    # --- 6. a fresh daemon on the same state dir reloads what was persisted --
    assert (state_dir / SESSION_ID / "state.json").is_file()
    restarted = make_app(make_config(state_dir, "http://127.0.0.1:1", persist=True))
    restarted_server = TestServer(restarted)
    await restarted_server.start_server()
    try:
        restarted_base = str(restarted_server.make_url("")).rstrip("/")
        async with (
            aiohttp.ClientSession() as client,
            client.get(f"{restarted_base}/ccbg/sessions/{SESSION_ID}") as response,
        ):
            assert response.status == 200
            reloaded = await response.json()
        assert reloaded["bump_count"] == final["bump_count"]
        assert reloaded["stopped_reason"] == "cache_write"
        assert reloaded["streams"] == 1
        reloaded_snapshots = restarted[STORE_KEY].get_snapshots(SESSION_ID)
        assert reloaded_snapshots[0].body == REQUEST_BODY, "snapshot body did not survive a restart"
    finally:
        await restarted_server.close()


def test_install_dry_run_under_a_temp_home(tmp_path: Path) -> None:
    """``ccbg install --dry-run`` must print the settings it would write, and write nothing."""
    home = tmp_path / "home"
    home.mkdir()
    environ = {"HOME": str(home), "CCBG_URL": "http://127.0.0.1:8787"}
    stdout = io.StringIO()

    code = main(
        ["install", "--scope", "user", "--dry-run"],
        environ=environ,
        stdout=stdout,
        stderr=io.StringIO(),
        cwd=tmp_path,
    )

    assert code == 0
    settings = json.loads(stdout.getvalue())
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8787"
    entries = settings["hooks"]["UserPromptSubmit"]
    assert any(
        hook.get("command") == "ccbg hook user-prompt-submit"
        for entry in entries
        for hook in entry["hooks"]
    )
    # HOME really was redirected, and a dry run wrote nothing under it.
    assert not (home / ".claude" / "settings.json").exists()
    assert not (home / ".claude" / "skills").exists()
