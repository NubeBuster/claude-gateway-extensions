"""Choosing which captured stream to replay for a session's bump.

Defines the :class:`TargetSelector` protocol and the default
:class:`LargestPrefixSelector`, which prefers the stream whose last response
reported the largest total input token count.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from claude_cache_bump_gateway.core.snapshot import Snapshot


class TargetSelector(Protocol):
    """Chooses which of a session's snapshots to replay for a bump."""

    def select(self, snapshots: Sequence[Snapshot]) -> Snapshot | None:
        """Return the snapshot to bump, or ``None`` if ``snapshots`` is empty."""
        ...


class LargestPrefixSelector:
    """Default :class:`TargetSelector`.

    Picks the snapshot with the largest ``usage.total_input``, breaking ties
    in favour of the most recently captured snapshot.
    """

    def select(self, snapshots: Sequence[Snapshot]) -> Snapshot | None:
        """Return the snapshot with the largest total input tokens, newest first on ties."""
        if not snapshots:
            return None
        return max(snapshots, key=lambda snap: (snap.usage.total_input, snap.captured_at))
