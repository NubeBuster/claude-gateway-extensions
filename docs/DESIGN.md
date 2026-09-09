# Design decisions

This file records the decisions behind claude-gateway-extensions and why they
were made. It is not a description of the code; the code, `README.md` and
`docs/API.md` own that.

## Scope of 1.0

1.0 does cache bumping only: capture the last `/v1/messages` request per
Claude Code session, replay it with `max_tokens: 0` before the prompt cache
TTL lapses, stop on the documented conditions. Everything under "Roadmap" is
deliberately absent from this release, not ruled out for the project.

## Decisions

- **Byte-transparent forwarding.** Headers (minus RFC hop-by-hop) and bodies
  pass through unmodified in both directions. The daemon must be safe to leave
  in the request path permanently, so it interprets nothing it does not need.
- **Bodies never reach a log.** Request and response bodies are written to no
  log at any level; headers and bodies are only ever tee'd into the daemon's
  private snapshot. They carry the user's credential and whole conversations,
  so raising the log level must never turn the daemon into a transcript
  recorder.
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
  The deadline is sent to the daemon as `until`, an absolute epoch
  timestamp that replaces the max-duration stop for that session; the
  daemon-side max duration remains the default only when no deadline is
  given, e.g. plain `cgx on`.
- **Waiting for the first snapshot is a bounded wait, not a stop.** A session
  enabled before its first request (`/cache 2h` as the opening prompt) has
  nothing to replay yet, so it waits for one instead of stopping and losing
  the deadline the user just set. The wait ends at its deadline, or at the
  max duration measured from the moment it was enabled, whichever comes
  first: a session that never sees traffic must not stay enabled forever.
- **Environment variables only, split by process.** Daemon settings are read
  by `cgx serve`; session settings by the hook and CLI, which Claude Code
  spawns with the scoped `settings.json` `env` block already applied, so
  user/project/local scoping comes for free. `settings.json` rejects unknown
  top-level keys and a parallel file ladder would duplicate Claude Code's.
  `managed` is deliberately not offered as a scope: it is root-owned and
  machine-wide, while the daemon it would point at is per user.
- **Every file a user owns is diffed and confirmed before it is written.**
  `cgx configure`, `unconfigure` and `cgx service install`/`uninstall`
  compute their changes first and show each one as a colored unified diff,
  applying it only on a `y`. A bad `settings.json` edit breaks live sessions
  and a bad unit edit breaks the running daemon, so consent is per file,
  `--yes` is the only bypass, and a non-interactive stdin without it aborts
  rather than choosing for the user.
- **The port is written down, never assumed.** `serve`, `service install`
  and `configure` each take `--port`, and `configure` resolves exactly one
  port (`--port`, then `CGX_PORT`/`CGX_URL`, then the installed unit, then
  the default) into the `ANTHROPIC_BASE_URL` it writes, so the settings
  file, the systemd unit and the running daemon cannot drift apart
  unnoticed; `cgx doctor` compares the three and names any that disagree.
- **Control API on `/cgx/`, loopback only, unauthenticated.** The prefix
  cannot collide with an Anthropic path. It is the integration point for
  everything external. A non-local `CGX_HOST` is the user's explicit choice
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
  directory; `CGX_PERSIST=false` keeps everything in memory.
- **Renamed from claude-cache-bump-gateway to claude-gateway-extensions.**
  Cache bumping is the first feature, not the project's identity; the name
  had to stop describing only 0.1's scope before more gateway features
  landed. CLI `ccbg` became `cgx`, env prefix `CCBG_` became `CGX_`, and
  the control API prefix `/ccbg/` became `/cgx/`.
- **Drain plus `SO_REUSEPORT` instead of socket handover.** A restart or
  upgrade must not drop an in-flight request or refuse connections during
  the swap. Rather than the legacy project's `SCM_RIGHTS` socket handover
  (passing the listening fd itself to the new process — still a roadmap
  line below), `serve` closes its listener immediately on `SIGTERM`/
  `SIGINT`, waits for in-flight requests (including long SSE streams) up to
  `CGX_DRAIN_TIMEOUT_S`, then exits; a second shutdown signal cuts the wait
  short. The listener binds with `SO_REUSEPORT` where the platform supports
  it (Linux, macOS), so a new daemon can bind the same port and start
  serving before the old one has finished draining — `cgx serve --handover`
  drives this for non-systemd users, `TimeoutStopSec`/`KillMode=mixed` in
  the systemd unit do it for `systemctl --user restart`. State is already
  flushed synchronously on every write (`FileStore`), so drain needs no
  separate flush step.

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
- Named checkpoints: label a captured snapshot and return to it later.
- TTL and placement policy: rewrite `cache_control` ttl and block placement.
- Credit accounting: track what bumping and normal traffic cost.
- Terminal-focus dialogs: prompt the user in the terminal that owns a session.
- Side-turn classification: tell subagent and sidecar prompts from the main
  conversation rather than inferring from prefix size.
