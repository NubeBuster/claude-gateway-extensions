"""Tests for claude_gateway_extensions.core.provenance."""

from __future__ import annotations

import subprocess
from pathlib import Path

from claude_gateway_extensions.core.provenance import (
    TreeCommit,
    find_git_checkout,
    format_commit_tree,
    gather_commit_tree,
    resolve_git_provenance,
)


def _commits(n: int, prefix: str = "c") -> list[TreeCommit]:
    return [
        TreeCommit(short_hash=f"{prefix}{i:07d}", subject=f"subject {prefix}{i}") for i in range(n)
    ]


# -- format_commit_tree: pure formatting, no git involved ---------------------


def test_format_commit_tree_at_tip() -> None:
    older = _commits(3, "o")
    running = TreeCommit(short_hash="f4df9bfa", subject="feat(proxy): thing")
    text = format_commit_tree(
        older=older,
        running=running,
        newer=[],
        branch="main",
        dirty=False,
        uptime_human="14h 30m",
        started_local="2026-09-15 07:43:00 CEST",
        tip_ref_label="origin/main",
    )
    lines = text.splitlines()
    assert lines[0] == "▲"
    assert lines[1] == "│"
    for i, commit in enumerate(older):
        assert lines[2 + i] == f"├── {commit.short_hash} {commit.subject}"
    assert "[RUNNING]" in lines[-2]
    assert "(origin/main)" in lines[-2]
    assert lines[-2].startswith("└── f4df9bfa")
    assert "up to date with main (clean)" in lines[-1]
    assert "up 14h 30m" in lines[-1]
    assert "started 2026-09-15 07:43:00 CEST" in lines[-1]
    assert "commits behind" not in text


def test_format_commit_tree_behind_le_5_no_omission() -> None:
    running = TreeCommit(short_hash="f4df9bfa", subject="feat(proxy): thing")
    newer = _commits(3, "n")
    text = format_commit_tree(
        older=_commits(2, "o"),
        running=running,
        newer=newer,
        branch="main",
        dirty=True,
        uptime_human="14h 30m",
        started_local="2026-09-15 07:43:00 CEST",
        tip_ref_label="main",
        tip_author="Jane Dev",
        tip_authored_ago="14h ago",
        tip_committed_local="2026-09-15 07:43:00 CEST",
    )
    assert "... commits omitted ..." not in text
    assert "┆" not in text
    assert "3 commits behind main (dirty)" in text
    assert "[RUNNING]" in text
    for commit in newer[:-1]:
        assert f"├── {commit.short_hash} {commit.subject}" in text
    tip = newer[-1]
    assert f"└── {tip.short_hash} (main) {tip.subject}" in text
    assert "authored 14h ago by Jane Dev" in text
    assert "committed 2026-09-15 07:43:00 CEST" in text


def test_format_commit_tree_behind_gt_5_has_omission_block() -> None:
    running = TreeCommit(short_hash="f4df9bfa", subject="feat(proxy): thing")
    newer = _commits(8, "n")
    text = format_commit_tree(
        older=[],
        running=running,
        newer=newer,
        branch="main",
        dirty=False,
        uptime_human="14h 30m",
        started_local=None,
        tip_ref_label="origin/main",
        tip_author="Jane Dev",
        tip_authored_ago="14h ago",
        tip_committed_local="2026-09-15 07:43:00 CEST",
    )
    assert "8 commits behind main (clean)" in text
    assert "... 4 commits omitted ..." in text
    for commit in newer[:2]:
        assert f"├── {commit.short_hash} {commit.subject}" in text
    assert f"├── {newer[-2].short_hash} {newer[-2].subject}" in text
    tip = newer[-1]
    assert f"└── {tip.short_hash} (origin/main) {tip.subject}" in text
    for commit in newer[2:-2]:
        assert commit.short_hash not in text


def test_format_commit_tree_badge_colours_dirty_behind_and_at_tip() -> None:
    running = TreeCommit(short_hash="f4df9bfa", subject="feat: thing")

    dirty_text = format_commit_tree(
        older=[],
        running=running,
        newer=_commits(1, "n"),
        branch="main",
        dirty=True,
        uptime_human="1h",
        started_local=None,
        tip_ref_label="main",
        tip_author="A",
        tip_authored_ago="1h ago",
        tip_committed_local="x",
        color=True,
    )
    assert "\033[1;31m[RUNNING]\033[0m" in dirty_text

    behind_text = format_commit_tree(
        older=[],
        running=running,
        newer=_commits(1, "n"),
        branch="main",
        dirty=False,
        uptime_human="1h",
        started_local=None,
        tip_ref_label="main",
        tip_author="A",
        tip_authored_ago="1h ago",
        tip_committed_local="x",
        color=True,
    )
    assert "\033[33m[RUNNING]\033[0m" in behind_text

    tip_text = format_commit_tree(
        older=[],
        running=running,
        newer=[],
        branch="main",
        dirty=False,
        uptime_human="1h",
        started_local=None,
        tip_ref_label="main",
        color=True,
    )
    assert "\033[32m[RUNNING]\033[0m" in tip_text
    assert "\033[33mf4df9bfa\033[0m" in tip_text
    assert "\033[36m(main)\033[0m" in tip_text


