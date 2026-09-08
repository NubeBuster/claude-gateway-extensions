"""Thin ``urllib``-based HTTP client for the daemon's control API.

Deliberately avoids ``aiohttp`` (or any event loop) so every CLI subcommand,
the ``UserPromptSubmit`` hook and the statusline segment start instantly.
``docs/API.md`` is the authoritative request/response contract; this module
encodes exactly those shapes, keyed to
:class:`~claude_cache_bump_gateway.core.scheduler.Scheduler`'s ``status()``
and ``status_all()`` output and to
:class:`~claude_cache_bump_gateway.core.bump.BumpResult`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

#: Default per-request timeout, in seconds, for every :class:`ControlClient` call.
DEFAULT_TIMEOUT_S = 3.0

#: A factory that builds a control-API client for a base URL. Typed loosely
#: (``Any`` return) rather than pinned to :class:`ControlClient` so tests can
#: inject a minimal fake that implements only the methods the command under
#: test actually calls, instead of the whole client surface.
ClientFactory = Callable[[str], Any]


class DaemonUnreachable(Exception):
    """Raised when the daemon cannot be reached at all (connection refused, timeout, DNS)."""


class ApiError(Exception):
    """Raised when the daemon responds with a non-2xx status (other than a bump's 409)."""

    def __init__(self, status: int, body: str) -> None:
        """Record the HTTP ``status`` and raw response ``body`` for the caller to inspect."""
        super().__init__(f"control API error: HTTP {status}: {body}")
        self.status = status
        self.body = body


class ControlClient:
    """Client for the daemon's ``/ccbg/`` control API.

    Every method blocks synchronously via ``urllib`` and raises
    :class:`DaemonUnreachable` on a connection failure or :class:`ApiError` on
    a non-2xx response, except that :meth:`bump` returns the parsed body for
    a ``409`` response instead of raising (a bump that could not proceed,
    for example because the session has no snapshot yet, is a normal, not an
    exceptional, outcome).
    """

    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        """Create a client for the daemon at ``base_url`` (e.g. ``http://127.0.0.1:8787``)."""
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        """Perform one HTTP request and return ``(status, parsed_json_body)``.

        Raises :class:`DaemonUnreachable` on any connection-level failure. A
        non-2xx HTTP response is *not* an error at this layer: it is
        returned to the caller as ``(status, parsed_body)`` so per-endpoint
        methods can decide what counts as an error.
        """
        url = f"{self._base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"content-type": "application/json"} if data is not None else {}
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise DaemonUnreachable(f"cannot reach daemon at {self._base_url}: {exc}") from exc
        parsed: Any = None
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw.decode("utf-8", errors="replace")
        return status, parsed

    def _get(self, path: str) -> Any:
        """``GET`` ``path``, raising :class:`ApiError` on a non-2xx response."""
        status, parsed = self._request("GET", path)
        if not 200 <= status < 300:
            raise ApiError(status, str(parsed))
        return parsed

    def health(self) -> dict[str, Any]:
        """Return the daemon's ``GET /ccbg/health`` response."""
        result: dict[str, Any] = self._get("/ccbg/health")
        return result

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return every session's status via ``GET /ccbg/sessions``.

        The route answers with a ``{"sessions": [...]}`` envelope (see
        ``docs/API.md``); the envelope is unwrapped here so callers only ever
        deal with a list of status dicts.
        """
        payload: Any = self._get("/ccbg/sessions")
        sessions = payload.get("sessions") if isinstance(payload, dict) else payload
        if not isinstance(sessions, list):
            raise ApiError(200, f"malformed /ccbg/sessions response: {payload!r}")
        result: list[dict[str, Any]] = sessions
        return result

    def get_session(self, session_id: str) -> dict[str, Any]:
        """Return one session's status via ``GET /ccbg/sessions/{id}``.

        Raises :class:`ApiError` with ``status == 404`` if the session is
        unknown to the daemon.
        """
        result: dict[str, Any] = self._get(f"/ccbg/sessions/{session_id}")
        return result

    def set_session(
        self,
        session_id: str,
        enabled: bool,
        interval_s: int | None = None,
        max_duration_s: int | None = None,
        until: float | None = None,
    ) -> dict[str, Any]:
        """Enable/disable bumping and optionally set overrides via ``PUT /ccbg/sessions/{id}``.

        ``until`` is an absolute Unix epoch deadline that replaces the
        max-duration stop for this session; see ``docs/API.md``.
        """
        payload: dict[str, Any] = {"enabled": enabled}
        if interval_s is not None:
            payload["interval_s"] = interval_s
        if max_duration_s is not None:
            payload["max_duration_s"] = max_duration_s
        if until is not None:
            payload["until"] = until
        status, parsed = self._request("PUT", f"/ccbg/sessions/{session_id}", payload)
        if not 200 <= status < 300:
            raise ApiError(status, str(parsed))
        return parsed

    def bump(self, session_id: str) -> dict[str, Any]:
        """Trigger an immediate bump via ``POST /ccbg/sessions/{id}/bump``.

        Returns the parsed response body for a 2xx result (``outcome: "ok"``),
        a 409 (a ``BumpResult`` with any other outcome) and a 404 (the session
        has no snapshot to replay, ``{"error": "no_snapshot"}``) — per
        ``docs/API.md``, all three are normal, non-exceptional outcomes of
        asking for a bump. Any other status raises :class:`ApiError`.
        """
        status, parsed = self._request("POST", f"/ccbg/sessions/{session_id}/bump")
        if status in (404, 409) or 200 <= status < 300:
            return parsed
        raise ApiError(status, str(parsed))

    def forget(self, session_id: str) -> None:
        """Discard a session's snapshots and state via ``DELETE /ccbg/sessions/{id}``."""
        status, parsed = self._request("DELETE", f"/ccbg/sessions/{session_id}")
        if not 200 <= status < 300:
            raise ApiError(status, str(parsed))


__all__ = ["ApiError", "ClientFactory", "ControlClient", "DaemonUnreachable"]
