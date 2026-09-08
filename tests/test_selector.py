"""Tests for claude_gateway_extensions.core.selector."""

import pytest

from claude_gateway_extensions.core.selector import LargestPrefixSelector
from claude_gateway_extensions.core.snapshot import Snapshot, Usage

#: Every test in this file guards one recorded decision; see tests/conftest.py.
pytestmark = pytest.mark.invariant(
    decision="Streams keyed by model + system + tools, largest prefix wins"
)


def _snap(stream: str, total_input: int, captured_at: float) -> Snapshot:
    return Snapshot(
        session_id="sess-1",
        stream_key=stream,
        model="claude-x",
        headers={},
        body=b"{}",
        captured_at=captured_at,
        usage=Usage(input_tokens=total_input),
    )


def test_select_none_on_empty() -> None:
    assert LargestPrefixSelector().select([]) is None


def test_select_largest_total_input() -> None:
    small = _snap("a", 10, captured_at=1.0)
    large = _snap("b", 1000, captured_at=1.0)
    assert LargestPrefixSelector().select([small, large]) is large


def test_select_ties_prefer_newest() -> None:
    older = _snap("a", 100, captured_at=1.0)
    newer = _snap("b", 100, captured_at=2.0)
    assert LargestPrefixSelector().select([older, newer]) is newer
