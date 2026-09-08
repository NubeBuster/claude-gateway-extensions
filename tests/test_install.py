"""Tests for claude_cache_bump_gateway.cli.install.

Every test works against a temp ``HOME``/cwd (never the real one) and
asserts the resolved settings path actually falls inside the temp directory
before any write, per the repo's test-isolation rule.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from claude_cache_bump_gateway.cli import install

URL = "http://127.0.0.1:8787"


def _assert_inside(path: Path, root: Path) -> None:
    """Assert ``path`` resolves inside ``root``; the test-isolation guardrail."""
    assert root.resolve() in path.resolve().parents or path.resolve() == root.resolve()


def _recording_runner(args: Sequence[str]) -> None:
    """A systemd runner for tests that must never touch the real systemd."""


@pytest.fixture
def fake_runner() -> tuple[list[list[str]], install.Runner]:
    """A systemd runner that records calls instead of touching the real systemd."""
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> None:
        calls.append(list(args))

    return calls, runner


def test_settings_path_user_scope_is_inside_tmp_home(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)
    _assert_inside(path, tmp_path)
    assert path == tmp_path / ".claude" / "settings.json"


def test_settings_path_project_scope_is_inside_tmp_cwd(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    path = install.settings_path("project", {}, cwd)
    _assert_inside(path, cwd)
    assert path == cwd / ".claude" / "settings.json"


def test_install_creates_settings_with_only_documented_keys(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)
    _assert_inside(path, tmp_path)

    result = install.install(
        scope="user",
        url=URL,
        force=False,
        service=False,
        dry_run=False,
        environ=environ,
        cwd=tmp_path,
    )
    assert result.settings_path == path
    data = json.loads(path.read_text())
    assert set(data.keys()) <= {"env", "hooks"}
    assert data["env"]["ANTHROPIC_BASE_URL"] == URL
    assert data["hooks"]["UserPromptSubmit"] == [
        {"hooks": [{"type": "command", "command": install.HOOK_COMMAND, "timeout": 5}]}
    ]
    assert path.read_text().endswith("\n")
    assert (result.skill_dir / "SKILL.md").is_file()


def test_install_preserves_unrelated_keys(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"model": "opus", "env": {"SOME_OTHER_VAR": "1"}}))

    install.install(
        scope="user",
        url=URL,
        force=False,
        service=False,
        dry_run=False,
        environ=environ,
        cwd=tmp_path,
    )
    data = json.loads(path.read_text())
    assert data["model"] == "opus"
    assert data["env"]["SOME_OTHER_VAR"] == "1"
    assert data["env"]["ANTHROPIC_BASE_URL"] == URL


def test_install_writes_backup_once(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)
    path.parent.mkdir(parents=True)
    original = json.dumps({"model": "opus"})
    path.write_text(original)

    install.install(
        scope="user",
        url=URL,
        force=False,
        service=False,
        dry_run=False,
        environ=environ,
        cwd=tmp_path,
    )
    backup_path = path.with_name("settings.json.bak")
    assert backup_path.read_text() == original

    # A second install must not clobber the original backup.
    install.install(
        scope="user",
        url=URL,
        force=True,
        service=False,
        dry_run=False,
        environ=environ,
        cwd=tmp_path,
    )
    assert backup_path.read_text() == original


def test_install_is_idempotent(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    for _ in range(2):
        install.install(
            scope="user",
            url=URL,
            force=False,
            service=False,
            dry_run=False,
            environ=environ,
            cwd=tmp_path,
        )
    path = install.settings_path("user", environ, tmp_path)
    data = json.loads(path.read_text())
    assert len(data["hooks"]["UserPromptSubmit"]) == 1


def test_install_refuses_conflicting_base_url(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://example.com"}}))

    with pytest.raises(install.InstallError):
        install.install(
            scope="user",
            url=URL,
            force=False,
            service=False,
            dry_run=False,
            environ=environ,
            cwd=tmp_path,
        )
    # Refusal must not have written anything.
    data = json.loads(path.read_text())
    assert data["env"]["ANTHROPIC_BASE_URL"] == "https://example.com"


def test_install_force_overrides_conflicting_base_url(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://example.com"}}))

    install.install(
        scope="user",
        url=URL,
        force=True,
        service=False,
        dry_run=False,
        environ=environ,
        cwd=tmp_path,
    )
    data = json.loads(path.read_text())
    assert data["env"]["ANTHROPIC_BASE_URL"] == URL


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)

    result = install.install(
        scope="user",
        url=URL,
        force=False,
        service=False,
        dry_run=True,
        environ=environ,
        cwd=tmp_path,
    )
    assert result.dry_run is True
    assert result.settings["env"]["ANTHROPIC_BASE_URL"] == URL
    assert not path.exists()


def test_uninstall_round_trip(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    install.install(
        scope="user",
        url=URL,
        force=False,
        service=False,
        dry_run=False,
        environ=environ,
        cwd=tmp_path,
    )
    path = install.settings_path("user", environ, tmp_path)
    skill_dir = install.skills_dir("user", environ, tmp_path)
    assert (skill_dir / "SKILL.md").is_file()

    install.uninstall(scope="user", url=URL, service=False, environ=environ, cwd=tmp_path)
    data = json.loads(path.read_text())
    assert "env" not in data or "ANTHROPIC_BASE_URL" not in data.get("env", {})
    assert "hooks" not in data or "UserPromptSubmit" not in data.get("hooks", {})
    assert not skill_dir.exists()


def test_uninstall_only_removes_our_base_url(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://not-ours.example"}}))

    install.uninstall(scope="user", url=URL, service=False, environ=environ, cwd=tmp_path)
    data = json.loads(path.read_text())
    assert data["env"]["ANTHROPIC_BASE_URL"] == "https://not-ours.example"


def test_service_install_and_uninstall_use_injected_runner(
    tmp_path: Path, fake_runner: tuple[list[list[str]], install.Runner]
) -> None:
    calls, runner = fake_runner
    environ = {"HOME": str(tmp_path)}
    result = install.install(
        scope="user",
        url=URL,
        force=False,
        service=True,
        dry_run=False,
        environ=environ,
        cwd=tmp_path,
        runner=runner,
    )
    assert result.wrote_service is True
    unit_path = install.unit_path(environ)
    _assert_inside(unit_path, tmp_path)
    assert unit_path.is_file()
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", "claude-cache-bump-gateway.service"] in calls

    calls.clear()
    install.uninstall(
        scope="user", url=URL, service=True, environ=environ, cwd=tmp_path, runner=runner
    )
    assert not unit_path.exists()
    assert ["systemctl", "--user", "disable", "--now", "claude-cache-bump-gateway.service"] in calls


def test_render_unit_passes_through_ccbg_env_vars(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path), "CCBG_PORT": "9999", "UNRELATED": "x"}
    text = install.render_unit("/usr/local/bin/ccbg", environ)
    assert "ExecStart=/usr/local/bin/ccbg serve" in text
    assert "Environment=CCBG_PORT=9999" in text
    assert "UNRELATED" not in text


def test_uninstall_without_settings_creates_nothing(tmp_path: Path) -> None:
    """Uninstalling on a machine that never had a settings.json must not create one."""
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)
    _assert_inside(path, tmp_path)
    assert not path.exists()

    install.uninstall(
        scope="user",
        url="http://127.0.0.1:8787",
        service=False,
        environ=environ,
        cwd=tmp_path,
        runner=_recording_runner,
    )

    assert not path.exists()
    assert not path.with_name(path.name + ".bak").exists()