def test_format_commit_tree_uptime_highlighted_only_with_color() -> None:
    running = TreeCommit(short_hash="f4df9bfa", subject="feat: thing")

    plain = format_commit_tree(
        older=[],
        running=running,
        newer=[],
        branch="main",
        dirty=False,
        uptime_human="14h 30m",
        started_local=None,
        tip_ref_label="main",
    )
    assert "\033[" not in plain
    assert "up 14h 30m" in plain

    colored = format_commit_tree(
        older=[],
        running=running,
        newer=[],
        branch="main",
        dirty=False,
        uptime_human="14h 30m",
        started_local=None,
        tip_ref_label="main",
        color=True,
    )
    assert "\033[1;32m14h 30m\033[0m" in colored


# -- gather_commit_tree: git-backed, against a disposable throwaway repo ------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _make_repo(tmp_path: Path, n_commits: int) -> tuple[Path, list[str]]:
    """Build a throwaway repo on ``main`` with ``n_commits`` linear commits.

    Returns the repo path and the list of short hashes, oldest first.
    Deliberately isolated (a fresh ``tmp_path`` repo, never the checkout
    this test suite itself lives in) so the test's outcome does not depend
    on the machine's real git history.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    hashes = []
    for i in range(n_commits):
        (repo / "f.txt").write_text(f"{i}\n")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-q", "-m", f"commit {i}")
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short=8", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        hashes.append(out.stdout.strip())
    return repo, hashes


def test_gather_commit_tree_at_tip(tmp_path: Path) -> None:
    repo, hashes = _make_repo(tmp_path, 3)
    tree = gather_commit_tree(
        repo_dir=repo,
        commit=hashes[-1],
        branch="main",
        dirty=False,
        uptime_seconds=3600,
        started_local="2026-09-15 07:43:00 CEST",
    )
    assert tree is not None
    assert "up to date with main" in tree
    assert "[RUNNING]" in tree
    assert hashes[-1] in tree


def test_gather_commit_tree_behind_with_omission(tmp_path: Path) -> None:
    repo, hashes = _make_repo(tmp_path, 10)
    tree = gather_commit_tree(
        repo_dir=repo,
        commit=hashes[2],
        branch="main",
        dirty=True,
        uptime_seconds=3600,
        started_local=None,
    )
    assert tree is not None
    assert "7 commits behind main (dirty)" in tree
    assert "... 3 commits omitted ..." in tree
    assert hashes[-1] in tree  # tip
    assert "authored" in tree
    assert "committed" in tree


def test_gather_commit_tree_none_when_branch_missing_or_detached(tmp_path: Path) -> None:
    repo, hashes = _make_repo(tmp_path, 1)
    assert (
        gather_commit_tree(
            repo_dir=repo,
            commit=hashes[0],
            branch=None,
            dirty=False,
            uptime_seconds=1,
            started_local=None,
        )
        is None
    )
    assert (
        gather_commit_tree(
            repo_dir=repo,
            commit=hashes[0],
            branch="detached",
            dirty=False,
            uptime_seconds=1,
            started_local=None,
        )
        is None
    )


def test_gather_commit_tree_none_on_invalid_repo(tmp_path: Path) -> None:
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    assert (
        gather_commit_tree(
            repo_dir=empty,
            commit="deadbeef",
            branch="main",
            dirty=False,
            uptime_seconds=1,
            started_local=None,
        )
        is None
    )


# -- resolve_git_provenance / find_git_checkout -------------------------------


def test_find_git_checkout_locates_dot_git(tmp_path: Path) -> None:
    repo, _ = _make_repo(tmp_path, 1)
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    assert find_git_checkout(nested / "file.py") == repo


def test_find_git_checkout_none_outside_a_repo(tmp_path: Path) -> None:
    assert find_git_checkout(tmp_path / "loose.py") is None


def test_resolve_git_provenance_reports_commit_branch_dirty(tmp_path: Path) -> None:
    repo, hashes = _make_repo(tmp_path, 1)
    (repo / "f.txt").write_text("dirty\n")
    provenance = resolve_git_provenance(repo / "f.txt")
    assert provenance is not None
    assert provenance["commit"] == hashes[0]
    assert provenance["branch"] == "main"
    assert provenance["dirty"] is True


def test_resolve_git_provenance_none_without_a_checkout(tmp_path: Path) -> None:
    assert resolve_git_provenance(tmp_path / "loose.py") is None
