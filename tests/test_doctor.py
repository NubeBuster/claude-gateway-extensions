"""Tests for claude_gateway_extensions.cli.doctor."""

from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from claude_gateway_extensions.cli import doctor, install, service
from claude_gateway_extensions.cli.client import DaemonUnreachable
from claude_gateway_extensions.config import Config

URL = "http://127.0.0.1:8787"


class _FakeClient:
    def __init__(self, base_url: str, *, healthy: bool = True) -> None:
        self.base_url = base_url
        self.healthy = healthy

    def health(self) -> dict[str, Any]:
        if not self.healthy:
            raise DaemonUnreachable("nope")
        return {"ok": True, "version": "0.0.0-test", "upstream": "http://example", "sessions": 0}

    def get_config(self) -> dict[str, Any]:
        # ``path: None`` deliberately never matches a real path, so the
        # "daemon resolves a different path" warning never fires here --
        # this fake exists to satisfy check_daemon/check_config_file's
        # daemon-reachable branch, not to test the mismatch itself (that has
        # its own dedicated test below).
        return {"path": None, "present": False, "parse_error": None, "shadow_fork": {}}


def _healthy_factory(base_url: str) -> _FakeClient:
    return _FakeClient(base_url, healthy=True)


def _unhealthy_factory(base_url: str) -> _FakeClient:
    return _FakeClient(base_url, healthy=False)


def test_detects_set_anthropic_api_key(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path), "ANTHROPIC_API_KEY": "sk-something"}
    config = Config.from_env({"CGX_URL": URL})
    stdout = io.StringIO()
    code = doctor.run_doctor(config, environ, tmp_path, stdout, client_factory=_healthy_factory)
    assert code == 1
    assert "fail: credential override present" in stdout.getvalue()
    assert "ANTHROPIC_API_KEY" in stdout.getvalue()


