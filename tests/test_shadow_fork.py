"""Tests for claude_gateway_extensions.cli.shadow_fork."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from claude_gateway_extensions.cli import definitions, shadow_fork
from claude_gateway_extensions.cli.client import ApiError, DaemonUnreachable


class _ScriptedClient:
    """Stand-in for ``ControlClient``: records every ``fork()`` call, scripted per test."""

    def __init__(
        self,
        base_url: str,
        *,
        result: dict[str, Any] | None,
        exc: Exception | None,
        calls: list[dict[str, Any]],
    ) -> None:
        self.base_url = base_url
        self._result = result
        self._exc = exc
        self._calls = calls

    def fork(
        self,
        session_id: str,
        prompt: str,
        hook_event: str | None,
        payload: object,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        self._calls.append(
            {
                "session_id": session_id,
                "prompt": prompt,
                "hook_event": hook_event,
                "payload": payload,
                "max_tokens": max_tokens,
                "model": model,
            }
        )
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: dict[str, Any] | None = None,
    exc: Exception | None = None,
) -> list[dict[str, Any]]:
    """Patch ``shadow_fork.ControlClient`` and return the list its ``fork()`` calls land in."""
    calls: list[dict[str, Any]] = []

    def factory(base_url: str) -> _ScriptedClient:
        return _ScriptedClient(base_url, result=result, exc=exc, calls=calls)

    monkeypatch.setattr(shadow_fork, "ControlClient", factory)
    return calls


def _run(
    payload: Mapping[str, object],
    environ: Mapping[str, str],
    template: str,
    prefix: str | None = None,
    model: str | None = None,
    apply: str | None = None,
) -> tuple[int, str]:
    stdin = io.StringIO(json.dumps(payload))
    stdout = io.StringIO()
    code = shadow_fork.run_shadow_fork(
        stdin, stdout, environ, template, prefix=prefix, model=model, apply=apply
    )
    return code, stdout.getvalue()


def _wrap_as_definition(text: str) -> str:
    """Wrap a bare ``key: value`` (+ blank line + body) string into a full ``.md`` definition.

    Most tests only care about the one or two front-matter fields they are
    testing, not ``description:`` or the ``---`` delimiters -- so a caller
    passes exactly what it used to pass the old ``.txt`` parser (front
    matter lines, a blank separator, then the body) and this adds a default
    ``description:`` (unless the caller's front matter already declares
    one) and the delimiters around it.
    """
    front, _sep, body = text.partition("\n\n")
    front_lines = front.splitlines() if front else []
    if not any(line.startswith("description:") for line in front_lines):
        front_lines.append("description: a test definition")
    return "---\n" + "\n".join(front_lines) + "\n---\n\n" + body


def _write_template(tmp_path: Path, name: str, text: str) -> dict[str, str]:
    """Write ``text`` (see :func:`_wrap_as_definition`) as ``name.md`` under a fresh
    ``CGX_TEMPLATE_DIR`` and return that env.
    """
    template_dir = tmp_path / "templates"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / f"{name}.md").write_text(_wrap_as_definition(text), encoding="utf-8")
    return {"CGX_TEMPLATE_DIR": str(template_dir)}


def _write_raw_template(tmp_path: Path, name: str, text: str) -> dict[str, str]:
    """Write ``text`` verbatim as ``name.md`` under a fresh ``CGX_TEMPLATE_DIR``.

    For tests that need to control the file's exact bytes (malformed front
    matter, missing delimiters, missing ``description:``), unlike
    :func:`_write_template` which patches those in.
    """
    template_dir = tmp_path / "templates"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / f"{name}.md").write_text(text, encoding="utf-8")
    return {"CGX_TEMPLATE_DIR": str(template_dir)}


_INTERVENE_RESULT = {"verdict": "intervene", "reason": "nope", "mutation": None}
_OK_RESULT = {"verdict": "ok", "reason": "fine", "mutation": None}


# --- silent no-ops: malformed input, prefix non-match -----------------------


def test_malformed_stdin_json_is_silent() -> None:
    stdin = io.StringIO("not json")
    stdout = io.StringIO()
    code = shadow_fork.run_shadow_fork(stdin, stdout, {}, "btw")
    assert code == 0
    assert stdout.getvalue() == ""


def test_non_dict_stdin_is_silent() -> None:
    stdin = io.StringIO(json.dumps([1, 2, 3]))
    stdout = io.StringIO()
    code = shadow_fork.run_shadow_fork(stdin, stdout, {}, "btw")
    assert code == 0
    assert stdout.getvalue() == ""


def test_missing_session_id_is_silent() -> None:
    code, out = _run({"prompt": "/btw hi", "hook_event_name": "UserPromptSubmit"}, {}, "btw")
    assert code == 0
    assert out == ""


def test_prefix_non_match_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "prompt": "hello there", "hook_event_name": "UserPromptSubmit"},
        {},
        "btw",
        prefix="/btw",
    )
    assert code == 0
    assert out == ""
    assert calls == []


def test_prefix_suffix_without_boundary_does_not_match(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "prompt": "/btwfoo", "hook_event_name": "UserPromptSubmit"},
        {},
        "btw",
        prefix="/btw",
    )
    assert code == 0
    assert out == ""
    assert calls == []


# --- fail-open, but visible: missing template / unreachable daemon ----------


@pytest.mark.invariant(decision="Fail-open, but visible")
def test_missing_template_skips_with_system_message() -> None:
    code, out = _run({"session_id": "s1", "hook_event_name": "PreToolUse"}, {}, "does-not-exist")
    assert code == 0
    data = json.loads(out)
    assert set(data) == {"systemMessage"}
    assert data["systemMessage"].startswith("cgx shadow-fork skipped")
    assert "decision" not in data
    assert "hookSpecificOutput" not in data


@pytest.mark.invariant(decision="Fail-open, but visible")
def test_missing_template_quiet_env_suppresses_message() -> None:
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse"},
        {"CGX_SHADOW_FORK_QUIET": "1"},
        "does-not-exist",
    )
    assert code == 0
    assert out == ""


@pytest.mark.invariant(decision="Definitions are Markdown files")
def test_tools_key_skips_with_system_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``tools:`` is rejected outright: a fork never calls a tool, so the key can never apply."""
    env = _write_raw_template(
        tmp_path, "t", "---\ndescription: a test\ntools: Bash\n---\n\nreview\n"
    )
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run({"session_id": "s1", "hook_event_name": "PreToolUse"}, env, "t")
    assert code == 0
    data = json.loads(out)
    assert data["systemMessage"].startswith("cgx shadow-fork skipped")
    assert "tools" in data["systemMessage"]
    assert calls == []


