"""Building and classifying a shadow review fork of a captured request.

A fork replays a session's captured snapshot once with a single extra user
turn appended, asking a reviewer model a question about the parent session
at the cost of a cache read plus the twin's own output tokens. See the
"Shadow review fork" section in ``docs/DESIGN.md`` for the full design.

:func:`build_fork_body` turns a snapshot body plus a review prompt/payload
into the exact request to replay. :func:`parse_verdict` turns the response
into a :class:`ForkVerdict`, failing open on anything it cannot trust.
:func:`fork_once` ties transport and both of the above together for the
scheduler.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from claude_gateway_extensions.core.bump import build_bump_headers
from claude_gateway_extensions.core.snapshot import Snapshot, Usage
from claude_gateway_extensions.core.upstream import Upstream

logger = logging.getLogger(__name__)

ForkOutcome = Literal["ok", "intervene"]

_FORK_PATH = "/v1/messages"

#: Minimum (family, version) known to preserve signed thinking blocks when a
#: non-tool-result user turn is appended -- measured behaviour, not a guess.
#: Lowering an entry needs a new measurement, not a tidy-up; see the "Shadow
#: review fork" section of docs/DESIGN.md. No Haiku model preserves them at
#: any version, so ``haiku`` is deliberately absent rather than ``(0, 0)``.
_THINKING_SAFE_MINIMUM: dict[str, tuple[int, ...]] = {
    "opus": (4, 5),
    "sonnet": (4, 6),
    "fable": (5, 1),
}

#: An 8-digit release date in a model id (``claude-opus-4-5-20251101``).
#: Never a version component, and it would otherwise dominate the comparison.
_DATE_RE = re.compile(r"^\d{8}$")


def _thinking_budget(data: Mapping[str, object]) -> int | None:
    """Return the captured body's positive ``thinking.budget_tokens`` if thinking is enabled."""
    thinking = data.get("thinking")
    if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
        return None
    budget = thinking.get("budget_tokens")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        return None
    return budget


def build_fork_body(
    body: bytes, prompt: str, payload: object, max_tokens: int, model: str | None = None
) -> bytes:
    """Return ``body`` re-serialised as a one-shot review fork request.

    ``model`` defaults to ``None``, which leaves the captured body's
    ``model`` field untouched -- the invariant this default exists to
    preserve is that a fork runs on the reviewed session's own model,
    since Anthropic namespaces the prompt cache per model and a fork on
    any other model cannot read the parent's cached prefix. Passing a
    ``model`` overrides it, an explicit choice that always forfeits the
    cache read.

    ``max_tokens`` is forced to ``max_tokens``, plus the captured body's
    ``thinking.budget_tokens`` when extended thinking is enabled there: the
    API requires ``max_tokens`` strictly greater than the thinking budget,
    and the captured ``thinking`` block itself is replayed untouched (it is
    part of the prefix-affecting parameter set). ``max_tokens`` still bounds
    only the reviewer's visible answer; the budget is headroom the API
    demands on top of it. ``stream`` is set to ``false`` only if the key is
    already present, exactly as :func:`~.bump.build_bump_body` treats it.
    ``tools`` and ``tool_choice`` are preserved by value, not dropped:
    Anthropic hashes the cache prefix over ``tools``, then ``system``, then
    ``messages``, so removing either key invalidates the whole prefix and
    turns every fork into a full ``cache_creation`` write. One message is
    appended: ``prompt`` followed by ``payload`` pretty-printed as a fenced
    JSON block. Its role is ``user``, unless the captured body's last
    message is itself ``system`` -- the API requires an inline ``system``
    message to precede an assistant turn or end the array, so a ``user``
    turn appended behind one is a hard 400; extending the run with another
    ``system`` message satisfies the same rule Claude Code's own trailing
    system runs already do. The appended message never carries a
    ``cache_control`` of its own: it always lands after every breakpoint
    the captured body already has, so a marker there buys nothing. A twin
    must still never act on the parent's behalf, so that guarantee now
    comes from the prompt instructing the reviewer never to call a tool,
    plus :func:`parse_verdict` treating a ``tool_use`` reply as fail-open.
    Everything else, including every nested ``cache_control`` block, is
    preserved by value exactly as :func:`~.bump.build_bump_body` preserves
    it, so the fork hits the same cached prefix.
    """
    data = json.loads(body)
    if model is not None:
        data["model"] = model
    budget = _thinking_budget(data)
    data["max_tokens"] = max_tokens + budget if budget is not None else max_tokens
    if "stream" in data:
        data["stream"] = False
    payload_text = json.dumps(payload, indent=2, ensure_ascii=False)
    content = f"{prompt}\n\n```json\n{payload_text}\n```"
    messages = data.setdefault("messages", [])
    last = messages[-1] if messages else None
    role = "system" if isinstance(last, dict) and last.get("role") == "system" else "user"
    messages.append({"role": role, "content": content})
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _model_family(model: str | None) -> str | None:
    """``opus``/``sonnet``/``haiku``/``fable`` for a model id, or ``None``.

    ``None`` means "not a family this module knows", never "no family": a
    family recognised here is a claim that its thinking-preservation
    behaviour has been measured. An unrecognised model (e.g. a future
    family) must fall through to the unknown path, not to safety.
    """
    lowered = (model or "").lower()
    for family in ("opus", "sonnet", "haiku", "fable"):
        if family in lowered:
            return family
    return None


