"""Tests for claude_gateway_extensions.core.snapshot."""

import json

import pytest

from claude_gateway_extensions.core.snapshot import (
    CACHE_TTL_1H_S,
    CACHE_TTL_5M_S,
    Breakpoint,
    SnapshotError,
    Usage,
    body_is_subagent,
    effective_ttl_label,
    parse_breakpoints,
    shortest_breakpoint_ttl_s,
    stream_key,
    wants_1h_ttl,
)


def _body(**kwargs: object) -> bytes:
    return json.dumps(kwargs).encode("utf-8")


@pytest.mark.invariant(decision="Streams keyed by model + system + tools, largest prefix wins")
def test_stream_key_stable_for_identical_input() -> None:
    body = _body(model="claude-x", system="you are helpful", tools=[{"name": "bash"}])
    assert stream_key(body) == stream_key(body)


@pytest.mark.invariant(decision="Streams keyed by model + system + tools, largest prefix wins")
def test_stream_key_ignores_messages_and_max_tokens() -> None:
    a = _body(model="claude-x", system="s", tools=[], messages=[{"role": "user"}], max_tokens=1)
    b = _body(
        model="claude-x", system="s", tools=[], messages=[{"role": "assistant"}], max_tokens=2
    )
    assert stream_key(a) == stream_key(b)


@pytest.mark.invariant(decision="Streams keyed by model + system + tools, largest prefix wins")
def test_stream_key_differs_on_model() -> None:
    a = _body(model="claude-x", system="s", tools=[])
    b = _body(model="claude-y", system="s", tools=[])
    assert stream_key(a) != stream_key(b)


@pytest.mark.invariant(decision="Streams keyed by model + system + tools, largest prefix wins")
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


def test_parse_breakpoints_orders_tools_system_messages() -> None:
    body = _body(
        model="m",
        tools=[{"name": "bash", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        system=[{"type": "text", "text": "s", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}},
                ],
            }
        ],
    )
    assert parse_breakpoints(body) == [
        Breakpoint("tools[0]", "1h"),
        Breakpoint("system[0]", "1h"),
        Breakpoint("messages[0].content[0]", "5m"),
    ]


def test_parse_breakpoints_empty_without_cache_control() -> None:
    body = _body(model="m", system="s", messages=[{"role": "user", "content": "hi"}])
    assert parse_breakpoints(body) == []


def test_parse_breakpoints_invalid_json_raises() -> None:
    with pytest.raises(SnapshotError):
        parse_breakpoints(b"not json")


@pytest.mark.invariant(decision="Interval derived from the snapshot's shortest breakpoint TTL")
def test_shortest_breakpoint_ttl_s_mixed_is_5m() -> None:
    breakpoints = [Breakpoint("system[0]", "1h"), Breakpoint("messages[0].content[0]", "5m")]
    assert shortest_breakpoint_ttl_s(breakpoints) == CACHE_TTL_5M_S
    assert effective_ttl_label(breakpoints) == "5m"


@pytest.mark.invariant(decision="Interval derived from the snapshot's shortest breakpoint TTL")
def test_shortest_breakpoint_ttl_s_all_1h_is_1h() -> None:
    breakpoints = [Breakpoint("system[0]", "1h"), Breakpoint("system[1]", "1h")]
    assert shortest_breakpoint_ttl_s(breakpoints) == CACHE_TTL_1H_S
    assert effective_ttl_label(breakpoints) == "1h"


@pytest.mark.invariant(decision="Interval derived from the snapshot's shortest breakpoint TTL")
def test_shortest_breakpoint_ttl_s_empty_is_5m() -> None:
    assert shortest_breakpoint_ttl_s([]) == CACHE_TTL_5M_S
    assert effective_ttl_label([]) == "5m"


def test_body_is_subagent_true_when_marker_present() -> None:
    body = _body(
        model="m",
        system=[
            {"type": "text", "text": "x-anthropic-billing-header: cc_is_subagent=true;other=1"}
        ],
    )
    assert body_is_subagent(body) is True


def test_body_is_subagent_false_when_marker_absent_from_attribution_block() -> None:
    body = _body(
        model="m",
        system=[{"type": "text", "text": "x-anthropic-billing-header: cc_is_subagent=false"}],
    )
    assert body_is_subagent(body) is False


def test_body_is_subagent_none_when_no_system() -> None:
    body = _body(model="m")
    assert body_is_subagent(body) is None


def test_body_is_subagent_none_when_system0_is_not_attribution_shaped() -> None:
    body = _body(model="m", system=[{"type": "text", "text": "you are a helpful assistant"}])
    assert body_is_subagent(body) is None


def test_body_is_subagent_none_when_marker_is_deeper_than_system0() -> None:
    # Only system[0] is ever considered: a lookalike further in is content
    # the user put there, not the client's own attribution block.
    body = _body(
        model="m",
        system=[
            {"type": "text", "text": "you are a helpful assistant"},
            {"type": "text", "text": "x-anthropic-billing-header: cc_is_subagent=true"},
        ],
    )
    assert body_is_subagent(body) is None


def test_body_is_subagent_none_when_body_unparseable() -> None:
    assert body_is_subagent(b"not json") is None


def test_body_is_subagent_none_when_body_not_an_object() -> None:
    assert body_is_subagent(b"[1, 2, 3]") is None


def test_usage_defaults_and_total_input() -> None:
    usage = Usage.from_dict(None)
    assert usage.total_input == 0
    usage = Usage.from_dict(
        {"input_tokens": 10, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 2}
    )
    assert usage.total_input == 17
