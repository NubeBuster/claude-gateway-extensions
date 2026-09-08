"""Shared pytest fixtures for server tests: a fake upstream and app harness.

The fake upstream is a real in-process aiohttp server (via ``aiohttp.test_utils``)
that records every request it receives and answers with scripted handlers, so
tests exercise the proxy's actual HTTP behaviour rather than a mocked
transport. No test in this suite talks to the real Anthropic API.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_cache_bump_gateway.config import Config
from claude_cache_bump_gateway.server.app import make_app

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@dataclass
class RecordedRequest:
    """One request the fake upstream received."""

    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes


class FakeUpstream:
    """In-process aiohttp app recording requests and replaying scripted handlers."""

    def __init__(self) -> None:
        """Build the underlying ``web.Application`` with a single catch-all route."""
        self.requests: list[RecordedRequest] = []
        self._handlers: list[Handler] = []
        self.app = web.Application()
        self.app.router.add_route("*", "/{tail:.*}", self._dispatch)

    def script(self, handler: Handler) -> None:
        """Queue ``handler`` to answer the next request the fake receives, in order."""
        self._handlers.append(handler)

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        self.requests.append(
            RecordedRequest(
                method=request.method,
                path=request.path,
                query=request.query_string,
                headers={k.lower(): v for k, v in request.headers.items()},
                body=body,
            )
        )
        handler = self._handlers.pop(0)
        return await handler(request)


@pytest_asyncio.fixture
async def fake_upstream() -> AsyncIterator[tuple[FakeUpstream, TestServer]]:
    """A running :class:`FakeUpstream` plus the ``TestServer`` hosting it."""
    fake = FakeUpstream()
    server = TestServer(fake.app)
    await server.start_server()
    try:
        yield fake, server
    finally:
        await server.close()


def make_config(tmp_path: Path, upstream: str, **overrides: object) -> Config:
    """Build a :class:`Config` for tests: persistence off, pointed at ``upstream``."""
    fields: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 0,
        "upstream": upstream,
        "state_dir": tmp_path,
        "persist": False,
        "default_enabled": False,
        "interval_1h_s": 3000,
        "interval_5m_s": 240,
        "max_duration_s": 86400,
        "on_cache_write": "stop",
        "max_streams_per_session": 8,
        "log_level": "INFO",
        "url": "http://127.0.0.1:8787",
    }
    fields.update(overrides)
    return Config(**fields)  # type: ignore[arg-type]


@dataclass
class ServerHarness:
    """A running proxy app plus its client and the fake upstream behind it."""

    client: TestClient
    fake: FakeUpstream
    app: web.Application


@pytest_asyncio.fixture
async def harness(
    tmp_path: Path, fake_upstream: tuple[FakeUpstream, TestServer]
) -> AsyncIterator[ServerHarness]:
    """A :class:`ServerHarness` with the proxy pointed at the fake upstream."""
    fake, fake_server = fake_upstream
    config = make_config(tmp_path, str(fake_server.make_url("")))
    app = make_app(config)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield ServerHarness(client=client, fake=fake, app=app)
    finally:
        await client.close()