@pytest.mark.invariant(decision="Definitions are Markdown files")
def test_missing_description_skips_with_system_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_raw_template(tmp_path, "t", "---\nintervene: deny\n---\n\nreview\n")
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run({"session_id": "s1", "hook_event_name": "PreToolUse"}, env, "t")
    assert code == 0
    data = json.loads(out)
    assert data["systemMessage"].startswith("cgx shadow-fork skipped")
    assert "description" in data["systemMessage"]
    assert calls == []


@pytest.mark.invariant(decision="Fail-open, but visible")
def test_unreachable_daemon_skips_with_system_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    _install_client(monkeypatch, exc=DaemonUnreachable("cannot reach daemon at http://x"))
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"}, env, "t"
    )
    assert code == 0
    data = json.loads(out)
    assert set(data) == {"systemMessage"}
    assert data["systemMessage"].startswith("cgx shadow-fork skipped")


@pytest.mark.invariant(decision="Fail-open, but visible")
def test_unreachable_daemon_quiet_env_suppresses_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    env["CGX_SHADOW_FORK_QUIET"] = "1"
    _install_client(monkeypatch, exc=DaemonUnreachable("cannot reach daemon at http://x"))
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"}, env, "t"
    )
    assert code == 0
    assert out == ""


def test_daemon_api_error_skips_with_system_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    _install_client(monkeypatch, exc=ApiError(500, "boom"))
    code, out = _run({"session_id": "s1", "hook_event_name": "PreToolUse"}, env, "t")
    assert code == 0
    data = json.loads(out)
    assert data["systemMessage"].startswith("cgx shadow-fork skipped")


# --- ok verdict is completely silent -----------------------------------------


def test_ok_verdict_produces_empty_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    _install_client(monkeypatch, result=_OK_RESULT)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"}, env, "t"
    )
    assert code == 0
    assert out == ""


# --- output contract is appended by the tool, not the definition ------------


def test_output_contract_is_appended_to_the_rendered_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    calls = _install_client(monkeypatch, result=_OK_RESULT)
    _run({"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"}, env, "t")
    prompt = calls[0]["prompt"]
    assert prompt.startswith("review Bash")
    assert '"verdict": "ok"|"intervene"' in prompt
    assert "Reply with strict JSON and nothing else" in prompt
    assert "Never call a tool" in prompt


