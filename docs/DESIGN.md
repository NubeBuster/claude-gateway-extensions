# Design

claude-cache-bump-gateway (`ccbg`) is a local transparent HTTP proxy between
Claude Code and the Anthropic API. It records the last `/v1/messages` request of
each Claude Code session and, while a session is marked for bumping, replays
that request with `max_tokens: 0` shortly before the server-side prompt cache
would expire. A cache read refreshes the cache TTL at no charge, so a user can
walk away and come back hours later to a warm cache.

This document records the decisions. `docs/API.md` specifies the control API.

## Purpose

- Keep one Claude Code session's prompt cache warm for up to a configurable
  maximum (default 24 h) with no user action beyond turning bumping on.
- Be safe to leave in the request path permanently: byte-transparent
  forwarding, no interpretation of traffic beyond what bumping needs.
- Be a building block: a small, documented local API that other tooling
  (PTY wrappers, statusline scripts, editor plugins) can drive.

## Scope of 0.1

0.1 does cache bumping only: capture the last `/v1/messages` request per
session, replay it on a timer, stop on the documented conditions. Everything
else listed under "Roadmap" is deliberately absent from this release, not
ruled out for the project.

## Roadmap

Candidate features for later releases, in no particular order:

- Cache-miss gate: hold or refuse a request whose prefix is no longer cached.
- Fork review: inspect a session's captured request before acting on it.
- Named checkpoints: label a captured snapshot and return to it later.
- TTL and placement policy: rewrite `cache_control` ttl and block placement.
- Credit accounting: track what bumping and normal traffic cost.
- Terminal-focus dialogs: prompt the user in the terminal that owns a session.
- Side-turn classification: tell subagent and sidecar prompts from the main
  conversation rather than inferring from prefix size.
- Zero-downtime reload: hand the listening socket to a new process on restart.
- Model substitution: route a request to a different model than requested.
- Audit logging: a durable record of what the daemon forwarded and replayed.
- PTY input interception: drive the gateway from the terminal instead of the
  control API.
- Control-API authentication: a credential in addition to the loopback bind.

Each of these enters through an addition to this design document before any
code, and must preserve the invariants that make the daemon safe to leave in
the request path: byte-transparent forwarding, fail-closed bumping, and a
loopback-only control API.

## Architecture

```
src/claude_cache_bump_gateway/
  __init__.py        version
  config.py          Config dataclass, the single source of truth, built from CCBG_* env vars
  core/
    clock.py         Clock protocol (now, monotonic, sleep) so schedulers are testable
    snapshot.py      Snapshot and SessionState models, stream_key(), usage parsing
    store.py         SnapshotStore protocol; MemoryStore; FileStore (XDG state dir, 0600)
    selector.py      TargetSelector protocol; LargestPrefixSelector default
    upstream.py      Upstream protocol (send a replay) and AiohttpUpstream
    bump.py          build_bump_body(), BumpResult, classify_bump_response()
    scheduler.py     Scheduler: per-session timers, stop conditions, retries
  server/
    app.py           aiohttp Application factory; wires core objects
    passthrough.py   transparent forwarding, hop-by-hop filtering, SSE streaming, tee
    control.py       /ccbg/* JSON routes
  cli/
    __init__.py      argparse entry point (main)
    client.py        thin HTTP client for the control API used by every subcommand
    hook.py          UserPromptSubmit hook handler
    statusline.py    statusline segment
    install.py       settings.json merge, skill copy, systemd unit
    doctor.py        environment checks
claude/skills/cache/SKILL.md        fall-through skill (D10a)
tests/               pytest, fake upstream server, no real API calls
```

Interfaces live in `core/` as `typing.Protocol` classes with one default
implementation each. Integrators swap implementations by constructing the
`Application` themselves via `server.app.make_app(config, *, store=, selector=,
upstream=, clock=)`. No monkey-patching, no global state.

## Request flow

1. Claude Code is pointed at the proxy with `ANTHROPIC_BASE_URL=http://127.0.0.1:8787`
   and no gateway credential, so its saved claude.ai login is forwarded unchanged
   as the request credential.
