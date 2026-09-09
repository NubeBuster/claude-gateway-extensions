"""Tests for claude_gateway_extensions.cli.hook."""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from claude_gateway_extensions.cli.hook import run_user_prompt_submit


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


@pytest.mark.invariant(decision="Skill as pure fall-through, hook does the work")
def test_non_matching_prompt_prints_nothing() -> None:
    code, out = _run({"session_id": "s1", "prompt": "hello there"}, {})
    assert code == 0
    assert out == ""


def test_cachefoo_does_not_match() -> None:
    code, out = _run({"session_id": "s1", "prompt": "/cachefoo"}, {})
    assert code == 0
    assert out == ""


def test_old_cache_bump_spelling_does_not_match() -> None:
    code, out = _run({"session_id": "s1", "prompt": "/cache-bump on"}, {})
    assert code == 0
    assert out == ""


@pytest.mark.invariant(decision="Skill as pure fall-through, hook does the work")
def test_invalid_json_exits_silently() -> None:
    stdin = io.StringIO("{not json")
    stdout = io.StringIO()
    code = run_user_prompt_submit(stdin, stdout, {})
    assert code == 0
    assert stdout.getvalue() == ""


@pytest.mark.invariant(decision="Absolute deadline, not an open-ended toggle")
def test_duration_enables_with_an_absolute_deadline(fake_daemon: tuple[str, dict, list]) -> None:
    base_url, routes, calls = fake_daemon
    routes[("PUT", "/cgx/sessions/s1")] = (
        200,
        {
            "session_id": "s1",
            "enabled": True,
            "until": 1700006000,
            "next_bump_at": 1700003000,
            "last_request_at": 1700000000,
            "last_bump_at": None,
        },
    )
    code, out = _run({"session_id": "s1", "prompt": "/cache 2h"}, {"CGX_URL": base_url})
    assert code == 0
    decision = json.loads(out)
    assert decision["decision"] == "block"
    assert "cache: bumping until" in decision["reason"]
    assert len(calls) == 1
    method, path, body = calls[0]
    assert (method, path) == ("PUT", "/cgx/sessions/s1")
    assert body["enabled"] is True
    assert isinstance(body["until"], int | float)
    assert "interval_s" not in body
    assert "max_duration_s" not in body


def test_off_blocks_with_off_reason(fake_daemon: tuple[str, dict, list]) -> None:
    base_url, routes, _calls = fake_daemon
    routes[("PUT", "/cgx/sessions/s1")] = (200, {"session_id": "s1", "enabled": False})
    code, out = _run({"session_id": "s1", "prompt": "/cache off"}, {"CGX_URL": base_url})
    assert code == 0
    decision = json.loads(out)
    assert decision["reason"] == "cache: off"


def test_zero_is_also_off(fake_daemon: tuple[str, dict, list]) -> None:
    base_url, routes, calls = fake_daemon
    routes[("PUT", "/cgx/sessions/s1")] = (200, {"session_id": "s1", "enabled": False})
    code, out = _run({"session_id": "s1", "prompt": "/cache 0"}, {"CGX_URL": base_url})
    assert code == 0
    decision = json.loads(out)
    assert decision["reason"] == "cache: off"
    assert calls == [("PUT", "/cgx/sessions/s1", {"enabled": False})]


def test_bump_reports_tokens(fake_daemon: tuple[str, dict, list]) -> None:
    base_url, routes, _calls = fake_daemon
    routes[("POST", "/cgx/sessions/s1/bump")] = (
        200,
        {"outcome": "ok", "usage": {"cache_read_input_tokens": 48213}},
    )
    code, out = _run({"session_id": "s1", "prompt": "/cache bump"}, {"CGX_URL": base_url})
    assert code == 0
    decision = json.loads(out)
    assert "48213 tokens read from cache" in decision["reason"]


@pytest.mark.invariant(decision="Rewarm: an explicit, opt-in return from a cold cache")
def test_bump_cache_write_reports_the_paid_write_and_its_size(
    fake_daemon: tuple[str, dict, list],
) -> None:
    base_url, routes, _calls = fake_daemon
    routes[("POST", "/cgx/sessions/s1/bump")] = (
        409,
        {"outcome": "cache_write", "usage": {"cache_creation_input_tokens": 4096}},
    )
    code, out = _run({"session_id": "s1", "prompt": "/cache bump"}, {"CGX_URL": base_url})
    assert code == 0
    decision = json.loads(out)
    assert "paid a full write, 4096 tokens" in decision["reason"]


@pytest.mark.invariant(decision="Rewarm: an explicit, opt-in return from a cold cache")
def test_rewarm_suffix_arms_the_session(fake_daemon: tuple[str, dict, list]) -> None:
    base_url, routes, calls = fake_daemon
    routes[("PUT", "/cgx/sessions/s1")] = (
        200,
        {"session_id": "s1", "enabled": True, "until": 1700006000, "rewarm": True},
    )
    code, out = _run({"session_id": "s1", "prompt": "/cache 2h rewarm"}, {"CGX_URL": base_url})
    assert code == 0
    decision = json.loads(out)
    assert "rewarm armed" in decision["reason"]
    assert len(calls) == 1
    _method, _path, body = calls[0]
    assert body["rewarm"] is True


