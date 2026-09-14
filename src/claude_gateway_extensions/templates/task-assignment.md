---
name: task-assignment
description: Advise on a model-to-task tier mismatch when a task is assigned an owner.
event: PreToolUse
matcher: TaskCreate|TaskUpdate
require:
  - tool_input.owner
intervene: context
---

Review the task assignment below for a model-to-task mismatch: does
the task's complexity (interpretation, ambiguity, debugging depth) match
the model tier named in its `model` field, if one is set? Sonnet is the
floor for anything involving interpretation; Haiku is for pure lookup
only; a task's model must never exceed this session's own tier.

Tool call ({tool_name}):

{tool_input}

This is advice only -- your verdict never blocks the tool call, so use
`intervene` whenever you have something worth saying and `ok` when the
assignment looks fine.

Answer with JSON only. Never call a tool.

Reply with strict JSON and nothing else:
{"verdict": "ok"|"intervene", "reason": "<one sentence of advice, or why the assignment is fine>", "mutation": null}
