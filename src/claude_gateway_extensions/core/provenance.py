"""Best-effort git provenance for the running daemon, and a tree view around it.

:func:`resolve_git_provenance` is meant to be called exactly once, when the
daemon starts (never from the request path): it walks up from a starting
file to find the nearest ``.git``, then resolves the checkout's commit,
branch and dirty flag. It returns ``None`` whenever any of that is
unavailable -- the package installed without a checkout (e.g. from a wheel),
git itself missing, or any subprocess failure -- so a daemon running from a
plain install starts and serves normally with no provenance to report.

:func:`format_commit_tree` is a pure formatter (a fake commit list in, a
string out) so its shape is testable without a git checkout.
:func:`gather_commit_tree` is the git-backed, best-effort orchestrator
around it: given a checkout directory and an already-resolved running
commit/branch, it lists the surrounding commits and renders the tree, or
returns ``None`` on any failure. See the "Status: git tree view" decision in
``docs/DESIGN.md`` for why this runs client-side, from the daemon's reported
commit, rather than as daemon-side work on every status request.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_gateway_extensions.core.status_format import format_duration, format_local_datetime


def find_git_checkout(start: Path) -> Path | None:
    """Walk up from ``start`` (a file or directory) to the nearest ``.git``, or ``None``.

    Used both to locate the checkout a running package file belongs to (for
    :func:`resolve_git_provenance`) and, client-side, to locate the checkout
    the ``cgx`` command itself is running from (for :func:`gather_commit_tree`).
    """
    current = start if start.is_dir() else start.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _git_line(repo_dir: Path, args: list[str]) -> str | None:
    """One-shot ``git -C repo_dir <args>``, stripped stdout or ``None`` on any failure."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_dir), *args],
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            text=True,
        ).strip()
        return out or None
    except Exception:
        return None


