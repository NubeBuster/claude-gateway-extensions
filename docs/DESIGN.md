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
- **Every non-`/cgx/` path is tunnelled verbatim.** The daemon forwards
  every request whose path is not under `/cgx/` to the upstream unchanged --
  method, path, query and body untouched -- because the upstream itself may
  be a chained proxy with its own control plane, and byte-transparency
  includes the URL, not just headers and bodies. Only `/cgx/` is reserved;
  every other path, however unfamiliar, is the upstream's to interpret.
- **Bodies never reach a log.** Request and response bodies are written to no
  log at any level; headers and bodies are only ever tee'd into the daemon's
  private snapshot. They carry the user's credential and whole conversations,
  so raising the log level must never turn the daemon into a transcript
  recorder.
- **Tee only the private copy.** Only `POST /v1/messages` is captured, and
  only the daemon's own copy of the request and response is decoded and
  parsed -- a gzip- or deflate-encoded request body (a chained upstream
  proxy or the SDK can send one) is decompressed for that private copy the
  same way a compressed response already is, with the stored snapshot's
  `content-encoding` dropped to match. The bytes forwarded in either
  direction are never touched. A body that still cannot be parsed once
  decoded logs one WARNING per session, naming the encoding and the failure,
  never the body, so a session stuck sending an uncapturable body is visible
  without a warning per request.
