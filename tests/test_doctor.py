"""Tests for claude_cache_bump_gateway.cli.doctor."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from claude_cache_bump_gateway.cli import doctor, install, service
from claude_cache_bump_gateway.cli.client import DaemonUnreachable
from claude_cache_bump_gateway.config import Config

URL = "http://127.0.0.1:8787"


class _FakeClient:
    def __init__(self, base_url: str, *, healthy: bool = True) -> None:
        self.base_url = base_url
        self.healthy = healthy

    def health(self) -> dict[str, Any]:
        if not self.healthy:
            raise DaemonUnreachable("nope")
        return {"status": "ok"}


def _healthy_factory(base_url: str) -> _FakeClient:
    return _FakeClient(base_url, healthy=True)


def _unhealthy_factory(base_url: str) -> _FakeClient:
    return _FakeClient(base_url, healthy=False)


def test_detects_set_anthropic_api_key(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path), "ANTHROPIC_API_KEY": "sk-something"}
    config = Config.from_env({"CCBG_URL": URL})
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
    config = Config.from_env({"CCBG_URL": URL, "HOME": str(tmp_path)})
    stdout = io.StringIO()
    code = doctor.run_doctor(config, environ, tmp_path, stdout, client_factory=_healthy_factory)
    output = stdout.getvalue()
    assert "fail" not in output
    assert code == 0


def test_daemon_unreachable_is_a_failure(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    config = Config.from_env({"CCBG_URL": URL})
    stdout = io.StringIO()
    code = doctor.run_doctor(config, environ, tmp_path, stdout, client_factory=_unhealthy_factory)
    assert code == 1
    assert "fail: daemon not reachable" in stdout.getvalue()


def test_missing_hook_and_skill_are_warnings_not_failures(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    config = Config.from_env({"CCBG_URL": URL})
    stdout = io.StringIO()
    code = doctor.run_doctor(config, environ, tmp_path, stdout, client_factory=_healthy_factory)
    output = stdout.getvalue()
    assert code == 0
    assert "warn:" in output


def test_missing_service_unit_is_a_warning_with_install_hint(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    config = Config.from_env({"CCBG_URL": URL})
    stdout = io.StringIO()
    code = doctor.run_doctor(config, environ, tmp_path, stdout, client_factory=_healthy_factory)
    output = stdout.getvalue()
    assert code == 0
    assert "warn: systemd service not installed; run `ccbg service install`" in output


def test_active_service_unit_is_ok(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    service.install_service(environ, runner=lambda args: None)

    def query(args: Sequence[str]) -> str:
        return "enabled" if "is-enabled" in args else "active"

    check = doctor.check_service(environ, query)
    assert check.status == "ok"
    assert "enabled=True, active=True" in check.message


def test_api_key_helper_in_settings_is_detected(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"apiKeyHelper": "/usr/local/bin/get-key"}))
    config = Config.from_env({"CCBG_URL": URL})
    stdout = io.StringIO()
    code = doctor.run_doctor(config, environ, tmp_path, stdout, client_factory=_healthy_factory)
    assert code == 1
    assert "apiKeyHelper" in stdout.getvalue()


def test_state_dir_wrong_mode_is_a_warning(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    os.chmod(state_dir, 0o755)
    config = Config.from_env({"CCBG_URL": URL, "CCBG_STATE_DIR": str(state_dir)})
    stdout = io.StringIO()
    code = doctor.run_doctor(config, environ, tmp_path, stdout, client_factory=_healthy_factory)
    output = stdout.getvalue()
    assert code == 0
    assert "warn: state dir" in output
