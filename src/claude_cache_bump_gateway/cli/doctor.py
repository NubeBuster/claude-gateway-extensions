"""``ccbg doctor``: a handful of environment checks with one line each.

Every check returns ``ok``, ``warn`` or ``fail``; :func:`run_doctor` prints
one line per check and returns exit code 1 if any check failed.
"""

from __future__ import annotations

import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO

from claude_cache_bump_gateway.cli import install
from claude_cache_bump_gateway.cli.client import (
    ApiError,
    ClientFactory,
    ControlClient,
    DaemonUnreachable,
)
from claude_cache_bump_gateway.config import Config

Status = Literal["ok", "warn", "fail"]

_CREDENTIAL_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_SCOPES: tuple[install.Scope, ...] = ("user", "project")


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


def check_daemon(client: ControlClient, url: str) -> CheckResult:
    """Check that the daemon answers ``GET /ccbg/health``."""
    try:
        client.health()
    except DaemonUnreachable:
        return CheckResult("fail", f"daemon not reachable at {url} (is `ccbg serve` running?)")
    except ApiError as exc:
        return CheckResult("fail", f"daemon at {url} returned HTTP {exc.status} for health check")
    return CheckResult("ok", f"daemon healthy at {url}")


def check_base_url(config: Config, environ: Mapping[str, str], cwd: Path) -> CheckResult:
    """Check that some scope's ``env.ANTHROPIC_BASE_URL`` points at the daemon."""
    for scope in _SCOPES:
        settings = _load_settings_quiet(scope, environ, cwd)
        env = settings.get("env")
        if isinstance(env, dict) and env.get("ANTHROPIC_BASE_URL") == config.url:
            path = install.settings_path(scope, environ, cwd)
            return CheckResult("ok", f"env.ANTHROPIC_BASE_URL set to {config.url} in {path}")
    return CheckResult(
        "warn", "env.ANTHROPIC_BASE_URL is not set in user or project settings; run `ccbg install`"
    )


def check_hook(environ: Mapping[str, str], cwd: Path) -> CheckResult:
    """Check that the ``UserPromptSubmit`` hook is registered in some scope."""
    for scope in _SCOPES:
        settings = _load_settings_quiet(scope, environ, cwd)
        hooks_section = settings.get("hooks")
        entries = hooks_section.get("UserPromptSubmit") if isinstance(hooks_section, dict) else None
        if isinstance(entries, list) and any(
            isinstance(e, dict) and install.hook_entry_has_command(e, install.HOOK_COMMAND)
            for e in entries
        ):
            path = install.settings_path(scope, environ, cwd)
            return CheckResult("ok", f"UserPromptSubmit hook registered in {path}")
    return CheckResult("warn", "UserPromptSubmit hook is not registered; run `ccbg install`")


def check_skill(environ: Mapping[str, str], cwd: Path) -> CheckResult:
    """Check that the ``cache`` skill is present in some scope."""
    for scope in _SCOPES:
        skill_dir = install.skills_dir(scope, environ, cwd)
        if (skill_dir / "SKILL.md").is_file():
            return CheckResult("ok", f"cache skill present at {skill_dir}")
    return CheckResult("warn", "cache skill not found; run `ccbg install`")


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
        env = settings.get("env")
        if isinstance(env, dict):
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
) -> int:
    """Run every check, print one line each, and return 1 if any check failed else 0."""
    client = client_factory(config.url)
    checks = [
        check_daemon(client, config.url),
        check_base_url(config, environ, cwd),
        check_hook(environ, cwd),
        check_skill(environ, cwd),
        check_credentials(environ, cwd),
        check_state_dir(config),
    ]
    for check in checks:
        stdout.write(f"{check.status}: {check.message}\n")
    return 1 if any(check.status == "fail" for check in checks) else 0


__all__ = ["CheckResult", "run_doctor"]
