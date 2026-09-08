"""Tests that a running daemon shuts down cleanly on SIGTERM.

``systemctl --user stop`` sends ``SIGTERM`` to the unit ``cgx service
install`` writes. Without a handler the process dies where it stands, so
``AppRunner.cleanup`` — and with it the app's ``cleanup_ctx``, the shared
client session and the scheduler's per-session tasks — never runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
from pathlib import Path

import pytest

from claude_gateway_extensions.server.app import (
    install_shutdown_handlers,
    remove_shutdown_handlers,
    serve,
)
from conftest import make_config

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
    loop = asyncio.get_running_loop()
    install_shutdown_handlers(loop, stop)
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


def test_shutdown_signals_cover_sigterm_and_sigint() -> None:
    from claude_gateway_extensions.server.app import SHUTDOWN_SIGNALS

    assert set(SHUTDOWN_SIGNALS) == {signal.SIGTERM, signal.SIGINT}


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_installed_handler_sets_the_event(sig: signal.Signals) -> None:
    """Each shutdown signal must set the stop event rather than killing the process."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    install_shutdown_handlers(loop, stop)
    try:
        signal.raise_signal(sig)
        await asyncio.wait_for(stop.wait(), timeout=5)
    finally:
        remove_shutdown_handlers(loop)
