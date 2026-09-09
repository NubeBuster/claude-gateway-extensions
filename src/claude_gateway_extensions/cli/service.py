"""Manage the systemd user unit that runs the daemon (``cgx serve``).

One daemon serves every project on a machine, so this is deliberately
separate from ``cgx configure``'s per-scope ``settings.json`` edits (see
``cli/install.py``'s module docstring and ``docs/DESIGN.md`` "Claude Code
integration"). :func:`install_service`/:func:`uninstall_service` write and
remove exactly the unit file this module renders; :func:`service_status`
reports its present/enabled/active state without touching ``settings.json``.

A bad unit edit breaks the live daemon just as surely as a bad
``settings.json`` edit breaks a live session, so ``cgx service
install``/``uninstall`` plan the unit change first
(:func:`plan_install_service`/:func:`plan_uninstall_service`) and go
through the same colored-diff-and-confirm flow as ``cgx
configure``/``unconfigure`` — see :mod:`claude_gateway_extensions.cli.confirm`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from claude_gateway_extensions.cli.confirm import PlannedChange
from claude_gateway_extensions.cli.install import user_home
from claude_gateway_extensions.config import DAEMON_ENV_VARS, DEFAULT_DRAIN_TIMEOUT_S

#: Slack systemd gives the daemon beyond its own drain timeout before
#: escalating from SIGTERM to SIGKILL (``TimeoutStopSec``), so a drain that
#: legitimately uses its full ``CGX_DRAIN_TIMEOUT_S`` budget is never killed
#: mid-flush by systemd racing ahead of the daemon's own bound.
_STOP_TIMEOUT_SLACK_S = 30

_UNIT_NAME = "claude-gateway-extensions.service"

#: A runner executes a systemd command that changes state (enable/disable);
#: tests inject a fake instead of the real ``subprocess.run`` so service
#: tests never touch the real systemd.
Runner = Callable[[Sequence[str]], None]

#: A query runs a systemd command that reports state (``is-active``,
#: ``is-enabled``) and returns its stripped stdout; tests inject a fake for
#: the same reason as :data:`Runner`.
Query = Callable[[Sequence[str]], str]


def _default_runner(args: Sequence[str]) -> None:
    """Run a systemd command for real, raising on a non-zero exit."""
    subprocess.run(args, check=True)


#: The real runner, for callers (e.g. the CLI) that need to name the default
#: explicitly instead of relying on a function's default argument value.
DEFAULT_RUNNER: Runner = _default_runner


def _default_query(args: Sequence[str]) -> str:
    """Run a systemd query command for real, returning its stripped stdout.

    Never raises on a non-zero exit: ``systemctl --user is-active``/
    ``is-enabled`` use the exit code to mean "no"/"disabled", but still
    print the human-readable answer to stdout, which is what callers
    compare against.
    """
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    return result.stdout.strip()


#: The real query, named explicitly for the same reason as :data:`DEFAULT_RUNNER`.
DEFAULT_QUERY: Query = _default_query


def _cgx_executable() -> str:
    """Return the absolute path of the running ``cgx`` executable."""
    found = shutil.which("cgx")
    if found:
        return str(Path(found).resolve())
    return str(Path(sys.argv[0]).resolve())


def unit_path(environ: Mapping[str, str]) -> Path:
    """Return the systemd user unit path for the daemon service."""
    xdg_config_home = environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config_home) if xdg_config_home else user_home(environ) / ".config"
    return base / "systemd" / "user" / _UNIT_NAME


def render_unit(
    executable: str,
    environ: Mapping[str, str],
    *,
    port: int | None = None,
    host: str | None = None,
) -> str:
    """Render the systemd user unit file content for ``executable``.

    Passes through every variable in :data:`~claude_gateway_extensions.config.DAEMON_ENV_VARS`
    currently set in ``environ`` as an ``Environment=`` line, so the service
    sees the same daemon-side configuration the installing shell had
    (documented in the README's "Daemon Environment Variables" section) --
    never a session/hook-side ``CGX_*`` variable (``CGX_URL``,
    ``CGX_FORK_REVIEW_QUIET``, ``CGX_TEMPLATE_DIR``, ...) that only happens
    to be set in that shell and that ``cgx serve`` never reads.
    ``port``/``host``, when given, are written explicitly as
    ``CGX_PORT``/``CGX_HOST``, overriding whatever those two happened to
    be in the installing shell — the whole point of ``--port``/``--host`` is
    to pin the unit's port independent of shell state.

    ``TimeoutStopSec`` is set to ``CGX_DRAIN_TIMEOUT_S`` (from ``environ``, or
    the daemon's own default) plus :data:`_STOP_TIMEOUT_SLACK_S`, and
    ``KillMode=mixed`` sends ``systemctl --user stop``'s ``SIGTERM`` to only
    the daemon's own process — giving it the same drain-then-exit path as an
    interactive ``Ctrl-C`` instead of systemd escalating to ``SIGKILL``
    before the daemon finishes draining. See the "Drain plus
    ``SO_REUSEPORT``..." decision in ``docs/DESIGN.md`` "Decisions".
    """
    env_vars = {key: value for key, value in environ.items() if key in DAEMON_ENV_VARS}
    if port is not None:
        env_vars["CGX_PORT"] = str(port)
    if host is not None:
        env_vars["CGX_HOST"] = host
    env_lines = [f"Environment={key}={value}" for key, value in sorted(env_vars.items())]
    try:
        drain_timeout = int(env_vars.get("CGX_DRAIN_TIMEOUT_S", DEFAULT_DRAIN_TIMEOUT_S))
    except ValueError:
        drain_timeout = DEFAULT_DRAIN_TIMEOUT_S
    lines = [
        "[Unit]",
        "Description=claude-gateway-extensions",
        "",
        "[Service]",
        f"ExecStart={executable} serve",
        *env_lines,
        "Restart=on-failure",
        "RestartSec=5",
        f"TimeoutStopSec={drain_timeout + _STOP_TIMEOUT_SLACK_S}",
        "KillMode=mixed",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def install_service(
    environ: Mapping[str, str],
    runner: Runner = _default_runner,
    *,
    port: int | None = None,
    host: str | None = None,
) -> Path:
    """Write the systemd user unit and enable it now; return the unit path.

    ``port``/``host`` are forwarded to :func:`render_unit` to pin the unit's
    ``CGX_PORT``/``CGX_HOST`` explicitly.
    """
    executable = _cgx_executable()
    path = unit_path(environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_unit(executable, environ, port=port, host=host))
    runner(["systemctl", "--user", "daemon-reload"])
    runner(["systemctl", "--user", "enable", "--now", _UNIT_NAME])
    return path


def uninstall_service(environ: Mapping[str, str], runner: Runner = _default_runner) -> None:
    """Disable and remove the systemd user unit, if present."""
    path = unit_path(environ)
    if not path.is_file():
        return
    runner(["systemctl", "--user", "disable", "--now", _UNIT_NAME])
    path.unlink()
    runner(["systemctl", "--user", "daemon-reload"])


def restart_service(environ: Mapping[str, str], runner: Runner = _default_runner) -> None:
    """Restart the systemd user unit in place via ``systemctl --user restart``.

    Does not touch the unit file — that is what ``cgx service install`` is
    for. Relies on ``render_unit``'s ``TimeoutStopSec``/``KillMode=mixed``
    (written by the last ``install``) for the stop half of the restart to
    drain in-flight requests rather than killing the daemon mid-request; an
    old unit installed before those directives existed restarts the same way
    systemd always restarted it (no drain), until ``cgx service install`` is
    re-run to pick them up.
    """
    del environ  # unused; kept for signature symmetry with install/uninstall
    runner(["systemctl", "--user", "restart", _UNIT_NAME])


def plan_install_service(
    environ: Mapping[str, str],
    runner: Runner = _default_runner,
    *,
    port: int | None = None,
    host: str | None = None,
) -> PlannedChange:
    """Compute the systemd unit change :func:`install_service` would make.

    Nothing is written; ``apply`` calls :func:`install_service` for real
    (write, then ``daemon-reload`` and ``enable --now``) on confirmation.
    The caller (``cgx service install``) shows the returned
    :class:`PlannedChange` as a diff and confirms it before calling ``apply``.
    """
    executable = _cgx_executable()
    path = unit_path(environ)
    new_content = render_unit(executable, environ, port=port, host=host)
    old_content = path.read_text() if path.is_file() else None

    def _apply() -> None:
        install_service(environ, runner, port=port, host=host)

    return PlannedChange(path, old_content, new_content, _apply)


def plan_uninstall_service(
    environ: Mapping[str, str], runner: Runner = _default_runner
) -> list[PlannedChange]:
    """Compute the systemd unit removal :func:`uninstall_service` would make.

    Omitted entirely (empty list) if no unit is installed: uninstalling
    must never write anything. ``apply`` calls :func:`uninstall_service`
    for real on confirmation.
    """
    path = unit_path(environ)
    if not path.is_file():
        return []
    return [PlannedChange(path, path.read_text(), None, lambda: uninstall_service(environ, runner))]


@dataclass
class ServiceStatus:
    """Present/enabled/active state of the systemd user unit.

    ``enabled``/``active`` are ``None`` when ``present`` is ``False``: an
    absent unit was never queried, rather than queried and found disabled.
    """

    unit_path: Path
    present: bool
    enabled: bool | None
    active: bool | None


def service_status(environ: Mapping[str, str], query: Query = _default_query) -> ServiceStatus:
    """Report whether the unit file exists and, if so, its enabled/active state."""
    path = unit_path(environ)
    if not path.is_file():
        return ServiceStatus(path, False, None, None)
    enabled = query(["systemctl", "--user", "is-enabled", _UNIT_NAME]) == "enabled"
    active = query(["systemctl", "--user", "is-active", _UNIT_NAME]) == "active"
    return ServiceStatus(path, True, enabled, active)


def installed_port(environ: Mapping[str, str]) -> int | None:
    """Return the ``CGX_PORT`` baked into the installed unit file, or ``None``.

    ``None`` covers both "no unit installed" and "the unit has no explicit
    ``CGX_PORT`` line" (the daemon would then fall back to its own
    default/environment at start time, which this function cannot see).
    """
    path = unit_path(environ)
    if not path.is_file():
        return None
    prefix = "Environment=CGX_PORT="
    for line in path.read_text().splitlines():
        if line.startswith(prefix):
            try:
                return int(line[len(prefix) :])
            except ValueError:
                return None
    return None


__all__ = [
    "DEFAULT_QUERY",
    "DEFAULT_RUNNER",
    "Query",
    "Runner",
    "ServiceStatus",
    "install_service",
    "installed_port",
    "plan_install_service",
    "plan_uninstall_service",
    "render_unit",
    "restart_service",
    "service_status",
    "uninstall_service",
    "unit_path",
]
