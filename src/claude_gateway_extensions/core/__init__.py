"""Core domain layer: models, protocols and their default implementations.

Re-exports the four extension-point protocols named in ``docs/DESIGN.md``
"Extension points" (:class:`Clock`, :class:`SnapshotStore`,
:class:`TargetSelector`, :class:`Upstream`) plus the models and functions
most callers need, so ``server/`` and ``cli/`` can do
``from claude_gateway_extensions.core import Scheduler, FileStore, ...``
instead of reaching into individual submodules.
"""

from claude_gateway_extensions.core.bump import (
    BumpOutcome,
    BumpResult,
    build_bump_body,
    build_bump_headers,
    bump_once,
    classify,
)
from claude_gateway_extensions.core.clock import Clock, FakeClock, SystemClock
from claude_gateway_extensions.core.scheduler import NoSnapshot, Scheduler
from claude_gateway_extensions.core.selector import LargestPrefixSelector, TargetSelector
from claude_gateway_extensions.core.snapshot import (
    CACHE_TTL_1H_S,
    CACHE_TTL_5M_S,
    Breakpoint,
    SessionState,
    Snapshot,
    SnapshotError,
    Usage,
    effective_ttl_label,
    parse_breakpoints,
    shortest_breakpoint_ttl_s,
    stream_key,
    wants_1h_ttl,
)
from claude_gateway_extensions.core.status_format import (
    format_clock,
    format_countdown,
    format_status_times,
)
from claude_gateway_extensions.core.store import FileStore, MemoryStore, SnapshotStore
from claude_gateway_extensions.core.upstream import (
    HOP_BY_HOP_HEADERS,
    AiohttpUpstream,
    Upstream,
    UpstreamResponse,
    filter_headers,
)

__all__ = [
    "CACHE_TTL_1H_S",
    "CACHE_TTL_5M_S",
    "HOP_BY_HOP_HEADERS",
    "AiohttpUpstream",
    "Breakpoint",
    "BumpOutcome",
    "BumpResult",
    "Clock",
    "FakeClock",
    "FileStore",
    "LargestPrefixSelector",
    "MemoryStore",
    "NoSnapshot",
    "Scheduler",
    "SessionState",
    "Snapshot",
    "SnapshotError",
    "SnapshotStore",
    "SystemClock",
    "TargetSelector",
    "Upstream",
    "UpstreamResponse",
    "Usage",
    "build_bump_body",
    "build_bump_headers",
    "bump_once",
    "classify",
    "effective_ttl_label",
    "filter_headers",
    "format_clock",
    "format_countdown",
    "format_status_times",
    "parse_breakpoints",
    "shortest_breakpoint_ttl_s",
    "stream_key",
    "wants_1h_ttl",
]
