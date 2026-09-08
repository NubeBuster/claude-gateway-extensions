"""Tests for claude_cache_bump_gateway.core.scheduler."""

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from claude_cache_bump_gateway.config import Config
from claude_cache_bump_gateway.core.clock import FakeClock
from claude_cache_bump_gateway.core.scheduler import NoSnapshot, Scheduler
from claude_cache_bump_gateway.core.selector import LargestPrefixSelector
from claude_cache_bump_gateway.core.snapshot import Snapshot, Usage
from claude_cache_bump_gateway.core.store import MemoryStore
from claude_cache_bump_gateway.core.upstream import UpstreamResponse


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


def _ok_response(cache_creation: int = 0) -> UpstreamResponse:
    body = json.dumps(
        {"usage": {"input_tokens": 10, "cache_creation_input_tokens": cache_creation}}
    ).encode()
    return UpstreamResponse(status=200, headers={"content-type": "application/json"}, body=body)


def _transient_response() -> UpstreamResponse:
    return UpstreamResponse(status=503, headers={}, body=b"unavailable")


def _snapshot(session_id: str = "sess-1", total_input: int = 100) -> Snapshot:
    return Snapshot(
        session_id=session_id,
        stream_key="stream-a",
        model="claude-x",
        headers={"authorization": "Bearer old", "content-type": "application/json"},
        body=b'{"model": "claude-x", "max_tokens": 4096, "messages": []}',
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
    }
    fields.update(overrides)
    return Config(**fields)  # type: ignore[arg-type]


async def _pump_until(predicate: Callable[[], bool], max_iters: int = 200_000) -> None:
    for _ in range(max_iters):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not met within pump budget")


async def _pump(iterations: int) -> None:
    for _ in range(iterations):
        await asyncio.sleep(0)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1000.0)


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore(max_streams_per_session=8)


async def test_enabling_schedules_bump_at_right_time(
    tmp_path: Path, clock: FakeClock, store: MemoryStore
) -> None:
    upstream = ScriptedUpstream([_ok_response()])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    scheduler.note_request("sess-1", _snapshot(), ("authorization", "Bearer new"))
    scheduler.set_enabled("sess-1", True, interval_s=5)

    await _pump_until(lambda: store.get_state("sess-1").bump_count == 1)  # type: ignore[union-attr]

    state = store.get_state("sess-1")
    assert state is not None
    assert state.last_bump_at == pytest.approx(1005.0)
    assert upstream.calls[0][2]["authorization"] == "Bearer new"

    await scheduler.stop()


async def test_real_request_resets_timer(
    tmp_path: Path, clock: FakeClock, store: MemoryStore
) -> None:
    upstream = ScriptedUpstream([_ok_response()])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    scheduler.note_request("sess-1", _snapshot(), ("authorization", "Bearer a"))
    scheduler.set_enabled("sess-1", True, interval_s=10)

    await _pump_until(lambda: clock.now() >= 1005.0)
    assert store.get_state("sess-1").bump_count == 0  # type: ignore[union-attr]

    scheduler.note_request("sess-1", None, None)  # reset timer at t=1005

    await _pump_until(lambda: store.get_state("sess-1").bump_count == 1)  # type: ignore[union-attr]
    state = store.get_state("sess-1")
    assert state is not None
    assert state.last_bump_at == pytest.approx(1015.0)

    await scheduler.stop()


async def test_cache_write_stops_with_reason(
    tmp_path: Path, clock: FakeClock, store: MemoryStore
) -> None:
    upstream = ScriptedUpstream([_ok_response(cache_creation=50)])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    scheduler.note_request("sess-1", _snapshot(), ("authorization", "Bearer a"))
    scheduler.set_enabled("sess-1", True, interval_s=2)

    await _pump_until(lambda: store.get_state("sess-1").stopped_reason is not None)  # type: ignore[union-attr]

    state = store.get_state("sess-1")
    assert state is not None
    assert state.stopped_reason == "cache_write"
    assert state.enabled is False
    assert state.bump_count == 1

    await scheduler.stop()


