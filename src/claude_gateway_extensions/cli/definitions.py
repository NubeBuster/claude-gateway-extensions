"""Parse a Shadow Fork definition: a Markdown file with ``---``-delimited front matter.

Per the "Definitions are Markdown files" decision in ``docs/DESIGN.md``, a
definition's front matter follows the `Claude Code subagent front-matter
convention <https://code.claude.com/docs/en/sub-agents#supported-frontmatter-fields>`_
(``name``, ``description``, ``model``, ``tools``) plus the fields this
project's hooks need (``event``, ``matcher``, ``require``, ``intervene``).

This module owns only the pure parse: reading the file and mapping IO
failures to :class:`~claude_gateway_extensions.cli.shadow_fork.TemplateError`
is :mod:`claude_gateway_extensions.cli.shadow_fork`'s job, not this one's --
:func:`parse_definition` never touches a filesystem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Hook events whose output uses the top-level ``decision: "block"`` shape.
#: These events have no ``ask`` decision of their own in the hook contract
#: (unlike ``PreToolUse``'s ``permissionDecision``), so a definition that
#: declares one of these as its ``event:`` and asks for ``intervene: ask``
#: is rejected at parse time -- see the "ask is rejected for a declared
#: non-PreToolUse event" decision in ``docs/DESIGN.md``.
DECISION_BLOCK_EVENTS = frozenset({"UserPromptSubmit", "PostToolUse"})

#: Every event a definition's ``event:`` front-matter line may declare.
MAPPABLE_EVENTS = frozenset({"PreToolUse", *DECISION_BLOCK_EVENTS})

#: The ``intervene:`` front-matter key's allowed values.
INTERVENE_VALUES = frozenset({"deny", "ask", "context"})

#: The closed set of front-matter keys a definition may declare. ``name``,
#: ``description`` and ``model`` are the Claude Code subagent fields this
#: parser accepts; ``tools`` is also a subagent field but is always rejected
#: (see :func:`parse_definition`); ``event``, ``matcher``, ``require`` and
#: ``intervene`` are the fields this project's hooks need.
_KNOWN_KEYS = frozenset(
    {"name", "description", "model", "tools", "event", "matcher", "require", "intervene"}
)

#: Keys whose value must be a scalar string, never a list.
_SCALAR_KEYS = frozenset({"name", "description", "model", "event", "matcher", "intervene"})

#: Keys whose value must be a list, never a scalar.
_LIST_KEYS = frozenset({"require"})

#: Matches a front-matter key, e.g. the ``intervene`` in ``intervene: ask``.
_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(.*)$")

#: Matches one block-list item line, e.g. ``  - tool_input.owner``.
_LIST_ITEM_RE = re.compile(r"^  - (.*)$")


class DefinitionError(Exception):
    """Raised when a definition's front matter cannot be parsed or fails validation."""


@dataclass(frozen=True)
class Definition:
    """One parsed Shadow Fork definition: its front-matter fields plus body."""

    name: str
    description: str
    model: str | None
    event: str | None
    matcher: str | None
    require: list[str] | None
    intervene: str
    body: str