def test_output_contract_text_is_identical_across_definitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The appended contract text is one constant, not re-derived per definition."""
    env_a = _write_template(tmp_path, "a", "intervene: deny\n\nreview A\n")
    calls_a = _install_client(monkeypatch, result=_OK_RESULT)
    _run({"session_id": "s1", "hook_event_name": "PreToolUse"}, env_a, "a")
    contract_a = calls_a[0]["prompt"].split("review A\n\n", 1)[1]

    env_b = _write_template(tmp_path, "b", "intervene: deny\n\nreview B\n")
    calls_b = _install_client(monkeypatch, result=_OK_RESULT)
    _run({"session_id": "s1", "hook_event_name": "PreToolUse"}, env_b, "b")
    contract_b = calls_b[0]["prompt"].split("review B\n\n", 1)[1]

    assert contract_a == contract_b


# --- visible fail-open: a daemon-side fail-open "ok" is not silent ----------


@pytest.mark.parametrize(
    "reason",
    [
        "unparseable_verdict",
        "fork_failed",
        "tool_use_reply",
        "thinking_unsafe_model",
        "thinking_model_unknown",
        "no_snapshot",
        "rate_limited",
        "snapshot_stale",
    ],
)
def test_fail_open_ok_verdict_emits_skip_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    _install_client(monkeypatch, result={"verdict": "ok", "reason": reason, "mutation": None})
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"}, env, "t"
    )
    assert code == 0
    data = json.loads(out)
    assert data["systemMessage"] == f"cgx shadow-fork skipped: {reason}"


def test_fail_open_ok_verdict_quiet_env_suppresses_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    env["CGX_SHADOW_FORK_QUIET"] = "1"
    _install_client(
        monkeypatch, result={"verdict": "ok", "reason": "rate_limited", "mutation": None}
    )
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"}, env, "t"
    )
    assert code == 0
    assert out == ""


def test_genuine_ok_verdict_from_model_stays_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model's own ``ok`` verdict, with an arbitrary free-text reason, is never mistaken
    for one of the fixed fail-open reason codes.
    """
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    _install_client(
        monkeypatch, result={"verdict": "ok", "reason": "this looks fine", "mutation": None}
    )
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"}, env, "t"
    )
    assert code == 0
    assert out == ""


def test_model_absent_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    calls = _install_client(monkeypatch, result=_OK_RESULT)
    _run({"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"}, env, "t")
    assert calls[0]["model"] is None


def test_model_override_reaches_the_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    calls = _install_client(monkeypatch, result=_OK_RESULT)
    _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"},
        env,
        "t",
        model="claude-override",
    )
    assert calls[0]["model"] == "claude-override"


def test_model_definition_field_reaches_the_client_when_no_cli_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A definition's ``model:`` front matter is passed through like a bare ``--model``."""
    env = _write_template(
        tmp_path, "t", "intervene: deny\nmodel: claude-from-definition\n\nreview {tool_name}\n"
    )
    calls = _install_client(monkeypatch, result=_OK_RESULT)
    _run({"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"}, env, "t")
    assert calls[0]["model"] == "claude-from-definition"


def test_model_cli_override_wins_over_definition_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(
        tmp_path, "t", "intervene: deny\nmodel: claude-from-definition\n\nreview {tool_name}\n"
    )
    calls = _install_client(monkeypatch, result=_OK_RESULT)
    _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"},
        env,
        "t",
        model="claude-from-cli",
    )
    assert calls[0]["model"] == "claude-from-cli"


# --- verdict-to-output mapping ------------------------------------------------


def test_pretooluse_deny_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"}, env, "t"
    )
    assert code == 0
    assert json.loads(out) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "nope",
        }
    }


def test_pretooluse_ask_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_template(tmp_path, "t", "intervene: ask\n\nreview {tool_name}\n")
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run({"session_id": "s1", "hook_event_name": "PreToolUse"}, env, "t")
    assert code == 0
    assert json.loads(out) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "nope",
        }
    }


def test_pretooluse_context_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_template(tmp_path, "t", "intervene: context\n\nreview {tool_name}\n")
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run({"session_id": "s1", "hook_event_name": "PreToolUse"}, env, "t")
    assert code == 0
    assert json.loads(out) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "nope",
        }
    }


def test_userpromptsubmit_block_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nanswer {argument}\n")
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "prompt": "/btw hi", "hook_event_name": "UserPromptSubmit"},
        env,
        "t",
        prefix="/btw",
    )
    assert code == 0
    assert json.loads(out) == {"decision": "block", "reason": "nope"}


def test_posttooluse_block_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run({"session_id": "s1", "hook_event_name": "PostToolUse"}, env, "t")
    assert code == 0
    assert json.loads(out) == {"decision": "block", "reason": "nope"}