2. Every request is forwarded to `upstream` (default `https://api.anthropic.com`)
   with method, path and query preserved. Header *values* are copied verbatim
   — names are lower-cased, which HTTP treats as equivalent and HTTP/2
   requires — except RFC hop-by-hop headers (`connection`, `keep-alive`,
   `proxy-authenticate`, `proxy-authorization`, `te`, `trailers`,
   `transfer-encoding`, `upgrade`, `host`, `content-length`). Bodies are never
   modified. Upstream errors are relayed with status and body intact.
3. Responses stream chunk by chunk with no buffering, so SSE events reach the
   client the moment upstream emits them. The upstream client has a connect
   timeout and no read timeout.
4. `POST /v1/messages` only: the request body is read fully before forwarding
   (Claude Code sends it as one buffer anyway), and the response is tee'd as it
   streams. After the response completes with status < 400, the pair
   (headers, body, final `usage`) is handed to the snapshot store. Every other
   request is streamed through untouched in both directions.
5. Paths under `/ccbg/` are served locally and never forwarded.

## Session and stream model

- The session key is the `x-claude-code-session-id` request header, read
  case-insensitively. Requests without it are forwarded but never snapshotted.
- Within a session, requests are grouped into streams keyed by
  `sha256(model + system + tools)` of the JSON body. Claude Code runs several
  distinct prompts per session (the main conversation, subagents, prompt
  suggestions, title generation); each becomes its own stream. The store keeps
  the latest snapshot per stream and at most `max_streams_per_session` streams
  (default 8, LRU).
- A `Snapshot` holds: session id, stream key, model, captured request headers,
  captured request body bytes, capture time, and the response `usage` block.
- The bump target for a session is chosen by a `TargetSelector`. The default,
  `LargestPrefixSelector`, picks the stream whose last response reported the
  largest `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`.
  In practice that is the main conversation. Selection happens at bump time,
  not capture time, so a late-arriving main-conversation turn wins.
- Per-session mutable state is `SessionState`: `enabled`, `interval_s`,
  `max_duration_s`, `last_request_at`, `last_bump_at`, `bump_count`,
  `stopped_reason`. It is persisted next to the snapshots.

Session identification was the largest source of bugs in the predecessor
codebase (delegate agents sharing the lead's session id, sidecar prompts
winning selection). The selector interface is deliberately tiny (one method,
one default) so that anyone who needs different behaviour replaces it rather
than patching around it.

## Bump protocol

A bump replays the selected snapshot:

- Body: the captured bytes, re-serialised with exactly two changes:
  `max_tokens` set to `0`, and `stream` set to `false` (the API rejects
  `stream: true` with `max_tokens: 0`). Model, system, tools, messages and every
  `cache_control` block are byte-identical, which is what makes the request hit
  the same cache prefix.
- Headers: the captured request headers minus hop-by-hop headers, with the
  credential header (`authorization` or `x-api-key`) replaced by the most recent
  value seen for that session. Claude Code rotates OAuth tokens; a frozen token
  returns 401. `accept-encoding` is pinned to `gzip` rather than replayed: the
  reply has to be *parsed*, and an encoding the daemon cannot decode would make
  `usage` unreadable and so turn every `cache_write` into an `ok`. Transport
  encoding is not part of the cached prefix.
- The response is read to completion and discarded; only `usage` is inspected.
  It is decompressed first — the proxy's shared client runs with
  `auto_decompress=False` so the passthrough can forward bytes untouched.

Outcome classification (`BumpResult`):

| Condition | Result | Effect |
|---|---|---|
| 200 and `usage.cache_creation_input_tokens == 0` | `ok` | timer reset |
| 200 and `cache_creation_input_tokens > 0` | `cache_write` | the cache was already gone and a write was paid; per `on_cache_write`, default `stop` |
| 200 and `usage` missing, not an object, or the body unparseable | `transient` | the cache read could not be confirmed; retry with backoff, then stop — never reset the timer on an unconfirmed bump |
| 401 or 403 | `unauthorized` | stop session |
| other 4xx | `rejected` | stop session, record body excerpt |
| 5xx or network error | `transient` | retry with exponential backoff (1 s, 4 s, 16 s), then stop |

