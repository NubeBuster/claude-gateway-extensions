"""aiohttp ``Application`` factory wiring the proxy's core objects together.

``make_app`` builds the application: the control API and the transparent
passthrough route, each wired to a swappable
:class:`~claude_gateway_extensions.core.store.SnapshotStore`,
:class:`~claude_gateway_extensions.core.selector.TargetSelector`,
:class:`~claude_gateway_extensions.core.upstream.Upstream` and
:class:`~claude_gateway_extensions.core.clock.Clock`. :func:`serve` warns
about a non-local ``Config.host`` and runs the resulting application until a
shutdown signal (``SIGTERM`` or ``SIGINT``) arrives, then drains in-flight
requests before cleaning up — see the "Drain plus ``SO_REUSEPORT``..."
decision in ``docs/DESIGN.md`` "Decisions".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.typedefs import Handler

from claude_gateway_extensions.config import Config
from claude_gateway_extensions.core.clock import Clock, SystemClock
from claude_gateway_extensions.core.scheduler import Scheduler
from claude_gateway_extensions.core.selector import LargestPrefixSelector, TargetSelector
from claude_gateway_extensions.core.store import FileStore, MemoryStore, SnapshotStore
from claude_gateway_extensions.core.upstream import AiohttpUpstream, Upstream
from claude_gateway_extensions.server import control, passthrough
from claude_gateway_extensions.server.keys import (
    CAPTURE_WARNED_KEY,
    CLIENT_SESSION_KEY,
    CLOCK_KEY,
    CONFIG_KEY,
    DRAIN_STATE_KEY,
    SCHEDULER_KEY,
    SELECTOR_KEY,
    STORE_KEY,
    UPSTREAM_KEY,
    DrainState,
)

logger = logging.getLogger(__name__)

#: How often the drain loop re-checks ``DrainState.in_flight`` and the force
#: flag. Small enough that a second shutdown signal or the last request
#: finishing is noticed promptly, cheap enough to poll instead of wiring a
#: condition variable for what is, in practice, a handful of iterations.
_DRAIN_POLL_S = 0.05

#: Name of the pid file ``serve`` writes under ``Config.state_dir`` while
#: running, used by ``cgx serve --handover`` to find the daemon it replaces.
_PID_FILE_NAME = "daemon.pid"

#: ``AppRunner``'s own ``shutdown_timeout``, used only for the final
#: force-close sweep in ``runner.cleanup()`` after :func:`_drain` has already
#: given every request its full, bounded wait. Must be a small *positive*
#: number, not ``0``: aiohttp's ``ceil_timeout`` treats ``delay <= 0`` as "no
#: timeout at all" (waits forever), the opposite of what a force-close needs.
_FORCE_CLOSE_TIMEOUT_S = 0.1


@web.middleware
async def track_in_flight(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Count ``request`` as in-flight for the duration of ``handler``.

    Covers every route (control API and passthrough alike, including a
    streaming ``/v1/messages`` response, which stays "in flight" for as long
    as the handler coroutine is still writing to it) so :func:`serve` can log
    and bound how long it waits during a drain.
    """
    drain_state: DrainState = request.app[DRAIN_STATE_KEY]
    drain_state.in_flight += 1
    try:
        return await handler(request)
    finally:
        drain_state.in_flight -= 1


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

    # auto_decompress=False: the server must not decompress an incoming
    # request body itself, or a gzip-encoded request forwarded byte-for-byte
    # (the "Byte-transparent forwarding" decision) would be silently
    # transcoded before the handler ever sees it -- and a body that fails to
    # decompress would be refused outright instead of reaching the handler
    # to be forwarded untouched and captured deliberately.
    app = web.Application(middlewares=[track_in_flight], handler_args={"auto_decompress": False})
    app[CONFIG_KEY] = config
    app[STORE_KEY] = resolved_store
    app[SELECTOR_KEY] = resolved_selector
    app[CLOCK_KEY] = resolved_clock
    app[DRAIN_STATE_KEY] = DrainState()
    app[CAPTURE_WARNED_KEY] = set()

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
    # /cgx/* is served locally rather than forwarded.
    control.register(app)
    passthrough.register(app)
    return app


#: Signals that ask the daemon to shut down. ``SIGTERM`` is what
#: ``systemctl --user stop`` sends to the unit ``cgx service install``
#: writes, so it must run the same cleanup path as ``Ctrl-C``.
SHUTDOWN_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGINT)


def _on_shutdown_signal(stop: asyncio.Event, force: asyncio.Event) -> None:
    """First shutdown signal starts the drain; a second one forces immediate exit."""
    if stop.is_set():
        force.set()
    else:
        stop.set()


