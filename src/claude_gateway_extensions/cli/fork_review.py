"""``cgx fork-review`` -- turn any Claude Code hook event into a shadow review.

Per the "Shadow review fork" section in ``docs/DESIGN.md``, this is the only
hook-side entry point: it reads the hook JSON on stdin, renders a named
template into a review prompt, calls ``POST /cgx/sessions/{id}/fork`` via
:class:`~claude_gateway_extensions.cli.client.ControlClient`, and maps the
verdict back to whichever event's hook output shape the payload names. On an
``ok`` verdict the parent session sees nothing at all.

Fail-open, but visible: a review that cannot run must never block or alter
the session, so every failure path (missing template, unreachable daemon,
API error, unexpected exception) exits 0 with no decision fields. It is not
silent, though -- each failure emits a ``systemMessage`` starting with
``"cgx fork-review skipped"`` so a broken template or a dead daemon cannot
fail invisibly. Set ``CGX_FORK_REVIEW_QUIET=1`` to suppress that message.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any, TextIO

from claude_gateway_extensions.cli.client import ApiError, ControlClient, DaemonUnreachable
from claude_gateway_extensions.config import Config, resolve_home

#: The template front matter's only recognised key, and its allowed values.
_INTERVENE_VALUES = {"deny", "ask", "context"}

#: Matches one front-matter line, e.g. ``intervene: ask``.
_FRONT_MATTER_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$")

#: Matches a ``{placeholder}`` -- deliberately narrower than
#: :meth:`str.format`'s syntax so a template's JSON contract example
#: (``{"verdict": ...}``) is never mistaken for a placeholder and never
#: needs escaping.
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: Boolean-ish values ``CGX_FORK_REVIEW_QUIET`` accepts as true, matching
#: ``config.py``'s ``_parse_bool`` set.
_TRUE_VALUES = {"true", "1", "yes"}

#: Hook events whose output uses the top-level ``decision: "block"`` shape.
_DECISION_BLOCK_EVENTS = {"UserPromptSubmit", "PostToolUse"}


class _TemplateError(Exception):
    """Raised when a named template cannot be found or read."""


def _env_flag(environ: Mapping[str, str], name: str) -> bool:
    """Return whether ``environ[name]`` is set to a true-ish value."""
    return environ.get(name, "").strip().lower() in _TRUE_VALUES


def _template_dir(environ: Mapping[str, str]) -> Path:
    r"""Return the user's template directory, following ``config.py``'s env-only style.

    Precedence: ``CGX_TEMPLATE_DIR`` if set, then ``XDG_CONFIG_HOME``, then
    (Windows signalled by ``APPDATA`` being present in ``environ``, never by
    ``sys.platform``) ``%APPDATA%\cgx\templates``, then the POSIX/XDG
    default ``~/.config/cgx/templates`` under :func:`resolve_home`.
    """
    override = environ.get("CGX_TEMPLATE_DIR")
    if override:
        return Path(override).expanduser()
    xdg_config_home = environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / "cgx" / "templates"
    appdata = environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "cgx" / "templates"
    return resolve_home(environ) / ".config" / "cgx" / "templates"


def _load_template(name: str, environ: Mapping[str, str]) -> str:
    """Return the named template's raw text: the user directory first, then packaged examples.

    Raises :class:`_TemplateError` if neither location has it or the user
    copy cannot be read.
    """
    user_path = _template_dir(environ) / f"{name}.txt"
    if user_path.is_file():
        try:
            return user_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise _TemplateError(f"cannot read template {name!r} ({exc})") from exc
    packaged = resources.files("claude_gateway_extensions").joinpath("templates", f"{name}.txt")
    try:
        if packaged.is_file():
            return packaged.read_text(encoding="utf-8")
    except OSError as exc:
        raise _TemplateError(f"cannot read packaged template {name!r} ({exc})") from exc
    raise _TemplateError(f"template not found: {name!r}")


def _parse_template(text: str) -> tuple[str, str]:
    """Split ``text`` into its ``intervene`` mode and body.

    Optional ``key: value`` lines at the top, then a blank separator line,
    then the body. The only recognised key is ``intervene`` (``deny`` by
    default); an absent header, or a value outside ``deny``/``ask``/
    ``context``, both fall back to ``deny``. A template with no front matter
    at all (no line matching the ``key: value`` shape) is used verbatim as
    the body.
    """
    lines = text.splitlines()
    front_matter: dict[str, str] = {}
    i = 0
    while i < len(lines):
        match = _FRONT_MATTER_LINE_RE.match(lines[i])
        if match is None:
            break
        front_matter[match.group(1)] = match.group(2).strip()
        i += 1
    if i > 0 and i < len(lines) and lines[i].strip() == "":
        i += 1
    body = "\n".join(lines[i:])
    intervene = front_matter.get("intervene", "deny")
    if intervene not in _INTERVENE_VALUES:
        intervene = "deny"
    return intervene, body


def _build_fields(payload: Mapping[str, Any], argument: str) -> dict[str, str]:
    """Return the ``{placeholder}`` substitution table: payload top-level fields plus ``argument``.

    A non-string field value is rendered as compact JSON rather than
    dropped, so a template can still reference e.g. ``{tool_input}``.
    """
    fields: dict[str, str] = {}
    for key, value in payload.items():
        fields[key] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    fields["argument"] = argument
    return fields


def _render(body: str, fields: Mapping[str, str]) -> str:
    """Substitute every ``{placeholder}`` found in ``fields`` into ``body``, rest kept literal."""

    def _substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        return fields[key] if key in fields else match.group(0)

    return _PLACEHOLDER_RE.sub(_substitute, body)


def _pretooluse_output(
    intervene_mode: str, reason: str, mutation: dict[str, Any] | None
) -> dict[str, Any]:
    """Build the ``PreToolUse`` ``hookSpecificOutput`` for an ``intervene`` verdict."""
    hook_specific: dict[str, Any] = {"hookEventName": "PreToolUse"}
    if intervene_mode == "context":
        hook_specific["additionalContext"] = reason
    else:
        hook_specific["permissionDecision"] = "ask" if intervene_mode == "ask" else "deny"
        hook_specific["permissionDecisionReason"] = reason
    if mutation is not None:
        hook_specific["updatedInput"] = mutation
    return {"hookSpecificOutput": hook_specific}


def _map_verdict(
    hook_event: object, intervene_mode: str, reason: str, mutation: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Map an ``intervene`` verdict to the hook output shape for ``hook_event``.

    Returns ``None`` for an unrecognised (or missing) ``hook_event_name``,
    which the caller turns into a silent exit 0. ``mutation`` is honoured
    only at ``PreToolUse``, which has an ``updatedInput`` slot for it; it is
    dropped everywhere else, per ``docs/DESIGN.md``.
    """
    if hook_event == "PreToolUse":
        return _pretooluse_output(intervene_mode, reason, mutation)
    if hook_event in _DECISION_BLOCK_EVENTS:
        return {"decision": "block", "reason": reason}
    return None


