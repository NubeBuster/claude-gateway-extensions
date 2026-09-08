"""Persistence for snapshots and per-session state.

Defines the :class:`SnapshotStore` protocol plus two implementations:
:class:`MemoryStore` (process-local, LRU-capped) and :class:`FileStore`
(one JSON file per snapshot and per session state, under the XDG state
directory, permissioned per the "Plaintext snapshots at 0600, persistence
opt-out" decision in ``docs/DESIGN.md`` "Decisions").
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import Protocol

from claude_cache_bump_gateway.core.snapshot import SessionState, Snapshot, Usage

#: Session ids are used verbatim as filesystem directory names; anything not
#: matching this pattern is rejected before it ever reaches the filesystem.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def _validate_session_id(session_id: str) -> None:
    """Raise ``ValueError`` if ``session_id`` is not a safe filesystem path component."""
    if not SESSION_ID_RE.match(session_id):
        raise ValueError(f"invalid session id: {session_id!r}")


class SnapshotStore(Protocol):
    """Persistence interface for snapshots and session state."""

    def put_snapshot(self, snapshot: Snapshot) -> None:
        """Store or replace the latest snapshot for ``snapshot``'s (session, stream) pair."""
        ...

    def get_snapshots(self, session_id: str) -> list[Snapshot]:
        """Return all snapshots currently held for ``session_id``."""
        ...

    def get_state(self, session_id: str) -> SessionState | None:
        """Return the persisted :class:`SessionState` for ``session_id``, if any."""
        ...

    def put_state(self, state: SessionState) -> None:
        """Store or replace the :class:`SessionState` for its session."""
        ...

    def delete_session(self, session_id: str) -> None:
        """Remove all snapshots and state for ``session_id``."""
        ...

    def list_sessions(self) -> list[str]:
        """Return the ids of every session with stored state or snapshots."""
        ...

    def prune(self, older_than_epoch: float) -> None:
        """Discard snapshots captured before ``older_than_epoch``."""
        ...


