# Oxford CRM — Version Policy
## Semantic and Phase Versioning Rules

> **Version:** 1.0 | **Phase:** 15B | **Audience:** Project Leadership & AI
> **Reading Time:** 1 minute | **Expected Knowledge:** How we version
> **Last Updated:** 2026-07-03 | **Update Trigger:** None
> **Dependencies:** None
> **Related Documents:** `17_ai_context/PROJECT_STATE.md`

---

## 1. Purpose

Oxford CRM uses a dual-versioning system to track both the application software release and the development Phase.

---

## 2. Phase Versioning (Development State)

The project is driven by "Phases". A Phase represents a targeted sprint of work.
- **Format:** `{Number}{Letter}` or `{Number}{Letter}.{SubNumber}`
- **Examples:** `15A`, `15B`, `14B.3`
- **Current Phase:** 15B
- **Where it lives:** Header block of every documentation file, and `PROJECT_STATE.md`.

---

## 3. Semantic Versioning (Documentation State)

The documentation uses semantic versioning to track its own evolution independent of the code.
- **Format:** `{Major}.{Minor}`
- **Major:** Increments when the fundamental architecture changes (e.g., Single Tenant → Multi Tenant).
- **Minor:** Increments when documentation is updated to reflect a new Phase.
- **Current Version:** 15.1
- **Where it lives:** Header block of every documentation file.

---

## 4. Release Naming (Business State)

The actual software deployed to the client has a human-readable release name.
- **Format:** `{Location} {Stage} v{Version}`
- **Current Release:** Kerala Production Candidate v1.0
- **Where it lives:** `PROJECT_BIBLE.md` and `READ_FIRST.md`.

---

## 5. Rules for Updating

- AI Assistants do NOT increment Major versions or change Release Names without explicit human authorization.
- Minor versions can be incremented when completing a documentation update at the end of a Phase.
