"""The daemon under test: a real `cgx serve` on its own port and state dir.

The harness never reuses a daemon the user is already running: a live run
writes snapshots, bumps and forks, and it asserts on a clean shutdown at the
end. Both are things to do to your own process, not to someone's.

Shutdown deliberately goes through the documented drain path (`SIGTERM`,
then wait for exit) rather than a kill, so the run also proves that the
daemon stops cleanly instead of only proving that it can be killed.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO


class DaemonError(RuntimeError):
    """The daemon failed to start, or failed to stop cleanly."""


def free_port() -> int:
    """Return a port number the OS just confirmed was free.

    Inherently racy against another process binding it in the meantime, but
    the alternative -- a fixed port -- collides with the user's own daemon
    every time, which is the failure that actually happens.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class Daemon:
    """A `cgx serve` child process owned by one live run."""

    port: int
    state_dir: Path
    log_path: Path
    cgx_bin: str = "cgx"
    upstream: str = "https://api.anthropic.com"
    log_level: str = "DEBUG"
    sleep: Callable[[float], object] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    process: subprocess.Popen[bytes] | None = field(default=None, init=False)
    _log_handle: IO[bytes] | None = field(default=None, init=False)

    @property
    def base_url(self) -> str:
        """The loopback URL the subject and the control client both use."""
        return f"http://127.0.0.1:{self.port}"

    def daemon_env(self, parent_env: dict[str, str]) -> dict[str, str]:
        """Return the daemon's environment: the parent's, plus this run's `CGX_*`.

        Unlike the subject, the daemon inherits the parent environment: it
        is not a model client, it needs whatever `PATH` and Python
        environment `cgx` was installed into, and `ANTHROPIC_BASE_URL` in
        the parent must not follow it (that would point the daemon at
        itself).
        """
        env = dict(parent_env)
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("CGX_URL", None)
        env["CGX_PORT"] = str(self.port)
        env["CGX_STATE_DIR"] = str(self.state_dir)
        env["CGX_UPSTREAM"] = self.upstream
        env["CGX_LOG_LEVEL"] = self.log_level
        return env

    def start(self, parent_env: dict[str, str], timeout_s: float = 30.0) -> None:
        """Launch `cgx serve` and block until it answers a health probe."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.log_path.open("wb")
        self._log_handle = handle
        self.process = subprocess.Popen(
            [self.cgx_bin, "serve"],
            env=self.daemon_env(parent_env),
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        self._wait_healthy(timeout_s)

    def _wait_healthy(self, timeout_s: float) -> None:
        deadline = self.monotonic() + timeout_s
        while self.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise DaemonError(
                    f"cgx serve exited with code {self.process.returncode} before becoming "
                    f"healthy; log: {self.log_path}"
                )
            if self.probe_health() == 200:
                return
            self.sleep(0.5)
        raise DaemonError(
            f"cgx serve was not healthy within {timeout_s:.0f}s; log: {self.log_path}"
        )

    def probe_health(self) -> int | None:
        """Return the health endpoint's status code, or None if unreachable."""
        try:
            with urllib.request.urlopen(f"{self.base_url}/cgx/health", timeout=2) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except OSError:
            return None

    def stop(self, timeout_s: float = 30.0) -> int | None:
        """Stop the daemon through its drain path and return its exit code.

        Idempotent, and never raises: teardown must complete even when the
        run is already failing. A daemon that outlives the timeout is killed
        and reports a non-zero code, which the caller asserts on.
        """
        process = self.process
        if process is None:
            return None
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.send_signal(signal.SIGTERM)
            deadline = self.monotonic() + timeout_s
            while process.poll() is None and self.monotonic() < deadline:
                self.sleep(0.2)
            if process.poll() is None:
                process.kill()
        code = process.wait()
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        self.process = None
        return code

    def log_tail(self, lines: int = 40) -> str:
        """Return the last ``lines`` of the daemon log, for a failure report."""
        if not self.log_path.is_file():
            return ""
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])


def default_cgx_bin() -> str:
    """Return the `cgx` executable to run: the venv's if present, else PATH's."""
    venv_cgx = Path(os.getcwd()) / ".venv" / "bin" / "cgx"
    return str(venv_cgx) if venv_cgx.is_file() else "cgx"


__all__ = ["Daemon", "DaemonError", "default_cgx_bin", "free_port"]
