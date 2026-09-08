"""Tests for claude_gateway_extensions.core.upstream."""

import pytest
from multidict import CIMultiDict

from claude_gateway_extensions.core.upstream import filter_headers

#: Every test in this file guards one recorded decision; see tests/conftest.py.
pytestmark = pytest.mark.invariant(decision="Byte-transparent forwarding")


def test_filter_headers_preserves_repeated_header_values() -> None:
    """A repeated header name must survive filtering, not collapse to one value."""
    headers = CIMultiDict()
    headers.add("Set-Cookie", "a=1")
    headers.add("Set-Cookie", "b=2")
    headers.add("Connection", "keep-alive")
    headers.add("X-Custom", "value")

    result = filter_headers(headers)

    assert result.getall("set-cookie") == ["a=1", "b=2"]
    assert result["x-custom"] == "value"
    assert "connection" not in result


def test_filter_headers_lower_cases_and_strips_hop_by_hop_from_plain_dict() -> None:
    """A plain dict (no repeats possible) keeps its existing lower-case/strip behaviour."""
    result = filter_headers({"Authorization": "Bearer x", "Host": "example.com"})

    assert result["authorization"] == "Bearer x"
    assert "host" not in result