class MemoryStore:
    """In-process :class:`SnapshotStore` with an LRU cap per session on stream count."""

    def __init__(self, max_streams_per_session: int) -> None:
        """Create a store retaining at most ``max_streams_per_session`` streams per session."""
        self._max_streams_per_session = max_streams_per_session
        self._snapshots: dict[str, OrderedDict[str, Snapshot]] = {}
        self._states: dict[str, SessionState] = {}

    def put_snapshot(self, snapshot: Snapshot) -> None:
        """Store ``snapshot``, evicting the least-recently-used stream if over capacity."""
        streams = self._snapshots.setdefault(snapshot.session_id, OrderedDict())
        streams.pop(snapshot.stream_key, None)
        streams[snapshot.stream_key] = snapshot
        while len(streams) > self._max_streams_per_session:
            streams.popitem(last=False)

    def get_snapshots(self, session_id: str) -> list[Snapshot]:
        """Return all snapshots for ``session_id``, oldest-used first."""
        return list(self._snapshots.get(session_id, {}).values())

    def get_state(self, session_id: str) -> SessionState | None:
        """Return the in-memory :class:`SessionState` for ``session_id``, if any."""
        return self._states.get(session_id)

    def put_state(self, state: SessionState) -> None:
        """Store ``state`` in memory, replacing any previous state for the same session."""
        self._states[state.session_id] = state

    def delete_session(self, session_id: str) -> None:
        """Drop all snapshots and state held for ``session_id``."""
        self._snapshots.pop(session_id, None)
        self._states.pop(session_id, None)

    def list_sessions(self) -> list[str]:
        """Return the union of session ids with snapshots or state."""
        return sorted(set(self._snapshots) | set(self._states))

    def prune(self, older_than_epoch: float) -> None:
        """Drop snapshots captured before ``older_than_epoch`` from every session."""
        for streams in self._snapshots.values():
            stale = [key for key, snap in streams.items() if snap.captured_at < older_than_epoch]
            for key in stale:
                del streams[key]


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically with mode 0600, via a temp file + rename."""
    tmp_path = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    os.chmod(tmp_path, _FILE_MODE)
    os.replace(tmp_path, path)


def _snapshot_to_json(snapshot: Snapshot) -> dict[str, object]:
    """Serialise ``snapshot`` to a JSON-compatible dict, base64-encoding the body."""
    return {
        "session_id": snapshot.session_id,
        "stream_key": snapshot.stream_key,
        "model": snapshot.model,
        "headers": snapshot.headers,
        "body_b64": base64.b64encode(snapshot.body).decode("ascii"),
        "captured_at": snapshot.captured_at,
        "usage": {
            "input_tokens": snapshot.usage.input_tokens,
            "output_tokens": snapshot.usage.output_tokens,
            "cache_read_input_tokens": snapshot.usage.cache_read_input_tokens,
            "cache_creation_input_tokens": snapshot.usage.cache_creation_input_tokens,
        },
    }


def _snapshot_from_json(data: dict[str, object]) -> Snapshot:
    """Deserialise a snapshot dict previously produced by :func:`_snapshot_to_json`."""
    usage_data = data["usage"]
    assert isinstance(usage_data, dict)
    return Snapshot(
        session_id=str(data["session_id"]),
        stream_key=str(data["stream_key"]),
        model=data["model"],  # type: ignore[arg-type]
        headers=dict(data["headers"]),  # type: ignore[arg-type]
        body=base64.b64decode(str(data["body_b64"])),
        captured_at=float(data["captured_at"]),  # type: ignore[arg-type]
        usage=Usage.from_dict(usage_data),
    )


def _state_to_json(state: SessionState) -> dict[str, object]:
    """Serialise ``state`` to a JSON-compatible dict."""
    return {
        "session_id": state.session_id,
        "enabled": state.enabled,
        "interval_s": state.interval_s,
        "max_duration_s": state.max_duration_s,
        "until": state.until,
        "last_request_at": state.last_request_at,
        "last_bump_at": state.last_bump_at,
        "bump_count": state.bump_count,
        "stopped_reason": state.stopped_reason,
        "last_credential": list(state.last_credential) if state.last_credential else None,
    }


def _state_from_json(data: dict[str, object]) -> SessionState:
    """Deserialise a session-state dict previously produced by :func:`_state_to_json`."""
    credential = data.get("last_credential")
    return SessionState(
        session_id=str(data["session_id"]),
        enabled=bool(data["enabled"]),
        interval_s=data["interval_s"],  # type: ignore[arg-type]
        max_duration_s=data["max_duration_s"],  # type: ignore[arg-type]
        until=data.get("until"),  # type: ignore[arg-type]
        last_request_at=data["last_request_at"],  # type: ignore[arg-type]
        last_bump_at=data["last_bump_at"],  # type: ignore[arg-type]
        bump_count=int(data["bump_count"]),  # type: ignore[arg-type]
        stopped_reason=data["stopped_reason"],  # type: ignore[arg-type]
        last_credential=tuple(credential) if credential else None,  # type: ignore[assignment]
    )


class FileStore:
    """File-backed :class:`SnapshotStore` under an XDG state directory.

    Layout: ``<root>/<session>/<stream>.json`` per snapshot and
    ``<root>/<session>/state.json`` per session state, directories 0700 and
    files 0600. Every session directory is loaded into memory on
    construction; writes go through :func:`_atomic_write` and are mirrored
    to the in-memory cache.
    """

    def __init__(self, root: Path, max_streams_per_session: int) -> None:
        """Create (if needed) ``root`` at 0700 and load any existing snapshots/state."""
        self._root = root
        self._max_streams_per_session = max_streams_per_session
        self._snapshots: dict[str, OrderedDict[str, Snapshot]] = {}
        self._states: dict[str, SessionState] = {}
        self._ensure_dir(self._root)
        self._load_all()

    @staticmethod
    def _ensure_dir(path: Path) -> None:
        """Create ``path`` (and parents) if missing, and enforce mode 0700 on it."""
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, _DIR_MODE)

    def _session_dir(self, session_id: str) -> Path:
        """Return the (validated) directory for ``session_id``, creating it at 0700."""
        _validate_session_id(session_id)
        session_dir = self._root / session_id
        self._ensure_dir(session_dir)
        return session_dir

    def _load_all(self) -> None:
        """Populate the in-memory cache from every session directory under ``root``."""
        for entry in sorted(self._root.iterdir()):
            if not entry.is_dir() or not SESSION_ID_RE.match(entry.name):
                continue
            session_id = entry.name
            state_path = entry / "state.json"
            if state_path.is_file():
                try:
                    data = json.loads(state_path.read_bytes())
                    self._states[session_id] = _state_from_json(data)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
            streams: OrderedDict[str, Snapshot] = OrderedDict()
            snapshot_paths = sorted(
                (p for p in entry.glob("*.json") if p.name != "state.json"),
                key=lambda p: p.stat().st_mtime,
            )
            for snap_path in snapshot_paths:
                try:
                    data = json.loads(snap_path.read_bytes())
                    snapshot = _snapshot_from_json(data)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                streams[snapshot.stream_key] = snapshot
            while len(streams) > self._max_streams_per_session:
                oldest_key, _ = streams.popitem(last=False)
                (entry / f"{oldest_key}.json").unlink(missing_ok=True)
            if streams:
                self._snapshots[session_id] = streams

    def put_snapshot(self, snapshot: Snapshot) -> None:
        """Persist ``snapshot`` to disk and update the in-memory cache, applying the LRU cap."""
        session_dir = self._session_dir(snapshot.session_id)
        streams = self._snapshots.setdefault(snapshot.session_id, OrderedDict())
        streams.pop(snapshot.stream_key, None)
        streams[snapshot.stream_key] = snapshot
        evicted: list[str] = []
        while len(streams) > self._max_streams_per_session:
            oldest_key, _ = streams.popitem(last=False)
            evicted.append(oldest_key)
        payload = json.dumps(_snapshot_to_json(snapshot)).encode("utf-8")
        _atomic_write(session_dir / f"{snapshot.stream_key}.json", payload)
        for key in evicted:
            (session_dir / f"{key}.json").unlink(missing_ok=True)

    def get_snapshots(self, session_id: str) -> list[Snapshot]:
        """Return the cached snapshots for ``session_id``."""
        _validate_session_id(session_id)
        return list(self._snapshots.get(session_id, {}).values())

    def get_state(self, session_id: str) -> SessionState | None:
        """Return the cached :class:`SessionState` for ``session_id``, if any."""
        _validate_session_id(session_id)
        return self._states.get(session_id)

    def put_state(self, state: SessionState) -> None:
        """Persist ``state`` to disk and update the in-memory cache."""
        session_dir = self._session_dir(state.session_id)
        payload = json.dumps(_state_to_json(state)).encode("utf-8")
        _atomic_write(session_dir / "state.json", payload)
        self._states[state.session_id] = state

    def delete_session(self, session_id: str) -> None:
        """Remove ``session_id``'s directory from disk and from the in-memory cache."""
        _validate_session_id(session_id)
        self._snapshots.pop(session_id, None)
        self._states.pop(session_id, None)
        session_dir = self._root / session_id
        if not session_dir.is_dir():
            return
        for child in session_dir.iterdir():
            child.unlink()
        session_dir.rmdir()

    def list_sessions(self) -> list[str]:
        """Return the union of session ids with cached snapshots or state."""
        return sorted(set(self._snapshots) | set(self._states))

    def prune(self, older_than_epoch: float) -> None:
        """Delete on disk and in memory every snapshot captured before ``older_than_epoch``."""
        for session_id, streams in list(self._snapshots.items()):
            stale = [key for key, snap in streams.items() if snap.captured_at < older_than_epoch]
            if not stale:
                continue
            session_dir = self._root / session_id
            for key in stale:
                del streams[key]
                (session_dir / f"{key}.json").unlink(missing_ok=True)
            if not streams:
                del self._snapshots[session_id]


__all__ = [
    "SESSION_ID_RE",
    "FileStore",
    "MemoryStore",
    "SnapshotStore",
]
