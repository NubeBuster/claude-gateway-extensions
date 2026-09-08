"""Idempotent installer/uninstaller for the Claude Code integration surface.

Merges ``env.ANTHROPIC_BASE_URL`` and the ``UserPromptSubmit`` hook into the
chosen ``settings.json`` and copies the ``cache`` skill. See the "Skill as
pure fall-through" and "Environment variables only, split by process" (why
no config file) decisions in ``docs/DESIGN.md`` "Decisions" for the
rationale; the only ``settings.json`` keys ever written are ``env`` (one
entry, ``ANTHROPIC_BASE_URL``) and ``hooks.UserPromptSubmit`` (one entry).
``statusLine`` is deliberately not touched — ``ccbg statusline`` is something
the user appends to their own command, per the README.

The daemon (systemd user service) is a separate, per-machine concern with
its own module, :mod:`claude_cache_bump_gateway.cli.service`: ``--scope``
here only ever selects which ``settings.json`` is edited, never anything
about the daemon.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

Scope = Literal["user", "project"]

#: Command registered for the ``UserPromptSubmit`` hook; also the marker used
#: to find and remove exactly the entry this installer added.
HOOK_COMMAND = "ccbg hook user-prompt-submit"
_HOOK_TIMEOUT_S = 5
_SKILL_NAME = "cache"

#: The daemon's default port and the resulting default URL (matches
#: ``config.py``'s ``CCBG_HOST``/``CCBG_PORT`` defaults) — the port below
#: which ``ccbg install`` does not bother also writing ``env.CCBG_URL``.
DEFAULT_PORT = 8787
DEFAULT_URL = f"http://127.0.0.1:{DEFAULT_PORT}"


class InstallError(Exception):
    """Raised when install/uninstall cannot proceed safely (e.g. a real conflict)."""


def user_home(environ: Mapping[str, str]) -> Path:
    """Return the home directory to use, honouring ``HOME`` from ``environ``."""
    home = environ.get("HOME")
    return Path(home) if home else Path.home()


def settings_path(scope: Scope, environ: Mapping[str, str], cwd: Path) -> Path:
    """Return the ``settings.json`` path for ``scope`` (``user`` or ``project``)."""
    if scope == "user":
        return user_home(environ) / ".claude" / "settings.json"
    return cwd / ".claude" / "settings.json"


def skills_dir(scope: Scope, environ: Mapping[str, str], cwd: Path) -> Path:
    """Return the destination directory for the ``cache`` skill for ``scope``."""
    if scope == "user":
        return user_home(environ) / ".claude" / "skills" / _SKILL_NAME
    return cwd / ".claude" / "skills" / _SKILL_NAME


def load_settings(path: Path) -> dict[str, Any]:
    """Load ``path`` as a JSON object, returning ``{}`` if it does not exist.

    Raises :class:`InstallError` if the file exists but is not a JSON object.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise InstallError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InstallError(f"{path}: top-level JSON value is not an object")
    return data


