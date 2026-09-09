"""Tests for claude_gateway_extensions.core.fork and Scheduler.fork_now."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from claude_gateway_extensions.config import Config
from claude_gateway_extensions.core.clock import FakeClock
from claude_gateway_extensions.core.fork import (
    ForkVerdict,
    build_fork_body,
    fork_once,
    parse_verdict,
)
from claude_gateway_extensions.core.scheduler import Scheduler
from claude_gateway_extensions.core.selector import LargestPrefixSelector
from claude_gateway_extensions.core.snapshot import SessionState, Snapshot, Usage
from claude_gateway_extensions.core.store import MemoryStore
from claude_gateway_extensions.core.upstream import UpstreamResponse


class ScriptedUpstream:
    """Fake Upstream returning pre-scripted responses (or raising) in order."""

    def __init__(self, responses: list[UpstreamResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], bytes]] = []

    async def send(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes
    ) -> UpstreamResponse:
        self.calls.append((method, url, dict(headers), body))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _verdict_body(verdict_obj: object, cache_creation: int = 0, input_tokens: int = 5) -> bytes:
    """Build a fake ``/v1/messages`` response body carrying ``verdict_obj`` as its reply text."""
    text = verdict_obj if isinstance(verdict_obj, str) else json.dumps(verdict_obj)
    return json.dumps(
        {
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_creation,
            },
        }
    ).encode()


def _verdict_response(verdict_obj: object, cache_creation: int = 0) -> UpstreamResponse:
    return UpstreamResponse(
        status=200,
        headers={"content-type": "application/json"},
        body=_verdict_body(verdict_obj, cache_creation=cache_creation),
    )


def _snapshot(session_id: str = "sess-1", total_input: int = 100) -> Snapshot:
    return Snapshot(
        session_id=session_id,
        stream_key="stream-a",
        model="claude-x",
        headers={"authorization": "Bearer old", "content-type": "application/json"},
        body=(
            b'{"model": "claude-x", "max_tokens": 4096, '
            b'"messages": [{"role": "user", "content": "hi"}]}'
        ),
        captured_at=1000.0,
        usage=Usage(input_tokens=total_input),
    )


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
        "fork_max_tokens": 512,
        "fork_max_per_min": 6,
    }
    fields.update(overrides)
    return Config(**fields)  # type: ignore[arg-type]


# -- build_fork_body ---------------------------------------------------------


@pytest.mark.invariant(decision="A fork is one replay, never a session")
def test_build_fork_body_preserves_values_and_sets_cap() -> None:
    original = {
        "model": "claude-x",
        "max_tokens": 4096,
        "stream": True,
        "system": [
            {
                "type": "text",
                "text": "you are helpful",
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ],
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "bash", "cache_control": {"type": "ephemeral"}}],
        "tool_choice": {"type": "auto"},
    }
    body = json.dumps(original).encode("utf-8")

    forked = build_fork_body(body, "Please review", {"tool": "Bash", "count": 2}, 256)
    data = json.loads(forked)

    assert data["max_tokens"] == 256
    assert data["stream"] is False
    # Everything else, including cache_control, tools/tool_choice and the
    # system prompt, is preserved by value exactly.
    assert data["model"] == original["model"]
    assert data["system"] == original["system"]
    assert data["tools"] == original["tools"]
    assert data["tool_choice"] == original["tool_choice"]

    assert len(data["messages"]) == 2
    assert data["messages"][0] == original["messages"][0]
    appended = data["messages"][1]
    assert appended["role"] == "user"
    assert appended["content"].startswith("Please review\n\n```json\n")
    assert appended["content"].endswith("```")
    assert '"tool": "Bash"' in appended["content"]
    assert '"count": 2' in appended["content"]


@pytest.mark.invariant(decision="A fork is one replay, never a session")
def test_build_fork_body_appends_exactly_one_message_when_messages_absent() -> None:
    body = json.dumps({"model": "m", "max_tokens": 10}).encode()
    data = json.loads(build_fork_body(body, "review", {"a": 1}, 100))
    assert len(data["messages"]) == 1
    assert data["messages"][0]["role"] == "user"


@pytest.mark.invariant(decision="the fork body preserves the cached prefix")
def test_build_fork_body_preserves_tools_and_tool_choice_when_present() -> None:
    body = json.dumps(
        {"model": "m", "max_tokens": 10, "tools": [{"name": "x"}], "tool_choice": "auto"}
    ).encode()
    data = json.loads(build_fork_body(body, "review", None, 100))
    assert data["tools"] == [{"name": "x"}]
    assert data["tool_choice"] == "auto"


def test_build_fork_body_does_not_add_stream_key_when_absent() -> None:
    body = json.dumps({"model": "m", "max_tokens": 10}).encode()
    data = json.loads(build_fork_body(body, "review", None, 100))
    assert "stream" not in data


# -- parse_verdict ------------------------------------------------------------


@pytest.mark.invariant(decision="Anything unparseable is `ok`")
def test_parse_verdict_non_200_is_fail_open() -> None:
    result = parse_verdict(500, b"internal error")
    assert result.verdict == "ok"
    assert result.reason == "fork_failed"
    assert result.usage is None
    assert result.paid_write is False


@pytest.mark.invariant(decision="Anything unparseable is `ok`")
def test_parse_verdict_truncated_body_is_fail_open() -> None:
    truncated = b'{"content": [{"type": "text", "text": "{\\"verdict\\": \\"ok\\""'
    result = parse_verdict(200, truncated)
    assert result.verdict == "ok"
    assert result.reason == "unparseable_verdict"


@pytest.mark.invariant(decision="Anything unparseable is `ok`")
def test_parse_verdict_prose_wrapped_json_is_fail_open() -> None:
    text = "Sure thing! " + json.dumps({"verdict": "ok", "reason": "fine"})
    result = parse_verdict(200, _verdict_body(text))
    assert result.verdict == "ok"
    assert result.reason == "unparseable_verdict"


@pytest.mark.invariant(decision="Anything unparseable is `ok`")
def test_parse_verdict_wrong_typed_verdict_field_is_fail_open() -> None:
    result = parse_verdict(200, _verdict_body({"verdict": "maybe", "reason": "x"}))
    assert result.verdict == "ok"
    assert result.reason == "unparseable_verdict"


@pytest.mark.invariant(decision="Anything unparseable is `ok`")
def test_parse_verdict_non_str_reason_is_fail_open() -> None:
    result = parse_verdict(200, _verdict_body({"verdict": "ok", "reason": 5}))
    assert result.verdict == "ok"
    assert result.reason == "unparseable_verdict"


@pytest.mark.invariant(decision="Anything unparseable is `ok`")
def test_parse_verdict_non_object_mutation_is_fail_open() -> None:
    result = parse_verdict(200, _verdict_body({"verdict": "ok", "reason": "x", "mutation": "no"}))
    assert result.verdict == "ok"
    assert result.reason == "unparseable_verdict"


def test_parse_verdict_accepts_valid_intervene_with_mutation() -> None:
    body = _verdict_body({"verdict": "intervene", "reason": "risky", "mutation": {"x": 1}})
    result = parse_verdict(200, body)
    assert result.verdict == "intervene"
    assert result.reason == "risky"
    assert result.mutation == {"x": 1}
    assert result.paid_write is False


def test_parse_verdict_tool_use_reply_is_fail_open() -> None:
    body = json.dumps(
        {
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
            "usage": {"input_tokens": 5, "cache_creation_input_tokens": 0},
        }
    ).encode()
    result = parse_verdict(200, body)
    assert result.verdict == "ok"
    assert result.reason == "tool_use_reply"
    assert result.usage is not None


@pytest.mark.invariant(decision="Every fork is expected to be a cache read")
def test_parse_verdict_flags_paid_write_but_still_returns_the_verdict() -> None:
    body = _verdict_body({"verdict": "ok", "reason": "fine"}, cache_creation=42)
    result = parse_verdict(200, body)
    assert result.verdict == "ok"
    assert result.reason == "fine"
    assert result.paid_write is True
    assert result.usage is not None
    assert result.usage.cache_creation_input_tokens == 42


# -- fork_once ----------------------------------------------------------------


async def test_fork_once_catches_transport_exceptions() -> None:
    upstream = ScriptedUpstream([RuntimeError("boom")])
    result = await fork_once(
        upstream, "https://api.anthropic.com", _snapshot(), None, "review", {"a": 1}, 100
    )
    assert result.verdict == "ok"
    assert result.reason == "fork_failed"


async def test_fork_once_returns_the_parsed_verdict() -> None:
    upstream = ScriptedUpstream([_verdict_response({"verdict": "ok", "reason": "fine"})])
    result = await fork_once(
        upstream,
        "https://api.anthropic.com",
        _snapshot(),
        ("authorization", "Bearer new"),
        "review",
        {"a": 1},
        100,
    )
    assert result.verdict == "ok"
    assert result.reason == "fine"
    assert upstream.calls[0][2]["authorization"] == "Bearer new"


# -- Scheduler.fork_now --------------------------------------------------------


async def test_fork_now_returns_ok_no_snapshot_when_nothing_captured(tmp_path: Path) -> None:
    clock = FakeClock(start=1000.0)
    store = MemoryStore(max_streams_per_session=8)
    upstream = ScriptedUpstream([])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    result = await scheduler.fork_now("sess-unknown", "review", {}, None)

    assert result == ForkVerdict(
        verdict="ok", reason="no_snapshot", mutation=None, usage=None, paid_write=False
    )
    assert upstream.calls == []


async def test_fork_now_rate_limits_then_recovers_after_a_minute(tmp_path: Path) -> None:
    clock = FakeClock(start=1000.0)
    store = MemoryStore(max_streams_per_session=8)
    store.put_snapshot(_snapshot("sess-1"))
    store.put_state(
        SessionState(session_id="sess-1", last_credential=("authorization", "Bearer a"))
    )
    upstream = ScriptedUpstream(
        [
            _verdict_response({"verdict": "ok", "reason": "1"}),
            _verdict_response({"verdict": "ok", "reason": "2"}),
            _verdict_response({"verdict": "ok", "reason": "3"}),
        ]
    )
    scheduler = Scheduler(
        _config(tmp_path, fork_max_per_min=2), store, LargestPrefixSelector(), upstream, clock
    )

    first = await scheduler.fork_now("sess-1", "review", {}, None)
    second = await scheduler.fork_now("sess-1", "review", {}, None)
    assert first.reason == "1"
    assert second.reason == "2"
    assert len(upstream.calls) == 2

    limited = await scheduler.fork_now("sess-1", "review", {}, None)
    assert limited == ForkVerdict(
        verdict="ok", reason="rate_limited", mutation=None, usage=None, paid_write=False
    )
    assert len(upstream.calls) == 2

    clock.advance(61.0)
    third = await scheduler.fork_now("sess-1", "review", {}, None)
    assert third.reason == "3"
    assert len(upstream.calls) == 3


@pytest.mark.invariant(decision="A fork must not touch bump bookkeeping")
async def test_fork_now_leaves_bump_bookkeeping_untouched(tmp_path: Path) -> None:
    clock = FakeClock(start=1000.0)
    store = MemoryStore(max_streams_per_session=8)
    store.put_snapshot(_snapshot("sess-1"))
    store.put_state(
        SessionState(
            session_id="sess-1",
            enabled=True,
            bump_count=5,
            last_bump_at=1234.0,
            last_credential=("authorization", "Bearer a"),
        )
    )
    upstream = ScriptedUpstream([_verdict_response({"verdict": "ok", "reason": "fine"}, 77)])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    result = await scheduler.fork_now("sess-1", "review", {}, None)

    assert result.verdict == "ok"
    assert result.paid_write is True
    state = store.get_state("sess-1")
    assert state is not None
    assert state.enabled is True
    assert state.bump_count == 5
    assert state.last_bump_at == 1234.0
