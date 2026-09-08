"""``ccbg`` argparse entry point.

Wires every subcommand to the control API client (:mod:`.client`), the hook
and statusline handlers, the installer, and doctor. Every dependency that a
test
needs to fake — the control API client, the daemon process, the systemd
runner, stdio, the environment and the working directory — is a parameter of
:func:`main`, not a module global, so tests exercise the real dispatch code
without touching the network, the filesystem outside a temp dir, or a real
process.

Exit codes: 0 ok, 1 operation failed, 2 usage error, 3 daemon unreachable.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from claude_cache_bump_gateway.cli import install, service
from claude_cache_bump_gateway.cli.client import (
    ApiError,
    ClientFactory,
    ControlClient,
    DaemonUnreachable,
)
from claude_cache_bump_gateway.cli.doctor import check_daemon, run_doctor
from claude_cache_bump_gateway.cli.hook import run_user_prompt_submit
from claude_cache_bump_gateway.cli.statusline import run_statusline
from claude_cache_bump_gateway.cli.when import At, WhenError, local_now, parse_when
from claude_cache_bump_gateway.config import Config, ConfigError
from claude_cache_bump_gateway.core.status_format import format_deadline

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3


class _UsageError(Exception):
    """Raised by a handler for a usage problem detected after argparse (e.g. no session id)."""


@dataclass
class _Context:
    """Every ambient dependency a subcommand handler needs, gathered in one place for tests."""

    environ: Mapping[str, str]
    stdin: TextIO
    stdout: TextIO
    stderr: TextIO
    cwd: Path
    client_factory: ClientFactory
    service_runner: service.Runner | None
    service_query: service.Query | None


def _require_session(args: argparse.Namespace, ctx: _Context) -> str:
    """Return the session id from ``--session`` or ``CLAUDE_SESSION_ID``.

    Raises :class:`_UsageError` with a message explaining how to find the id
    if neither is set.
    """
    session_id = args.session or ctx.environ.get("CLAUDE_SESSION_ID")
    if not session_id:
        raise _UsageError(
            "no session id given: pass --session ID or set CLAUDE_SESSION_ID "
            "(run `ccbg status` to list known session ids, or read "
            "$CLAUDE_SESSION_ID from inside a Claude Code session)"
        )
    return session_id


def _format_until_local(until: object, now: float) -> str:
    """Format an ``until`` epoch value via :func:`format_deadline`, or ``-`` if unset."""
    if not isinstance(until, int | float):
        return "-"
    return format_deadline(until, now)


def _format_status_line(session: Mapping[str, Any], now: float) -> str:
    """Format one ``status``/``on``/``off`` output line for a session status payload."""
    session_id = session.get("session_id", "?")
    enabled = session.get("enabled", False)
    next_bump_at = session.get("next_bump_at")
    next_bump = "-"
    if isinstance(next_bump_at, int | float):
        next_bump = f"{max(0, round((next_bump_at - now) / 60))}m"
    last_result = session.get("last_result")
    last_outcome = last_result.get("outcome") if isinstance(last_result, dict) else None
    stopped_reason = session.get("stopped_reason") or "-"
    until = _format_until_local(session.get("until"), now)
    return (
        f"{session_id}  enabled={enabled}  next_bump={next_bump}  until={until}  "
        f"last_result={last_outcome or '-'}  stopped={stopped_reason}"
    )


def _cmd_serve(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Run the daemon by importing and calling ``server.run(config)`` lazily.

    Imported lazily so every other subcommand works without ``aiohttp`` (or
    the server package, which may not exist yet during CLI-only development)
    ever being imported.
    """
    del args
    try:
        server_module = importlib.import_module("claude_cache_bump_gateway.server")
    except ModuleNotFoundError as exc:
        ctx.stderr.write(f"server module not available: {exc}\n")
        return EXIT_FAILED
    server_module.run(config)
    return EXIT_OK


