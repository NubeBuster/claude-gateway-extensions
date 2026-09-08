"""Typed ``web.Application`` storage keys shared across the server modules.

Kept in their own module (rather than in ``app.py``) so ``passthrough.py``
and ``control.py`` can import them without a circular dependency on the
module that wires the application together. Uses ``web.AppKey`` (the
type-checked replacement for plain string keys) so ``request.app[KEY]``
resolves to the right type under pyright without casts.
"""

from __future__ import annotations

import aiohttp
from aiohttp import web

from claude_cache_bump_gateway.config import Config
from claude_cache_bump_gateway.core.clock import Clock
from claude_cache_bump_gateway.core.scheduler import Scheduler
from claude_cache_bump_gateway.core.selector import TargetSelector
from claude_cache_bump_gateway.core.store import SnapshotStore
from claude_cache_bump_gateway.core.upstream import Upstream

CONFIG_KEY: web.AppKey[Config] = web.AppKey("config", Config)
STORE_KEY: web.AppKey[SnapshotStore] = web.AppKey("store", SnapshotStore)
SELECTOR_KEY: web.AppKey[TargetSelector] = web.AppKey("selector", TargetSelector)
SCHEDULER_KEY: web.AppKey[Scheduler] = web.AppKey("scheduler", Scheduler)
UPSTREAM_KEY: web.AppKey[Upstream] = web.AppKey("upstream", Upstream)
CLOCK_KEY: web.AppKey[Clock] = web.AppKey("clock", Clock)
CLIENT_SESSION_KEY: web.AppKey[aiohttp.ClientSession] = web.AppKey(
    "client_session", aiohttp.ClientSession
)
