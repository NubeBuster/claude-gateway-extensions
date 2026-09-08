"""Tests that a running daemon shuts down cleanly, draining in-flight requests.

``systemctl --user stop`` sends ``SIGTERM`` to the unit ``cgx service
install`` writes. Without a handler the process dies where it stands, so
``AppRunner.cleanup`` — and with it the app's ``cleanup_ctx``, the shared
client session and the scheduler's per-session tasks — never runs. On top of
that, ``serve`` must not drop a request that was already in flight (including
a long SSE stream) when the signal arrived; see the "Drain plus
``SO_REUSEPORT``..." decision in ``docs/DESIGN.md`` "Decisions".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from claude_gateway_extensions.server.app import (
    install_shutdown_handlers,
    remove_shutdown_handlers,
    serve,
)
from conftest import FakeUpstream, make_config

_no_reuseport = pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(socket, "SO_REUSEPORT"),
    reason="SO_REUSEPORT is not available on this platform",
)

#: Every test here sends a real SIGTERM/SIGINT via ``os.kill``/``signal.raise_signal``
#: and expects the loop's own signal handler to catch it; on Windows those
#: calls terminate the process outright instead (see
#: ``server/app.py``'s ``install_shutdown_handlers`` docstring), so the
#: whole module is POSIX-only.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="real SIGTERM/SIGINT delivery is POSIX-only"
)


def _free_port() -> int:
    """Return a port that was free a moment ago, for the daemon to bind."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _is_listening(port: int) -> bool:
    """Return whether something accepts TCP connections on ``port``."""
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


async def _wait_until(predicate: object, timeout: float = 5.0) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses."""
    assert callable(predicate)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_sigterm_stops_the_daemon_and_leaves_state_on_disk(tmp_path: Path) -> None:
    """A real SIGTERM must end serve(), release the port, and leave state flushed."""
    port = _free_port()
    config = make_config(tmp_path, "http://127.0.0.1:1", port=port, persist=True)
    task = asyncio.create_task(serve(config))

    assert await _wait_until(lambda: _is_listening(port)), "daemon never started listening"

    def put_session() -> None:
        import urllib.request

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/cgx/sessions/sess-term",
            data=json.dumps({"enabled": True}).encode(),
            headers={"content-type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(request, timeout=5).read()

    await asyncio.get_running_loop().run_in_executor(None, put_session)

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=5)

    assert not _is_listening(port), "port still accepting connections after shutdown"
    assert (tmp_path / "sess-term" / "state.json").is_file(), "session state was not flushed"


async def test_handlers_are_removed_after_serve_returns(tmp_path: Path) -> None:
    """serve() must restore the default signal disposition, not leave the loop's handler behind."""
    port = _free_port()
    config = make_config(tmp_path, "http://127.0.0.1:1", port=port, persist=False)
    task = asyncio.create_task(serve(config))
    assert await _wait_until(lambda: _is_listening(port))

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=5)

    # Re-installing must succeed, which it only can if the slot was released.
    stop = asyncio.Event()
    force = asyncio.Event()
    loop = asyncio.get_running_loop()
    install_shutdown_handlers(loop, stop, force)
    try:
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), timeout=5)
    finally:
        remove_shutdown_handlers(loop)


@pytest.mark.invariant(decision="Control API on /cgx/, loopback only, unauthenticated")
async def test_non_local_host_logs_a_warning_but_still_starts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-local CGX_HOST is the user's choice: serve() warns, it does not refuse."""
    port = _free_port()
    config = make_config(tmp_path, "http://127.0.0.1:1", host="0.0.0.0", port=port, persist=False)
    caplog.set_level(logging.WARNING, logger="claude_gateway_extensions.server.app")
    task = asyncio.create_task(serve(config))

    assert await _wait_until(lambda: _is_listening(port)), "daemon never started listening"

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=5)

    assert any("reachable from other machines" in record.message for record in caplog.records), (
        "no warning logged for a non-local host"
    )


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_installed_handler_sets_the_event(sig: signal.Signals) -> None:
    """Each shutdown signal must set the stop event rather than killing the process."""
    stop = asyncio.Event()
    force = asyncio.Event()
    loop = asyncio.get_running_loop()
    install_shutdown_handlers(loop, stop, force)
    try:
        signal.raise_signal(sig)
        await asyncio.wait_for(stop.wait(), timeout=5)
        assert not force.is_set(), "a single signal must not set force"
    finally:
        remove_shutdown_handlers(loop)


async def test_second_signal_sets_force() -> None:
    """A second shutdown signal, received after the first, must set force too."""
    stop = asyncio.Event()
    force = asyncio.Event()
    loop = asyncio.get_running_loop()
    install_shutdown_handlers(loop, stop, force)
    try:
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), timeout=5)
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(force.wait(), timeout=5)
    finally:
        remove_shutdown_handlers(loop)