def _leading_numbers(tokens: list[str]) -> tuple[int, ...]:
    """The run of non-date integers at the head of ``tokens``."""
    numbers: list[int] = []
    for token in tokens:
        if not token.isdigit() or _DATE_RE.match(token):
            break
        numbers.append(int(token))
    return tuple(numbers)


def _model_version(model: str | None, family: str) -> tuple[int, ...] | None:
    """The version numbers around ``family`` in a model id, or ``None``.

    Both orderings Claude Code has been seen sending are read:
    ``claude-opus-4-5-20251101`` puts the version after the family and
    ``claude-3-7-sonnet-20250219`` puts it before. Numbers after the family
    win when both are present -- the newer spelling -- and an 8-digit
    release date is never a version component.
    """
    tokens = [t for t in re.split(r"[^0-9a-z]+", (model or "").lower()) if t]
    try:
        index = tokens.index(family)
    except ValueError:
        return None
    after = _leading_numbers(tokens[index + 1 :])
    if after:
        return after
    before = _leading_numbers(list(reversed(tokens[:index])))
    return tuple(reversed(before)) if before else None


def _pad(version: tuple[int, ...], length: int) -> tuple[int, ...]:
    """``version`` zero-extended to ``length``, so ``(5,) >= (4, 6)`` compares."""
    return tuple(list(version) + [0] * length)[:length]


def _preserves_thinking_blocks(model: str | None) -> bool | None:
    """Whether appending a user turn to ``model`` keeps its signed thinking blocks.

    Three-valued, and the third value is the point: ``None`` means the
    model id could not be placed in a known family/version at all, which is
    neither a licence to spend nor evidence of harm -- the caller refuses on
    it under its own reason code rather than treating unknown as safe.
    """
    family = _model_family(model)
    if family is None:
        return None
    if family == "haiku":
        # Every Haiku model invalidates, at every version -- a decision,
        # not a lookup, and must not fall through to the table.
        return False
    version = _model_version(model, family)
    if version is None:
        return None
    minimum = _THINKING_SAFE_MINIMUM.get(family)
    if minimum is None:
        return None
    return _pad(version, len(minimum)) >= minimum


def _body_has_thinking_blocks(data: object) -> bool:
    """Whether a parsed captured body carries an assistant thinking block."""
    if not isinstance(data, dict):
        return False
    messages = data.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") in (
                "thinking",
                "redacted_thinking",
            ):
                return True
    return False


