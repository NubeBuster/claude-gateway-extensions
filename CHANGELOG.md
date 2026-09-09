# Changelog

## Unreleased

- Shadow review fork: `cgx fork-review` gained `--apply <command>`, run
  through the shell exactly as given, with the verdict JSON
  (`{"verdict", "reason", "mutation"}`) on its stdin, only on an
  `intervene` verdict. Its own stdout is discarded, it is bounded by the
  same 5s timeout as the hook itself, and a failing, timing-out or
  missing command adds the existing fail-open `systemMessage` alongside
  the verdict's normal mapped output rather than in place of it. New
  packaged `rename` template (`intervene: deny`) pairs with it for a
  custom `/rename` that composes the label from the fork's own context
  and applies it outside the conversation being renamed.
- Shadow review fork: a fork's snapshot selection now excludes known
  subagent traffic before applying the usual largest-prefix rule, falling
  back to selecting over every snapshot (flagged `subagent_snapshot:
  true` on the response) only when no snapshot is known not to be a
  subagent's. Subagent-ness is read from the `cc_is_subagent=true` marker
  in the `system[0]` attribution block Claude Code prepends (new
  `core/snapshot.py::body_is_subagent`, three-valued so an unparseable or
  attribution-less body is treated as unknown, never as main-conversation
  traffic) and stored on each `Snapshot` as `is_subagent` at capture time.
  Previously a big subagent prompt could win the plain largest-prefix
  selection and the verdict would be a confident answer about a
  conversation the hook never fired in. Bumping's own selection is
  unchanged.
- Shadow review fork: the reviewer model is now stated and enforced as the
  reviewed session's own model by default (an invariant test pins
  `build_fork_body` to never change it), with an opt-in override -- an
  optional `model` in the `POST /cgx/sessions/{id}/fork` request body,
  else new `CGX_FORK_MODEL`, else `cgx fork-review --model`. An override
  always forfeits the cache read (the prompt cache is namespaced per
  model) and is logged once per fork at WARNING with that reason. The
  response now reports `model` (the model actually sent to) and
  `stream_key` (the selected snapshot's) alongside the existing verdict
  fields.
- Shadow review fork: the fork body no longer strips `tools`/`tool_choice`
  before replay -- Anthropic hashes the cache prefix over `tools`, then
  `system`, then `messages`, so dropping either key invalidated the whole
  prefix and turned every fork into a full `cache_creation` write. A twin
  is now kept from acting on the parent's behalf by the prompt (every
  packaged template instructs the reviewer to answer with JSON only and
  never call a tool) and by `parse_verdict`, which now treats a reply
  whose `content` carries a `tool_use` block as the fail-open `ok` result
  with reason `tool_use_reply`.
- Shadow review fork: `max_tokens` sent to the upstream now adds the
  captured snapshot's `thinking.budget_tokens` on top of the fork cap when
  extended thinking is enabled there, since the API requires `max_tokens`
  strictly greater than the thinking budget; a thinking-enabled session
  previously hit a hard 400 that `parse_verdict` fail-opened into a silent
  `ok` with no verdict and no logged reason.
- Windows CI: fixed the windows-latest test job, which was failing on
  `main` and `dev`. `tests/test_confirm.py` compared rendered headers
  against POSIX path literals instead of the same `Path` object's own
  string form, which renders with backslashes on Windows; three
  `tests/test_cli_dispatch.py` tests exercised `cgx service` (Linux-only
  by design) without the `skipif(sys.platform == "win32")` marker every
  other `service` test in the file already carries.
- Shadow review fork: a fork whose captured body carries thinking blocks
  now refuses ahead of the request, with reason `thinking_unsafe_model` or
  `thinking_model_unknown`, when the target model is not known to preserve
  them across the appended turn -- some models strip signed thinking
  blocks on a non-tool-result user turn, which invalidates the message
  cache and pays a full `cache_creation` write of the whole conversation.
  New `CGX_FORK_THINKING_GUARD` (default `true`) opts a session out for a
  user who has measured their own model.
- Shadow review fork: the appended review turn is now `system`, not
  `user`, when the captured body's last message is itself `system` --
  the API requires an inline `system` message to precede an assistant
  turn or end the array, so a `user` turn appended behind Claude Code's
  own trailing system runs was a hard 400 that `parse_verdict`
  fail-opened into a silent `ok`.
