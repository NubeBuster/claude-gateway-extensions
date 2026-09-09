"""The live run itself: drive a real subject, then assert on the numbers.

Every assertion here exists because regressing it costs a user money, and
none of them can be made against the fake upstream in `tests/`: they are
statements about what the real API billed for a real conversation.

The assertion list and its rationale are recorded in `docs/DESIGN.md`,
"Live regression harness".
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from claude_gateway_extensions.cli.client import ControlClient

from .budget import Budget
from .daemon import Daemon
from .subject import TmuxSubject
from .transcript import Transcript, existing_transcripts, project_dir

#: What a fork is expected to do, per subject model family. A fork can only
#: read the parent's cached prefix on a model that preserves signed thinking
#: blocks; on every Haiku the daemon must refuse the fork instead of paying
#: to rewrite the whole conversation. This is the harness's own expectation,
#: deliberately not read from the daemon's table -- an assertion that asks
#: the code under test what to expect cannot catch that table changing.
FORK_EXPECTATION = {
    "haiku": "refusal",
    "sonnet": "cache_read",
    "opus": "cache_read",
}

#: The turns the subject is asked to take before any bump or fork. Three
#: short ones are enough to give the daemon a snapshot worth replaying,
#: and each answer is trivially checkable.
WARMUP_PROMPTS = (
    "What is 2+2? Answer with just the number.",
    "Name the capital of Portugal. One word only.",
    "Say the word banana and nothing else.",
)

#: The turn taken after the forks, to prove the twin never touched the parent.
FINAL_PROMPT = "Say the word banana again, and nothing else."
FINAL_EXPECTED = "banana"

#: The review question a fork asks. Answered from the conversation alone,
#: so a correct verdict is also evidence the fork saw the parent's context.
FORK_PROMPT = (
    "You are a silent reviewer of the conversation above. Answer using only "
    "the conversation's own context. Never call a tool. Reply with strict "
    "JSON and nothing else, in exactly this shape:\n"
    '{"verdict": "ok", "reason": "<one short sentence>", "mutation": null}\n\n'
    "Question: list, in one short sentence, the topics the user asked about "
    "in this conversation."
)

#: Fail-open reason codes the daemon returns instead of a real verdict.
DEGRADED_REASONS = frozenset(
    {
        "no_snapshot",
        "rate_limited",
        "unparseable_verdict",
        "fork_failed",
        "tool_use_reply",
        "thinking_unsafe_model",
        "thinking_model_unknown",
        "snapshot_stale",
    }
)


class LiveRunError(RuntimeError):
    """The run could not reach the point where its assertions are meaningful."""


@dataclass(frozen=True)
class Check:
    """One assertion's outcome."""

    name: str
    passed: bool
    detail: str

    def line(self) -> str:
        """Render the check as one table row."""
        return f"  {'PASS' if self.passed else 'FAIL'}  {self.name:<28} {self.detail}"


@dataclass
class Report:
    """Everything a run has to say: its checks, its cost and its diagnostics."""

    checks: list[Check] = field(default_factory=list)
    cost_usd: float = 0.0
    cap_usd: float = 0.0
    session_id: str | None = None
    model: str = ""
    error: str | None = None
    diagnostics: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when every check passed and nothing aborted the run."""
        return self.error is None and bool(self.checks) and all(c.passed for c in self.checks)

    def add(self, name: str, passed: bool, detail: str) -> None:
        """Record one check."""
        self.checks.append(Check(name, passed, detail))

    def render(self) -> str:
        """Render the whole report: table, cost, then diagnostics if it failed."""
        lines = [
            f"live run: model={self.model} session={self.session_id or '(none)'}",
            *[check.line() for check in self.checks],
            f"  estimated spend: ${self.cost_usd:.4f} of ${self.cap_usd:.2f} cap "
            f"(list prices; a subscription is not billed per token)",
            f"  result: {'PASS' if self.ok else 'FAIL'}",
        ]
        if self.error:
            lines.append(f"  aborted: {self.error}")
        if not self.ok:
            for name, text in self.diagnostics.items():
                lines.extend([f"--- {name} ---", text])
        return "\n".join(lines)


@dataclass(frozen=True)
class RunConfig:
    """Everything one live run needs to know."""

    claude_bin: Path
    cgx_bin: str
    scratch_dir: Path
    home: Path
    model: str = "haiku"
    tag: str = "A"
    budget_usd: float = 1.0
    ready_timeout_s: float = 90.0
    turn_timeout_s: float = 240.0
    fork_expectation: str | None = None

    @property
    def subject_cwd(self) -> Path:
        """The empty scratch directory the subject is launched from."""
        return self.scratch_dir / "subject"

    @property
    def session_name(self) -> str:
        """The `T-`-prefixed name every test subject in this project carries."""
        return f"T-cgx-live-{self.model}-{self.tag}"

    def expectation(self) -> str:
        """Return 'refusal' or 'cache_read' for this run's subject model."""
        if self.fork_expectation:
            return self.fork_expectation
        for family, expected in FORK_EXPECTATION.items():
            if family in self.model.lower():
                return expected
        raise LiveRunError(
            f"no fork expectation known for model {self.model!r}; "
            f"pass --expect-fork read|refusal to say what it should do"
        )


