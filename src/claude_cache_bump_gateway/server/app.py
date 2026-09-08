"""aiohttp ``Application`` factory wiring the proxy's core objects together.

``make_app`` builds the application described in ``docs/DESIGN.md``
"Architecture": the control API and the transparent passthrough route, each
wired to a swappable :class:`~claude_cache_bump_gateway.core.store.SnapshotStore`,
:class:`~claude_cache_bump_gateway.core.selector.TargetSelector`,
:class:`~claude_cache_bump_gateway.core.upstream.Upstream` and
:class:`~claude_cache_bump_gateway.core.clock.Clock`. :func:`serve` validates
``Config`` and runs the resulting application until a shutdown signal
(``SIGTERM`` or ``SIGINT``) arrives, then cleans up.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
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


#: Signals that ask the daemon to shut down. ``SIGTERM`` is what
#: ``systemctl --user stop`` sends to the unit ``ccbg install --service``
#: writes, so it must run the same cleanup path as ``Ctrl-C``.
SHUTDOWN_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGINT)


def install_shutdown_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """Arrange for :data:`SHUTDOWN_SIGNALS` to set ``stop`` instead of killing the process.

    A no-op where the loop does not support signal handlers (Windows, or a
    loop running off the main thread); the caller still shuts down cleanly on
    ``KeyboardInterrupt`` there.
    """
    for sig in SHUTDOWN_SIGNALS:
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover - platform
            logger.debug("cannot install a handler for %s on this platform", sig.name)


def remove_shutdown_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Undo :func:`install_shutdown_handlers`, restoring the default disposition."""
    for sig in SHUTDOWN_SIGNALS:
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.remove_signal_handler(sig)


async def serve(config: Config) -> None:
    """Validate ``config``, build the app, and serve it on ``config.host:config.port``.

    Runs until a shutdown signal arrives (or the task is cancelled), then
    tears the listener and the scheduler down via
    :class:`aiohttp.web.AppRunner`'s cleanup, which is what flushes the app's
    ``cleanup_ctx`` — closing the shared client session and stopping the
    scheduler's per-session tasks.
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
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    install_shutdown_handlers(loop, stop)
    try:
        await stop.wait()
        logger.info("ccbg shutting down")
    finally:
        remove_shutdown_handlers(loop)
        await runner.cleanup()
