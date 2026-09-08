"""Tests for claude_cache_bump_gateway.cli.service.

Every test works against a temp ``HOME`` (never the real one) and asserts
the resolved unit path actually falls inside the temp directory before any
write, per the repo's test-isolation rule. No test ever touches the real
systemd: a fake ``Runner``/``Query`` is injected everywhere.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from claude_cache_bump_gateway.cli import service


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
    assert ["systemctl", "--user", "enable", "--now", "claude-cache-bump-gateway.service"] in calls

    calls.clear()
    service.uninstall_service(environ, runner)
    assert not unit_path.exists()
    assert ["systemctl", "--user", "disable", "--now", "claude-cache-bump-gateway.service"] in calls


def test_uninstall_without_install_is_a_no_op(
    tmp_path: Path, fake_runner: tuple[list[list[str]], service.Runner]
) -> None:
    calls, runner = fake_runner
    environ = {"HOME": str(tmp_path)}

    service.uninstall_service(environ, runner)
    assert calls == []


def test_render_unit_passes_through_ccbg_env_vars(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path), "CCBG_PORT": "9999", "UNRELATED": "x"}
    text = service.render_unit("/usr/local/bin/ccbg", environ)
    assert "ExecStart=/usr/local/bin/ccbg serve" in text
    assert "Environment=CCBG_PORT=9999" in text
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
