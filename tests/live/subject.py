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
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: Environment variables passed through to the subject, by name. Everything
#: else is dropped: a subject launched with an agent session's environment
#: inherits that session's role and answers as it, which is a contaminated
#: subject rather than an isolated one (measured 2026-09-09). ``HOME`` is not
#: here: the subject always gets the fake one from `prepare_fake_home`,
#: never the parent's, so a missing override can never fall back to it.
ENV_ALLOWLIST = ("PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "SHELL", "TMPDIR")

#: The OAuth subscription login file, the only thing copied from the real
#: `~/.claude` into the subject's fake one. Everything else (settings.json,
#: hooks, skills, project state) stays on the real side unread.
CREDENTIALS_FILENAME = ".credentials.json"


class FakeHomeError(RuntimeError):
    """The subject's fake HOME could not be prepared."""


@dataclass(frozen=True)
class FakeHome:
    """The isolated HOME the subject is launched with."""

    home: Path
    config_dir: Path


def prepare_fake_home(scratch_dir: Path, real_home: Path) -> FakeHome:
    """Build a fake HOME under ``scratch_dir`` and seed it with only the login.

    The subject must authenticate as the user's subscription, but must never
    read the user's real settings, hooks, skills or `.claude.json`: those
    would make it a differently-configured client than the one being tested,
    and could run the user's own hooks against the harness's fake traffic.
    Copying nothing but the credentials file gives it a login and nothing
    else. Failing early and plainly when the file is absent beats a subject
    that reaches the input prompt and then cannot get past the login step.
    """
    real_credentials = real_home / ".claude" / CREDENTIALS_FILENAME
    if not real_credentials.is_file():
        raise FakeHomeError(
            f"{real_credentials} not found; the live harness needs an existing "
            "subscription login (run `claude` once interactively to create it) "
            "to authenticate the isolated subject"
        )
    home = scratch_dir / "home"
    config_dir = home / ".claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    fake_credentials = config_dir / CREDENTIALS_FILENAME
    shutil.copyfile(real_credentials, fake_credentials)
    fake_credentials.chmod(0o600)
    return FakeHome(home=home, config_dir=config_dir)


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
    fake_home: Path
    fake_config_dir: Path
    runner: Runner = _run
    sleep: Callable[[float], object] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    width: int = 200
    height: int = 50
    started: bool = field(default=False, init=False)

    def subject_env(self, parent_env: dict[str, str]) -> dict[str, str]:
        """Return the environment the subject is launched with.

        Built by allowlist from ``parent_env``, plus the isolation the
        project's test-subject rules require: ``HOME`` and
        ``CLAUDE_CONFIG_DIR`` always point at this subject's fake home
        (never the parent's, allowlisted or not), no
        ``ANTHROPIC_API_KEY``/``ANTHROPIC_AUTH_TOKEN`` (so the subject
        cannot reach a pay-as-you-go organisation), non-essential traffic
        off, and ``ANTHROPIC_BASE_URL`` pointed at the harness's own daemon.
        """
        env = {name: parent_env[name] for name in ENV_ALLOWLIST if name in parent_env}
        env["HOME"] = str(self.fake_home)
        env["CLAUDE_CONFIG_DIR"] = str(self.fake_config_dir)
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


__all__ = [
    "CREDENTIALS_FILENAME",
    "ENV_ALLOWLIST",
    "READY_MARKERS",
    "FakeHome",
    "FakeHomeError",
    "SubjectError",
    "TmuxSubject",
    "pane_is_ready",
    "prepare_fake_home",
]
