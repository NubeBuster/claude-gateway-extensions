# Control API

Served by the same listener as the proxy itself, under `/cgx/`. JSON only,
listens on `127.0.0.1` by default, unauthenticated (see the README's
"Security and privacy"). The
`/cgx/` prefix cannot collide with an Anthropic API path, so it is safe to
share a listener with the transparent passthrough.

A session id is any string matching `^[A-Za-z0-9._-]{1,128}$` (the same
pattern the store uses for its on-disk directory names). Any other value is
rejected with `400 {"error": "invalid_session_id"}` before it reaches the
scheduler or store.

## GET /cgx/health

Liveness and a quick summary.

Response `200`:

```json
{"ok": true, "version": "0.1.0", "upstream": "https://api.anthropic.com", "sessions": 2}
```

Response `503`, once a shutdown signal has put the daemon into its drain
window (see "Graceful shutdown" below):

```json
{"status": "draining"}
```

A brand-new connection is refused outright once draining starts (the
listener is closed immediately), so this `503` is only ever seen by a client
that already had a connection open — the request line itself still resolves
just fine, only the shape of the answer changes.

## GET /cgx/sessions

Status for every session the daemon currently knows about.

Response `200`:

```json
{"sessions": [{"session_id": "abc123", "enabled": true, "interval_s": null,
  "max_duration_s": null, "until": null, "last_request_at": 1700000000.0,
  "last_bump_at": 1700003000.0, "bump_count": 3, "stopped_reason": null,
  "next_bump_at": 1700006000.0, "cache_expires_at": 1700003300.0,
  "streams": 2, "last_result": null,
  "formatted": {"ttl_countdown": "04:37", "expires_at": "20:18",
  "next_bump_countdown": "49:37", "next_bump_at": "21:06"}}]}
```

## GET /cgx/sessions/{id}

One session's status dict (same shape as an entry above).

Status codes: `200` on success, `400` on an invalid id, `404` with
`{"error": "unknown_session"}` if the id is well-formed but unknown.

`cache_expires_at` is the epoch time the prompt cache is expected to
expire: the later of `last_request_at`/`last_bump_at`, plus the TTL
Anthropic actually applies to the session's selected snapshot (1 hour or
5 minutes, from the same `cache_control.ttl` check the scheduler uses to
pick its bump interval — not the smaller, configurable bump interval
itself). It is `null` only when the session has no snapshot yet, and is
present whether or not bumping is enabled.

`formatted` holds human-readable strings computed at request time in the
daemon's local timezone: countdowns (`ttl_countdown`, `next_bump_countdown`)
render as `MM:SS`, switching to `H:MM:SS` at one hour or more and clamping
to `00:00` once elapsed; clock values (`expires_at`, `next_bump_at`) render
as local `HH:MM`. `ttl_countdown`/`expires_at` are present whenever
`cache_expires_at` is known, even when the session is disabled, so a
statusline can always show the cache's time to live; `next_bump_countdown`/
`next_bump_at` are `null` when no bump is scheduled.

## PUT /cgx/sessions/{id}

Enable or disable bumping for a session, optionally overriding its interval
or max duration. Creates the session's state if it did not already exist.

Request body:

```json
{"enabled": true, "interval_s": 300, "max_duration_s": 86400, "until": 1700006000}
```

`enabled` is required and must be a boolean. `interval_s` and
`max_duration_s` are optional integers (seconds) and must be `>= 1`; omit or
pass `null` to leave a previously set value untouched, or to fall back to the
config default if none was ever set. Zero and negative values are rejected
rather than clamped — an `interval_s` of `0` would make the daemon bump on
every scheduler tick, spending a cache read per second.

`until` is an optional number (Unix epoch seconds, fractional allowed).
When set, it replaces the `max_duration_s` stop for this session with an
absolute deadline: the scheduler stops the session (`stopped_reason:
"deadline"`) once the clock reaches `until`, regardless of `max_duration_s`.
Omit or pass `null` to leave a previously set deadline untouched, or to fall
back to `max_duration_s` if none was ever set — the daemon's `max_duration_s`
default only applies when `until` is never given, e.g. plain `cgx on`. A
past `until` is accepted (not rejected as `interval_s`/`max_duration_s`
would be): the session simply stops on its next scheduler tick.