`BumpResult` is the single return type of the bump call. Callers cannot skip
the cache-write check because it is part of the result, not a separate step.

## Scheduling and stop conditions

- One asyncio timer per enabled session, measured from the later of the last
  real request start and the last successful bump. A real request while bumping
  is enabled simply resets the timer; no bump is fired while a real request for
  that session is in flight.
- Interval: if the snapshot body contains any `cache_control` with `ttl: "1h"`,
  default 50 min; otherwise default 4 min. Both are overridable per session via
  the API and globally via config. Claude Code on a subscription already
  requests the 1 h TTL, so the 50 min path is the common one.
- Bumping for a session stops when: it is disabled via the API; `max_duration_s`
  (default 24 h) has elapsed since the last real request; a stop condition from
  the bump protocol fires; the session is forgotten via the API; or no snapshot
  exists. The reason is recorded in `SessionState.stopped_reason` and exposed
  by the API.
- New sessions start with `enabled = config.default_enabled` (default `false`).
  Turning bumping on is a deliberate act.

## Persistence

State lives under `${XDG_STATE_HOME:-~/.local/state}/claude-cache-bump-gateway/`
as one JSON file per snapshot (`<session>/<stream>.json`) and one per session
state (`<session>/state.json`), directory mode 0700, files 0600. On start the
daemon loads state, prunes anything older than `max_duration_s`, and resumes
timers for enabled sessions.

Snapshots contain the full conversation and the credential header. This is
inherent to replaying the exact request. `CCBG_PERSIST=false` keeps everything
in memory only, at the cost of losing bump state across daemon restarts.

## Configuration

Configuration is environment variables only, prefix `CCBG_`, parsed once into a
frozen `Config` dataclass:

| Variable | Default | Meaning |
|---|---|---|
| `CCBG_HOST` | `127.0.0.1` | listen address; anything but loopback is refused without `CCBG_ALLOW_NON_LOOPBACK=true` |
| `CCBG_PORT` | `8787` | listen port |
| `CCBG_UPSTREAM` | `https://api.anthropic.com` | forward target |
| `CCBG_STATE_DIR` | XDG state path above | persistence root |
| `CCBG_PERSIST` | `true` | write snapshots and state to disk |
| `CCBG_DEFAULT_ENABLED` | `false` | bump new sessions without being asked |
| `CCBG_INTERVAL_1H_S` | `3000` | interval when the prompt requests 1 h TTL |
| `CCBG_INTERVAL_5M_S` | `240` | interval otherwise |
| `CCBG_MAX_DURATION_S` | `86400` | stop bumping this long after the last real request |
| `CCBG_ON_CACHE_WRITE` | `stop` | `stop` or `continue` when a bump paid a cache write |
| `CCBG_MAX_STREAMS_PER_SESSION` | `8` | snapshots retained per session |
| `CCBG_LOG_LEVEL` | `INFO` | Python logging level |
| `CCBG_URL` | `http://127.0.0.1:8787` | where CLI subcommands find the daemon |
| `CCBG_ALLOW_NON_LOOPBACK` | `false` | allow `CCBG_HOST` to bind a non-loopback address |

Why no configuration file: Claude Code already has a scoped configuration
ladder (managed, user, project, local) whose `env` block is inherited by the
Claude Code process, by hooks, and by anything they spawn. Putting `CCBG_*`
variables in the `env` block of the appropriate `settings.json` gives per-user
and per-project scoping for free, and `ANTHROPIC_BASE_URL` has to live there
anyway. `settings.json` rejects unknown top-level keys, so a `ccbg` section
inside it is not an option, and a parallel file ladder would duplicate Claude
Code's. Daemon-side settings that must exist before Claude Code starts (host,
port, state dir) go in the systemd unit or the shell environment.

## Control API