- **Replays are never snapshots.** A `/v1/messages` body with `max_tokens: 0`
  is by definition a bump or fork replay -- this daemon's own, or a chained
  upstream proxy's that also bumps -- never a real turn. It is forwarded
  untouched like any other request, but never captured: capturing it would
  overwrite the session's latest real snapshot with a copy of a copy. It is
  counted per session instead, as `replays_seen` on the session status, so
  the user can tell a chained bumper is active.
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
- **Rewarm: an explicit, opt-in return from a cold cache.** A bump on a
  session whose cache prefix already expired pays a `cache_write` and, by
  the fail-closed rule above, stops the session -- from the CLI that reads
  as a refusal, with no way to deliberately bring the session back.
  `rewarm: true` on `PUT /cgx/sessions/{id}` (`cgx on ... --rewarm`,
  `/cache <deadline> rewarm`) tolerates exactly one `cache_write` bump
  result for that session: the daemon records it (`rewarm_write`: the
  write's usage numbers and timestamp, surfaced in `formatted` and `cgx
  status`), keeps the session enabled, and resumes normal bumping instead
  of stopping it. The flag is consumed by that one write -- a second
  rewarm needs a new explicit enable -- and is cleared on disable and on
  every auto-stop, exactly like `until`. A bump reply that paid the write
  always says so plainly, including its size. Every session enabled
  without the flag keeps the fail-closed default unchanged: a
  `cache_write` still stops it.
- **Interval derived from the snapshot's shortest breakpoint TTL.** A body
  can carry several `cache_control` breakpoints at different TTLs -- `tools`,
  `system` and each message's content blocks, in that prompt order, and any
  1h breakpoints must precede the 5m ones -- and a bump has to keep the
  *tail* entry alive, which expires at the shortest TTL present, not the
  longest. Every breakpoint is parsed (`parse_breakpoints`) and the interval
  and `cache_expires_at` are both derived from the shortest one: bumped at
  50 min if it is 1h, at 4 min otherwise (including when no breakpoint
  states a TTL at all, Anthropic's default 5-minute ephemeral cache). The
  daemon reads this from the captured request; nothing else is authoritative
  about the TTL. `GET /cgx/sessions/{id}` exposes the parsed list as
  `breakpoints` (`{"at": "system[2]", "ttl": "1h"}`-shaped) and the derived
  `effective_ttl` (`"1h"`/`"5m"`), and `formatted.cache` renders them as a
  compact `1h x3` (or mixed `1h x2, 5m x1`) summary for `cgx status`.
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
  machine-wide, while the daemon it would point at is per user. Daemon
  settings are read once at `cgx serve` startup and frozen for that run,
  so changing one needs a daemon restart; hook-side variables have no
  such lag since the hook and CLI re-read the environment on every
  invocation. The one exception is the Shadow Fork tuning, which a
  process cannot pick up from an environment it has already inherited, so
  it has a reloadable file of its own (see "Shadow Fork tuning is hot
  reloaded from one config file" below); every other daemon variable
  stays frozen for the run.
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
- **The port is probed before it is bound.** `SO_REUSEPORT` lets a second
  process share a port silently -- without a check first, `serve` could take
  over a share of a foreign program's connections instead of refusing to
  start, or fail with a bare OS error that never says what is actually
  there. Before binding, `serve` probes `GET <host>:<port>/cgx/health`.
  Nothing answers: proceed to bind, and if the bind itself then fails, exit
  non-zero naming the OS error, never retry -- something not speaking HTTP
  got there first. A cgx daemon answers (a recognisable health body): bind
  only with `--handover`, replacing it; otherwise exit non-zero naming its
  pid and pointing at `--handover`. Anything else answers: exit non-zero
  naming the first line of its response; cgx never shares a port with a
  foreign listener. `cgx doctor` applies the same recognition to the
  configured port so a foreign program there is a `fail`, not a false `ok`.

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
  shadow fork's selection is not subject to this (see its own section
  below, "The snapshot is chosen..."): it excludes known subagent
  snapshots via `body_is_subagent` before applying the same rule, because
  a fork's cost of getting this wrong is a wrong verdict, not a wasted
  read.
- The hook relies on `UserPromptSubmit` receiving the raw prompt before skill
  expansion. If that order changes, the skill body still explains the situation
  to the user.

## Shadow Fork

A shadow fork ("microfork") asks one question about a live session
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
  optional `model`, else `CGX_SHADOW_FORK_MODEL` (the daemon-wide default), else
  `cgx shadow-fork --model`, overrides it for one fork; an override
  always forfeits the cache read and is logged once at WARNING with that
  reason. A definition's own `model:` front-matter key (see "Definitions
  are Markdown files" below) slots into this chain as another way to
  populate the request field, never as a daemon-side concept: `cgx
  shadow-fork` resolves `--model` if given, else the loaded definition's
  `model:`, else omits the field entirely and lets the daemon apply its
  own defaults (a config file, then `CGX_SHADOW_FORK_MODEL`, then the
  snapshot's own model) exactly as it does today. The daemon never learns
  a definition exists; it only ever sees the one resolved `model` field
  the hook CLI sends, same as a bare `--model` override. The response reports `model` (the model actually sent to) and
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
  Measured live on 2026-09-09 against a real Claude Code session: two
  consecutive forks of a 16046-token prefix each returned
  `cache_read_input_tokens: 16046` with `cache_creation_input_tokens: 0`,
  a parsed verdict, and `paid_write: false`.
- **`stream` is false and `max_tokens` is capped per fork** (request
  field, else `CGX_SHADOW_FORK_MAX_TOKENS`, default 512). A verdict is a
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
  (`CGX_SHADOW_FORK_THINKING_GUARD`, default on). On some models the API strips
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
- **The output contract is appended to every rendered prompt by `cgx
  shadow-fork` itself, never repeated in a definition's body.** One
  constant string (`cli/shadow_fork.py::_OUTPUT_CONTRACT`) is the reply
  shape every definition shares, so a definition author writes only what
  to review and, when it needs one, its own `verdict`/`intervene`
  constraint (e.g. `btw`'s "always reply `intervene`") -- never the JSON
  shape itself, which stayed duplicated verbatim across all four packaged
  examples before this. `parse_verdict`'s strictness is unaffected: moving
  where the contract text comes from changes nothing about how the reply
  is parsed.
- **Anything unparseable is `ok`, with a warning logged and never the
  text itself.** A broken prompt, a truncated reply or a chatty model
  must never block the user's work, so the fail-open direction is an
  invariant of the parser rather than a fallback bolted on to it.
- **Every fork is expected to be a cache read.**
  `cache_creation_input_tokens > 0` means the prefix drifted, which costs
  a full write; the verdict is still returned (the answer was paid for
  already) but flagged `paid_write: true` so a caller or a statusline can
  see a fork that is quietly costing real money.
- **Forks are rate limited per session** (`CGX_SHADOW_FORK_MAX_PER_MIN`, default
  6; over the limit answers `ok` with reason `rate_limited`). A hook on a
  hot event can fire dozens of times a minute, and a cheap fork times
  fifty is not cheap.
- **No snapshot is `ok` with reason `no_snapshot`, not an error.** A
  session whose first request has not landed yet cannot be reviewed, and
  a hook failure there would break a session that has done nothing wrong.
- **A snapshot older than its own cache TTL is refused before any request,
  `ok` with reason `snapshot_stale`, and does not count against
  `CGX_SHADOW_FORK_MAX_PER_MIN`.** The TTL is read from the snapshot's own body
  (`wants_1h_ttl` selects the 1h window, else 5m) rather than assumed,
  since a captured request may have asked for either. Bumping needs no
  such check because a bumped session is by definition kept warm; a fork
  fires on a hook against whatever session happens to be there, including
  one idle for hours with bumping off, so past the TTL the cached prefix
  is already gone upstream and replaying it pays for the full
  conversation as `cache_creation` before the verdict is even read. An
  unparseable body is treated as stale, not fresh, so a fork never spends
  money on a snapshot it cannot classify.
- **`POST /cgx/sessions/{id}/shadow-fork`**, body `{prompt, hook_event, payload,
  max_tokens?, model?}`, answering `200 {verdict, reason, mutation, usage,
  paid_write, model, stream_key}` — including for the `ok` degradations
  above, since a non-200 would push fail-open logic into every client.
- **Fork status is a per-session counter set, kept and exposed the same
  way bump status already is.** `SessionState` gains `fork_attempts`
  (every `Scheduler.fork_now` call), `forks_sent` (only the calls that
  passed the `no_snapshot`/`rate_limited`/`snapshot_stale` pre-checks and
  actually reached `fork_once`), `last_fork_at`, and `last_fork_reason` --
  the last call's outcome, but never as prose: it is always either one of
  `FAIL_OPEN_REASONS` or the bare `"ok"`/`"intervene"` verdict itself,
  the same invariant that already governs the daemon's logs, so nothing
  in status can leak a prompt or a reply. `GET /cgx/sessions/{id}` reports
  all four fields; `cgx status` renders them as a compact `N/N last=<code>`
  summary (`formatted.fork`, `None` for a session never forked), the same
  pattern `breakpoints`/`effective_ttl`/`formatted.cache` used to surface
  the shortest-breakpoint-TTL work.
- **`cgx shadow-fork --template <name>` is the only hook-side entry
  point.** It reads the hook JSON on stdin, renders the named template,
  calls the API and maps the verdict to the output shape of whichever
  event the payload names; it is top-level rather than a `cgx hook
  <event>` entry point because one implementation serves every event.
- **Fail-open, but visible.** Every failure the hook itself hits (a
  missing template, an unreachable daemon, an API error) exits 0 with no
  decision fields, same as the engine's fail-open verdicts, but also
  emits `{"systemMessage": "cgx shadow-fork skipped: <reason>"}` so a
  broken template or a dead daemon cannot fail invisibly;
  `CGX_SHADOW_FORK_QUIET=1` suppresses it for scripted use. This now also
  covers the engine's own fail-open degradations: an `ok` verdict whose
  `reason` is one of the fixed reason codes (`no_snapshot`,
  `rate_limited`, `snapshot_stale`, `unparseable_verdict`, `fork_failed`,
  `tool_use_reply`, `thinking_unsafe_model`, `thinking_model_unknown` --
  the full set lives in `core/fork_reasons.py::FAIL_OPEN_REASONS`, a leaf
  module with no `aiohttp` dependency so the CLI hook path can import it
  without pulling the daemon's transport in) gets the same visible
  `systemMessage` treatment instead of the silent exit 0 it used to get,
  since these all mean the review never actually ran. A genuine `ok` from
  the reviewer model carries its own free-text `reason` and is never
  mistaken for one of these fixed codes, so it stays silent as before --
  that is the feature working, not a failure.
- **The verdict-to-hook mapping is fixed per event, and the template
  picks only the intervention style.** `PreToolUse` gets
  `hookSpecificOutput.permissionDecision` (`deny` by default, `ask` or a
  plain `additionalContext` advisory when the template says so);
  `UserPromptSubmit` and `PostToolUse` get `hookSpecificOutput.
  additionalContext` for `intervene: context` (both events accept it in
  their own hook contract) and otherwise the top-level `decision: "block"`
  shape with the reason; `ok` always exits 0 with no output. Neither event
  has an `ask` decision of its own, so `intervene: ask` combined with a
  definition's own declared `event: UserPromptSubmit`/`PostToolUse` is
  rejected at parse time (`DefinitionError`) rather than silently
  degrading to `deny` at hook time; a definition with no declared `event:`
  cannot be checked this way and is still accepted, behaving as `deny` at
  runtime if the hook it ends up wired to fires for one of these events.
- **`mutation` is honoured only where the hook API has a slot for it.**
  At `PreToolUse` it becomes `updatedInput` (whole object, per the hook
  contract); anywhere else it is logged as ignored rather than
  approximated with injected text.
- **A template's front matter may also declare `event` and `matcher`,**
  the hook event and Claude Code hook matcher it was written for —
  `event` one of the events `_map_verdict` maps (`PreToolUse`,
  `UserPromptSubmit`, `PostToolUse`), anything else (including absent)
  falling back to undeclared exactly like an unrecognised `intervene`
  value; `matcher` a free string (e.g. `Bash`), never validated here.
  `matcher` is informational only for `cgx shadow-fork` itself — it is
  read by `cgx configure --shadow-fork` to place the hook entry. A
  declared `event` that disagrees with the payload's `hook_event_name`
  is a configuration mistake, not a review outcome: the hook exits 0
  with the existing fail-open skip `systemMessage`, naming both, before
  a fork is ever requested — previously a template wired to the wrong
  event evaporated silently, since `_map_verdict` already returned
  `None` for it after paying for the fork.
- **A definition may also declare `require:`, an ANDed list of
  conditions on the hook payload evaluated before the template is
  rendered or the daemon is called.** `require:` is a YAML block list (see
  "Definitions are Markdown files" below), one requirement per item, not
  the comma-separated scalar the old `.txt` format used -- the format
  already needs list syntax for the accepted subset to have a reason to
  exist, and one requirement per line reads better than packing several
  into one comma-joined string. Each item is `path` or `path=regex`:
  `path` means "this field exists and is a non-empty string"; `path=regex`
  means "this field exists and `re.search` matches it" (rendered as
  compact JSON first when the value is not already a string, same as a
  non-string `{placeholder}`). `matcher` alone can only see a tool name, so a hook
  on a hot event like `TaskUpdate` would otherwise fire on every status
  flip and description edit, not just the ones a template has anything
  to say about — each one a full replay of the largest prompt prefix on
  the machine, and `CGX_SHADOW_FORK_MAX_PER_MIN` does not help, since it drops
  whichever reviews land after the noise rather than the noise itself.
  A requirement that does not hold is the same "does not apply here"
  class as a `--prefix` mismatch: silent, exit 0, no fork requested,
  never a `systemMessage` — a guard that narrated every ordinary event
  would fill the log with the non-event it exists to produce. A
  malformed `require:` value (an invalid dotted path or an unparseable
  regex) is a broken template, not a non-match, so it is the usual
  fail-open skip with the visible `systemMessage` instead. The packaged
  `task-assignment` template declares `require: tool_input.owner`, since
  its `TaskCreate|TaskUpdate` matcher alone cannot express "an owner is
  being set".
- **Definitions are Markdown files (`.md`) with `---`-delimited YAML
  front matter, in the user's config directory** (`CGX_TEMPLATE_DIR`,
  default `$XDG_CONFIG_HOME/cgx/templates`), the body `{placeholder}`
  substituted from the hook payload's top-level fields exactly as before,
  unknown placeholders left literal. The extension and delimiter follow
  the [Claude Code subagent front-matter
  convention](https://code.claude.com/docs/en/sub-agents#supported-frontmatter-fields)
  so a definition reads like any other agent definition, even though it
  is never loaded as a real subagent (a fork calls no tools and takes no
  second turn; see "Non-goals" above) -- familiarity for whoever edits it
  is the whole point, not interoperability with the subagent loader.
  Four ship as examples — task assignment review at `PreToolUse`
  `TaskCreate|TaskUpdate`, a permission classifier at `PreToolUse`
  `Bash`, and the `UserPromptSubmit` pair `btw` and `rename` below —
  because the feature's value is entirely in prompts the user is
  expected to edit.
- **The front matter is parsed by a minimal in-house parser for a flat
  scalar/list YAML subset, not PyYAML** (the project's one runtime
  dependency stays `aiohttp`). The file must start with a `---` line and
  the front matter must end with another `---` line on its own; a
  missing opening or closing delimiter is an error. Between them, each
  line is either `key: value` (a scalar: the rest of the line, trimmed;
  a value wrapped in matching `"` or `'` has the quotes stripped
  verbatim, no escape processing) or a bare `key:` followed by one or
  more `  - item` lines (a block list, one item per line, same value
  rule per item), a blank line, or a full-line `#` comment (leading
  whitespace then `#` to end of line) — both ignored, since Claude Code's
  own agent files carry both and the parser gains nothing by refusing
  them. `key` is `[A-Za-z][A-Za-z0-9_-]*`, matching the old format.
  Anything else in the front matter block — a nested mapping, a
  flow-style `[a, b]` or `{a: b}`, a multi-line scalar (`|`/`>`), an
  inline (not full-line) `#`, a list item with no preceding bare `key:`,
  a duplicate key — is a parse error naming the line, not a silent
  fallback: the old `.txt` parser's leniency (unrecognised front matter
  used verbatim as body) is gone along with the format, because a
  `description` is now required and a definition that fails to parse its
  own front matter can never satisfy that. A key given as the wrong
  shape for what it accepts (e.g. `event:` as a list) is the same class
  of error.
