"""Per-session bump scheduling: timers, retries and stop conditions.

:class:`Scheduler` owns one asyncio task per enabled session. Each task
polls in small ticks (never sleeping past ``_TICK_S`` in one call) so that
real requests, disablement and in-flight requests are observed promptly and
so tests can drive the whole thing through a :class:`~.clock.FakeClock`
without ever waiting on real time. See the interval and fail-closed
classification bullets in ``docs/DESIGN.md`` "Decisions" for the rules
this module implements.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses

from claude_cache_bump_gateway.config import Config
from claude_cache_bump_gateway.core.bump import BumpResult, bump_once
from claude_cache_bump_gateway.core.clock import Clock
from claude_cache_bump_gateway.core.selector import TargetSelector
from claude_cache_bump_gateway.core.snapshot import (
    SessionState,
    Snapshot,
    SnapshotError,
    wants_1h_ttl,
)
from claude_cache_bump_gateway.core.store import SnapshotStore
from claude_cache_bump_gateway.core.upstream import Upstream


class NoSnapshot(Exception):
    """Raised by :meth:`Scheduler.bump_now` when the session has nothing to replay."""


class Scheduler:
    """Schedules, retries and stops per-session cache-warming bumps.

    Construct one per daemon instance and drive it with :meth:`start` /
    :meth:`stop`; the HTTP layer feeds it via :meth:`note_request`,
    :meth:`begin_request` / :meth:`end_request`, and the control API drives
    :meth:`set_enabled`, :meth:`bump_now`, :meth:`forget` and the ``status*``
    methods.
    """

    #: How often a session's task re-checks its state while waiting for its
    #: next bump to come due. Real requests, disablement and in-flight
    #: requests are only ever stale by up to this much.
    _TICK_S: float = 1.0

    #: Backoff, in seconds, between retries of a ``transient`` bump. Three
    #: retries (four attempts total) then the session stops.
    _RETRY_BACKOFFS_S: tuple[float, ...] = (1.0, 4.0, 16.0)

    def __init__(
        self,
        config: Config,
        store: SnapshotStore,
        selector: TargetSelector,
        upstream: Upstream,
        clock: Clock,
    ) -> None:
        """Wire the scheduler to its collaborators; does not start any tasks yet."""
        self._config = config
        self._store = store
        self._selector = selector
        self._upstream = upstream
        self._clock = clock
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._in_flight: dict[str, int] = {}
        self._last_result: dict[str, BumpResult | None] = {}

    async def start(self) -> None:
        """Prune stale snapshots and resume timers for every enabled session in the store."""
        self._store.prune(self._clock.now() - self._config.max_duration_s)
        for session_id in self._store.list_sessions():
            state = self._store.get_state(session_id)
            if state is not None and state.enabled:
                self._ensure_task(session_id)

    async def stop(self) -> None:
        """Cancel every running session task and wait for them to finish."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    def note_request(
        self,
        session_id: str,
        snapshot: Snapshot | None,
        credential: tuple[str, str] | None,
    ) -> None:
        """Record a completed real request, resetting the session's timer.

        Creates a new :class:`SessionState` (with ``enabled =
        config.default_enabled``) the first time a session is seen. Storing
        ``snapshot`` and updating ``last_request_at``/``credential`` is
        enough to reset the timer: the running task re-reads state every
        tick and recomputes its target from the fresh ``last_request_at``.
        """
        state = self._store.get_state(session_id)
        if state is None:
            state = SessionState(session_id=session_id, enabled=self._config.default_enabled)
        state = dataclasses.replace(state, last_request_at=self._clock.now())
        if credential is not None:
            state = dataclasses.replace(state, last_credential=credential)
        self._store.put_state(state)
        if snapshot is not None:
            self._store.put_snapshot(snapshot)
        if state.enabled:
            self._ensure_task(session_id)

    def set_enabled(
        self,
        session_id: str,
        enabled: bool,
        interval_s: int | None = None,
        max_duration_s: int | None = None,
        until: float | None = None,
    ) -> SessionState:
        """Enable or disable bumping for a session, optionally overriding interval/max duration.

        ``until`` is an absolute epoch deadline that, once set, replaces the
        max-duration stop for this session (see :meth:`_run_session`); omit
        or pass ``None`` to leave a previously set deadline untouched, same
        as ``interval_s``/``max_duration_s``.

        Disabling clears the deadline, so re-enabling the session without a
        new one falls back to the daemon's default max duration instead of
        silently resuming a deadline the user already turned off.
        """
        state = self._store.get_state(session_id)
        if state is None:
            state = SessionState(session_id=session_id, enabled=enabled)
        state = dataclasses.replace(state, enabled=enabled)
        if interval_s is not None:
            state = dataclasses.replace(state, interval_s=interval_s)
        if max_duration_s is not None:
            state = dataclasses.replace(state, max_duration_s=max_duration_s)
        if until is not None:
            state = dataclasses.replace(state, until=until)
        if enabled:
            state = dataclasses.replace(state, stopped_reason=None)
        else:
            state = dataclasses.replace(state, until=None)
        self._store.put_state(state)
        if enabled:
            self._ensure_task(session_id)
        else:
            self._cancel_task(session_id)
        return state

    async def bump_now(self, session_id: str) -> BumpResult:
        """Replay the session's selected snapshot once, applying the outcome's normal effects.

        Raises :class:`NoSnapshot` if the session has no snapshot to replay.
        Unlike the scheduled path, a ``transient`` outcome here is not
        retried and does not stop the session.
        """
        state = self._store.get_state(session_id)
        snapshot = self._selector.select(self._store.get_snapshots(session_id))
        if snapshot is None:
            raise NoSnapshot(session_id)
        credential = state.last_credential if state is not None else None
        result = await bump_once(self._upstream, self._config.upstream, snapshot, credential)
        stop_reason = self._apply_result(session_id, result)
        if stop_reason is not None:
            self._persist_stop(session_id, stop_reason)
            self._cancel_task(session_id)
        return result

    def forget(self, session_id: str) -> None:
        """Stop and discard everything the scheduler and store hold for ``session_id``."""
        self._cancel_task(session_id)
        self._store.delete_session(session_id)
        self._last_result.pop(session_id, None)
        self._in_flight.pop(session_id, None)

    def begin_request(self, session_id: str) -> None:
        """Mark a real request for ``session_id`` as in flight, deferring any pending bump."""
        self._in_flight[session_id] = self._in_flight.get(session_id, 0) + 1

    def end_request(self, session_id: str) -> None:
        """Mark a real request for ``session_id`` as finished."""
        current = self._in_flight.get(session_id, 0)
        if current <= 1:
            self._in_flight.pop(session_id, None)
        else:
            self._in_flight[session_id] = current - 1

    def status(self, session_id: str) -> dict[str, object]:
        """Return a JSON-serialisable status snapshot for ``session_id``.

        Raises ``KeyError`` if the session is unknown.
        """
        state = self._store.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        snapshots = self._store.get_snapshots(session_id)
        snapshot = self._selector.select(snapshots)
        next_bump_at = self._next_bump_at(state, snapshot) if state.enabled else None
        last_result = self._last_result.get(session_id)
        return {
            "session_id": state.session_id,
            "enabled": state.enabled,
            "interval_s": state.interval_s,
            "max_duration_s": state.max_duration_s,
            "until": state.until,
            "last_request_at": state.last_request_at,
            "last_bump_at": state.last_bump_at,
            "bump_count": state.bump_count,
            "stopped_reason": state.stopped_reason,
            "next_bump_at": next_bump_at,
            "streams": len(snapshots),
            "last_result": dataclasses.asdict(last_result) if last_result is not None else None,
        }

    def status_all(self) -> list[dict[str, object]]:
        """Return :meth:`status` for every session known to the store."""
        return [self.status(session_id) for session_id in self._store.list_sessions()]

    # -- internals ---------------------------------------------------------

    def _ensure_task(self, session_id: str) -> None:
        """Start a session task if none is currently running for it."""
        existing = self._tasks.get(session_id)
        if existing is not None and not existing.done():
            return
        self._tasks[session_id] = asyncio.create_task(self._run_session(session_id))

    def _cancel_task(self, session_id: str) -> None:
        """Cancel and forget the running task for ``session_id``, if any."""
        task = self._tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _persist_stop(self, session_id: str, reason: str) -> None:
        """Set ``enabled = False`` and record ``stopped_reason`` for ``session_id``."""
        state = self._store.get_state(session_id)
        if state is None:
            return
        state = dataclasses.replace(state, enabled=False, stopped_reason=reason)
        self._store.put_state(state)

    def _effective_interval(self, state: SessionState, snapshot: Snapshot) -> int:
        """Return the configured per-session interval, or the config default for this snapshot."""
        if state.interval_s is not None:
            return state.interval_s
        try:
            wants_1h = wants_1h_ttl(snapshot.body)
        except SnapshotError:
            wants_1h = False
        return self._config.interval_1h_s if wants_1h else self._config.interval_5m_s

    def _next_bump_at(self, state: SessionState, snapshot: Snapshot | None) -> float | None:
        """Return the epoch time of the next bump, or ``None`` if there is no snapshot to bump."""
        if snapshot is None:
            return None
        interval = self._effective_interval(state, snapshot)
        anchor = max(state.last_request_at or 0.0, state.last_bump_at or 0.0)
        return anchor + interval

    def _apply_result(self, session_id: str, result: BumpResult) -> str | None:
        """Record ``result`` and apply its bookkeeping effect; return a stop reason if any."""
        self._last_result[session_id] = result
        if result.outcome in ("ok", "cache_write"):
            state = self._store.get_state(session_id)
            if state is not None:
                state = dataclasses.replace(
                    state,
                    last_bump_at=self._clock.now(),
                    bump_count=state.bump_count + 1,
                )
                self._store.put_state(state)
        if result.outcome == "unauthorized":
            return "unauthorized"
        if result.outcome == "rejected":
            return "rejected"
        if result.outcome == "cache_write" and self._config.on_cache_write == "stop":
            return "cache_write"
        return None

    async def _attempt_with_retries(self, session_id: str, snapshot: Snapshot) -> str | None:
        """Bump once, retrying transient failures with backoff; return a stop reason if any."""
        state = self._store.get_state(session_id)
        credential = state.last_credential if state is not None else None
        result = await bump_once(self._upstream, self._config.upstream, snapshot, credential)
        if result.outcome != "transient":
            return self._apply_result(session_id, result)
        for backoff in self._RETRY_BACKOFFS_S:
            await self._clock.sleep(backoff)
            result = await bump_once(self._upstream, self._config.upstream, snapshot, credential)
            if result.outcome != "transient":
                return self._apply_result(session_id, result)
        self._last_result[session_id] = result
        return "transient"

    async def _run_session(self, session_id: str) -> None:
        """Task body: wait for the next bump, then bump, forever until disabled or stopped."""
        try:
            while True:
                state = self._store.get_state(session_id)
                if state is None or not state.enabled:
                    return
                snapshots = self._store.get_snapshots(session_id)
                snapshot = self._selector.select(snapshots)
                if snapshot is None:
                    self._persist_stop(session_id, "no_snapshot")
                    return
                if state.until is not None:
                    if self._clock.now() >= state.until:
                        self._persist_stop(session_id, "deadline")
                        return
                else:
                    max_duration = (
                        state.max_duration_s
                        if state.max_duration_s is not None
                        else self._config.max_duration_s
                    )
                    last_request_at = (
                        state.last_request_at
                        if state.last_request_at is not None
                        else self._clock.now()
                    )
                    if self._clock.now() - last_request_at >= max_duration:
                        self._persist_stop(session_id, "max_duration")
                        return
                next_bump_at = self._next_bump_at(state, snapshot)
                assert next_bump_at is not None
                remaining = next_bump_at - self._clock.now()
                if remaining > 0:
                    await self._clock.sleep(min(remaining, self._TICK_S))
                    continue
                if self._in_flight.get(session_id, 0) > 0:
                    await self._clock.sleep(self._TICK_S)
                    continue
                stop_reason = await self._attempt_with_retries(session_id, snapshot)
                if stop_reason is not None:
                    self._persist_stop(session_id, stop_reason)
                    return
        finally:
            if self._tasks.get(session_id) is asyncio.current_task():
                self._tasks.pop(session_id, None)
