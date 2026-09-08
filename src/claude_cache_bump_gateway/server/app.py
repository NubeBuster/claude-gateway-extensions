"""aiohttp ``Application`` factory wiring the proxy's core objects together.

``make_app`` builds the application described in ``docs/DESIGN.md``
"Architecture": the control API and the transparent passthrough route, each
wired to a swappable :class:`~claude_cache_bump_gateway.core.store.SnapshotStore`,
:class:`~claude_cache_bump_gateway.core.selector.TargetSelector`,
:class:`~claude_cache_bump_gateway.core.upstream.Upstream` and
:class:`~claude_cache_bump_gateway.core.clock.Clock`. :func:`serve` validates
``Config`` and runs the resulting application until interrupted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import aiohttp
from aiohttp import web

from claude_cache_bump_gateway.config import Config
from claude_cache_bump_gateway.core.clock import Clock, SystemClock
from claude_cache_bump_gateway.core.scheduler import Scheduler
from claude_cache_bump_gateway.core.selector import LargestPrefixSelector, TargetSelector
from claude_cache_bump_gateway.core.store import FileStore, MemoryStore, SnapshotStore
from claude_cache_bump_gateway.core.upstream import AiohttpUpstream, Upstream
from claude_cache_bump_gateway.server import control, passthrough
from claude_cache_bump_gateway.server.keys import (
    CLIENT_SESSION_KEY,
    CLOCK_KEY,
    CONFIG_KEY,
    SCHEDULER_KEY,
    SELECTOR_KEY,
    STORE_KEY,
    UPSTREAM_KEY,
)

logger = logging.getLogger(__name__)


def make_app(
    config: Config,
    *,
    store: SnapshotStore | None = None,
    selector: TargetSelector | None = None,
    upstream: Upstream | None = None,
    clock: Clock | None = None,
) -> web.Application:
    """Build the proxy's ``web.Application``, defaulting every collaborator.

    Defaults: :class:`FileStore` under ``config.state_dir`` when
    ``config.persist`` else :class:`MemoryStore`; :class:`LargestPrefixSelector`;
    :class:`AiohttpUpstream` over a ``ClientSession`` owned by this
    application's ``cleanup_ctx``; :class:`SystemClock`. Any of the four may
    be overridden, primarily for tests. The client session is always created
    (regardless of an ``upstream`` override) because the passthrough route
    streams through it directly, independent of the buffered ``Upstream``
    protocol used for bump replays.
    """
    resolved_clock: Clock = clock if clock is not None else SystemClock()
    resolved_store: SnapshotStore
    if store is not None:
        resolved_store = store
    elif config.persist:
        resolved_store = FileStore(config.state_dir, config.max_streams_per_session)
    else:
        resolved_store = MemoryStore(config.max_streams_per_session)
    resolved_selector: TargetSelector = (
        selector if selector is not None else LargestPrefixSelector()
    )

    app = web.Application()
    app[CONFIG_KEY] = config
    app[STORE_KEY] = resolved_store
    app[SELECTOR_KEY] = resolved_selector
    app[CLOCK_KEY] = resolved_clock

    async def _lifecycle(app: web.Application) -> AsyncIterator[None]:
        """Open the shared client session and run the scheduler for the app's lifetime."""
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False) as session:
            app[CLIENT_SESSION_KEY] = session
            resolved_upstream = upstream if upstream is not None else AiohttpUpstream(session)
            app[UPSTREAM_KEY] = resolved_upstream
            scheduler = Scheduler(
                config, resolved_store, resolved_selector, resolved_upstream, resolved_clock
            )
            app[SCHEDULER_KEY] = scheduler
            await scheduler.start()
            try:
                yield
            finally:
                await scheduler.stop()

    app.cleanup_ctx.append(_lifecycle)

    # Control routes must be registered before the passthrough catch-all so
    # /ccbg/* is served locally rather than forwarded.
    control.register(app)
    passthrough.register(app)
    return app


async def serve(config: Config) -> None:
    """Validate ``config``, build the app, and serve it on ``config.host:config.port``.

    Runs until cancelled (e.g. by the caller's own signal handling); the
    listener and scheduler are cleaned up via :class:`aiohttp.web.AppRunner`.
    """
    config.validate()
    logging.basicConfig(level=config.log_level)
    logger.setLevel(config.log_level)
    app = make_app(config)
    logger.info(
        "ccbg serving host=%s port=%d upstream=%s state_dir=%s persist=%s",
        config.host,
        config.port,
        config.upstream,
        config.state_dir,
        config.persist,
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, config.host, config.port)
    await site.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