class LiveRun:
    """Owns one run's daemon, subject and assertions, and tears them all down."""

    def __init__(
        self,
        config: RunConfig,
        parent_env: dict[str, str],
        daemon: Daemon,
        subject: TmuxSubject,
        client_factory: Callable[[str], ControlClient] = ControlClient,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        """Wire a run from its already-constructed daemon and subject."""
        self.config = config
        self.parent_env = parent_env
        self.daemon = daemon
        self.subject = subject
        self.client_factory = client_factory
        self.sleep = sleep
        self.budget = Budget(cap_usd=config.budget_usd)
        self.report = Report(cap_usd=config.budget_usd, model=config.model)
        self.transcript = Transcript(directory=project_dir(config.home, config.subject_cwd))

    def execute(self) -> Report:
        """Run everything, then tear down whatever was started, always."""
        try:
            self._run_steps()
        except Exception as exc:  # the report is this run's interface, not a traceback
            self.report.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._teardown()
        self.report.cost_usd = self.budget.total_usd
        return self.report

    # -- steps -------------------------------------------------------------

    def _run_steps(self) -> None:
        self.config.subject_cwd.mkdir(parents=True, exist_ok=True)
        self.daemon.start(self.parent_env)
        client = self.client_factory(self.daemon.base_url)
        before = self._session_ids(client)

        # Pinned before launch: the scratch cwd is stable, so its transcript
        # directory still holds every past run's file, and a past run's
        # completed turns would be counted as this one's progress.
        self.transcript.ignore = existing_transcripts(self.transcript.directory)

        self.subject.start(self.parent_env)
        self.subject.wait_ready(self.config.ready_timeout_s)
        self._take_turns(WARMUP_PROMPTS)

        session_id = self._discover_session_id(client, before)
        self.report.session_id = session_id

        self._check_snapshot(client, session_id)
        self._check_bump(client, session_id)
        self._check_forks(client, session_id)
        self._check_subject_survives()

    def _take_turns(self, prompts: Sequence[str]) -> None:
        """Send each prompt and wait for the subject's own record of the turn."""
        for prompt in prompts:
            self.budget.check("another subject turn")
            already = len(self.transcript.completed_turns())
            self.subject.send(prompt)
            turns = self.transcript.wait_for_turns(already + 1, self.config.turn_timeout_s)
            latest = turns[-1]
            self.budget.add(
                "subject turn", self.transcript.model(latest), self.transcript.usage(latest)
            )

    def _session_ids(self, client: ControlClient) -> set[str]:
        return {
            str(entry.get("session_id"))
            for entry in client.list_sessions()
            if entry.get("session_id")
        }

    def _discover_session_id(self, client: ControlClient, before: set[str]) -> str:
        """Return the session id that appeared while the subject took its turns.

        An interactive session emits no machine-readable init event, and a
        session id read out of a file name goes stale the moment the session
        is resumed. The daemon saw the real
        ``x-claude-code-session-id`` header, so it is asked instead.
        """
        appeared = sorted(self._session_ids(client) - before)
        if len(appeared) == 1:
            return appeared[0]
        raise LiveRunError(
            f"expected exactly one new session at the daemon, saw {appeared or 'none'}. "
            f"Another Claude Code session may be pointed at this run's port."
        )

    def _check_snapshot(self, client: ControlClient, session_id: str) -> None:
        status = client.get_session(session_id)
        streams = status.get("streams")
        count = streams if isinstance(streams, int) else 0
        self.report.add(
            "snapshot captured",
            count >= 1,
            f"streams={count} (a session with no snapshot can never be bumped)",
        )

    def _check_bump(self, client: ControlClient, session_id: str) -> None:
        self.budget.check("a bump")
        result = client.bump(session_id)
        usage = result.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        model = result.get("model")
        self.budget.add("bump", model if isinstance(model, str) else None, usage)
        read = usage.get("cache_read_input_tokens")
        created = usage.get("cache_creation_input_tokens")
        self.report.add(
            "bump is a cache read",
            result.get("outcome") == "ok" and _positive(read) and created == 0,
            f"outcome={result.get('outcome')} read={read} creation={created}",
        )

    def _check_forks(self, client: ControlClient, session_id: str) -> None:
        expectation = self.config.expectation()
        first = self._one_fork(client, session_id, 1)
        if expectation == "refusal":
            self._check_fork_refused(first)
            return
        self._check_fork_read(first, "fork is a cache read")
        second = self._one_fork(client, session_id, 2)
        self._check_fork_read(second, "second fork is a cache read")

    def _one_fork(self, client: ControlClient, session_id: str, index: int) -> Mapping[str, object]:
        self.budget.check(f"fork #{index}")
        result = client.fork(
            session_id,
            prompt=FORK_PROMPT,
            hook_event="cgx-live-harness",
            payload={"run": self.config.tag, "fork": index},
            max_tokens=400,
        )
        usage = result.get("usage")
        model = result.get("model")
        self.budget.add(
            f"fork #{index}",
            model if isinstance(model, str) else None,
            usage if isinstance(usage, dict) else None,
        )
        return result

    def _check_fork_read(self, result: Mapping[str, object], name: str) -> None:
        usage = result.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        read = usage.get("cache_read_input_tokens")
        created = usage.get("cache_creation_input_tokens")
        reason = result.get("reason")
        parsed = isinstance(reason, str) and reason not in DEGRADED_REASONS and bool(reason.strip())
        self.report.add(
            name,
            parsed and _positive(read) and created == 0 and result.get("paid_write") is False,
            f"read={read} creation={created} paid_write={result.get('paid_write')} "
            f"verdict={result.get('verdict')!r} parsed={parsed}",
        )

    def _check_fork_refused(self, result: Mapping[str, object]) -> None:
        self.report.add(
            "haiku fork refused unsent",
            result.get("reason") == "thinking_unsafe_model"
            and result.get("paid_write") is False
            and result.get("usage") is None,
            f"reason={result.get('reason')!r} paid_write={result.get('paid_write')} "
            f"usage={result.get('usage')!r}",
        )

    def _check_subject_survives(self) -> None:
        self._take_turns([FINAL_PROMPT])
        latest = self.transcript.completed_turns()[-1]
        reply = self.transcript.reply_text(latest).lower()
        self.report.add(
            "subject turn after forks",
            FINAL_EXPECTED in reply,
            f"reply={reply.strip()[:60]!r} (a fork must leave the parent untouched)",
        )

    # -- teardown ----------------------------------------------------------

    def _teardown(self) -> None:
        """Release everything this run started, whatever went wrong.

        Diagnostics are collected first, because stopping the subject
        destroys the pane that explains a launch failure.
        """
        try:
            if self.subject.started:
                self.report.diagnostics["subject pane"] = self.subject.capture()
        except Exception:  # a diagnostic must never mask the real error
            pass
        self.subject.stop()
        exit_code = self.daemon.stop()
        self.report.diagnostics["daemon log tail"] = self.daemon.log_tail()
        if exit_code is not None:
            self.report.add(
                "daemon exits cleanly",
                exit_code == 0,
                f"exit code {exit_code} after SIGTERM (the documented drain path)",
            )


def _positive(value: object) -> bool:
    return isinstance(value, int) and value > 0


__all__ = [
    "DEGRADED_REASONS",
    "FORK_EXPECTATION",
    "Check",
    "LiveRun",
    "LiveRunError",
    "Report",
    "RunConfig",
]
