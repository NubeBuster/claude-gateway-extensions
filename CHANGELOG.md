# Changelog

## 0.1.0 - unreleased

- Transparent HTTP proxy between Claude Code and the Anthropic API with
  byte-transparent forwarding, SSE streaming, and `/v1/messages` snapshot
  capture per session/stream.
- Scheduler that replays a session's largest captured stream with
  `max_tokens: 0` shortly before its prompt cache TTL expires, with
  outcome-based stop conditions (unauthorized, rejected, cache write).
- Loopback-only JSON control API (`/ccbg/*`) for session status, enable/
  disable, immediate bump, and forget.
- `ccbg` CLI: `serve`, `status`, `on`/`off`, `now`, `forget`, `hook`,
  `statusline`, `configure`/`unconfigure`, `service install`/`uninstall`/
  `status`, `doctor`.
- Claude Code integration: `/cache` skill and `UserPromptSubmit` hook,
  statusline segment, idempotent settings.json installer. The systemd user
  service that runs the daemon is a separate `ccbg service` subcommand
  group, since one daemon serves every project on the machine.
- `/cache` takes an absolute deadline instead of an open-ended `on`:
  durations (`2h`, `2d 4h`), clock times (`9PM`, `21:00`, `noon`),
  `today`/`tomorrow`, and ISO dates, all parsed timezone-aware in the
  user's local zone (`cli/when.py`). The control API's `PUT
  /ccbg/sessions/{id}` gained `until` (absolute epoch seconds), which
  replaces the max-duration stop for that session (`stopped_reason:
  "deadline"`); `ccbg on --until` and the statusline/`ccbg status` deadline
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
- Explicit `--port`/`--host` wiring: `ccbg serve`, `ccbg service install`
  and `ccbg configure` all take `--port N` (`serve`/`service install` also
  take `--host H`); `ccbg configure` resolves the port from
  `CCBG_PORT`/`CCBG_URL` or the installed systemd unit before falling back
  to 8787, and prints the URL it wrote. The hook, statusline and CLI now
  also fall back to a loopback `ANTHROPIC_BASE_URL` when `CCBG_URL` is
  unset. `ccbg doctor` gained a check that the settings URL, the unit's
  port and the reachable daemon all agree.
- `ccbg install`/`uninstall` are renamed to `ccbg configure`/`unconfigure`
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
  `ccbg service install`/`uninstall` go through the same diff-and-confirm
  flow for the systemd unit file, with the same `--yes`/`--dry-run`/
  non-TTY behaviour and exit codes: a bad unit edit breaks the live daemon
  just as a bad settings edit breaks a live session.
