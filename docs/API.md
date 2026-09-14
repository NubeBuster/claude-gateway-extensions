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
{"ok": true, "version": "1.0.0", "upstream": "https://api.anthropic.com", "sessions": 2}
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

## GET /cgx/config

The Shadow Fork tuning file's resolved path and load state, and each of the
four Shadow Fork settings' effective value and source. See `docs/DESIGN.md`'s
"Shadow Fork tuning is hot reloaded..." decision for the full design; this
section documents only the response shape. The file is re-read here exactly
as it is before a fork (its `stat` is checked and, if changed, it is
re-parsed), so this always reports what the *next* fork would use.

Response `200`:

```json
{"path": "/home/user/.config/cgx/config.toml", "present": true,
  "parse_error": null,
  "shadow_fork": {
    "max_tokens": {"value": 768, "source": "file"},
    "max_per_min": {"value": 6, "source": "default"},
    "thinking_guard": {"value": true, "source": "env"},
    "model": {"value": null, "source": "default"}}}
```

`path` is this daemon's resolved `CGX_CONFIG_FILE` (see the README's
Configuration table for the default). `present` is whether the file exists
on disk as of this check. `parse_error` is a human-readable message naming
what was wrong with the file the last time it was loaded (syntax error,
unknown key, or a value of the wrong type), or `null` when the file is
absent or the last load succeeded -- a present-but-invalid file still
reports its *previous* good values below (or the environment's, if none
were ever loaded), per the "applies atomically or not at all" rule.

Each of the four `shadow_fork` entries has `value` (the effective value that
would be used for the next fork, after the file/environment/default
precedence) and `source`: `"file"` when the tuning file sets that key,
`"env"` when the corresponding `CGX_SHADOW_FORK_*` variable was set in the
daemon's environment, otherwise `"default"`. A fork request's own
`max_tokens`/`model` field, when given, overrides these further; that
per-request override is never reflected here.

## GET /cgx/sessions

Status for every session the daemon currently knows about.

Response `200`:

```json
{"sessions": [{"session_id": "abc123", "enabled": true, "interval_s": null,
  "max_duration_s": null, "until": null, "last_request_at": 1700000000.0,
  "last_bump_at": 1700003000.0, "bump_count": 3, "replays_seen": 0,
  "stopped_reason": null,
  "next_bump_at": 1700006000.0, "cache_expires_at": 1700003300.0,
  "streams": 2, "last_result": null, "rewarm": false, "rewarm_write": null,
  "breakpoints": [{"at": "system[0]", "ttl": "1h"},
  {"at": "system[1]", "ttl": "1h"}, {"at": "system[2]", "ttl": "1h"}],
  "effective_ttl": "1h",
  "formatted": {"ttl_countdown": "04:37", "expires_at": "20:18",
  "next_bump_countdown": "49:37", "next_bump_at": "21:06",
  "rewarm_write": null, "cache": "1h x3"}}]}
```

## GET /cgx/sessions/{id}

One session's status dict (same shape as an entry above).

Status codes: `200` on success, `400` on an invalid id, `404` with
`{"error": "unknown_session"}` if the id is well-formed but unknown.

`breakpoints` lists every `cache_control` block found in the session's
selected snapshot, in prompt order (`tools`, then `system`, then
`messages`), as `{"at": "<position>", "ttl": "1h"|"5m"}`; `<position>` is
`tools[i]`, `system[i]` or `messages[i].content[j]`. `effective_ttl` is the
*shortest* TTL among them (`"5m"` if `breakpoints` is empty, Anthropic's
default ephemeral cache) — see the "Interval derived from the snapshot's
shortest breakpoint TTL" decision in `docs/DESIGN.md`: a body mixing `1h`
and `5m` breakpoints expires its shortest-lived entry well before an
interval sized for the longest one would ever fire, so both the bump
interval and `cache_expires_at` below are derived from it. Both are empty
list / `null` when the session has no snapshot yet.

`cache_expires_at` is the epoch time the prompt cache is expected to
expire: the later of `last_request_at`/`last_bump_at`, plus the real
Anthropic TTL in seconds for `effective_ttl` above (3600 or 300). It is
`null` only when the session has no snapshot yet, and is present whether or
not bumping is enabled.