- True socket handover: pass the listening fd itself to a new process via
  `SCM_RIGHTS` on restart, instead of the drain-plus-`SO_REUSEPORT` approach
  in "Decisions" — would avoid the brief window where both daemons hold
  independent listening sockets on the same port.
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
- Subagent traffic shares the lead's session id. Bumping's default
  selection still has no concept of this: a subagent prompt larger than
  the main conversation wins the plain largest-prefix rule, and keeping
  the wrong stream warm is a wasted read, not a wrong answer. A shadow
  review fork's selection is not subject to this (see its own section
  below, "The snapshot is chosen..."): it excludes known subagent
  snapshots via `body_is_subagent` before applying the same rule, because
  a fork's cost of getting this wrong is a wrong verdict, not a wasted
  read.
- The hook relies on `UserPromptSubmit` receiving the raw prompt before skill
  expansion. If that order changes, the skill body still explains the situation
  to the user.

## Shadow review fork

A shadow review fork ("microfork") asks one question about a live session
without that session ever learning it was asked. At a hook, or any other
trigger that can reach the control API, the daemon replays the session's
captured snapshot with a single review turn appended, reads a verdict out
of the reply and discards the twin. The parent pays a cache read plus the
twin's output tokens, and its context grows by the verdict text or by
nothing at all — cheaper than blocking it to rethink, and cheaper than a
standing instruction every turn re-reads. This supersedes the "Fork
review" roadmap line; no part of it ships in 1.0.

- **A fork is one replay, never a session.** It is the bump request with
  `max_tokens` restored and one message appended, sent through the same
  `Upstream`: a twin that could take a second turn would need
  conversation state the daemon has no business owning.
- **The snapshot is chosen by the same `TargetSelector` as bumping, but
  only after excluding known subagent traffic.** The stream worth keeping
  warm is the stream worth reviewing — the main conversation — and a
  second selection rule would silently drift from the first, so
  `fork_now` still calls the same selector; it just runs it over the
  subset with `is_subagent is False` first. Subagent traffic shares the
  lead's session id (see "Open risks"), so the snapshot set for a session
  mixes main-conversation streams with subagent ones, and a big subagent
  prompt would otherwise win the plain largest-prefix rule — worse for a
  fork than for a bump, since the verdict becomes a confident answer
  about a conversation that is not the one the hook fired in. Claude Code
  states subagent-ness in the request body, not a header:
  `core/snapshot.py::body_is_subagent` reads the `x-anthropic-billing-header:`
  attribution block Claude Code prepends as `system[0]` for its
  `cc_is_subagent=true` marker, three-valued (`None` when the body is
  unreadable or carries no such block) so "unknown" is never read as
  "main conversation" and never preferred over a snapshot known `False`.
  When the known-safe subset is empty, `fork_now` falls back to selecting
  over every snapshot for the session and flags the outcome
  `subagent_snapshot: true` rather than refusing it — a fork is one-shot
  spend, not a recurring cost, so there is nothing to protect by blocking
  it the way `snapshot_stale` blocks a stale replay. Bumping's own
  selection is unchanged: it has no equivalent concept of "wrong
  conversation" to avoid, since it exists only to keep whatever stream it
  picks warm.