def test_userpromptsubmit_context_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_template(tmp_path, "t", "intervene: context\n\nanswer {argument}\n")
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "prompt": "/btw hi", "hook_event_name": "UserPromptSubmit"},
        env,
        "t",
        prefix="/btw",
    )
    assert code == 0
    assert json.loads(out) == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "nope",
        }
    }


def test_posttooluse_context_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_template(tmp_path, "t", "intervene: context\n\nreview {tool_name}\n")
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run({"session_id": "s1", "hook_event_name": "PostToolUse"}, env, "t")
    assert code == 0
    assert json.loads(out) == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "nope",
        }
    }


def test_ask_with_no_declared_event_behaves_as_deny_at_userpromptsubmit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A definition with no ``event:`` line can declare ``ask``; at runtime, on an event with
    no ``ask`` decision of its own, it falls back to the same ``block`` shape ``deny`` uses.
    """
    env = _write_template(tmp_path, "t", "intervene: ask\n\nanswer {argument}\n")
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "prompt": "/btw hi", "hook_event_name": "UserPromptSubmit"},
        env,
        "t",
        prefix="/btw",
    )
    assert code == 0
    assert json.loads(out) == {"decision": "block", "reason": "nope"}


def test_unrecognised_hook_event_produces_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run({"session_id": "s1", "hook_event_name": "Stop"}, env, "t")
    assert code == 0
    assert out == ""


# --- mutation: PreToolUse gets updatedInput, elsewhere it's dropped ---------


def test_mutation_becomes_updated_input_at_pretooluse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    result = {"verdict": "intervene", "reason": "nope", "mutation": {"command": "ls"}}
    _install_client(monkeypatch, result=result)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"}, env, "t"
    )
    assert code == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["updatedInput"] == {"command": "ls"}


def test_mutation_is_dropped_at_userpromptsubmit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nanswer {argument}\n")
    result = {"verdict": "intervene", "reason": "nope", "mutation": {"command": "ls"}}
    _install_client(monkeypatch, result=result)
    code, out = _run(
        {"session_id": "s1", "prompt": "/btw hi", "hook_event_name": "UserPromptSubmit"},
        env,
        "t",
        prefix="/btw",
    )
    assert code == 0
    data = json.loads(out)
    assert data == {"decision": "block", "reason": "nope"}
    assert "mutation" not in data


@pytest.mark.invariant(decision="mutation is honoured only where the hook API has a slot for it")
def test_dropped_mutation_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nanswer {argument}\n")
    result = {"verdict": "intervene", "reason": "nope", "mutation": {"command": "ls"}}
    _install_client(monkeypatch, result=result)
    with caplog.at_level("WARNING", logger="claude_gateway_extensions.cli.shadow_fork"):
        code, out = _run(
            {"session_id": "s1", "prompt": "/btw hi", "hook_event_name": "UserPromptSubmit"},
            env,
            "t",
            prefix="/btw",
        )
    assert code == 0
    assert json.loads(out) == {"decision": "block", "reason": "nope"}
    assert any(
        "mutation" in record.getMessage() and "UserPromptSubmit" in record.getMessage()
        for record in caplog.records
    )


# --- front-matter parsing -----------------------------------------------------


def test_absent_front_matter_skips_with_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A definition with no front matter at all is malformed, not verbatim-body-deny.

    Unlike the old ``.txt`` format, a ``.md`` definition must start with a
    ``---`` delimiter -- ``description:`` is required, so there is no shape
    a front-matter-less file could be valid in.
    """
    env = _write_raw_template(tmp_path, "t", "review {tool_name}, no header at all\n")
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run({"session_id": "s1", "hook_event_name": "PreToolUse"}, env, "t")
    assert code == 0
    data = json.loads(out)
    assert data["systemMessage"].startswith("cgx shadow-fork skipped")
    assert calls == []


# --- definitions.parse_definition: the accepted flat YAML subset ------------


def test_parse_definition_reads_event_matcher_and_model() -> None:
    definition = definitions.parse_definition(
        "---\n"
        "description: a test\n"
        "model: claude-opus-4-5\n"
        "intervene: ask\n"
        "event: PreToolUse\n"
        "matcher: Bash\n"
        "---\n\n"
        "review {tool_name}\n",
        default_name="t",
    )
    assert definition.name == "t"
    assert definition.description == "a test"
    assert definition.model == "claude-opus-4-5"
    assert definition.intervene == "ask"
    assert definition.event == "PreToolUse"
    assert definition.matcher == "Bash"
    assert definition.require is None
    assert definition.body == "review {tool_name}"


