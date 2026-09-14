"""Offline tests for the live harness's own logic (`tests/live/`).

The live run itself needs the real API and is therefore not collected here.
Everything about it that can be wrong without spending money is: the
subject's environment allowlist and argv, readiness detection, transcript
parsing, session-id discovery, the price table and budget stop, the
assertion verdicts, and teardown ordering.

The doubles are a fake tmux (records argv, replays scripted pane text) and
a fake control client (returns scripted daemon responses). No process is
started and no socket is opened.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from live.budget import Budget, BudgetExceeded, price_for, usage_cost_usd
from live.harness import LiveRun, LiveRunError, Report, RunConfig
from live.subject import (
    CREDENTIALS_FILENAME,
    ENV_ALLOWLIST,
    FakeHomeError,
    SubjectError,
    TmuxSubject,
    pane_is_ready,
    prepare_fake_home,
)
from live.transcript import (
    Transcript,
    TranscriptError,
    existing_transcripts,
    project_dir,
    project_slug,
)


class FakeTmux:
    """Records every tmux argv and answers `capture-pane` from a script."""

    def __init__(self, panes: list[str] | None = None, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.panes = panes if panes is not None else [""]
        self.fail_on = fail_on

    def __call__(self, argv) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        if self.fail_on is not None and self.fail_on in argv:
            return subprocess.CompletedProcess(argv, 1, "", "tmux: boom")
        stdout = ""
        if "capture-pane" in argv:
            stdout = self.panes[0] if len(self.panes) == 1 else self.panes.pop(0)
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def commands(self) -> list[str]:
        """The tmux subcommand of each recorded call."""
        return [argv[1] for argv in self.calls]


def make_subject(runner: FakeTmux, **kwargs) -> TmuxSubject:
    """Build a subject wired to ``runner`` with time stubbed out."""
    ticks = iter(range(0, 10_000))
    return TmuxSubject(
        session_name="T-cgx-live-haiku-A",
        cwd=Path("/tmp/scratch/subject"),
        claude_bin=Path("/opt/claude"),
        model="haiku",
        base_url="http://127.0.0.1:9999",
        fake_home=Path("/tmp/scratch/home"),
        fake_config_dir=Path("/tmp/scratch/home/.claude"),
        runner=runner,
        sleep=lambda _s: None,
        monotonic=lambda: float(next(ticks)),
        **kwargs,
    )


# -- subject: isolation ----------------------------------------------------


def test_subject_env_is_an_allowlist_not_an_inheritance():
    subject = make_subject(FakeTmux())
    env = subject.subject_env(
        {
            "HOME": "/home/u",
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "sk-should-not-survive",
            "ANTHROPIC_AUTH_TOKEN": "tok",
            "CLAUDE_CODE_ENTRYPOINT": "some-agent-session",
        }
    )
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9999"


def test_subject_env_always_uses_the_fake_home_never_the_real_one():
    subject = make_subject(FakeTmux())
    env = subject.subject_env({"HOME": "/home/real-user", "PATH": "/usr/bin"})
    assert env["HOME"] == "/tmp/scratch/home"
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/scratch/home/.claude"


def test_subject_env_sets_the_fake_home_even_when_parent_has_none():
    subject = make_subject(FakeTmux())
    env = subject.subject_env({"PATH": "/usr/bin"})
    assert env["HOME"] == "/tmp/scratch/home"
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/scratch/home/.claude"


def test_env_allowlist_carries_no_anthropic_or_claude_names():
    assert not [name for name in ENV_ALLOWLIST if name.startswith(("ANTHROPIC_", "CLAUDE_"))]


def test_env_allowlist_never_inherits_home():
    assert "HOME" not in ENV_ALLOWLIST


# -- subject: fake home -----------------------------------------------------


def test_prepare_fake_home_copies_only_the_credentials_file(tmp_path):
    real_home = tmp_path / "real"
    (real_home / ".claude").mkdir(parents=True)
    (real_home / ".claude" / CREDENTIALS_FILENAME).write_text('{"token": "secret"}')
    (real_home / ".claude" / "settings.json").write_text("{}")
    (real_home / ".claude" / "skills").mkdir()
    (real_home / ".claude.json").write_text("{}")

    scratch = tmp_path / "scratch"
    fake = prepare_fake_home(scratch, real_home)

    assert fake.home == scratch / "home"
    assert fake.config_dir == scratch / "home" / ".claude"
    copied = list(fake.config_dir.iterdir())
    assert [p.name for p in copied] == [CREDENTIALS_FILENAME]
    assert (fake.config_dir / CREDENTIALS_FILENAME).read_text() == '{"token": "secret"}'
    assert not (fake.home / ".claude.json").exists()


def test_prepare_fake_home_credentials_are_private(tmp_path):
    real_home = tmp_path / "real"
    (real_home / ".claude").mkdir(parents=True)
    (real_home / ".claude" / CREDENTIALS_FILENAME).write_text("{}")

    fake = prepare_fake_home(tmp_path / "scratch", real_home)
    mode = (fake.config_dir / CREDENTIALS_FILENAME).stat().st_mode & 0o777
    assert mode == 0o600


def test_prepare_fake_home_fails_early_when_credentials_are_absent(tmp_path):
    real_home = tmp_path / "real"
    real_home.mkdir()
    with pytest.raises(FakeHomeError, match="credentials"):
        prepare_fake_home(tmp_path / "scratch", real_home)


def test_launch_argv_names_and_isolates_the_subject():
    argv = make_subject(FakeTmux()).launch_argv()
    assert argv[0] == "/opt/claude"
    assert "--safe-mode" in argv
    assert argv[argv.index("--name") + 1].startswith("T-")
    assert "--print" not in argv


def test_start_launches_a_detached_env_scrubbed_session():
    runner = FakeTmux()
    make_subject(runner).start({"HOME": "/home/u", "PATH": "/usr/bin"})
    argv = runner.calls[0]
    assert argv[:4] == ["tmux", "new-session", "-d", "-s"]
    assert "-c" in argv and "/tmp/scratch/subject" in argv
    command = argv[-1]
    assert command.startswith("env -i ")
    assert "ANTHROPIC_BASE_URL=http://127.0.0.1:9999" in command


def test_start_reports_a_tmux_failure_instead_of_continuing():
    subject = make_subject(FakeTmux(fail_on="new-session"))
    with pytest.raises(SubjectError, match="new-session"):
        subject.start({})
    assert not subject.started


# -- subject: readiness and input -----------------------------------------


@pytest.mark.parametrize(
    ("pane", "ready"),
    [
        ("banner\n\n❯ \n", True),  # noqa: RUF001 - the TUI's own prompt ornament
        ("  > ", True),
        ("Do you trust the files in this folder? 1. Yes 2. No", False),
        ("", False),
        ("a > b in prose", False),
    ],
)
def test_pane_is_ready_only_on_a_prompt_at_line_start(pane, ready):
    assert pane_is_ready(pane) is ready


def test_wait_ready_fails_with_the_pane_when_something_else_is_on_screen():
    dialog = "Do you trust the files in this folder?"
    subject = make_subject(FakeTmux(panes=[dialog]))
    with pytest.raises(SubjectError) as excinfo:
        subject.wait_ready(timeout_s=3.0)
    assert dialog in str(excinfo.value)
    assert "never to answer it" in str(excinfo.value)


def test_send_types_the_text_then_enter_separately():
    runner = FakeTmux()
    make_subject(runner).send("hello")
    assert runner.commands() == ["send-keys", "send-keys"]
    assert runner.calls[0][-2:] == ["-l", "hello"]
    assert runner.calls[1][-1] == "Enter"


def test_stop_is_idempotent_and_only_kills_a_started_session():
    runner = FakeTmux()
    subject = make_subject(runner)
    subject.stop()
    assert runner.calls == []
    subject.started = True
    subject.stop()
    subject.stop()
    assert runner.commands() == ["kill-session"]


# -- transcript ------------------------------------------------------------


def test_project_slug_matches_claude_codes_directory_naming():
    assert project_slug(Path("/tmp/scripts/cgx-live/subject")) == "-tmp-scripts-cgx-live-subject"


def test_project_dir_is_rooted_at_the_given_claude_config_dir_not_home():
    config_dir = Path("/tmp/scratch/home/.claude")
    cwd = Path("/tmp/scripts/cgx-live/subject")
    assert project_dir(config_dir, cwd) == config_dir / "projects" / project_slug(cwd)


def write_transcript(directory: Path, lines: list[str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "11111111-2222-3333-4444-555555555555.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def assistant_line(text: str, stop: str = "end_turn", read: int = 0, created: int = 0) -> str:
    return (
        '{"type": "assistant", "sessionId": "sid-1", "message": {"model": "claude-haiku-4-5", '
        f'"stop_reason": "{stop}", "content": [{{"type": "text", "text": "{text}"}}], '
        f'"usage": {{"input_tokens": 5, "output_tokens": 7, '
        f'"cache_read_input_tokens": {read}, "cache_creation_input_tokens": {created}}}}}}}'
    )


def test_completed_turns_ignores_tool_use_stops_and_torn_lines(tmp_path):
    write_transcript(
        tmp_path,
        [
            '{"type": "user", "message": {"content": "hi"}}',
            assistant_line("thinking", stop="tool_use"),
            assistant_line("banana"),
            '{"type": "assistant", "message": {"stop_reason": "end_t',
        ],
    )
    transcript = Transcript(directory=tmp_path)
    turns = transcript.completed_turns()
    assert len(turns) == 1
    assert transcript.reply_text(turns[0]) == "banana"
    assert transcript.model(turns[0]) == "claude-haiku-4-5"
    assert transcript.session_id() == "sid-1"


def test_a_past_runs_transcript_is_never_read_as_this_runs_progress(tmp_path):
    stale = tmp_path / "old-session.jsonl"
    tmp_path.mkdir(parents=True, exist_ok=True)
    stale.write_text(assistant_line("stale") + "\n", encoding="utf-8")
    before = existing_transcripts(tmp_path)

    transcript = Transcript(directory=tmp_path, ignore=before)
    assert transcript.path() is None
    assert transcript.completed_turns() == []

    write_transcript(tmp_path, [assistant_line("fresh")])
    assert transcript.reply_text(transcript.completed_turns()[0]) == "fresh"


def test_entries_are_empty_before_the_subject_writes_anything(tmp_path):
    transcript = Transcript(directory=tmp_path / "not-yet")
    assert transcript.path() is None
    assert transcript.entries() == []


def test_wait_for_turns_times_out_naming_the_transcript(tmp_path):
    write_transcript(tmp_path, [assistant_line("one")])
    transcript = Transcript(directory=tmp_path)
    ticks = iter([0.0, 1.0, 2.0, 99.0])
    with pytest.raises(TranscriptError, match="only 1 of 2 turns"):
        transcript.wait_for_turns(
            2, timeout_s=5.0, sleep=lambda _s: None, monotonic=lambda: next(ticks)
        )


# -- budget ----------------------------------------------------------------


def test_price_lookup_is_by_family_and_defaults_to_the_dearest():
    assert price_for("claude-haiku-4-5-20251001").input_usd == 1.0
    assert price_for("claude-sonnet-5").input_usd == 3.0
    assert price_for("some-unreleased-model").input_usd == price_for("claude-opus-4-5").input_usd
    assert price_for(None).input_usd == 5.0


def test_a_cache_read_is_priced_far_below_a_write():
    usage_read = {"cache_read_input_tokens": 16_046}
    usage_write = {"cache_creation_input_tokens": 16_046}
    assert usage_cost_usd("claude-sonnet-5", usage_read) < usage_cost_usd(
        "claude-sonnet-5", usage_write
    )


def test_usage_cost_reads_missing_and_odd_counters_as_zero():
    assert usage_cost_usd("claude-haiku-4-5", {}) == 0.0
    assert usage_cost_usd("claude-haiku-4-5", {"input_tokens": None}) == 0.0


def test_budget_stops_the_next_step_once_the_cap_is_passed():
    budget = Budget(cap_usd=0.01)
    budget.add("bump", "claude-opus-4-5", {"output_tokens": 1_000_000})
    assert budget.exceeded
    with pytest.raises(BudgetExceeded, match="refusing to run another turn"):
        budget.check("another turn")


def test_budget_ignores_a_call_that_reported_no_usage():
    budget = Budget(cap_usd=1.0)
    assert budget.add("refused fork", "claude-haiku-4-5", None) == 0.0
    assert budget.entries == []


# -- run configuration -----------------------------------------------------


def make_config(**kwargs) -> RunConfig:
    defaults = dict(
        claude_bin=Path("/opt/claude"),
        cgx_bin="cgx",
        scratch_dir=Path("/tmp/scratch"),
        subject_config_dir=Path("/tmp/scratch/home/.claude"),
        real_home=Path("/home/u"),
    )
    defaults.update(kwargs)
    return RunConfig(**defaults)  # pyright: ignore[reportArgumentType]


def test_fork_expectation_follows_the_subject_model():
    assert make_config(model="haiku").expectation() == "refusal"
    assert make_config(model="sonnet").expectation() == "cache_read"


def test_an_unknown_model_demands_an_explicit_expectation():
    with pytest.raises(LiveRunError, match="--expect-fork"):
        make_config(model="mystery").expectation()
    assert make_config(model="mystery", fork_expectation="cache_read").expectation() == "cache_read"


def test_the_subject_name_is_prefixed_and_carries_model_and_tag():
    assert make_config(model="sonnet", tag="B").session_name == "T-cgx-live-sonnet-B"


# -- home isolation ----------------------------------------------------------


def make_live_run(tmp_path: Path, **config_kwargs) -> LiveRun:
    """A `LiveRun` wired to a real, throwaway `RunConfig`, no daemon or subject."""
    config = make_config(
        scratch_dir=tmp_path / "scratch",
        subject_config_dir=tmp_path / "fake-home" / ".claude",
        real_home=tmp_path / "real-home",
        **config_kwargs,
    )
    return LiveRun(
        config=config,
        parent_env={},
        daemon=object(),  # pyright: ignore[reportArgumentType]
        subject=object(),  # pyright: ignore[reportArgumentType]
        client_factory=lambda _url: None,  # pyright: ignore[reportArgumentType]
    )


def test_home_redirect_check_passes_when_only_the_fake_dir_gets_a_transcript(tmp_path):
    run = make_live_run(tmp_path)
    write_transcript(run.transcript.directory, [assistant_line("banana")])
    run._assert_home_redirect_took_effect(real_before=frozenset())


def test_home_redirect_check_aborts_when_the_real_home_also_gains_a_file(tmp_path):
    run = make_live_run(tmp_path)
    write_transcript(run.transcript.directory, [assistant_line("banana")])
    write_transcript(run.real_project_dir, [assistant_line("leaked")])
    with pytest.raises(LiveRunError, match="real"):
        run._assert_home_redirect_took_effect(real_before=frozenset())


def test_home_redirect_check_ignores_a_real_file_that_predates_the_run(tmp_path):
    run = make_live_run(tmp_path)
    write_transcript(run.transcript.directory, [assistant_line("banana")])
    stale = write_transcript(run.real_project_dir, [assistant_line("old")])
    run._assert_home_redirect_took_effect(real_before=frozenset({stale}))


def test_home_redirect_check_aborts_when_the_fake_dir_never_got_a_transcript(tmp_path):
    run = make_live_run(tmp_path)
    with pytest.raises(LiveRunError, match="fake CLAUDE_CONFIG_DIR"):
        run._assert_home_redirect_took_effect(real_before=frozenset())


# -- assertions ------------------------------------------------------------


class FakeClient:
    """A control client returning scripted daemon responses."""

    def __init__(self, sessions=("session-1",), **responses) -> None:
        self._sessions = list(sessions)
        self._responses = responses

    def list_sessions(self):
        return [{"session_id": sid} for sid in self._sessions]

    def get_session(self, session_id):
        return self._responses.get("status", {"session_id": session_id, "streams": 1})

    def bump(self, session_id):
        return self._responses["bump"]

    def fork(self, session_id, **kwargs):
        forks = self._responses["forks"]
        return forks[0] if len(forks) == 1 else forks.pop(0)


def make_run(client: FakeClient, config: RunConfig | None = None) -> LiveRun:
    """A run wired to fakes, with no daemon and no subject started."""
    config = config or make_config()
    run = LiveRun.__new__(LiveRun)
    run.config = config
    run.budget = Budget(cap_usd=config.budget_usd)
    run.report = Report(cap_usd=config.budget_usd, model=config.model)
    run.client_factory = lambda _url: client  # pyright: ignore[reportAttributeAccessIssue]
    return run


CACHE_READ_FORK = {
    "verdict": "ok",
    "reason": "The user asked about arithmetic and Portugal.",
    "usage": {"cache_read_input_tokens": 16046, "cache_creation_input_tokens": 0},
    "paid_write": False,
    "model": "claude-sonnet-5",
}


def test_bump_check_fails_when_the_bump_paid_a_cache_write():
    client = FakeClient(
        bump={
            "outcome": "ok",
            "model": "claude-sonnet-5",
            "usage": {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 16046},
        }
    )
    run = make_run(client)
    run._check_bump(client, "session-1")  # pyright: ignore[reportArgumentType]
    check = run.report.checks[-1]
    assert not check.passed
    assert "creation=16046" in check.detail


def test_bump_check_passes_on_a_read_with_zero_creation():
    client = FakeClient(
        bump={
            "outcome": "ok",
            "model": "claude-sonnet-5",
            "usage": {"cache_read_input_tokens": 16046, "cache_creation_input_tokens": 0},
        }
    )
    run = make_run(client)
    run._check_bump(client, "session-1")  # pyright: ignore[reportArgumentType]
    assert run.report.checks[-1].passed


def test_a_degraded_reason_is_not_a_parsed_verdict():
    degraded = dict(CACHE_READ_FORK, reason="unparseable_verdict")
    run = make_run(FakeClient())
    run._check_fork_read(degraded, "fork is a cache read")
    assert not run.report.checks[-1].passed


def test_two_forks_are_asserted_on_a_thinking_safe_model():
    client = FakeClient(forks=[dict(CACHE_READ_FORK), dict(CACHE_READ_FORK)])
    run = make_run(client, make_config(model="sonnet"))
    run._check_forks(client, "session-1")  # pyright: ignore[reportArgumentType]
    assert [c.name for c in run.report.checks] == [
        "fork is a cache read",
        "second fork is a cache read",
    ]
    assert all(c.passed for c in run.report.checks)


def test_a_haiku_fork_must_be_refused_unsent():
    refusal = {
        "verdict": "ok",
        "reason": "thinking_unsafe_model",
        "usage": None,
        "paid_write": False,
        "model": "claude-haiku-4-5-20251001",
    }
    client = FakeClient(forks=[refusal])
    run = make_run(client, make_config(model="haiku"))
    run._check_forks(client, "session-1")  # pyright: ignore[reportArgumentType]
    assert run.report.checks[-1].name == "haiku fork refused unsent"
    assert run.report.checks[-1].passed


def test_a_haiku_fork_that_was_actually_sent_fails_the_check():
    sent = {"verdict": "ok", "reason": "looks fine", "usage": {}, "paid_write": True}
    client = FakeClient(forks=[sent])
    run = make_run(client, make_config(model="haiku"))
    run._check_forks(client, "session-1")  # pyright: ignore[reportArgumentType]
    assert not run.report.checks[-1].passed


def test_session_discovery_needs_exactly_one_new_session():
    run = make_run(FakeClient())
    client = FakeClient(sessions=["old", "new"])
    assert run._discover_session_id(client, {"old"}) == "new"  # pyright: ignore[reportArgumentType]

    ambiguous = FakeClient(sessions=["a", "b"])
    with pytest.raises(LiveRunError, match="exactly one new session"):
        run._discover_session_id(ambiguous, set())  # pyright: ignore[reportArgumentType]


# -- report ----------------------------------------------------------------


def test_a_report_with_no_checks_is_not_a_pass():
    assert not Report().ok


def test_a_failed_report_renders_its_diagnostics_and_the_cost():
    report = Report(cap_usd=1.0, cost_usd=0.02, model="haiku")
    report.add("bump is a cache read", False, "creation=16046")
    report.diagnostics["subject pane"] = "pane text"
    rendered = report.render()
    assert "FAIL  bump is a cache read" in rendered
    assert "$0.0200 of $1.00 cap" in rendered
    assert "pane text" in rendered


def test_a_passing_report_keeps_its_diagnostics_out_of_the_way():
    report = Report(cap_usd=1.0, model="haiku")
    report.add("bump is a cache read", True, "read=16046 creation=0")
    report.diagnostics["subject pane"] = "pane text"
    assert "pane text" not in report.render()
