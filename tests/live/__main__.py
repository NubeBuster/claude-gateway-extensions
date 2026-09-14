"""`make live` entry point: parse the run's arguments, run it, print the table.

Deliberately not a `pytest` test and not a `cgx` subcommand. It spends real
money against the real API, so it must be asked for explicitly, with a cap
on the argument line -- see `docs/DESIGN.md`, "Live regression harness".
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from .daemon import Daemon, default_cgx_bin, free_port
from .harness import LiveRun, RunConfig
from .subject import FakeHomeError, TmuxSubject, prepare_fake_home

#: Where the run keeps its scratch cwd, daemon state and logs. Fixed rather
#: than a fresh temp directory per run: a stable path keeps whatever state
#: Claude Code associates with a project from being recreated every time,
#: and makes a failed run's artefacts findable afterwards.
DEFAULT_SCRATCH = Path("/tmp/scripts/cgx-live")


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for `python -m live`."""
    parser = argparse.ArgumentParser(
        prog="make live",
        description=(
            "Run one live regression check: a real interactive Claude Code session "
            "through a real cgx daemon to the real Anthropic API, asserting that "
            "bumps and shadow forks read the prompt cache instead of paying to "
            "rewrite it. This spends money on the account the session is signed "
            "in with."
        ),
    )
    parser.add_argument(
        "--model",
        default="haiku",
        help=(
            "subject model (default: haiku, the cheapest). haiku asserts that a "
            "fork is refused as thinking-unsafe; sonnet or opus assert that a "
            "fork reads the cached prefix."
        ),
    )
    parser.add_argument("--tag", default="A", help="run tag in the subject's name (default: A)")
    parser.add_argument(
        "--budget-usd",
        type=float,
        default=1.0,
        help="abort before any step that would push the estimate past this (default: 1.0)",
    )
    parser.add_argument(
        "--expect-fork",
        choices=("cache_read", "refusal"),
        default=None,
        help="what a fork should do, for a model family the harness has no entry for",
    )
    parser.add_argument(
        "--claude-bin",
        default=None,
        help="path to the claude executable (default: the one on PATH)",
    )
    parser.add_argument(
        "--cgx-bin",
        default=None,
        help="path to the cgx executable (default: ./.venv/bin/cgx, else PATH)",
    )
    parser.add_argument(
        "--scratch",
        type=Path,
        default=DEFAULT_SCRATCH,
        help=f"scratch directory for cwd, state and logs (default: {DEFAULT_SCRATCH})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="port for this run's daemon (default: one the OS says is free)",
    )
    parser.add_argument(
        "--turn-timeout-s",
        type=float,
        default=240.0,
        help="how long one subject turn may take (default: 240)",
    )
    return parser


def resolve_claude_bin(explicit: str | None) -> Path:
    """Return the claude executable to launch, or fail with a clear message."""
    found = explicit or shutil.which("claude")
    if not found:
        raise SystemExit(
            "no 'claude' executable found on PATH; pass --claude-bin. "
            "A PTY wrapper named 'claude' is not the binary: point this at the real one."
        )
    return Path(found)


def require_tmux() -> None:
    """Fail early and plainly when tmux is missing."""
    if shutil.which("tmux") is None:
        raise SystemExit("tmux is required: the subject runs in a detached tmux session")


def main(argv: list[str] | None = None) -> int:
    """Run one live check and return a process exit code."""
    args = build_parser().parse_args(argv)
    require_tmux()

    real_home = Path(os.path.expanduser("~"))
    try:
        fake_home = prepare_fake_home(args.scratch, real_home)
    except FakeHomeError as exc:
        raise SystemExit(str(exc)) from exc

    config = RunConfig(
        claude_bin=resolve_claude_bin(args.claude_bin),
        cgx_bin=args.cgx_bin or default_cgx_bin(),
        scratch_dir=args.scratch,
        subject_config_dir=fake_home.config_dir,
        real_home=real_home,
        model=args.model,
        tag=args.tag,
        budget_usd=args.budget_usd,
        turn_timeout_s=args.turn_timeout_s,
        fork_expectation=args.expect_fork,
    )
    port = args.port or free_port()
    daemon = Daemon(
        port=port,
        state_dir=config.scratch_dir / "state",
        log_path=config.scratch_dir / "daemon.log",
        cgx_bin=config.cgx_bin,
    )
    subject = TmuxSubject(
        session_name=config.session_name,
        cwd=config.subject_cwd,
        claude_bin=config.claude_bin,
        model=config.model,
        base_url=daemon.base_url,
        fake_home=fake_home.home,
        fake_config_dir=fake_home.config_dir,
    )

    print(
        f"live: subject {config.session_name} in {config.subject_cwd}, "
        f"HOME={fake_home.home}, daemon on :{port}, cap ${config.budget_usd:.2f}",
        flush=True,
    )
    report = LiveRun(config, dict(os.environ), daemon, subject).execute()
    print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