async def test_cache_write_continues_when_configured(
    tmp_path: Path, clock: FakeClock, store: MemoryStore
) -> None:
    upstream = ScriptedUpstream([_ok_response(cache_creation=50), _ok_response(cache_creation=0)])
    scheduler = Scheduler(
        _config(tmp_path, on_cache_write="continue"),
        store,
        LargestPrefixSelector(),
        upstream,
        clock,
    )

    scheduler.note_request("sess-1", _snapshot(), ("authorization", "Bearer a"))
    scheduler.set_enabled("sess-1", True, interval_s=2)

    await _pump_until(lambda: store.get_state("sess-1").bump_count == 2)  # type: ignore[union-attr]
    state = store.get_state("sess-1")
    assert state is not None
    assert state.enabled is True
    assert state.stopped_reason is None

    await scheduler.stop()


async def test_transient_retries_thrice_then_stops(
    tmp_path: Path, clock: FakeClock, store: MemoryStore
) -> None:
    upstream = ScriptedUpstream([_transient_response() for _ in range(4)])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    scheduler.note_request("sess-1", _snapshot(), ("authorization", "Bearer a"))
    scheduler.set_enabled("sess-1", True, interval_s=1)

    await _pump_until(lambda: store.get_state("sess-1").stopped_reason is not None)  # type: ignore[union-attr]

    state = store.get_state("sess-1")
    assert state is not None
    assert state.stopped_reason == "transient"
    assert state.enabled is False
    assert len(upstream.calls) == 4
    # 3 backoffs of 1 + 4 + 16 = 21s elapsed on top of the 1s scheduling wait.
    assert clock.now() == pytest.approx(1001.0 + 21.0)

    await scheduler.stop()


async def test_max_duration_stops(tmp_path: Path, clock: FakeClock, store: MemoryStore) -> None:
    upstream = ScriptedUpstream([])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    scheduler.note_request("sess-1", _snapshot(), ("authorization", "Bearer a"))
    scheduler.set_enabled("sess-1", True, interval_s=1000, max_duration_s=5)

    await _pump_until(lambda: store.get_state("sess-1").stopped_reason is not None)  # type: ignore[union-attr]

    state = store.get_state("sess-1")
    assert state is not None
    assert state.stopped_reason == "max_duration"
    assert state.bump_count == 0
    assert upstream.calls == []

    await scheduler.stop()


async def test_deadline_stops_at_absolute_time(
    tmp_path: Path, clock: FakeClock, store: MemoryStore
) -> None:
    upstream = ScriptedUpstream([])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    scheduler.note_request("sess-1", _snapshot(), ("authorization", "Bearer a"))
    scheduler.set_enabled("sess-1", True, interval_s=1000, until=1005.0)

    await _pump_until(lambda: store.get_state("sess-1").stopped_reason is not None)  # type: ignore[union-attr]

    state = store.get_state("sess-1")
    assert state is not None
    assert state.stopped_reason == "deadline"
    assert state.bump_count == 0
    assert upstream.calls == []
    assert clock.now() == pytest.approx(1005.0)

    await scheduler.stop()


async def test_auto_stop_clears_the_deadline_so_re_enabling_runs_again(
    tmp_path: Path, clock: FakeClock, store: MemoryStore
) -> None:
    """A session stopped by its deadline must not stop again the moment it is re-enabled."""
    upstream = ScriptedUpstream([_ok_response()])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    scheduler.note_request("sess-1", _snapshot(), ("authorization", "Bearer a"))
    scheduler.set_enabled("sess-1", True, interval_s=1000, until=1005.0)
    await _pump_until(lambda: store.get_state("sess-1").stopped_reason is not None)  # type: ignore[union-attr]

    stopped = store.get_state("sess-1")
    assert stopped is not None
    assert stopped.stopped_reason == "deadline"
    assert stopped.until is None

    scheduler.set_enabled("sess-1", True, interval_s=5)
    await _pump_until(lambda: store.get_state("sess-1").bump_count == 1)  # type: ignore[union-attr]

    state = store.get_state("sess-1")
    assert state is not None
    assert state.enabled is True
    assert state.until is None
    assert state.stopped_reason is None

    await scheduler.stop()


async def test_deadline_takes_precedence_over_max_duration(
    tmp_path: Path, clock: FakeClock, store: MemoryStore
) -> None:
    """A generous ``until`` must not be cut short by a smaller ``max_duration_s``."""
    upstream = ScriptedUpstream([_ok_response()])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    scheduler.note_request("sess-1", _snapshot(), ("authorization", "Bearer a"))
    scheduler.set_enabled("sess-1", True, interval_s=5, max_duration_s=2, until=1000.0 + 3600.0)

    await _pump_until(lambda: store.get_state("sess-1").bump_count == 1)  # type: ignore[union-attr]

    state = store.get_state("sess-1")
    assert state is not None
    assert state.stopped_reason is None
    assert state.enabled is True

    await scheduler.stop()


