"""Tests for claude_gateway_extensions.config."""

import os
from pathlib import Path

import pytest

from claude_gateway_extensions.config import (
    Config,
    ConfigError,
    ConfigFileWatcher,
    ShadowForkSettings,
    merge_shadow_fork,
    parse_shadow_fork_file,
    resolve_config_file,
)


@pytest.mark.invariant(decision="Environment variables only, split by process")
def test_defaults() -> None:
    config = Config.from_env({})
    assert config.host == "127.0.0.1"
    assert config.port == 8787
    assert config.upstream == "https://api.anthropic.com"
    assert config.persist is True
    assert config.default_enabled is False
    assert config.interval_1h_s == 3000
    assert config.interval_5m_s == 240
    assert config.max_duration_s == 86400
    assert config.on_cache_write == "stop"
    assert config.max_streams_per_session == 8
    assert config.log_level == "INFO"
    assert config.url == "http://127.0.0.1:8787"
    assert config.shadow_fork_max_tokens == 512
    assert config.shadow_fork_max_per_min == 6


def test_fork_settings_from_env() -> None:
    config = Config.from_env(
        {"CGX_SHADOW_FORK_MAX_TOKENS": "256", "CGX_SHADOW_FORK_MAX_PER_MIN": "3"}
    )
    assert config.shadow_fork_max_tokens == 256
    assert config.shadow_fork_max_per_min == 3


def test_default_state_dir_uses_xdg_state_home() -> None:
    config = Config.from_env({"XDG_STATE_HOME": "/tmp/xdg-state"})
    assert config.state_dir == Path("/tmp/xdg-state/claude-gateway-extensions")


def test_default_state_dir_falls_back_to_home() -> None:
    config = Config.from_env({"HOME": "/home/someone"})
    assert config.state_dir == Path("/home/someone/.local/state/claude-gateway-extensions")


def test_explicit_state_dir_overrides_default() -> None:
    config = Config.from_env({"CGX_STATE_DIR": "/srv/cgx"})
    assert config.state_dir == Path("/srv/cgx")


def test_default_state_dir_uses_localappdata_on_windows() -> None:
    """``LOCALAPPDATA`` (a Windows-only env var) picks the Windows default, from any OS."""
    config = Config.from_env({"LOCALAPPDATA": "C:\\Users\\someone\\AppData\\Local"})
    assert config.state_dir == Path("C:\\Users\\someone\\AppData\\Local") / (
        "claude-gateway-extensions"
    )


def test_default_state_dir_prefers_xdg_state_home_over_localappdata() -> None:
    config = Config.from_env(
        {"XDG_STATE_HOME": "/tmp/xdg-state", "LOCALAPPDATA": "C:\\Users\\someone\\AppData\\Local"}
    )
    assert config.state_dir == Path("/tmp/xdg-state/claude-gateway-extensions")


def test_default_state_dir_falls_back_to_userprofile_without_home() -> None:
    config = Config.from_env({"USERPROFILE": "C:\\Users\\someone"})
    assert config.state_dir == Path("C:\\Users\\someone") / ".local" / "state" / (
        "claude-gateway-extensions"
    )


@pytest.mark.parametrize("raw", ["true", "1", "yes"])
def test_bool_parsing_true(raw: str) -> None:
    config = Config.from_env({"CGX_PERSIST": raw})
    assert config.persist is True


@pytest.mark.parametrize("raw", ["false", "0", "NO"])
def test_bool_parsing_false(raw: str) -> None:
    config = Config.from_env({"CGX_PERSIST": raw})
    assert config.persist is False


def test_bool_parsing_invalid_raises() -> None:
    with pytest.raises(ConfigError):
        Config.from_env({"CGX_PERSIST": "maybe"})


def test_int_parsing_invalid_raises() -> None:
    with pytest.raises(ConfigError):
        Config.from_env({"CGX_PORT": "not-a-port"})


def test_on_cache_write_invalid_raises() -> None:
    with pytest.raises(ConfigError):
        Config.from_env({"CGX_ON_CACHE_WRITE": "explode"})


def test_on_cache_write_continue_accepted() -> None:
    config = Config.from_env({"CGX_ON_CACHE_WRITE": "continue"})
    assert config.on_cache_write == "continue"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
@pytest.mark.invariant(decision="Control API on /cgx/, loopback only, unauthenticated")
def test_loopback_hosts_accepted(host: str) -> None:
    config = Config.from_env({"CGX_HOST": host})
    assert config.host == host
    assert config.is_local_host() is True