def backup_settings(path: Path) -> None:
    """Copy ``path`` to ``path.bak`` if it exists and no backup has been made yet.

    The backup is written once, before this installer's first modification,
    so it always reflects the user's pre-install file rather than being
    overwritten by a later install/uninstall run.
    """
    if not path.is_file():
        return
    backup_path = path.with_name(path.name + ".bak")
    if backup_path.exists():
        return
    shutil.copyfile(path, backup_path)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` as indented JSON with a trailing newline, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2) + "\n"
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(text)
    os.replace(tmp_path, path)


def merge_base_url(settings: dict[str, Any], url: str, force: bool) -> None:
    """Set ``env.ANTHROPIC_BASE_URL`` to ``url`` in-place.

    Raises :class:`InstallError` if a different value is already present and
    ``force`` is ``False``.
    """
    env = settings.setdefault("env", {})
    existing = env.get("ANTHROPIC_BASE_URL")
    if existing is not None and existing != url and not force:
        raise InstallError(
            f"env.ANTHROPIC_BASE_URL is already set to {existing!r} (expected {url!r}); "
            "pass --force to override"
        )
    env["ANTHROPIC_BASE_URL"] = url


def remove_base_url(settings: dict[str, Any], url: str) -> None:
    """Remove ``env.ANTHROPIC_BASE_URL`` in-place, but only if it equals ``url``."""
    env = settings.get("env")
    if isinstance(env, dict) and env.get("ANTHROPIC_BASE_URL") == url:
        del env["ANTHROPIC_BASE_URL"]
        if not env:
            del settings["env"]


def merge_ccbg_url(settings: dict[str, Any], url: str) -> None:
    """Set ``env.CCBG_URL`` to ``url`` in-place.

    Only called for a non-default ``url`` (see :func:`install`): the common
    default-port case relies on the hook/statusline/CLI's ``ANTHROPIC_BASE_URL``
    fallback (``config.py``) instead of a redundant settings entry.
    """
    env = settings.setdefault("env", {})
    env["CCBG_URL"] = url


def remove_ccbg_url(settings: dict[str, Any], url: str) -> None:
    """Remove ``env.CCBG_URL`` if it equals ``url`` in-place (mirrors ``remove_base_url``)."""
    env = settings.get("env")
    if isinstance(env, dict) and env.get("CCBG_URL") == url:
        del env["CCBG_URL"]
        if not env:
            del settings["env"]


def resolve_install_port(
    port: int | None,
    environ: Mapping[str, str],
    installed_port: int | None,
) -> int:
    """Resolve the daemon port ``ccbg install`` writes into ``ANTHROPIC_BASE_URL``.

    Precedence: the ``--port`` flag, then ``CCBG_PORT`` or (failing that)
    the port embedded in ``CCBG_URL`` if either is set in ``environ``, then
    ``installed_port`` (the port baked into the systemd unit, if
    ``ccbg service install`` already ran — see
    :func:`~claude_cache_bump_gateway.cli.service.installed_port`), then
    :data:`DEFAULT_PORT`.
    """
    if port is not None:
        return port
    env_port = environ.get("CCBG_PORT")
    if env_port:
        return int(env_port)
    env_url = environ.get("CCBG_URL")
    if env_url:
        parsed_port = urlsplit(env_url).port
        if parsed_port is not None:
            return parsed_port
    if installed_port is not None:
        return installed_port
    return DEFAULT_PORT


def resolve_install_url(
    port: int | None,
    environ: Mapping[str, str],
    installed_port: int | None,
) -> str:
    """Return the loopback URL ``ccbg install`` writes; see :func:`resolve_install_port`."""
    return f"http://127.0.0.1:{resolve_install_port(port, environ, installed_port)}"


def hook_entry_has_command(entry: Mapping[str, Any], command: str) -> bool:
    """Return whether a ``hooks.UserPromptSubmit`` entry already runs ``command``."""
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(isinstance(h, dict) and h.get("command") == command for h in hooks)


def merge_hook(settings: dict[str, Any], command: str = HOOK_COMMAND) -> None:
    """Append a ``UserPromptSubmit`` hook entry running ``command``, if not already present."""
    hooks_section = settings.setdefault("hooks", {})
    entries = hooks_section.setdefault("UserPromptSubmit", [])
    if not isinstance(entries, list):
        raise InstallError("hooks.UserPromptSubmit is not a list; refusing to modify it")
    if any(hook_entry_has_command(e, command) for e in entries if isinstance(e, dict)):
        return
    entries.append({"hooks": [{"type": "command", "command": command, "timeout": _HOOK_TIMEOUT_S}]})


def remove_hook(settings: dict[str, Any], command: str = HOOK_COMMAND) -> None:
    """Remove exactly the ``UserPromptSubmit`` hook entry running ``command``, if present."""
    hooks_section = settings.get("hooks")
    if not isinstance(hooks_section, dict):
        return
    entries = hooks_section.get("UserPromptSubmit")
    if not isinstance(entries, list):
        return
    remaining = [
        e for e in entries if not (isinstance(e, dict) and hook_entry_has_command(e, command))
    ]
    if remaining:
        hooks_section["UserPromptSubmit"] = remaining
    else:
        hooks_section.pop("UserPromptSubmit", None)
    if not hooks_section:
        settings.pop("hooks", None)


def skill_source_dir() -> Any:
    """Return the packaged ``cache`` skill directory.

    The canonical source is the repo-root ``claude/skills/cache/``; the
    wheel's ``force-include`` mapping (see ``pyproject.toml``) copies it to
    ``claude_cache_bump_gateway/data/skills/cache/`` at build time, so an
    installed wheel resolves it via ``importlib.resources``. An editable
    install (``pip install -e .``) never runs that mapping, so this falls
    back to the repo-root path computed from this file's location.

    Raises :class:`InstallError` if neither location exists.
    """
    packaged = (
        importlib.resources.files("claude_cache_bump_gateway") / "data" / "skills" / _SKILL_NAME
    )
    if packaged.is_dir():
        return packaged
    repo_root = Path(__file__).resolve().parents[3]
    fallback = repo_root / "claude" / "skills" / _SKILL_NAME
    if fallback.is_dir():
        return fallback
    raise InstallError(
        f"cannot find the packaged '{_SKILL_NAME}' skill: checked {packaged} "
        f"(installed wheel) and {fallback} (editable checkout)"
    )


def copy_skill(dest_dir: Path) -> None:
    """Copy the packaged skill's ``SKILL.md`` into ``dest_dir``, creating it if needed."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    source = skill_source_dir() / "SKILL.md"
    with importlib.resources.as_file(source) as source_path:
        shutil.copyfile(source_path, dest_dir / "SKILL.md")


def remove_skill(dest_dir: Path) -> None:
    """Remove ``dest_dir`` (the skill directory this installer created) if present."""
    if dest_dir.is_dir():
        shutil.rmtree(dest_dir)


@dataclass
class InstallResult:
    """Summary of what :func:`install` did (or would do, for ``--dry-run``)."""

    settings_path: Path
    skill_dir: Path
    settings: dict[str, Any]
    dry_run: bool


def install(
    *,
    scope: Scope,
    url: str,
    force: bool,
    dry_run: bool,
    environ: Mapping[str, str],
    cwd: Path,
) -> InstallResult:
    """Idempotently install the Claude Code integration for ``scope``.

    Raises :class:`InstallError` if ``env.ANTHROPIC_BASE_URL`` already points
    elsewhere and ``force`` is ``False``. With ``dry_run=True``, computes and
    returns the resulting settings without writing anything.
    """
    path = settings_path(scope, environ, cwd)
    skill_dir = skills_dir(scope, environ, cwd)
    settings = load_settings(path)
    merge_base_url(settings, url, force)
    if url != DEFAULT_URL:
        merge_ccbg_url(settings, url)
    merge_hook(settings)
    if dry_run:
        return InstallResult(path, skill_dir, settings, True)
    backup_settings(path)
    _atomic_write_json(path, settings)
    copy_skill(skill_dir)
    return InstallResult(path, skill_dir, settings, False)


def uninstall(
    *,
    scope: Scope,
    url: str,
    environ: Mapping[str, str],
    cwd: Path,
) -> InstallResult:
    """Reverse exactly what :func:`install` added for ``scope``.

    A ``settings.json`` that does not exist is left absent rather than
    created empty: uninstalling must never leave behind a file the user
    never had.
    """
    path = settings_path(scope, environ, cwd)
    skill_dir = skills_dir(scope, environ, cwd)
    settings = load_settings(path)
    remove_base_url(settings, url)
    remove_ccbg_url(settings, url)
    remove_hook(settings)
    if path.is_file():
        backup_settings(path)
        _atomic_write_json(path, settings)
    remove_skill(skill_dir)
    return InstallResult(path, skill_dir, settings, False)


__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_URL",
    "HOOK_COMMAND",
    "InstallError",
    "InstallResult",
    "Scope",
    "hook_entry_has_command",
    "install",
    "resolve_install_port",
    "resolve_install_url",
    "settings_path",
    "skills_dir",
    "uninstall",
]
