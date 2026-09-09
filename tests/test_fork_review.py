"""Tests for claude_gateway_extensions.cli.fork_review."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from claude_gateway_extensions.cli import fork_review
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
    """Patch ``fork_review.ControlClient`` and return the list its ``fork()`` calls land in."""
    calls: list[dict[str, Any]] = []

    def factory(base_url: str) -> _ScriptedClient:
        return _ScriptedClient(base_url, result=result, exc=exc, calls=calls)

    monkeypatch.setattr(fork_review, "ControlClient", factory)
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
    code = fork_review.run_fork_review(
        stdin, stdout, environ, template, prefix=prefix, model=model, apply=apply
    )
    return code, stdout.getvalue()


def _write_template(tmp_path: Path, name: str, text: str) -> dict[str, str]:
    """Write ``text`` as ``name.txt`` under a fresh ``CGX_TEMPLATE_DIR`` and return that env."""
    template_dir = tmp_path / "templates"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / f"{name}.txt").write_text(text, encoding="utf-8")
    return {"CGX_TEMPLATE_DIR": str(template_dir)}


_INTERVENE_RESULT = {"verdict": "intervene", "reason": "nope", "mutation": None}
_OK_RESULT = {"verdict": "ok", "reason": "fine", "mutation": None}


# --- silent no-ops: malformed input, prefix non-match -----------------------


def test_malformed_stdin_json_is_silent() -> None:
    stdin = io.StringIO("not json")
    stdout = io.StringIO()
    code = fork_review.run_fork_review(stdin, stdout, {}, "btw")
    assert code == 0
    assert stdout.getvalue() == ""


def test_non_dict_stdin_is_silent() -> None:
    stdin = io.StringIO(json.dumps([1, 2, 3]))
    stdout = io.StringIO()
    code = fork_review.run_fork_review(stdin, stdout, {}, "btw")
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
    assert data["systemMessage"].startswith("cgx fork-review skipped")
    assert "decision" not in data
    assert "hookSpecificOutput" not in data


@pytest.mark.invariant(decision="Fail-open, but visible")
def test_missing_template_quiet_env_suppresses_message() -> None:
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse"},
        {"CGX_FORK_REVIEW_QUIET": "1"},
        "does-not-exist",
    )
    assert code == 0
    assert out == ""


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
    assert data["systemMessage"].startswith("cgx fork-review skipped")


@pytest.mark.invariant(decision="Fail-open, but visible")
def test_unreachable_daemon_quiet_env_suppresses_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    env["CGX_FORK_REVIEW_QUIET"] = "1"
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
    assert data["systemMessage"].startswith("cgx fork-review skipped")


# --- ok verdict is completely silent -----------------------------------------


def test_ok_verdict_produces_empty_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    _install_client(monkeypatch, result=_OK_RESULT)
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


# --- front-matter parsing -----------------------------------------------------


def test_absent_front_matter_defaults_to_deny(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(tmp_path, "t", "review {tool_name}, no header at all\n")
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run({"session_id": "s1", "hook_event_name": "PreToolUse"}, env, "t")
    assert code == 0
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_parse_template_reads_event_and_matcher() -> None:
    intervene, event, matcher, require, body = fork_review._parse_template(
        "intervene: ask\nevent: PreToolUse\nmatcher: Bash\n\nreview {tool_name}\n"
    )
    assert intervene == "ask"
    assert event == "PreToolUse"
    assert matcher == "Bash"
    assert require is None
    assert body == "review {tool_name}"


def test_parse_template_ignores_unknown_key() -> None:
    intervene, event, matcher, require, body = fork_review._parse_template(
        "intervene: deny\nauthor: someone\n\nreview\n"
    )
    assert intervene == "deny"
    assert event is None
    assert matcher is None
    assert require is None
    assert body == "review"


def test_parse_template_invalid_event_falls_back_to_undeclared() -> None:
    _intervene, event, _matcher, _require, _body = fork_review._parse_template(
        "intervene: deny\nevent: NotARealEvent\n\nreview\n"
    )
    assert event is None


def test_parse_template_no_front_matter_leaves_event_and_matcher_unset() -> None:
    intervene, event, matcher, require, body = fork_review._parse_template(
        "review, no header at all\n"
    )
    assert intervene == "deny"
    assert event is None
    assert matcher is None
    assert require is None
    assert body == "review, no header at all"


def test_parse_template_reads_require() -> None:
    _intervene, _event, _matcher, require, _body = fork_review._parse_template(
        "intervene: deny\nrequire: tool_input.owner\n\nreview\n"
    )
    assert require == "tool_input.owner"


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
    assert data["systemMessage"].startswith("cgx fork-review skipped")
    assert "PreToolUse" in data["systemMessage"]
    assert "UserPromptSubmit" in data["systemMessage"]
    assert calls == []


def test_declared_event_mismatch_quiet_env_suppresses_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(
        tmp_path, "t", "intervene: deny\nevent: PreToolUse\n\nreview {tool_name}\n"
    )
    env["CGX_FORK_REVIEW_QUIET"] = "1"
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
        tmp_path, "t", "intervene: deny\nrequire: tool_input.owner\n\nreview {tool_input}\n"
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
        tmp_path, "t", "intervene: deny\nrequire: tool_input.owner\n\nreview {tool_input}\n"
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
        tmp_path, "t", "intervene: deny\nrequire: tool_input.owner\n\nreview {tool_input}\n"
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
        "intervene: deny\nrequire: tool_name=^Task(Create|Update)$\n\nreview {tool_name}\n",
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
        "intervene: deny\nrequire: tool_name=^Task(Create|Update)$\n\nreview {tool_name}\n",
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
        "intervene: deny\nrequire: tool_input.owner,tool_name=^Task(Create|Update)$\n\n"
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
        tmp_path, "t", "intervene: deny\nrequire: tool_input.[owner]\n\nreview {tool_input}\n"
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
    assert data["systemMessage"].startswith("cgx fork-review skipped")
    assert calls == []


def test_require_malformed_regex_skips_with_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(
        tmp_path, "t", "intervene: deny\nrequire: tool_name=[unterminated\n\nreview\n"
    )
    calls = _install_client(monkeypatch, result=_INTERVENE_RESULT)
    code, out = _run(
        {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash"}, env, "t"
    )
    assert code == 0
    data = json.loads(out)
    assert data["systemMessage"].startswith("cgx fork-review skipped")
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
    (template_dir / "t.txt").write_text("intervene: deny\n\nreview\n", encoding="utf-8")
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
    (template_dir / "t.txt").write_text("intervene: deny\n\nreview\n", encoding="utf-8")
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
    (template_dir / "t.txt").write_text("intervene: deny\n\nreview\n", encoding="utf-8")
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
    (decoy_dir / "t.txt").write_text("intervene: ask\n\ndecoy\n", encoding="utf-8")

    override_dir = tmp_path / "override"
    override_dir.mkdir()
    (override_dir / "t.txt").write_text("intervene: deny\n\nreal\n", encoding="utf-8")

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
    ``run_fork_review``'s stdout.
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
    assert data["systemMessage"].startswith("cgx fork-review skipped")
    assert "APPLY-STDOUT-LEAK" not in out


def test_apply_timeout_skips_with_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    _install_client(monkeypatch, result=_INTERVENE_RESULT)
    monkeypatch.setattr(fork_review, "_APPLY_TIMEOUT_S", 0.2)
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
    assert data["systemMessage"].startswith("cgx fork-review skipped")


def test_apply_failure_message_suppressed_by_quiet_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _write_template(tmp_path, "t", "intervene: deny\n\nreview {tool_name}\n")
    env["CGX_FORK_REVIEW_QUIET"] = "1"
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
