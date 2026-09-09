"""Tests for claude_gateway_extensions.cli argparse dispatch and exit codes.

The daemon is never touched: every test injects a fake ``ControlClient`` via
``main(..., client_factory=...)``, per the dependency-injection design in
``cli/__init__.py`` (command handlers take a client factory, not a module
global to monkeypatch).
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from claude_gateway_extensions.cli import EXIT_FAILED, EXIT_OK, EXIT_UNREACHABLE, EXIT_USAGE, main
from claude_gateway_extensions.cli.client import ApiError, DaemonUnreachable


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


class _TtyStdin(io.StringIO):
    """A stdin fake that reports itself as a TTY, for the interactive y/N flow."""

    def isatty(self) -> bool:
        return True


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


def test_status_json_passes_through_cache_expires_at_and_formatted() -> None:
    formatted = {
        "ttl_countdown": "55:19",
        "expires_at": "20:18",
        "next_bump_countdown": None,
        "next_bump_at": None,
    }
    client = _FakeClient(
        "http://x",
        sessions={
            "s1": {
                "session_id": "s1",
                "enabled": False,
                "cache_expires_at": 1700003319.0,
                "formatted": formatted,
            }
        },
    )
    code, out, _ = _run(["status", "--json"], client)
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload[0]["cache_expires_at"] == 1700003319.0
    assert payload[0]["formatted"] == formatted


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


@pytest.mark.invariant(decision="Absolute deadline, not an open-ended toggle")
def test_on_with_until_flag_reuses_parse_when() -> None:
    client = _FakeClient("http://x")
    code, out, _ = _run(["on", "--session", "s1", "--until", "2h"], client)
    assert code == EXIT_OK
    assert "until=" in out
    ((_name, call_args),) = client.calls
    session_id, enabled, interval_s, max_duration_s, until = call_args
    assert (session_id, enabled, interval_s, max_duration_s) == ("s1", True, None, None)
    assert isinstance(until, float)


@pytest.mark.invariant(decision="Absolute deadline, not an open-ended toggle")
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


def test_configure_service_flag_errors_with_pointer_to_service_install() -> None:
    client = _FakeClient("http://x")
    code, _, err = _run(["configure", "--service"], client, environ={"HOME": "/nonexistent-home"})
    assert code == EXIT_USAGE
    assert "cgx configure" in err
    assert "cgx service install" in err


def test_unconfigure_service_flag_errors_with_pointer_to_service_uninstall() -> None:
    client = _FakeClient("http://x")
    code, _, err = _run(["unconfigure", "--service"], client, environ={"HOME": "/nonexistent-home"})
    assert code == EXIT_USAGE
    assert "cgx unconfigure" in err
    assert "cgx service uninstall" in err


@pytest.mark.skipif(sys.platform == "win32", reason="cgx service is Linux-only")
def test_service_install_uses_injected_runner(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    calls: list[list[str]] = []

    def runner(args: list[str]) -> None:
        calls.append(list(args))

    code, out, _ = _run(
        ["service", "install", "--yes"],
        client,
        environ={"HOME": str(tmp_path)},
        service_runner=runner,
    )
    assert code == EXIT_OK
    assert "claude-gateway-extensions.service" in out
    assert any(args[:2] == ["systemctl", "--user"] for args in calls)
    assert (
        tmp_path.resolve()
        in (tmp_path / ".config" / "systemd" / "user" / "claude-gateway-extensions.service")
        .resolve()
        .parents
    )


@pytest.mark.skipif(sys.platform == "win32", reason="cgx service is Linux-only")
def test_service_install_port_and_host_flags(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    calls: list[list[str]] = []

    def runner(args: list[str]) -> None:
        calls.append(list(args))

    code, out, _ = _run(
        ["service", "install", "--port", "9999", "--host", "0.0.0.0", "--yes"],
        client,
        environ={"HOME": str(tmp_path)},
        service_runner=runner,
    )
    assert code == EXIT_OK
    unit_path = tmp_path / ".config" / "systemd" / "user" / "claude-gateway-extensions.service"
    text = unit_path.read_text()
    assert "Environment=CGX_PORT=9999" in text
    assert "Environment=CGX_HOST=0.0.0.0" in text
    assert "applied:" in out


@pytest.mark.skipif(sys.platform == "win32", reason="cgx service is Linux-only")
def test_service_restart_uses_injected_runner(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    calls: list[list[str]] = []

    def runner(args: list[str]) -> None:
        calls.append(list(args))

    code, out, _ = _run(
        ["service", "restart"], client, environ={"HOME": str(tmp_path)}, service_runner=runner
    )
    assert code == EXIT_OK
    assert calls == [["systemctl", "--user", "restart", "claude-gateway-extensions.service"]]
    assert "restarted" in out


@pytest.mark.skipif(sys.platform == "win32", reason="cgx service is Linux-only")
def test_service_status_reports_installed_unit_port(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    code, out, _ = _run(
        ["service", "install", "--port", "9999", "--yes"],
        client,
        environ={"HOME": str(tmp_path)},
        service_runner=lambda args: None,
    )
    assert code == EXIT_OK

    def query(args: list[str]) -> str:
        return "enabled" if "is-enabled" in args else "active"

    code, out, _ = _run(
        ["service", "status"], client, environ={"HOME": str(tmp_path)}, service_query=query
    )
    assert code == EXIT_OK
    assert "port=9999" in out


@pytest.mark.skipif(sys.platform == "win32", reason="cgx service is Linux-only")
@pytest.mark.invariant(
    decision="Every file a user owns is diffed and confirmed before it is written"
)
def test_service_install_dry_run_prints_diff_and_writes_nothing(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, out, _ = _run(
        ["service", "install", "--dry-run"], client, environ=environ, service_runner=lambda a: None
    )
    assert code == EXIT_OK
    assert "will create" in out
    unit_path = tmp_path / ".config" / "systemd" / "user" / "claude-gateway-extensions.service"
    assert not unit_path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="cgx service is Linux-only")
@pytest.mark.invariant(
    decision="Every file a user owns is diffed and confirmed before it is written"
)
def test_service_install_non_tty_stdin_without_yes_aborts(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, out, err = _run(
        ["service", "install"], client, environ=environ, service_runner=lambda a: None
    )
    assert code == EXIT_USAGE
    assert "will create" in out
    assert "--yes" in err
    unit_path = tmp_path / ".config" / "systemd" / "user" / "claude-gateway-extensions.service"
    assert not unit_path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="cgx service is Linux-only")
def test_service_install_interactive_y_applies(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, out, _ = _run(
        ["service", "install"],
        client,
        environ=environ,
        service_runner=lambda a: None,
        stdin=_TtyStdin("y\n"),
    )
    assert code == EXIT_OK
    assert "Apply this change to" in out
    unit_path = tmp_path / ".config" / "systemd" / "user" / "claude-gateway-extensions.service"
    assert unit_path.is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="cgx service is Linux-only")
def test_service_install_is_idempotent_on_a_second_run(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, _, _ = _run(
        ["service", "install", "--yes"], client, environ=environ, service_runner=lambda a: None
    )
    assert code == EXIT_OK

    code, out, _ = _run(
        ["service", "install", "--yes"], client, environ=environ, service_runner=lambda a: None
    )
    assert code == EXIT_OK
    assert out == "no changes\n"


@pytest.mark.skipif(sys.platform == "win32", reason="cgx service is Linux-only")
def test_service_uninstall_round_trip_with_yes(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, _, _ = _run(
        ["service", "install", "--yes"], client, environ=environ, service_runner=lambda a: None
    )
    assert code == EXIT_OK

    code, out, _ = _run(
        ["service", "uninstall", "--yes"], client, environ=environ, service_runner=lambda a: None
    )
    assert code == EXIT_OK
    assert "applied:" in out
    unit_path = tmp_path / ".config" / "systemd" / "user" / "claude-gateway-extensions.service"
    assert not unit_path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="cgx service is Linux-only")
def test_service_uninstall_with_nothing_installed_is_no_changes(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, out, _ = _run(
        ["service", "uninstall", "--yes"], client, environ=environ, service_runner=lambda a: None
    )
    assert code == EXIT_OK
    assert out == "no changes\n"


@pytest.mark.skipif(sys.platform == "win32", reason="cgx service is Linux-only")
@pytest.mark.invariant(
    decision="Every file a user owns is diffed and confirmed before it is written"
)
def test_service_uninstall_non_tty_stdin_without_yes_aborts(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, _, _ = _run(
        ["service", "install", "--yes"], client, environ=environ, service_runner=lambda a: None
    )
    assert code == EXIT_OK

    code, out, err = _run(
        ["service", "uninstall"], client, environ=environ, service_runner=lambda a: None
    )
    assert code == EXIT_USAGE
    assert "will remove" in out
    assert "--yes" in err
    unit_path = tmp_path / ".config" / "systemd" / "user" / "claude-gateway-extensions.service"
    assert unit_path.is_file()


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_configure_writes_url_for_explicit_port(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    code, out, _ = _run(
        ["configure", "--scope", "user", "--port", "9999", "--yes"],
        client,
        environ={"HOME": str(tmp_path)},
    )
    assert code == EXIT_OK
    assert "daemon URL: http://127.0.0.1:9999" in out
    settings_path = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())
    assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9999"
    assert data["env"]["CGX_URL"] == "http://127.0.0.1:9999"


@pytest.mark.skipif(sys.platform == "win32", reason="cgx service is Linux-only")
@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_configure_port_falls_back_to_installed_service_port(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, _, _ = _run(
        ["service", "install", "--port", "9999", "--yes"],
        client,
        environ=environ,
        service_runner=lambda a: None,
    )
    assert code == EXIT_OK

    code, out, _ = _run(["configure", "--scope", "user", "--yes"], client, environ=environ)
    assert code == EXIT_OK
    assert "daemon URL: http://127.0.0.1:9999" in out


@pytest.mark.invariant(
    decision="Every file a user owns is diffed and confirmed before it is written"
)
def test_configure_dry_run_prints_diffs_and_writes_nothing(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, out, _ = _run(["configure", "--scope", "user", "--dry-run"], client, environ=environ)
    assert code == EXIT_OK
    assert "will create" in out
    assert not (tmp_path / ".claude" / "settings.json").exists()


@pytest.mark.invariant(
    decision="Every file a user owns is diffed and confirmed before it is written"
)
def test_configure_non_tty_stdin_without_yes_aborts(tmp_path: Path) -> None:
    """Non-interactive stdin (the common case under a test harness) must never silently write."""
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, out, err = _run(["configure", "--scope", "user"], client, environ=environ)
    assert code == EXIT_USAGE
    assert "will create" in out
    assert "--yes" in err
    assert not (tmp_path / ".claude" / "settings.json").exists()


@pytest.mark.invariant(
    decision="Every file a user owns is diffed and confirmed before it is written"
)
def test_configure_interactive_y_confirms_each_file(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, out, _ = _run(
        ["configure", "--scope", "user"], client, environ=environ, stdin=_TtyStdin("y\ny\n")
    )
    assert code == EXIT_OK
    assert "Apply this change to" in out
    assert "applied:" in out
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert (tmp_path / ".claude" / "skills" / "cache" / "SKILL.md").exists()


@pytest.mark.invariant(
    decision="Every file a user owns is diffed and confirmed before it is written"
)
def test_configure_interactive_partial_acceptance(tmp_path: Path) -> None:
    """Rejecting the skill file must still apply the accepted settings.json change."""
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, out, _ = _run(
        ["configure", "--scope", "user"], client, environ=environ, stdin=_TtyStdin("y\nn\n")
    )
    assert code == EXIT_OK
    assert "applied:" in out
    assert "skipped:" in out
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / ".claude" / "skills" / "cache" / "SKILL.md").exists()


@pytest.mark.invariant(
    decision="Every file a user owns is diffed and confirmed before it is written"
)
def test_configure_interactive_reject_all_is_a_failure(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, out, _ = _run(
        ["configure", "--scope", "user"], client, environ=environ, stdin=_TtyStdin("n\nn\n")
    )
    assert code == EXIT_FAILED
    assert "skipped:" in out
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_configure_is_idempotent_on_a_second_run(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, _, _ = _run(["configure", "--scope", "user", "--yes"], client, environ=environ)
    assert code == EXIT_OK

    code, out, _ = _run(["configure", "--scope", "user", "--yes"], client, environ=environ)
    assert code == EXIT_OK
    assert out == "daemon URL: http://127.0.0.1:8787\nno changes\n"


def test_configure_scope_local_writes_settings_local_json(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    project = tmp_path / "project"
    project.mkdir()
    environ = {"HOME": str(tmp_path / "home")}
    code, out, _ = _run(
        ["configure", "--scope", "local", "--yes"], client, environ=environ, cwd=project
    )
    assert code == EXIT_OK
    assert (project / ".claude" / "settings.local.json").is_file()
    assert not (project / ".claude" / "settings.json").exists()
    # local shares project's skill directory.
    assert (project / ".claude" / "skills" / "cache" / "SKILL.md").is_file()
    assert "daemon URL" in out


@pytest.mark.invariant(decision="Environment variables only, split by process")
def test_configure_scope_managed_is_rejected_by_argparse() -> None:
    """argparse itself rejects an unsupported ``--scope`` choice (writes to real stderr)."""
    client = _FakeClient("http://x")
    code, _, _ = _run(["configure", "--scope", "managed"], client)
    assert code == EXIT_USAGE


def test_unconfigure_round_trip_with_yes(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, _, _ = _run(["configure", "--scope", "user", "--yes"], client, environ=environ)
    assert code == EXIT_OK

    code, out, _ = _run(["unconfigure", "--scope", "user", "--yes"], client, environ=environ)
    assert code == EXIT_OK
    assert "applied:" in out
    settings_path = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())
    assert "env" not in data or "ANTHROPIC_BASE_URL" not in data.get("env", {})
    assert not (tmp_path / ".claude" / "skills" / "cache").exists()


def test_unconfigure_with_nothing_installed_is_no_changes(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, out, _ = _run(["unconfigure", "--scope", "user", "--yes"], client, environ=environ)
    assert code == EXIT_OK
    assert out == "no changes\n"


def test_configure_fork_review_flag_wires_hook_and_copies_template(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, _, _ = _run(
        ["configure", "--scope", "user", "--fork-review", "btw:/btw", "--yes"],
        client,
        environ=environ,
    )
    assert code == EXIT_OK
    settings_path = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())
    commands = {e["hooks"][0]["command"] for e in data["hooks"]["UserPromptSubmit"]}
    assert "cgx fork-review --template btw --prefix /btw" in commands
    assert (tmp_path / ".config" / "cgx" / "templates" / "btw.txt").is_file()


def test_configure_fork_review_unknown_template_fails(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, _, err = _run(
        ["configure", "--scope", "user", "--fork-review", "does-not-exist-at-all", "--yes"],
        client,
        environ=environ,
    )
    assert code == EXIT_FAILED
    assert "does-not-exist-at-all" in err
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_unconfigure_fork_review_round_trip(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, _, _ = _run(
        ["configure", "--scope", "user", "--fork-review", "btw:/btw", "--yes"],
        client,
        environ=environ,
    )
    assert code == EXIT_OK

    code, _, _ = _run(
        ["unconfigure", "--scope", "user", "--fork-review", "btw:/btw", "--yes"],
        client,
        environ=environ,
    )
    assert code == EXIT_OK
    settings_path = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())
    entries = data.get("hooks", {}).get("UserPromptSubmit", [])
    commands = {e["hooks"][0]["command"] for e in entries}
    assert "cgx fork-review --template btw --prefix /btw" not in commands


@pytest.mark.invariant(
    decision="Every file a user owns is diffed and confirmed before it is written"
)
def test_unconfigure_non_tty_stdin_without_yes_aborts(tmp_path: Path) -> None:
    client = _FakeClient("http://x")
    environ = {"HOME": str(tmp_path)}
    code, _, _ = _run(["configure", "--scope", "user", "--yes"], client, environ=environ)
    assert code == EXIT_OK

    code, out, err = _run(["unconfigure", "--scope", "user"], client, environ=environ)
    assert code == EXIT_USAGE
    assert "will remove" in out or "ANTHROPIC_BASE_URL" in out
    assert "--yes" in err
    assert (tmp_path / ".claude" / "settings.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="cgx service is Linux-only")
def test_service_status_reports_not_installed_and_daemon_health() -> None:
    client = _FakeClient("http://x")
    code, out, _ = _run(["service", "status"], client, environ={"HOME": "/nonexistent-home"})
    assert code == EXIT_OK
    assert "not installed; run `cgx service install`" in out
    assert "daemon healthy" in out
    assert client.calls == [("health", ())]


@pytest.mark.skipif(sys.platform == "win32", reason="cgx service is Linux-only")
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

    fake_module = types.ModuleType("claude_gateway_extensions.server")
    calls: list[Any] = []
    fake_module.run = lambda config, **kwargs: calls.append(config)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_gateway_extensions.server", fake_module)

    client = _FakeClient("http://x")
    code, _, _ = _run(["serve"], client)
    assert code == EXIT_OK
    assert len(calls) == 1


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_serve_port_flag_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    fake_module = types.ModuleType("claude_gateway_extensions.server")
    calls: list[Any] = []
    fake_module.run = lambda config, **kwargs: calls.append(config)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_gateway_extensions.server", fake_module)

    client = _FakeClient("http://x")
    code, _, _ = _run(
        ["serve", "--port", "9999", "--host", "0.0.0.0"],
        client,
        environ={"CGX_PORT": "1111"},
    )
    assert code == EXIT_OK
    assert calls[0].port == 9999
    assert calls[0].host == "0.0.0.0"


def test_serve_without_flags_uses_env_port(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    fake_module = types.ModuleType("claude_gateway_extensions.server")
    calls: list[Any] = []
    fake_module.run = lambda config, **kwargs: calls.append(config)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_gateway_extensions.server", fake_module)

    client = _FakeClient("http://x")
    code, _, _ = _run(["serve"], client, environ={"CGX_PORT": "1111"})
    assert code == EXIT_OK
    assert calls[0].port == 1111


def test_serve_handover_flag_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    fake_module = types.ModuleType("claude_gateway_extensions.server")
    calls: list[dict[str, Any]] = []
    fake_module.run = lambda config, **kwargs: calls.append(kwargs)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_gateway_extensions.server", fake_module)

    client = _FakeClient("http://x")
    code, _, _ = _run(["serve", "--handover"], client)
    assert code == EXIT_OK
    assert calls == [{"handover": True}]


def test_serve_without_handover_flag_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    fake_module = types.ModuleType("claude_gateway_extensions.server")
    calls: list[dict[str, Any]] = []
    fake_module.run = lambda config, **kwargs: calls.append(kwargs)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_gateway_extensions.server", fake_module)

    client = _FakeClient("http://x")
    code, _, _ = _run(["serve"], client)
    assert code == EXIT_OK
    assert calls == [{"handover": False}]


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


@pytest.mark.parametrize("subcommand", ["install", "uninstall", "status"])
@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_service_any_subcommand_is_linux_only(
    subcommand: str, platform: str, tmp_path: Path
) -> None:
    """``cgx service <anything>`` refuses on non-Linux with a one-line pointer."""
    client = _FakeClient("http://x")
    code, _, err = _run(
        ["service", subcommand],
        client,
        environ={"HOME": str(tmp_path)},
        platform=platform,
    )
    assert code == EXIT_FAILED
    assert "cgx service manages a systemd user unit and is Linux-only" in err
    assert "cgx serve --port <N>" in err
    assert "cgx configure --port <N>" in err
    unit_path = tmp_path / ".config" / "systemd" / "user" / "claude-gateway-extensions.service"
    assert not unit_path.exists()


def test_service_install_still_works_when_platform_is_linux(tmp_path: Path) -> None:
    """The explicit ``platform="linux"`` default must not regress the existing behaviour."""
    client = _FakeClient("http://x")
    code, out, _ = _run(
        ["service", "install", "--yes"],
        client,
        environ={"HOME": str(tmp_path)},
        service_runner=lambda args: None,
        platform="linux",
    )
    assert code == EXIT_OK
    assert "claude-gateway-extensions.service" in out
