"""Per-session bump scheduling: timers, retries and stop conditions.

:class:`Scheduler` owns one asyncio task per enabled session. A task with a
snapshot to bump polls in small ticks (never sleeping past ``_TICK_S`` in
one call) so that real requests, disablement and in-flight requests are
observed promptly; one still waiting for its first snapshot parks on an
event instead, since there is nothing to re-check every second. Either way
tests drive the whole thing through a :class:`~.clock.FakeClock` without
ever waiting on real time. See the interval and fail-closed
classification bullets in ``docs/DESIGN.md`` "Decisions" for the rules
this module implements.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses

from claude_gateway_extensions.config import (
    Config,
    ConfigFileState,
    ConfigFileWatcher,
    merge_shadow_fork,
)
from claude_gateway_extensions.core.bump import BumpResult, bump_once
from claude_gateway_extensions.core.clock import Clock
from claude_gateway_extensions.core.fork_reasons import (
    FAIL_OPEN_REASONS,
    REASON_NO_SNAPSHOT,
    REASON_RATE_LIMITED,
    REASON_SNAPSHOT_STALE,
)
from claude_gateway_extensions.core.selector import TargetSelector
from claude_gateway_extensions.core.shadow_fork import ForkVerdict, fork_once
from claude_gateway_extensions.core.snapshot import (
    CACHE_TTL_1H_S,
    CACHE_TTL_5M_S,
    Breakpoint,
    RewarmWrite,
    SessionState,
    Snapshot,
    SnapshotError,
    effective_ttl_label,
    parse_breakpoints,
    shortest_breakpoint_ttl_s,
    wants_1h_ttl,
)
from claude_gateway_extensions.core.status_format import format_status_times
from claude_gateway_extensions.core.store import SnapshotStore
from claude_gateway_extensions.core.upstream import Upstream


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
        config_file_watcher: ConfigFileWatcher | None = None,
    ) -> None:
        """Wire the scheduler to its collaborators; does not start any tasks yet.

        ``config_file_watcher`` defaults to a fresh :class:`ConfigFileWatcher`
        over ``config.config_file`` -- overridable so tests can inject one
        pointed at a scratch file without touching real paths.
        """
        self._config = config
        self._store = store
        self._selector = selector
        self._upstream = upstream
        self._clock = clock
        self._config_file_watcher = (
            config_file_watcher
            if config_file_watcher is not None
            else ConfigFileWatcher(config.config_file)
        )
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._in_flight: dict[str, int] = {}
        self._last_result: dict[str, BumpResult | None] = {}
        self._wakeups: dict[str, asyncio.Event] = {}
        self._enabled_since: dict[str, float] = {}
        self._fork_times: dict[str, list[float]] = {}

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
        if snapshot is not None:
            self._wake(session_id)

    def note_replay(self, session_id: str) -> None:
        """Record a captured-and-discarded replay body for ``session_id``.

        See the "Replays are never snapshots" decision in ``docs/DESIGN.md``.
        Only increments ``replays_seen`` and persists it -- unlike
        :meth:`note_request`, this must not reset ``last_request_at``,
        touch the credential, or start the session's bump task: a replay is
        by definition not a real turn, so it must not look like one to the
        scheduler.
        """
        state = self._store.get_state(session_id)
        if state is None:
            state = SessionState(session_id=session_id, enabled=self._config.default_enabled)
        state = dataclasses.replace(state, replays_seen=state.replays_seen + 1)
        self._store.put_state(state)

    def set_enabled(
        self,
        session_id: str,
        enabled: bool,
        interval_s: int | None = None,
        max_duration_s: int | None = None,
        until: float | None = None,
        rewarm: bool = False,
    ) -> SessionState:
        """Enable or disable bumping for a session, optionally overriding interval/max duration.

        ``until`` is an absolute epoch deadline that, once set, replaces the
        max-duration stop for this session (see :meth:`_run_session`); omit
        or pass ``None`` to leave a previously set deadline untouched, same
        as ``interval_s``/``max_duration_s``.

        Disabling clears the deadline, so re-enabling the session without a
        new one falls back to the daemon's default max duration instead of
        silently resuming a deadline the user already turned off.

        ``rewarm=True`` arms the session's rewarm flag (see the "Rewarm"
        decision in ``docs/DESIGN.md``): the next ``cache_write`` bump result
        is tolerated instead of stopping the session. It only ever turns the
        flag on here -- re-enabling without ``rewarm`` leaves an
        already-armed flag alone -- and is cleared, like ``until``, on
        disable and on every auto-stop.
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
            if rewarm:
                state = dataclasses.replace(state, rewarm=True)
        else:
            state = dataclasses.replace(state, until=None, rewarm=False)
        self._store.put_state(state)
        if enabled:
            self._ensure_task(session_id)
            self._wake(session_id)
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

    async def fork_now(
        self,
        session_id: str,
        prompt: str,
        payload: object,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> ForkVerdict:
        """Replay the session's selected snapshot once as a shadow fork.

        Unlike bumping, selection here prefers a snapshot known *not* to be
        subagent traffic (``is_subagent is False``): the selector runs over
        that subset first, falling back to every snapshot only when the
        subset is empty. The fallback is flagged, not refused, on the
        returned verdict's ``subagent_snapshot`` -- a fork is one-shot
        spend, not recurring, so there is nothing to protect by refusing
        it. A snapshot whose ``is_subagent`` is unknown (``None``, an
        unparseable or attribution-less body) is treated the same as a
        known subagent snapshot for this preference: never assumed to be
        the main conversation.

        ``model`` overrides the reviewer model for this fork only, else
        falling back to the effective ``CGX_SHADOW_FORK_MODEL`` (the tuning
        file if it sets one, else the daemon-wide environment default), else
        the snapshot's own model -- the only choice that reads the
        parent's cached prefix. Never raises and never touches bump
        bookkeeping: it does not set ``last_bump_at``, does not increment
        ``bump_count``, does not stop the session on a paid write, and
        does not reset any timer. A fork is a side channel, not a
        cache-warming replay, so none of the bump effects apply to it.

        The four Shadow Fork tuning values (max tokens, the per-minute rate
        limit, the thinking guard and the reviewer model) are resolved fresh
        on every call through :attr:`_config_file_watcher`, so a tuning-file
        edit takes effect on the very next fork -- see the "Shadow Fork
        tuning is hot reloaded..." decision in ``docs/DESIGN.md``.

        Every call updates this session's fork counters (see the "Fork
        status" decision in ``docs/DESIGN.md``) before returning, whichever
        path it returns by.
        """
        effective = merge_shadow_fork(self._config, self._config_file_watcher.current().settings)
        state = self._store.get_state(session_id)
        snapshots = self._store.get_snapshots(session_id)
        main_snapshots = [snap for snap in snapshots if snap.is_subagent is False]
        subagent_snapshot = False
        if main_snapshots:
            snapshot = self._selector.select(main_snapshots)
        else:
            snapshot = self._selector.select(snapshots)
            subagent_snapshot = snapshot is not None
        if snapshot is None:
            result = ForkVerdict(
                verdict="ok", reason=REASON_NO_SNAPSHOT, mutation=None, usage=None, paid_write=False
            )
            self._record_fork_outcome(session_id, sent=False, reason_code=result.reason)
            return result
        assert effective.max_per_min is not None  # merge_shadow_fork always fills this in
        if self._fork_rate_limited(session_id, effective.max_per_min):
            result = ForkVerdict(
                verdict="ok",
                reason=REASON_RATE_LIMITED,
                mutation=None,
                usage=None,
                paid_write=False,
            )
            self._record_fork_outcome(session_id, sent=False, reason_code=result.reason)
            return result
        if self._snapshot_is_stale(snapshot):
            result = ForkVerdict(
                verdict="ok",
                reason=REASON_SNAPSHOT_STALE,
                mutation=None,
                usage=None,
                paid_write=False,
            )
            self._record_fork_outcome(session_id, sent=False, reason_code=result.reason)
            return result
        self._record_fork(session_id)
        credential = state.last_credential if state is not None else None
        cap = max_tokens if max_tokens is not None else effective.max_tokens
        model_override = model if model is not None else effective.model
        assert cap is not None  # merge_shadow_fork always fills this in
        result = await fork_once(
            self._upstream,
            self._config.upstream,
            snapshot,
            credential,
            prompt,
            payload,
            cap,
            bool(effective.thinking_guard),
            model_override,
        )
        if subagent_snapshot:
            result = dataclasses.replace(result, subagent_snapshot=True)
        self._record_fork_outcome(session_id, sent=True, reason_code=self._fork_reason_code(result))
        return result

    @staticmethod
    def _fork_reason_code(result: ForkVerdict) -> str:
        """Return the fixed enum value to persist as ``last_fork_reason`` for ``result``.

        Never the model's own free-text ``reason``: a fail-open outcome's
        ``reason`` is already one of :data:`FAIL_OPEN_REASONS` and is used
        verbatim, while a genuine verdict from the model collapses to its
        bare ``verdict`` (``"ok"`` or ``"intervene"``) instead -- both are
        fixed enum values, never prose that could carry prompt or reply
        content.
        """
        if result.reason in FAIL_OPEN_REASONS:
            return result.reason
        return result.verdict

    def _record_fork_outcome(self, session_id: str, sent: bool, reason_code: str) -> None:
        """Update this session's persisted fork counters after one :meth:`fork_now` call.

        ``fork_attempts`` increments on every call; ``forks_sent`` only when
        the review passed the ``no_snapshot``/``rate_limited``/
        ``snapshot_stale`` pre-checks and actually reached
        :func:`~claude_gateway_extensions.core.shadow_fork.fork_once`.
        ``last_fork_at`` and ``last_fork_reason`` always refresh to this
        call's outcome.
        """
        state = self._store.get_state(session_id)
        if state is None:
            state = SessionState(session_id=session_id, enabled=self._config.default_enabled)
        state = dataclasses.replace(
            state,
            fork_attempts=state.fork_attempts + 1,
            forks_sent=state.forks_sent + (1 if sent else 0),
            last_fork_at=self._clock.now(),
            last_fork_reason=reason_code,
        )
        self._store.put_state(state)

    def _snapshot_is_stale(self, snapshot: Snapshot) -> bool:
        """Return whether ``snapshot`` is older than the TTL its own body requested.

        An unparseable body is treated as stale rather than fresh: a fork
        must never spend money to replay a snapshot it cannot classify.
        """
        try:
            ttl_s = CACHE_TTL_1H_S if wants_1h_ttl(snapshot.body) else CACHE_TTL_5M_S
        except SnapshotError:
            return True
        return self._clock.now() - snapshot.captured_at > ttl_s

    def _fork_rate_limited(self, session_id: str, max_per_min: int) -> bool:
        """Return whether ``session_id`` already made ``max_per_min`` forks in the trailing 60s."""
        window_start = self._clock.now() - 60.0
        times = [t for t in self._fork_times.get(session_id, []) if t >= window_start]
        self._fork_times[session_id] = times
        return len(times) >= max_per_min

    def _record_fork(self, session_id: str) -> None:
        """Record a fork attempt for ``session_id`` at the current time."""
        self._fork_times.setdefault(session_id, []).append(self._clock.now())

    def forget(self, session_id: str) -> None:
        """Stop and discard everything the scheduler and store hold for ``session_id``."""
        self._cancel_task(session_id)
        self._store.delete_session(session_id)
        self._last_result.pop(session_id, None)
        self._in_flight.pop(session_id, None)
        self._wakeups.pop(session_id, None)
        self._enabled_since.pop(session_id, None)
        self._fork_times.pop(session_id, None)

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
        cache_expires_at = self._cache_expires_at(state, snapshot)
        last_result = self._last_result.get(session_id)
        now = self._clock.now()
        rewarm_write = (
            dataclasses.asdict(state.rewarm_write) if state.rewarm_write is not None else None
        )
        breakpoints = self._breakpoints_safe(snapshot) if snapshot is not None else []
        breakpoints_json = [dataclasses.asdict(bp) for bp in breakpoints]
        effective_ttl = effective_ttl_label(breakpoints) if snapshot is not None else None
        return {
            "session_id": state.session_id,
            "enabled": state.enabled,
            "interval_s": state.interval_s,
            "max_duration_s": state.max_duration_s,
            "until": state.until,
            "last_request_at": state.last_request_at,
            "last_bump_at": state.last_bump_at,
            "bump_count": state.bump_count,
            "replays_seen": state.replays_seen,
            "stopped_reason": state.stopped_reason,
            "next_bump_at": next_bump_at,
            "cache_expires_at": cache_expires_at,
            "streams": len(snapshots),
            "last_result": dataclasses.asdict(last_result) if last_result is not None else None,
            "rewarm": state.rewarm,
            "rewarm_write": rewarm_write,
            "breakpoints": breakpoints_json,
            "effective_ttl": effective_ttl,
            "fork_attempts": state.fork_attempts,
            "forks_sent": state.forks_sent,
            "last_fork_at": state.last_fork_at,
            "last_fork_reason": state.last_fork_reason,
            "formatted": format_status_times(
                cache_expires_at=cache_expires_at,
                next_bump_at=next_bump_at,
                now=now,
                rewarm_write=rewarm_write,
                breakpoints=breakpoints_json,
                fork_attempts=state.fork_attempts,
                forks_sent=state.forks_sent,
                last_fork_reason=state.last_fork_reason,
            ),
        }

    def status_all(self) -> list[dict[str, object]]:
        """Return :meth:`status` for every session known to the store."""
        return [self.status(session_id) for session_id in self._store.list_sessions()]

    def config_file_state(self) -> ConfigFileState:
        """Return the Shadow Fork tuning file's current load state, re-``stat``ing it now.

        The same :class:`~claude_gateway_extensions.config.ConfigFileWatcher`
        instance :meth:`fork_now` reads from, so ``GET /cgx/config`` reports
        exactly what the next fork would use, not a separate read of the file.
        """
        return self._config_file_watcher.current()

    # -- internals ---------------------------------------------------------

    def _ensure_task(self, session_id: str) -> None:
        """Start a session task if none is currently running for it.

        Records the moment the session became enabled: that is the anchor the
        max-duration bound is measured from while the session has no snapshot
        to bump yet (see :meth:`_waiting_bound`).
        """
        existing = self._tasks.get(session_id)
        if existing is not None and not existing.done():
            return
        self._enabled_since[session_id] = self._clock.now()
        self._tasks[session_id] = asyncio.create_task(self._run_session(session_id))

    def _wake(self, session_id: str) -> None:
        """Wake a session task waiting for its first snapshot, if one is waiting."""
        event = self._wakeups.get(session_id)
        if event is not None:
            event.set()

    def _cancel_task(self, session_id: str) -> None:
        """Cancel and forget the running task for ``session_id``, if any."""
        task = self._tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _persist_stop(self, session_id: str, reason: str) -> None:
        """Set ``enabled = False``, clear ``until`` and record ``stopped_reason``.

        Every stop reason ends the session's working life, so the deadline
        goes with it, for the same reason :meth:`set_enabled` clears it on an
        explicit disable: a later enable without a new deadline must fall
        back to the max-duration stop instead of tripping over a deadline
        already in the past and stopping again on its first tick.
        """
        state = self._store.get_state(session_id)
        if state is None:
            return
        state = dataclasses.replace(
            state, enabled=False, stopped_reason=reason, until=None, rewarm=False
        )
        self._store.put_state(state)

    @staticmethod
    def _breakpoints_safe(snapshot: Snapshot) -> list[Breakpoint]:
        """Return ``snapshot``'s breakpoints in prompt order, or ``[]`` if unparseable."""
        try:
            return parse_breakpoints(snapshot.body)
        except SnapshotError:
            return []

    def _max_duration_s(self, state: SessionState) -> int:
        """Return the configured per-session max duration, or the daemon default."""
        if state.max_duration_s is not None:
            return state.max_duration_s
        return self._config.max_duration_s

    def _effective_interval(self, state: SessionState, snapshot: Snapshot) -> int:
        """Return the configured per-session interval, or the config default for this snapshot.

        The default is keyed on the snapshot's *shortest* breakpoint TTL
        (see :meth:`_cache_ttl_s`), not "any 1h breakpoint present": a body
        mixing `1h` and `5m` breakpoints expires its 5m tail well before an
        interval sized for 1h would ever fire.
        """
        if state.interval_s is not None:
            return state.interval_s
        ttl_s = self._cache_ttl_s(snapshot)
        return self._config.interval_1h_s if ttl_s == CACHE_TTL_1H_S else self._config.interval_5m_s

    def _next_bump_at(self, state: SessionState, snapshot: Snapshot | None) -> float | None:
        """Return the epoch time of the next bump, or ``None`` if there is no snapshot to bump."""
        if snapshot is None:
            return None
        interval = self._effective_interval(state, snapshot)
        anchor = max(state.last_request_at or 0.0, state.last_bump_at or 0.0)
        return anchor + interval

    def _waiting_bound(self, session_id: str, state: SessionState) -> float:
        """Return the epoch time at which a session with no snapshot stops waiting.

        A waiting session has no ``last_request_at`` to measure the max
        duration from, so it is measured from the moment the session was
        enabled; an explicit ``until`` replaces it, exactly as it does for a
        session that is already bumping.
        """
        if state.until is not None:
            return state.until
        return self._enabled_since.get(session_id, self._clock.now()) + self._max_duration_s(state)

    async def _await_first_snapshot(self, session_id: str, state: SessionState) -> None:
        """Park until a snapshot is captured for ``session_id`` or its wait bound is due.

        Costs one timer, not a tick per second: a session enabled before its
        first request may wait for hours. Returning does not mean either
        thing happened; the caller re-reads state and store and decides.
        Clearing the wakeup here cannot lose a snapshot, because the caller
        read the store with no await in between and :meth:`note_request`
        only ever runs between awaits.
        """
        remaining = self._waiting_bound(session_id, state) - self._clock.now()
        if remaining <= 0:
            return
        event = self._wakeups.setdefault(session_id, asyncio.Event())
        event.clear()
        await self._clock.wait_for_event(event, remaining)

    def _cache_ttl_s(self, snapshot: Snapshot) -> int:
        """Return the actual prompt-cache TTL for ``snapshot``: its shortest breakpoint TTL.

        See the "Interval derived from the snapshot's shortest breakpoint
        TTL" decision in ``docs/DESIGN.md``: a body may carry breakpoints at
        more than one TTL (1h ones must precede 5m ones in the API), and
        the tail entry a bump has to find alive is the one that expires
        first. :meth:`_effective_interval` uses this same TTL to size the
        bump interval, and :meth:`_cache_expires_at` to compute expiry.
        """
        return shortest_breakpoint_ttl_s(self._breakpoints_safe(snapshot))

    def _cache_expires_at(self, state: SessionState, snapshot: Snapshot | None) -> float | None:
        """Return when the prompt cache is expected to expire, or ``None`` without a snapshot.

        The start point is the later of the last real request or the last
        successful bump; present regardless of whether bumping is enabled,
        so a statusline can always show the cache's remaining time to live.
        """
        if snapshot is None:
            return None
        anchor = max(state.last_request_at or 0.0, state.last_bump_at or 0.0)
        return anchor + self._cache_ttl_s(snapshot)

    def _apply_result(self, session_id: str, result: BumpResult) -> str | None:
        """Record ``result`` and apply its bookkeeping effect; return a stop reason if any.

        A ``cache_write`` on a session with its rewarm flag armed is
        tolerated rather than stopped: the flag is consumed (cleared) and the
        write is recorded as ``rewarm_write`` (see the "Rewarm" decision in
        ``docs/DESIGN.md``). Any other ``cache_write`` -- including a second
        one on the same session, since the flag is already spent -- stops
        the session exactly as before.
        """
        self._last_result[session_id] = result
        rewarm_consumed = False
        if result.outcome in ("ok", "cache_write"):
            state = self._store.get_state(session_id)
            if state is not None:
                state = dataclasses.replace(
                    state,
                    last_bump_at=self._clock.now(),
                    bump_count=state.bump_count + 1,
                )
                if result.outcome == "cache_write" and state.rewarm:
                    rewarm_consumed = True
                    assert result.usage is not None  # a cache_write always carries usage
                    state = dataclasses.replace(
                        state,
                        rewarm=False,
                        rewarm_write=RewarmWrite(at=self._clock.now(), usage=result.usage),
                    )
                self._store.put_state(state)
        if result.outcome == "unauthorized":
            return "unauthorized"
        if result.outcome == "rejected":
            return "rejected"
        if (
            result.outcome == "cache_write"
            and not rewarm_consumed
            and self._config.on_cache_write == "stop"
        ):
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

    def _deadline_stop_reason(
        self, session_id: str, state: SessionState, snapshot: Snapshot | None
    ) -> str | None:
        """Return the stop reason if ``state``'s deadline or max duration is already due.

        Checks, in priority order: an explicit ``until`` deadline; the
        waiting bound when there is no snapshot yet; otherwise the max
        duration measured from the last real request.
        """
        if state.until is not None:
            if self._clock.now() >= state.until:
                return "deadline"
        elif snapshot is None:
            if self._clock.now() >= self._waiting_bound(session_id, state):
                return "max_duration"
        elif (
            state.last_request_at is not None
            and self._clock.now() - state.last_request_at >= self._max_duration_s(state)
        ):
            return "max_duration"
        return None

    async def _run_session(self, session_id: str) -> None:
        """Task body: wait for the next bump, then bump, forever until disabled or stopped.

        Having no snapshot yet is a wait, not a stop: a session enabled
        before its first request (the ``/cache 2h`` typed as the opening
        prompt) keeps its deadline and overrides intact until
        :meth:`note_request` captures one, and only then starts bumping. That
        wait is bounded, not endless, and costs nothing while it lasts: it
        ends when a snapshot arrives, at the session's deadline, or at its
        max duration measured from the enable time, whichever comes first.
        """
        try:
            while True:
                state = self._store.get_state(session_id)
                if state is None or not state.enabled:
                    return
                snapshots = self._store.get_snapshots(session_id)
                snapshot = self._selector.select(snapshots)
                stop_reason = self._deadline_stop_reason(session_id, state, snapshot)
                if stop_reason is not None:
                    self._persist_stop(session_id, stop_reason)
                    return
                if snapshot is None:
                    await self._await_first_snapshot(session_id, state)
                    continue
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
                self._wakeups.pop(session_id, None)