def install_shutdown_handlers(
    loop: asyncio.AbstractEventLoop, stop: asyncio.Event, force: asyncio.Event
) -> None:
    """Arrange for :data:`SHUTDOWN_SIGNALS` to set ``stop`` (then ``force``) instead of killing.

    The first signal sets ``stop``, starting the drain in :func:`serve`; a
    second one (received while still draining) sets ``force``, which cuts
    the drain wait short. A no-op where the loop does not support signal
    handlers (Windows, or a loop running off the main thread); the caller
    still shuts down cleanly on ``KeyboardInterrupt`` there.
    """
    for sig in SHUTDOWN_SIGNALS:
        try:
            loop.add_signal_handler(sig, _on_shutdown_signal, stop, force)
        except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover - platform
            logger.debug("cannot install a handler for %s on this platform", sig.name)


def remove_shutdown_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Undo :func:`install_shutdown_handlers`, restoring the default disposition."""
    for sig in SHUTDOWN_SIGNALS:
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.remove_signal_handler(sig)


def _reuse_port_supported() -> bool:
    """Return whether this platform's socket module exposes ``SO_REUSEPORT``.

    True on Linux and macOS, false on Windows (which has no such option) —
    used to bind the listener with ``reuse_port=True`` only where it is
    actually supported, per the "Drain plus ``SO_REUSEPORT``..." decision in
    ``docs/DESIGN.md`` "Decisions".
    """
    return hasattr(socket, "SO_REUSEPORT")


def _pid_file_path(config: Config) -> Path:
    """Return the path of the pid file ``serve`` writes under ``config.state_dir``."""
    return config.state_dir / _PID_FILE_NAME


def _write_pid_file(config: Config) -> None:
    """Write this process's pid to :func:`_pid_file_path`, creating the directory if needed."""
    path = _pid_file_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))


def _read_pid_file(config: Config) -> int | None:
    """Return the pid recorded in the pid file, or ``None`` if absent or unreadable."""
    try:
        return int(_pid_file_path(config).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _remove_pid_file_if_ours(config: Config) -> None:
    """Remove the pid file only if it still names this process.

    A ``cgx serve --handover`` successor overwrites the pid file with its own
    pid before this (the outgoing) process finishes draining and exits; an
    unconditional removal here would delete the successor's live pid record
    out from under it. Best-effort, like the rest of the handover flow — a
    missing state directory or a permission problem is not fatal.
    """
    if _read_pid_file(config) == os.getpid():
        with contextlib.suppress(OSError):
            _pid_file_path(config).unlink(missing_ok=True)


def _local_health_url(config: Config) -> str:
    """Return the ``/cgx/health`` URL for this daemon's own bind address.

    Built from ``config.host``/``config.port`` directly (not ``config.url``,
    which may point elsewhere via ``CGX_URL``/``ANTHROPIC_BASE_URL``) since
    this is only ever used to probe the daemon's own listener during
    ``--handover``. A wildcard bind (``0.0.0.0``/``::``) is not itself a
    connectable address, so the probe targets loopback instead.
    """
    host = "127.0.0.1" if config.host in ("0.0.0.0", "::") else config.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{config.port}/cgx/health"


async def _wait_until_healthy(config: Config, timeout: float = 10.0) -> None:
    """Poll this daemon's own health endpoint until it answers ``200``, bounded by ``timeout``.

    Used by ``--handover`` before signalling the daemon being replaced: the
    new process's listener is already accepting connections by the time this
    is called (``SO_REUSEPORT`` let it bind alongside the old one), but this
    confirms the application itself is actually serving before anything is
    asked to step aside. Logs a warning and proceeds anyway if the timeout
    elapses, rather than blocking a handover forever on a health check that
    is best-effort by nature.
    """
    url = _local_health_url(config)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=1)) as resp:
                    if resp.status == 200:
                        return
            except aiohttp.ClientError:
                pass
            if loop.time() >= deadline:
                logger.warning(
                    "handover: own health check did not succeed within %ss; continuing anyway",
                    timeout,
                )
                return
            await asyncio.sleep(0.05)


