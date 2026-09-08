"""Tests for claude_cache_bump_gateway.cli.statusline."""

from __future__ import annotations

import io
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from claude_cache_bump_gateway.cli.statusline import run_statusline


class _Handler(BaseHTTPRequestHandler):
    routes: ClassVar[dict[tuple[str, str], tuple[int, object]]] = {}

    def do_GET(self) -> None:
        key = (self.command, self.path)
        status, payload = self.routes.get(key, (404, {"error": "not_found"}))
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def fake_daemon() -> Iterator[tuple[str, dict[tuple[str, str], tuple[int, object]]]]:
    routes: dict[tuple[str, str], tuple[int, object]] = {}

    class Handler(_Handler):
        pass

    Handler.routes = routes
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", routes
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _run(payload: dict[str, object], environ: dict[str, str]) -> str:
    stdin = io.StringIO(json.dumps(payload))
    stdout = io.StringIO()
    code = run_statusline(stdin, stdout, environ)
    assert code == 0
    return stdout.getvalue()


def test_on_with_next_bump(fake_daemon: tuple[str, dict]) -> None:
    base_url, routes = fake_daemon
    routes[("GET", "/ccbg/sessions/s1")] = (
        200,
        {"session_id": "s1", "enabled": True, "next_bump_at": time.time() + 47 * 60},
    )
    out = _run({"session_id": "s1"}, {"CCBG_URL": base_url})
    assert out.strip() == "cache-bump: on, next in 47m"


def test_off(fake_daemon: tuple[str, dict]) -> None:
    base_url, routes = fake_daemon
    routes[("GET", "/ccbg/sessions/s1")] = (200, {"session_id": "s1", "enabled": False})
    out = _run({"session_id": "s1"}, {"CCBG_URL": base_url})
    assert out.strip() == "cache-bump: off"


def test_off_with_stopped_reason(fake_daemon: tuple[str, dict]) -> None:
    base_url, routes = fake_daemon
    routes[("GET", "/ccbg/sessions/s1")] = (
        200,
        {"session_id": "s1", "enabled": False, "stopped_reason": "unauthorized"},
    )
    out = _run({"session_id": "s1"}, {"CCBG_URL": base_url})
    assert out.strip() == "cache-bump: off, stopped: unauthorized"


def test_daemon_unreachable_prints_nothing() -> None:
    out = _run({"session_id": "s1"}, {"CCBG_URL": "http://127.0.0.1:1"})
    assert out == ""


def test_unknown_session_prints_nothing(fake_daemon: tuple[str, dict]) -> None:
    base_url, _routes = fake_daemon
    out = _run({"session_id": "missing"}, {"CCBG_URL": base_url})
    assert out == ""


def test_invalid_json_prints_nothing() -> None:
    stdin = io.StringIO("{not json")
    stdout = io.StringIO()
    code = run_statusline(stdin, stdout, {})
    assert code == 0
    assert stdout.getvalue() == ""
