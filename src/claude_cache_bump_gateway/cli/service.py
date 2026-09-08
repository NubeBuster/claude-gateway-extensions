"""Manage the systemd user unit that runs the daemon (``ccbg serve``).

One daemon serves every project on a machine, so this is deliberately
separate from ``ccbg install``'s per-scope ``settings.json`` edits (see
``cli/install.py``'s module docstring and ``docs/DESIGN.md`` "Claude Code
integration"). :func:`install_service`/:func:`uninstall_service` write and
remove exactly the unit file this module renders; :func:`service_status`
reports its present/enabled/active state without touching ``settings.json``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from claude_cache_bump_gateway.cli.install import user_home

_UNIT_NAME = "claude-cache-bump-gateway.service"

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


def _ccbg_executable() -> str:
    """Return the absolute path of the running ``ccbg`` executable."""
    found = shutil.which("ccbg")
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

    Passes through every ``CCBG_*`` variable currently set in ``environ`` as
    an ``Environment=`` line, so the service sees the same configuration the
    installing shell had (documented in the README's "Daemon settings"
    section). ``port``/``host``, when given, are written explicitly as
    ``CCBG_PORT``/``CCBG_HOST``, overriding whatever those two happened to
    be in the installing shell — the whole point of ``--port``/``--host`` is
    to pin the unit's port independent of shell state.
    """
    env_vars = {key: value for key, value in environ.items() if key.startswith("CCBG_")}
    if port is not None:
        env_vars["CCBG_PORT"] = str(port)
    if host is not None:
        env_vars["CCBG_HOST"] = host
    env_lines = [f"Environment={key}={value}" for key, value in sorted(env_vars.items())]
    lines = [
        "[Unit]",
        "Description=claude-cache-bump-gateway",
        "",
        "[Service]",
        f"ExecStart={executable} serve",
        *env_lines,
        "Restart=on-failure",
        "RestartSec=5",
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
    ``CCBG_PORT``/``CCBG_HOST`` explicitly.
    """
    executable = _ccbg_executable()
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
    """Return the ``CCBG_PORT`` baked into the installed unit file, or ``None``.

    ``None`` covers both "no unit installed" and "the unit has no explicit
    ``CCBG_PORT`` line" (the daemon would then fall back to its own
    default/environment at start time, which this function cannot see).
    """
    path = unit_path(environ)
    if not path.is_file():
        return None
    prefix = "Environment=CCBG_PORT="
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
    "render_unit",
    "service_status",
    "uninstall_service",
    "unit_path",
]
