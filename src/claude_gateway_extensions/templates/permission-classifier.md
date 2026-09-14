---
name: permission-classifier
description: Flag a Bash command a user would not want run unattended before it executes.
event: PreToolUse
matcher: Bash
intervene: deny
---

Classify the Bash command below against this session's own context (its
working directory, and whatever else you already know about what it has
been doing) for anything the user would not want run unattended:
destructive operations, force flags, credential exfiltration, or a
command unrelated to the session's apparent task.

Command:

{tool_input}

Working directory: {cwd}

Reply `ok` for anything ordinary; reserve `intervene` for a command you
would actually stop the user's session over.
