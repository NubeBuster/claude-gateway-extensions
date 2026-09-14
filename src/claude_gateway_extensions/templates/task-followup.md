---
name: task-followup
description: Advise on a model-to-task tier mismatch when a follow-up message is sent to a running teammate.
event: PreToolUse
matcher: SendMessage
intervene: context
---

You are about to send a teammate follow-up work ({tool_name}):

{tool_input}

Judge, from this session's own context -- you know what this teammate has
already handled and what you are about to hand it next -- whether the
CONTENT of this message matches that teammate's model tier. Judge both
directions; neither is the safe one:

- work that needs judgment under ambiguity, unidentified-root-cause
  debugging, instruction authoring, or review of another agent's
  reasoning, handed to a smaller model, produces confident, wrong work;
- well-specified execution, mechanical patching, a suite run, or long
  context-free grinding, handed to a larger model, burns quota for
  nothing.

This is about what the message actually asks for, never about how well the
teammate answered last time or how the conversation has been going.

This is advice only -- your verdict never blocks the send. Reply `ok` when
the fit is right, when the work is ambiguous enough that either tier is
defensible, or when you cannot actually tell what the message asks for --
ordinary follow-up traffic is normal and must not stall on an uncertain
review. Reply `intervene` only for a mismatch you can state in one
sentence and would defend to the user.

`reason` is shown to the user on a notification that cuts off past about
40 words -- write one plain sentence naming the mismatch, addressed to a
reader who cannot see this conversation.