- **Unknown keys are a parse error, not silently ignored** — the old
  `.txt` parser's "any other key is ignored" behaviour let a typo
  (`evnet:`) evaporate silently; the new parser has a closed, fixed key
  set and rejects anything outside it by name. The accepted keys:
  - `name` (scalar, optional, defaults to the file stem) — the Claude
    Code subagent field; purely cosmetic here, never used to resolve
    which file loads (that is still the `--template NAME` argument /
    filename), shown instead of the stem in an error or `cgx doctor`
    message when set.
  - `description` (scalar, **required**) — the Claude Code subagent
    field; human-facing, shown by `cgx doctor`. Required because a
    definition exists to be read and edited by the user, and an
    undescribed one defeats that; also the field whose absence makes
    "front matter is now mandatory" enforceable without a separate rule.
  - `model` (scalar, optional) — the Claude Code subagent field, reused
    for the reviewer model override with the same semantics as the
    request's `model` field and `CGX_SHADOW_FORK_MODEL` (see "A fork
    runs on the reviewed session's own model by default" above for the
    full precedence chain).
  - `tools` (**rejected**, any value) — the Claude Code subagent field,
    but there is nothing for it to mean here: a fork never calls a tool
    at all, by design (see "Non-goals" above), so `tools:` restricting
    which ones it may call is always a no-op at best and a false promise
    at worst. Rejecting it outright, the same as an unknown key, means a
    definition author who writes it gets told immediately rather than
    trusting a restriction that silently never applied.
  - `event` (scalar, optional) — one of `PreToolUse`, `UserPromptSubmit`,
    `PostToolUse`; unlike the `.txt` format, an unrecognised value is now
    a parse error naming the line, not a silent fallback to undeclared.
    A required `description` plus hard errors everywhere else in the
    format made the old fallback the odd one out, and the file is
    user-authored, so a typo (`event: UserPromtSubmit`) is worth hearing
    about rather than silently never matching. `cgx configure
    --shadow-fork` still separately turns an absent `event:` into its
    own explicit `InstallError` naming the template, since "not declared
    at all" and "declared wrong" are different failures worth different
    messages.
  - `matcher` (scalar, optional) — unchanged: a free string (e.g.
    `Bash`), never validated here, read only by `cgx configure
    --shadow-fork` to place the hook entry.
  - `require` (list, optional) — see above; a scalar `require:` value
    is the wrong shape and a parse error, not a single-item list.
  - `intervene` (scalar, optional, defaults to `deny`) — one of `deny`,
    `ask`, `context`; unlike the `.txt` format, an unrecognised value is
    now a parse error naming the line rather than a silent fallback to
    `deny`, for the same reason as `event` above. `ask` combined with a
    declared `event: UserPromptSubmit` or `event: PostToolUse` is also a
    parse error: neither event has an `ask` decision of its own in the
    hook contract, so accepting it here would only degrade to `deny`
    silently at hook time.

  `name`, `description`, `model` and `tools` are the official Claude
  Code subagent fields this convention borrows; `event`, `matcher`,
  `require` and `intervene` are the fields this project's hooks need
  that the convention has no equivalent for, carried in the same front
  matter block rather than a second block, since the parser has no way
  to tell which section a key belongs to and no reason to care.
