"""Tests for the server package's synchronous ``run`` entry point.

``cgx serve`` imports ``claude_gateway_extensions.server`` lazily and calls
``run(config)``; these tests pin that contract from the server side.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from claude_gateway_extensions import server
from conftest import make_config


def test_run_drives_serve_on_a_fresh_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path, "http://127.0.0.1:1")
    seen: list[Any] = []

    def fake_asyncio_run(coro: Any) -> None:
        seen.append(coro)
        coro.close()

    monkeypatch.setattr(asyncio, "run", fake_asyncio_run)
    server.run(config)
    assert len(seen) == 1
    assert seen[0].__qualname__ == "serve"


def test_run_swallows_keyboard_interrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path, "http://127.0.0.1:1")

    def fake_asyncio_run(coro: Any) -> None:
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", fake_asyncio_run)
    server.run(config)  # must not raise
