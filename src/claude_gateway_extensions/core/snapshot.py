"""Data models for captured requests and per-session bump state.

Defines :class:`Usage`, :class:`Snapshot` and :class:`SessionState`, plus the
pure functions used to key and classify a captured ``/v1/messages`` request
body: :func:`stream_key`, :func:`wants_1h_ttl` and :func:`body_is_subagent`.
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
#: bump interval (``CGX_INTERVAL_1H_S``), which is a smaller safety margin
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
    """A captured ``/v1/messages`` request/response pair for one stream.

    ``is_subagent`` is :func:`body_is_subagent`'s three-valued read of
    ``body`` at capture time: ``True``/``False`` for a body that states
    its side of the split via the attribution block, ``None`` for one
    that does not (or could not be parsed) -- never coerced to a
    two-valued default, so a caller that must not treat "unknown" as
    "main conversation" can tell the two apart.
    """

    session_id: str
    stream_key: str
    model: str | None
    headers: dict[str, str]
    body: bytes
    captured_at: float
    usage: Usage
    is_subagent: bool | None = None


@dataclass
class RewarmWrite:
    """Record of the one ``cache_write`` bump a rewarm was allowed to tolerate.

    See the "Rewarm" decision in ``docs/DESIGN.md``. Written once, when the
    tolerated write happens; never cleared afterwards, so it stays visible
    in status as a historical record even after the flag it consumed is
    gone.
    """

    at: float
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
    rewarm: bool = False
    rewarm_write: RewarmWrite | None = None
    replays_seen: int = 0


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


@dataclass(frozen=True)
class Breakpoint:
    """One ``cache_control`` block found in a captured request body.

    ``at`` is a human-readable position label in prompt order (``tools[0]``,
    ``system[2]``, ``messages[1].content[0]``); ``ttl`` is ``"1h"`` or
    ``"5m"`` -- Anthropic's default ephemeral cache when a ``cache_control``
    block omits ``ttl`` entirely.
    """

    at: str
    ttl: str


def _breakpoint_ttl_label(cache_control: Mapping[str, Any]) -> str:
    """Return ``"1h"`` or ``"5m"`` for one ``cache_control`` block's ``ttl``."""
    return "1h" if cache_control.get("ttl") == "1h" else "5m"


def parse_breakpoints(body: bytes) -> list[Breakpoint]:
    """Return every ``cache_control`` block in ``body``, in prompt order.

    The API evaluates a request's cache in prompt order -- ``tools``, then
    ``system``, then ``messages`` -- and 1h breakpoints must precede 5m ones
    within that order, so a caller that needs the *last* (tail) breakpoint,
    or the shortest TTL among them, can rely on this list's order rather
    than re-deriving it. Only ``tools``/``system`` top-level blocks and
    ``messages[i].content[j]`` blocks are considered -- the only places the
    API accepts a ``cache_control``. Raises :class:`SnapshotError` if
    ``body`` is not a JSON object.
    """
    data = _load_json(body)
    if not isinstance(data, dict):
        raise SnapshotError("request body is not a JSON object")
    breakpoints: list[Breakpoint] = []
    tools = data.get("tools")
    if isinstance(tools, list):
        for i, block in enumerate(tools):
            cache_control = block.get("cache_control") if isinstance(block, dict) else None
            if isinstance(cache_control, dict):
                breakpoints.append(Breakpoint(f"tools[{i}]", _breakpoint_ttl_label(cache_control)))
    system = data.get("system")
    if isinstance(system, list):
        for i, block in enumerate(system):
            cache_control = block.get("cache_control") if isinstance(block, dict) else None
            if isinstance(cache_control, dict):
                breakpoints.append(Breakpoint(f"system[{i}]", _breakpoint_ttl_label(cache_control)))
    messages = data.get("messages")
    if isinstance(messages, list):
        for mi, message in enumerate(messages):
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for ci, block in enumerate(content):
                cache_control = block.get("cache_control") if isinstance(block, dict) else None
                if isinstance(cache_control, dict):
                    breakpoints.append(
                        Breakpoint(
                            f"messages[{mi}].content[{ci}]", _breakpoint_ttl_label(cache_control)
                        )
                    )
    return breakpoints


def shortest_breakpoint_ttl_s(breakpoints: list[Breakpoint]) -> int:
    """Return the shortest TTL among ``breakpoints``, in seconds; ``CACHE_TTL_5M_S`` if empty.

    See the "Interval derived from the snapshot's shortest breakpoint TTL"
    decision in ``docs/DESIGN.md``: a body may mix ``1h`` and ``5m``
    breakpoints, and the tail entry a bump has to find alive expires at the
    shortest one present, not the longest.
    """
    if not breakpoints:
        return CACHE_TTL_5M_S
    return min(CACHE_TTL_1H_S if bp.ttl == "1h" else CACHE_TTL_5M_S for bp in breakpoints)


def effective_ttl_label(breakpoints: list[Breakpoint]) -> str:
    """Return ``"1h"`` or ``"5m"`` for :func:`shortest_breakpoint_ttl_s` of ``breakpoints``."""
    return "1h" if shortest_breakpoint_ttl_s(breakpoints) == CACHE_TTL_1H_S else "5m"


#: Marker text of the attribution/billing block Claude Code prepends as
#: ``system[0]`` (a text block, not an HTTP header, despite the name).
_ATTRIBUTION_HEADER_PREFIX = "x-anthropic-billing-header:"

#: The attribution block's own statement of which side of the main/subagent
#: split a request came from -- the only thing on the wire that states it;
#: nothing distinguishes a subagent's request from its parent's by header,
#: model, or session id.
_SUBAGENT_ATTRIBUTION_MARKER = "cc_is_subagent=true"


def _is_attribution_block(block: object) -> bool:
    """Whether ``block`` is the CLI's attribution/billing ``system`` block.

    Shape-based: recognised by its ``x-anthropic-billing-header:`` text, so
    a session whose ``system[0]`` is an ordinary preamble is never mistaken
    for it. Only ``system[0]`` is ever considered by :func:`body_is_subagent`
    -- that is where the client prepends it, so a lookalike deeper in
    ``system`` is content the user put there.
    """
    if not isinstance(block, dict):
        return False
    if block.get("type") not in (None, "text"):
        return False
    text = block.get("text")
    return isinstance(text, str) and text.lstrip().startswith(_ATTRIBUTION_HEADER_PREFIX)


def body_is_subagent(body: bytes) -> bool | None:
    """Whether a captured body is subagent traffic, or ``None`` if unreadable.

    Three-valued on purpose, and callers must keep the third value apart
    from the other two: ``None`` means "this body did not parse, or carries
    no attribution block", never "it is a main-conversation request". A
    guard that read an unparseable body as ``False`` would let exactly the
    traffic it cannot inspect through the check it exists to apply.

    Read off the attribution block rather than any header, because that
    block is where the CLI states it -- see `_SUBAGENT_ATTRIBUTION_MARKER`.
    """
    try:
        data = _load_json(body)
    except SnapshotError:
        return None
    if not isinstance(data, dict):
        return None
    system = data.get("system")
    if not isinstance(system, list) or not system:
        return None
    block = system[0]
    if not _is_attribution_block(block):
        return None
    return _SUBAGENT_ATTRIBUTION_MARKER in block.get("text", "")
