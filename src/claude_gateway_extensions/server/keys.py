"""Typed ``web.Application`` storage keys shared across the server modules.

Kept in their own module (rather than in ``app.py``) so ``passthrough.py``
and ``control.py`` can import them without a circular dependency on the
module that wires the application together. Uses ``web.AppKey`` (the
type-checked replacement for plain string keys) so ``request.app[KEY]``
resolves to the right type under pyright without casts.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp
from aiohttp import web

from claude_gateway_extensions.config import Config
from claude_gateway_extensions.core.clock import Clock
from claude_gateway_extensions.core.scheduler import Scheduler
from claude_gateway_extensions.core.selector import TargetSelector
from claude_gateway_extensions.core.store import SnapshotStore
from claude_gateway_extensions.core.upstream import Upstream


@dataclass
class DrainState:
    """Tracks in-flight HTTP requests and whether the daemon is currently draining.

    ``in_flight`` is incremented/decremented by
    :func:`~claude_gateway_extensions.server.app.track_in_flight` for every
    request (control API and passthrough alike); :func:`~.app.serve` reads it
    the moment a shutdown signal arrives to log and bound the drain wait, and
    :func:`~claude_gateway_extensions.server.control.health` reads
    ``draining`` to answer ``503`` instead of ``200``. Defined here (rather
    than in ``app.py``) for the same reason as the ``*_KEY`` constants below:
    ``control.py`` needs it without importing ``app.py``.
    """

    draining: bool = False
    in_flight: int = 0


CONFIG_KEY: web.AppKey[Config] = web.AppKey("config", Config)
STORE_KEY: web.AppKey[SnapshotStore] = web.AppKey("store", SnapshotStore)
SELECTOR_KEY: web.AppKey[TargetSelector] = web.AppKey("selector", TargetSelector)
SCHEDULER_KEY: web.AppKey[Scheduler] = web.AppKey("scheduler", Scheduler)
UPSTREAM_KEY: web.AppKey[Upstream] = web.AppKey("upstream", Upstream)
CLOCK_KEY: web.AppKey[Clock] = web.AppKey("clock", Clock)
CLIENT_SESSION_KEY: web.AppKey[aiohttp.ClientSession] = web.AppKey(
    "client_session", aiohttp.ClientSession
)
DRAIN_STATE_KEY: web.AppKey[DrainState] = web.AppKey("drain_state", DrainState)

#: Session ids that have already logged one capture-parse-failure warning
#: (see the "decode the private copy, warn once per session" behaviour in
#: ``passthrough._capture``), so a session stuck sending an unparseable body
#: logs exactly once instead of once per request.
CAPTURE_WARNED_KEY: web.AppKey[set[str]] = web.AppKey("capture_warned", set)