def _check_thinking_safety(body: bytes, model: str | None) -> ForkVerdict | None:
    """Return a fail-open refusal if replaying ``body`` on ``model`` would strip thinking.

    ``None`` means proceed: either the body carries no thinking blocks (so
    there is nothing for an invalidating model to strip and refusing would
    rule out the whole non-thinking half of the traffic for no gain), the
    body could not be parsed (``build_fork_body`` will raise its own error
    on the same body), or the model is known to preserve them.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not _body_has_thinking_blocks(data):
        return None
    verdict = _preserves_thinking_blocks(model)
    if verdict is True:
        return None
    if verdict is None:
        logger.warning("fork refused: reason=thinking_model_unknown model=%s", model)
        return _fail_open("thinking_model_unknown")
    logger.warning("fork refused: reason=thinking_unsafe_model model=%s", model)
    return _fail_open("thinking_unsafe_model")


@dataclass
class ForkVerdict:
    """The outcome of one shadow review fork.

    ``model`` and ``stream_key`` are populated once a snapshot has been
    selected and a request built for it: ``model`` is whichever model the
    request was actually sent to (the snapshot's own by default, or the
    override when one is in effect) and ``stream_key`` is the snapshot's.
    Both are ``None`` for outcomes reached before a snapshot is known
    (``no_snapshot``). ``subagent_snapshot`` is ``True`` when
    ``Scheduler.fork_now`` had no snapshot known *not* to be subagent
    traffic and fell back to selecting over every snapshot for the
    session -- flagged, never refused, since a fork is one-shot spend.
    """

    verdict: Literal["ok", "intervene"]
    reason: str
    mutation: dict[str, object] | None
    usage: Usage | None
    paid_write: bool
    model: str | None = None
    stream_key: str | None = None
    subagent_snapshot: bool = False


def _fail_open(
    reason: str,
    usage: Usage | None = None,
    model: str | None = None,
    stream_key: str | None = None,
) -> ForkVerdict:
    """Return the fail-open ``ok`` verdict every unparseable/failed reply falls back to."""
    paid_write = usage is not None and usage.cache_creation_input_tokens > 0
    return ForkVerdict(
        verdict="ok",
        reason=reason,
        mutation=None,
        usage=usage,
        paid_write=paid_write,
        model=model,
        stream_key=stream_key,
    )


def _extract_usage(data: object) -> Usage | None:
    """Return a :class:`Usage` read from a response dict's ``usage`` block, if any."""
    if not isinstance(data, dict):
        return None
    raw_usage = data.get("usage")
    if not isinstance(raw_usage, dict):
        return None
    return Usage.from_dict(raw_usage)


def _has_tool_use(data: Mapping[str, object]) -> bool:
    """Return whether ``content`` carries any block of type ``tool_use``."""
    content = data.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content)


def _extract_text(data: Mapping[str, object]) -> str | None:
    """Concatenate every ``type: text`` content block's ``text``, or ``None`` if unreadable."""
    content = data.get("content")
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if not isinstance(text, str):
                return None
            parts.append(text)
    return "".join(parts)


