"""aiohttp server layer: transparent passthrough, tee capture, control API.

:func:`run` is the package's synchronous entry point, called by
``cgx serve`` after it imports this package lazily (so no other subcommand
pays for ``aiohttp``). Importing this package therefore also imports
``aiohttp``, which is what lets ``cgx serve`` report a missing dependency as
a clean failure rather than a traceback.
"""

from __future__ import annotations

import asyncio

from claude_gateway_extensions.config import Config
from claude_gateway_extensions.server.app import make_app, serve


def run(config: Config) -> None:
    """Serve the proxy until interrupted, then shut down cleanly.

    Blocking wrapper around :func:`serve` for the CLI, which has no event
    loop of its own. ``Ctrl-C`` is a normal way to stop the daemon, so a
    ``KeyboardInterrupt`` returns rather than propagating a traceback.
    """
    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        return


__all__ = ["make_app", "run", "serve"]