def test_all_ok_after_full_install(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    install.install(
        scope="user",
        url=URL,
        force=False,
        dry_run=False,
        environ=environ,
        cwd=tmp_path,
    )
    config = Config.from_env({"CGX_URL": URL, "HOME": str(tmp_path)})
    stdout = io.StringIO()
    code = doctor.run_doctor(config, environ, tmp_path, stdout, client_factory=_healthy_factory)
    output = stdout.getvalue()
    assert "fail" not in output
    assert code == 0


def test_daemon_unreachable_is_a_failure(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    config = Config.from_env({"CGX_URL": URL})
    stdout = io.StringIO()
    code = doctor.run_doctor(config, environ, tmp_path, stdout, client_factory=_unhealthy_factory)
    assert code == 1
    assert "fail: daemon not reachable" in stdout.getvalue()


@pytest.mark.invariant(decision="The port is probed before it is bound")
def test_daemon_answers_but_not_as_cgx_is_a_failure(tmp_path: Path) -> None:
    """A 2xx response that is not shaped like cgx's own health body is a failure, not `ok`."""

    class _ForeignClient:
        def __init__(self, base_url: str) -> None:
            del base_url

        def health(self) -> dict[str, Any]:
            return {"status": "ok"}  # a foreign program's own idea of "healthy"

    environ = {"HOME": str(tmp_path)}
    config = Config.from_env({"CGX_URL": URL})
    stdout = io.StringIO()
    code = doctor.run_doctor(config, environ, tmp_path, stdout, client_factory=_ForeignClient)
    assert code == 1
    assert "fail: " in stdout.getvalue()
    assert "not as cgx" in stdout.getvalue()


def test_missing_hook_and_skill_are_warnings_not_failures(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    config = Config.from_env({"CGX_URL": URL})
    stdout = io.StringIO()
    code = doctor.run_doctor(config, environ, tmp_path, stdout, client_factory=_healthy_factory)
    output = stdout.getvalue()
    assert code == 0
    assert "warn:" in output


@pytest.mark.skipif(sys.platform == "win32", reason="run_doctor skips the systemd check there")
def test_missing_service_unit_is_a_warning_with_install_hint(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    config = Config.from_env({"CGX_URL": URL})
    stdout = io.StringIO()
    code = doctor.run_doctor(config, environ, tmp_path, stdout, client_factory=_healthy_factory)
    output = stdout.getvalue()
    assert code == 0
    assert "warn: systemd service not installed; run `cgx service install`" in output


def test_active_service_unit_is_ok(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    service.install_service(environ, runner=lambda args: None)

    def query(args: Sequence[str]) -> str:
        return "enabled" if "is-enabled" in args else "active"

    check = doctor.check_service(environ, query)
    assert check.status == "ok"
    assert "enabled=True, active=True" in check.message


def test_run_doctor_skips_service_check_on_non_linux(tmp_path: Path) -> None:
    """On a non-Linux ``platform``, run_doctor must not warn about a systemd unit."""
    environ = {"HOME": str(tmp_path)}
    config = Config.from_env({"CGX_URL": URL})
    stdout = io.StringIO()
    code = doctor.run_doctor(
        config, environ, tmp_path, stdout, client_factory=_healthy_factory, platform="win32"
    )
    output = stdout.getvalue()
    assert code == 0
    assert "warn: systemd service not installed" not in output
    assert "ok: systemd service checks skipped (not on Linux)" in output


def test_run_doctor_still_checks_service_when_platform_is_linux(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    config = Config.from_env({"CGX_URL": URL})
    stdout = io.StringIO()
    code = doctor.run_doctor(
        config, environ, tmp_path, stdout, client_factory=_healthy_factory, platform="linux"
    )
    output = stdout.getvalue()
    assert code == 0
    assert "warn: systemd service not installed; run `cgx service install`" in output


def test_api_key_helper_in_settings_is_detected(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"apiKeyHelper": "/usr/local/bin/get-key"}))
    config = Config.from_env({"CGX_URL": URL})
    stdout = io.StringIO()
    code = doctor.run_doctor(config, environ, tmp_path, stdout, client_factory=_healthy_factory)
    assert code == 1
    assert "apiKeyHelper" in stdout.getvalue()


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_port_consistency_ok_when_only_one_source_known(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    config = Config.from_env({"CGX_URL": URL})
    check = doctor.check_port_consistency(config, environ, tmp_path, True)
    assert check.status == "ok"


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_port_consistency_ok_when_settings_and_daemon_agree(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    install.install(
        scope="user", url=URL, force=False, dry_run=False, environ=environ, cwd=tmp_path
    )
    config = Config.from_env({"CGX_URL": URL})
    check = doctor.check_port_consistency(config, environ, tmp_path, True)
    assert check.status == "ok"


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_port_consistency_warns_on_settings_daemon_port_mismatch(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    install.install(
        scope="user",
        url="http://127.0.0.1:9999",
        force=False,
        dry_run=False,
        environ=environ,
        cwd=tmp_path,
    )
    config = Config.from_env({"CGX_URL": URL})
    check = doctor.check_port_consistency(config, environ, tmp_path, True)
    assert check.status == "warn"
    assert "9999" in check.message
    assert "8787" in check.message
    assert "--port" in check.message


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_port_consistency_warns_on_unit_daemon_port_mismatch(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    service.install_service(environ, runner=lambda args: None, port=9999)
    config = Config.from_env({"CGX_URL": URL})
    check = doctor.check_port_consistency(config, environ, tmp_path, True)
    assert check.status == "warn"
    assert "9999" in check.message


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_port_consistency_ignores_daemon_port_when_unreachable(tmp_path: Path) -> None:
    """A daemon that failed the reachability check must not also produce a port mismatch."""
    environ = {"HOME": str(tmp_path)}
    service.install_service(environ, runner=lambda args: None, port=9999)
    config = Config.from_env({"CGX_URL": URL})
    check = doctor.check_port_consistency(config, environ, tmp_path, False)
    assert check.status == "ok"


def test_shadow_fork_check_reports_resolving_template(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    home.mkdir()
    cwd.mkdir()
    environ = {"HOME": str(home)}
    spec = install.ShadowForkSpec(name="btw", prefix="/btw")
    install.install(
        scope="user",
        url=URL,
        force=False,
        dry_run=False,
        environ=environ,
        cwd=cwd,
        shadow_forks=[spec],
    )
    results = doctor.check_shadow_fork_templates(environ, cwd)
    assert len(results) == 1
    assert results[0].status == "ok"
    assert "'btw'" in results[0].message


def test_shadow_fork_check_fails_on_missing_template(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    cwd.mkdir()
    environ = {"HOME": str(home)}
    path = install.settings_path("user", environ, cwd)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "cgx shadow-fork --template ghost",
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )
    results = doctor.check_shadow_fork_templates(environ, cwd)
    assert len(results) == 1
    assert results[0].status == "fail"
    assert "ghost" in results[0].message
    assert "template not found" in results[0].message


def test_shadow_fork_check_warns_on_event_mismatch(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    cwd.mkdir()
    environ = {"HOME": str(home)}
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "moved.txt").write_text(
        "intervene: deny\nevent: PreToolUse\n\nreview\n", encoding="utf-8"
    )
    environ["CGX_TEMPLATE_DIR"] = str(template_dir)

    path = install.settings_path("user", environ, cwd)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "cgx shadow-fork --template moved",
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )
    results = doctor.check_shadow_fork_templates(environ, cwd)
    assert len(results) == 1
    assert results[0].status == "warn"
    assert "PreToolUse" in results[0].message
    assert "UserPromptSubmit" in results[0].message


def test_shadow_fork_check_empty_when_no_entries(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    home.mkdir()
    cwd.mkdir()
    environ = {"HOME": str(home)}
    install.install(scope="user", url=URL, force=False, dry_run=False, environ=environ, cwd=cwd)
    assert doctor.check_shadow_fork_templates(environ, cwd) == []


def test_state_dir_wrong_mode_is_a_warning(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    os.chmod(state_dir, 0o755)
    config = Config.from_env({"CGX_URL": URL, "CGX_STATE_DIR": str(state_dir)})
    stdout = io.StringIO()
    code = doctor.run_doctor(config, environ, tmp_path, stdout, client_factory=_healthy_factory)
    output = stdout.getvalue()
    assert code == 0
    assert "warn: state dir" in output


def test_check_config_file_ok_when_absent(tmp_path: Path) -> None:
    config = Config.from_env({"CGX_URL": URL, "HOME": str(tmp_path)})
    client = _healthy_factory(URL)
    result = doctor.check_config_file(config, client, daemon_reachable=True)  # type: ignore[arg-type]
    assert result.status == "ok"
    assert "not present" in result.message


def test_check_config_file_ok_when_valid(tmp_path: Path) -> None:
    config = Config.from_env(
        {"CGX_URL": URL, "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    )
    config.config_file.parent.mkdir(parents=True)
    config.config_file.write_text("[shadow_fork]\nmax_tokens = 512\n")
    client = _healthy_factory(URL)
    result = doctor.check_config_file(config, client, daemon_reachable=True)  # type: ignore[arg-type]
    assert result.status == "ok"
    assert "parses cleanly" in result.message


def test_check_config_file_fail_on_invalid_toml(tmp_path: Path) -> None:
    config = Config.from_env(
        {"CGX_URL": URL, "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    )
    config.config_file.parent.mkdir(parents=True)
    config.config_file.write_text("[shadow_fork]\nmax_tokens = not valid toml\n")
    client = _healthy_factory(URL)
    result = doctor.check_config_file(config, client, daemon_reachable=True)  # type: ignore[arg-type]
    assert result.status == "fail"
    assert "invalid" in result.message


def test_check_config_file_warns_when_daemon_resolves_a_different_path(tmp_path: Path) -> None:
    config = Config.from_env(
        {"CGX_URL": URL, "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    )

    class _MismatchedClient:
        def get_config(self) -> dict[str, Any]:
            return {"path": "/somewhere/else/config.toml"}

    result = doctor.check_config_file(
        config,
        _MismatchedClient(),  # type: ignore[arg-type]
        daemon_reachable=True,
    )
    assert result.status == "warn"
    assert "daemon resolves" in result.message


def test_check_config_file_skips_daemon_comparison_when_unreachable(tmp_path: Path) -> None:
    config = Config.from_env(
        {"CGX_URL": URL, "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    )
    client = _unhealthy_factory(URL)
    result = doctor.check_config_file(config, client, daemon_reachable=False)  # type: ignore[arg-type]
    assert result.status == "ok"
