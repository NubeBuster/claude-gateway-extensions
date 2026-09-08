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
  `statusline`, `install`/`uninstall`, `service install`/`uninstall`/
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