- **A fork runs on the reviewed session's own model by default, never a
  fixed list a tool ships.** `build_fork_body` leaves the captured body's
  `model` untouched by default, precisely because Anthropic namespaces
  the prompt cache per model: a fork sent on any other model cannot read
  the parent's cached prefix and pays a full write, and every user (and
  every company) has a different set of models available, so no shipped
  default list could be right for all of them. The request body's
  optional `model`, else `CGX_FORK_MODEL` (the daemon-wide default), else
  `cgx fork-review --model`, overrides it for one fork; an override
  always forfeits the cache read and is logged once at WARNING with that
  reason. The response reports `model` (the model actually sent to) and
  `stream_key` (the selected snapshot's) so a caller can see which model
  answered without guessing; both are `null` only for `no_snapshot`,
  reached before a snapshot exists. An override that lands on a model
  other than the snapshot's is a cost decision the caller made on
  purpose, never a refusal.
- **The appended turn is one message, `user` unless the captured body's
  last message is `system`: the rendered prompt, then the hook payload as
  a fenced JSON block.** Keeping the data verbatim and clearly delimited
  means a template author writes instructions, not an escaping scheme.
- **A trailing `system` run gets a `system` append, never a `user` one
  ahead of it.** The API requires an inline `system` message to precede
  an assistant turn or end the array, so a `user` turn appended behind
  one is a hard 400; Claude Code's own trailing system runs already hold
  more than one consecutive `system` message, so extending the run by
  one more is the same shape, not a new one. The appended message is
  identical in both branches and never carries a `cache_control` of its
  own -- it always lands after every breakpoint the captured body has,
  so a marker there buys nothing, and nothing before it changes either
  way.
- **The fork body preserves the cached prefix: `tools` and `tool_choice`
  are kept by value, never stripped.** Anthropic hashes the cache prefix
  in the fixed order `tools`, then `system`, then `messages`; dropping
  either key is the maximum-cost cache miss, turning every shadow review
  into a full `cache_creation` write of the whole conversation instead of
  the cache read the feature is premised on. A twin must still never act
  on the parent's behalf, so that guarantee comes from the prompt instead
  of the body: every packaged template instructs the reviewer to answer
  with JSON only and never call a tool, and `parse_verdict` treats any
  reply whose `content` carries a `tool_use` block as the fail-open `ok`
  result with reason `tool_use_reply`, logging only the reason code.
- **`stream` is false and `max_tokens` is capped per fork** (request
  field, else `CGX_FORK_MAX_TOKENS`, default 512). A verdict is a
  sentence and a label, so a runaway twin is a bug, not a use case, and a
  non-streamed reply is one body to parse.
- **The cap is additive on top of the captured `thinking.budget_tokens`
  when extended thinking is enabled there.** The API rejects a request
  whose `max_tokens` does not strictly exceed the thinking budget, and
  the captured `thinking` block is replayed untouched like every other
  prefix-affecting field; the cap still bounds only the reviewer's
  visible answer, the budget is headroom the API demands on top of it.
- **A fork whose captured body carries thinking blocks is refused ahead
  of the request when the target model is not known to preserve them**
  (`CGX_FORK_THINKING_GUARD`, default on). On some models the API strips
  signed thinking blocks when a non-tool-result user turn is appended,
  invalidating the message cache and rewriting the whole conversation as
  `cache_creation` -- the single most expensive thing this feature can
  do, and otherwise only visible after the fact via `paid_write`. The
  minimum version known (measured) to preserve them per family is Opus
  4.5, Sonnet 4.6, Fable 5.1; no Haiku model preserves them at any
  version. A model below its family's minimum, in a family with no
  entry, or unrecognised entirely refuses with the existing fail-open
  shape (reason `thinking_unsafe_model` or `thinking_model_unknown`) --
  unknown must never resolve as safe. The guard is silent when the body
  carries no thinking blocks: there is then nothing for an invalidating
  model to strip. The version table is a record of measurement, not a
  guess; lowering an entry needs a new measurement, not a tidy-up.
- **The verdict is strict JSON: `{"verdict": "ok" | "intervene",
  "reason": str, "mutation": object | null}`.** A fixed contract is what
  lets one hook CLI serve every template without each one inventing its
  own reply format.
- **Anything unparseable is `ok`, with a warning logged and never the
  text itself.** A broken prompt, a truncated reply or a chatty model
  must never block the user's work, so the fail-open direction is an
  invariant of the parser rather than a fallback bolted on to it.
- **Every fork is expected to be a cache read.**
  `cache_creation_input_tokens > 0` means the prefix drifted, which costs
  a full write; the verdict is still returned (the answer was paid for
  already) but flagged `paid_write: true` so a caller or a statusline can
  see a fork that is quietly costing real money.
- **Forks are rate limited per session** (`CGX_FORK_MAX_PER_MIN`, default
  6; over the limit answers `ok` with reason `rate_limited`). A hook on a
  hot event can fire dozens of times a minute, and a cheap fork times
  fifty is not cheap.
- **No snapshot is `ok` with reason `no_snapshot`, not an error.** A
  session whose first request has not landed yet cannot be reviewed, and
  a hook failure there would break a session that has done nothing wrong.
- **A snapshot older than its own cache TTL is refused before any request,
  `ok` with reason `snapshot_stale`, and does not count against
  `CGX_FORK_MAX_PER_MIN`.** The TTL is read from the snapshot's own body
  (`wants_1h_ttl` selects the 1h window, else 5m) rather than assumed,
  since a captured request may have asked for either. Bumping needs no
  such check because a bumped session is by definition kept warm; a fork
  fires on a hook against whatever session happens to be there, including
  one idle for hours with bumping off, so past the TTL the cached prefix
  is already gone upstream and replaying it pays for the full
  conversation as `cache_creation` before the verdict is even read. An
  unparseable body is treated as stale, not fresh, so a fork never spends
  money on a snapshot it cannot classify.
- **`POST /cgx/sessions/{id}/fork`**, body `{prompt, hook_event, payload,
  max_tokens?, model?}`, answering `200 {verdict, reason, mutation, usage,
  paid_write, model, stream_key}` — including for the `ok` degradations
  above, since a non-200 would push fail-open logic into every client.
- **`cgx fork-review --template <name>` is the only hook-side entry
  point.** It reads the hook JSON on stdin, renders the named template,
  calls the API and maps the verdict to the output shape of whichever
  event the payload names; it is top-level rather than a `cgx hook
  <event>` entry point because one implementation serves every event.
- **Fail-open, but visible.** Every failure the hook itself hits (a
  missing template, an unreachable daemon, an API error) exits 0 with no
  decision fields, same as the engine's fail-open verdicts, but also
  emits `{"systemMessage": "cgx fork-review skipped: <reason>"}` so a
  broken template or a dead daemon cannot fail invisibly;
  `CGX_FORK_REVIEW_QUIET=1` suppresses it for scripted use.
- **The verdict-to-hook mapping is fixed per event, and the template
  picks only the intervention style.** `PreToolUse` gets
  `hookSpecificOutput.permissionDecision` (`deny` by default, `ask` or a
  plain `additionalContext` advisory when the template says so),
  `UserPromptSubmit` and the `Post*` events get top-level `decision:
  "block"` with the reason, and `ok` always exits 0 with no output.
- **`mutation` is honoured only where the hook API has a slot for it.**
  At `PreToolUse` it becomes `updatedInput` (whole object, per the hook
  contract); anywhere else it is logged as ignored rather than
  approximated with injected text.
- **Templates are plain text files in the user's config directory**
  (`CGX_TEMPLATE_DIR`, default `$XDG_CONFIG_HOME/cgx/templates`),
  `{placeholder}` substituted from the hook payload's top-level fields,
  unknown placeholders left literal. Two ship as examples — task
  assignment review at `PreToolUse` `TaskCreate`, and a permission
  classifier at `PreToolUse` `Bash` — because the feature's value is
  entirely in prompts the user is expected to edit.
- **`/btw <question>` is a `UserPromptSubmit` template that always
  intervenes.** The answer comes back as the block reason, so the
  question and its answer are shown to the user and never enter the
  parent's context — which is the whole point of asking it on the side.
- **The fork replays the parent's credential and logs neither prompt nor
  verdict in full.** Both rules already hold for bumping, and a review
  turn is exactly the kind of content the no-body-logging invariant
  exists to keep out of logs.
- **Non-goals for the first cut: no multi-turn twins, no tool use by the
  twin, no fork of one session against another's snapshot, no persistence
  of a fork's reply.** Each of these turns a stateless one-shot into a
  thing with a lifecycle, and none is needed by the four use cases that
  motivated the feature.
- **`cgx fork-review --apply <command>` lets an `intervene` verdict act on
  the side rather than only through the hook's own decision fields.** The
  motivating case is a user-controlled `/rename`: the built-in session
  rename is not customisable, and doing it in-conversation costs a turn and
  pollutes the context being renamed, while a fork already holds the same
  context at a cache read. On `intervene`, and only then, the command runs
  through the shell exactly as the user gave it (their own string, never
  split), fed the verdict JSON (`{"verdict", "reason", "mutation"}`) on
  stdin, stdout discarded so it can never contaminate the hook's own stdout
  contract, bounded by the same timeout as the hook itself
  (`cli/install.py::_HOOK_TIMEOUT_S`, 5s). A non-zero exit, a timeout or a
  missing command is the existing fail-open skip -- exit 0, a `systemMessage`
  naming it, suppressed by `CGX_FORK_REVIEW_QUIET` -- attached alongside the
  verdict's own mapped output rather than replacing it, since the template's
  `intervene:` mode still decides what the parent conversation sees. The
  packaged `rename` template (`intervene: deny`, so the prompt itself is
  always swallowed) pairs with `cgx fork-review --template rename --prefix
  /rename --apply '<user command>'` as a `UserPromptSubmit` hook.
