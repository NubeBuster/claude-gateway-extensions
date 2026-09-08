---
name: cache
description: Turn on prompt-cache bumping for this session until a deadline, or check status/bump/off. Handled by the ccbg hook when installed.
argument-hint: "[<duration>|<clock>|<date> | off | status | bump]"
disable-model-invocation: true
---

This request, `/cache $ARGUMENTS`, reached you as a model turn instead
of being intercepted by a hook. That means the claude-cache-bump-gateway
`UserPromptSubmit` hook is not installed, or the daemon is not running.

`/cache` no longer takes a bare `on`: enabling always carries a deadline,
e.g. `/cache 2h`, `/cache 9PM`, `/cache tomorrow 10AM`, or
`/cache 2026-12-31 08:00`. `/cache off` (or `/cache 0`) disables it,
`/cache status` reports it, `/cache bump` bumps immediately.

Tell the user this directly, and ask them to run:

- `ccbg doctor` to see what is missing
- `ccbg install` to register the hook (and `ccbg serve` or a systemd
  service to run the daemon, if it is not already running)

Do not attempt to toggle cache bumping yourself, guess at its status, or
take any other action for this request.
