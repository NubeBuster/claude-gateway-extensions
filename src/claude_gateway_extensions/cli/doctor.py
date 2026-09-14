"""``cgx doctor``: a handful of environment checks with one line each.

Every check returns ``ok``, ``warn`` or ``fail``; :func:`run_doctor` prints
one line per check and returns exit code 1 if any check failed.
"""

from __future__ import annotations

import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO
from urllib.parse import urlsplit

from claude_gateway_extensions.cli import install, service, shadow_fork
from claude_gateway_extensions.cli.client import (
    ApiError,
    ClientFactory,
    ControlClient,
    DaemonUnreachable,
)
from claude_gateway_extensions.config import Config

Status = Literal["ok", "warn", "fail"]

_CREDENTIAL_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_SCOPES: tuple[install.Scope, ...] = ("user", "project", "local")


@dataclass
class CheckResult:
    """One doctor check's outcome: a status and a one-line human-readable message."""

    status: Status
    message: str


def _load_settings_quiet(scope: install.Scope, environ: Mapping[str, str], cwd: Path) -> dict:
    """Load a scope's settings, treating unreadable/invalid JSON as empty rather than raising."""
    path = install.settings_path(scope, environ, cwd)
    try:
        return install.load_settings(path)
    except install.InstallError:
        return {}


def _dict_section(settings: dict, key: str) -> dict | None:
    """Return ``settings[key]`` if it is a dict, else ``None``.

    Settings files are user-editable JSON, so any top-level key (``env``,
    ``hooks``, ...) may hold a non-dict value or be absent entirely; every
    check below only cares about the dict case and treats anything else the
    same way, so that comparison lives here once.
    """
    value = settings.get(key)
    return value if isinstance(value, dict) else None


def check_daemon(client: ControlClient, url: str) -> CheckResult:
    """Check that the daemon answers ``GET /cgx/health`` as cgx itself, not a foreign program.

    See the "The port is probed before it is bound" decision in
    ``docs/DESIGN.md``: a 2xx response alone is not enough, because any
    program could be listening on the configured port -- only a body
    shaped like this daemon's own (``ok: true`` and a string ``version``)
    counts.
    """
    try:
        body = client.health()
    except DaemonUnreachable:
        return CheckResult("fail", f"daemon not reachable at {url} (is `cgx serve` running?)")
    except ApiError as exc:
        return CheckResult("fail", f"daemon at {url} returned HTTP {exc.status} for health check")
    if (
        not isinstance(body, dict)
        or body.get("ok") is not True
        or not isinstance(body.get("version"), str)
    ):
        return CheckResult(
            "fail",
            f"{url} answered, but not as cgx -- a different program may own this port",
        )
    return CheckResult("ok", f"daemon healthy at {url}")


def check_base_url(config: Config, environ: Mapping[str, str], cwd: Path) -> CheckResult:
    """Check that some scope's ``env.ANTHROPIC_BASE_URL`` points at the daemon."""
    for scope in _SCOPES:
        settings = _load_settings_quiet(scope, environ, cwd)
        env = _dict_section(settings, "env")
        if env is not None and env.get("ANTHROPIC_BASE_URL") == config.url:
            path = install.settings_path(scope, environ, cwd)
            return CheckResult("ok", f"env.ANTHROPIC_BASE_URL set to {config.url} in {path}")
    return CheckResult(
        "warn",
        "env.ANTHROPIC_BASE_URL is not set in user, project or local settings; run `cgx configure`",
    )


def _find_settings_port(
    environ: Mapping[str, str], cwd: Path
) -> tuple[int | None, install.Scope | None]:
    """Return the port embedded in whichever scope's ``env.ANTHROPIC_BASE_URL`` is set first."""
    for scope in _SCOPES:
        settings = _load_settings_quiet(scope, environ, cwd)
        env = _dict_section(settings, "env")
        base_url = env.get("ANTHROPIC_BASE_URL") if env is not None else None
        if isinstance(base_url, str):
            port = urlsplit(base_url).port
            if port is not None:
                return port, scope
    return None, None