def _sse_stream_handler(started: asyncio.Event, release: asyncio.Event):
    """Build a fake-upstream handler that holds an SSE response open until ``release`` fires."""

    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={"content-type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(
            b'data: {"type": "message_start", "message": {"usage": '
            b'{"input_tokens": 10, "cache_creation_input_tokens": 0}}}\n\n'
        )
        started.set()
        await release.wait()
        await resp.write(
            b'data: {"type": "message_delta", "usage": {"cache_creation_input_tokens": 0}}\n\n'
        )
        await resp.write_eof()
        return resp

    return handler


async def _post_messages(port: int, session_id: str) -> bytes:
    """POST a minimal ``/v1/messages`` request through the daemon and return the response body."""
    async with (
        aiohttp.ClientSession() as session,
        session.post(
            f"http://127.0.0.1:{port}/v1/messages",
            json={"model": "claude-x", "messages": []},
            headers={"x-claude-code-session-id": session_id},
        ) as resp,
    ):
        return await resp.read()


@pytest.mark.invariant(decision="Drain plus SO_REUSEPORT instead of socket handover")
async def test_drain_waits_for_an_in_flight_sse_stream_and_completes_it(
    tmp_path: Path, fake_upstream: tuple[FakeUpstream, TestServer]
) -> None:
    fake, fake_server = fake_upstream
    port = _free_port()
    config = make_config(
        tmp_path, str(fake_server.make_url("")), port=port, persist=False, drain_timeout_s=5
    )
    started = asyncio.Event()
    release = asyncio.Event()
    fake.script(_sse_stream_handler(started, release))

    task = asyncio.create_task(serve(config))
    assert await _wait_until(lambda: _is_listening(port))

    request_task = asyncio.create_task(_post_messages(port, "sess-drain"))
    await asyncio.wait_for(started.wait(), timeout=5)

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.sleep(0.2)  # let the drain begin

    assert not _is_listening(port), "new connections must be refused once draining"
    assert not task.done(), "drain must wait for the in-flight stream, not exit immediately"
    assert not request_task.done(), "the in-flight request must still be running"

    release.set()
    body = await asyncio.wait_for(request_task, timeout=5)
    assert b"message_delta" in body

    await asyncio.wait_for(task, timeout=5)


async def test_drain_timeout_forces_exit(
    tmp_path: Path, fake_upstream: tuple[FakeUpstream, TestServer]
) -> None:
    """A request that never finishes must not block shutdown past CGX_DRAIN_TIMEOUT_S."""
    fake, fake_server = fake_upstream
    port = _free_port()
    config = make_config(
        tmp_path, str(fake_server.make_url("")), port=port, persist=False, drain_timeout_s=1
    )
    started = asyncio.Event()
    release = asyncio.Event()  # never set: the handler hangs until force-closed
    fake.script(_sse_stream_handler(started, release))

    task = asyncio.create_task(serve(config))
    assert await _wait_until(lambda: _is_listening(port))

    request_task = asyncio.create_task(_post_messages(port, "sess-timeout"))
    await asyncio.wait_for(started.wait(), timeout=5)

    start = asyncio.get_running_loop().time()
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=5)
    elapsed = asyncio.get_running_loop().time() - start
    assert elapsed < 4.0, "serve() must not wait far longer than the drain timeout"

    request_task.cancel()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await request_task