`formatted` holds human-readable strings computed at request time in the
daemon's local timezone: countdowns (`ttl_countdown`, `next_bump_countdown`)
render as `MM:SS`, switching to `H:MM:SS` at one hour or more and clamping
to `00:00` once elapsed; clock values (`expires_at`, `next_bump_at`) render
as local `HH:MM`. `ttl_countdown`/`expires_at` are present whenever
`cache_expires_at` is known, even when the session is disabled, so a
statusline can always show the cache's time to live; `next_bump_countdown`/
`next_bump_at` are `null` when no bump is scheduled. `formatted.rewarm_write`
renders `rewarm_write` below (e.g. `"4096 cache-write tokens at 20:18"`), or
`null` when the session has never had a rewarm-tolerated write.
`formatted.cache` renders `breakpoints` as a compact summary grouped by
ttl, e.g. `"1h x3"` for three 1h breakpoints or `"1h x2, 5m x1"` for a
mixed body; `null` when `breakpoints` is empty.

`rewarm` is `true` while the session's rewarm flag is armed (see `PUT
/cgx/sessions/{id}` below): the next `cache_write` bump result is tolerated
instead of stopping the session, and the flag flips back to `false` once
that write happens. `rewarm_write` is `null` until the first (and, per
session, only) write the flag ever tolerates; from then on it holds that
write's usage and timestamp permanently, even after the flag that let it
through is spent:

```json
{"at": 1700003000.0, "usage": {"input_tokens": 1, "output_tokens": 0,
  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 4096}}
```

## PUT /cgx/sessions/{id}

Enable or disable bumping for a session, optionally overriding its interval
or max duration. Creates the session's state if it did not already exist.

Request body:

```json
{"enabled": true, "interval_s": 300, "max_duration_s": 86400, "until": 1700006000,
  "rewarm": false}
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

`rewarm` is an optional boolean, `false` by default. `"rewarm": true` arms
the session's rewarm flag: the next bump result classified `cache_write`
(see the "Fail-closed bump classification" and "Rewarm" decisions in
`docs/DESIGN.md`) is tolerated -- recorded as `rewarm_write` above, session
kept enabled, bumping resumes on the normal schedule -- instead of stopping
the session as it would by default. The flag is consumed by that one write:
a second `cache_write` after it stops the session exactly like today, and a
new one needs a fresh `"rewarm": true`. Passing `false` (or omitting the
field) on an already-armed session leaves the flag untouched -- only
`"enabled": false` and every automatic stop clear it, exactly like `until`.

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
`cache_write` still returns `409` even when the session's `rewarm` flag
tolerated it (the write itself was still paid) -- check the session's
status afterwards to see whether it stopped or is still `enabled: true`
with the flag now spent.

Status codes: `200` when `outcome` is `ok`, `409` for any other outcome (the
result is still returned in the body), `400` on an invalid id, `404` with
`{"error": "no_snapshot"}` when the session has no captured request to
replay.

## DELETE /cgx/sessions/{id}

Forget everything held for the session: state, snapshots, and any running
bump timer. Idempotent; deleting an unknown (but well-formed) id still
returns `204`.

Status codes: `204` on success, `400` on an invalid id.

## POST /cgx/sessions/{id}/fork

Run one shadow fork now: replay the session's selected snapshot
(the same one bumping would pick) with a single review turn appended, and
return the verdict. See `docs/DESIGN.md`'s "Shadow Fork" section
for the design; this section documents only the contract a client sees.

Request body:

```json
{"prompt": "Classify the command below...", "hook_event": "PreToolUse",
  "payload": {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
  "max_tokens": 512, "model": "claude-opus-4-5-20251101"}
```

`prompt` is required and must be a string: it becomes the fork's one
appended user message, followed by `payload` pretty-printed as a fenced
JSON block. `hook_event` is optional and accepted as any JSON value, but
the daemon does not validate it, act on it, or log it -- it exists so a
hook-side caller (`cgx shadow-fork`) has a place to pass its event name
through in the same body, purely for the caller's own bookkeeping.
`payload` is optional (omit or pass `null` for none) and is opaque to the
daemon: whatever JSON value it is gets embedded verbatim in the fork's
appended message. `max_tokens` is an optional positive integer that caps
this one fork's reply, overriding `CGX_SHADOW_FORK_MAX_TOKENS`; `0` or a negative
value is rejected rather than clamped, same as `interval_s` above. The
request actually sent upstream is this cap plus the captured snapshot's
`thinking.budget_tokens` when extended thinking is enabled there (the API
requires `max_tokens` strictly greater than the thinking budget); the cap
itself still bounds the reviewer's visible answer, the budget is headroom
the API demands on top of it. `model` is an optional non-empty string
that overrides the reviewer model for this fork only, overriding
`CGX_SHADOW_FORK_MODEL`; omitted, the fork runs on the snapshot's own model,
which is the only choice that can read the parent's cached prefix (the
prompt cache is namespaced per model), so any override always forfeits
the cache read and pays a full `cache_creation` write.

