"""Tests for claude_cache_bump_gateway.config."""

from pathlib import Path

import pytest

from claude_cache_bump_gateway.config import Config, ConfigError


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
    assert config.allow_non_loopback is False


def test_default_state_dir_uses_xdg_state_home() -> None:
    config = Config.from_env({"XDG_STATE_HOME": "/tmp/xdg-state"})
    assert config.state_dir == Path("/tmp/xdg-state/claude-cache-bump-gateway")


def test_default_state_dir_falls_back_to_home() -> None:
    config = Config.from_env({"HOME": "/home/someone"})
    assert config.state_dir == Path("/home/someone/.local/state/claude-cache-bump-gateway")


def test_explicit_state_dir_overrides_default() -> None:
    config = Config.from_env({"CCBG_STATE_DIR": "/srv/ccbg"})
    assert config.state_dir == Path("/srv/ccbg")


@pytest.mark.parametrize("raw", ["true", "True", "1", "yes", "YES"])
def test_bool_parsing_true(raw: str) -> None:
    config = Config.from_env({"CCBG_PERSIST": raw})
    assert config.persist is True


@pytest.mark.parametrize("raw", ["false", "False", "0", "no", "NO"])
def test_bool_parsing_false(raw: str) -> None:
    config = Config.from_env({"CCBG_PERSIST": raw})
    assert config.persist is False


def test_bool_parsing_invalid_raises() -> None:
    with pytest.raises(ConfigError):
        Config.from_env({"CCBG_PERSIST": "maybe"})


def test_int_parsing_invalid_raises() -> None:
    with pytest.raises(ConfigError):
        Config.from_env({"CCBG_PORT": "not-a-port"})


def test_on_cache_write_invalid_raises() -> None:
    with pytest.raises(ConfigError):
        Config.from_env({"CCBG_ON_CACHE_WRITE": "explode"})


def test_on_cache_write_continue_accepted() -> None:
    config = Config.from_env({"CCBG_ON_CACHE_WRITE": "continue"})
    assert config.on_cache_write == "continue"


def test_non_loopback_host_refused_by_default() -> None:
    with pytest.raises(ConfigError):
        Config.from_env({"CCBG_HOST": "0.0.0.0"})


def test_non_loopback_host_allowed_with_override() -> None:
    config = Config.from_env({"CCBG_HOST": "0.0.0.0", "CCBG_ALLOW_NON_LOOPBACK": "true"})
    assert config.host == "0.0.0.0"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_accepted(host: str) -> None:
    config = Config.from_env({"CCBG_HOST": host})
    assert config.host == host


@pytest.mark.parametrize("host", ["127.0.0.2", "127.255.255.254", "[::1]"])
def test_any_loopback_ip_literal_accepted(host: str) -> None:
    config = Config.from_env({"CCBG_HOST": host})
    assert config.host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "myhost"])
def test_non_loopback_hosts_refused_by_default(host: str) -> None:
    with pytest.raises(ConfigError):
        Config.from_env({"CCBG_HOST": host})


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "myhost"])
def test_non_loopback_hosts_accepted_with_override(host: str) -> None:
    config = Config.from_env({"CCBG_HOST": host, "CCBG_ALLOW_NON_LOOPBACK": "true"})
    assert config.host == host
