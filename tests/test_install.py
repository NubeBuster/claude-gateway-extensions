"""Tests for claude_gateway_extensions.cli.install.

Every test works against a temp ``HOME``/cwd (never the real one) and
asserts the resolved settings path actually falls inside the temp directory
before any write, per the repo's test-isolation rule.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_gateway_extensions.cli import install

URL = "http://127.0.0.1:8787"


def _assert_inside(path: Path, root: Path) -> None:
    """Assert ``path`` resolves inside ``root``; the test-isolation guardrail."""
    assert root.resolve() in path.resolve().parents or path.resolve() == root.resolve()


def test_settings_path_user_scope_is_inside_tmp_home(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)
    _assert_inside(path, tmp_path)
    assert path == tmp_path / ".claude" / "settings.json"


def test_user_home_falls_back_to_userprofile_without_home(tmp_path: Path) -> None:
    """Windows has no ``HOME``; ``user_home`` must still resolve from ``USERPROFILE``."""
    environ = {"USERPROFILE": str(tmp_path)}
    assert install.user_home(environ) == tmp_path


def test_user_home_prefers_home_over_userprofile(tmp_path: Path) -> None:
    home = tmp_path / "home"
    userprofile = tmp_path / "userprofile"
    environ = {"HOME": str(home), "USERPROFILE": str(userprofile)}
    assert install.user_home(environ) == home


def test_settings_path_project_scope_is_inside_tmp_cwd(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    path = install.settings_path("project", {}, cwd)
    _assert_inside(path, cwd)
    assert path == cwd / ".claude" / "settings.json"


def test_settings_path_local_scope_is_inside_tmp_cwd(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    path = install.settings_path("local", {}, cwd)
    _assert_inside(path, cwd)
    assert path == cwd / ".claude" / "settings.local.json"


def test_skills_dir_local_scope_matches_project_scope(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    assert install.skills_dir("local", {}, cwd) == install.skills_dir("project", {}, cwd)


@pytest.mark.invariant(decision="Environment variables only, split by process")
def test_install_creates_settings_with_only_documented_keys(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)
    _assert_inside(path, tmp_path)

    result = install.install(
        scope="user",
        url=URL,
        force=False,
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


@pytest.mark.invariant(decision="Environment variables only, split by process")
def test_install_preserves_unrelated_keys(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"model": "opus", "env": {"SOME_OTHER_VAR": "1"}}))

    install.install(
        scope="user",
        url=URL,
        force=False,
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
            dry_run=False,
            environ=environ,
            cwd=tmp_path,
        )
    path = install.settings_path("user", environ, tmp_path)
    data = json.loads(path.read_text())
    assert len(data["hooks"]["UserPromptSubmit"]) == 1


@pytest.mark.invariant(
    decision="Every file a user owns is diffed and confirmed before it is written"
)
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
        dry_run=False,
        environ=environ,
        cwd=tmp_path,
    )
    path = install.settings_path("user", environ, tmp_path)
    skill_dir = install.skills_dir("user", environ, tmp_path)
    assert (skill_dir / "SKILL.md").is_file()

    install.uninstall(scope="user", url=URL, environ=environ, cwd=tmp_path)
    data = json.loads(path.read_text())
    assert "env" not in data or "ANTHROPIC_BASE_URL" not in data.get("env", {})
    assert "hooks" not in data or "UserPromptSubmit" not in data.get("hooks", {})
    assert not skill_dir.exists()


@pytest.mark.invariant(
    decision="Every file a user owns is diffed and confirmed before it is written"
)
def test_uninstall_only_removes_our_base_url(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://not-ours.example"}}))

    install.uninstall(scope="user", url=URL, environ=environ, cwd=tmp_path)
    data = json.loads(path.read_text())
    assert data["env"]["ANTHROPIC_BASE_URL"] == "https://not-ours.example"


@pytest.mark.invariant(
    decision="Every file a user owns is diffed and confirmed before it is written"
)
def test_uninstall_without_settings_creates_nothing(tmp_path: Path) -> None:
    """Uninstalling on a machine that never had a settings.json must not create one."""
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)
    _assert_inside(path, tmp_path)
    assert not path.exists()

    install.uninstall(
        scope="user",
        url="http://127.0.0.1:8787",
        environ=environ,
        cwd=tmp_path,
    )

    assert not path.exists()
    assert not path.with_name(path.name + ".bak").exists()


def test_skill_source_dir_prefers_installed_wheel_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the wheel's force-include mapping populated the package, use it."""
    packaged = tmp_path / "packaged" / "data" / "skills" / "cache"
    packaged.mkdir(parents=True)
    (packaged / "SKILL.md").write_text("packaged\n")

    monkeypatch.setattr(install.importlib.resources, "files", lambda _pkg: tmp_path / "packaged")
    assert install.skill_source_dir() == packaged