Response `200`:

```json
{"verdict": "ok", "reason": "fine", "mutation": null,
  "usage": {"input_tokens": 3, "output_tokens": 12,
  "cache_read_input_tokens": 4096, "cache_creation_input_tokens": 0},
  "paid_write": false, "model": "claude-opus-4-5-20251101",
  "stream_key": "a1b2c3...", "subagent_snapshot": false}
```

`verdict` is `"ok"` or `"intervene"`. `reason` is always a string.
`mutation` is an object when the reviewer supplied one, otherwise `null`.
`usage` is the fork reply's usage block (same fields as `BumpResult.usage`
above), or `null` when no request ever reached the upstream
(`no_snapshot`, `rate_limited`, `snapshot_stale`). `paid_write` is `true`
when the fork's own `cache_creation_input_tokens` was greater than zero:
the cached prefix had drifted, so this fork paid a full cache write
instead of the expected cache read; the verdict is still returned, since
the answer was already paid for, but a caller or statusline can use this
flag to surface a fork that is quietly costing real money. `model` is
the model the fork was actually sent to (the snapshot's own by default,
or the effective override) and `stream_key` is the selected snapshot's
stream key; both are `null` for `no_snapshot`, the only outcome reached
before a snapshot is selected. `subagent_snapshot` is `true` when no
snapshot known *not* to be subagent traffic existed for the session, so
the fork fell back to selecting over every snapshot including subagent
ones -- flagged, never refused, since a fork is one-shot spend; `false`
otherwise, including for `no_snapshot`.

This endpoint always answers `200` -- never `409` or `404` -- including
for the following degraded outcomes, so no client needs a fail-open
branch of its own the way `POST .../bump`'s `409` requires one:

- `no_snapshot`: the session has no captured request to fork from yet
  (`usage: null`, `paid_write: false`). An unknown (but well-formed)
  session id collapses into this same outcome rather than a `404`: unlike
  every other session endpoint, this one does not distinguish "session
  never seen" from "session seen but has no snapshot".
- `rate_limited`: the session has already made `CGX_SHADOW_FORK_MAX_PER_MIN`
  forks in the trailing 60 seconds (`usage: null`, `paid_write: false`).
  No request reaches the upstream.
- `snapshot_stale`: the selected snapshot is older than the TTL its own
  body requested (`ttl: "1h"` selects the 1h window, otherwise 5m); past
  that TTL the cached prefix is gone upstream and replaying it would pay
  for a full cache write instead of a read (`usage: null`,
  `paid_write: false`). No request reaches the upstream, and the refusal
  does not count against `CGX_SHADOW_FORK_MAX_PER_MIN`. A snapshot whose body
  cannot be parsed is treated as stale.
- `fork_failed`: the fork request itself failed -- a network error or a
  non-200 status from the upstream.
- `unparseable_verdict`: the upstream request succeeded but the reply was
  not the expected shape -- text content blocks that cannot be
  concatenated, or content that is not strict JSON matching
  `{"verdict": "ok"|"intervene", "reason": str, "mutation": object|null}`.
  `usage` is still populated from the reply when it was readable, and
  `paid_write` is computed from it, so a caller can see that a wasted
  fork was also an expensive one.
- `tool_use_reply`: the reply's `content` carried a block of type
  `tool_use` (`usage` populated when readable, `paid_write` computed from
  it as above). The fork body no longer strips `tools`/`tool_choice`, so a
  twin calling a tool is refused by reading the reply rather than by the
  request shape.
- `thinking_unsafe_model`/`thinking_model_unknown`: the captured snapshot
  carries thinking blocks and the target model is not known
  (`thinking_model_unknown`) or known not (`thinking_unsafe_model`) to
  preserve them across the appended turn, which would invalidate the
  message cache (`usage: null`, `paid_write: false`). No request reaches
  the upstream. Set `CGX_SHADOW_FORK_THINKING_GUARD=false` to disable this check
  for a model you have measured yourself.

`400` cases: `{"error": "invalid_session_id"}` for a malformed id (same
pattern as every other endpoint); `{"error": "invalid_body"}` when the
body is not a JSON object, `prompt` is missing or not a string,
`max_tokens` is present and not a positive integer, or `model` is
present and not a non-empty string.

A fork never alters the session's bump state: it does not set
`last_bump_at`, does not increment `bump_count`, does not reset the bump
timer, and never stops the session -- a fork whose own reply looked like
a cache write only sets `paid_write: true` above, it never triggers the
`cache_write` stop that a real bump would. A fork is a side channel that
shares only the session's captured snapshot and credential, nothing else.

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
