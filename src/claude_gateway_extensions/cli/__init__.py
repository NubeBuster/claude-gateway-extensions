"""``cgx`` argparse entry point.

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
import io
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TextIO

from claude_gateway_extensions.cli import confirm, install, service
from claude_gateway_extensions.cli.client import (
    ApiError,
    ClientFactory,
    ControlClient,
    DaemonUnreachable,
)
from claude_gateway_extensions.cli.doctor import check_daemon, run_doctor
from claude_gateway_extensions.cli.hook import run_user_prompt_submit
from claude_gateway_extensions.cli.shadow_fork import run_shadow_fork
from claude_gateway_extensions.cli.statusline import run_statusline
from claude_gateway_extensions.cli.when import At, WhenError, local_now, parse_when
from claude_gateway_extensions.config import Config, ConfigError
from claude_gateway_extensions.core.status_format import format_deadline

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
    platform: str


def _require_session(args: argparse.Namespace, ctx: _Context) -> str:
    """Return the session id from ``--session`` or ``CLAUDE_SESSION_ID``.

    Raises :class:`_UsageError` with a message explaining how to find the id
    if neither is set.
    """
    session_id = args.session or ctx.environ.get("CLAUDE_SESSION_ID")
    if not session_id:
        raise _UsageError(
            "no session id given: pass --session ID or set CLAUDE_SESSION_ID "
            "(run `cgx status` to list known session ids, or read "
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
    formatted = session.get("formatted")
    rewarm_write = formatted.get("rewarm_write") if isinstance(formatted, dict) else None
    rewarm_suffix = f"  rewarm_write={rewarm_write}" if rewarm_write else ""
    cache = formatted.get("cache") if isinstance(formatted, dict) else None
    cache_suffix = f"  cache={cache}" if cache else ""
    return (
        f"{session_id}  enabled={enabled}  next_bump={next_bump}  until={until}  "
        f"last_result={last_outcome or '-'}  stopped={stopped_reason}"
        f"{cache_suffix}{rewarm_suffix}"
    )


def _client_error_exit(ctx: _Context, exc: DaemonUnreachable | ApiError) -> int:
    """Write a control-client failure to stderr and return the matching exit code.

    Shared by every ``_cmd_*`` handler that talks to the daemon so the
    ``DaemonUnreachable``/``ApiError`` -> exit-code mapping lives in one
    place; a handler with its own status-specific message (e.g.
    :func:`_cmd_status`'s 404 handling) still calls this for the rest.
    """
    ctx.stderr.write(f"{exc}\n")
    return EXIT_UNREACHABLE if isinstance(exc, DaemonUnreachable) else EXIT_FAILED


def _cmd_serve(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Run the daemon by importing and calling ``server.run(config)`` lazily.

    Imported lazily so every other subcommand works without ``aiohttp`` (or
    the server package, which may not exist yet during CLI-only development)
    ever being imported. ``--port``/``--host`` override whatever
    ``CGX_PORT``/``CGX_HOST`` resolved to from the environment. ``--handover``
    binds alongside (not instead of) an already-running daemon via
    ``SO_REUSEPORT`` and asks it to step aside once this process is healthy —
    a zero-gap manual upgrade path for anyone not using ``cgx service``; see
    the README's "Running without systemd". A port already owned by
    something else (another cgx daemon without ``--handover``, or any other
    program) is reported here as ``PortOwnedError`` and turned into a plain
    stderr line and a non-zero exit rather than a traceback.
    """
    if args.port is not None or args.host is not None:
        config = replace(
            config,
            host=args.host if args.host is not None else config.host,
            port=args.port if args.port is not None else config.port,
        )
    try:
        server_module = importlib.import_module("claude_gateway_extensions.server")
    except ModuleNotFoundError as exc:
        ctx.stderr.write(f"server module not available: {exc}\n")
        return EXIT_FAILED
    try:
        server_module.run(config, handover=args.handover)
    except server_module.PortOwnedError as exc:
        ctx.stderr.write(f"{exc}\n")
        return EXIT_FAILED
    return EXIT_OK


def _cmd_status(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """List every session's status, or one session's if ``--session``/env resolves one."""
    client = ctx.client_factory(config.url)
    session_id = args.session or ctx.environ.get("CLAUDE_SESSION_ID")
    try:
        sessions: list[dict[str, Any]]
        sessions = [client.get_session(session_id)] if session_id else client.list_sessions()
    except DaemonUnreachable as exc:
        return _client_error_exit(ctx, exc)
    except ApiError as exc:
        if exc.status == 404:
            if session_id:
                ctx.stderr.write(f"unknown session: {session_id}\n")
            else:
                ctx.stderr.write(f"no session list at {config.url} (is this the cgx daemon?)\n")
            return EXIT_FAILED
        return _client_error_exit(ctx, exc)
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
            rewarm=getattr(args, "rewarm", False),
        )
    except (DaemonUnreachable, ApiError) as exc:
        return _client_error_exit(ctx, exc)
    ctx.stdout.write(_format_status_line(result, time.time()) + "\n")
    return EXIT_OK


def _cmd_now(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Bump the session immediately and print the outcome and cache token counts."""
    session_id = _require_session(args, ctx)
    client = ctx.client_factory(config.url)
    try:
        result = client.bump(session_id)
    except (DaemonUnreachable, ApiError) as exc:
        return _client_error_exit(ctx, exc)
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
    except (DaemonUnreachable, ApiError) as exc:
        return _client_error_exit(ctx, exc)
    ctx.stdout.write(f"forgot {session_id}\n")
    return EXIT_OK


def _cmd_hook(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Dispatch a Claude Code hook event by name."""
    del config
    if args.hook_command == "user-prompt-submit":
        return run_user_prompt_submit(ctx.stdin, ctx.stdout, ctx.environ)
    ctx.stderr.write(f"unknown hook command: {args.hook_command}\n")
    return EXIT_USAGE


def _cmd_shadow_fork(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Run a shadow fork against a hook event and print the mapped verdict.

    ``--session``, when given, overrides the hook payload's ``session_id``
    rather than being threaded through :func:`run_shadow_fork` as a separate
    parameter -- mainly useful for invoking this by hand outside a real
    hook, e.g. to try a template against a live session.
    """
    del config
    stdin = ctx.stdin
    if args.session:
        raw = ctx.stdin.read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            payload["session_id"] = args.session
            raw = json.dumps(payload)
        stdin = io.StringIO(raw)
    return run_shadow_fork(
        stdin,
        ctx.stdout,
        ctx.environ,
        args.template,
        prefix=args.prefix,
        model=args.model,
        apply=args.apply,
    )


def _cmd_statusline(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Print the statusline segment."""
    del args, config
    return run_statusline(ctx.stdin, ctx.stdout, ctx.environ)


#: Pointer printed when a removed flag is passed; kept as one constant so the
#: configure/unconfigure messages and their test assertions stay in sync.
#: ``cmd`` is the (renamed) top-level command, ``service_cmd`` the unchanged
#: ``cgx service`` subcommand it points at (they no longer share a name).
_SERVICE_FLAG_REMOVED = (
    "--service was removed from `cgx {cmd}`; the daemon is per user, not per "
    "scope — run `cgx service {service_cmd}` instead\n"
)


def _cmd_configure(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Plan the Claude Code integration for the chosen scope and confirm each write.

    The URL written to ``env.ANTHROPIC_BASE_URL`` is derived from
    ``--port`` (falling back to ``CGX_PORT``/``CGX_URL`` in the
    environment, then the installed systemd unit's port, then the default),
    not from ``config.url`` — see :func:`install.resolve_install_url`. Every
    settings.json/skill-file write is planned first (:func:`install.plan_install`),
    then shown as a colored diff and confirmed per file
    (:func:`confirm.apply_changes`) before anything touches disk.
    """
    del config
    if args.service:
        ctx.stderr.write(_SERVICE_FLAG_REMOVED.format(cmd="configure", service_cmd="install"))
        return EXIT_USAGE
    installed_port = service.installed_port(ctx.environ)
    url = install.resolve_install_url(args.port, ctx.environ, installed_port)
    shadow_forks = [install.parse_shadow_fork_spec(v) for v in (args.shadow_fork or [])]
    try:
        changes = install.plan_install(
            scope=args.scope,
            url=url,
            force=args.force,
            environ=ctx.environ,
            cwd=ctx.cwd,
            shadow_forks=shadow_forks,
        )
    except install.InstallError as exc:
        ctx.stderr.write(f"{exc}\n")
        return EXIT_FAILED
    ctx.stdout.write(f"daemon URL: {url}\n")
    return confirm.apply_changes(
        changes,
        yes=args.yes,
        dry_run=args.dry_run,
        stdin=ctx.stdin,
        stdout=ctx.stdout,
        stderr=ctx.stderr,
        environ=ctx.environ,
    )


def _cmd_unconfigure(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Plan removal of the Claude Code integration for the chosen scope and confirm each write."""
    if args.service:
        ctx.stderr.write(_SERVICE_FLAG_REMOVED.format(cmd="unconfigure", service_cmd="uninstall"))
        return EXIT_USAGE
    shadow_forks = [install.parse_shadow_fork_spec(v) for v in (args.shadow_fork or [])]
    try:
        changes = install.plan_uninstall(
            scope=args.scope,
            url=config.url,
            environ=ctx.environ,
            cwd=ctx.cwd,
            shadow_forks=shadow_forks,
        )
    except install.InstallError as exc:
        ctx.stderr.write(f"{exc}\n")
        return EXIT_FAILED
    return confirm.apply_changes(
        changes,
        yes=args.yes,
        dry_run=args.dry_run,
        stdin=ctx.stdin,
        stdout=ctx.stdout,
        stderr=ctx.stderr,
        environ=ctx.environ,
    )


def _cmd_service_install(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Plan the systemd user unit that runs the daemon and confirm writing it.

    ``--port``/``--host`` are written into the unit explicitly, overriding
    whatever ``CGX_PORT``/``CGX_HOST`` happen to be set in the installing
    shell (every other ``CGX_*`` var is still copied through unchanged). A
    bad unit edit breaks the live daemon, so the unit is planned first
    (:func:`service.plan_install_service`) and goes through the same
    diff-and-confirm flow as ``cgx configure`` (:func:`confirm.apply_changes`).
    """
    del config
    runner = ctx.service_runner if ctx.service_runner is not None else service.DEFAULT_RUNNER
    change = service.plan_install_service(ctx.environ, runner, port=args.port, host=args.host)
    return confirm.apply_changes(
        [change],
        yes=args.yes,
        dry_run=args.dry_run,
        stdin=ctx.stdin,
        stdout=ctx.stdout,
        stderr=ctx.stderr,
        environ=ctx.environ,
    )


def _cmd_service_uninstall(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Plan removing the systemd user unit and confirm it, reversing exactly what install wrote."""
    del config
    runner = ctx.service_runner if ctx.service_runner is not None else service.DEFAULT_RUNNER
    changes = service.plan_uninstall_service(ctx.environ, runner)
    return confirm.apply_changes(
        changes,
        yes=args.yes,
        dry_run=args.dry_run,
        stdin=ctx.stdin,
        stdout=ctx.stdout,
        stderr=ctx.stderr,
        environ=ctx.environ,
    )


def _cmd_service_restart(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Restart the running systemd user unit via ``systemctl --user restart``.

    Unlike ``install``/``uninstall`` this does not edit the unit file, so it
    skips the diff-and-confirm flow — it only asks systemd to restart what is
    already installed. Safe to leave the running daemon serving right up to
    the restart because ``render_unit`` writes ``TimeoutStopSec``/
    ``KillMode=mixed``, giving ``systemctl``'s ``SIGTERM`` time to drain in
    place instead of killing the daemon mid-request.
    """
    del args
    runner = ctx.service_runner if ctx.service_runner is not None else service.DEFAULT_RUNNER
    service.restart_service(ctx.environ, runner)
    ctx.stdout.write("service restarted\n")
    return EXIT_OK


def _cmd_service_status(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Print the systemd unit's present/enabled/active state and the daemon health check."""
    del args
    query = ctx.service_query if ctx.service_query is not None else service.DEFAULT_QUERY
    status = service.service_status(ctx.environ, query)
    if not status.present:
        ctx.stdout.write("service: not installed; run `cgx service install`\n")
    else:
        port = service.installed_port(ctx.environ)
        port_desc = (
            str(port) if port is not None else "unset (daemon falls back to its own default)"
        )
        ctx.stdout.write(
            f"service: unit present at {status.unit_path}, "
            f"enabled={status.enabled}, active={status.active}, port={port_desc}\n"
        )
    client = ctx.client_factory(config.url)
    health = check_daemon(client, config.url)
    ctx.stdout.write(f"{health.status}: {health.message}\n")
    return EXIT_OK if health.status != "fail" else EXIT_FAILED


#: ``cgx service`` manages a systemd user unit, so it is Linux-only; printed
#: verbatim (as one line) by :func:`_cmd_service` on every other platform,
#: pointing at the two commands that replace it (see README's "Windows and
#: macOS" section).
_SERVICE_LINUX_ONLY_MSG = (
    "cgx service manages a systemd user unit and is Linux-only; on this "
    "platform run `cgx serve --port <N>` under your own process manager, "
    "then `cgx configure --port <N>`\n"
)


def _cmd_service(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Dispatch a ``cgx service`` subcommand by name.

    Refuses every subcommand on a non-Linux ``ctx.platform`` instead of
    reaching the individual handlers, which assume a systemd user session.
    """
    if ctx.platform != "linux":
        ctx.stderr.write(_SERVICE_LINUX_ONLY_MSG)
        return EXIT_FAILED
    handler = _SERVICE_HANDLERS[args.service_command]
    return handler(args, config, ctx)


def _cmd_doctor(args: argparse.Namespace, config: Config, ctx: _Context) -> int:
    """Run the doctor checks."""
    del args
    return run_doctor(
        config,
        ctx.environ,
        ctx.cwd,
        ctx.stdout,
        client_factory=ctx.client_factory,
        platform=ctx.platform,
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build the ``cgx`` argument parser with every subcommand."""
    parser = argparse.ArgumentParser(
        prog="cgx", description="Keep a Claude Code session's prompt cache warm."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    _PORT_HELP = "override CGX_PORT (default: env or 8787)"
    _HOST_HELP = "override CGX_HOST (default: env or 127.0.0.1)"

    serve_parser = subparsers.add_parser("serve", help="run the daemon")
    serve_parser.add_argument("--port", type=int, default=None, help=_PORT_HELP)
    serve_parser.add_argument("--host", default=None, help=_HOST_HELP)
    serve_parser.add_argument(
        "--handover",
        action="store_true",
        help="bind alongside an already-running daemon (SO_REUSEPORT) and signal it "
        "to drain and exit once this process is healthy; for upgrading without systemd",
    )

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
            p.add_argument(
                "--rewarm",
                action="store_true",
                help="tolerate exactly one cache-write bump instead of stopping on it",
            )

    add_on_off("on")
    add_on_off("off")

    now_parser = subparsers.add_parser("now", help="bump a session immediately")
    now_parser.add_argument("--session", help="session id (default: $CLAUDE_SESSION_ID)")

    forget_parser = subparsers.add_parser("forget", help="forget a session")
    forget_parser.add_argument("--session", help="session id (default: $CLAUDE_SESSION_ID)")

    hook_parser = subparsers.add_parser("hook", help="Claude Code hook entry points")
    hook_parser.add_argument("hook_command", choices=["user-prompt-submit"])

    shadow_fork_parser = subparsers.add_parser(
        "shadow-fork",
        help="review a hook event via a shadow fork and map its verdict to the hook's output",
    )
    shadow_fork_parser.add_argument(
        "--template", required=True, help="template name to render (user dir, then packaged)"
    )
    shadow_fork_parser.add_argument(
        "--prefix",
        default=None,
        help="only act when the payload's prompt starts with this text (e.g. /btw)",
    )
    shadow_fork_parser.add_argument(
        "--session", default=None, help="override the hook payload's session_id"
    )
    shadow_fork_parser.add_argument(
        "--model",
        default=None,
        help=(
            "override the reviewer model for this fork only (default: "
            "CGX_SHADOW_FORK_MODEL, else the session's own model); an override "
            "always forfeits the cache read"
        ),
    )
    shadow_fork_parser.add_argument(
        "--apply",
        default=None,
        help=(
            "on an intervene verdict, run this shell command with the verdict "
            "JSON on its stdin, for a side effect the conversation never sees "
            "(e.g. a session rename); your own string, run through the shell "
            "exactly as given"
        ),
    )

    subparsers.add_parser("statusline", help="print a statusline segment")

    _SCOPE_HELP = (
        "chooses which Claude Code settings.json is edited (user, project or "
        "local); the daemon is per user, see `cgx service`; managed is not "
        "supported (root-owned, machine-wide, conflicts with per-user daemons)"
    )
    _YES_HELP = "apply every change without prompting (for scripts)"

    configure_parser = subparsers.add_parser(
        "configure", help="write the Claude Code integration, confirming each file"
    )
    configure_parser.add_argument(
        "--scope", choices=["user", "project", "local"], default="user", help=_SCOPE_HELP
    )
    configure_parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    configure_parser.add_argument("--yes", "-y", action="store_true", help=_YES_HELP)
    configure_parser.add_argument(
        "--force", action="store_true", help="override a conflicting ANTHROPIC_BASE_URL"
    )
    configure_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="daemon port to point ANTHROPIC_BASE_URL at "
        "(default: CGX_PORT/CGX_URL, then the installed service's port, then 8787)",
    )
    configure_parser.add_argument("--service", action="store_true", help=argparse.SUPPRESS)
    configure_parser.add_argument(
        "--shadow-fork",
        action="append",
        dest="shadow_fork",
        metavar="NAME[:PREFIX]",
        default=None,
        help=(
            "wire a `cgx shadow-fork --template NAME` hook, using the "
            "template's own `event`/`matcher` front matter to place it; "
            "repeatable. PREFIX restricts a UserPromptSubmit template to "
            "prompts starting with it, e.g. `btw:/btw`"
        ),
    )

    unconfigure_parser = subparsers.add_parser(
        "unconfigure", help="remove the Claude Code integration, confirming each file"
    )
    unconfigure_parser.add_argument(
        "--scope", choices=["user", "project", "local"], default="user", help=_SCOPE_HELP
    )
    unconfigure_parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    unconfigure_parser.add_argument("--yes", "-y", action="store_true", help=_YES_HELP)
    unconfigure_parser.add_argument("--service", action="store_true", help=argparse.SUPPRESS)
    unconfigure_parser.add_argument(
        "--shadow-fork",
        action="append",
        dest="shadow_fork",
        metavar="NAME[:PREFIX]",
        default=None,
        help="remove exactly the hook entry the matching `configure --shadow-fork` added",
    )

    subparsers.add_parser("doctor", help="check the environment")

    service_parser = subparsers.add_parser(
        "service", help="manage the systemd user service running the daemon"
    )
    service_subparsers = service_parser.add_subparsers(dest="service_command", required=True)
    service_install_parser = service_subparsers.add_parser(
        "install", help="write and enable the systemd user unit, confirming the diff"
    )
    service_install_parser.add_argument("--port", type=int, default=None, help=_PORT_HELP)
    service_install_parser.add_argument("--host", default=None, help=_HOST_HELP)
    service_install_parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    service_install_parser.add_argument("--yes", "-y", action="store_true", help=_YES_HELP)
    service_uninstall_parser = service_subparsers.add_parser(
        "uninstall", help="disable and remove the systemd user unit, confirming the diff"
    )
    service_uninstall_parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    service_uninstall_parser.add_argument("--yes", "-y", action="store_true", help=_YES_HELP)
    service_subparsers.add_parser("status", help="show the unit state and daemon health")
    service_subparsers.add_parser(
        "restart", help="restart the daemon via systemd (drains in place, no unit edit)"
    )

    return parser


_HANDLERS: dict[str, Callable[[argparse.Namespace, Config, _Context], int]] = {
    "serve": _cmd_serve,
    "status": _cmd_status,
    "on": lambda args, config, ctx: _cmd_set_enabled(args, config, ctx, True),
    "off": lambda args, config, ctx: _cmd_set_enabled(args, config, ctx, False),
    "now": _cmd_now,
    "forget": _cmd_forget,
    "hook": _cmd_hook,
    "shadow-fork": _cmd_shadow_fork,
    "statusline": _cmd_statusline,
    "configure": _cmd_configure,
    "unconfigure": _cmd_unconfigure,
    "doctor": _cmd_doctor,
    "service": _cmd_service,
}

_SERVICE_HANDLERS: dict[str, Callable[[argparse.Namespace, Config, _Context], int]] = {
    "install": _cmd_service_install,
    "uninstall": _cmd_service_uninstall,
    "status": _cmd_service_status,
    "restart": _cmd_service_restart,
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
    platform: str = sys.platform,
) -> int:
    """Parse ``argv`` and dispatch to the matching subcommand handler.

    Every ambient dependency (environment, stdio, working directory, the
    control API client, the systemd runner, ``sys.platform``) defaults to
    the real thing and can be overridden, which is how tests exercise this
    function directly instead of shelling out to a subprocess or actually
    running on a different OS. ``platform`` gates ``cgx service`` and the
    systemd check in ``cgx doctor``, both Linux-only.
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

    ctx = _Context(
        env, inp, out, err, directory, client_factory, service_runner, service_query, platform
    )

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
