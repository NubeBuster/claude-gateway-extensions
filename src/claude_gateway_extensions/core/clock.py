"""Time abstraction so the scheduler is deterministically testable.

``Scheduler`` never calls :func:`time.time`, :func:`time.monotonic` or
:func:`asyncio.sleep` directly; it goes through a :class:`Clock`. Production
code uses :class:`SystemClock`; tests use :class:`FakeClock`, which is
advanced explicitly instead of sleeping wall-clock time.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    """Abstraction over wall-clock time, monotonic time, and sleeping."""

    def now(self) -> float:
        """Return the current epoch time in seconds, as by ``time.time()``."""
        ...

    def monotonic(self) -> float:
        """Return a monotonic timestamp in seconds, as by ``time.monotonic()``."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend the caller for ``seconds`` seconds."""
        ...

    async def wait_for_event(self, event: asyncio.Event, seconds: float) -> bool:
        """Wait until ``event`` is set or ``seconds`` elapse; return whether it was set."""
        ...


class SystemClock:
    """Real-time :class:`Clock` backed by the standard library."""

    def now(self) -> float:
        """Return ``time.time()``."""
        return time.time()

    def monotonic(self) -> float:
        """Return ``time.monotonic()``."""
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """Await ``asyncio.sleep(seconds)``."""
        await asyncio.sleep(seconds)

    async def wait_for_event(self, event: asyncio.Event, seconds: float) -> bool:
        """Wait on ``event`` with an ``asyncio`` timeout; return whether it was set.

        One timer, no polling: the caller stays parked until the event is set
        or the timeout fires.
        """
        if seconds <= 0:
            return event.is_set()
        try:
            await asyncio.wait_for(event.wait(), seconds)
        except TimeoutError:
            return False
        return True


class FakeClock:
    """Deterministic :class:`Clock` for tests.

    ``now()`` and ``monotonic()`` both track the same internal counter,
    seeded at construction and moved forward only by :meth:`advance`.
    :meth:`sleep` does not block; it advances the clock by ``seconds`` and
    yields control once so waiting coroutines observe the new time.
    """

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        """Seed the fake clock at ``start`` epoch seconds."""
        self._current = start

    def now(self) -> float:
        """Return the current fake epoch time."""
        return self._current

    def monotonic(self) -> float:
        """Return the current fake monotonic time (same counter as ``now``)."""
        return self._current

    async def sleep(self, seconds: float) -> None:
        """Advance the fake clock by ``seconds`` and yield control once."""
        self.advance(seconds)
        await asyncio.sleep(0)

    async def wait_for_event(self, event: asyncio.Event, seconds: float) -> bool:
        """Yield until ``event`` is set or the fake clock reaches the timeout.

        Deliberately does not advance the clock, unlike :meth:`sleep`: a
        waiter is the one thing that must not move time forward on its own,
        or a test could never observe the wait it is asserting about. Tests
        drive it with :meth:`advance` (timeout) or by setting the event.
        """
        deadline = self._current + seconds
        while not event.is_set():
            if self._current >= deadline:
                return False
            await asyncio.sleep(0)
        return True

    def advance(self, seconds: float) -> None:
        """Move the fake clock forward by ``seconds`` without yielding."""
        self._current += seconds
