---
name: task-assignment
description: Advise on a model-to-task tier mismatch when a task is assigned an owner.
event: PreToolUse
matcher: TaskCreate|TaskUpdate
require:
  - tool_input.owner
intervene: context
---

You are about to dispatch a task by setting its owner ({tool_name}):

{tool_input}

Judge, from this session's own context -- you hold the task list, the plan,
and what this work actually involves -- whether the task's complexity
(interpretation, ambiguity, debugging depth) matches the model tier named in
its `model` field, if one is set. Judge both directions; neither is the safe
one:

- work that needs judgment under ambiguity, unidentified-root-cause
  debugging, instruction authoring, or review of another agent's reasoning,
  handed to a smaller model, produces confident, wrong work;
- well-specified execution, mechanical patching, a suite run, or long
  context-free grinding, handed to a larger model, burns quota for nothing.

This is about the task's work, never about how the assignee happens to be
named.

This is advice only -- your verdict never blocks the assignment. Reply `ok`
when the fit is right, when the task is ambiguous enough that either tier
is defensible, or when you cannot actually tell what this work involves --
a routine dispatch is normal traffic and must not stall on an uncertain
review. Reply `intervene` only for a mismatch you can state in one sentence
and would defend to the user.

`reason` is shown to the user on a notification that cuts off past about 40
words -- write one plain sentence naming the mismatch, addressed to a
reader who cannot see this conversation.