def test_parse_definition_name_defaults_to_stem_but_can_be_overridden() -> None:
    default = definitions.parse_definition(
        "---\ndescription: a test\n---\n\nbody\n", default_name="stem-name"
    )
    assert default.name == "stem-name"
    overridden = definitions.parse_definition(
        "---\nname: custom\ndescription: a test\n---\n\nbody\n", default_name="stem-name"
    )
    assert overridden.name == "custom"


def test_parse_definition_rejects_unknown_key() -> None:
    with pytest.raises(definitions.DefinitionError, match="unknown front matter key 'author'"):
        definitions.parse_definition(
            "---\ndescription: a test\nauthor: someone\n---\n\nreview\n", default_name="t"
        )


def test_parse_definition_rejects_tools_key() -> None:
    with pytest.raises(definitions.DefinitionError, match="tools"):
        definitions.parse_definition(
            "---\ndescription: a test\ntools: Bash\n---\n\nreview\n", default_name="t"
        )


def test_parse_definition_rejects_invalid_event() -> None:
    with pytest.raises(definitions.DefinitionError, match="event"):
        definitions.parse_definition(
            "---\ndescription: a test\nevent: NotARealEvent\n---\n\nreview\n", default_name="t"
        )


def test_parse_definition_rejects_invalid_intervene() -> None:
    with pytest.raises(definitions.DefinitionError, match="intervene"):
        definitions.parse_definition(
            "---\ndescription: a test\nintervene: maybe\n---\n\nreview\n", default_name="t"
        )


@pytest.mark.parametrize("event", ["UserPromptSubmit", "PostToolUse"])
def test_parse_definition_rejects_ask_for_decision_block_event(event: str) -> None:
    with pytest.raises(definitions.DefinitionError, match="intervene: ask"):
        definitions.parse_definition(
            f"---\ndescription: a test\nintervene: ask\nevent: {event}\n---\n\nreview\n",
            default_name="t",
        )


def test_parse_definition_allows_ask_with_no_declared_event() -> None:
    """``ask`` with no ``event:`` line can't be checked statically, so it is accepted."""
    definition = definitions.parse_definition(
        "---\ndescription: a test\nintervene: ask\n---\n\nreview\n", default_name="t"
    )
    assert definition.intervene == "ask"
    assert definition.event is None


def test_parse_definition_requires_description() -> None:
    with pytest.raises(definitions.DefinitionError, match="description"):
        definitions.parse_definition("---\nevent: PreToolUse\n---\n\nreview\n", default_name="t")


def test_parse_definition_requires_opening_delimiter() -> None:
    with pytest.raises(definitions.DefinitionError, match="---"):
        definitions.parse_definition("description: a test\n\nreview\n", default_name="t")


def test_parse_definition_requires_closing_delimiter() -> None:
    with pytest.raises(definitions.DefinitionError, match="unterminated"):
        definitions.parse_definition("---\ndescription: a test\n\nreview\n", default_name="t")


def test_parse_definition_rejects_duplicate_key() -> None:
    with pytest.raises(definitions.DefinitionError, match="duplicate"):
        definitions.parse_definition(
            "---\ndescription: one\ndescription: two\n---\n\nreview\n", default_name="t"
        )


def test_parse_definition_rejects_scalar_require() -> None:
    with pytest.raises(definitions.DefinitionError, match="require"):
        definitions.parse_definition(
            "---\ndescription: a test\nrequire: tool_input.owner\n---\n\nreview\n",
            default_name="t",
        )


def test_parse_definition_rejects_list_for_scalar_key() -> None:
    with pytest.raises(definitions.DefinitionError, match="event"):
        definitions.parse_definition(
            "---\ndescription: a test\nevent:\n  - PreToolUse\n---\n\nreview\n", default_name="t"
        )


def test_parse_definition_reads_require_list() -> None:
    definition = definitions.parse_definition(
        "---\ndescription: a test\nrequire:\n  - tool_input.owner\n  - tool_name=^Task\n"
        "---\n\nreview\n",
        default_name="t",
    )
    assert definition.require == ["tool_input.owner", "tool_name=^Task"]


def test_parse_definition_ignores_blank_lines_and_comments() -> None:
    definition = definitions.parse_definition(
        "---\n"
        "# a comment\n"
        "description: a test\n"
        "\n"
        "  # an indented comment\n"
        "event: PreToolUse\n"
        "---\n\n"
        "review\n",
        default_name="t",
    )
    assert definition.description == "a test"
    assert definition.event == "PreToolUse"


def test_parse_definition_strips_matching_quotes() -> None:
    definition = definitions.parse_definition(
        '---\ndescription: "a quoted: description"\n---\n\nreview\n', default_name="t"
    )
    assert definition.description == "a quoted: description"


