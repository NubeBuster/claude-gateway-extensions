"""Idempotent installer/uninstaller for the Claude Code integration surface.

Merges ``env.ANTHROPIC_BASE_URL`` and the ``UserPromptSubmit`` hook into the
chosen ``settings.json`` and copies the ``cache`` skill. See the "Skill as
pure fall-through" and "Environment variables only, split by process" (why
no config file) decisions in ``docs/DESIGN.md`` "Decisions" for the
rationale; without ``--fork-review``, the only ``settings.json`` keys ever
written are ``env`` (``ANTHROPIC_BASE_URL``, plus ``CGX_URL`` on a
non-default port) and ``hooks.UserPromptSubmit`` (one entry). Each
``--fork-review NAME[:PREFIX]`` adds one more ``hooks.<event>`` entry,
``<event>`` and an optional ``matcher`` read from the named template's own
front matter (see ``cli/fork_review.py``), plus a one-time copy of that
template into the user's template directory if not already there.
``statusLine`` is deliberately not touched — ``cgx statusline`` is something
the user appends to their own command, per the README.

The daemon (systemd user service) is a separate, per-machine concern with
its own module, :mod:`claude_gateway_extensions.cli.service`: ``--scope``
here only ever selects which ``settings.json`` is edited, never anything
about the daemon.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import shlex
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from claude_gateway_extensions.cli import fork_review
from claude_gateway_extensions.cli.confirm import PlannedChange
from claude_gateway_extensions.config import resolve_home

#: ``managed`` is deliberately absent: it is root-owned and machine-wide,
#: which would apply the same ``ANTHROPIC_BASE_URL``/hook to every user on
#: the machine and conflicts with per-user daemons (see ``--scope`` help).
Scope = Literal["user", "project", "local"]

#: Command registered for the ``UserPromptSubmit`` hook; also the marker used
#: to find and remove exactly the entry this installer added.
HOOK_COMMAND = "cgx hook user-prompt-submit"
_HOOK_TIMEOUT_S = 5
_SKILL_NAME = "cache"

#: The daemon's default port and the resulting default URL (matches
#: ``config.py``'s ``CGX_HOST``/``CGX_PORT`` defaults) — the port below
#: which ``cgx configure`` does not bother also writing ``env.CGX_URL``.
DEFAULT_PORT = 8787
DEFAULT_URL = f"http://127.0.0.1:{DEFAULT_PORT}"


class InstallError(Exception):
    """Raised when install/uninstall cannot proceed safely (e.g. a real conflict)."""


def user_home(environ: Mapping[str, str]) -> Path:
    """Return the home directory to use.

    Delegates to :func:`claude_gateway_extensions.config.resolve_home`,
    which honours ``HOME`` then ``USERPROFILE`` (Windows) from ``environ``.
    """
    return resolve_home(environ)


def settings_path(scope: Scope, environ: Mapping[str, str], cwd: Path) -> Path:
    """Return the settings path for ``scope`` (``user``, ``project`` or ``local``).

    ``local`` is Claude Code's gitignored per-developer overlay on top of a
    project: same directory as ``project``, different filename
    (``settings.local.json``).
    """
    if scope == "user":
        return user_home(environ) / ".claude" / "settings.json"
    if scope == "local":
        return cwd / ".claude" / "settings.local.json"
    return cwd / ".claude" / "settings.json"


def skills_dir(scope: Scope, environ: Mapping[str, str], cwd: Path) -> Path:
    """Return the destination directory for the ``cache`` skill for ``scope``.

    ``local`` shares ``project``'s skill directory: Claude Code does not
    scope skills per local/project, only ``settings.json`` differs.
    """
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


def write_settings(path: Path, settings: dict[str, Any]) -> None:
    """Back up ``path`` if present, then atomically write ``settings`` as JSON.

    The single primitive :func:`plan_install`/:func:`plan_uninstall`'s
    planned changes call to actually apply a settings.json edit, kept
    separate from computing the new content so the CLI's confirm flow can
    show the diff first and only call this on confirmation.
    """
    backup_settings(path)
    _atomic_write_json(path, settings)


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


def merge_cgx_url(settings: dict[str, Any], url: str) -> None:
    """Set ``env.CGX_URL`` to ``url`` in-place.

    Only called for a non-default ``url`` (see :func:`install`): the common
    default-port case relies on the hook/statusline/CLI's ``ANTHROPIC_BASE_URL``
    fallback (``config.py``) instead of a redundant settings entry.
    """
    env = settings.setdefault("env", {})
    env["CGX_URL"] = url


def remove_cgx_url(settings: dict[str, Any], url: str) -> None:
    """Remove ``env.CGX_URL`` if it equals ``url`` in-place (mirrors ``remove_base_url``)."""
    env = settings.get("env")
    if isinstance(env, dict) and env.get("CGX_URL") == url:
        del env["CGX_URL"]
        if not env:
            del settings["env"]


def resolve_install_port(
    port: int | None,
    environ: Mapping[str, str],
    installed_port: int | None,
) -> int:
    """Resolve the daemon port ``cgx configure`` writes into ``ANTHROPIC_BASE_URL``.

    Precedence: the ``--port`` flag, then ``CGX_PORT`` or (failing that)
    the port embedded in ``CGX_URL`` if either is set in ``environ``, then
    ``installed_port`` (the port baked into the systemd unit, if
    ``cgx service install`` already ran — see
    :func:`~claude_gateway_extensions.cli.service.installed_port`), then
    :data:`DEFAULT_PORT`.
    """
    if port is not None:
        return port
    env_port = environ.get("CGX_PORT")
    if env_port:
        return int(env_port)
    env_url = environ.get("CGX_URL")
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
    """Return the loopback URL ``cgx configure`` writes; see :func:`resolve_install_port`."""
    return f"http://127.0.0.1:{resolve_install_port(port, environ, installed_port)}"


def hook_entry_has_command(entry: Mapping[str, Any], command: str) -> bool:
    """Return whether a hook entry already runs ``command``."""
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(isinstance(h, dict) and h.get("command") == command for h in hooks)


def merge_hook(
    settings: dict[str, Any],
    event: str,
    command: str = HOOK_COMMAND,
    *,
    matcher: str | None = None,
) -> None:
    """Append a ``hooks.<event>`` entry running ``command``, if not already present.

    ``matcher`` (set on the entry when given) is Claude Code's tool-name
    filter, relevant at ``PreToolUse``/``PostToolUse``; the
    ``UserPromptSubmit`` hook this installer has always written passes none.
    """
    hooks_section = settings.setdefault("hooks", {})
    entries = hooks_section.setdefault(event, [])
    if not isinstance(entries, list):
        raise InstallError(f"hooks.{event} is not a list; refusing to modify it")
    if any(hook_entry_has_command(e, command) for e in entries if isinstance(e, dict)):
        return
    entry: dict[str, Any] = {
        "hooks": [{"type": "command", "command": command, "timeout": _HOOK_TIMEOUT_S}]
    }
    if matcher:
        entry["matcher"] = matcher
    entries.append(entry)


def remove_hook(settings: dict[str, Any], event: str, command: str = HOOK_COMMAND) -> None:
    """Remove exactly the ``hooks.<event>`` entry running ``command``, if present."""
    hooks_section = settings.get("hooks")
    if not isinstance(hooks_section, dict):
        return
    entries = hooks_section.get(event)
    if not isinstance(entries, list):
        return
    remaining = [
        e for e in entries if not (isinstance(e, dict) and hook_entry_has_command(e, command))
    ]
    if remaining:
        hooks_section[event] = remaining
    else:
        hooks_section.pop(event, None)
    if not hooks_section:
        settings.pop("hooks", None)


@dataclass(frozen=True)
class ForkReviewSpec:
    """One parsed ``--fork-review NAME[:PREFIX]`` value."""

    name: str
    prefix: str | None


def parse_fork_review_spec(value: str) -> ForkReviewSpec:
    """Parse a ``--fork-review NAME[:PREFIX]`` argument value.

    Splits on the first ``:`` only, so a ``PREFIX`` may itself contain a
    colon (e.g. ``rename:/rename:``, unlikely but not worth forbidding).
    """
    name, sep, prefix = value.partition(":")
    return ForkReviewSpec(name=name, prefix=prefix if sep else None)


def fork_review_command(spec: ForkReviewSpec) -> str:
    """Return the exact ``cgx fork-review`` command line for ``spec``.

    This string is the removal marker :func:`hook_entry_has_command` and
    :func:`remove_hook` match on, so ``configure`` and ``unconfigure`` must
    both reconstruct it identically from the same ``--fork-review`` value --
    which this one function does for both.
    """
    command = f"cgx fork-review --template {spec.name}"
    if spec.prefix:
        command += f" --prefix {shlex.quote(spec.prefix)}"
    return command


def resolve_fork_review_hook(
    spec: ForkReviewSpec, environ: Mapping[str, str]
) -> tuple[str, str | None]:
    """Return the ``(event, matcher)`` declared by ``spec.name``'s template.

    Raises :class:`InstallError` naming the template if it cannot be
    found/read, or if it declares no ``event:`` front-matter line --
    ``cgx configure`` needs to know which hook to attach to and never
    guesses one.
    """
    try:
        event, matcher = fork_review.load_template_declaration(spec.name, environ)
    except fork_review.TemplateError as exc:
        raise InstallError(str(exc)) from exc
    if event is None:
        raise InstallError(
            f"template {spec.name!r} does not declare an `event:` front-matter "
            "line; cgx fork-review needs to know which hook to attach it to"
        )
    return event, matcher


def _write_template_file(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def copy_fork_review_templates(
    fork_reviews: Sequence[ForkReviewSpec], environ: Mapping[str, str]
) -> None:
    """Seed each spec's packaged template into the user's template directory if absent.

    Never overwrites a template the user already has there (their own, or
    a previously-seeded copy they have since edited) -- the point is to
    hand them an editable starting copy of the prompt that is now running
    on their session, not to keep it in sync with the package.
    """
    for spec in fork_reviews:
        user_path = fork_review.user_template_path(spec.name, environ)
        if user_path.is_file():
            continue
        try:
            text = fork_review.packaged_template_text(spec.name)
        except fork_review.TemplateError:
            # No packaged template under this name; resolve_fork_review_hook
            # already required a user-directory template to exist for the
            # declaration to have resolved, so there is nothing to seed.
            continue
        _write_template_file(user_path, text)


def skill_source_dir() -> Any:
    """Return the packaged ``cache`` skill directory.

    The canonical source is the repo-root ``claude/skills/cache/``; the
    wheel's ``force-include`` mapping (see ``pyproject.toml``) copies it to
    ``claude_gateway_extensions/data/skills/cache/`` at build time, so an
    installed wheel resolves it via ``importlib.resources``. An editable
    install (``pip install -e .``) never runs that mapping, so this falls
    back to the repo-root path computed from this file's location.

    Raises :class:`InstallError` if neither location exists.
    """
    packaged = (
        importlib.resources.files("claude_gateway_extensions") / "data" / "skills" / _SKILL_NAME
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
    fork_reviews: Sequence[ForkReviewSpec] = (),
) -> InstallResult:
    """Idempotently install the Claude Code integration for ``scope``.

    Raises :class:`InstallError` if ``env.ANTHROPIC_BASE_URL`` already points
    elsewhere and ``force`` is ``False``, or if a ``fork_reviews`` entry
    names a template that cannot be resolved or declares no ``event:``.
    With ``dry_run=True``, computes and returns the resulting settings
    without writing anything -- ``fork_reviews`` template resolution still
    runs, and still raises, since it is a validation step, not a write.
    """
    path = settings_path(scope, environ, cwd)
    skill_dir = skills_dir(scope, environ, cwd)
    settings = load_settings(path)
    merge_base_url(settings, url, force)
    if url != DEFAULT_URL:
        merge_cgx_url(settings, url)
    merge_hook(settings, "UserPromptSubmit")
    for spec in fork_reviews:
        event, matcher = resolve_fork_review_hook(spec, environ)
        merge_hook(settings, event, fork_review_command(spec), matcher=matcher)
    if dry_run:
        return InstallResult(path, skill_dir, settings, True)
    write_settings(path, settings)
    copy_skill(skill_dir)
    copy_fork_review_templates(fork_reviews, environ)
    return InstallResult(path, skill_dir, settings, False)


def uninstall(
    *,
    scope: Scope,
    url: str,
    environ: Mapping[str, str],
    cwd: Path,
    dry_run: bool = False,
    fork_reviews: Sequence[ForkReviewSpec] = (),
) -> InstallResult:
    """Reverse exactly what :func:`install` added for ``scope``.

    A ``settings.json`` that does not exist is left absent rather than
    created empty: uninstalling must never leave behind a file the user
    never had. With ``dry_run=True``, computes and returns the resulting
    settings without writing or removing anything (mirrors :func:`install`).
    A template's copied file, if any, is never removed -- the user may have
    edited it since.
    """
    path = settings_path(scope, environ, cwd)
    skill_dir = skills_dir(scope, environ, cwd)
    settings = load_settings(path)
    remove_base_url(settings, url)
    remove_cgx_url(settings, url)
    remove_hook(settings, "UserPromptSubmit")
    for spec in fork_reviews:
        event, _matcher = resolve_fork_review_hook(spec, environ)
        remove_hook(settings, event, fork_review_command(spec))
    if dry_run:
        return InstallResult(path, skill_dir, settings, True)
    if path.is_file():
        write_settings(path, settings)
    remove_skill(skill_dir)
    return InstallResult(path, skill_dir, settings, False)


def _read_skill_source() -> str:
    """Return the packaged ``cache`` skill's ``SKILL.md`` content."""
    source = skill_source_dir() / "SKILL.md"
    with importlib.resources.as_file(source) as source_path:
        return source_path.read_text()


