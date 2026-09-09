"""The test subject: a real interactive Claude Code session in a detached
tmux session.

`--print` is a different client from the TUI a user runs, so a guard built
on it would not fail where users are hurt. tmux gives a real terminal
without needing a visible window, which is what makes the run unattended.

Input goes in with `send-keys`. Pane text comes back only for readiness
detection and for the diagnostic attached to a failure -- never for an
assertion, which reads the control API or the transcript instead.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: Environment variables passed through to the subject, by name. Everything
#: else is dropped: a subject launched with an agent session's environment
#: inherits that session's role and answers as it, which is a contaminated
#: subject rather than an isolated one (measured 2026-09-09).
ENV_ALLOWLIST = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "SHELL", "TMPDIR")

#: Prompt characters the TUI draws once it is waiting for input. Matched
#: only at the start of a line (after the pane's own padding) so ordinary
#: output containing an angle bracket is not read as readiness. Readiness
#: detection only; an assertion never reads the pane.
READY_MARKERS = ("❯", ">")  # noqa: RUF001 - the TUI's own prompt ornament, and an ASCII fallback

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class SubjectError(RuntimeError):
    """The subject could not be launched, driven or stopped."""


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def pane_is_ready(pane: str) -> bool:
    """True when the pane shows an input prompt at the start of some line."""
    return any(line.strip().startswith(READY_MARKERS) for line in pane.splitlines())


@dataclass
class TmuxSubject:
    """An interactive Claude Code session running in `tmux new-session -d`.

    ``runner`` is injected so the offline tests can drive a fake tmux; it
    takes an argv sequence and returns a completed process.
    """

    session_name: str
    cwd: Path
    claude_bin: Path
    model: str
    base_url: str
    runner: Runner = _run
    sleep: Callable[[float], object] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    width: int = 200
    height: int = 50
    started: bool = field(default=False, init=False)

    def subject_env(self, parent_env: dict[str, str]) -> dict[str, str]:
        """Return the environment the subject is launched with.

        Built by allowlist from ``parent_env``, plus the isolation the
        project's test-subject rules require: no
        ``ANTHROPIC_API_KEY``/``ANTHROPIC_AUTH_TOKEN`` (so the subject
        cannot reach a pay-as-you-go organisation), non-essential traffic
        off, and ``ANTHROPIC_BASE_URL`` pointed at the harness's own daemon.
        """
        env = {name: parent_env[name] for name in ENV_ALLOWLIST if name in parent_env}
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env["ANTHROPIC_BASE_URL"] = self.base_url
        env.setdefault("TERM", "xterm-256color")
        return env

    def launch_argv(self) -> list[str]:
        """Return the claude argv: interactive, safe-mode, named as a test subject."""
        return [
            str(self.claude_bin),
            "--model",
            self.model,
            "--safe-mode",
            "--name",
            self.session_name,
        ]

    def start(self, parent_env: dict[str, str]) -> None:
        """Create the detached tmux session running the subject."""
        env = self.subject_env(parent_env)
        assignments = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(env.items()))
        command = f"env -i {assignments} {shlex.join(self.launch_argv())}"
        result = self.runner(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                self.session_name,
                "-x",
                str(self.width),
                "-y",
                str(self.height),
                "-c",
                str(self.cwd),
                command,
            ]
        )
        if result.returncode != 0:
            raise SubjectError(f"tmux new-session failed: {result.stderr.strip()}")
        self.started = True

    def capture(self) -> str:
        """Return the current pane text (diagnostics and readiness only)."""
        result = self.runner(["tmux", "capture-pane", "-p", "-t", self.session_name])
        if result.returncode != 0:
            raise SubjectError(f"tmux capture-pane failed: {result.stderr.strip()}")
        return result.stdout

    def wait_ready(self, timeout_s: float, poll_s: float = 1.0) -> None:
        """Block until the TUI is waiting for input.

        A fresh scratch cwd shows no trust dialog on the builds this was
        measured against, so anything still on screen at the timeout is an
        unexpected first-run interaction: the harness fails with the pane
        attached rather than typing at it, which would encode one build's
        key order and silently mis-answer the next.
        """
        deadline = self.monotonic() + timeout_s
        pane = ""
        while self.monotonic() < deadline:
            pane = self.capture()
            if pane_is_ready(pane):
                return
            self.sleep(poll_s)
        raise SubjectError(
            f"subject did not reach an input prompt within {timeout_s:.0f}s. "
            f"If a first-run dialog is on screen below, the harness must learn to "
            f"fail on it explicitly, never to answer it:\n{pane}"
        )

    def send(self, text: str) -> None:
        """Type ``text`` into the subject and submit it."""
        literal = self.runner(["tmux", "send-keys", "-t", self.session_name, "-l", text])
        if literal.returncode != 0:
            raise SubjectError(f"tmux send-keys failed: {literal.stderr.strip()}")
        # Submitted as a separate keystroke: `-l` sends the text literally,
        # so a trailing newline inside it would be typed, not entered.
        enter = self.runner(["tmux", "send-keys", "-t", self.session_name, "Enter"])
        if enter.returncode != 0:
            raise SubjectError(f"tmux send-keys Enter failed: {enter.stderr.strip()}")

    def stop(self) -> None:
        """Kill the tmux session. Idempotent, and never raises."""
        if not self.started:
            return
        self.started = False
        self.runner(["tmux", "kill-session", "-t", self.session_name])


__all__ = ["ENV_ALLOWLIST", "READY_MARKERS", "SubjectError", "TmuxSubject", "pane_is_ready"]