@pytest.mark.parametrize("host", ["127.0.0.2", "127.255.255.254", "[::1]"])
@pytest.mark.invariant(decision="Control API on /cgx/, loopback only, unauthenticated")
def test_any_loopback_ip_literal_is_local(host: str) -> None:
    config = Config.from_env({"CGX_HOST": host})
    assert config.host == host
    assert config.is_local_host() is True


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "myhost"])
@pytest.mark.invariant(decision="Control API on /cgx/, loopback only, unauthenticated")
def test_non_local_hosts_are_accepted_but_not_local(host: str) -> None:
    """A non-local CGX_HOST is the user's explicit choice; it is never refused."""
    config = Config.from_env({"CGX_HOST": host})
    assert config.host == host
    assert config.is_local_host() is False


def test_url_defaults_from_host_and_port() -> None:
    """With nothing set, url tracks CGX_HOST/CGX_PORT rather than a hardcoded literal."""
    config = Config.from_env({"CGX_PORT": "9001"})
    assert config.url == "http://127.0.0.1:9001"


def test_url_prefers_cgx_url_over_everything() -> None:
    config = Config.from_env(
        {
            "CGX_URL": "http://127.0.0.1:9999",
            "CGX_PORT": "9001",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:7000",
        }
    )
    assert config.url == "http://127.0.0.1:9999"


def test_url_falls_back_to_loopback_anthropic_base_url() -> None:
    config = Config.from_env({"ANTHROPIC_BASE_URL": "http://127.0.0.1:9001"})
    assert config.url == "http://127.0.0.1:9001"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:9001",  # not http
        "http://192.168.1.5:9001",  # not loopback
        "http://example.com:9001",  # not loopback
    ],
)
@pytest.mark.invariant(decision="The port is written down, never assumed")
def test_url_ignores_non_loopback_or_non_http_anthropic_base_url(base_url: str) -> None:
    """A non-loopback (or non-http) ANTHROPIC_BASE_URL is not this daemon; ignore it."""
    config = Config.from_env({"ANTHROPIC_BASE_URL": base_url})
    assert config.url == "http://127.0.0.1:8787"


# -- resolve_config_file ----------------------------------------------------


def test_resolve_config_file_default_uses_xdg_config_home() -> None:
    path = resolve_config_file({"XDG_CONFIG_HOME": "/tmp/xdg-config"})
    assert path == Path("/tmp/xdg-config/cgx/config.toml")


def test_resolve_config_file_default_falls_back_to_home() -> None:
    path = resolve_config_file({"HOME": "/home/someone"})
    assert path == Path("/home/someone/.config/cgx/config.toml")


def test_resolve_config_file_override_wins_over_everything() -> None:
    path = resolve_config_file(
        {"CGX_CONFIG_FILE": "/srv/cgx.toml", "XDG_CONFIG_HOME": "/tmp/xdg-config"}
    )
    assert path == Path("/srv/cgx.toml")


def test_config_from_env_sets_config_file_and_records_configured_env_vars() -> None:
    config = Config.from_env(
        {"XDG_CONFIG_HOME": "/tmp/xdg-config", "CGX_SHADOW_FORK_MAX_TOKENS": "256"}
    )
    assert config.config_file == Path("/tmp/xdg-config/cgx/config.toml")
    assert "CGX_SHADOW_FORK_MAX_TOKENS" in config.configured_env_vars
    assert "CGX_SHADOW_FORK_MAX_PER_MIN" not in config.configured_env_vars


# -- parse_shadow_fork_file ---------------------------------------------------


def test_parse_shadow_fork_file_reads_all_four_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[shadow_fork]\n"
        "max_tokens = 768\n"
        "max_per_min = 10\n"
        "thinking_guard = false\n"
        'model = "claude-reviewer"\n'
    )
    settings = parse_shadow_fork_file(path)
    assert settings == ShadowForkSettings(
        max_tokens=768, max_per_min=10, thinking_guard=False, model="claude-reviewer"
    )


