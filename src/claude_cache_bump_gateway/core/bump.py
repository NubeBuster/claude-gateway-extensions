"""Building and classifying a cache-warming replay of a captured request.

:func:`build_bump_body` and :func:`build_bump_headers` turn a
:class:`~claude_cache_bump_gateway.core.snapshot.Snapshot` into the exact
request to replay. :func:`classify` turns the response into a
:class:`BumpResult` per the outcome table in ``docs/DESIGN.md`` "Bump
protocol". :func:`bump_once` ties transport, building and classification
together for the scheduler.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from claude_cache_bump_gateway.core.snapshot import Snapshot, Usage
from claude_cache_bump_gateway.core.upstream import Upstream, filter_headers

BumpOutcome = Literal["ok", "cache_write", "unauthorized", "rejected", "transient"]

_DETAIL_EXCERPT_LIMIT = 500
_BUMP_PATH = "/v1/messages"


def build_bump_body(body: bytes) -> bytes:
    """Return ``body`` re-serialised with ``max_tokens`` forced to 0 and streaming disabled.

    Only ``max_tokens`` (always set) and ``stream`` (set to ``false`` only if
    the key is already present) change; every other field, including nested
    ``cache_control`` blocks, is preserved byte-for-byte in value.
    """
    data = json.loads(body)
    data["max_tokens"] = 0
    if "stream" in data:
        data["stream"] = False
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def build_bump_headers(
    snapshot_headers: Mapping[str, str], credential: tuple[str, str] | None
) -> dict[str, str]:
    """Return replay headers: hop-by-hop stripped, credential swapped, content-type ensured."""
    headers = filter_headers(snapshot_headers)
    if credential is not None:
        name, value = credential
        headers[name.lower()] = value
    if "content-type" not in headers:
        headers["content-type"] = "application/json"
    return headers


@dataclass
class BumpResult:
    """The outcome of replaying one snapshot."""

    outcome: BumpOutcome
    status: int | None
    usage: Usage | None
    detail: str
    stream_key: str | None
    model: str | None


def _excerpt(body: bytes) -> str:
    """Return up to 500 characters of ``body`` decoded as UTF-8 (best effort)."""
    text = body.decode("utf-8", errors="replace")
    return text[:_DETAIL_EXCERPT_LIMIT]


def classify(status: int, body_bytes: bytes) -> BumpResult:
    """Classify a bump response per ``docs/DESIGN.md`` "Bump protocol"."""
    if status == 200:
        usage = None
        try:
            data = json.loads(body_bytes)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            usage = Usage.from_dict(data.get("usage"))
        if usage is not None and usage.cache_creation_input_tokens > 0:
            return BumpResult(
                outcome="cache_write",
                status=status,
                usage=usage,
                detail="cache write occurred on replay",
                stream_key=None,
                model=None,
            )
        return BumpResult(
            outcome="ok", status=status, usage=usage, detail="", stream_key=None, model=None
        )
    if status in (401, 403):
        return BumpResult(
            outcome="unauthorized",
            status=status,
            usage=None,
            detail=_excerpt(body_bytes),
            stream_key=None,
            model=None,
        )
    if 400 <= status < 500:
        return BumpResult(
            outcome="rejected",
            status=status,
            usage=None,
            detail=_excerpt(body_bytes),
            stream_key=None,
            model=None,
        )
    return BumpResult(
        outcome="transient",
        status=status,
        usage=None,
        detail=_excerpt(body_bytes),
        stream_key=None,
        model=None,
    )


async def bump_once(
    upstream: Upstream,
    upstream_base: str,
    snapshot: Snapshot,
    credential: tuple[str, str] | None,
) -> BumpResult:
    """Replay ``snapshot`` against ``upstream_base`` and classify the outcome.

    Network exceptions are caught and turned into a ``transient`` result so
    callers never need a separate exception path.
    """
    url = f"{upstream_base.rstrip('/')}{_BUMP_PATH}"
    headers = build_bump_headers(snapshot.headers, credential)
    body = build_bump_body(snapshot.body)
    try:
        response = await upstream.send("POST", url, headers, body)
    except Exception as exc:
        return BumpResult(
            outcome="transient",
            status=None,
            usage=None,
            detail=str(exc),
            stream_key=snapshot.stream_key,
            model=snapshot.model,
        )
    result = classify(response.status, response.body)
    return dataclasses.replace(result, stream_key=snapshot.stream_key, model=snapshot.model)