def check_port_consistency(
    config: Config, environ: Mapping[str, str], cwd: Path, daemon_reachable: bool
) -> CheckResult:
    """Check that the settings URL, the unit's port and the reachable daemon agree on a port.

    ``daemon_reachable`` comes from :func:`check_daemon` (not re-checked here, to avoid a
    second network round trip) — it gates whether ``config.url``'s port counts as "the
    reachable daemon" or is left out of the comparison. Sources that are simply absent (no
    settings written yet, no unit installed) are not a mismatch; only two or more known,
    disagreeing ports are.
    """
    settings_port, settings_scope = _find_settings_port(environ, cwd)
    unit_port = service.installed_port(environ)
    daemon_port = urlsplit(config.url).port if daemon_reachable else None

    sources: dict[str, int] = {}
    if settings_port is not None:
        sources[f"{settings_scope} settings ANTHROPIC_BASE_URL"] = settings_port
    if unit_port is not None:
        sources["systemd unit CGX_PORT"] = unit_port
    if daemon_port is not None:
        sources["reachable daemon (config.url)"] = daemon_port

    if len(set(sources.values())) <= 1:
        if sources:
            port = next(iter(sources.values()))
            return CheckResult("ok", f"port {port} agrees across {', '.join(sources)}")
        return CheckResult("ok", "no port sources to compare yet")

    detail = ", ".join(f"{name}={port}" for name, port in sources.items())
    return CheckResult(
        "warn",
        f"ports disagree ({detail}); pick one port N and run "
        f"`cgx service install --port N` and `cgx configure --port N` to realign",
    )


def check_hook(environ: Mapping[str, str], cwd: Path) -> CheckResult:
    """Check that the ``UserPromptSubmit`` hook is registered in some scope."""
    for scope in _SCOPES:
        settings = _load_settings_quiet(scope, environ, cwd)
        hooks_section = _dict_section(settings, "hooks")
        entries = hooks_section.get("UserPromptSubmit") if hooks_section is not None else None
        if isinstance(entries, list) and any(
            isinstance(e, dict) and install.hook_entry_has_command(e, install.HOOK_COMMAND)
            for e in entries
        ):
            path = install.settings_path(scope, environ, cwd)
            return CheckResult("ok", f"UserPromptSubmit hook registered in {path}")
    return CheckResult("warn", "UserPromptSubmit hook is not registered; run `cgx configure`")


def check_skill(environ: Mapping[str, str], cwd: Path) -> CheckResult:
    """Check that the ``cache`` skill is present in some scope."""
    for scope in _SCOPES:
        skill_dir = install.skills_dir(scope, environ, cwd)
        if (skill_dir / "SKILL.md").is_file():
            return CheckResult("ok", f"cache skill present at {skill_dir}")
    return CheckResult("warn", "cache skill not found; run `cgx configure`")


#: Matches a ``cgx shadow-fork --template NAME ...`` hook command, the exact
#: string :func:`~claude_gateway_extensions.cli.install.shadow_fork_command`
#: writes; captures the template name.
_SHADOW_FORK_COMMAND_RE = re.compile(r"^cgx shadow-fork --template (\S+)")


def check_shadow_fork_templates(environ: Mapping[str, str], cwd: Path) -> list[CheckResult]:
    """Check that every ``cgx shadow-fork --template X`` hook entry resolves.

    Scans ``hooks.<event>`` in every scope's settings (the same files the
    other checks read) for a ``cgx shadow-fork`` command, resolves its
    template the same way the hook does
    (:func:`~claude_gateway_extensions.cli.shadow_fork.load_template_declaration`,
    reused rather than re-implemented), and reports one result per entry: a
    missing/unreadable template is ``fail`` (it fails open, silently, at
    hook time -- this is the only place it is surfaced up front); a
    front-matter ``event:`` that disagrees with the ``hooks.<event>`` key
    the entry is actually wired under is ``warn`` (the hook still runs, just
    never matches, per ``run_shadow_fork``'s declared-event skip); otherwise
    ``ok`` naming where it resolved from. Returns an empty list if no
    ``--shadow-fork`` hook entries exist in any scope.
    """
    results: list[CheckResult] = []
    for scope in _SCOPES:
        settings = _load_settings_quiet(scope, environ, cwd)
        hooks_section = _dict_section(settings, "hooks")
        if hooks_section is None:
            continue
        settings_path = install.settings_path(scope, environ, cwd)
        for event, entries in hooks_section.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                for hook in entry.get("hooks", []) if isinstance(entry.get("hooks"), list) else []:
                    if not isinstance(hook, dict):
                        continue
                    command = hook.get("command")
                    if not isinstance(command, str):
                        continue
                    match = _SHADOW_FORK_COMMAND_RE.match(command)
                    if match is None:
                        continue
                    name = match.group(1)
                    where = f"shadow-fork template {name!r} (hooks.{event} in {settings_path})"
                    try:
                        declared_event, _matcher = shadow_fork.load_template_declaration(
                            name, environ
                        )
                    except shadow_fork.TemplateError as exc:
                        results.append(CheckResult("fail", f"{where}: {exc}"))
                        continue
                    user_path = shadow_fork.user_template_path(name, environ)
                    location = str(user_path) if user_path.is_file() else "the packaged template"
                    if declared_event is not None and declared_event != event:
                        results.append(
                            CheckResult(
                                "warn",
                                f"{where} resolves to {location}, but its front matter "
                                f"declares event {declared_event!r} -- the hook fires under "
                                f"{event!r} and will never match it",
                            )
                        )
                    else:
                        results.append(CheckResult("ok", f"{where} resolves to {location}"))
    return results