def test_parse_shadow_fork_file_missing_table_is_all_none(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("# no [shadow_fork] table here\n")
    assert parse_shadow_fork_file(path) == ShadowForkSettings()


def test_parse_shadow_fork_file_partial_table_leaves_rest_none(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[shadow_fork]\nmax_tokens = 999\n")
    settings = parse_shadow_fork_file(path)
    assert settings == ShadowForkSettings(max_tokens=999)


def test_parse_shadow_fork_file_syntax_error_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[shadow_fork\nmax_tokens = 1\n")
    with pytest.raises(ConfigError):
        parse_shadow_fork_file(path)


def test_parse_shadow_fork_file_unknown_key_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[shadow_fork]\nbogus_key = 1\n")
    with pytest.raises(ConfigError, match="bogus_key"):
        parse_shadow_fork_file(path)


@pytest.mark.parametrize(
    ("key", "bad_toml"),
    [
        ("max_tokens", 'max_tokens = "not an int"'),
        ("max_tokens", "max_tokens = true"),  # bool is not an int here
        ("max_per_min", 'max_per_min = "not an int"'),
        ("thinking_guard", 'thinking_guard = "not a bool"'),
        ("model", "model = 123"),
    ],
)
def test_parse_shadow_fork_file_wrong_type_raises(tmp_path: Path, key: str, bad_toml: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"[shadow_fork]\n{bad_toml}\n")
    with pytest.raises(ConfigError, match=key):
        parse_shadow_fork_file(path)


# -- ConfigFileWatcher --------------------------------------------------------


def test_config_file_watcher_missing_file_is_all_none_and_not_present(tmp_path: Path) -> None:
    watcher = ConfigFileWatcher(tmp_path / "does-not-exist.toml")
    state = watcher.current()
    assert state.present is False
    assert state.parse_error is None
    assert state.settings == ShadowForkSettings()


@pytest.mark.invariant(
    decision="Shadow Fork tuning is hot reloaded from one config file; "
    "the file wins over the environment for exactly the keys it holds."
)
def test_config_file_watcher_reloads_after_mtime_change(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[shadow_fork]\nmax_tokens = 100\n")
    watcher = ConfigFileWatcher(path)
    assert watcher.current().settings.max_tokens == 100

    # Same content re-read without a stat change must not re-parse -- but
    # since a passing test can't observe "did not re-parse" directly, the
    # meaningful assertion is the one below: a real edit *does* take effect.
    # Push the mtime forward after the write: a same-size rewrite within one
    # coarse filesystem clock tick (Windows) would otherwise keep its stat key.
    mtime = os.stat(path).st_mtime + 5
    path.write_text("[shadow_fork]\nmax_tokens = 200\n")
    os.utime(path, (mtime, mtime))
    assert watcher.current().settings.max_tokens == 200


def test_config_file_watcher_keeps_last_good_on_invalid_reload(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[shadow_fork]\nmax_tokens = 100\n")
    watcher = ConfigFileWatcher(path)
    assert watcher.current().settings.max_tokens == 100

    os.utime(path, (os.stat(path).st_mtime + 5, os.stat(path).st_mtime + 5))
    path.write_text("[shadow_fork]\nmax_tokens = not valid toml\n")
    state = watcher.current()
    assert state.parse_error is not None
    assert state.present is True
    assert state.settings.max_tokens == 100  # kept the last good value


def test_config_file_watcher_disappearing_file_resets_to_environment(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[shadow_fork]\nmax_tokens = 100\n")
    watcher = ConfigFileWatcher(path)
    assert watcher.current().settings.max_tokens == 100

    path.unlink()
    state = watcher.current()
    assert state.present is False
    assert state.parse_error is None
    assert state.settings == ShadowForkSettings()  # not "kept the last good value"


def test_config_file_watcher_invalid_file_never_loaded_falls_through_to_none(
    tmp_path: Path,
) -> None:
    """An invalid file that was *never* valid keeps the all-``None`` starting settings."""
    path = tmp_path / "config.toml"
    path.write_text("[shadow_fork]\nmax_tokens = not valid toml\n")
    watcher = ConfigFileWatcher(path)
    state = watcher.current()
    assert state.parse_error is not None
    assert state.settings == ShadowForkSettings()


# -- merge_shadow_fork --------------------------------------------------------


def _config(tmp_path: Path, **overrides: object) -> Config:
    fields: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8787,
        "upstream": "https://api.anthropic.com",
        "state_dir": tmp_path,
        "persist": False,
        "default_enabled": False,
        "interval_1h_s": 3000,
        "interval_5m_s": 240,
        "max_duration_s": 86400,
        "on_cache_write": "stop",
        "max_streams_per_session": 8,
        "log_level": "INFO",
        "url": "http://127.0.0.1:8787",
        "drain_timeout_s": 600,
        "shadow_fork_max_tokens": 512,
        "shadow_fork_max_per_min": 6,
        "shadow_fork_thinking_guard": True,
        "shadow_fork_model": None,
        "config_file": tmp_path / "config.toml",
        "configured_env_vars": frozenset(),
    }
    fields.update(overrides)
    return Config(**fields)  # type: ignore[arg-type]


@pytest.mark.invariant(
    decision="Shadow Fork tuning is hot reloaded from one config file; "
    "the file wins over the environment for exactly the keys it holds."
)
def test_merge_shadow_fork_file_wins_per_key_over_config(tmp_path: Path) -> None:
    config = _config(tmp_path, shadow_fork_max_tokens=512, shadow_fork_model="env-model")
    file_settings = ShadowForkSettings(
        max_tokens=768, max_per_min=None, thinking_guard=None, model=None
    )
    merged = merge_shadow_fork(config, file_settings)
    assert merged.max_tokens == 768  # file wins
    assert merged.max_per_min == 6  # falls through to config (env/default)
    assert merged.thinking_guard is True  # falls through to config
    assert merged.model == "env-model"  # falls through to config
