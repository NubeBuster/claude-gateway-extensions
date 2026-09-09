"""Tests for claude_gateway_extensions.core.store."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from claude_gateway_extensions.core.snapshot import SessionState, Snapshot, Usage
from claude_gateway_extensions.core.store import FileStore, MemoryStore


def _snapshot(
    session_id: str, stream: str, captured_at: float = 1000.0, is_subagent: bool | None = None
) -> Snapshot:
    return Snapshot(
        session_id=session_id,
        stream_key=stream,
        model="claude-x",
        headers={"authorization": "Bearer t"},
        body=b'{"model": "claude-x"}',
        captured_at=captured_at,
        usage=Usage(input_tokens=1),
        is_subagent=is_subagent,
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX chmod bits do not apply on Windows")
@pytest.mark.invariant(decision="Plaintext snapshots at 0600, persistence opt-out")
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


def test_file_store_round_trips_is_subagent(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = FileStore(root, max_streams_per_session=8)
    store.put_snapshot(_snapshot("sess-1", "stream-a", is_subagent=True))

    reloaded = FileStore(root, max_streams_per_session=8)
    got = reloaded.get_snapshots("sess-1")
    assert len(got) == 1
    assert got[0].is_subagent is True


def test_file_store_loads_snapshot_without_is_subagent_key(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = FileStore(root, max_streams_per_session=8)
    store.put_snapshot(_snapshot("sess-1", "stream-a"))

    snap_path = root / "sess-1" / "stream-a.json"
    data = json.loads(snap_path.read_text())
    del data["is_subagent"]
    snap_path.write_text(json.dumps(data))

    reloaded = FileStore(root, max_streams_per_session=8)
    got = reloaded.get_snapshots("sess-1")
    assert len(got) == 1
    assert got[0].is_subagent is None


def test_file_store_survives_a_platform_that_rejects_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``chmod`` that raises (e.g. an ACL-only filesystem) must not stop a write."""

    def _raise(*args: object, **kwargs: object) -> None:
        raise OSError("no chmod here")

    monkeypatch.setattr(os, "chmod", _raise)
    root = tmp_path / "state"
    store = FileStore(root, max_streams_per_session=8)
    snap = _snapshot("sess-1", "stream-a")
    store.put_snapshot(snap)
    state = SessionState(session_id="sess-1", enabled=True)
    store.put_state(state)
    assert store.get_snapshots("sess-1") == [snap]
    assert store.get_state("sess-1") == state
    assert (root / "sess-1" / "stream-a.json").is_file()


def test_file_store_round_trips_until(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = FileStore(root, max_streams_per_session=8)
    state = SessionState(session_id="sess-1", enabled=True, until=1700006000.0)
    store.put_state(state)

    reloaded = FileStore(root, max_streams_per_session=8)
    got_state = reloaded.get_state("sess-1")
    assert got_state == state
    assert got_state.until == 1700006000.0  # type: ignore[union-attr]


def test_file_store_loads_state_without_until_key(tmp_path: Path) -> None:
    """A ``state.json`` written before ``until`` existed must still load, with ``until=None``."""
    root = tmp_path / "state"
    session_dir = root / "sess-1"
    session_dir.mkdir(parents=True)
    legacy = {
        "session_id": "sess-1",
        "enabled": True,
        "interval_s": None,
        "max_duration_s": None,
        "last_request_at": None,
        "last_bump_at": None,
        "bump_count": 0,
        "stopped_reason": None,
        "last_credential": None,
    }
    (session_dir / "state.json").write_bytes(json.dumps(legacy).encode("utf-8"))

    store = FileStore(root, max_streams_per_session=8)
    state = store.get_state("sess-1")
    assert state is not None
    assert state.until is None


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
@pytest.mark.invariant(decision="Plaintext snapshots at 0600, persistence opt-out")
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