- **The body may still contain `{placeholder}` substitutions** —
  unchanged from the `.txt` format: the hook payload's top-level fields
  by name, plus `argument` (the text after a matched `--prefix`), a
  non-string field rendered as compact JSON, and any placeholder naming
  a field the payload does not carry left literal rather than raised.
- **Migration is a hard cut, not a deprecation window: `.txt` support is
  dropped outright, the four packaged templates become `.md` files with
  converted front matter, and nothing reads a `.txt` file as a
  definition again.** Nothing has shipped in a release that reads
  `.txt` definitions — `cgx shadow-fork` and the packaged templates
  postdate `v1.0.0` — so there is no installed base to carry forward and
  no version of the parser needs to accept both shapes at once. `cgx
  doctor` gains a check that flags a leftover `<name>.txt` file sitting
  in the user's template directory (a stale copy from editing an example
  before this change) as a `warn`, naming the `.md` file it should
  become, since a `--template NAME` that used to resolve there now
  resolves nowhere and fails open, silently, at hook time — exactly the
  class of problem `cgx doctor` exists to surface up front.
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
- **`cgx shadow-fork --apply <command>` lets an `intervene` verdict act on
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
  naming it, suppressed by `CGX_SHADOW_FORK_QUIET` -- attached alongside the
  verdict's own mapped output rather than replacing it, since the template's
  `intervene:` mode still decides what the parent conversation sees. The
  packaged `rename` template (`intervene: deny`, so the prompt itself is
  always swallowed) pairs with `cgx shadow-fork --template rename --prefix
  /rename --apply '<user command>'` as a `UserPromptSubmit` hook.