Served on the same listener under `/ccbg/`, JSON, loopback only, unauthenticated.
Endpoints: `GET /ccbg/health`, `GET /ccbg/sessions`, `GET /ccbg/sessions/{id}`,
`PUT /ccbg/sessions/{id}`, `POST /ccbg/sessions/{id}/bump`,
`DELETE /ccbg/sessions/{id}`. Full request and response shapes: `docs/API.md`.
The prefix `/ccbg/` cannot collide with an Anthropic path. The API is the
integration point for anything that is not this project.

## Claude Code integration

Deliberately minimal and entirely optional; the daemon plus `ANTHROPIC_BASE_URL`
is the whole product, the rest is convenience.

- Skill `claude/skills/cache/SKILL.md`, invoked as `/cache [on|off|status|bump]`
  (bare `/cache` means `status`).
  Its body only tells the model that the hook is not installed and how to install
  it. When the hook is installed the prompt never reaches the model, so the skill
  is pure discoverability with a graceful fall-through.
- `UserPromptSubmit` hook `ccbg hook user-prompt-submit`: reads the hook JSON
  from stdin; if `prompt` is `/cache` followed by end-of-string or whitespace,
  calls the control API for `session_id` and prints
  `{"decision": "block", "reason": "<one-line status>"}` so the command is
  handled without a model turn. Any other prompt exits 0 with no output. A
  hook is used because it is the only deterministic path; a skill always ends
  in a model turn.
- `ccbg statusline`: reads the statusline JSON from stdin, prints one short
  segment (`cache: on, next in 47m`, `cache: off`, or nothing when the
  daemon is unreachable) for users to append to their own statusline command.
- `ccbg install [--scope user|project]`: idempotently merges into the chosen
  `settings.json` the `env.ANTHROPIC_BASE_URL` entry and the hook registration,
  and copies the skill into the matching `skills/` directory. `ccbg uninstall`
  reverses exactly those edits. `--service` writes and enables a systemd user
  unit running `ccbg serve`. Only documented settings keys are ever written.
- `ccbg doctor`: daemon reachable, settings wired, skill present, and no
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `apiKeyHelper` set (any of them
  would replace the subscription credential and change billing).

`_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL=1` is not set by the installer. Its
only known effect is letting Claude Code verify native 1M-context models behind
a custom base URL. It is undocumented and underscore-prefixed; the README
describes it as an opt-in with that trade-off.

## Security

- The control API can spend the user's subscription quota and is unauthenticated.
  The daemon binds loopback and refuses other addresses unless explicitly
  overridden. Do not put it behind a port forward.
- Persisted snapshots hold conversation content and credentials at mode 0600 in
  a 0700 directory. Disable persistence if that is unacceptable.
- The proxy never logs request or response bodies. `DEBUG` logs headers with
  credential values redacted.
- Bumps are billed as cache reads. A bump that turns into a cache write costs a
  full write; the default policy stops bumping that session when this happens.

## Extension points

- `TargetSelector`: choose which stream to bump for a session.
- `SnapshotStore`: replace file persistence (for example an encrypted store).
- `Upstream`: replace the replay transport (for tests or a different client).
- `Clock`: control time in tests.
- `make_app(...)` keyword arguments accept any of the above.
- The control API for everything external.

## Open risks

- The 1 h TTL refresh-on-read behaviour is documented but the predecessor's
  live measurements found Anthropic's TTL behaviour has changed before. The
  `cache_write` stop condition is the safety valve; keep it default.
- Bumps issued while a session is genuinely active are wasted but harmless;
  bumps issued after the user changed model or effort level will pay a write
  once and then stop. Neither is detected proactively.
- Subagent traffic shares the lead's session id. The default selector prefers
  the largest prefix, which is normally the lead's conversation, but a very
  large subagent prompt could win. Integrators who care should supply a selector
  that also looks at `x-claude-code-agent-id`.
- The hook-based `/cache` command relies on `UserPromptSubmit` receiving
  the raw text before skill expansion. If a future Claude Code version expands
  skills first, the skill body still explains the situation to the user.
- Upstream requests are held open with no read timeout, matching Claude Code's
  own behaviour; a hung upstream is the client's problem to abort.