def parse_verdict(
    status: int, body: bytes, model: str | None = None, stream_key: str | None = None
) -> ForkVerdict:
    """Classify a fork response, failing open on anything unconfirmed.

    Anything at all unexpected -- a non-200 status, a body that cannot be
    parsed as the reply's JSON, text content blocks that cannot be
    concatenated, a verdict payload that is not strict JSON, or one whose
    ``verdict``/``reason``/``mutation`` fields do not match the fixed
    contract -- yields the fail-open ``ok`` verdict rather than raising or
    blocking the caller. This fail-open direction is an invariant: a broken
    template or a chatty model must never block the user's work. ``model``
    and ``stream_key`` are carried onto every returned verdict verbatim,
    for a caller reporting which model actually answered.
    """
    if status != 200:
        logger.warning("fork request failed: status=%s", status)
        return _fail_open("fork_failed", model=model, stream_key=stream_key)

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("fork verdict unparseable: reason=unparseable_verdict")
        return _fail_open("unparseable_verdict", model=model, stream_key=stream_key)

    usage = _extract_usage(data)

    if not isinstance(data, dict):
        logger.warning("fork verdict unparseable: reason=unparseable_verdict")
        return _fail_open("unparseable_verdict", usage, model, stream_key)

    if _has_tool_use(data):
        logger.warning("fork verdict rejected: reason=tool_use_reply")
        return _fail_open("tool_use_reply", usage, model, stream_key)

    text = _extract_text(data)
    if text is None:
        logger.warning("fork verdict unparseable: reason=unparseable_verdict")
        return _fail_open("unparseable_verdict", usage, model, stream_key)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("fork verdict unparseable: reason=unparseable_verdict")
        return _fail_open("unparseable_verdict", usage, model, stream_key)

    if not isinstance(parsed, dict):
        logger.warning("fork verdict unparseable: reason=unparseable_verdict")
        return _fail_open("unparseable_verdict", usage, model, stream_key)

    verdict = parsed.get("verdict")
    reason = parsed.get("reason")
    mutation = parsed.get("mutation")

    if verdict not in ("ok", "intervene"):
        logger.warning("fork verdict unparseable: reason=unparseable_verdict")
        return _fail_open("unparseable_verdict", usage, model, stream_key)
    if not isinstance(reason, str):
        logger.warning("fork verdict unparseable: reason=unparseable_verdict")
        return _fail_open("unparseable_verdict", usage, model, stream_key)
    if mutation is not None and not isinstance(mutation, dict):
        logger.warning("fork verdict unparseable: reason=unparseable_verdict")
        return _fail_open("unparseable_verdict", usage, model, stream_key)

    paid_write = usage is not None and usage.cache_creation_input_tokens > 0
    return ForkVerdict(
        verdict=verdict,
        reason=reason,
        mutation=mutation,
        usage=usage,
        paid_write=paid_write,
        model=model,
        stream_key=stream_key,
    )


async def fork_once(
    upstream: Upstream,
    upstream_base: str,
    snapshot: Snapshot,
    credential: tuple[str, str] | None,
    prompt: str,
    payload: object,
    max_tokens: int,
    thinking_guard: bool = True,
    model: str | None = None,
) -> ForkVerdict:
    """Replay ``snapshot`` as a review fork against ``upstream_base`` and classify the reply.

    ``model`` overrides the model the fork is sent to; ``None`` (the
    default) sends it to the snapshot's own model, which is the only
    choice that can read the parent's cached prefix -- Anthropic
    namespaces the prompt cache per model, so any override always pays a
    full cache write. An override in effect is logged once per fork at
    WARNING with that reason. Every returned verdict carries the model
    actually used and the snapshot's ``stream_key``.

    When ``thinking_guard`` is true (``CGX_FORK_THINKING_GUARD``, default
    on) and the captured snapshot carries thinking blocks, the effective
    model (the override, if any, else the snapshot's own) is checked
    against the measured thinking-preservation table before anything is
    sent: a model below its family's minimum, in a family with no entry
    (every Haiku model), or unrecognised entirely refuses with the
    existing fail-open shape rather than paying a full cold write of the
    conversation. Network exceptions are caught and turned into the
    fail-open ``ok`` verdict so callers never need a separate exception
    path.
    """
    effective_model = model if model is not None else snapshot.model
    if model is not None and model != snapshot.model:
        logger.warning(
            "fork model overridden: session=%s snapshot_model=%s override_model=%s "
            "reason=cache_read_forfeited",
            snapshot.session_id,
            snapshot.model,
            model,
        )
    if thinking_guard:
        refusal = _check_thinking_safety(snapshot.body, effective_model)
        if refusal is not None:
            return dataclasses.replace(
                refusal, model=effective_model, stream_key=snapshot.stream_key
            )
    url = f"{upstream_base.rstrip('/')}{_FORK_PATH}"
    headers = build_bump_headers(snapshot.headers, credential)
    body = build_fork_body(snapshot.body, prompt, payload, max_tokens, model)
    try:
        response = await upstream.send("POST", url, headers, body)
    except Exception:
        logger.warning("fork request failed: reason=fork_failed")
        return _fail_open("fork_failed", model=effective_model, stream_key=snapshot.stream_key)
    return parse_verdict(response.status, response.body, effective_model, snapshot.stream_key)
