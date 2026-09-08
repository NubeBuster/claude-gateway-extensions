"""Daemon configuration, parsed once from ``CGX_*`` environment variables.

``Config`` is the single source of truth for every tunable, per the
"Environment variables only, split by process" decision in ``docs/DESIGN.md``
"Decisions". It is a frozen dataclass built by
:meth:`Config.from_env`; nothing else in this codebase reads ``os.environ``
directly.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

OnCacheWrite = Literal["stop", "continue"]

_TRUE_VALUES = {"true", "1", "yes"}
_FALSE_VALUES = {"false", "0", "no"}


class ConfigError(ValueError):
    """Raised when a ``CGX_*`` environment variable has an invalid value."""


def _default_state_dir(env: Mapping[str, str]) -> Path:
    """Return the XDG state directory default for this daemon's persistence root.

    Reads ``XDG_STATE_HOME`` and ``HOME`` from ``env`` (not ``os.environ``
    directly) so :meth:`Config.from_env` is fully testable via its
    ``environ`` argument.
    """
    xdg_state_home = env.get("XDG_STATE_HOME")
    if xdg_state_home:
        base = Path(xdg_state_home)
    else:
        home = env.get("HOME")
        base = (Path(home) if home else Path.home()) / ".local" / "state"
    return base / "claude-gateway-extensions"


def _parse_bool(name: str, raw: str) -> bool:
    """Parse a ``CGX_*`` boolean value, accepting true/false/1/0/yes/no case-insensitively."""
    lowered = raw.strip().lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ConfigError(f"{name}: invalid boolean value {raw!r}")


def _parse_int(name: str, raw: str) -> int:
    """Parse a ``CGX_*`` integer value, raising ``ConfigError`` on failure."""
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: invalid integer value {raw!r}") from exc


def _is_loopback_host(host: str) -> bool:
    """Return whether ``host`` is ``localhost`` or a loopback IP literal.

    Accepts any address in the IPv4 ``127.0.0.0/8`` block or the IPv6
    loopback ``::1``, not just ``127.0.0.1``; surrounding brackets on an
    IPv6 literal (``[::1]``) are stripped before parsing. A value that is
    not ``localhost`` and does not parse as an IP address is treated as
    non-local (fail closed) rather than raising here.
    """
    if host == "localhost":
        return True
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _parse_on_cache_write(raw: str) -> OnCacheWrite:
    """Parse ``CGX_ON_CACHE_WRITE``, restricting the value to the two documented literals."""
    if raw not in ("stop", "continue"):
        raise ConfigError(f"CGX_ON_CACHE_WRITE: invalid value {raw!r}, expected stop or continue")
    return raw


def _is_loopback_http_url(url: str) -> bool:
    """Return whether ``url`` is an ``http://`` URL whose host is loopback.

    Used to decide whether an ambient ``ANTHROPIC_BASE_URL`` (which Claude
    Code sets for every hook/statusline invocation once ``cgx configure`` has
    run) is safe to treat as "this is the daemon" rather than some unrelated
    gateway.
    """
    parsed = urlsplit(url)
    return parsed.scheme == "http" and bool(parsed.hostname) and _is_loopback_host(parsed.hostname)


def _format_host_for_url(host: str) -> str:
    """Return ``host`` formatted for embedding in a URL, bracketing a bare IPv6 literal."""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _resolve_daemon_url(env: Mapping[str, str], host: str, port: int) -> str:
    """Resolve the URL clients (hook, statusline, CLI) use to reach the daemon.

    Precedence: ``CGX_URL`` if set, then ``ANTHROPIC_BASE_URL`` if it is a
    loopback ``http://`` URL (Claude Code passes it to every hook/statusline
    invocation, so this removes the need to also set ``CGX_URL`` in the
    common case; a non-loopback ``ANTHROPIC_BASE_URL`` is ignored since it is
    not this daemon), then ``http://{host}:{port}`` built from this same
    ``Config``'s resolved host/port.
    """
    cgx_url = env.get("CGX_URL")
    if cgx_url:
        return cgx_url
    base_url = env.get("ANTHROPIC_BASE_URL")
    if base_url and _is_loopback_http_url(base_url):
        return base_url
    return f"http://{_format_host_for_url(host)}:{port}"


@dataclass(frozen=True)
class Config:
    """Immutable daemon configuration.

    See ``README.md``'s Configuration table for the meaning and default of
    every field; the "Control API on ``/cgx/``..." decision in
    ``docs/DESIGN.md`` "Decisions" covers how ``host``/``port`` are used.
    """

    host: str
    port: int
    upstream: str
    state_dir: Path
    persist: bool
    default_enabled: bool
    interval_1h_s: int
    interval_5m_s: int
    max_duration_s: int
    on_cache_write: OnCacheWrite
    max_streams_per_session: int
    log_level: str
    url: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Config:
        """Build a :class:`Config` from ``CGX_*`` variables in ``environ``.

        Defaults to ``os.environ`` when ``environ`` is ``None``. Every
        variable is optional and has a default, so a user who sets nothing
        gets a working gateway. Raises :class:`ConfigError` on any
        malformed value.
        """
        env = os.environ if environ is None else environ

        host = env.get("CGX_HOST", "127.0.0.1")
        port = _parse_int("CGX_PORT", env.get("CGX_PORT", "8787"))
        upstream = env.get("CGX_UPSTREAM", "https://api.anthropic.com")
        state_dir_raw = env.get("CGX_STATE_DIR")
        state_dir = Path(state_dir_raw).expanduser() if state_dir_raw else _default_state_dir(env)
        persist = _parse_bool("CGX_PERSIST", env.get("CGX_PERSIST", "true"))
        default_enabled = _parse_bool(
            "CGX_DEFAULT_ENABLED", env.get("CGX_DEFAULT_ENABLED", "false")
        )
        interval_1h_s = _parse_int("CGX_INTERVAL_1H_S", env.get("CGX_INTERVAL_1H_S", "3000"))
        interval_5m_s = _parse_int("CGX_INTERVAL_5M_S", env.get("CGX_INTERVAL_5M_S", "240"))
        max_duration_s = _parse_int("CGX_MAX_DURATION_S", env.get("CGX_MAX_DURATION_S", "86400"))
        on_cache_write = _parse_on_cache_write(env.get("CGX_ON_CACHE_WRITE", "stop"))
        max_streams_per_session = _parse_int(
            "CGX_MAX_STREAMS_PER_SESSION", env.get("CGX_MAX_STREAMS_PER_SESSION", "8")
        )
        log_level = env.get("CGX_LOG_LEVEL", "INFO")
        url = _resolve_daemon_url(env, host, port)

        return cls(
            host=host,
            port=port,
            upstream=upstream,
            state_dir=state_dir,
            persist=persist,
            default_enabled=default_enabled,
            interval_1h_s=interval_1h_s,
            interval_5m_s=interval_5m_s,
            max_duration_s=max_duration_s,
            on_cache_write=on_cache_write,
            max_streams_per_session=max_streams_per_session,
            log_level=log_level,
            url=url,
        )

    def is_local_host(self) -> bool:
        """Return whether ``host`` reaches this machine only (localhost or a loopback IP)."""
        return _is_loopback_host(self.host)
