# Oxford CRM — New Chat Guide
## How Humans Should Initialize a New AI Session

> **Version:** 1.0 | **Phase:** 15B | **Audience:** Human Developers
> **Reading Time:** 2 minutes | **Expected Knowledge:** How to prompt effectively
> **Last Updated:** 2026-07-03 | **Update Trigger:** None
> **Dependencies:** None
> **Related Documents:** `02_MASTER_CONTINUATION_PROMPT.md`

---

## 1. Purpose

When starting a new conversation with an AI assistant (ChatGPT, Cursor, etc.), the AI has zero context. If you just say "Fix the billing bug," the AI will hallucinate architecture and break the repository. This guide tells human operators exactly how to inject the Master Memory.

---

## 2. Best Practices for Initialization

1. **Always Use the Master Prompt:** Paste the contents of `02_MASTER_CONTINUATION_PROMPT.md` into the very first message of the chat.
2. **Point to the Memory Folder:** Tell the AI to read `docs/18_bootstrap/01_READ_THIS_FIRST.md` before doing anything else.
3. **Be Explicit About the Task:** After the AI acknowledges the bootstrap, provide your specific task.

---

## 3. Example Prompt Template

**Copy and paste this into a new chat:**

```text
You are continuing the Oxford CRM project.

Before doing anything, read the Master Continuation Prompt located at:
docs/18_bootstrap/02_MASTER_CONTINUATION_PROMPT.md

Then, follow the AI Bootstrap Guide at:
docs/18_bootstrap/03_AI_BOOTSTRAP_GUIDE.md

Once you have completed the bootstrap sequence and read the project state, acknowledge your readiness.

DO NOT write code yet.

My task for today is: [Insert your task here]
```

---

## 4. Common Mistakes to Avoid

- **Mistake:** Assuming the AI knows what Oxford CRM is.
  *Result:* It builds a generic CRM that breaks tenant isolation.
- **Mistake:** Letting the AI write a database migration without reading `SCHEMA_RULES.md`.
  *Result:* PostgreSQL locks, broken schemas, and catastrophic data loss.
- **Mistake:** Failing to update the documentation at the end of the chat.
  *Result:* The next AI session will be operating on outdated state.