def test_skill_source_dir_falls_back_to_repo_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An editable install never runs force-include; fall back to the repo path."""
    monkeypatch.setattr(
        install.importlib.resources, "files", lambda _pkg: Path("/nonexistent-package-dir")
    )
    source = install.skill_source_dir()
    assert (source / "SKILL.md").is_file()
    assert source.parts[-3:] == ("claude", "skills", "cache")


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_resolve_install_port_prefers_explicit_flag() -> None:
    assert install.resolve_install_port(9999, {"CGX_PORT": "1111"}, 2222) == 9999


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_resolve_install_port_falls_back_to_cgx_port_env() -> None:
    assert install.resolve_install_port(None, {"CGX_PORT": "1111"}, 2222) == 1111


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_resolve_install_port_falls_back_to_cgx_url_env() -> None:
    assert install.resolve_install_port(None, {"CGX_URL": "http://127.0.0.1:1111"}, 2222) == 1111


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_resolve_install_port_falls_back_to_installed_unit_port() -> None:
    assert install.resolve_install_port(None, {}, 2222) == 2222


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_resolve_install_port_falls_back_to_default() -> None:
    assert install.resolve_install_port(None, {}, None) == install.DEFAULT_PORT


def test_resolve_install_url_builds_loopback_url() -> None:
    assert install.resolve_install_url(9999, {}, None) == "http://127.0.0.1:9999"


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_install_writes_cgx_url_for_non_default_port(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    url = "http://127.0.0.1:9999"
    install.install(
        scope="user", url=url, force=False, dry_run=False, environ=environ, cwd=tmp_path
    )
    path = install.settings_path("user", environ, tmp_path)
    data = json.loads(path.read_text())
    assert data["env"]["CGX_URL"] == url


@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_install_does_not_write_cgx_url_for_default_port(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    install.install(
        scope="user", url=URL, force=False, dry_run=False, environ=environ, cwd=tmp_path
    )
    path = install.settings_path("user", environ, tmp_path)
    data = json.loads(path.read_text())
    assert "CGX_URL" not in data.get("env", {})


def test_uninstall_removes_cgx_url(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    url = "http://127.0.0.1:9999"
    install.install(
        scope="user", url=url, force=False, dry_run=False, environ=environ, cwd=tmp_path
    )
    path = install.settings_path("user", environ, tmp_path)
    assert json.loads(path.read_text())["env"]["CGX_URL"] == url

    install.uninstall(scope="user", url=url, environ=environ, cwd=tmp_path)
    data = json.loads(path.read_text())
    assert "CGX_URL" not in data.get("env", {})


def test_skill_source_dir_raises_when_neither_location_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        install.importlib.resources, "files", lambda _pkg: Path("/nonexistent-package-dir")
    )
    fake_module_file = (
        tmp_path / "fake-repo" / "src" / "claude_gateway_extensions" / "cli" / "install.py"
    )
    fake_module_file.parent.mkdir(parents=True)
    monkeypatch.setattr(install, "__file__", str(fake_module_file))

    with pytest.raises(install.InstallError):
        install.skill_source_dir()


def test_plan_install_on_a_fresh_scope_shows_two_creates(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    changes = install.plan_install(
        scope="user", url=URL, force=False, environ=environ, cwd=tmp_path
    )
    assert [c.old_content for c in changes] == [None, None]
    settings_change, skill_change = changes
    assert URL in (settings_change.new_content or "")
    assert settings_change.path == install.settings_path("user", environ, tmp_path)
    assert skill_change.path == install.skills_dir("user", environ, tmp_path) / "SKILL.md"
    assert skill_change.new_content


def test_plan_install_after_a_real_install_shows_no_changes(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    install.install(
        scope="user", url=URL, force=False, dry_run=False, environ=environ, cwd=tmp_path
    )
    changes = install.plan_install(
        scope="user", url=URL, force=False, environ=environ, cwd=tmp_path
    )
    assert all(c.old_content == c.new_content for c in changes)


def test_plan_install_apply_writes_settings_and_skill(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    changes = install.plan_install(
        scope="user", url=URL, force=False, environ=environ, cwd=tmp_path
    )
    for change in changes:
        change.apply()
    settings_path = install.settings_path("user", environ, tmp_path)
    assert json.loads(settings_path.read_text())["env"]["ANTHROPIC_BASE_URL"] == URL
    skill_dir = install.skills_dir("user", environ, tmp_path)
    assert (skill_dir / "SKILL.md").is_file()


def test_plan_uninstall_omits_files_that_do_not_exist(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    changes = install.plan_uninstall(scope="user", url=URL, environ=environ, cwd=tmp_path)
    assert changes == []


def test_plan_uninstall_after_install_reports_removal(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    install.install(
        scope="user", url=URL, force=False, dry_run=False, environ=environ, cwd=tmp_path
    )
    changes = install.plan_uninstall(scope="user", url=URL, environ=environ, cwd=tmp_path)
    settings_change, skill_change = changes
    assert settings_change.old_content is not None
    assert URL not in (settings_change.new_content or "")
    assert skill_change.new_content is None
    assert skill_change.old_content


def test_plan_uninstall_apply_removes_settings_entries_and_skill(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    install.install(
        scope="user", url=URL, force=False, dry_run=False, environ=environ, cwd=tmp_path
    )
    changes = install.plan_uninstall(scope="user", url=URL, environ=environ, cwd=tmp_path)
    for change in changes:
        change.apply()
    settings_path = install.settings_path("user", environ, tmp_path)
    data = json.loads(settings_path.read_text())
    assert "env" not in data or "ANTHROPIC_BASE_URL" not in data.get("env", {})
    skill_dir = install.skills_dir("user", environ, tmp_path)
    assert not skill_dir.exists()


@pytest.mark.invariant(decision="No first-party base URL flag by default")
def test_configure_writes_no_env_key_beyond_the_documented_two(tmp_path: Path) -> None:
    """The ``env`` block gets exactly ``ANTHROPIC_BASE_URL`` and ``CGX_URL``, nothing else.

    ``_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL`` in particular is an
    undocumented opt-in whose only known effect is 1M-context verification
    behind a custom URL; it is never written on the user's behalf. Any
    further key belongs in docs/DESIGN.md and the README tables first.
    """
    environ = {"HOME": str(tmp_path)}
    path = install.settings_path("user", environ, tmp_path)
    _assert_inside(path, tmp_path)

    install.install(
        scope="user",
        url="http://127.0.0.1:9999",
        force=False,
        dry_run=False,
        environ=environ,
        cwd=tmp_path,
    )

    env = json.loads(path.read_text())["env"]
    assert set(env) == {"ANTHROPIC_BASE_URL", "CGX_URL"}
