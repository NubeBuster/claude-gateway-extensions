# Changelog

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
