---
name: btw
description: Answer a side question asked via /btw without it ever entering the parent conversation.
event: UserPromptSubmit
intervene: deny
---

A side question was asked via `/btw`, to be answered here instead of in the
parent conversation:

Question: {argument}

Answer it directly and concisely, using this session's own context (recent
files, its task, the conversation so far) as background. Your `reason`
becomes the answer shown to the user for this blocked prompt and never
enters the parent conversation -- give the full answer here; do not defer,
summarize, or ask a follow-up question.

Always reply `intervene`, never `ok`: an `ok` verdict discards this answer
instead of showing it to the user.
