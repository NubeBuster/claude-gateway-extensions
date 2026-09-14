"""The fixed set of Shadow Fork "fail-open" reason codes.

A leaf module with no other project imports (and, critically, no
``aiohttp``): :mod:`~claude_gateway_extensions.core.shadow_fork` and
:mod:`~claude_gateway_extensions.core.scheduler` both produce these codes
and both already pull in ``aiohttp`` transitively via
:mod:`~claude_gateway_extensions.core.upstream`, but
:mod:`~claude_gateway_extensions.cli.shadow_fork` -- the hook-side entry
point every hook invocation runs -- must not, per the "every CLI subcommand
... starts instantly" invariant in ``cli/client.py``. This module is the
one place both sides import the set from, so neither daemon module has to
duplicate the list and the CLI never has to import either of them for it.
"""

from __future__ import annotations

REASON_UNPARSEABLE_VERDICT = "unparseable_verdict"
REASON_FORK_FAILED = "fork_failed"
REASON_TOOL_USE_REPLY = "tool_use_reply"
REASON_THINKING_UNSAFE_MODEL = "thinking_unsafe_model"
REASON_THINKING_MODEL_UNKNOWN = "thinking_model_unknown"
REASON_NO_SNAPSHOT = "no_snapshot"
REASON_RATE_LIMITED = "rate_limited"
REASON_SNAPSHOT_STALE = "snapshot_stale"

#: Every reason code an ``ok`` verdict carries when the fork did not
#: actually run to completion -- as opposed to a genuine "no concerns"
#: answer from the reviewer model, whose own ``reason`` is arbitrary free
#: text and never one of these fixed codes. See the "Visible fail-open"
#: decision in ``docs/DESIGN.md``: ``cli/shadow_fork.py`` surfaces a verdict
#: whose ``reason`` is in this set as a skip ``systemMessage`` instead of
#: staying silent, and never logs or exposes anything else about the fork.
FAIL_OPEN_REASONS = frozenset(
    {
        REASON_UNPARSEABLE_VERDICT,
        REASON_FORK_FAILED,
        REASON_TOOL_USE_REPLY,
        REASON_THINKING_UNSAFE_MODEL,
        REASON_THINKING_MODEL_UNKNOWN,
        REASON_NO_SNAPSHOT,
        REASON_RATE_LIMITED,
        REASON_SNAPSHOT_STALE,
    }
)

__all__ = [
    "FAIL_OPEN_REASONS",
    "REASON_FORK_FAILED",
    "REASON_NO_SNAPSHOT",
    "REASON_RATE_LIMITED",
    "REASON_SNAPSHOT_STALE",
    "REASON_THINKING_MODEL_UNKNOWN",
    "REASON_THINKING_UNSAFE_MODEL",
    "REASON_TOOL_USE_REPLY",
    "REASON_UNPARSEABLE_VERDICT",
]