@pytest.mark.invariant(decision="Rewarm: an explicit, opt-in return from a cold cache")
def test_duration_without_rewarm_does_not_send_the_flag(
    fake_daemon: tuple[str, dict, list],
) -> None:
    base_url, routes, calls = fake_daemon
    routes[("PUT", "/cgx/sessions/s1")] = (
        200,
        {"session_id": "s1", "enabled": True, "until": 1700006000},
    )
    _run({"session_id": "s1", "prompt": "/cache 2h"}, {"CGX_URL": base_url})
    _method, _path, body = calls[0]
    assert "rewarm" not in body


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_falls_back_to_loopback_anthropic_base_url(fake_daemon: tuple[str, dict, list]) -> None:
    """No CGX_URL set: a loopback ANTHROPIC_BASE_URL (what Claude Code passes to hooks) works."""
    base_url, routes, _calls = fake_daemon
    routes[("PUT", "/cgx/sessions/s1")] = (200, {"session_id": "s1", "enabled": False})
    code, out = _run({"session_id": "s1", "prompt": "/cache off"}, {"ANTHROPIC_BASE_URL": base_url})
    assert code == 0
    decision = json.loads(out)
    assert decision["reason"] == "cache: off"


@pytest.mark.invariant(decision="Control API on /cgx/, loopback only, unauthenticated")
def test_non_loopback_anthropic_base_url_is_ignored() -> None:
    """A non-loopback ANTHROPIC_BASE_URL is not this daemon; the hook must not try to reach it.

    ``CGX_PORT`` pins the resulting default to a port nothing is listening
    on, so this stays hermetic instead of depending on port 8787 being free.
    """
    code, out = _run(
        {"session_id": "s1", "prompt": "/cache off"},
        {"ANTHROPIC_BASE_URL": "https://api.anthropic.com", "CGX_PORT": "18787"},
    )
    assert code == 0
    decision = json.loads(out)
    assert "daemon not reachable at http://127.0.0.1:18787" in decision["reason"]


@pytest.mark.invariant(decision="Absolute deadline, not an open-ended toggle")
def test_on_is_removed_and_gives_the_unparseable_hint(
    fake_daemon: tuple[str, dict, list],
) -> None:
    base_url, _routes, calls = fake_daemon
    code, out = _run({"session_id": "s1", "prompt": "/cache on"}, {"CGX_URL": base_url})
    assert code == 0
    decision = json.loads(out)
    assert "try 2h, 9PM, tomorrow 10AM" in decision["reason"]
    assert calls == []


def test_deadline_in_the_past_is_an_error(fake_daemon: tuple[str, dict, list]) -> None:
    base_url, _routes, calls = fake_daemon
    code, out = _run({"session_id": "s1", "prompt": "/cache yesterday"}, {"CGX_URL": base_url})
    assert code == 0
    decision = json.loads(out)
    assert "in the past" in decision["reason"]
    assert calls == []


def test_unsupported_date_format_hints_iso(fake_daemon: tuple[str, dict, list]) -> None:
    base_url, _routes, calls = fake_daemon
    code, out = _run({"session_id": "s1", "prompt": "/cache 4/20/2027"}, {"CGX_URL": base_url})
    assert code == 0
    decision = json.loads(out)
    assert "YYYY-MM-DD" in decision["reason"]
    assert calls == []


@pytest.mark.invariant(decision="Skill as pure fall-through, hook does the work")
def test_daemon_unreachable_prints_helpful_reason_and_exits_zero() -> None:
    code, out = _run(
        {"session_id": "s1", "prompt": "/cache status"},
        {"CGX_URL": "http://127.0.0.1:1"},
    )
    assert code == 0
    decision = json.loads(out)
    assert decision["decision"] == "block"
    assert "daemon not reachable" in decision["reason"]
    assert "127.0.0.1:1" in decision["reason"]


@pytest.mark.invariant(decision="Skill as pure fall-through, hook does the work")
def test_default_status_action(fake_daemon: tuple[str, dict, list]) -> None:
    base_url, routes, _calls = fake_daemon
    routes[("GET", "/cgx/sessions/s1")] = (
        200,
        {"session_id": "s1", "enabled": True, "next_bump_at": None},
    )
    code, out = _run({"session_id": "s1", "prompt": "/cache"}, {"CGX_URL": base_url})
    assert code == 0
    decision = json.loads(out)
    assert "on for this session" in decision["reason"]


def test_status_shows_the_deadline_when_set(fake_daemon: tuple[str, dict, list]) -> None:
    base_url, routes, _calls = fake_daemon
    routes[("GET", "/cgx/sessions/s1")] = (
        200,
        {
            "session_id": "s1",
            "enabled": True,
            "until": 1700006000,
            "next_bump_at": 1700003000,
        },
    )
    code, out = _run({"session_id": "s1", "prompt": "/cache status"}, {"CGX_URL": base_url})
    assert code == 0
    decision = json.loads(out)
    assert "cache: on until" in decision["reason"]
