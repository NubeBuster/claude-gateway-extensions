"""Tests for claude_cache_bump_gateway.core.store."""

import stat
from pathlib import Path

import pytest

from claude_cache_bump_gateway.core.snapshot import SessionState, Snapshot, Usage
from claude_cache_bump_gateway.core.store import FileStore, MemoryStore


def _snapshot(session_id: str, stream: str, captured_at: float = 1000.0) -> Snapshot:
    return Snapshot(
        session_id=session_id,
        stream_key=stream,
        model="claude-x",
        headers={"authorization": "Bearer t"},
        body=b'{"model": "claude-x"}',
        captured_at=captured_at,
        usage=Usage(input_tokens=1),
    )


def test_memory_store_round_trip() -> None:
    store = MemoryStore(max_streams_per_session=8)
    snap = _snapshot("sess-1", "stream-a")
    store.put_snapshot(snap)
    assert store.get_snapshots("sess-1") == [snap]

    state = SessionState(session_id="sess-1", enabled=True)
    store.put_state(state)
    assert store.get_state("sess-1") == state
    assert store.list_sessions() == ["sess-1"]

    store.delete_session("sess-1")
    assert store.get_snapshots("sess-1") == []
    assert store.get_state("sess-1") is None


def test_memory_store_lru_cap() -> None:
    store = MemoryStore(max_streams_per_session=2)
    store.put_snapshot(_snapshot("sess-1", "a"))
    store.put_snapshot(_snapshot("sess-1", "b"))
    store.put_snapshot(_snapshot("sess-1", "c"))
    remaining = {s.stream_key for s in store.get_snapshots("sess-1")}
    assert remaining == {"b", "c"}


def test_memory_store_prune() -> None:
    store = MemoryStore(max_streams_per_session=8)
    store.put_snapshot(_snapshot("sess-1", "a", captured_at=100.0))
    store.put_snapshot(_snapshot("sess-1", "b", captured_at=200.0))
    store.prune(older_than_epoch=150.0)
    remaining = {s.stream_key for s in store.get_snapshots("sess-1")}
    assert remaining == {"b"}


def test_file_store_permissions_and_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = FileStore(root, max_streams_per_session=8)
    snap = _snapshot("sess-1", "stream-a")
    store.put_snapshot(snap)
    state = SessionState(session_id="sess-1", enabled=True, last_credential=("authorization", "x"))
    store.put_state(state)

    session_dir = root / "sess-1"
    assert stat.S_IMODE(session_dir.stat().st_mode) == 0o700
    snap_path = session_dir / "stream-a.json"
    state_path = session_dir / "state.json"
    assert stat.S_IMODE(snap_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600

    # Reload from disk in a fresh store instance.
    reloaded = FileStore(root, max_streams_per_session=8)
    got_snapshots = reloaded.get_snapshots("sess-1")
    assert len(got_snapshots) == 1
    assert got_snapshots[0].session_id == "sess-1"
    assert got_snapshots[0].stream_key == "stream-a"
    assert got_snapshots[0].body == snap.body
    got_state = reloaded.get_state("sess-1")
    assert got_state == state


def test_file_store_lru_cap(tmp_path: Path) -> None:
    store = FileStore(tmp_path / "state", max_streams_per_session=2)
    store.put_snapshot(_snapshot("sess-1", "a", captured_at=1))
    store.put_snapshot(_snapshot("sess-1", "b", captured_at=2))
    store.put_snapshot(_snapshot("sess-1", "c", captured_at=3))
    remaining = {s.stream_key for s in store.get_snapshots("sess-1")}
    assert remaining == {"b", "c"}
    on_disk = {p.stem for p in (tmp_path / "state" / "sess-1").glob("*.json") if p.stem != "state"}
    assert on_disk == {"b", "c"}


def test_file_store_delete_session(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = FileStore(root, max_streams_per_session=8)
    store.put_snapshot(_snapshot("sess-1", "a"))
    store.put_state(SessionState(session_id="sess-1"))
    store.delete_session("sess-1")
    assert store.get_snapshots("sess-1") == []
    assert store.get_state("sess-1") is None
    assert not (root / "sess-1").exists()


@pytest.mark.parametrize("bad_id", ["../escape", "a/b", "", "x" * 129, "sp ace"])
def test_file_store_rejects_invalid_session_id(tmp_path: Path, bad_id: str) -> None:
    store = FileStore(tmp_path / "state", max_streams_per_session=8)
    with pytest.raises(ValueError):
        store.put_state(SessionState(session_id=bad_id))


def test_file_store_prune_removes_files(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = FileStore(root, max_streams_per_session=8)
    store.put_snapshot(_snapshot("sess-1", "a", captured_at=100.0))
    store.put_snapshot(_snapshot("sess-1", "b", captured_at=200.0))
    store.prune(older_than_epoch=150.0)
    assert {s.stream_key for s in store.get_snapshots("sess-1")} == {"b"}
    assert not (root / "sess-1" / "a.json").exists()
    assert (root / "sess-1" / "b.json").exists()