async def test_past_deadline_stops_immediately(
    tmp_path: Path, clock: FakeClock, store: MemoryStore
) -> None:
    upstream = ScriptedUpstream([])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    scheduler.note_request("sess-1", _snapshot(), ("authorization", "Bearer a"))
    scheduler.set_enabled("sess-1", True, interval_s=1000, until=500.0)

    await _pump_until(lambda: store.get_state("sess-1").stopped_reason is not None)  # type: ignore[union-attr]

    state = store.get_state("sess-1")
    assert state is not None
    assert state.stopped_reason == "deadline"
    assert upstream.calls == []

    await scheduler.stop()


async def test_disabling_clears_the_deadline(
    tmp_path: Path, clock: FakeClock, store: MemoryStore
) -> None:
    """``/cache off`` drops ``until``; re-enabling falls back to the max duration."""
    upstream = ScriptedUpstream([])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    scheduler.note_request("sess-1", _snapshot(), ("authorization", "Bearer a"))
    scheduler.set_enabled("sess-1", True, interval_s=1000, until=1005.0)
    disabled = scheduler.set_enabled("sess-1", False)
    assert disabled.until is None
    assert store.get_state("sess-1").until is None  # type: ignore[union-attr]

    reenabled = scheduler.set_enabled("sess-1", True)
    assert reenabled.until is None
    assert reenabled.max_duration_s is None

    await scheduler.stop()


async def test_disabling_keeps_interval_and_max_duration_overrides(
    tmp_path: Path, clock: FakeClock, store: MemoryStore
) -> None:
    """Only ``until`` is cleared on disable; the other overrides survive."""
    upstream = ScriptedUpstream([])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    scheduler.note_request("sess-1", _snapshot(), ("authorization", "Bearer a"))
    scheduler.set_enabled("sess-1", True, interval_s=1000, max_duration_s=7200, until=1005.0)
    disabled = scheduler.set_enabled("sess-1", False)

    assert disabled.interval_s == 1000
    assert disabled.max_duration_s == 7200
    assert disabled.until is None

    await scheduler.stop()


async def test_in_flight_request_defers_bump(
    tmp_path: Path, clock: FakeClock, store: MemoryStore
) -> None:
    upstream = ScriptedUpstream([_ok_response()])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    scheduler.note_request("sess-1", _snapshot(), ("authorization", "Bearer a"))
    scheduler.begin_request("sess-1")
    scheduler.set_enabled("sess-1", True, interval_s=2)

    await _pump_until(lambda: clock.now() >= 1005.0)
    assert store.get_state("sess-1").bump_count == 0  # type: ignore[union-attr]
    assert upstream.calls == []

    scheduler.end_request("sess-1")
    await _pump_until(lambda: store.get_state("sess-1").bump_count == 1)  # type: ignore[union-attr]

    await scheduler.stop()


async def test_bump_now_works(tmp_path: Path, clock: FakeClock, store: MemoryStore) -> None:
    upstream = ScriptedUpstream([_ok_response()])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    scheduler.note_request("sess-1", _snapshot(), ("authorization", "Bearer a"))
    result = await scheduler.bump_now("sess-1")

    assert result.outcome == "ok"
    state = store.get_state("sess-1")
    assert state is not None
    assert state.bump_count == 1
    assert state.last_bump_at == pytest.approx(1000.0)


async def test_bump_now_raises_no_snapshot(
    tmp_path: Path, clock: FakeClock, store: MemoryStore
) -> None:
    upstream = ScriptedUpstream([])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    with pytest.raises(NoSnapshot):
        await scheduler.bump_now("no-such-session")


async def test_forget_removes_session(tmp_path: Path, clock: FakeClock, store: MemoryStore) -> None:
    upstream = ScriptedUpstream([_ok_response()])
    scheduler = Scheduler(_config(tmp_path), store, LargestPrefixSelector(), upstream, clock)

    scheduler.note_request("sess-1", _snapshot(), ("authorization", "Bearer a"))
    scheduler.set_enabled("sess-1", True, interval_s=5)
    await _pump(5)

    scheduler.forget("sess-1")
    await _pump(5)

    assert store.get_state("sess-1") is None
    assert store.get_snapshots("sess-1") == []
    assert scheduler.status_all() == []
