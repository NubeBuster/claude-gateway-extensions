"""Tests for claude_gateway_extensions.config."""

from pathlib import Path

import pytest

from claude_gateway_extensions.config import Config, ConfigError


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
