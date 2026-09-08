---
name: cache-bump
description: Toggle prompt-cache bumping for this session (on, off, status, now). Handled by the ccbg hook when installed.
argument-hint: "on [2h] | off | status | now"
disable-model-invocation: true
---

This request, `/cache-bump $ARGUMENTS`, reached you as a model turn instead
of being intercepted by a hook. That means the claude-cache-bump-gateway
`UserPromptSubmit` hook is not installed, or the daemon is not running.

Tell the user this directly, and ask them to run:

- `ccbg doctor` to see what is missing
- `ccbg install` to register the hook (and `ccbg serve` or a systemd
  service to run the daemon, if it is not already running)

Do not attempt to toggle cache bumping yourself, guess at its status, or
take any other action for this request.