- Makefile: `check`/`fix`/`test`/`lint`/`types` now fail loudly with a
  one-line error when `.venv` is missing instead of silently falling back
  to a bare `python`/`ruff`/`pyright`/`pytest` from `PATH`. New `venv`
  target bootstraps it.
- Shadow review fork: a fork now refuses a snapshot older than its own
  cache TTL, reason `snapshot_stale`, before any request reaches the
  upstream and without counting against `CGX_FORK_MAX_PER_MIN`. Past the
  TTL the cached prefix is already gone upstream, so the previous
  behaviour replayed the whole conversation as a paid `cache_creation`
  write before the verdict was even read. The TTL is read from the
  snapshot's own body (`wants_1h_ttl`); an unparseable body is treated as
  stale.

## 1.0.0 - 2026-09-09

- Transparent HTTP proxy between Claude Code and the Anthropic API with
  byte-transparent forwarding, SSE streaming, and `/v1/messages` snapshot
  capture per session/stream.
- Scheduler that replays a session's largest captured stream with
  `max_tokens: 0` shortly before its prompt cache TTL expires, with
  outcome-based stop conditions (unauthorized, rejected, cache write).
- Loopback-only JSON control API (`/cgx/*`) for session status, enable/
  disable, immediate bump, and forget.
- `cgx` CLI: `serve`, `status`, `on`/`off`, `now`, `forget`, `hook`,
  `statusline`, `configure`/`unconfigure`, `service install`/`uninstall`/
  `status`, `doctor`.
- Claude Code integration: `/cache` skill and `UserPromptSubmit` hook,
  statusline segment, idempotent settings.json installer. The systemd user
  service that runs the daemon is a separate `cgx service` subcommand
  group, since one daemon serves every project on the machine.
- `/cache` takes an absolute deadline instead of an open-ended `on`:
  durations (`2h`, `2d 4h`), clock times (`9PM`, `21:00`, `noon`),
  `today`/`tomorrow`, and ISO dates, all parsed timezone-aware in the
  user's local zone (`cli/when.py`). The control API's `PUT
  /cgx/sessions/{id}` gained `until` (absolute epoch seconds), which
  replaces the max-duration stop for that session (`stopped_reason:
  "deadline"`); `cgx on --until` and the statusline/`cgx status` deadline
  display reuse the same parser and field. A bare number other than `0` is
  rejected rather than guessed at, and disabling a session clears its
  deadline, so re-enabling without one falls back to the max duration.
- `/cache` deadlines also accept weekday names and abbreviations
  (`wed`/`wednesday`/`we`, `fri 9AM`, `tu 21:00`), case-insensitive,
  resolving to the next local occurrence of that weekday.
- A clock time with minutes but no am/pm and a non-zero-padded 1..12 hour
  (`4:20`, `12:30`) is 12h-ambiguous: it now resolves to whichever of the
  two 12-hour candidates is the nearest upcoming one, instead of always
  reading as 24h. Zero-padded hours (`04:20`) and hours 13..23 are
  unaffected. With an explicit day (`today`/`tomorrow`/a weekday), the
  earliest candidate still ahead on that day wins.
- The hook's status replies and `cgx status` show time remaining until a
  session's deadline: `until HH:MM — Nd Nh Nm from now` on the same local
  calendar day, `until tomorrow at HH:MM — Nd Nh Nm from now` the next
  day, or the full ISO 8601 timestamp beyond that.
- `/cache` deadline names gain `morning` (9AM), `dinner` (6PM), and
  `nighttime` (10PM), alongside the existing `noon`, `teatime`, and
  `midnight`.
- Explicit `--port`/`--host` wiring: `cgx serve`, `cgx service install`
  and `cgx configure` all take `--port N` (`serve`/`service install` also
  take `--host H`); `cgx configure` resolves the port from
  `CGX_PORT`/`CGX_URL` or the installed systemd unit before falling back
  to 8787, and prints the URL it wrote. The hook, statusline and CLI now
  also fall back to a loopback `ANTHROPIC_BASE_URL` when `CGX_URL` is
  unset. `cgx doctor` gained a check that the settings URL, the unit's
  port and the reachable daemon all agree.
