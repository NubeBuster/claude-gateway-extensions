# Control API

Served by the same listener as the proxy itself, under `/ccbg/`. JSON only,
listens on `127.0.0.1` by default, unauthenticated (see `docs/DESIGN.md`
"Security"). The
`/ccbg/` prefix cannot collide with an Anthropic API path, so it is safe to
share a listener with the transparent passthrough.

A session id is any string matching `^[A-Za-z0-9._-]{1,128}$` (the same
pattern the store uses for its on-disk directory names). Any other value is
rejected with `400 {"error": "invalid_session_id"}` before it reaches the
scheduler or store.

## GET /ccbg/health

Liveness and a quick summary.

Response `200`:

```json
{"ok": true, "version": "0.1.0", "upstream": "https://api.anthropic.com", "sessions": 2}
```

## GET /ccbg/sessions

Status for every session the daemon currently knows about.

Response `200`:

```json
{"sessions": [{"session_id": "abc123", "enabled": true, "interval_s": null,
  "max_duration_s": null, "last_request_at": 1700000000.0,
  "last_bump_at": 1700003000.0, "bump_count": 3, "stopped_reason": null,
  "next_bump_at": 1700006000.0, "streams": 2, "last_result": null}]}
```

## GET /ccbg/sessions/{id}

One session's status dict (same shape as an entry above).

Status codes: `200` on success, `400` on an invalid id, `404` with
`{"error": "unknown_session"}` if the id is well-formed but unknown.

## PUT /ccbg/sessions/{id}

Enable or disable bumping for a session, optionally overriding its interval
or max duration. Creates the session's state if it did not already exist.

Request body:

```json
{"enabled": true, "interval_s": 300, "max_duration_s": 86400}
```

`enabled` is required and must be a boolean. `interval_s` and
`max_duration_s` are optional integers (seconds) and must be `>= 1`; omit or
pass `null` to leave a previously set value untouched, or to fall back to the
config default if none was ever set. Zero and negative values are rejected
rather than clamped — an `interval_s` of `0` would make the daemon bump on
every scheduler tick, spending a cache read per second.

Response: the session's updated status dict (same shape as `GET
/ccbg/sessions/{id}`).

Status codes: `200` on success, `400` on an invalid id or a malformed body
(`{"error": "invalid_body"}`).

## POST /ccbg/sessions/{id}/bump

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
`transient` (see `docs/DESIGN.md` "Bump protocol" for what each means).

Status codes: `200` when `outcome` is `ok`, `409` for any other outcome (the
result is still returned in the body), `400` on an invalid id, `404` with
`{"error": "no_snapshot"}` when the session has no captured request to
replay.

## DELETE /ccbg/sessions/{id}

Forget everything held for the session: state, snapshots, and any running
bump timer. Idempotent; deleting an unknown (but well-formed) id still
returns `204`.

Status codes: `204` on success, `400` on an invalid id.

## Integrating

The API is unauthenticated by design and listens on `127.0.0.1` by default
(see `docs/DESIGN.md` "Security"). Setting `CCBG_HOST` to a non-local
address is never refused, but the daemon logs a warning on startup, since
anyone who can reach the listener can spend your subscription quota.

Turn bumping on for the current session:

```sh
curl -s -X PUT "http://127.0.0.1:8787/ccbg/sessions/$SESSION_ID" \
  -H 'content-type: application/json' -d '{"enabled": true}'
```

Turn it off:

```sh
curl -s -X PUT "http://127.0.0.1:8787/ccbg/sessions/$SESSION_ID" \
  -H 'content-type: application/json' -d '{"enabled": false}'
```

Check status:

```sh
curl -s "http://127.0.0.1:8787/ccbg/sessions/$SESSION_ID"
```

Bump right now, without waiting for the next scheduled bump:

```sh
curl -s -X POST "http://127.0.0.1:8787/ccbg/sessions/$SESSION_ID/bump"
```
