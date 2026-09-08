"""Tests for claude_cache_bump_gateway.core.bump."""

import gzip
import json
import zlib

from claude_cache_bump_gateway.core.bump import (
    build_bump_body,
    build_bump_headers,
    classify,
)
from claude_cache_bump_gateway.core.upstream import decode_body


def test_build_bump_body_sets_max_tokens_zero_and_stream_false() -> None:
    original = {
        "model": "claude-x",
        "max_tokens": 4096,
        "stream": True,
        "system": [
            {
                "type": "text",
                "text": "héllo",
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ],
        "messages": [{"role": "user", "content": "hi 世界"}],
        "tools": [{"name": "bash", "cache_control": {"type": "ephemeral"}}],
    }
    body = json.dumps(original).encode("utf-8")

    bumped = build_bump_body(body)
    data = json.loads(bumped)

    assert data["max_tokens"] == 0
    assert data["stream"] is False
    # Everything else, including unicode and cache_control, is untouched.
    assert data["model"] == original["model"]
    assert data["system"] == original["system"]
    assert data["messages"] == original["messages"]
    assert data["tools"] == original["tools"]
    # Unicode preserved literally (ensure_ascii=False).
    assert "héllo" in bumped.decode("utf-8")
    assert "世界" in bumped.decode("utf-8")


def test_build_bump_body_no_stream_key_stays_absent() -> None:
    body = json.dumps({"model": "m", "max_tokens": 10}).encode("utf-8")
    data = json.loads(build_bump_body(body))
    assert "stream" not in data
    assert data["max_tokens"] == 0


def test_build_bump_headers_strips_hop_by_hop_and_sets_content_type() -> None:
    headers = {
        "Authorization": "Bearer old",
        "Connection": "keep-alive",
        "Content-Length": "123",
        "X-Claude-Code-Session-Id": "sess-1",
    }
    result = build_bump_headers(headers, credential=None)
    assert "connection" not in result
    assert "content-length" not in result
    assert result["authorization"] == "Bearer old"
    assert result["x-claude-code-session-id"] == "sess-1"
    assert result["content-type"] == "application/json"


def test_build_bump_headers_replaces_credential() -> None:
    headers = {"authorization": "Bearer old", "content-type": "application/json"}
    result = build_bump_headers(headers, credential=("authorization", "Bearer new"))
    assert result["authorization"] == "Bearer new"
    assert result["content-type"] == "application/json"


def test_classify_ok() -> None:
    body = json.dumps({"usage": {"cache_creation_input_tokens": 0, "input_tokens": 5}}).encode()
    result = classify(200, body)
    assert result.outcome == "ok"
    assert result.usage is not None
    assert result.usage.input_tokens == 5


def test_classify_cache_write() -> None:
    body = json.dumps({"usage": {"cache_creation_input_tokens": 100}}).encode()
    result = classify(200, body)
    assert result.outcome == "cache_write"


def test_classify_unauthorized() -> None:
    assert classify(401, b"{}").outcome == "unauthorized"
    assert classify(403, b"{}").outcome == "unauthorized"


def test_classify_rejected() -> None:
    result = classify(400, b'{"error": "bad request"}')
    assert result.outcome == "rejected"
    assert "bad request" in result.detail


def test_classify_rejected_excerpt_capped_at_500_chars() -> None:
    body = b"x" * 1000
    result = classify(422, body)
    assert result.outcome == "rejected"
    assert len(result.detail) == 500


def test_classify_transient_on_5xx() -> None:
    assert classify(500, b"boom").outcome == "transient"
    assert classify(503, b"boom").outcome == "transient"


def test_build_bump_headers_pins_a_decodable_accept_encoding() -> None:
    """A replay must not ask for an encoding this code cannot decode (br, zstd)."""
    headers = build_bump_headers({"accept-encoding": "br, zstd", "content-type": "text/x"}, None)
    assert headers["accept-encoding"] == "gzip"
    assert headers["content-type"] == "text/x"


def test_decode_body_handles_gzip_deflate_and_passes_others_through() -> None:
    payload = b'{"usage": {"cache_creation_input_tokens": 7}}'
    assert decode_body(gzip.compress(payload), "gzip") == payload
    assert decode_body(zlib.compress(payload), "deflate") == payload
    assert decode_body(payload, "identity") == payload
    assert decode_body(payload, None) == payload
    # Unknown or corrupt encodings are returned untouched rather than raising.
    assert decode_body(payload, "br") == payload
    assert decode_body(b"not gzip", "gzip") == b"not gzip"


def test_classify_reads_usage_from_a_gzipped_body_via_decode() -> None:
    """The cache_write safety valve must survive a compressed upstream response."""
    payload = json.dumps({"usage": {"cache_creation_input_tokens": 12}}).encode()
    decoded = decode_body(gzip.compress(payload), "gzip")
    assert classify(200, decoded).outcome == "cache_write"


def test_classify_survives_a_body_it_cannot_decode() -> None:
    """An undecodable 200 body must not raise, and must fail closed as transient.

    An unconfirmed bump must never reset the timer as though the cache read
    succeeded, so a body this code cannot parse is transient, not ok.
    """
    result = classify(200, gzip.compress(b'{"usage": {}}'))
    assert result.outcome == "transient"
    assert result.usage is None


def test_classify_transient_when_usage_missing_from_200_body() -> None:
    """A 200 body with no usage block must fail closed rather than reset the timer."""
    result = classify(200, b'{"id": "msg_1"}')
    assert result.outcome == "transient"
    assert result.usage is None


def test_classify_transient_when_usage_is_null() -> None:
    """An explicit ``usage: null`` is just as unreadable as a missing key."""
    result = classify(200, b'{"usage": null}')
    assert result.outcome == "transient"
    assert result.usage is None
