"""Building and classifying a shadow review fork of a captured request.

A fork replays a session's captured snapshot once with a single extra user
turn appended, asking a reviewer model a question about the parent session
at the cost of a cache read plus the twin's own output tokens. See the
"Shadow review fork" section in ``docs/DESIGN.md`` for the full design.

:func:`build_fork_body` turns a snapshot body plus a review prompt/payload
into the exact request to replay. :func:`parse_verdict` turns the response
into a :class:`ForkVerdict`, failing open on anything it cannot trust.
:func:`fork_once` ties transport and both of the above together for the
scheduler.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from claude_gateway_extensions.core.bump import build_bump_headers
from claude_gateway_extensions.core.snapshot import Snapshot, Usage
from claude_gateway_extensions.core.upstream import Upstream

logger = logging.getLogger(__name__)

ForkOutcome = Literal["ok", "intervene"]

_FORK_PATH = "/v1/messages"


def build_fork_body(body: bytes, prompt: str, payload: object, max_tokens: int) -> bytes:
    """Return ``body`` re-serialised as a one-shot review fork request.

    ``max_tokens`` is forced to ``max_tokens`` and ``stream`` is set to
    ``false`` only if the key is already present, exactly as
    :func:`~.bump.build_bump_body` treats it. ``tools`` and ``tool_choice``
    are preserved by value, not dropped: Anthropic hashes the cache prefix
    over ``tools``, then ``system``, then ``messages``, so removing either
    key invalidates the whole prefix and turns every fork into a full
    ``cache_creation`` write. One user message is appended: ``prompt``
    followed by ``payload`` pretty-printed as a fenced JSON block. A twin
    must still never act on the parent's behalf, so that guarantee now
    comes from the prompt instructing the reviewer never to call a tool,
    plus :func:`parse_verdict` treating a ``tool_use`` reply as fail-open.
    Everything else, including every nested ``cache_control`` block, is
    preserved by value exactly as :func:`~.bump.build_bump_body` preserves
    it, so the fork hits the same cached prefix.
    """
    data = json.loads(body)
    data["max_tokens"] = max_tokens
    if "stream" in data:
        data["stream"] = False
    payload_text = json.dumps(payload, indent=2, ensure_ascii=False)
    content = f"{prompt}\n\n```json\n{payload_text}\n```"
    messages = data.setdefault("messages", [])
    messages.append({"role": "user", "content": content})
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass
class ForkVerdict:
    """The outcome of one shadow review fork."""

    verdict: Literal["ok", "intervene"]
    reason: str
    mutation: dict[str, object] | None
    usage: Usage | None
    paid_write: bool


def _fail_open(reason: str, usage: Usage | None = None) -> ForkVerdict:
    """Return the fail-open ``ok`` verdict every unparseable/failed reply falls back to."""
    paid_write = usage is not None and usage.cache_creation_input_tokens > 0
    return ForkVerdict(
        verdict="ok", reason=reason, mutation=None, usage=usage, paid_write=paid_write
    )


def _extract_usage(data: object) -> Usage | None:
    """Return a :class:`Usage` read from a response dict's ``usage`` block, if any."""
    if not isinstance(data, dict):
        return None
    raw_usage = data.get("usage")
    if not isinstance(raw_usage, dict):
        return None
    return Usage.from_dict(raw_usage)


def _has_tool_use(data: Mapping[str, object]) -> bool:
    """Return whether ``content`` carries any block of type ``tool_use``."""
    content = data.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content)


def _extract_text(data: Mapping[str, object]) -> str | None:
    """Concatenate every ``type: text`` content block's ``text``, or ``None`` if unreadable."""
    content = data.get("content")
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if not isinstance(text, str):
                return None
            parts.append(text)
    return "".join(parts)


def parse_verdict(status: int, body: bytes) -> ForkVerdict:
    """Classify a fork response, failing open on anything unconfirmed.

    Anything at all unexpected -- a non-200 status, a body that cannot be
    parsed as the reply's JSON, text content blocks that cannot be
    concatenated, a verdict payload that is not strict JSON, or one whose
    ``verdict``/``reason``/``mutation`` fields do not match the fixed
    contract -- yields the fail-open ``ok`` verdict rather than raising or
    blocking the caller. This fail-open direction is an invariant: a broken
    template or a chatty model must never block the user's work.
    """
    if status != 200:
        logger.warning("fork request failed: status=%s", status)
        return _fail_open("fork_failed")

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("fork verdict unparseable: reason=unparseable_verdict")
        return _fail_open("unparseable_verdict")

    usage = _extract_usage(data)

    if not isinstance(data, dict):
        logger.warning("fork verdict unparseable: reason=unparseable_verdict")
        return _fail_open("unparseable_verdict", usage)

    if _has_tool_use(data):
        logger.warning("fork verdict rejected: reason=tool_use_reply")
        return _fail_open("tool_use_reply", usage)

    text = _extract_text(data)
    if text is None:
        logger.warning("fork verdict unparseable: reason=unparseable_verdict")
        return _fail_open("unparseable_verdict", usage)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("fork verdict unparseable: reason=unparseable_verdict")
        return _fail_open("unparseable_verdict", usage)

    if not isinstance(parsed, dict):
        logger.warning("fork verdict unparseable: reason=unparseable_verdict")
        return _fail_open("unparseable_verdict", usage)

    verdict = parsed.get("verdict")
    reason = parsed.get("reason")
    mutation = parsed.get("mutation")

    if verdict not in ("ok", "intervene"):
        logger.warning("fork verdict unparseable: reason=unparseable_verdict")
        return _fail_open("unparseable_verdict", usage)
    if not isinstance(reason, str):
        logger.warning("fork verdict unparseable: reason=unparseable_verdict")
        return _fail_open("unparseable_verdict", usage)
    if mutation is not None and not isinstance(mutation, dict):
        logger.warning("fork verdict unparseable: reason=unparseable_verdict")
        return _fail_open("unparseable_verdict", usage)

    paid_write = usage is not None and usage.cache_creation_input_tokens > 0
    return ForkVerdict(
        verdict=verdict, reason=reason, mutation=mutation, usage=usage, paid_write=paid_write
    )


async def fork_once(
    upstream: Upstream,
    upstream_base: str,
    snapshot: Snapshot,
    credential: tuple[str, str] | None,
    prompt: str,
    payload: object,
    max_tokens: int,
) -> ForkVerdict:
    """Replay ``snapshot`` as a review fork against ``upstream_base`` and classify the reply.

    Network exceptions are caught and turned into the fail-open ``ok``
    verdict so callers never need a separate exception path.
    """
    url = f"{upstream_base.rstrip('/')}{_FORK_PATH}"
    headers = build_bump_headers(snapshot.headers, credential)
    body = build_fork_body(snapshot.body, prompt, payload, max_tokens)
    try:
        response = await upstream.send("POST", url, headers, body)
    except Exception:
        logger.warning("fork request failed: reason=fork_failed")
        return _fail_open("fork_failed")
    return parse_verdict(response.status, response.body)