def plan_install(
    *,
    scope: Scope,
    url: str,
    force: bool,
    environ: Mapping[str, str],
    cwd: Path,
    fork_reviews: Sequence[ForkReviewSpec] = (),
) -> list[PlannedChange]:
    """Compute the settings.json, skill-file and template changes :func:`install` would make.

    Nothing is written; raises :class:`InstallError` under the same
    conditions :func:`install` would (bad JSON, a conflicting
    ``ANTHROPIC_BASE_URL`` without ``force``, a missing skill source, a
    ``fork_reviews`` entry naming an unresolvable or event-less template).
    The caller (``cgx configure``) shows each :class:`PlannedChange` as a
    diff and confirms it before calling its ``apply``.
    """
    preview = install(
        scope=scope,
        url=url,
        force=force,
        dry_run=True,
        environ=environ,
        cwd=cwd,
        fork_reviews=fork_reviews,
    )
    settings_old = preview.settings_path.read_text() if preview.settings_path.is_file() else None
    settings_new = json.dumps(preview.settings, indent=2) + "\n"
    changes = [
        PlannedChange(
            preview.settings_path,
            settings_old,
            settings_new,
            lambda: write_settings(preview.settings_path, preview.settings),
        )
    ]
    skill_file = preview.skill_dir / "SKILL.md"
    skill_old = skill_file.read_text() if skill_file.is_file() else None
    skill_new = _read_skill_source()
    changes.append(
        PlannedChange(skill_file, skill_old, skill_new, lambda: copy_skill(preview.skill_dir))
    )
    for spec in fork_reviews:
        user_path = fork_review.user_template_path(spec.name, environ)
        if user_path.is_file():
            continue
        text = fork_review.packaged_template_text(spec.name)
        changes.append(
            PlannedChange(
                user_path,
                None,
                text,
                lambda p=user_path, t=text: _write_template_file(p, t),
            )
        )
    return changes


