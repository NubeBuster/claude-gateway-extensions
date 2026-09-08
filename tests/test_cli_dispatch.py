"""Tests for claude_cache_bump_gateway.cli argparse dispatch and exit codes.

The daemon is never touched: every test injects a fake ``ControlClient`` via
``main(..., client_factory=...)``, per the dependency-injection design in
``cli/__init__.py`` (command handlers take a client factory, not a module
global to monkeypatch).
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest

from claude_cache_bump_gateway.cli import EXIT_FAILED, EXIT_OK, EXIT_UNREACHABLE, EXIT_USAGE, main
from claude_cache_bump_gateway.cli.client import ApiError, DaemonUnreachable


class _FakeClient:
    """Records calls and returns pre-programmed responses or raises pre-programmed errors."""

    def __init__(
        self,
        base_url: str,
        *,
        sessions: dict[str, dict[str, Any]] | None = None,
        raise_unreachable: bool = False,
        raise_api_error: ApiError | None = None,
        bump_result: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url
        self.sessions = sessions or {}
        self.raise_unreachable = raise_unreachable
        self.raise_api_error = raise_api_error
        self.bump_result = bump_result
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def _maybe_raise(self) -> None:
        if self.raise_unreachable:
            raise DaemonUnreachable("nope")
        if self.raise_api_error is not None:
            raise self.raise_api_error

    def health(self) -> dict[str, Any]:
        self.calls.append(("health", ()))
        self._maybe_raise()
        return {"status": "ok"}

    def list_sessions(self) -> list[dict[str, Any]]:
        self.calls.append(("list_sessions", ()))
        self._maybe_raise()
        return list(self.sessions.values())

    def get_session(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("get_session", (session_id,)))
        self._maybe_raise()
        if session_id not in self.sessions:
            raise ApiError(404, "not found")
        return self.sessions[session_id]

    def set_session(
        self,
        session_id: str,
        enabled: bool,
        interval_s: int | None = None,
        max_duration_s: int | None = None,
        until: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("set_session", (session_id, enabled, interval_s, max_duration_s, until)))
        self._maybe_raise()
        session = {
            "session_id": session_id,
            "enabled": enabled,
            "until": until,
            "next_bump_at": None,
            "last_result": None,
            "stopped_reason": None,
        }
        self.sessions[session_id] = session
        return session

    def bump(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("bump", (session_id,)))
        self._maybe_raise()
        return self.bump_result if self.bump_result is not None else {"outcome": "ok", "usage": {}}

    def forget(self, session_id: str) -> None:
        self.calls.append(("forget", (session_id,)))
        self._maybe_raise()
        self.sessions.pop(session_id, None)


def _run(argv: list[str], client: _FakeClient, **kwargs: Any) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(
        argv,
        environ=kwargs.pop("environ", {}),
        stdout=stdout,
        stderr=stderr,
        client_factory=lambda base_url: client,
        **kwargs,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_help_exits_zero() -> None:
    assert main(["--help"]) == EXIT_OK


def test_no_command_is_usage_error() -> None:
    assert main([]) == EXIT_USAGE


def test_status_lists_all_sessions() -> None:
    client = _FakeClient("http://x", sessions={"s1": {"session_id": "s1", "enabled": True}})
    code, out, _ = _run(["status"], client)
    assert code == EXIT_OK
    assert "s1" in out
    assert client.calls == [("list_sessions", ())]


def test_status_single_session_via_flag() -> None:
    client = _FakeClient("http://x", sessions={"s1": {"session_id": "s1", "enabled": False}})
    code, out, _ = _run(["status", "--session", "s1"], client)
    assert code == EXIT_OK
    assert "s1" in out
    assert client.calls == [("get_session", ("s1",))]


def test_status_json() -> None:
    client = _FakeClient("http://x", sessions={"s1": {"session_id": "s1", "enabled": True}})
    code, out, _ = _run(["status", "--json"], client)
    assert code == EXIT_OK
    assert '"session_id": "s1"' in out


def test_status_unknown_session_is_failure() -> None:
    client = _FakeClient("http://x")
    code, _, err = _run(["status", "--session", "missing"], client)
    assert code == EXIT_FAILED
    assert "missing" in err


def test_status_daemon_unreachable() -> None:
    client = _FakeClient("http://x", raise_unreachable=True)
    code, _, err = _run(["status"], client)
    assert code == EXIT_UNREACHABLE
    assert err


def test_on_requires_session_id() -> None:
    client = _FakeClient("http://x")
    code, _, err = _run(["on"], client)
    assert code == EXIT_USAGE
    assert "session" in err.lower()


def test_on_uses_claude_session_id_env() -> None:
    client = _FakeClient("http://x")
    code, out, _ = _run(["on"], client, environ={"CLAUDE_SESSION_ID": "s1"})
    assert code == EXIT_OK
    assert "s1" in out
    assert client.calls == [("set_session", ("s1", True, None, None, None))]


def test_on_explicit_session_and_duration() -> None:
    client = _FakeClient("http://x")
    code, _, _ = _run(
        ["on", "--session", "s1", "--interval", "60", "--max-duration", "7200"], client
    )
    assert code == EXIT_OK
    assert client.calls == [("set_session", ("s1", True, 60, 7200, None))]


def test_on_with_until_flag_reuses_parse_when() -> None:
    client = _FakeClient("http://x")
    code, out, _ = _run(["on", "--session", "s1", "--until", "2h"], client)
    assert code == EXIT_OK
    assert "until=" in out
    ((_name, call_args),) = client.calls
    session_id, enabled, interval_s, max_duration_s, until = call_args
    assert (session_id, enabled, interval_s, max_duration_s) == ("s1", True, None, None)
    assert isinstance(until, float)


def test_on_with_until_status_word_is_a_usage_error() -> None:
    client = _FakeClient("http://x")
    code, _, err = _run(["on", "--session", "s1", "--until", "status"], client)
    assert code == EXIT_USAGE
    assert "expected a deadline" in err
    assert client.calls == []


def test_on_with_unparseable_until_is_a_usage_error() -> None:
    client = _FakeClient("http://x")
    code, _, err = _run(["on", "--session", "s1", "--until", "banana"], client)
    assert code == EXIT_USAGE
    assert "banana" in err
    assert client.calls == []


def test_off() -> None:
    client = _FakeClient("http://x")
    code, out, _ = _run(["off", "--session", "s1"], client)
    assert code == EXIT_OK
    assert "enabled=False" in out
    assert client.calls == [("set_session", ("s1", False, None, None, None))]


def test_now_ok_outcome_exits_zero() -> None:
    client = _FakeClient(
        "http://x", bump_result={"outcome": "ok", "usage": {"cache_read_input_tokens": 4821}}
    )
    code, out, _ = _run(["now", "--session", "s1"], client)
    assert code == EXIT_OK
    assert "4821" in out


def test_now_non_ok_outcome_exits_one() -> None:
    client = _FakeClient("http://x", bump_result={"outcome": "unauthorized", "usage": {}})
    code, out, _ = _run(["now", "--session", "s1"], client)
    assert code == EXIT_FAILED
    assert "unauthorized" in out


def test_forget() -> None:
    client = _FakeClient("http://x", sessions={"s1": {"session_id": "s1"}})
    code, out, _ = _run(["forget", "--session", "s1"], client)
    assert code == EXIT_OK
    assert "s1" in out
    assert "s1" not in client.sessions


def test_install_service_flag_errors_with_pointer_to_service_install() -> None:
    client = _FakeClient("http://x")
    code, _, err = _run(["install", "--service"], client, environ={"HOME": "/nonexistent-home"})
    assert code == EXIT_USAGE
    assert "ccbg service install" in err


def test_uninstall_service_flag_errors_with_pointer_to_service_uninstall() -> None:
    client = _FakeClient("http://x")
    code, _, err = _run(["uninstall", "--service"], client, environ={"HOME": "/nonexistent-home"})
    assert code == EXIT_USAGE
    assert "ccbg service uninstall" in err


def test_service_install_uses_injected_runner(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    calls: list[list[str]] = []

    def runner(args: list[str]) -> None:
        calls.append(list(args))

    code, out, _ = _run(
        ["service", "install"],
        client,
        environ={"HOME": str(tmp_path)},
        service_runner=runner,
    )
    assert code == EXIT_OK
    assert "claude-cache-bump-gateway.service" in out
    assert any(args[:2] == ["systemctl", "--user"] for args in calls)
    assert (
        tmp_path.resolve()
        in (tmp_path / ".config" / "systemd" / "user" / "claude-cache-bump-gateway.service")
        .resolve()
        .parents
    )


def test_service_status_reports_not_installed_and_daemon_health() -> None:
    client = _FakeClient("http://x")
    code, out, _ = _run(["service", "status"], client, environ={"HOME": "/nonexistent-home"})
    assert code == EXIT_OK
    assert "not installed; run `ccbg service install`" in out
    assert "daemon healthy" in out
    assert client.calls == [("health", ())]


def test_service_status_daemon_unreachable_is_a_failure() -> None:
    client = _FakeClient("http://x", raise_unreachable=True)
    code, out, _ = _run(["service", "status"], client, environ={"HOME": "/nonexistent-home"})
    assert code == EXIT_FAILED
    assert "fail: daemon not reachable" in out


def test_doctor_reports_daemon_unreachable() -> None:
    client = _FakeClient("http://x", raise_unreachable=True)
    code, out, _ = _run(["doctor"], client, environ={"HOME": "/nonexistent-home"})
    assert code == EXIT_FAILED
    assert "fail:" in out


def test_serve_imports_server_module_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    """``serve`` must not require the server package to exist until invoked."""
    import types

    fake_module = types.ModuleType("claude_cache_bump_gateway.server")
    calls: list[Any] = []
    fake_module.run = lambda config: calls.append(config)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_cache_bump_gateway.server", fake_module)

    client = _FakeClient("http://x")
    code, _, _ = _run(["serve"], client)
    assert code == EXIT_OK
    assert len(calls) == 1


def test_serve_missing_server_module_is_a_clean_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing dependency of the server package must fail cleanly, not traceback.

    The server package itself always ships alongside the CLI, so the realistic
    failure is its ``aiohttp`` dependency being absent — which surfaces as the
    same ``ModuleNotFoundError`` out of the lazy import.
    """
    import importlib

    def _missing(name: str) -> object:
        raise ModuleNotFoundError(f"No module named 'aiohttp' (importing {name})")

    monkeypatch.setattr(importlib, "import_module", _missing)
    client = _FakeClient("http://x")
    code, _, err = _run(["serve"], client)
    assert code == EXIT_FAILED
    assert "server" in err.lower()
