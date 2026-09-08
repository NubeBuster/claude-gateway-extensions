"""Data models for captured requests and per-session bump state.

Defines :class:`Usage`, :class:`Snapshot` and :class:`SessionState`, plus the
two pure functions used to key and classify a captured ``/v1/messages``
request body: :func:`stream_key` and :func:`wants_1h_ttl`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any


class SnapshotError(ValueError):
    """Raised when a captured request body cannot be parsed as JSON."""


#: Anthropic's actual prompt-cache TTL, in seconds, for a snapshot whose
#: ``cache_control.ttl`` is ``"1h"``. Distinct from the daemon's configured
#: bump interval (``CCBG_INTERVAL_1H_S``), which is a smaller safety margin
#: under this TTL, not the TTL itself.
CACHE_TTL_1H_S = 3600

#: Anthropic's actual prompt-cache TTL, in seconds, for a snapshot that does
#: not request ``"1h"`` (the default 5-minute ephemeral cache).
CACHE_TTL_5M_S = 300


@dataclass
class Usage:
    """Token usage reported in a ``/v1/messages`` response ``usage`` block."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def total_input(self) -> int:
        """Return the sum of fresh, cache-read and cache-creation input tokens."""
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Usage:
        """Build a :class:`Usage` from a response ``usage`` dict, defaulting missing fields to 0."""
        if data is None:
            return cls()
        return cls(
            input_tokens=int(data.get("input_tokens", 0) or 0),
            output_tokens=int(data.get("output_tokens", 0) or 0),
            cache_read_input_tokens=int(data.get("cache_read_input_tokens", 0) or 0),
            cache_creation_input_tokens=int(data.get("cache_creation_input_tokens", 0) or 0),
        )


@dataclass
class Snapshot:
    """A captured ``/v1/messages`` request/response pair for one stream."""

    session_id: str
    stream_key: str
    model: str | None
    headers: dict[str, str]
    body: bytes
    captured_at: float
    usage: Usage


@dataclass
class SessionState:
    """Per-session mutable bump state, persisted next to its snapshots."""

    session_id: str
    enabled: bool = False
    interval_s: int | None = None
    max_duration_s: int | None = None
    until: float | None = None
    last_request_at: float | None = None
    last_bump_at: float | None = None
    bump_count: int = 0
    stopped_reason: str | None = None
    last_credential: tuple[str, str] | None = field(default=None)


def _load_json(body: bytes) -> Any:
    """Parse ``body`` as JSON, raising :class:`SnapshotError` on failure."""
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SnapshotError(f"invalid JSON request body: {exc}") from exc


def stream_key(body: bytes) -> str:
    """Return the stream key for a request body: sha256 hex of its canonical (model, system, tools).

    Missing fields are treated as ``null``. Raises :class:`SnapshotError` if
    ``body`` is not valid JSON.
    """
    data = _load_json(body)
    if not isinstance(data, dict):
        raise SnapshotError("request body is not a JSON object")
    canonical = {
        "model": data.get("model"),
        "system": data.get("system"),
        "tools": data.get("tools"),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _iter_dicts(value: Any) -> Iterator[dict[str, Any]]:
    """Yield every dict nested anywhere within ``value`` (including ``value`` itself)."""
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _iter_dicts(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_dicts(item)


def wants_1h_ttl(body: bytes) -> bool:
    """Return ``True`` if any nested dict's ``cache_control.ttl`` equals ``"1h"``.

    Raises :class:`SnapshotError` if ``body`` is not valid JSON.
    """
    data = _load_json(body)
    for node in _iter_dicts(data):
        cache_control = node.get("cache_control")
        if isinstance(cache_control, dict) and cache_control.get("ttl") == "1h":
            return True
    return False