def test_parse_definition_list_item_without_preceding_key_is_an_error() -> None:
    with pytest.raises(definitions.DefinitionError, match="list item"):
        definitions.parse_definition(
            "---\ndescription: a test\n  - stray item\n---\n\nreview\n", default_name="t"
        )


def test_declared_event_mismatch_skips_with_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(
        tmp_path, "t", "intervene: deny\nevent: PreToolUse\n\nreview {tool_name}\n"
    )
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "hi"}, env, "t"
    )
    assert code == 0
    data = json.loads(out)
    assert set(data) == {"systemMessage"}
    assert data["systemMessage"].startswith("cgx shadow-fork skipped")
    assert "PreToolUse" in data["systemMessage"]
    assert "UserPromptSubmit" in data["systemMessage"]
    assert calls == []


def test_declared_event_mismatch_quiet_env_suppresses_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(
        tmp_path, "t", "intervene: deny\nevent: PreToolUse\n\nreview {tool_name}\n"
    )
    env["CGX_SHADOW_FORK_QUIET"] = "1"
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "hi"}, env, "t"
    )
    assert code == 0
    assert out == ""
    assert calls == []


def test_declared_event_match_proceeds_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(
        tmp_path, "t", "intervene: deny\nevent: PreToolUse\n\nreview {tool_name}\n"
    )
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"}, env, "t"
    )
    assert code == 0
    assert len(calls) == 1
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- require: a payload guard that runs before rendering or the daemon call --


def test_require_field_present_reaches_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(
        tmp_path,
        "t",
        "intervene: deny\nrequire:\n  - tool_input.owner\n\nreview {tool_input}\n",
    )
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_input": {"owner": "sonnet"},
        },
        env,
        "t",
    )
    assert code == 0
    assert len(calls) == 1
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_require_field_missing_is_silent_with_no_daemon_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(
        tmp_path,
        "t",
        "intervene: deny\nrequire:\n  - tool_input.owner\n\nreview {tool_input}\n",
    )
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_input": {"status": "done"}},
        env,
        "t",
    )
    assert code == 0
    assert out == ""
    assert calls == []


def test_require_empty_string_field_counts_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(
        tmp_path,
        "t",
        "intervene: deny\nrequire:\n  - tool_input.owner\n\nreview {tool_input}\n",
    )
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_input": {"owner": ""}},
        env,
        "t",
    )
    assert code == 0
    assert out == ""
    assert calls == []


def test_require_regex_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_template(
        tmp_path,
        "t",
        "intervene: deny\nrequire:\n  - tool_name=^Task(Create|Update)$\n\nreview {tool_name}\n",
    )
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "TaskUpdate"},
        env,
        "t",
    )
    assert code == 0
    assert len(calls) == 1
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_require_regex_rejects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_template(
        tmp_path,
        "t",
        "intervene: deny\nrequire:\n  - tool_name=^Task(Create|Update)$\n\nreview {tool_name}\n",
    )
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"}, env, "t"
    )
    assert code == 0
    assert out == ""
    assert calls == []


def test_require_multiple_requirements_are_anded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(
        tmp_path,
        "t",
        "intervene: deny\nrequire:\n  - tool_input.owner\n"
        "  - tool_name=^Task(Create|Update)$\n\n"
        "review {tool_input}\n",
    )
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    # tool_name matches but tool_input.owner is absent: must not reach the daemon.
    code, out = _run(
        {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "TaskUpdate",
            "tool_input": {"status": "done"},
        },
        env,
        "t",
    )
    assert code == 0
    assert out == ""
    assert calls == []

    # Both hold: reaches the daemon.
    code, out = _run(
        {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "TaskUpdate",
            "tool_input": {"owner": "sonnet"},
        },
        env,
        "t",
    )
    assert code == 0
    assert len(calls) == 1
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_require_malformed_value_skips_with_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(
        tmp_path,
        "t",
        "intervene: deny\nrequire:\n  - tool_input.[owner]\n\nreview {tool_input}\n",
    )
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_input": {"owner": "x"}},
        env,
        "t",
    )
    assert code == 0
    data = json.loads(out)
    assert set(data) == {"systemMessage"}
    assert data["systemMessage"].startswith("cgx shadow-fork skipped")
    assert calls == []


def test_require_malformed_regex_skips_with_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(
        tmp_path,
        "t",
        "intervene: deny\nrequire:\n  - tool_name=[unterminated\n\nreview\n",
    )
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"}, env, "t"
    )
    assert code == 0
    data = json.loads(out)
    assert data["systemMessage"].startswith("cgx shadow-fork skipped")
    assert calls == []


