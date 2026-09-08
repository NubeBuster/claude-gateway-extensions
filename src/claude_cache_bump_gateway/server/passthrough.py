"""Transparent forwarding, hop-by-hop filtering, SSE streaming and tee capture.

A single catch-all handler forwards every method and path to
``config.upstream`` unbuffered, per ``docs/DESIGN.md`` "Request flow". Only
``POST /v1/messages`` is treated specially: the request body is read fully
(Claude Code sends it as one buffer anyway) and the response is tee'd as it
streams so a :class:`~claude_cache_bump_gateway.core.snapshot.Snapshot` can be
captured for the session's bump scheduler. Every other request passes
through in both directions without inspection.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
from aiohttp import web

from claude_cache_bump_gateway.config import Config
from claude_cache_bump_gateway.core.clock import Clock
from claude_cache_bump_gateway.core.scheduler import Scheduler
from claude_cache_bump_gateway.core.snapshot import Snapshot, Usage, stream_key
from claude_cache_bump_gateway.core.upstream import decode_body, filter_headers
from claude_cache_bump_gateway.server.keys import (
    CLIENT_SESSION_KEY,
    CLOCK_KEY,
    CONFIG_KEY,
    SCHEDULER_KEY,
)

logger = logging.getLogger(__name__)

#: Request header carrying the Claude Code session id, read case-insensitively.
SESSION_ID_HEADER = "x-claude-code-session-id"

_MESSAGES_PATH = "/v1/messages"


async def handle(request: web.Request) -> web.StreamResponse:
    """Forward ``request`` upstream unbuffered, tee'ing and capturing ``/v1/messages``."""
    config: Config = request.app[CONFIG_KEY]
    session: aiohttp.ClientSession = request.app[CLIENT_SESSION_KEY]
    scheduler: Scheduler = request.app[SCHEDULER_KEY]
    clock: Clock = request.app[CLOCK_KEY]

    target_url = config.upstream.rstrip("/") + request.path_qs
    outbound_headers = filter_headers(dict(request.headers))
    is_messages = request.method == "POST" and request.path == _MESSAGES_PATH
    session_id = request.headers.get(SESSION_ID_HEADER)

    request_body: bytes | None = None
    outbound_data: bytes | aiohttp.StreamReader
    if is_messages:
        request_body = await request.read()
        outbound_data = request_body
    else:
        outbound_data = request.content

    # Only a real /v1/messages request with a session id defers that
    # session's scheduled bump; every other request passes through untracked.
    tracking_id = session_id if is_messages else None
    if tracking_id is not None:
        scheduler.begin_request(tracking_id)

    start = clock.monotonic()
    try:
        resp = await _forward(
            request=request,
            session=session,
            scheduler=scheduler,
            clock=clock,
            target_url=target_url,
            headers=outbound_headers,
            data=outbound_data,
            is_messages=is_messages,
            session_id=session_id,
            request_body=request_body,
        )
    finally:
        if tracking_id is not None:
            scheduler.end_request(tracking_id)

    logger.debug(
        "proxied method=%s path=%s status=%s duration=%.3fs headers=%s",
        request.method,
        request.path,
        resp.status,
        clock.monotonic() - start,
        sorted(outbound_headers.keys()),
    )
    return resp


async def _forward(
    *,
    request: web.Request,
    session: aiohttp.ClientSession,
    scheduler: Scheduler,
    clock: Clock,
    target_url: str,
    headers: dict[str, str],
    data: bytes | aiohttp.StreamReader,
    is_messages: bool,
    session_id: str | None,
    request_body: bytes | None,
) -> web.StreamResponse:
    """Send the request upstream and stream the response back, tee'ing if applicable.

    A connection failure while establishing the upstream request yields a 502
    with a small JSON error body. A failure writing to the client after the
    response has started (client disconnect) stops writing and returns the
    partial response; nothing further can be done once headers are sent.
    """
    async with contextlib.AsyncExitStack() as stack:
        try:
            upstream_resp = await stack.enter_async_context(
                session.request(
                    request.method,
                    target_url,
                    headers=headers,
                    data=data,
                    allow_redirects=False,
                )
            )
        except aiohttp.ClientError as exc:
            logger.debug("upstream unreachable: %s", exc)
            return web.json_response(
                {"error": {"type": "ccbg_upstream_unreachable", "message": str(exc)}},
                status=502,
            )

        resp = web.StreamResponse(
            status=upstream_resp.status,
            headers=filter_headers(dict(upstream_resp.headers)),
        )
        await resp.prepare(request)

        should_tee = is_messages and upstream_resp.status < 400 and request_body is not None
        tee_buffer = bytearray() if should_tee else None
        content_type = upstream_resp.headers.get("content-type", "")
        content_encoding = upstream_resp.headers.get("content-encoding")

        try:
            async for chunk in upstream_resp.content.iter_any():
                if tee_buffer is not None:
                    tee_buffer.extend(chunk)
                await resp.write(chunk)
        except (ConnectionResetError, ConnectionAbortedError) as exc:
            logger.debug("client disconnected mid-stream: %s", exc)
            return resp
        await resp.write_eof()

    if tee_buffer is not None and session_id is not None and request_body is not None:
        _capture(
            scheduler,
            clock,
            session_id,
            request_body,
            headers,
            bytes(tee_buffer),
            content_type,
            content_encoding,
        )
    return resp


