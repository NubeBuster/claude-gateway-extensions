"""Daemon configuration, parsed once from ``CGX_*`` environment variables.

``Config`` is the single source of truth for every tunable, per the
"Environment variables only, split by process" decision in ``docs/DESIGN.md``
"Decisions". It is a frozen dataclass built by
:meth:`Config.from_env`; nothing else in this codebase reads ``os.environ``
directly.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

OnCacheWrite = Literal["stop", "continue"]

_TRUE_VALUES = {"true", "1", "yes"}
_FALSE_VALUES = {"false", "0", "no"}

#: Default ``CGX_DRAIN_TIMEOUT_S``, shared with ``cli/service.py`` so the
#: systemd unit's ``TimeoutStopSec`` (drain timeout + 30s) agrees with the
#: daemon's own default even when the installing shell never set the
#: variable explicitly.
DEFAULT_DRAIN_TIMEOUT_S = 600

#: Every ``CGX_*`` variable :meth:`Config.from_env` reads -- the daemon-side
#: half of the "Environment variables only, split by process" decision in
#: ``docs/DESIGN.md``. The single source ``cli/service.py``'s
#: ``render_unit`` filters against, so a systemd unit only ever gets
#: variables ``cgx serve`` actually reads, never session/hook-side ones
#: (``CGX_URL``, ``CGX_SHADOW_FORK_QUIET``, ``CGX_TEMPLATE_DIR``, ...) that
#: happened to be set in the installing shell. Keep in sync with
#: :meth:`Config.from_env` by hand -- there is no dataclass-field
#: introspection here, since a field's name does not mechanically produce
#: its ``CGX_*`` spelling.
DAEMON_ENV_VARS: tuple[str, ...] = (
    "CGX_HOST",
    "CGX_PORT",
    "CGX_UPSTREAM",
    "CGX_STATE_DIR",
    "CGX_PERSIST",
    "CGX_DEFAULT_ENABLED",
    "CGX_INTERVAL_1H_S",
    "CGX_INTERVAL_5M_S",
    "CGX_MAX_DURATION_S",
    "CGX_ON_CACHE_WRITE",
    "CGX_MAX_STREAMS_PER_SESSION",
    "CGX_LOG_LEVEL",
    "CGX_DRAIN_TIMEOUT_S",
    "CGX_SHADOW_FORK_MAX_TOKENS",
    "CGX_SHADOW_FORK_MAX_PER_MIN",
    "CGX_SHADOW_FORK_THINKING_GUARD",
    "CGX_SHADOW_FORK_MODEL",
    "CGX_CONFIG_FILE",
)

#: The four keys :func:`parse_shadow_fork_file` accepts in the ``[shadow_fork]``
#: table -- any other key in that table is a rejecting error (see
#: "Shadow Fork tuning is hot reloaded..." in ``docs/DESIGN.md``).
_SHADOW_FORK_FILE_KEYS = frozenset({"max_tokens", "max_per_min", "thinking_guard", "model"})

#: ``CGX_*`` names whose value in the file, when present, wins over both the
#: environment and the built-in default for that key -- keyed the same as
#: :class:`ShadowForkSettings`'s fields, used by ``server/control.py`` to
#: report each effective value's source.
SHADOW_FORK_ENV_VARS: Mapping[str, str] = {
    "max_tokens": "CGX_SHADOW_FORK_MAX_TOKENS",
    "max_per_min": "CGX_SHADOW_FORK_MAX_PER_MIN",
    "thinking_guard": "CGX_SHADOW_FORK_THINKING_GUARD",
    "model": "CGX_SHADOW_FORK_MODEL",
}


class ConfigError(ValueError):
    """Raised when a ``CGX_*`` environment variable has an invalid value."""


def resolve_home(env: Mapping[str, str]) -> Path:
    """Return the user's home directory, honouring ``env`` over the real process environment.

    Checks ``HOME`` then ``USERPROFILE`` (the Windows equivalent) in
    ``env``, so callers stay testable via their own ``environ`` mapping
    without touching ``os.environ``. Falls back to :meth:`Path.home` (which
    *does* read the real process environment) only when neither is set in
    ``env`` — the same fallback :func:`_default_state_dir` and
    ``cli/install.py``'s ``user_home`` share.
    """
    home = env.get("HOME") or env.get("USERPROFILE")
    return Path(home) if home else Path.home()


def _default_state_dir(env: Mapping[str, str]) -> Path:
    """Return the state directory default for this daemon's persistence root.

    Precedence: ``XDG_STATE_HOME`` (an explicit override, honoured on every
    platform) then, on Windows, ``LOCALAPPDATA`` (presence in ``env`` is
    what signals Windows here — this module reads only its ``environ``
    mapping, never ``sys.platform``, so the Windows branch stays testable
    from any OS) then the POSIX/XDG default ``~/.local/state`` under
    :func:`resolve_home`.
    """
    xdg_state_home = env.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / "claude-gateway-extensions"
    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "claude-gateway-extensions"
    return resolve_home(env) / ".local" / "state" / "claude-gateway-extensions"


def resolve_config_file(env: Mapping[str, str]) -> Path:
    r"""Return the Shadow Fork tuning file's path, following ``_template_dir``'s env-only style.

    Precedence: ``CGX_CONFIG_FILE`` if set, then ``XDG_CONFIG_HOME``, then
    (Windows signalled by ``APPDATA`` being present in ``env``, never by
    ``sys.platform``) ``%APPDATA%\cgx\config.toml``, then the POSIX/XDG
    default ``~/.config/cgx/config.toml`` under :func:`resolve_home` -- the
    same ``cgx`` directory ``cli/shadow_fork.py``'s ``_template_dir`` resolves
    templates under. See the "Shadow Fork tuning is hot reloaded..." decision
    in ``docs/DESIGN.md``.
    """
    override = env.get("CGX_CONFIG_FILE")
    if override:
        return Path(override).expanduser()
    xdg_config_home = env.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / "cgx" / "config.toml"
    appdata = env.get("APPDATA")
    if appdata:
        return Path(appdata) / "cgx" / "config.toml"
    return resolve_home(env) / ".config" / "cgx" / "config.toml"


@dataclass(frozen=True)
class ShadowForkSettings:
    """The four Shadow Fork keys as read from ``[shadow_fork]`` in ``CGX_CONFIG_FILE``.

    Every field is ``None`` when the file omits it (or the file has never
    been loaded), meaning "fall through to the environment/default" per the
    precedence rule in the "Shadow Fork tuning is hot reloaded..." decision
    in ``docs/DESIGN.md``.
    """

    max_tokens: int | None = None
    max_per_min: int | None = None
    thinking_guard: bool | None = None
    model: str | None = None


def parse_shadow_fork_file(path: Path) -> ShadowForkSettings:
    """Parse ``path``'s ``[shadow_fork]`` table into a :class:`ShadowForkSettings`.

    Raises :class:`ConfigError` on a syntax error, an unknown key inside
    ``[shadow_fork]``, a ``[shadow_fork]`` that is not a table, or a value of
    the wrong type -- the whole file is rejected as one unit, per the
    "applies atomically or not at all" rule in ``docs/DESIGN.md``. A missing
    ``[shadow_fork]`` table (or a key it omits) resolves to ``None`` for that
    field, same as an entirely absent file.
    """
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read file: {exc}") from exc

    table = data.get("shadow_fork", {})
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: [shadow_fork] must be a table")

    unknown = set(table) - _SHADOW_FORK_FILE_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown key(s) in [shadow_fork]: {', '.join(sorted(unknown))}")

    max_tokens = table.get("max_tokens")
    if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int)):
        raise ConfigError(f"{path}: shadow_fork.max_tokens must be an integer")
    max_per_min = table.get("max_per_min")
    if max_per_min is not None and (
        isinstance(max_per_min, bool) or not isinstance(max_per_min, int)
    ):
        raise ConfigError(f"{path}: shadow_fork.max_per_min must be an integer")
    thinking_guard = table.get("thinking_guard")
    if thinking_guard is not None and not isinstance(thinking_guard, bool):
        raise ConfigError(f"{path}: shadow_fork.thinking_guard must be a boolean")
    model = table.get("model")
    if model is not None and not isinstance(model, str):
        raise ConfigError(f"{path}: shadow_fork.model must be a string")

    return ShadowForkSettings(
        max_tokens=max_tokens,
        max_per_min=max_per_min,
        thinking_guard=thinking_guard,
        model=model,
    )


@dataclass(frozen=True)
class ConfigFileState:
    """A :class:`ConfigFileWatcher` snapshot: resolved path, load status and last-good settings.

    ``settings`` holds the last successfully parsed :class:`ShadowForkSettings`
    (all-``None`` if the file has never loaded, whether because it is
    missing or because every attempt so far has failed to parse).
    """

    path: Path
    present: bool
    parse_error: str | None
    settings: ShadowForkSettings


class ConfigFileWatcher:
    """Re-reads a Shadow Fork tuning file only when its ``stat`` changes.

    :meth:`current` is cheap to call on every fork request and every ``GET
    /cgx/config`` (see the "checked at most once per fork request..." rule in
    ``docs/DESIGN.md``): it always ``stat``s the file, but only re-parses it
    when ``(mtime, size, inode)`` differs from the last check. A file that
    fails to parse keeps the previously loaded settings (or all-``None`` if
    none ever loaded) and logs one WARNING naming the file and the error,
    exactly once per distinct ``stat`` -- a file that never changes again
    never re-logs. A file that disappears resolves to all-``None`` settings
    (the environment) rather than keeping the last-good ones: "missing" and
    "present but invalid" are deliberately different outcomes.
    """

    def __init__(self, path: Path) -> None:
        """Watch ``path``; nothing is read until the first :meth:`current` call."""
        self._path = path
        self._stat_key: tuple[int, int, int] | None = None
        self._settings = ShadowForkSettings()
        self._parse_error: str | None = None

    def current(self) -> ConfigFileState:
        """Return the file's current load state, re-parsing only if its ``stat`` changed."""
        try:
            st = self._path.stat()
        except OSError:
            self._stat_key = None
            self._parse_error = None
            self._settings = ShadowForkSettings()
            return ConfigFileState(self._path, False, None, self._settings)

        stat_key = (int(st.st_mtime_ns), st.st_size, st.st_ino)
        if stat_key == self._stat_key:
            return ConfigFileState(self._path, True, self._parse_error, self._settings)

        self._stat_key = stat_key
        try:
            self._settings = parse_shadow_fork_file(self._path)
        except ConfigError as exc:
            logger.warning("%s", exc)
            self._parse_error = str(exc)
        else:
            self._parse_error = None
        return ConfigFileState(self._path, True, self._parse_error, self._settings)