def test_unknown_placeholder_survives_literally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {totally_unknown_field}\n")
    calls = _install_client(monkeypatch, result=_OK_RESULT)
    code, out = _run({"session_id": "s1", "hook_event_name": "PreToolUse"}, env, "t")
    assert code == 0
    assert out == ""
    assert len(calls) == 1
    assert "{totally_unknown_field}" in calls[0]["prompt"]


def test_json_contract_braces_survive_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The literal JSON contract in a template must not be mistaken for a placeholder."""
    template = (
        'intervene: deny\n\n{"verdict": "ok"|"intervene", "reason": "...", "mutation": null}\n'
    )
    env = _write_template(tmp_path, "t", template)
    calls = _install_client(monkeypatch, result=_OK_RESULT)
    code, out = _run({"session_id": "s1", "hook_event_name": "PreToolUse"}, env, "t")
    assert code == 0
    assert out == ""
    assert '{"verdict": "ok"|"intervene"' in calls[0]["prompt"]


# --- template directory resolution: HOME / XDG_CONFIG_HOME / APPDATA --------


def test_template_dir_uses_xdg_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xdg_config_home = tmp_path / "xdg-config"
    template_dir = xdg_config_home / "cgx" / "templates"
    template_dir.mkdir(parents=True)
    (template_dir / "t.md").write_text(
        "---\ndescription: a test\nintervene: deny\n---\n\nreview\n", encoding="utf-8"
    )
    env = {"XDG_CONFIG_HOME": str(xdg_config_home)}
    # Assert the redirect took effect: XDG_CONFIG_HOME resolves to exactly the
    # directory the template was placed under, inside pytest's tmp_path sandbox.
    assert Path(env["XDG_CONFIG_HOME"]) == xdg_config_home
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run({"session_id": "s1", "hook_event_name": "PreToolUse"}, env, "t")
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] == "nope"


def test_template_dir_uses_appdata_when_xdg_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appdata = tmp_path / "AppData" / "Roaming"
    template_dir = appdata / "cgx" / "templates"
    template_dir.mkdir(parents=True)
    (template_dir / "t.md").write_text(
        "---\ndescription: a test\nintervene: deny\n---\n\nreview\n", encoding="utf-8"
    )
    env = {"APPDATA": str(appdata)}
    assert Path(env["APPDATA"]) == appdata
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run({"session_id": "s1", "hook_event_name": "PreToolUse"}, env, "t")
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] == "nope"


def test_template_dir_falls_back_to_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home" / "someone"
    template_dir = home / ".config" / "cgx" / "templates"
    template_dir.mkdir(parents=True)
    (template_dir / "t.md").write_text(
        "---\ndescription: a test\nintervene: deny\n---\n\nreview\n", encoding="utf-8"
    )
    env = {"HOME": str(home)}
    assert Path(env["HOME"]) == home
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run({"session_id": "s1", "hook_event_name": "PreToolUse"}, env, "t")
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] == "nope"


def test_template_dir_prefers_cgx_template_dir_over_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg_config_home = tmp_path / "xdg-config"
    decoy_dir = xdg_config_home / "cgx" / "templates"
    decoy_dir.mkdir(parents=True)
    (decoy_dir / "t.md").write_text(
        "---\ndescription: a test\nintervene: ask\n---\n\ndecoy\n", encoding="utf-8"
    )

    override_dir = tmp_path / "override"
    override_dir.mkdir()
    (override_dir / "t.md").write_text(
        "---\ndescription: a test\nintervene: deny\n---\n\nreal\n", encoding="utf-8"
    )

    env = {"CGX_TEMPLATE_DIR": str(override_dir), "XDG_CONFIG_HOME": str(xdg_config_home)}
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run({"session_id": "s1", "hook_event_name": "PreToolUse"}, env, "t")
    assert code == 0
    # If the decoy (XDG_CONFIG_HOME) had been used instead, this would be "ask".
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- --apply: act on an intervene verdict without touching the conversation --


def _write_apply_script(tmp_path: Path, *, exit_code: int = 0, sleep_s: float = 0.0) -> str:
    """Return a command that records its stdin, echoes a leak marker, then exits.

    ``_run_apply`` always invokes the command via ``subprocess.run(...,
    shell=True)``, which is ``/bin/sh`` on POSIX and ``cmd.exe`` on Windows.
    Rather than a ``#!/bin/sh`` script (POSIX-only: shebangs, ``chmod``,
    ``cat`` are not portable to ``cmd.exe``), this writes an inline Python
    script and returns a command that runs it via ``sys.executable``, which
    both shells can invoke identically. The echoed ``APPLY-STDOUT-LEAK`` line
    lets a test assert the command's own stdout never reaches
    ``run_shadow_fork``'s stdout.
    """
    script = tmp_path / "apply.py"
    stdin_capture = tmp_path / "apply-stdin.txt"
    script.write_text(
        "import sys, time\n"
        f"time.sleep({sleep_s})\n"
        f"open({str(stdin_capture)!r}, 'w', encoding='utf-8').write(sys.stdin.read())\n"
        "print('APPLY-STDOUT-LEAK')\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def test_apply_runs_on_intervene_with_verdict_json_on_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    script = _write_apply_script(tmp_path)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"},
        env,
        "t",
        apply=str(script),
    )
    assert code == 0
    stdin_capture = tmp_path / "apply-stdin.txt"
    assert stdin_capture.is_file()
    assert json.loads(stdin_capture.read_text(encoding="utf-8")) == {
        "verdict": "intervene",
        "reason": "nope",
        "mutation": None,
    }
    # The mapped verdict output is unaffected by a successful apply.
    assert json.loads(out) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "nope",
        }
    }


def test_apply_does_not_run_on_ok_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    _install_client(monkeypatch, result=_OK_RESULT)
    script = _write_apply_script(tmp_path)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"},
        env,
        "t",
        apply=str(script),
    )
    assert code == 0
    assert out == ""
    assert not (tmp_path / "apply-stdin.txt").is_file()


def test_apply_failing_command_skips_with_message_and_keeps_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    script = _write_apply_script(tmp_path, exit_code=1)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"},
        env,
        "t",
        apply=str(script),
    )
    assert code == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert data["systemMessage"].startswith("cgx shadow-fork skipped")
    assert "APPLY-STDOUT-LEAK" not in out


def test_apply_timeout_skips_with_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    monkeypatch.setattr(shadow_fork, "_APPLY_TIMEOUT_S", 0.2)
    script = _write_apply_script(tmp_path, sleep_s=2.0)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"},
        env,
        "t",
        apply=str(script),
    )
    assert code == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "timed out" in data["systemMessage"]
    assert "APPLY-STDOUT-LEAK" not in out


def test_apply_missing_command_skips_with_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"},
        env,
        "t",
        apply=str(tmp_path / "does-not-exist-at-all"),
    )
    assert code == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert data["systemMessage"].startswith("cgx shadow-fork skipped")


def test_apply_failure_message_suppressed_by_quiet_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    env["CGX_SHADOW_FORK_QUIET"] = "1"
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    script = _write_apply_script(tmp_path, exit_code=1)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"},
        env,
        "t",
        apply=str(script),
    )
    assert code == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "systemMessage" not in data


# --- packaged example templates ----------------------------------------------


@pytest.mark.parametrize(
    ("name", "hook_event_name", "expected_intervene"),
    [
        ("task-assignment", "PreToolUse", "context"),
        ("task-followup", "PreToolUse", "context"),
        ("permission-classifier", "PreToolUse", "deny"),
        ("btw", "UserPromptSubmit", "deny"),
        ("rename", "UserPromptSubmit", "deny"),
    ],
)
def test_packaged_templates_are_loadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    hook_event_name: str,
    expected_intervene: str,
) -> None:
    """With no user template directory populated, the packaged examples are used.

    Scripting an ``intervene`` verdict and checking the mapped output's shape
    (rather than just that *something* was produced) confirms the packaged
    template's own ``intervene:`` front matter was actually parsed.
    ``hook_event_name`` matches each template's own declared ``event:`` line,
    since a mismatch now skips instead of reviewing. ``tool_input`` carries
    an ``owner`` field so ``task-assignment``'s ``require:`` guard holds too.
    """
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    env = {"HOME": str(empty_home)}
    assert Path(env["HOME"]) == empty_home
    assert not (empty_home / ".config" / "cgx" / "templates").exists()
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {
            "session_id": "s1",
            "hook_event_name": hook_event_name,
            "tool_name": "Bash",
            "tool_input": {"command": "ls", "owner": "sonnet"},
            "cwd": "/tmp",
            "prompt": "hello",
        },
        env,
        name,
    )
    assert code == 0
    assert len(calls) == 1
    assert '"verdict"' in calls[0]["prompt"]
    if hook_event_name == "UserPromptSubmit":
        assert json.loads(out) == {"decision": "block", "reason": "nope"}
        return
    hook_specific = json.loads(out)["hookSpecificOutput"]
    if expected_intervene == "context":
        assert "additionalContext" in hook_specific
    else:
        assert hook_specific["permissionDecision"] == expected_intervene
