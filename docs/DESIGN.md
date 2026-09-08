# Design decisions

This file records the decisions behind claude-cache-bump-gateway and why they
were made. It is not a description of the code; the code, `README.md` and
`docs/API.md` own that.

## Scope of 0.1

0.1 does cache bumping only: capture the last `/v1/messages` request per
Claude Code session, replay it with `max_tokens: 0` before the prompt cache
TTL lapses, stop on the documented conditions. Everything under "Roadmap" is
deliberately absent from this release, not ruled out for the project.

## Decisions

- **Byte-transparent forwarding.** Headers (minus RFC hop-by-hop) and bodies
  pass through unmodified in both directions. The daemon must be safe to leave
  in the request path permanently, so it interprets nothing it does not need.
- **Tee only the private copy.** Only `POST /v1/messages` is captured, and
  only the daemon's own copy of the response is decoded and parsed. The bytes
  the client receives are never touched.
- **Session identity is `x-claude-code-session-id`.** It is the documented
  per-session header. Requests without it are forwarded but never captured.
- **Streams keyed by model + system + tools, largest prefix wins.** One
  session carries several distinct prompts (main conversation, subagents,
  title generation). Each is its own stream; the default `TargetSelector`
  bumps the one with the largest reported input, which is the main
  conversation in practice. The selector is one method so integrators replace
  it instead of patching around it.
- **Fail-closed bump classification.** A bump counts as a cache read only on
  HTTP 200 with `cache_creation_input_tokens == 0`. A paid write stops the
  session by default; unreadable usage retries then stops; auth errors stop.
  The check is part of `BumpResult`, so no caller can skip it.
- **Interval derived from the snapshot's TTL.** A body requesting a 1 h TTL is
  bumped at 50 min, otherwise at 4 min. The daemon reads this from the
  captured request; nothing else is authoritative about the TTL.
- **Absolute deadline, not an open-ended toggle.** Enabling always states
  when to stop (`/cache 2h`, `/cache 9PM`, `/cache tomorrow 10AM`). An
  unlimited "on" invites forgotten sessions that spend quota indefinitely.
  Being implemented; the daemon-side max duration remains the default only
  when no deadline is given.
- **Environment variables only, split by process.** Daemon settings are read
  by `ccbg serve`; session settings by the hook and CLI, which Claude Code
  spawns with the scoped `settings.json` `env` block already applied, so
  user/project/local scoping comes for free. `settings.json` rejects unknown
  top-level keys and a parallel file ladder would duplicate Claude Code's.
- **Control API on `/ccbg/`, loopback only, unauthenticated.** The prefix
  cannot collide with an Anthropic path. It is the integration point for
  everything external. A non-local `CCBG_HOST` is the user's explicit choice
  and logs a warning rather than being refused.
- **No first-party base URL flag by default.**
  `_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL` is undocumented and its only known
  effect is 1M-context verification behind a custom URL. Documented as an
  opt-in with that trade-off.
- **Skill as pure fall-through, hook does the work.** The `UserPromptSubmit`
  hook is the only deterministic path; it blocks the prompt and answers
  directly. The skill exists for discoverability and tells the model what to
  do when the hook is missing.
- **One runtime dependency, argparse over typer.** `aiohttp` serves and
  forwards; the CLI and hook use `urllib` so they start fast. No second HTTP
  or CLI framework.
- **Plaintext snapshots at 0600, persistence opt-out.** Replaying the exact
  request needs the conversation and the credential. Files are 0600 in a 0700
  directory; `CCBG_PERSIST=false` keeps everything in memory.

## Extension points

- `TargetSelector`: choose which stream to bump for a session.
- `SnapshotStore`: replace file persistence (for example an encrypted store).
- `Upstream`: replace the replay transport (for tests or a different client).
- `Clock`: control time in tests.
- `make_app(...)` keyword arguments accept any of the above.
- The control API for everything external.

## Roadmap

Candidate features for later releases, in no particular order:

- SessionStart hook that registers a session with its project's policy, so
  default-enabled becomes per-project.
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

Each of these enters through an addition to this document before any code,
and must preserve the invariants that make the daemon safe to leave in the
request path: byte-transparent forwarding, fail-closed bumping, and a
loopback-only control API.

## Open risks

- Bumping relies on the documented behaviour that a cache read refreshes the
  TTL. The `cache_write` stop is the guard if that changes; keep it default.
- Bumps after the user changed model or effort level pay one write, then stop.
  This is not detected proactively.
- Subagent traffic shares the lead's session id. A subagent prompt larger than
  the main conversation would win the default selection; integrators who care
  can supply a selector that also reads `x-claude-code-agent-id`.
- The hook relies on `UserPromptSubmit` receiving the raw prompt before skill
  expansion. If that order changes, the skill body still explains the situation
  to the user.
