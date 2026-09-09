---
name: cache
description: Turn on prompt-cache bumping for this session until a deadline, or check status/bump/off. Handled by the cgx hook when installed.
argument-hint: "[<duration>|<clock>|<weekday>|<date> [rewarm] | off | status | bump]"
disable-model-invocation: true
---

This request, `/cache $ARGUMENTS`, reached you as a model turn instead
of being intercepted by a hook. That means the claude-gateway-extensions
`UserPromptSubmit` hook is not installed, or the daemon is not running.

`/cache` no longer takes a bare `on`: enabling always carries a deadline,
e.g. `/cache 2h`, `/cache 9PM`, `/cache tomorrow 10AM`, `/cache fri 9AM`,
`/cache teatime` or `/cache 2026-12-31 08:00`. Append `rewarm` to a deadline
(`/cache 2h rewarm`) to tolerate exactly one paid cache write instead of
stopping the session on it, for bringing a cold session back. `/cache off`
(or `/cache 0`) disables it, `/cache` or `/cache status` reports it,
`/cache bump` bumps immediately.

Tell the user this directly, and ask them to run:

- `cgx doctor` to see what is missing
- `cgx configure` to register the hook (and `cgx serve` or
  `cgx service install` to run the daemon, if it is not already running)

Do not attempt to toggle cache bumping yourself, guess at its status, or
take any other action for this request.
