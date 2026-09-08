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
  `statusline`, `install`/`uninstall`, `doctor`.
- Claude Code integration: `/cache-bump` skill and `UserPromptSubmit` hook,
  statusline segment, idempotent settings.json installer with systemd user
  service support.