def _capture(
    scheduler: Scheduler,
    clock: Clock,
    session_id: str,
    request_body: bytes,
    headers: Mapping[str, str],
    tee_body: bytes,
    content_type: str,
    content_encoding: str | None,
) -> None:
    """Store a snapshot of a completed ``/v1/messages`` exchange for ``session_id``.

    Best-effort: any parsing failure is logged at DEBUG and never propagates
    or affects the client, whose response has already been fully written.
    """
    try:
        key = stream_key(request_body)
        body_data = json.loads(request_body)
        model = body_data.get("model") if isinstance(body_data, dict) else None
        usage = _extract_usage(tee_body, content_type, content_encoding)
        snapshot = Snapshot(
            session_id=session_id,
            stream_key=key,
            model=model if isinstance(model, str) else None,
            headers=dict(headers),
            body=request_body,
            captured_at=clock.now(),
            usage=usage,
        )
        credential = _extract_credential(headers)
        scheduler.note_request(session_id, snapshot, credential)
    except Exception:
        logger.debug("failed to capture snapshot for session %s", session_id, exc_info=True)


def _extract_usage(body: bytes, content_type: str, content_encoding: str | None = None) -> Usage:
    """Parse the tee'd response body into a :class:`Usage`, handling SSE or plain JSON.

    The tee'd copy is decompressed first: what goes to the client is
    forwarded byte-for-byte, but this private copy has to be readable, and
    Anthropic gzips ``/v1/messages`` responses whenever the client asks for
    it (which Claude Code does).
    """
    decoded = decode_body(body, content_encoding)
    if "text/event-stream" in content_type.lower():
        raw = _parse_sse_usage(decoded)
    else:
        raw = _parse_json_usage(decoded)
    return Usage.from_dict(raw)


def _parse_sse_usage(body: bytes) -> dict[str, Any] | None:
    """Merge ``usage`` from the ``message_start`` and ``message_delta`` SSE events.

    ``message_delta``'s fields override ``message_start``'s on overlap, per
    ``docs/DESIGN.md`` "Request flow".
    """
    merged: dict[str, Any] | None = None
    for raw_line in body.split(b"\n"):
        line = raw_line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[len(b"data:") :].strip()
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        usage: dict[str, Any] | None = None
        if event.get("type") == "message_start":
            message = event.get("message")
            if isinstance(message, dict) and isinstance(message.get("usage"), dict):
                usage = message["usage"]
        elif event.get("type") == "message_delta" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
        if usage is not None:
            merged = usage if merged is None else {**merged, **usage}
    return merged


def _parse_json_usage(body: bytes) -> dict[str, Any] | None:
    """Return the ``usage`` field of a plain JSON response body, if present."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    return usage if isinstance(usage, dict) else None


def _extract_credential(headers: Mapping[str, str]) -> tuple[str, str] | None:
    """Return the (header name, value) of the request credential, if any."""
    for name in ("authorization", "x-api-key"):
        value = headers.get(name)
        if value is not None:
            return (name, value)
    return None


def register(app: web.Application) -> None:
    """Register the catch-all passthrough route.

    Must be added after :func:`claude_cache_bump_gateway.server.control.register`
    so ``/ccbg/*`` is matched first.
    """
    app.router.add_route("*", "/{tail:.*}", handle)
