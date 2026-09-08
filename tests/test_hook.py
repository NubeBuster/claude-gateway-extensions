"""Tests for claude_cache_bump_gateway.cli.hook."""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from claude_cache_bump_gateway.cli.hook import run_user_prompt_submit


class _Handler(BaseHTTPRequestHandler):
    routes: ClassVar[dict[tuple[str, str], tuple[int, object]]] = {}
    calls: ClassVar[list[tuple[str, str, object]]] = []

    def _dispatch(self) -> None:
        length = int(self.headers.get("content-length", 0))
        raw_body = self.rfile.read(length) if length else b""
        parsed_body = json.loads(raw_body) if raw_body else None
        self.calls.append((self.command, self.path, parsed_body))
        key = (self.command, self.path)
        status, payload = self.routes.get(key, (404, {"error": "not_found"}))
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def log_message(self, format: str, *args: object) -> None:
        pass


#: ``HTTPServer.shutdown()`` waits up to one poll interval for the serving
#: thread to notice; the stdlib default of 0.5 s is paid by every test.
_POLL_INTERVAL_S = 0.01


@pytest.fixture
def fake_daemon() -> Iterator[tuple[str, dict[tuple[str, str], tuple[int, object]], list]]:
    routes: dict[tuple[str, str], tuple[int, object]] = {}
    calls: list[tuple[str, str, object]] = []

    class Handler(_Handler):
        pass

    Handler.routes = routes
    Handler.calls = calls
    server = HTTPServer(("127.0.0.1", 0), Handler)
    # poll_interval bounds how long shutdown() blocks; the 0.5 s default
    # made teardown dominate this file's runtime.
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=_POLL_INTERVAL_S), daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", routes, calls
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _run(prompt_payload: dict[str, object], environ: dict[str, str]) -> tuple[int, str]:
    stdin = io.StringIO(json.dumps(prompt_payload))
    stdout = io.StringIO()
    code = run_user_prompt_submit(stdin, stdout, environ)
    return code, stdout.getvalue()


def test_non_matching_prompt_prints_nothing() -> None:
    code, out = _run({"session_id": "s1", "prompt": "hello there"}, {})
    assert code == 0
    assert out == ""


def test_invalid_json_exits_silently() -> None:
    stdin = io.StringIO("{not json")
    stdout = io.StringIO()
    code = run_user_prompt_submit(stdin, stdout, {})
    assert code == 0
    assert stdout.getvalue() == ""


def test_on_with_duration_sets_max_duration_and_blocks(
    fake_daemon: tuple[str, dict, list],
) -> None:
    base_url, routes, calls = fake_daemon
    routes[("PUT", "/ccbg/sessions/s1")] = (
        200,
        {"session_id": "s1", "enabled": True, "next_bump_at": None},
    )
    code, out = _run(
        {"session_id": "s1", "prompt": "/cache-bump on 2h"},
        {"CCBG_URL": base_url},
    )
    assert code == 0
    decision = json.loads(out)
    assert decision["decision"] == "block"
    assert "on for this session" in decision["reason"]
    assert calls == [("PUT", "/ccbg/sessions/s1", {"enabled": True, "max_duration_s": 7200})]


def test_off_blocks_with_off_reason(fake_daemon: tuple[str, dict, list]) -> None:
    base_url, routes, _calls = fake_daemon
    routes[("PUT", "/ccbg/sessions/s1")] = (200, {"session_id": "s1", "enabled": False})
    code, out = _run({"session_id": "s1", "prompt": "/cache-bump off"}, {"CCBG_URL": base_url})
    assert code == 0
    decision = json.loads(out)
    assert decision["reason"] == "cache-bump: off"


def test_now_reports_tokens(fake_daemon: tuple[str, dict, list]) -> None:
    base_url, routes, _calls = fake_daemon
    routes[("POST", "/ccbg/sessions/s1/bump")] = (
        200,
        {"outcome": "ok", "usage": {"cache_read_input_tokens": 48213}},
    )
    code, out = _run({"session_id": "s1", "prompt": "/cache-bump now"}, {"CCBG_URL": base_url})
    assert code == 0
    decision = json.loads(out)
    assert "48213 tokens read from cache" in decision["reason"]


def test_daemon_unreachable_prints_helpful_reason_and_exits_zero() -> None:
    code, out = _run(
        {"session_id": "s1", "prompt": "/cache-bump status"},
        {"CCBG_URL": "http://127.0.0.1:1"},
    )
    assert code == 0
    decision = json.loads(out)
    assert decision["decision"] == "block"
    assert "daemon not reachable" in decision["reason"]
    assert "127.0.0.1:1" in decision["reason"]


def test_default_status_action(fake_daemon: tuple[str, dict, list]) -> None:
    base_url, routes, _calls = fake_daemon
    routes[("GET", "/ccbg/sessions/s1")] = (
        200,
        {"session_id": "s1", "enabled": True, "next_bump_at": None},
    )
    code, out = _run({"session_id": "s1", "prompt": "/cache-bump"}, {"CCBG_URL": base_url})
    assert code == 0
    decision = json.loads(out)
    assert "on for this session" in decision["reason"]
