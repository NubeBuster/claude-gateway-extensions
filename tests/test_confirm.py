"""Tests for claude_gateway_extensions.cli.confirm's diff rendering and apply flow."""

from __future__ import annotations

import io
from pathlib import Path

from claude_gateway_extensions.cli.confirm import PlannedChange, apply_changes, render_change


class _TtyStdin(io.StringIO):
    """A stdin fake that reports itself as a TTY, unlike a plain ``io.StringIO``."""

    def isatty(self) -> bool:
        return True


class _TtyStdout(io.StringIO):
    """A stdout fake that reports itself as a TTY, for color-enabled rendering."""

    def isatty(self) -> bool:
        return True


def _noop() -> None:
    pass


def test_render_change_modify_has_no_color_by_default() -> None:
    change = PlannedChange(Path("/x/settings.json"), "a\n", "b\n", _noop)
    text = render_change(change, color=False)
    assert "\033[" not in text
    assert "-a" in text
    assert "+b" in text


def test_render_change_colorizes_additions_removals_and_hunks() -> None:
    change = PlannedChange(Path("/x/settings.json"), "a\n", "b\n", _noop)
    text = render_change(change, color=True)
    assert "\033[31m-a" in text
    assert "\033[32m+b" in text
    assert "\033[36m@@" in text


def test_render_change_create_has_will_create_header() -> None:
    change = PlannedChange(Path("/x/new.json"), None, "content\n", _noop)
    text = render_change(change, color=False)
    assert text.startswith("will create /x/new.json\n")
    assert "+content" in text


def test_render_change_remove_has_will_remove_header() -> None:
    change = PlannedChange(Path("/x/SKILL.md"), "content\n", None, _noop)
    text = render_change(change, color=False)
    assert text.startswith("will remove /x/SKILL.md\n")
    assert "-content" in text


def test_apply_changes_no_pending_prints_no_changes_and_prompts_nothing() -> None:
    change = PlannedChange(Path("/x/settings.json"), "same\n", "same\n", _noop)
    stdin, stdout, stderr = io.StringIO(), io.StringIO(), io.StringIO()
    code = apply_changes(
        [change], yes=False, dry_run=False, stdin=stdin, stdout=stdout, stderr=stderr, environ={}
    )
    assert code == 0
    assert stdout.getvalue() == "no changes\n"


def test_apply_changes_dry_run_prints_diff_and_writes_nothing() -> None:
    applied = []
    change = PlannedChange(Path("/x/settings.json"), "a\n", "b\n", lambda: applied.append(1))
    stdout = io.StringIO()
    code = apply_changes(
        [change],
        yes=False,
        dry_run=True,
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=io.StringIO(),
        environ={},
    )
    assert code == 0
    assert "+b" in stdout.getvalue()
    assert applied == []


def test_apply_changes_non_tty_stdin_without_yes_aborts(tmp_path: Path) -> None:
    applied = []
    change = PlannedChange(tmp_path / "settings.json", "a\n", "b\n", lambda: applied.append(1))
    stdout, stderr = io.StringIO(), io.StringIO()
    code = apply_changes(
        [change],
        yes=False,
        dry_run=False,
        stdin=io.StringIO(),  # not a TTY
        stdout=stdout,
        stderr=stderr,
        environ={},
    )
    assert code == 2
    assert "+b" in stdout.getvalue()
    assert "--yes" in stderr.getvalue()
    assert applied == []


def test_apply_changes_yes_applies_without_prompting() -> None:
    applied = []
    change = PlannedChange(Path("/x/settings.json"), "a\n", "b\n", lambda: applied.append(1))
    stdout = io.StringIO()
    code = apply_changes(
        [change],
        yes=True,
        dry_run=False,
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=io.StringIO(),
        environ={},
    )
    assert code == 0
    assert applied == [1]
    assert "applied:" in stdout.getvalue()


def test_apply_changes_interactive_y_applies() -> None:
    applied = []
    change = PlannedChange(Path("/x/settings.json"), "a\n", "b\n", lambda: applied.append(1))
    stdout = io.StringIO()
    code = apply_changes(
        [change],
        yes=False,
        dry_run=False,
        stdin=_TtyStdin("y\n"),
        stdout=stdout,
        stderr=io.StringIO(),
        environ={},
    )
    assert code == 0
    assert applied == [1]
    assert "Apply this change to" in stdout.getvalue()


def test_apply_changes_interactive_n_skips() -> None:
    applied = []
    change = PlannedChange(Path("/x/settings.json"), "a\n", "b\n", lambda: applied.append(1))
    stdout = io.StringIO()
    code = apply_changes(
        [change],
        yes=False,
        dry_run=False,
        stdin=_TtyStdin("n\n"),
        stdout=stdout,
        stderr=io.StringIO(),
        environ={},
    )
    assert code == 1
    assert applied == []
    assert "skipped:" in stdout.getvalue()


def test_apply_changes_partial_acceptance_summary_and_exit_code() -> None:
    applied = []
    accepted = PlannedChange(Path("/x/a.json"), "a\n", "b\n", lambda: applied.append("a"))
    rejected = PlannedChange(Path("/x/b.json"), "c\n", "d\n", lambda: applied.append("b"))
    stdout = io.StringIO()
    code = apply_changes(
        [accepted, rejected],
        yes=False,
        dry_run=False,
        stdin=_TtyStdin("y\nn\n"),
        stdout=stdout,
        stderr=io.StringIO(),
        environ={},
    )
    assert code == 0
    assert applied == ["a"]
    out = stdout.getvalue()
    assert "applied:" in out
    assert "/x/a.json" in out
    assert "skipped:" in out
    assert "/x/b.json" in out


def test_color_disabled_when_stdout_is_not_a_tty() -> None:
    change = PlannedChange(Path("/x/settings.json"), "a\n", "b\n", _noop)
    stdout = io.StringIO()  # not a TTY
    code = apply_changes(
        [change],
        yes=True,
        dry_run=False,
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=io.StringIO(),
        environ={},
    )
    assert code == 0
    assert "\033[" not in stdout.getvalue()


def test_color_disabled_when_no_color_env_set_even_on_a_tty() -> None:
    change = PlannedChange(Path("/x/settings.json"), "a\n", "b\n", _noop)
    stdout = _TtyStdout()
    code = apply_changes(
        [change],
        yes=True,
        dry_run=False,
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=io.StringIO(),
        environ={"NO_COLOR": "1"},
    )
    assert code == 0
    assert "\033[" not in stdout.getvalue()


def test_color_enabled_on_a_tty_without_no_color() -> None:
    change = PlannedChange(Path("/x/settings.json"), "a\n", "b\n", _noop)
    stdout = _TtyStdout()
    code = apply_changes(
        [change],
        yes=True,
        dry_run=False,
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=io.StringIO(),
        environ={},
    )
    assert code == 0
    assert "\033[" in stdout.getvalue()