def _cmd_status(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """List every session's status, or one session's if ``--session``/env resolves one."""
    client = ctx.client_factory(config.url)
    session_id = args.session or ctx.environ.get("CLAUDE_SESSION_ID")
    try:
        sessions: list[dict[str, Any]]
        sessions = [client.get_session(session_id)] if session_id else client.list_sessions()
    except DaemonUnreachable as exc:
        ctx.stderr.write(f"{exc}\n")
        return EXIT_UNREACHABLE
    except ApiError as exc:
        if exc.status == 404:
            ctx.stderr.write(f"unknown session: {session_id}\n")
            return EXIT_FAILED
        ctx.stderr.write(f"{exc}\n")
        return EXIT_FAILED
    if args.json:
        ctx.stdout.write(json.dumps(sessions, indent=2) + "\n")
        return EXIT_OK
    now = time.time()
    for session in sessions:
        ctx.stdout.write(_format_status_line(session, now) + "\n")
    return EXIT_OK


def _resolve_until(args: argparse.Namespace) -> float | None:
    """Parse ``--until`` (an ``on``-only flag) into an epoch deadline, or ``None`` if unset."""
    until_text = getattr(args, "until", None)
    if until_text is None:
        return None
    try:
        deadline = parse_when(until_text, local_now())
    except WhenError as exc:
        raise _UsageError(f"--until {until_text!r}: {exc}") from exc
    if not isinstance(deadline, At):
        raise _UsageError(
            f"--until {until_text!r}: expected a deadline, e.g. 2h, 9PM, 2026-12-31 08:00"
        )
    return deadline.when.timestamp()


def _cmd_set_enabled(args: argparse.Namespace, config: Config, ctx: _Context, enabled: bool) -> int:
    """Shared handler for ``on`` and ``off``."""
    session_id = _require_session(args, ctx)
    until = _resolve_until(args)
    client = ctx.client_factory(config.url)
    try:
        result = client.set_session(
            session_id,
            enabled,
            interval_s=args.interval,
            max_duration_s=args.max_duration,
            until=until,
        )
    except DaemonUnreachable as exc:
        ctx.stderr.write(f"{exc}\n")
        return EXIT_UNREACHABLE
    except ApiError as exc:
        ctx.stderr.write(f"{exc}\n")
        return EXIT_FAILED
    ctx.stdout.write(_format_status_line(result, time.time()) + "\n")
    return EXIT_OK


def _cmd_now(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Bump the session immediately and print the outcome and cache token counts."""
    session_id = _require_session(args, ctx)
    client = ctx.client_factory(config.url)
    try:
        result = client.bump(session_id)
    except DaemonUnreachable as exc:
        ctx.stderr.write(f"{exc}\n")
        return EXIT_UNREACHABLE
    except ApiError as exc:
        ctx.stderr.write(f"{exc}\n")
        return EXIT_FAILED
    outcome = result.get("outcome")
    usage = result.get("usage") or {}
    if outcome is None:
        ctx.stdout.write(f"error: {result.get('error') or result}\n")
        return EXIT_FAILED
    ctx.stdout.write(
        f"outcome={outcome} "
        f"cache_read={usage.get('cache_read_input_tokens', '-')} "
        f"cache_creation={usage.get('cache_creation_input_tokens', '-')}\n"
    )
    return EXIT_OK if outcome == "ok" else EXIT_FAILED


def _cmd_forget(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Discard a session's snapshots and state."""
    session_id = _require_session(args, ctx)
    client = ctx.client_factory(config.url)
    try:
        client.forget(session_id)
    except DaemonUnreachable as exc:
        ctx.stderr.write(f"{exc}\n")
        return EXIT_UNREACHABLE
    except ApiError as exc:
        ctx.stderr.write(f"{exc}\n")
        return EXIT_FAILED
    ctx.stdout.write(f"forgot {session_id}\n")
    return EXIT_OK


def _cmd_hook(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Dispatch a Claude Code hook event by name."""
    del config
    if args.hook_command == "user-prompt-submit":
        return run_user_prompt_submit(ctx.stdin, ctx.stdout, ctx.environ)
    ctx.stderr.write(f"unknown hook command: {args.hook_command}\n")
    return EXIT_USAGE


def _cmd_statusline(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Print the statusline segment."""
    del args, config
    return run_statusline(ctx.stdin, ctx.stdout, ctx.environ)


#: Pointer printed when a removed flag is passed; kept as one constant so the
#: install/uninstall messages and their test assertions stay in sync.
_SERVICE_FLAG_REMOVED = (
    "--service was removed from `ccbg {cmd}`; the daemon is per user, not per "
    "scope — run `ccbg service {cmd}` instead\n"
)


def _cmd_install(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Install the Claude Code integration for the chosen scope."""
    if args.service:
        ctx.stderr.write(_SERVICE_FLAG_REMOVED.format(cmd="install"))
        return EXIT_USAGE
    try:
        result = install.install(
            scope=args.scope,
            url=config.url,
            force=args.force,
            dry_run=args.dry_run,
            environ=ctx.environ,
            cwd=ctx.cwd,
        )
    except install.InstallError as exc:
        ctx.stderr.write(f"{exc}\n")
        return EXIT_FAILED
    if result.dry_run:
        ctx.stdout.write(json.dumps(result.settings, indent=2) + "\n")
        return EXIT_OK
    ctx.stdout.write(f"wrote {result.settings_path}\n")
    ctx.stdout.write(f"installed skill at {result.skill_dir}\n")
    return EXIT_OK


def _cmd_uninstall(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Remove the Claude Code integration for the chosen scope."""
    if args.service:
        ctx.stderr.write(_SERVICE_FLAG_REMOVED.format(cmd="uninstall"))
        return EXIT_USAGE
    try:
        result = install.uninstall(
            scope=args.scope,
            url=config.url,
            environ=ctx.environ,
            cwd=ctx.cwd,
        )
    except install.InstallError as exc:
        ctx.stderr.write(f"{exc}\n")
        return EXIT_FAILED
    ctx.stdout.write(f"removed integration from {result.settings_path}\n")
    return EXIT_OK


def _cmd_service_install(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Write and enable the systemd user unit that runs the daemon."""
    del args, config
    runner = ctx.service_runner if ctx.service_runner is not None else service.DEFAULT_RUNNER
    path = service.install_service(ctx.environ, runner)
    ctx.stdout.write(f"installed and started claude-cache-bump-gateway.service ({path})\n")
    return EXIT_OK


def _cmd_service_uninstall(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Disable and remove the systemd user unit, reversing exactly what install wrote."""
    del args, config
    runner = ctx.service_runner if ctx.service_runner is not None else service.DEFAULT_RUNNER
    service.uninstall_service(ctx.environ, runner)
    ctx.stdout.write("removed claude-cache-bump-gateway.service\n")
    return EXIT_OK


def _cmd_service_status(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Print the systemd unit's present/enabled/active state and the daemon health check."""
    del args
    query = ctx.service_query if ctx.service_query is not None else service.DEFAULT_QUERY
    status = service.service_status(ctx.environ, query)
    if not status.present:
        ctx.stdout.write("service: not installed; run `ccbg service install`\n")
    else:
        ctx.stdout.write(
            f"service: unit present at {status.unit_path}, "
            f"enabled={status.enabled}, active={status.active}\n"
        )
    client = ctx.client_factory(config.url)
    health = check_daemon(client, config.url)
    ctx.stdout.write(f"{health.status}: {health.message}\n")
    return EXIT_OK if health.status != "fail" else EXIT_FAILED


def _cmd_service(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Dispatch a ``ccbg service`` subcommand by name."""
    handler = _SERVICE_HANDLERS[args.service_command]
    return handler(args, config, ctx)


def _cmd_doctor(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Run the doctor checks."""
    del args
    return run_doctor(config, ctx.environ, ctx.cwd, ctx.stdout, client_factory=ctx.client_factory)


def _build_parser() -> argparse.ArgumentParser:
    """Build the ``ccbg`` argument parser with every subcommand."""
    parser = argparse.ArgumentParser(
        prog="ccbg", description="Keep a Claude Code session's prompt cache warm."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("serve", help="run the daemon")

    status_parser = subparsers.add_parser("status", help="show session status")
    status_parser.add_argument("--session", help="session id (default: all sessions)")
    status_parser.add_argument("--json", action="store_true", help="print raw JSON")

    def add_on_off(name: str) -> None:
        p = subparsers.add_parser(name, help=f"turn bumping {name} for a session")
        p.add_argument("--session", help="session id (default: $CLAUDE_SESSION_ID)")
        p.add_argument("--interval", type=int, default=None, help="bump interval in seconds")
        p.add_argument(
            "--max-duration",
            type=int,
            default=None,
            dest="max_duration",
            help="max duration in seconds",
        )
        if name == "on":
            p.add_argument(
                "--until",
                default=None,
                help="absolute deadline, e.g. 2h, 9PM, 2026-12-31 08:00 (see /cache grammar)",
            )

    add_on_off("on")
    add_on_off("off")

    now_parser = subparsers.add_parser("now", help="bump a session immediately")
    now_parser.add_argument("--session", help="session id (default: $CLAUDE_SESSION_ID)")

    forget_parser = subparsers.add_parser("forget", help="forget a session")
    forget_parser.add_argument("--session", help="session id (default: $CLAUDE_SESSION_ID)")

    hook_parser = subparsers.add_parser("hook", help="Claude Code hook entry points")
    hook_parser.add_argument("hook_command", choices=["user-prompt-submit"])

    subparsers.add_parser("statusline", help="print a statusline segment")

    _SCOPE_HELP = (
        "chooses which Claude Code settings.json is edited (user or project); "
        "the daemon is per user, see `ccbg service`"
    )

    install_parser = subparsers.add_parser("install", help="install the Claude Code integration")
    install_parser.add_argument(
        "--scope", choices=["user", "project"], default="user", help=_SCOPE_HELP
    )
    install_parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    install_parser.add_argument(
        "--force", action="store_true", help="override a conflicting ANTHROPIC_BASE_URL"
    )
    install_parser.add_argument("--service", action="store_true", help=argparse.SUPPRESS)

    uninstall_parser = subparsers.add_parser("uninstall", help="remove the Claude Code integration")
    uninstall_parser.add_argument(
        "--scope", choices=["user", "project"], default="user", help=_SCOPE_HELP
    )
    uninstall_parser.add_argument("--service", action="store_true", help=argparse.SUPPRESS)

    subparsers.add_parser("doctor", help="check the environment")

    service_parser = subparsers.add_parser(
        "service", help="manage the systemd user service running the daemon"
    )
    service_subparsers = service_parser.add_subparsers(dest="service_command", required=True)
    service_subparsers.add_parser("install", help="write and enable the systemd user unit")
    service_subparsers.add_parser("uninstall", help="disable and remove the systemd user unit")
    service_subparsers.add_parser("status", help="show the unit state and daemon health")

    return parser


_HANDLERS: dict[str, Callable[[argparse.Namespace, Config, _Context], int]] = {
    "serve": _cmd_serve,
    "status": _cmd_status,
    "on": lambda args, config, ctx: _cmd_set_enabled(args, config, ctx, True),
    "off": lambda args, config, ctx: _cmd_set_enabled(args, config, ctx, False),
    "now": _cmd_now,
    "forget": _cmd_forget,
    "hook": _cmd_hook,
    "statusline": _cmd_statusline,
    "install": _cmd_install,
    "uninstall": _cmd_uninstall,
    "doctor": _cmd_doctor,
    "service": _cmd_service,
}

_SERVICE_HANDLERS: dict[str, Callable[[argparse.Namespace, Config, _Context], int]] = {
    "install": _cmd_service_install,
    "uninstall": _cmd_service_uninstall,
    "status": _cmd_service_status,
}


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    cwd: Path | None = None,
    client_factory: ClientFactory = ControlClient,
    service_runner: service.Runner | None = None,
    service_query: service.Query | None = None,
) -> int:
    """Parse ``argv`` and dispatch to the matching subcommand handler.

    Every ambient dependency (environment, stdio, working directory, the
    control API client, the systemd runner) defaults to the real thing and
    can be overridden, which is how tests exercise this function directly
    instead of shelling out to a subprocess.
    """
    env = os.environ if environ is None else environ
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    inp = sys.stdin if stdin is None else stdin
    directory = Path.cwd() if cwd is None else cwd

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else EXIT_USAGE

    ctx = _Context(env, inp, out, err, directory, client_factory, service_runner, service_query)

    try:
        config = Config.from_env(env)
    except ConfigError as exc:
        err.write(f"{exc}\n")
        return EXIT_USAGE

    handler = _HANDLERS[args.command]
    try:
        return handler(args, config, ctx)
    except _UsageError as exc:
        err.write(f"{exc}\n")
        return EXIT_USAGE


__all__ = ["EXIT_FAILED", "EXIT_OK", "EXIT_UNREACHABLE", "EXIT_USAGE", "main"]