def resolve_git_provenance(start: Path) -> dict[str, Any] | None:
    """Best-effort commit/branch/dirty for the checkout containing ``start``.

    Returns ``None`` when no ``.git`` is found above ``start``, or git
    itself fails to answer any of the three queries. Call once at process
    start; never from the request path.
    """
    repo_dir = find_git_checkout(start)
    if repo_dir is None:
        return None

    commit = _git_line(repo_dir, ["rev-parse", "--short=8", "HEAD"])
    if not commit:
        return None

    branch_raw = _git_line(repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch: str | None = None
    if branch_raw and branch_raw != "HEAD":
        branch = branch_raw
    elif branch_raw == "HEAD":
        branch = "detached"

    status_out = _git_line(repo_dir, ["status", "--porcelain=v1", "-uno"])
    dirty = bool(status_out)

    return {"repo_dir": str(repo_dir), "commit": commit, "branch": branch, "dirty": dirty}


@dataclass
class TreeCommit:
    """One commit line in the tree view: short hash plus subject."""

    short_hash: str
    subject: str


def _tree_c(code: str, text: str, color: bool) -> str:
    """Wrap *text* in an ANSI escape *code*, only when *color* is true."""
    return f"\033[{code}m{text}\033[0m" if color else text


def format_commit_tree(
    *,
    older: list[TreeCommit],
    running: TreeCommit,
    newer: list[TreeCommit],
    branch: str,
    dirty: bool,
    uptime_human: str,
    started_local: str | None,
    tip_ref_label: str,
    tip_author: str | None = None,
    tip_authored_ago: str | None = None,
    tip_committed_local: str | None = None,
    color: bool = False,
) -> str:
    """Render a git-log tree view around the running commit.

    Pure formatting: takes already-resolved commit lists so it can be
    tested without a git checkout. Oldest at top, branch tip at bottom.
    ``newer`` holds the commits strictly after ``running`` up to and
    including the branch tip, in oldest-first order; its length is the
    "behind" count. An empty ``newer`` means ``running`` is the tip.
    """

    def faint(text: str) -> str:
        return _tree_c("2", text, color)

    def hashc(text: str) -> str:
        return _tree_c("33", text, color)

    def refc(text: str) -> str:
        return _tree_c("36", text, color)

    uptime_display = _tree_c("1;32", uptime_human, color)

    lines = [faint("▲"), faint("│")]

    for commit in older:
        lines.append(f"{faint('├──')} {hashc(commit.short_hash)} {commit.subject}")

    at_tip = len(newer) == 0
    behind_count = len(newer)
    state = "dirty" if dirty else "clean"

    if at_tip:
        badge_code = "1;31" if dirty else "32"
        badge = _tree_c(badge_code, "[RUNNING]", color)
        ref = refc(f"({tip_ref_label})")
        lines.append(f"{faint('└──')} {hashc(running.short_hash)} {badge} {ref} {running.subject}")
        sub = f"up to date with {branch} ({state}) • up {uptime_display}"
        if started_local:
            sub += f" • started {started_local}"
        lines.append(f"{faint('    └──')} {sub}")
        return "\n".join(lines)

    badge_code = "1;31" if dirty else "33"
    badge = _tree_c(badge_code, "[RUNNING]", color)
    lines.append(f"{faint('├──')} {hashc(running.short_hash)} {badge} {running.subject}")
    sub = f"{behind_count} commits behind {branch} ({state}) • up {uptime_display}"
    if started_local:
        sub += f" • started {started_local}"
    lines.append(f"{faint('│   └──')} {sub}")

    if behind_count <= 5:
        shown, tip = newer[:-1], newer[-1]
        for commit in shown:
            lines.append(f"{faint('├──')} {hashc(commit.short_hash)} {commit.subject}")
    else:
        head, tail = newer[:2], newer[-2:]
        omitted = behind_count - 4
        for commit in head:
            lines.append(f"{faint('├──')} {hashc(commit.short_hash)} {commit.subject}")
        lines.append(faint("┆"))
        lines.append(faint(f"┆   ... {omitted} commits omitted ..."))
        lines.append(faint("┆"))
        lines.append(f"{faint('├──')} {hashc(tail[0].short_hash)} {tail[0].subject}")
        tip = tail[1]

    ref = refc(f"({tip_ref_label})")
    lines.append(f"{faint('└──')} {hashc(tip.short_hash)} {ref} {tip.subject}")

    tip_sub_bits = []
    if tip_authored_ago and tip_author:
        tip_sub_bits.append(f"authored {tip_authored_ago} by {tip_author}")
    if tip_committed_local:
        tip_sub_bits.append(f"committed {tip_committed_local}")
    if tip_sub_bits:
        lines.append(f"{faint('    └──')} " + " • ".join(tip_sub_bits))

    return "\n".join(lines)


def _parse_commit_lines(raw: str | None) -> list[TreeCommit]:
    if not raw:
        return []
    commits = []
    for line in raw.splitlines():
        if "\x1f" not in line:
            continue
        short_hash, subject = line.split("\x1f", 1)
        commits.append(TreeCommit(short_hash=short_hash, subject=subject))
    return commits


def gather_commit_tree(
    *,
    repo_dir: Path,
    commit: str,
    branch: str | None,
    dirty: bool,
    uptime_seconds: float,
    started_local: str | None,
    color: bool = False,
) -> str | None:
    """Best-effort git-log tree around the running commit, or ``None``.

    Runs entirely against ``repo_dir`` (the checkout the caller -- the
    ``cgx`` CLI -- is itself running from), using ``commit``/``branch``/
    ``dirty`` as already resolved (typically the daemon's own reported
    values from ``GET /cgx/health``). Returns ``None`` whenever git info is
    unavailable (no branch, detached HEAD, or any subprocess failure) so
    callers can silently omit the tree rather than crash or clutter status
    output.
    """
    if not commit or not branch or branch == "detached":
        return None

    try:
        tip_hash = _git_line(repo_dir, ["rev-parse", "--short=8", branch])
        if not tip_hash:
            return None

        origin_hash = _git_line(repo_dir, ["rev-parse", "--short=8", f"origin/{branch}"])
        tip_ref_label = f"origin/{branch}" if origin_hash == tip_hash else branch

        behind_raw = _git_line(repo_dir, ["rev-list", "--count", f"{commit}..{branch}"])
        if behind_raw is None or not behind_raw.isdigit():
            return None
        behind_count = int(behind_raw)

        # --abbrev=8 forces the same 8-char length as the --short=8 calls
        # above: %h's own abbreviation length is otherwise whatever git
        # judges unique for the repo (7 chars is common), which would make
        # the running/tip hashes disagree in width with the rest of the
        # tree's hashes for no reason a reader could see.
        running_raw = _git_line(repo_dir, ["log", "-1", "--abbrev=8", "--format=%h\x1f%s", commit])
        if running_raw is None:
            return None
        running_commits = _parse_commit_lines(running_raw)
        if not running_commits:
            return None
        running = running_commits[0]

        older_raw = _git_line(
            repo_dir, ["log", "--abbrev=8", "--format=%h\x1f%s", "--skip=1", "-n", "3", commit]
        )
        older = list(reversed(_parse_commit_lines(older_raw)))

        newer: list[TreeCommit] = []
        tip_author = tip_authored_ago = tip_committed_local = None
        if behind_count > 0:
            newer_raw = _git_line(
                repo_dir,
                ["log", "--abbrev=8", "--format=%h\x1f%s", "--reverse", f"{commit}..{branch}"],
            )
            newer = _parse_commit_lines(newer_raw)
            if len(newer) != behind_count:
                return None

            tip_meta_raw = _git_line(repo_dir, ["log", "-1", "--format=%an\x1f%at\x1f%cI", branch])
            if tip_meta_raw:
                parts = tip_meta_raw.split("\x1f")
                if len(parts) == 3:
                    tip_author, author_epoch_raw, committer_iso = parts
                    try:
                        author_epoch = float(author_epoch_raw)
                        tip_authored_ago = format_duration(time.time() - author_epoch) + " ago"
                    except ValueError:
                        pass
                    try:
                        committer_dt = datetime.fromisoformat(committer_iso)
                        tip_committed_local = format_local_datetime(committer_dt.timestamp())
                    except ValueError:
                        pass

        return format_commit_tree(
            older=older,
            running=running,
            newer=newer,
            branch=branch,
            dirty=dirty,
            uptime_human=format_duration(uptime_seconds),
            started_local=started_local,
            tip_ref_label=tip_ref_label,
            tip_author=tip_author,
            tip_authored_ago=tip_authored_ago,
            tip_committed_local=tip_committed_local,
            color=color,
        )
    except Exception:
        return None


__all__ = [
    "TreeCommit",
    "find_git_checkout",
    "format_commit_tree",
    "gather_commit_tree",
    "resolve_git_provenance",
]