def check_credentials(environ: Mapping[str, str], cwd: Path) -> CheckResult:
    """Check that nothing overrides the subscription credential.

    Any of ``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN`` (environment or
    settings ``env``) or a settings ``apiKeyHelper`` replaces the forwarded
    subscription login and changes what the daemon bills against.
    """
    problems: list[str] = []
    for name in _CREDENTIAL_ENV_VARS:
        if environ.get(name):
            problems.append(f"{name} is set in the environment")
    for scope in _SCOPES:
        settings = _load_settings_quiet(scope, environ, cwd)
        env = _dict_section(settings, "env")
        if env is not None:
            for name in _CREDENTIAL_ENV_VARS:
                if env.get(name):
                    problems.append(f"{name} is set in {scope} settings env")
        if settings.get("apiKeyHelper"):
            problems.append(f"apiKeyHelper is set in {scope} settings")
    if problems:
        return CheckResult(
            "fail",
            "credential override present (replaces the subscription credential): "
            + "; ".join(problems),
        )
    return CheckResult(
        "ok", "no ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN/apiKeyHelper override found"
    )


def check_service(
    environ: Mapping[str, str], query: service.Query = service.DEFAULT_QUERY
) -> CheckResult:
    """Check the systemd user unit's presence, and its enabled/active state if present."""
    status = service.service_status(environ, query)
    if not status.present:
        return CheckResult("warn", "systemd service not installed; run `cgx service install`")
    if not status.active:
        return CheckResult(
            "warn",
            f"systemd service at {status.unit_path} is not active "
            f"(enabled={status.enabled}, active={status.active})",
        )
    return CheckResult(
        "ok",
        f"systemd service at {status.unit_path} is "
        f"enabled={status.enabled}, active={status.active}",
    )


def check_state_dir(config: Config) -> CheckResult:
    """Check that the state directory, if it exists, is mode 0700."""
    if not config.state_dir.is_dir():
        return CheckResult("ok", f"state dir does not exist yet ({config.state_dir})")
    mode = stat.S_IMODE(config.state_dir.stat().st_mode)
    if mode != 0o700:
        return CheckResult(
            "warn", f"state dir {config.state_dir} has mode {oct(mode)}, expected 0700"
        )
    return CheckResult("ok", f"state dir {config.state_dir} is 0700")


def run_doctor(
    config: Config,
    environ: Mapping[str, str],
    cwd: Path,
    stdout: TextIO,
    client_factory: ClientFactory = ControlClient,
    platform: str = sys.platform,
) -> int:
    """Run every check, print one line each, and return 1 if any check failed else 0.

    ``platform`` (default ``sys.platform``, overridable for tests) skips
    :func:`check_service` on anything but Linux: the systemd unit it checks
    for can never exist there, so the check would only ever produce a
    permanent, unfixable warning. A one-line ``ok`` note takes its place.
    """
    client = client_factory(config.url)
    daemon_check = check_daemon(client, config.url)
    service_check = (
        check_service(environ)
        if platform == "linux"
        else CheckResult("ok", "systemd service checks skipped (not on Linux)")
    )
    checks = [
        daemon_check,
        check_base_url(config, environ, cwd),
        check_port_consistency(config, environ, cwd, daemon_check.status == "ok"),
        check_hook(environ, cwd),
        check_skill(environ, cwd),
        check_credentials(environ, cwd),
        service_check,
        check_state_dir(config),
        *check_shadow_fork_templates(environ, cwd),
    ]
    for check in checks:
        stdout.write(f"{check.status}: {check.message}\n")
    return 1 if any(check.status == "fail" for check in checks) else 0


__all__ = [
    "CheckResult",
    "check_port_consistency",
    "check_shadow_fork_templates",
    "run_doctor",
]