def merge_shadow_fork(config: Config, file_settings: ShadowForkSettings) -> ShadowForkSettings:
    """Merge ``file_settings`` over ``config``'s env/default values, file winning per key.

    Every field of the result is non-``None`` (the config's own fields always
    have a value): callers that need the resolved values without a fork
    request's own override use this directly; :meth:`Scheduler.fork_now`
    applies the request field on top.
    """
    return ShadowForkSettings(
        max_tokens=(
            file_settings.max_tokens
            if file_settings.max_tokens is not None
            else config.shadow_fork_max_tokens
        ),
        max_per_min=(
            file_settings.max_per_min
            if file_settings.max_per_min is not None
            else config.shadow_fork_max_per_min
        ),
        thinking_guard=(
            file_settings.thinking_guard
            if file_settings.thinking_guard is not None
            else config.shadow_fork_thinking_guard
        ),
        model=(
            file_settings.model if file_settings.model is not None else config.shadow_fork_model
        ),
    )


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
    drain_timeout_s: int
    shadow_fork_max_tokens: int
    shadow_fork_max_per_min: int
    shadow_fork_thinking_guard: bool
    shadow_fork_model: str | None
    config_file: Path
    configured_env_vars: frozenset[str]

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
        drain_timeout_s = _parse_int(
            "CGX_DRAIN_TIMEOUT_S",
            env.get("CGX_DRAIN_TIMEOUT_S", str(DEFAULT_DRAIN_TIMEOUT_S)),
        )
        shadow_fork_max_tokens = _parse_int(
            "CGX_SHADOW_FORK_MAX_TOKENS", env.get("CGX_SHADOW_FORK_MAX_TOKENS", "512")
        )
        shadow_fork_max_per_min = _parse_int(
            "CGX_SHADOW_FORK_MAX_PER_MIN", env.get("CGX_SHADOW_FORK_MAX_PER_MIN", "6")
        )
        shadow_fork_thinking_guard = _parse_bool(
            "CGX_SHADOW_FORK_THINKING_GUARD", env.get("CGX_SHADOW_FORK_THINKING_GUARD", "true")
        )
        shadow_fork_model = env.get("CGX_SHADOW_FORK_MODEL") or None
        config_file = resolve_config_file(env)
        configured_env_vars = frozenset(name for name in DAEMON_ENV_VARS if env.get(name))

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
            drain_timeout_s=drain_timeout_s,
            shadow_fork_max_tokens=shadow_fork_max_tokens,
            shadow_fork_max_per_min=shadow_fork_max_per_min,
            shadow_fork_thinking_guard=shadow_fork_thinking_guard,
            shadow_fork_model=shadow_fork_model,
            config_file=config_file,
            configured_env_vars=configured_env_vars,
        )

    def is_local_host(self) -> bool:
        """Return whether ``host`` reaches this machine only (localhost or a loopback IP)."""
        return _is_loopback_host(self.host)