def run_fork_review(
    stdin: TextIO,
    stdout: TextIO,
    environ: Mapping[str, str],
    template: str,
    prefix: str | None = None,
    model: str | None = None,
) -> int:
    """Read a hook event from ``stdin``, review it via a shadow fork, and print the mapped verdict.

    Always returns 0. Unparseable/non-object stdin, a missing or empty
    ``session_id``, and a ``--prefix`` that does not match the payload's
    ``prompt`` all exit silently (empty stdout) -- these are "this hook does
    not apply here", not failures. Every failure past that point (missing
    template, unreachable daemon, a non-2xx API response, or any other
    exception) is fail-open: no decision fields, and a ``systemMessage``
    (unless ``CGX_FORK_REVIEW_QUIET`` is set) naming the reason. An ``ok``
    verdict from the daemon is also silent -- only ``intervene`` produces
    output. ``model`` overrides the reviewer model for this fork only,
    always forfeiting the cache read; omitted, the daemon falls back to
    ``CGX_FORK_MODEL`` then the session's own model.
    """
    raw = stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return 0

    argument = ""
    if prefix is not None:
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            return 0
        stripped = prompt.strip()
        if not stripped.startswith(prefix):
            return 0
        rest = stripped[len(prefix) :]
        if rest and not rest[0].isspace():
            return 0
        argument = rest.strip()

    def _skip(reason: str) -> int:
        if not _env_flag(environ, "CGX_FORK_REVIEW_QUIET"):
            stdout.write(json.dumps({"systemMessage": f"cgx fork-review skipped: {reason}"}))
            stdout.write("\n")
        return 0

    try:
        template_text = _load_template(template, environ)
    except _TemplateError as exc:
        return _skip(str(exc))

    intervene_mode, body = _parse_template(template_text)
    fields = _build_fields(payload, argument)
    rendered_prompt = _render(body, fields)

    hook_event = payload.get("hook_event_name")
    config = Config.from_env(environ)
    client = ControlClient(config.url)
    try:
        result = client.fork(
            session_id, rendered_prompt, hook_event, payload, config.fork_max_tokens, model
        )
    except DaemonUnreachable as exc:
        return _skip(f"daemon not reachable ({exc})")
    except ApiError as exc:
        return _skip(f"daemon error ({exc.status})")
    except Exception as exc:
        return _skip(f"unexpected error ({exc})")

    if not isinstance(result, dict) or result.get("verdict") != "intervene":
        return 0

    reason = result.get("reason")
    if not isinstance(reason, str):
        reason = ""
    mutation = result.get("mutation")
    if not isinstance(mutation, dict):
        mutation = None

    output = _map_verdict(hook_event, intervene_mode, reason, mutation)
    if output is None:
        return 0
    stdout.write(json.dumps(output))
    stdout.write("\n")
    return 0


__all__ = ["run_fork_review"]