- **`cgx configure --shadow-fork NAME[:PREFIX]` wires that pairing into
  `settings.json` itself, repeatable, off unless given.** Each value
  resolves the template the same way `cli/shadow_fork.py::_load_template`
  does (user directory, then the packaged copy), reads its declared
  `event`/`matcher`, and merges one `hooks.<event>` entry running `cgx
  shadow-fork --template NAME` (plus `--prefix PREFIX` when given) --
  `merge_hook`/`remove_hook`/`hook_entry_has_command` take the event (and
  an optional `matcher`, set on the entry for `PreToolUse`/`PostToolUse`)
  instead of hard-coding `UserPromptSubmit`, so the same idempotent
  "already present, do nothing" merge and the same exact-command-string
  removal marker now serve every event, not just the one this installer
  has always written. A template with no declared `event` is an
  `InstallError` naming it, never a guess -- `cgx configure` must know
  which hook to attach to. Configure also copies the packaged template
  into the user's template directory when not already there (a
  `PlannedChange`, diffed and confirmed like every other write), since the
  point of the feature is a prompt the user is expected to edit; it is
  never overwritten once present, so an edit survives a later `configure`
  run. `cgx unconfigure --shadow-fork NAME[:PREFIX]` reverses exactly the
  entry the matching `configure` call added and leaves everything else
  (including a hand-written entry at the same event) untouched; it never
  removes the copied template file, since the user may have edited it.
