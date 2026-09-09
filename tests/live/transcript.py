"""Reading a live subject's own transcript instead of its terminal.

Claude Code appends one JSON object per line to
``~/.claude/projects/<slug>/<session-id>.jsonl`` as a session runs. That
file is the subject's own record of what happened -- the assistant turns,
their ``stop_reason`` and their token usage -- so it answers "did the turn
finish" and "what did this cost" exactly, where `capture-pane` answers only
"what is currently rendered".
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class TranscriptError(RuntimeError):
    """The subject's transcript could not be found or did not progress."""


def project_slug(cwd: Path) -> str:
    """Return the ``~/.claude/projects`` directory name Claude Code uses for ``cwd``.

    The mapping is the absolute path with separators replaced by hyphens, so
    ``/tmp/scripts/cgx-live/subject`` becomes
    ``-tmp-scripts-cgx-live-subject``.
    """
    return str(cwd).replace("/", "-")


def project_dir(home: Path, cwd: Path) -> Path:
    """Return the transcript directory for ``cwd`` under ``home``."""
    return home / ".claude" / "projects" / project_slug(cwd)


@dataclass
class Transcript:
    """The transcript files Claude Code writes for one scratch cwd.

    A run creates exactly one session in its own scratch directory, so the
    newest ``.jsonl`` there is that session's transcript. The file is
    resolved lazily rather than at construction: it does not exist until the
    subject's first turn.
    """

    directory: Path

    def path(self) -> Path | None:
        """Return the newest transcript file, or None if none exists yet."""
        if not self.directory.is_dir():
            return None
        files = sorted(self.directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        return files[-1] if files else None

    def entries(self) -> list[dict[str, object]]:
        """Return every parsed entry, skipping a torn final line.

        The subject writes to this file while it is being read, so the last
        line can be incomplete; that is normal and is not an error.
        """
        path = self.path()
        if path is None:
            return []
        parsed: list[dict[str, object]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                parsed.append(entry)
        return parsed

    def session_id(self) -> str | None:
        """Return the session id the transcript records, if it has one yet."""
        for entry in self.entries():
            value = entry.get("sessionId")
            if isinstance(value, str) and value:
                return value
        return None

    def completed_turns(self) -> list[dict[str, object]]:
        """Return the assistant entries that ended a turn, in file order.

        A turn is over when the assistant stops for a reason other than
        wanting a tool result: ``end_turn``, ``max_tokens`` or ``stop_sequence``.
        Intermediate ``tool_use`` stops are steps within one turn.
        """
        finished = []
        for entry in self.entries():
            if entry.get("type") != "assistant":
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            if message.get("stop_reason") in {"end_turn", "max_tokens", "stop_sequence"}:
                finished.append(entry)
        return finished

    def reply_text(self, entry: Mapping[str, object]) -> str:
        """Return the concatenated text blocks of one assistant entry."""
        message = entry.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if not isinstance(content, list):
            return ""
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )

    def usage(self, entry: Mapping[str, object]) -> dict[str, object]:
        """Return one assistant entry's usage block, empty when absent."""
        message = entry.get("message")
        if not isinstance(message, dict):
            return {}
        usage = message.get("usage")
        return usage if isinstance(usage, dict) else {}

    def model(self, entry: Mapping[str, object]) -> str | None:
        """Return the model an assistant entry was answered by, if recorded."""
        message = entry.get("message")
        if not isinstance(message, dict):
            return None
        model = message.get("model")
        return model if isinstance(model, str) else None

    def wait_for_turns(
        self,
        count: int,
        timeout_s: float,
        poll_s: float = 1.0,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ) -> list[dict[str, object]]:
        """Block until at least ``count`` turns have completed, then return them.

        ``sleep`` and ``monotonic`` are injected so the offline tests can
        drive this loop without real time passing.
        """
        deadline = monotonic() + timeout_s
        while True:
            finished = self.completed_turns()
            if len(finished) >= count:
                return finished
            if monotonic() >= deadline:
                raise TranscriptError(
                    f"only {len(finished)} of {count} turns completed within {timeout_s:.0f}s "
                    f"(transcript: {self.path() or self.directory})"
                )
            sleep(poll_s)


__all__ = ["Transcript", "TranscriptError", "project_dir", "project_slug"]
