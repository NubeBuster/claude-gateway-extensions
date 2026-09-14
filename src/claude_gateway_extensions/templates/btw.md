---
name: btw
description: Answer a side question asked via /btw without it ever entering the parent conversation.
event: UserPromptSubmit
intervene: deny
---

Answer the question below directly and concisely, using this session's own
context (recent files, its task, the conversation so far) as background.
Your answer is delivered to the user as the reason for a blocked prompt and
never enters the parent conversation, so give the full answer here -- do
not defer, summarize, or ask a follow-up question.

Question: {argument}

Always reply `intervene` (never `ok`), since an `ok` verdict would discard
this answer instead of showing it to the user.
