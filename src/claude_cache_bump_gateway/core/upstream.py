"""Outbound HTTP transport used to replay a captured request.

Defines the :class:`Upstream` protocol and its production implementation,
:class:`AiohttpUpstream`. Also hosts :data:`HOP_BY_HOP_HEADERS` and
:func:`filter_headers`, shared with the server's passthrough path since both
need identical hop-by-hop stripping.
"""

from __future__ import annotations

import gzip
import logging
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import aiohttp

logger = logging.getLogger(__name__)

#: RFC 7230 hop-by-hop headers plus `host` and `content-length`, which must
#: never be forwarded verbatim (the transport sets its own).
HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


def filter_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return ``headers`` lower-cased with hop-by-hop headers removed."""
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


def decode_body(body: bytes, content_encoding: str | None) -> bytes:
    """Return ``body`` decompressed according to ``content_encoding``.

    The proxy's shared ``ClientSession`` runs with ``auto_decompress=False``
    so the passthrough path can forward bytes untouched; that setting also
    applies to bump replays, whose body has to be *parsed* rather than
    forwarded. Anything not understood (or that fails to decode) is returned
    unchanged and left for the caller to fail on.
    """
    encoding = (content_encoding or "").strip().lower()
    if not encoding or encoding == "identity":
        return body
    try:
        if encoding == "gzip":
            return gzip.decompress(body)
        if encoding == "deflate":
            return zlib.decompress(body)
    except (OSError, zlib.error) as exc:
        logger.debug("could not decode a %s response body: %s", encoding, exc)
        return body
    logger.debug("unsupported content-encoding %r on a replay response", encoding)
    return body


@dataclass
class UpstreamResponse:
    """The result of replaying a request against upstream."""

    status: int
    headers: dict[str, str]
    body: bytes


class Upstream(Protocol):
    """Transport used to replay a captured request against the real API."""

    async def send(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes
    ) -> UpstreamResponse:
        """Send ``method`` ``url`` with ``headers`` and ``body``, returning the full response."""
        ...


class AiohttpUpstream:
    """:class:`Upstream` implementation backed by an ``aiohttp.ClientSession``."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Wrap an existing ``aiohttp.ClientSession`` (its lifecycle is the caller's)."""
        self._session = session

    async def send(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes
    ) -> UpstreamResponse:
        """Issue the request with redirects disabled and return the buffered, decoded response.

        The body is decompressed per its ``content-encoding`` (see
        :func:`decode_body`) and that header dropped, because every caller
        parses this body rather than forwarding it.
        """
        async with self._session.request(
            method, url, headers=dict(headers), data=body, allow_redirects=False
        ) as response:
            raw = await response.read()
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            decoded = decode_body(raw, response_headers.get("content-encoding"))
            if decoded is not raw:
                response_headers.pop("content-encoding", None)
            return UpstreamResponse(status=response.status, headers=response_headers, body=decoded)