def plan_uninstall(
    *,
    scope: Scope,
    url: str,
    environ: Mapping[str, str],
    cwd: Path,
    fork_reviews: Sequence[ForkReviewSpec] = (),
) -> list[PlannedChange]:
    """Compute the settings.json and skill-file changes :func:`uninstall` would make.

    Nothing is written. Unlike :func:`plan_install`, a file that does not
    already exist is simply omitted (never planned as "will create"):
    uninstalling must never bring a file into existence. A copied template
    file is never planned for removal here either, matching :func:`uninstall`.
    """
    preview = uninstall(
        scope=scope, url=url, environ=environ, cwd=cwd, dry_run=True, fork_reviews=fork_reviews
    )
    changes: list[PlannedChange] = []
    if preview.settings_path.is_file():
        settings_old = preview.settings_path.read_text()
        settings_new = json.dumps(preview.settings, indent=2) + "\n"
        changes.append(
            PlannedChange(
                preview.settings_path,
                settings_old,
                settings_new,
                lambda: write_settings(preview.settings_path, preview.settings),
            )
        )
    skill_file = preview.skill_dir / "SKILL.md"
    if skill_file.is_file():
        changes.append(
            PlannedChange(
                skill_file,
                skill_file.read_text(),
                None,
                lambda: remove_skill(preview.skill_dir),
            )
        )
    return changes


__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_URL",
    "HOOK_COMMAND",
    "ForkReviewSpec",
    "InstallError",
    "InstallResult",
    "Scope",
    "fork_review_command",
    "hook_entry_has_command",
    "install",
    "parse_fork_review_spec",
    "plan_install",
    "plan_uninstall",
    "resolve_fork_review_hook",
    "resolve_install_port",
    "resolve_install_url",
    "settings_path",
    "skills_dir",
    "uninstall",
    "write_settings",
]
