"""Colored-diff-and-confirm flow for commands that write files a user has.

Generic on purpose: this module knows only about ``(path, old_content,
new_content, apply)`` tuples (:class:`PlannedChange`), never about
``settings.json``, the skill directory, or systemd units specifically. See
``cli/install.py``'s ``plan_install``/``plan_uninstall`` for the concrete
plans that ``ccbg configure``/``ccbg unconfigure`` build and hand to
:func:`apply_changes`.

Exit codes returned by :func:`apply_changes` (chosen to match
``cli/__init__.py``'s ``EXIT_OK``/``EXIT_FAILED``/``EXIT_USAGE`` exactly, so
callers can return the value directly): ``0`` nothing pending or something
was applied, ``1`` every pending change was rejected, ``2`` stdin is not a
TTY and ``yes`` is ``False`` (nothing written).
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

_GREEN = "\033[32m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_RESET = "\033[0m"


@dataclass
class PlannedChange:
    """One file a command would write, computed before anything is touched.

    ``old_content``/``new_content`` of ``None`` mean "does not exist" /
    "will not exist after" (a create or a removal). ``apply`` performs the
    real filesystem operation and is never derived from the text itself, so
    it stays free to do things a text diff can't represent exactly (preserve
    JSON key order via a dict, ``shutil.rmtree`` a directory, ...).
    """

    path: Path
    old_content: str | None
    new_content: str | None
    apply: Callable[[], None]


def _color_enabled(stdout: TextIO, environ: Mapping[str, str]) -> bool:
    """Return whether to colorize: a TTY stdout, unless ``NO_COLOR`` is set."""
    if environ.get("NO_COLOR"):
        return False
    return bool(getattr(stdout, "isatty", lambda: False)())


def _colorize(line: str) -> str:
    """Color one unified-diff line: red removals, green additions, cyan hunks."""
    if line.startswith("+++") or line.startswith("---"):
        return line
    if line.startswith("+"):
        return f"{_GREEN}{line}{_RESET}"
    if line.startswith("-"):
        return f"{_RED}{line}{_RESET}"
    if line.startswith("@@"):
        return f"{_CYAN}{line}{_RESET}"
    return line


def render_change(change: PlannedChange, *, color: bool) -> str:
    """Render ``change`` as a create/remove header (if any) plus a unified diff."""
    header = ""
    if change.old_content is None:
        header = f"will create {change.path}\n"
    elif change.new_content is None:
        header = f"will remove {change.path}\n"
    old_lines = (change.old_content or "").splitlines(keepends=True)
    new_lines = (change.new_content or "").splitlines(keepends=True)
    diff_lines = difflib.unified_diff(
        old_lines, new_lines, fromfile=str(change.path), tofile=str(change.path)
    )
    body = []
    for line in diff_lines:
        if not line.endswith("\n"):
            line += "\n"
        body.append(_colorize(line) if color else line)
    return header + "".join(body)


def apply_changes(
    changes: Sequence[PlannedChange],
    *,
    yes: bool,
    dry_run: bool,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    environ: Mapping[str, str],
) -> int:
    """Show a diff per pending change, confirm it, apply it, and report an exit code.

    A change whose ``old_content`` already equals ``new_content`` is not
    pending: it is never shown and never prompted for (idempotency). If
    nothing is pending, prints "no changes" and returns 0.

    ``dry_run`` prints every pending diff and returns 0 without writing
    anything. Otherwise, if ``yes`` is False and ``stdin`` is not a TTY,
    every pending diff is printed to ``stdout`` and the whole command aborts
    (returns 2) without writing anything, since there is no one to answer a
    prompt — the reason goes to ``stderr``, matching every other usage error
    in this CLI. Otherwise each pending change is shown and applied on
    ``yes`` or on an interactive ``y`` answer, skipped otherwise; the final
    summary lists applied and skipped paths and the return value is 0 if
    anything was applied, 1 if everything was skipped.
    """
    pending = [c for c in changes if c.old_content != c.new_content]
    if not pending:
        stdout.write("no changes\n")
        return 0

    color = _color_enabled(stdout, environ)

    if dry_run:
        for change in pending:
            stdout.write(render_change(change, color=color))
        return 0

    stdin_is_tty = bool(getattr(stdin, "isatty", lambda: False)())
    if not yes and not stdin_is_tty:
        for change in pending:
            stdout.write(render_change(change, color=color))
        stderr.write("stdin is not a TTY; re-run with --yes to apply without prompting\n")
        return 2

    applied: list[Path] = []
    skipped: list[Path] = []
    for change in pending:
        stdout.write(render_change(change, color=color))
        if yes:
            do_apply = True
        else:
            stdout.write(f"Apply this change to {change.path}? [y/N] ")
            answer = stdin.readline()
            do_apply = answer.strip().lower() == "y"
        if do_apply:
            change.apply()
            applied.append(change.path)
        else:
            skipped.append(change.path)

    if applied:
        stdout.write("applied:\n")
        for path in applied:
            stdout.write(f"  {path}\n")
    if skipped:
        stdout.write("skipped:\n")
        for path in skipped:
            stdout.write(f"  {path}\n")
    return 0 if applied else 1


__all__ = ["PlannedChange", "apply_changes", "render_change"]
