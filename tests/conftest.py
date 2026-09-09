"""Shared pytest fixtures for server tests, and the ``invariant`` marker report hook.

The fake upstream is a real in-process aiohttp server (via ``aiohttp.test_utils``)
that records every request it receives and answers with scripted handlers, so
tests exercise the proxy's actual HTTP behaviour rather than a mocked
transport. No test in this suite talks to the real Anthropic API.

A test marked ``@pytest.mark.invariant(decision="...")`` guards a decision
recorded in ``docs/DESIGN.md``. When such a test fails, this module appends
:data:`_INVARIANT_NOTE` to the failure report so the reader knows the failure
is a contradicted decision rather than a broken test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_gateway_extensions.config import Config
from claude_gateway_extensions.server.app import make_app

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

_INVARIANT_NOTE = """\
This test guards a recorded design decision:

    {decision}

It is written down in docs/DESIGN.md, under "Decisions". The failure above
means the code no longer behaves the way that decision describes.

Do not adjust, weaken, skip, xfail or delete this test to make it pass, and
do not change the behaviour it pins. Either fix the code so the decision
holds again, or stop: changing the decision itself requires an amendment to
docs/DESIGN.md and the user's explicit consent, in that order.
"""


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    """Append :data:`_INVARIANT_NOTE` to the report of a failing ``invariant`` test."""
    report: pytest.TestReport = yield
    marker = item.get_closest_marker("invariant")
    if report.failed and marker is not None:
        decision = marker.kwargs.get("decision") or (marker.args[0] if marker.args else "unnamed")
        report.sections.append(
            ("Contradicted design decision", _INVARIANT_NOTE.format(decision=decision))
        )
    return report


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
        "drain_timeout_s": 600,
        "fork_max_tokens": 512,
        "fork_max_per_min": 6,
        "fork_thinking_guard": True,
        "fork_model": None,
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