def _signal_old_daemon(pid: int) -> None:
    """Send ``SIGTERM`` to the daemon ``--handover`` is replacing; best-effort."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        logger.debug("handover: no process at pid=%d (stale pid file)", pid)
    except OSError as exc:
        logger.warning("handover: failed to signal previous daemon pid=%d: %s", pid, exc)
    else:
        logger.info("handover: sent SIGTERM to previous daemon pid=%d", pid)


async def _drain(
    config: Config,
    app: web.Application,
    force: asyncio.Event,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Stop the scheduler and wait for in-flight requests, bounded by ``config.drain_timeout_s``.

    Cancelling every session task immediately (rather than waiting for it to
    happen naturally during ``app.cleanup()`` later) satisfies "starts no new
    bump during drain": a scheduled bump is an internal replay, not a live
    Claude Code request, so cancelling one mid-flight is safe — it resumes
    from the persisted state on the next start. Real in-flight requests are
    a different matter and are left alone here; :data:`DrainState.in_flight`
    (via :func:`track_in_flight`) is what this function actually waits on,
    and ``runner.cleanup()``'s own forced close (see :func:`serve`) is what
    ends a request that is still running once the timeout, or a second
    shutdown signal, cuts the wait short.
    """
    drain_state = app[DRAIN_STATE_KEY]
    waited_for = drain_state.in_flight
    logger.info(
        "cgx draining: waiting for %d in-flight request(s), bounded by %ds",
        waited_for,
        config.drain_timeout_s,
    )
    drain_state.draining = True
    scheduler: Scheduler = app[SCHEDULER_KEY]
    await scheduler.stop()
    deadline = loop.time() + config.drain_timeout_s
    while drain_state.in_flight > 0 and not force.is_set() and loop.time() < deadline:
        await asyncio.sleep(_DRAIN_POLL_S)
    logger.info("cgx exiting after draining %d in-flight request(s)", waited_for)


async def serve(config: Config, *, handover: bool = False) -> None:
    """Build the app and serve it on ``config.host:config.port``.

    Runs until a shutdown signal arrives (or the task is cancelled). A
    shutdown signal drains in-flight requests (including long SSE streams)
    for up to ``config.drain_timeout_s`` before tearing the listener and the
    scheduler down via :class:`aiohttp.web.AppRunner`'s cleanup, which is
    what flushes the app's ``cleanup_ctx`` — closing the shared client
    session and stopping the scheduler's per-session tasks (already stopped
    explicitly at drain start; see :func:`_drain`). ``handover=True``
    (``cgx serve --handover``) waits for this process's own health check to
    succeed, then signals the previously running daemon (found via the pid
    file under ``config.state_dir``) to start its own drain — see the
    "Drain plus ``SO_REUSEPORT``..." decision in ``docs/DESIGN.md``
    "Decisions".
    """
    logging.basicConfig(level=config.log_level)
    logger.setLevel(config.log_level)
    if not config.is_local_host():
        logger.warning(
            "CGX_HOST=%s is reachable from other machines; the control API has no authentication",
            config.host,
        )
    app = make_app(config)
    logger.info(
        "cgx serving host=%s port=%d upstream=%s state_dir=%s persist=%s",
        config.host,
        config.port,
        config.upstream,
        config.state_dir,
        config.persist,
    )
    # This daemon manages its own bounded drain wait (see _drain) using
    # DrainState.in_flight; by the time runner.cleanup() runs, any still-open
    # request has already had its full CGX_DRAIN_TIMEOUT_S (or was cut short
    # by a second shutdown signal), so shutdown_timeout only needs to cover
    # the final force-close sweep, not the drain itself.
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=_FORCE_CLOSE_TIMEOUT_S)
    await runner.setup()
    site = web.TCPSite(runner, config.host, config.port, reuse_port=_reuse_port_supported())
    await site.start()

    stop = asyncio.Event()
    force = asyncio.Event()
    loop = asyncio.get_running_loop()
    install_shutdown_handlers(loop, stop, force)
    try:
        old_pid = _read_pid_file(config) if handover else None
        if handover:
            await _wait_until_healthy(config)
            if old_pid is not None and old_pid != os.getpid():
                _signal_old_daemon(old_pid)
        _write_pid_file(config)
        await stop.wait()
        logger.info("cgx shutting down")
        # Stop accepting new connections immediately; already-accepted
        # connections (an in-flight request, or a client's pooled keep-alive
        # connection making a fresh request, e.g. a health-check poll) are
        # untouched by this and keep being served normally through _drain.
        await site.stop()
        await _drain(config, app, force, loop)
    finally:
        remove_shutdown_handlers(loop)
        await runner.cleanup()
        _remove_pid_file_if_ours(config)


__all__ = [
    "SHUTDOWN_SIGNALS",
    "install_shutdown_handlers",
    "make_app",
    "remove_shutdown_handlers",
    "serve",
    "track_in_flight",
]
