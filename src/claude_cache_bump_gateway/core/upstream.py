"""Outbound HTTP transport used to replay a captured request.

Defines the :class:`Upstream` protocol and its production implementation,
:class:`AiohttpUpstream`. Also hosts :data:`HOP_BY_HOP_HEADERS` and
:func:`filter_headers`, shared with the server's passthrough path since both
need identical hop-by-hop stripping.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import aiohttp

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
        """Issue the request with redirects disabled and return the buffered response."""
        async with self._session.request(
            method, url, headers=dict(headers), data=body, allow_redirects=False
        ) as response:
            response_body = await response.read()
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            return UpstreamResponse(
                status=response.status, headers=response_headers, body=response_body
            )
