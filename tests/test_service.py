"""Tests for claude_gateway_extensions.cli.service.

Every test works against a temp ``HOME`` (never the real one) and asserts
the resolved unit path actually falls inside the temp directory before any
write, per the repo's test-isolation rule. No test ever touches the real
systemd: a fake ``Runner``/``Query`` is injected everywhere.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from claude_gateway_extensions.cli import service


def _assert_inside(path: Path, root: Path) -> None:
    """Assert ``path`` resolves inside ``root``; the test-isolation guardrail."""
    assert root.resolve() in path.resolve().parents or path.resolve() == root.resolve()


@pytest.fixture
def fake_runner() -> tuple[list[list[str]], service.Runner]:
    """A systemd runner that records calls instead of touching the real systemd."""
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> None:
        calls.append(list(args))

    return calls, runner


def test_install_and_uninstall_use_injected_runner(
    tmp_path: Path, fake_runner: tuple[list[list[str]], service.Runner]
) -> None:
    calls, runner = fake_runner
    environ = {"HOME": str(tmp_path)}

    unit_path = service.install_service(environ, runner)
    _assert_inside(unit_path, tmp_path)
    assert unit_path.is_file()
    assert unit_path == service.unit_path(environ)
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", "claude-gateway-extensions.service"] in calls

    calls.clear()
    service.uninstall_service(environ, runner)
    assert not unit_path.exists()
    assert ["systemctl", "--user", "disable", "--now", "claude-gateway-extensions.service"] in calls


def test_uninstall_without_install_is_a_no_op(
    tmp_path: Path, fake_runner: tuple[list[list[str]], service.Runner]
) -> None:
    calls, runner = fake_runner
    environ = {"HOME": str(tmp_path)}

    service.uninstall_service(environ, runner)
    assert calls == []


def test_render_unit_passes_through_cgx_env_vars(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path), "CGX_PORT": "9999", "UNRELATED": "x"}
    text = service.render_unit("/usr/local/bin/cgx", environ)
    assert "ExecStart=/usr/local/bin/cgx serve" in text
    assert "Environment=CGX_PORT=9999" in text
    assert "UNRELATED" not in text


def test_status_reports_absent_unit_without_querying(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    calls: list[list[str]] = []

    def query(args: Sequence[str]) -> str:
        calls.append(list(args))
        return ""

    status = service.service_status(environ, query)
    assert status.present is False
    assert status.enabled is None
    assert status.active is None
    assert calls == []


def test_status_reports_enabled_and_active_unit(
    tmp_path: Path, fake_runner: tuple[list[list[str]], service.Runner]
) -> None:
    _, runner = fake_runner
    environ = {"HOME": str(tmp_path)}
    service.install_service(environ, runner)

    def query(args: Sequence[str]) -> str:
        return "enabled" if "is-enabled" in args else "active"

    status = service.service_status(environ, query)
    assert status.present is True
    assert status.enabled is True
    assert status.active is True


def test_status_reports_disabled_inactive_unit(
    tmp_path: Path, fake_runner: tuple[list[list[str]], service.Runner]
) -> None:
    _, runner = fake_runner
    environ = {"HOME": str(tmp_path)}
    service.install_service(environ, runner)

    def query(args: Sequence[str]) -> str:
        return "disabled" if "is-enabled" in args else "inactive"

    status = service.service_status(environ, query)
    assert status.present is True
    assert status.enabled is False
    assert status.active is False


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_render_unit_explicit_port_overrides_shell_env(tmp_path: Path) -> None:
    """--port must win over whatever CGX_PORT happens to be in the installing shell."""
    environ = {"HOME": str(tmp_path), "CGX_PORT": "1111"}
    text = service.render_unit("/usr/local/bin/cgx", environ, port=9999)
    assert "Environment=CGX_PORT=9999" in text
    assert "Environment=CGX_PORT=1111" not in text


def test_render_unit_explicit_host_overrides_shell_env(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path), "CGX_HOST": "0.0.0.0"}
    text = service.render_unit("/usr/local/bin/cgx", environ, host="127.0.0.1")
    assert "Environment=CGX_HOST=127.0.0.1" in text
    assert "Environment=CGX_HOST=0.0.0.0" not in text


def test_render_unit_includes_timeout_stop_sec_and_kill_mode_by_default(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    text = service.render_unit("/usr/local/bin/cgx", environ)
    assert "TimeoutStopSec=630" in text  # default drain timeout 600 + 30s slack
    assert "KillMode=mixed" in text


def test_render_unit_timeout_stop_sec_follows_drain_timeout_env(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path), "CGX_DRAIN_TIMEOUT_S": "60"}
    text = service.render_unit("/usr/local/bin/cgx", environ)
    assert "TimeoutStopSec=90" in text
    assert "Environment=CGX_DRAIN_TIMEOUT_S=60" in text


def test_render_unit_still_passes_through_other_cgx_vars_with_explicit_port(
    tmp_path: Path,
) -> None:
    """Explicit --port must not disable the documented pass-through of other CGX_* vars."""
    environ = {"HOME": str(tmp_path), "CGX_LOG_LEVEL": "DEBUG"}
    text = service.render_unit("/usr/local/bin/cgx", environ, port=9999)
    assert "Environment=CGX_PORT=9999" in text
    assert "Environment=CGX_LOG_LEVEL=DEBUG" in text


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_install_service_forwards_port_and_host(
    tmp_path: Path, fake_runner: tuple[list[list[str]], service.Runner]
) -> None:
    _, runner = fake_runner
    environ = {"HOME": str(tmp_path)}
    unit_path = service.install_service(environ, runner, port=9999, host="0.0.0.0")
    text = unit_path.read_text()
    assert "Environment=CGX_PORT=9999" in text
    assert "Environment=CGX_HOST=0.0.0.0" in text


def test_installed_port_absent_without_a_unit(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    assert service.installed_port(environ) is None


def test_installed_port_absent_when_unit_has_no_explicit_port(
    tmp_path: Path, fake_runner: tuple[list[list[str]], service.Runner]
) -> None:
    _, runner = fake_runner
    environ = {"HOME": str(tmp_path)}
    service.install_service(environ, runner)
    assert service.installed_port(environ) is None


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_installed_port_reads_back_the_written_port(
    tmp_path: Path, fake_runner: tuple[list[list[str]], service.Runner]
) -> None:
    _, runner = fake_runner
    environ = {"HOME": str(tmp_path)}
    service.install_service(environ, runner, port=9999)
    assert service.installed_port(environ) == 9999


def test_plan_install_service_on_a_fresh_machine_shows_a_create(
    tmp_path: Path, fake_runner: tuple[list[list[str]], service.Runner]
) -> None:
    _, runner = fake_runner
    environ = {"HOME": str(tmp_path)}
    change = service.plan_install_service(environ, runner)
    assert change.old_content is None
    assert change.path == service.unit_path(environ)
    assert "ExecStart=" in (change.new_content or "")


def test_plan_install_service_after_a_real_install_shows_no_changes(
    tmp_path: Path, fake_runner: tuple[list[list[str]], service.Runner]
) -> None:
    _, runner = fake_runner
    environ = {"HOME": str(tmp_path)}
    service.install_service(environ, runner, port=9999)
    change = service.plan_install_service(environ, runner, port=9999)
    assert change.old_content == change.new_content


def test_plan_install_service_apply_writes_and_enables(
    tmp_path: Path, fake_runner: tuple[list[list[str]], service.Runner]
) -> None:
    calls, runner = fake_runner
    environ = {"HOME": str(tmp_path)}
    change = service.plan_install_service(environ, runner)
    assert calls == []  # planning must not touch systemd
    change.apply()
    assert change.path.is_file()
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", "claude-gateway-extensions.service"] in calls


def test_plan_uninstall_service_omits_change_when_nothing_installed(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    assert service.plan_uninstall_service(environ) == []


def test_plan_uninstall_service_after_install_shows_a_removal(
    tmp_path: Path, fake_runner: tuple[list[list[str]], service.Runner]
) -> None:
    _, runner = fake_runner
    environ = {"HOME": str(tmp_path)}
    service.install_service(environ, runner)
    (change,) = service.plan_uninstall_service(environ, runner)
    assert change.old_content
    assert change.new_content is None


def test_restart_service_uses_injected_runner(
    tmp_path: Path, fake_runner: tuple[list[list[str]], service.Runner]
) -> None:
    calls, runner = fake_runner
    environ = {"HOME": str(tmp_path)}

    service.restart_service(environ, runner)

    assert calls == [["systemctl", "--user", "restart", "claude-gateway-extensions.service"]]


def test_plan_uninstall_service_apply_removes_and_disables(
    tmp_path: Path, fake_runner: tuple[list[list[str]], service.Runner]
) -> None:
    calls, runner = fake_runner
    environ = {"HOME": str(tmp_path)}
    service.install_service(environ, runner)
    calls.clear()
    (change,) = service.plan_uninstall_service(environ, runner)
    change.apply()
    assert not change.path.exists()
    assert ["systemctl", "--user", "disable", "--now", "claude-gateway-extensions.service"] in calls