- `cgx install`/`uninstall` are renamed to `cgx configure`/`unconfigure`
  and no longer write a settings.json or the `cache` skill file silently:
  each existing file is shown as a colored unified diff (a new file as
  "will create") and confirmed with `Apply this change to <path>? [y/N]`
  before anything is written; a rejected file is skipped and the command
  continues, with a final summary of applied/skipped paths (exit 0 if
  anything applied, 1 if everything was rejected). `--yes`/`-y` applies
  every change without prompting; `--dry-run` still only prints diffs.
  Non-interactive stdin without `--yes` prints the diffs and aborts (exit
  2) rather than silently doing nothing or silently writing. Gained
  `--scope local` (`.claude/settings.local.json`, sharing `project`'s
  skill directory); `--scope managed` is deliberately unsupported.
  `cgx service install`/`uninstall` go through the same diff-and-confirm
  flow for the systemd unit file, with the same `--yes`/`--dry-run`/
  non-TTY behaviour and exit codes: a bad unit edit breaks the live daemon
  just as a bad settings edit breaks a live session.
- Renamed the project from `claude-cache-bump-gateway` to
  `claude-gateway-extensions`: cache bumping is its first feature, not its
  identity. CLI `ccbg` is now `cgx`, env prefix `CCBG_` is now `CGX_`, and
  the control API prefix `/ccbg/` is now `/cgx/`.
- Windows and macOS support without systemd: `cgx serve`, the hook,
  statusline and `cgx configure` now run on any OS. `CGX_STATE_DIR`
  defaults to `%LOCALAPPDATA%\claude-gateway-extensions` on Windows
  (`LOCALAPPDATA` in the environment is what selects this, keeping the
  default testable without touching `sys.platform`) and home-directory
  resolution now honours `USERPROFILE` when `HOME` is unset. Snapshot
  file/directory permissions (0600/0700) are best-effort off POSIX,
  inheriting the user profile's ACLs on Windows instead. `cgx service
  <install|uninstall|status>` now exits 1 with a pointer to `cgx serve
  --port <N>` plus `cgx configure --port <N>` on any non-Linux platform,
  since it manages a systemd user unit; `cgx doctor` skips the systemd
  check there with a one-line note instead of a permanent warning. CI
  gained a `windows-latest` job running `ruff check`, `pyright` and
  `pytest` directly; the daemon's `tzdata` runtime dependency is
  Windows-only (`zoneinfo` needs it there, `aiohttp` is still the only
  dependency everywhere else).
- Graceful shutdown: `SIGTERM`/`SIGINT` now close the listener immediately
  and drain in-flight requests (including SSE streams) for up to
  `CGX_DRAIN_TIMEOUT_S` (default 600) instead of dropping them; no new bump
  starts once draining begins, and a second shutdown signal forces an
  immediate exit. `GET /cgx/health` answers `503 {"status": "draining"}`
  during that window. The listener binds with `SO_REUSEPORT` on Linux/macOS,
  and `cgx serve --handover` uses it for a zero-gap manual upgrade (binds
  alongside the running daemon, waits for its own health check, then signals
  the old one via a new `<state dir>/daemon.pid`). `cgx service install`
  now writes `TimeoutStopSec`/`KillMode=mixed` into the systemd unit so
  `systemctl --user restart`/the new `cgx service restart` drain in place
  instead of killing the daemon mid-request; existing installs see the diff
  on the next `cgx service install`.
- Session status (`GET /cgx/sessions[/{id}]`, `cgx status --json`, and the
  hook's status reply) gains `cache_expires_at` (epoch time the prompt
  cache is expected to expire, from the real Anthropic TTL rather than the
  daemon's bump interval) and a `formatted` block of human-readable
  MM:SS/H:MM:SS countdowns and local HH:MM clock times.
- `pytest.mark.invariant(decision="...")` marks a test that guards a
  decision recorded in `docs/DESIGN.md`; a failure explains the decision
  and forbids weakening the test instead of the code without an
  amendment and the user's consent.
- Makefile with `check`/`fix`/`test`/`lint`/`types` targets is the one
  command for verification and CI.
