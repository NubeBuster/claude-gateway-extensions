---
name: rename
description: Compose a short label for the session, applied outside the conversation via --apply.
event: UserPromptSubmit
intervene: deny
---

Look at this session's own context (recent files, its task, the
conversation so far) and come up with a short label for it: lowercase,
kebab-case, two to four words, no punctuation other than hyphens. Prefer
the concrete subject of the work over generic words like "session" or
"task".

Your `reason` is never shown to the user directly and never enters the
parent conversation -- it is consumed by a rename command outside this
conversation, so answer with nothing but the label itself.

Always reply `intervene`, never `ok`: an `ok` verdict discards the label
instead of handing it to the rename command.
