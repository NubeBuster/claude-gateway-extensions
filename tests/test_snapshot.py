"""Tests for claude_cache_bump_gateway.core.snapshot."""

import json

import pytest

from claude_cache_bump_gateway.core.snapshot import (
    SnapshotError,
    Usage,
    stream_key,
    wants_1h_ttl,
)


def _body(**kwargs: object) -> bytes:
    return json.dumps(kwargs).encode("utf-8")


def test_stream_key_stable_for_identical_input() -> None:
    body = _body(model="claude-x", system="you are helpful", tools=[{"name": "bash"}])
    assert stream_key(body) == stream_key(body)


def test_stream_key_ignores_messages_and_max_tokens() -> None:
    a = _body(model="claude-x", system="s", tools=[], messages=[{"role": "user"}], max_tokens=1)
    b = _body(
        model="claude-x", system="s", tools=[], messages=[{"role": "assistant"}], max_tokens=2
    )
    assert stream_key(a) == stream_key(b)


def test_stream_key_differs_on_model() -> None:
    a = _body(model="claude-x", system="s", tools=[])
    b = _body(model="claude-y", system="s", tools=[])
    assert stream_key(a) != stream_key(b)


def test_stream_key_treats_missing_fields_as_null() -> None:
    a = _body(model="claude-x")
    b = _body(model="claude-x", system=None, tools=None)
    assert stream_key(a) == stream_key(b)


def test_stream_key_invalid_json_raises() -> None:
    with pytest.raises(SnapshotError):
        stream_key(b"{not json")


def test_stream_key_non_object_raises() -> None:
    with pytest.raises(SnapshotError):
        stream_key(b"[1, 2, 3]")


def test_wants_1h_ttl_true_when_present_nested() -> None:
    body = _body(
        model="m",
        system=[{"type": "text", "text": "x", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
    )
    assert wants_1h_ttl(body) is True


def test_wants_1h_ttl_false_when_absent() -> None:
    body = _body(model="m", system="s")
    assert wants_1h_ttl(body) is False


def test_wants_1h_ttl_false_when_ttl_is_5m() -> None:
    body = _body(
        model="m",
        tools=[{"name": "bash", "cache_control": {"type": "ephemeral", "ttl": "5m"}}],
    )
    assert wants_1h_ttl(body) is False


def test_wants_1h_ttl_invalid_json_raises() -> None:
    with pytest.raises(SnapshotError):
        wants_1h_ttl(b"not json")


def test_usage_defaults_and_total_input() -> None:
    usage = Usage.from_dict(None)
    assert usage.total_input == 0
    usage = Usage.from_dict(
        {"input_tokens": 10, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 2}
    )
    assert usage.total_input == 17