- **Shadow Fork tuning is hot reloaded from one config file; the file
  wins over the environment for exactly the keys it holds.** The four
  daemon-side Shadow Fork settings (`CGX_SHADOW_FORK_MAX_TOKENS`,
  `CGX_SHADOW_FORK_MAX_PER_MIN`, `CGX_SHADOW_FORK_THINKING_GUARD`,
  `CGX_SHADOW_FORK_MODEL`) are the ones a user tunes while watching
  forks land, and a restart per attempt is the wrong feedback loop --
  but a running process can never observe a change to the environment it
  inherited, so the environment alone can never hot reload. A TOML file,
  `config.toml` next to the template directory (`CGX_CONFIG_FILE`, default
  `$XDG_CONFIG_HOME/cgx/config.toml`, `%APPDATA%\cgx\config.toml` on
  Windows -- the directory the user already edits templates in, and
  `tomllib` is stdlib on every Python this project supports), holds a
  `[shadow_fork]` table with `max_tokens`, `max_per_min`,
  `thinking_guard` and `model`. Precedence per key is the fork request's
  own field, then the file, then the environment, then the built-in
  default: the environment is frozen at start, so the file must outrank
  it to be useful at all, and a key the file does not mention falls
  through to whatever the daemon started with rather than silently
  resetting a value the systemd unit carries. `CGX_CONFIG_FILE` itself is
  an ordinary frozen daemon variable, forwarded into the unit like
  `CGX_STATE_DIR`, because the unit does not forward `XDG_CONFIG_HOME`
  and the daemon and the shell must agree on which file is meant.
  Nothing else lives in the file: every other daemon setting stays
  environment-only and frozen, since a `host` or `state_dir` in a file
  that claims to be hot would be a lie, and a file holding both kinds
  would need a second precedence rule for the frozen half. The file is
  re-read when its `stat` (mtime, size, inode) changes, checked at most
  once per fork request and per `GET /cgx/config` -- a value nobody is
  about to use has no reason to be loaded, a `stat` costs nothing next
  to the replay it precedes, and forks are already rate limited, so
  there is no timer, no watcher thread and no second dependency. The
  file applies atomically or not at all: a syntax error, an unknown key
  or a value of the wrong type rejects the whole file, keeps the last
  good values (or the environment's, if none were ever loaded), logs one
  WARNING naming the file and the error, and never refuses to start or
  stops the daemon -- a tuning file must not be able to take the
  gateway out of the request path, and a rejected key would otherwise
  be a typo that silently did nothing. A missing file is the environment
  as before, never an error. `GET /cgx/config` reports the resolved
  path, whether the file is present, its parse error if any, and each
  effective value with its source (`file`, `env` or `default`); `cgx
  doctor` parses the same file with the same parser, reports its path and
  any error, and warns when the daemon resolves a different path than the
  shell running `doctor` does. Templates need none of this: the hook is
  a fresh process per event and reads them from disk each time. Hook
  wiring in `settings.json` is outside this decision's reach -- Claude
  Code reads `hooks` at session start, so a `cgx configure --shadow-fork`
  run still applies to sessions started after it, and no daemon-side
  mechanism can change that.

## Live regression harness

The cache-read guarantees this project exists for cannot be proven against
the fake upstream in `tests/`: a bump that silently starts paying
`cache_creation` still passes every offline test, and the regression costs
the user money before anyone notices. So one command drives a real Claude
Code session against a real daemon against the real API and asserts on the
numbers, deliberately outside `make check`.

- **It lives in `tests/live/`, run by `make live`, never as a `cgx`
  subcommand.** The shipped package has one runtime dependency and no test
  concerns in it; a `cgx live` subcommand would put `tmux`, a scratch
  `$HOME` writer and a price table into the thing that sits in the user's
  request path. `make live` is the entry point because the repo already
  verifies through the Makefile, and `pytest` is the wrong front door for a
  run that spends money and needs an explicit budget argument.
- **Stdlib plus `tmux`, no new dependency, dev extra at most.** The harness
  talks to the daemon with `urllib` (as the CLI client already does) and to
  the subject with `tmux` subprocesses.
- **The subject is a real interactive TUI in a detached tmux session, not
  `--print`.** `--print` is a different client: it does not exercise the
  session lifecycle, the trust dialog or the resume path a user actually
  runs, and the whole point of a live guard is that it fails where users
  would be hurt. `tmux new-session -d` plus `send-keys` drives it; nothing
  requires a visible window, so the run is unattended by construction.
- **Assertions read the daemon's control API and the subject's own
  transcript JSONL, never rendered pane text.** Pane text is a terminal
  rendering: it wraps, redraws and truncates, so an assertion over it
  passes on noise and hangs on the frame it was written for. The verdicts
  come from `POST /cgx/sessions/{id}/bump` and `/shadow-fork` response bodies and
  from `GET /cgx/sessions`; turn completion and the subject's replies come
  from `<CLAUDE_CONFIG_DIR>/projects/<slug>/<session-id>.jsonl` (the
  subject's fake one -- see below), which Claude Code writes per turn.
  `capture-pane` output is collected only into the failure report, as a
  diagnostic.
- **The subject's session id is discovered from the daemon, not from the
  subject.** An interactive session emits no machine-readable init event,
  and a session id derived from a file name goes stale on the first resume.
  `GET /cgx/sessions` before the first turn and after it yields exactly one
  new id -- the session that just sent `x-claude-code-session-id` through
  the passthrough -- which is the same identity every control call uses.
- **First-run interaction is waited out and asserted, never typed at and
  never configured around.** Two candidate mechanisms were measured
  against Claude Code 2.1.266 before choosing (2026-09-09): a `claude -p`
  warm-up in the scratch cwd left no `projects.<cwd>` entry in
  `~/.claude.json` at all, so it cannot persist trust for a following
  interactive launch; and an interactive `--safe-mode` launch from a
  never-registered scratch cwd -- empty, and again with a file in it --
  showed no workspace trust dialog, reaching the input prompt directly.
  So the harness seeds nothing: it waits for the input prompt and fails
  with the captured pane if anything else is on screen when the timeout
  expires. Answering a dialog with `send-keys` is rejected separately --
  it would encode one build's exact key order into the harness and
  silently mis-answer the next one. If a future build does gate a fresh
  cwd, the harness must fail loudly on it rather than grow either
  workaround.
- **The subject is launched with a fake `HOME` and `CLAUDE_CONFIG_DIR`,
  seeded with only the OAuth subscription login.** A subject that
  inherits the user's real `~/.claude` reads their settings, hooks and
  skills, writes its transcript into their real
  `~/.claude/projects/<slug>/` and rewrites their real `~/.claude.json` --
  none of which the user consented to for an unattended test run. The
  harness instead builds `<scratch>/home/.claude`, copies in nothing but
  `.credentials.json` (mode `0600`) so the subject can still authenticate,
  and fails early with a plain message if that file does not exist,
  rather than reaching the input prompt and then failing to log in. The
  subject's environment is otherwise built from an explicit allowlist
  rather than inherited: measured on 2026-09-09, a subject launched with
  the full environment of an agent session inherits that session's role
  and answers as it, which is a contaminated subject, not an isolated
  one. Embedding these rules in the harness is deliberate: a user of this
  public repo gets the isolation for free instead of reconstructing it.
- **The redirect is asserted, not assumed.** After the subject's warm-up
  turns, the harness checks that this run's transcript appeared under the
  fake `CLAUDE_CONFIG_DIR` and aborts the run if the real
  `~/.claude/projects/<slug>/` gained a file it did not have before the
  subject launched -- a file-count delta, never a grep for a fingerprint,
  because a leaked transcript reads as ordinary Claude Code output. A
  build that silently falls back to the real `~/.claude` when the env
  vars it expects are absent would otherwise pass every other check while
  still writing into the user's real project state.
- **`/etc/claude-code/managed-settings.json`, where present, still applies
  to the subject -- including its hooks and env block -- and neither the
  fake `HOME` nor `--safe-mode` suppresses it.** That file is system-wide
  and not redirectable by environment, so isolating the subject's `HOME`
  isolates it from the user's *personal* configuration only; it is a
  known limitation, not a gap the harness tries to close.
- **Haiku is the default subject model; the fork assertions that need a
  thinking-safe model take `--model sonnet`.** A run whose model nobody
  chose must be the cheapest one, and the haiku path is not a degraded
  variant of the run -- it is the assertion that the thinking guard
  refuses (`thinking_unsafe_model`, `paid_write: false`), which is exactly
  the protection that keeps a shadow fork from writing a whole
  conversation. So both models are real cases, and `--model sonnet`
  additionally asserts the read-with-zero-creation fork.
- **A budget guard aborts the run, computed from reported usage against a
  price table in the harness.** Every daemon response and every transcript
  entry carries token counts; the harness accumulates them, converts with
  a per-model table it owns (never a network lookup), and refuses to send
  another turn once the estimate passes `--budget-usd`. The estimate is
  reported at the end of every run, pass or fail: a live guard whose cost
  is invisible is one nobody runs twice.
- **Teardown stops the tmux session and the daemon process, and nothing
  else.** Both are released in a `finally` that runs on assertion failure,
  on exception and on `SIGINT`; the daemon is stopped through its own
  drain path (`SIGTERM`, then a health probe for `draining`, then wait) so
  the run also proves the documented shutdown behaviour instead of
  killing it. The scratch dir -- including the fake `HOME` -- is left in
  place: its stable path is deliberate (see `DEFAULT_SCRATCH` in
  `tests/live/__main__.py`), and with a fake `HOME` there is no
  `~/.claude.json` entry on the user's real one to clean up.
- **The assertion list is fixed, and each assertion names what regressing
  it would cost.** In order: a snapshot exists for the session after
  ordinary turns (`streams >= 1`); a bump answers `ok` with
  `cache_read_input_tokens > 0` and `cache_creation_input_tokens == 0`
  (the premise of the whole project); a fork on a thinking-safe model
  answers a parsed verdict with the same read-not-write shape and
  `paid_write: false`; a second fork repeats it, so a fork is not
  one-shot-cheap; a fork on haiku is refused with reason
  `thinking_unsafe_model` and `paid_write: false`, never sent; the
  subject's next turn after the forks completes normally, proving the twin
  never touched the parent; and `cgx serve` exits cleanly through drain.
- **The harness's own logic is unit-tested offline with a fake tmux and a
  fake daemon.** The parts that can be wrong without spending money --
  session-id discovery, transcript parsing, the price table, the budget
  stop, the teardown ordering -- are ordinary `pytest` cases inside `make
  check`; only the live run needs the API.