async def test_health_returns_503_while_draining(
    tmp_path: Path, fake_upstream: tuple[FakeUpstream, TestServer]
) -> None:
    """A client with an already-open (pooled) connection must still see 503 while draining.

    The listening socket is closed the moment the drain starts, so a *new*
    connection is refused; a connection opened before that (kept alive by
    aiohttp's connection pool) is untouched and keeps being served normally,
    which is how a health-checking client actually observes "draining"
    rather than a bare connection failure.
    """
    fake, fake_server = fake_upstream
    port = _free_port()
    config = make_config(
        tmp_path, str(fake_server.make_url("")), port=port, persist=False, drain_timeout_s=5
    )
    started = asyncio.Event()
    release = asyncio.Event()
    fake.script(_sse_stream_handler(started, release))

    task = asyncio.create_task(serve(config))
    assert await _wait_until(lambda: _is_listening(port))

    health_url = f"http://127.0.0.1:{port}/cgx/health"
    session = aiohttp.ClientSession()
    async with session.get(health_url) as warm_up:
        assert warm_up.status == 200

    request_task = asyncio.create_task(_post_messages(port, "sess-health"))
    await asyncio.wait_for(started.wait(), timeout=5)

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.sleep(0.2)

    async with session.get(health_url) as resp:
        assert resp.status == 503
        assert await resp.json() == {"status": "draining"}

    await session.close()
    release.set()
    await asyncio.wait_for(request_task, timeout=5)
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.invariant(decision="Drain plus SO_REUSEPORT instead of socket handover")
async def test_no_bump_fires_during_drain(
    tmp_path: Path, fake_upstream: tuple[FakeUpstream, TestServer]
) -> None:
    """A scheduled bump due to fire moments after SIGTERM must never reach upstream."""

    async def capture_handler(request: web.Request) -> web.Response:
        del request
        return web.json_response({"usage": {"input_tokens": 10, "cache_creation_input_tokens": 0}})

    fake, fake_server = fake_upstream
    port = _free_port()
    config = make_config(
        tmp_path, str(fake_server.make_url("")), port=port, persist=False, drain_timeout_s=5
    )
    fake.script(capture_handler)

    task = asyncio.create_task(serve(config))
    assert await _wait_until(lambda: _is_listening(port))

    await _post_messages(port, "sess-nobump")
    assert len(fake.requests) == 1

    async with aiohttp.ClientSession() as session:
        await session.put(
            f"http://127.0.0.1:{port}/cgx/sessions/sess-nobump",
            json={"enabled": True, "interval_s": 1},
        )

    await asyncio.sleep(0.3)  # well before the 1s interval elapses
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=5)

    await asyncio.sleep(1.0)  # past the interval, to catch a bump that should not happen
    assert len(fake.requests) == 1, "no bump replay must reach upstream once draining started"


@_no_reuseport
async def test_reuseport_lets_a_second_daemon_bind_before_the_first_stops(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config_a = make_config(tmp_path / "a", "http://127.0.0.1:1", port=port, persist=False)
    config_b = make_config(tmp_path / "b", "http://127.0.0.1:1", port=port, persist=False)

    task_a = asyncio.create_task(serve(config_a))
    assert await _wait_until(lambda: _is_listening(port)), "first daemon never started listening"

    task_b = asyncio.create_task(serve(config_b))
    await asyncio.sleep(0.3)
    assert not task_b.done(), (
        f"second daemon failed to bind: {task_b.exception() if task_b.done() else None}"
    )

    task_a.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task_a

    assert await _wait_until(lambda: _is_listening(port)), (
        "port must still be served by the second daemon after the first stops"
    )

    task_b.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task_b


async def test_pid_file_written_on_start_and_removed_on_exit(tmp_path: Path) -> None:
    port = _free_port()
    config = make_config(tmp_path, "http://127.0.0.1:1", port=port, persist=False)
    pid_file = tmp_path / "daemon.pid"

    task = asyncio.create_task(serve(config))
    assert await _wait_until(lambda: _is_listening(port))
    assert await _wait_until(pid_file.is_file), "pid file was not written on start"
    assert int(pid_file.read_text().strip()) == os.getpid()

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=5)

    assert not pid_file.exists(), "pid file was not removed on exit"


@_no_reuseport
async def test_handover_signals_the_previous_daemon_and_takes_over_the_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``serve(handover=True)`` must SIGTERM the pid it finds on disk and take over the file.

    A genuine two-*process* handover can't be driven from a single-process
    test: ``asyncio.loop.add_signal_handler`` is process-wide, so two
    coroutine-level "daemons" sharing one pid would fight over the same
    SIGTERM handler slot in a way that never happens across real processes
    (each has its own). This drives the real ``serve(handover=True)``
    coroutine ("a running in-process daemon") against a pid file seeded with
    a stand-in pid, and intercepts ``os.kill`` to observe what it signals
    without actually sending a signal to an unrelated process.
    """
    port = _free_port()
    config = make_config(tmp_path, "http://127.0.0.1:1", port=port, persist=False)
    pid_file = tmp_path / "daemon.pid"
    fake_old_pid = 999_999_999  # stand-in for "some other process"; never actually signaled
    pid_file.write_text(str(fake_old_pid))

    signalled: list[int] = []
    real_kill = os.kill

    def fake_kill(pid: int, sig: int) -> None:
        if pid == fake_old_pid:
            signalled.append(pid)
            return
        real_kill(pid, sig)

    monkeypatch.setattr("claude_gateway_extensions.server.app.os.kill", fake_kill)

    task = asyncio.create_task(serve(config, handover=True))
    assert await _wait_until(lambda: _is_listening(port))
    assert await _wait_until(lambda: signalled == [fake_old_pid]), (
        "handover must SIGTERM the pid found in the pid file"
    )
    assert pid_file.read_text().strip() == str(os.getpid()), (
        "handover must take over the pid file for the new daemon"
    )

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, timeout=5)
