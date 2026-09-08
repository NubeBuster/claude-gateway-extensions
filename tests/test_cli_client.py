"""Tests for claude_cache_bump_gateway.cli.client against a fake in-process HTTP server."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from claude_cache_bump_gateway.cli.client import ApiError, ControlClient, DaemonUnreachable

Handler = Callable[["_FakeRequestHandler"], None]


class _FakeRequestHandler(BaseHTTPRequestHandler):
    """Minimal handler that delegates to a per-test routing function."""

    routes: ClassVar[dict[tuple[str, str], tuple[int, object]]] = {}

    def _dispatch(self) -> None:
        key = (self.command, self.path)
        if key not in self.routes:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": "not_found"}')
            return
        status, body = self.routes[key]
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self._dispatch()

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def fake_server() -> Iterator[tuple[str, dict[tuple[str, str], tuple[int, object]]]]:
    """Start a fake control-API HTTP server and yield ``(base_url, routes)``."""
    routes: dict[tuple[str, str], tuple[int, object]] = {}

    class _Handler(_FakeRequestHandler):
        pass

    _Handler.routes = routes
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", routes
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_health(fake_server: tuple[str, dict]) -> None:
    base_url, routes = fake_server
    routes[("GET", "/ccbg/health")] = (200, {"status": "ok"})
    client = ControlClient(base_url)
    assert client.health() == {"status": "ok"}


def test_list_sessions_unwraps_the_envelope(fake_server: tuple[str, dict]) -> None:
    base_url, routes = fake_server
    routes[("GET", "/ccbg/sessions")] = (200, {"sessions": [{"session_id": "abc"}]})
    client = ControlClient(base_url)
    assert client.list_sessions() == [{"session_id": "abc"}]


def test_list_sessions_rejects_a_malformed_envelope(fake_server: tuple[str, dict]) -> None:
    base_url, routes = fake_server
    routes[("GET", "/ccbg/sessions")] = (200, {"nope": 1})
    client = ControlClient(base_url)
    with pytest.raises(ApiError):
        client.list_sessions()


def test_get_session_404_raises_api_error(fake_server: tuple[str, dict]) -> None:
    base_url, routes = fake_server
    routes[("GET", "/ccbg/sessions/missing")] = (404, {"error": "unknown_session"})
    client = ControlClient(base_url)
    with pytest.raises(ApiError) as exc_info:
        client.get_session("missing")
    assert exc_info.value.status == 404


def test_set_session_put(fake_server: tuple[str, dict]) -> None:
    base_url, routes = fake_server
    routes[("PUT", "/ccbg/sessions/abc")] = (200, {"session_id": "abc", "enabled": True})
    client = ControlClient(base_url)
    result = client.set_session("abc", True, interval_s=120, max_duration_s=3600)
    assert result == {"session_id": "abc", "enabled": True}


def test_bump_ok(fake_server: tuple[str, dict]) -> None:
    base_url, routes = fake_server
    routes[("POST", "/ccbg/sessions/abc/bump")] = (200, {"outcome": "ok"})
    client = ControlClient(base_url)
    assert client.bump("abc") == {"outcome": "ok"}


def test_bump_409_does_not_raise(fake_server: tuple[str, dict]) -> None:
    base_url, routes = fake_server
    routes[("POST", "/ccbg/sessions/abc/bump")] = (409, {"outcome": "cache_write"})
    client = ControlClient(base_url)
    assert client.bump("abc") == {"outcome": "cache_write"}


def test_bump_404_no_snapshot_does_not_raise(fake_server: tuple[str, dict]) -> None:
    base_url, routes = fake_server
    routes[("POST", "/ccbg/sessions/abc/bump")] = (404, {"error": "no_snapshot"})
    client = ControlClient(base_url)
    assert client.bump("abc") == {"error": "no_snapshot"}


def test_bump_500_raises_api_error(fake_server: tuple[str, dict]) -> None:
    base_url, routes = fake_server
    routes[("POST", "/ccbg/sessions/abc/bump")] = (500, {"error": "boom"})
    client = ControlClient(base_url)
    with pytest.raises(ApiError) as exc_info:
        client.bump("abc")
    assert exc_info.value.status == 500


def test_forget(fake_server: tuple[str, dict]) -> None:
    base_url, routes = fake_server
    routes[("DELETE", "/ccbg/sessions/abc")] = (200, {})
    client = ControlClient(base_url)
    client.forget("abc")  # must not raise


def test_daemon_unreachable_when_nothing_listens() -> None:
    client = ControlClient("http://127.0.0.1:1", timeout=0.5)
    with pytest.raises(DaemonUnreachable):
        client.health()