Disabling a session (`"enabled": false`) clears `until`, unlike
`interval_s`/`max_duration_s`, which survive. Every automatic stop
(`deadline`, `max_duration`, `cache_write`, `unauthorized`, `rejected`,
`transient`) clears it too. Re-enabling without a new `until` therefore
falls back to the max-duration stop rather than resuming a deadline that is
already spent.

Enabling a session whose first request has not been captured yet is
allowed and is not a stop condition: the session stays `"enabled": true`
with `next_bump_at` and `streams` reporting nothing to bump
(`null` and `0`), keeping its deadline and overrides, and starts bumping
from the first `/v1/messages` request it sees. Only the immediate bump
route below answers `404 no_snapshot` in that state.

That wait is bounded. If no request ever arrives, the session stops at
`until` (`stopped_reason: "deadline"`) when one was given, otherwise at
`max_duration_s` measured from the moment it was enabled
(`stopped_reason: "max_duration"`) — a session that never sees traffic
does not stay enabled forever.

Response: the session's updated status dict (same shape as `GET
/cgx/sessions/{id}`).

Status codes: `200` on success, `400` on an invalid id or a malformed body
(`{"error": "invalid_body"}`).

## POST /cgx/sessions/{id}/bump

Replay the session's selected snapshot once, immediately, outside the normal
schedule. Applies the same outcome bookkeeping as a scheduled bump (timer
reset, stop conditions) except that a `transient` outcome here is not
retried and does not stop the session.

Response body is a `BumpResult`:

```json
{"outcome": "ok", "status": 200, "usage": {"input_tokens": 10,
  "output_tokens": 0, "cache_read_input_tokens": 4096,
  "cache_creation_input_tokens": 0}, "detail": "", "stream_key": "ab12...",
  "model": "claude-opus-4"}
```

`outcome` is one of `ok`, `cache_write`, `unauthorized`, `rejected`,
`transient`: `ok` is a confirmed cache read; `cache_write` means the cache
was gone and a write was paid; `unauthorized` is 401/403; `rejected` is any
other 4xx; `transient` is a network or 5xx error or unreadable usage.

Status codes: `200` when `outcome` is `ok`, `409` for any other outcome (the
result is still returned in the body), `400` on an invalid id, `404` with
`{"error": "no_snapshot"}` when the session has no captured request to
replay.

## DELETE /cgx/sessions/{id}

Forget everything held for the session: state, snapshots, and any running
bump timer. Idempotent; deleting an unknown (but well-formed) id still
returns `204`.

Status codes: `204` on success, `400` on an invalid id.

## Graceful shutdown

`SIGTERM` or `SIGINT` (what `systemctl --user stop`, `cgx service restart`
and `Ctrl-C` all send) starts a drain instead of stopping the daemon
immediately: the listener closes at once (no new connections), every
in-flight request — including a long SSE stream — is given up to
`CGX_DRAIN_TIMEOUT_S` (default 600) to finish, no new scheduled bump starts
during the drain, and the daemon then exits `0`. A second shutdown signal
received while still draining cuts the wait short. See `docs/DESIGN.md`'s
"Drain plus `SO_REUSEPORT`..." decision for the full rationale, including
`cgx serve --handover` and the systemd unit's `TimeoutStopSec`/
`KillMode=mixed`.

## Integrating

The API is unauthenticated by design and listens on `127.0.0.1` by default
(see the README's "Security and privacy"). Setting `CGX_HOST` to a non-local
address is never refused, but the daemon logs a warning on startup, since
anyone who can reach the listener can spend your subscription quota.

The examples below use the default port `8787`; substitute whatever port the
daemon listens on (`CGX_PORT`, or `cgx serve --port`/`cgx service install
--port`). `cgx configure --port` writes the same port into
`env.ANTHROPIC_BASE_URL`, which the hook, statusline and CLI fall back to
when `CGX_URL` is unset.

Turn bumping on for the current session:

```sh
curl -s -X PUT "http://127.0.0.1:8787/cgx/sessions/$SESSION_ID" \
  -H 'content-type: application/json' -d '{"enabled": true}'
```

Turn it off:

```sh
curl -s -X PUT "http://127.0.0.1:8787/cgx/sessions/$SESSION_ID" \
  -H 'content-type: application/json' -d '{"enabled": false}'
```

Check status:

```sh
curl -s "http://127.0.0.1:8787/cgx/sessions/$SESSION_ID"
```

Bump right now, without waiting for the next scheduled bump:

```sh
curl -s -X POST "http://127.0.0.1:8787/cgx/sessions/$SESSION_ID/bump"
```