def _strip_quotes(raw: str) -> str:
    """Strip matching leading/trailing quote characters from ``raw``, verbatim, no escapes."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    return raw


def _parse_front_matter(lines: list[str]) -> dict[str, str | list[str]]:
    """Parse the front-matter ``lines`` (between the two ``---`` delimiters).

    Returns a mapping of each declared key to its scalar string or list-of-
    strings value, validated against :data:`_KNOWN_KEYS`, :data:`_SCALAR_KEYS`
    and :data:`_LIST_KEYS`, with no duplicate keys. Raises
    :class:`DefinitionError` naming the offending line on anything outside
    the accepted flat scalar/list subset: a nested mapping, a flow-style
    collection, a multi-line scalar, an inline (not full-line) comment, a
    list item with no preceding bare ``key:``, a duplicate key, an unknown
    key, or a key given the wrong shape for what it accepts.
    """
    front_matter: dict[str, str | list[str]] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            i += 1
            continue
        if _LIST_ITEM_RE.match(line) is not None:
            raise DefinitionError(f"list item with no preceding key: {line!r}")
        match = _KEY_RE.match(line)
        if match is None:
            raise DefinitionError(f"malformed front matter line: {line!r}")
        key, rest = match.group(1), match.group(2).strip()
        if key not in _KNOWN_KEYS:
            raise DefinitionError(f"unknown front matter key {key!r}")
        if key in front_matter:
            raise DefinitionError(f"duplicate front matter key {key!r}")
        if key == "tools":
            raise DefinitionError(
                "`tools:` is rejected -- a Shadow Fork never calls a tool, "
                "so the key can never mean anything here"
            )
        if rest == "":
            # A bare `key:` either opens a block list or is an empty scalar.
            items: list[str] = []
            j = i + 1
            while j < len(lines) and _LIST_ITEM_RE.match(lines[j]) is not None:
                item_match = _LIST_ITEM_RE.match(lines[j])
                assert item_match is not None
                items.append(_strip_quotes(item_match.group(1).strip()))
                j += 1
            if items:
                if key not in _LIST_KEYS:
                    raise DefinitionError(f"`{key}:` must be a scalar value, not a list")
                front_matter[key] = items
                i = j
                continue
            if key in _LIST_KEYS:
                raise DefinitionError(f"`{key}:` list has no items")
            front_matter[key] = ""
            i += 1
            continue
        if key in _LIST_KEYS:
            raise DefinitionError(f"`{key}:` must be a list value, not a scalar")
        front_matter[key] = _strip_quotes(rest)
        i += 1
    return front_matter


def parse_definition(text: str, *, default_name: str) -> Definition:
    """Parse ``text`` (a whole definition file's contents) into a :class:`Definition`.

    ``text`` must start with a ``---`` line and the front matter must end
    with another ``---`` line on its own; a missing opening or closing
    delimiter is a :class:`DefinitionError`. A single blank line
    immediately after the closing delimiter is consumed as a separator (for
    readability) and is not part of the body; everything else after the
    closing delimiter is the body verbatim.

    ``description`` is required. ``name`` defaults to ``default_name`` (the
    caller's file stem). ``intervene`` defaults to ``"deny"``. An
    unrecognised ``event`` or ``intervene`` value is a :class:`DefinitionError`
    naming it -- unlike the old ``.txt`` format, there is no silent fallback
    once front matter (and therefore ``description``) is mandatory.
    ``intervene: ask`` combined with a declared ``event:`` in
    :data:`DECISION_BLOCK_EVENTS` is also a :class:`DefinitionError`: those
    events have no ``ask`` decision of their own, so accepting it here would
    let a definition silently degrade to ``deny`` at hook time instead of
    telling its author up front. A definition with no declared ``event:``
    cannot be checked this way -- it is still accepted, and behaves as
    ``deny`` at runtime when the hook that ends up calling it fires for one
    of these events (see ``cli/shadow_fork.py::_map_verdict``).
    """
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        raise DefinitionError("missing front matter delimiter: file must start with `---`")
    try:
        closing = next(i for i in range(1, len(lines)) if lines[i].rstrip() == "---")
    except StopIteration as exc:
        raise DefinitionError(
            "unterminated front matter: no closing `---` delimiter found"
        ) from exc

    front_matter = _parse_front_matter(lines[1:closing])

    body_lines = lines[closing + 1 :]
    if body_lines and body_lines[0].strip() == "":
        body_lines = body_lines[1:]
    body = "\n".join(body_lines)

    description = front_matter.get("description")
    if not isinstance(description, str) or description == "":
        raise DefinitionError("`description:` is required")

    event = front_matter.get("event")
    assert event is None or isinstance(event, str)
    if event is not None and event not in MAPPABLE_EVENTS:
        raise DefinitionError(
            f"unrecognised `event:` value {event!r}; expected one of {sorted(MAPPABLE_EVENTS)}"
        )

    intervene = front_matter.get("intervene", "deny")
    assert isinstance(intervene, str)
    if intervene not in INTERVENE_VALUES:
        raise DefinitionError(
            f"unrecognised `intervene:` value {intervene!r}; "
            f"expected one of {sorted(INTERVENE_VALUES)}"
        )
    if intervene == "ask" and event in DECISION_BLOCK_EVENTS:
        raise DefinitionError(
            f"`intervene: ask` is rejected for `event: {event}` -- "
            f"{sorted(DECISION_BLOCK_EVENTS)} have no `ask` decision of their "
            "own in the hook contract; use `deny` or `context` instead"
        )

    name = front_matter.get("name") or default_name
    assert isinstance(name, str)
    model = front_matter.get("model") or None
    assert model is None or isinstance(model, str)
    matcher = front_matter.get("matcher") or None
    assert matcher is None or isinstance(matcher, str)
    require = front_matter.get("require")
    assert require is None or isinstance(require, list)

    return Definition(
        name=name,
        description=description,
        model=model,
        event=event,
        matcher=matcher,
        require=require,
        intervene=intervene,
        body=body,
    )


__all__ = [
    "DECISION_BLOCK_EVENTS",
    "INTERVENE_VALUES",
    "MAPPABLE_EVENTS",
    "Definition",
    "DefinitionError",
    "parse_definition",
]
