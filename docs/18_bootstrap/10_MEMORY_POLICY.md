# Oxford CRM — Memory Policy
## Documentation Lifecycle and Persistence

> **Version:** 1.0 | **Phase:** 15B | **Audience:** All Developers & AI
> **Reading Time:** 2 minutes | **Expected Knowledge:** What to update and when
> **Last Updated:** 2026-07-03 | **Update Trigger:** Core documentation changes
> **Dependencies:** None
> **Related Documents:** `08_AI_HANDOFF.md`, `11_VERSION_POLICY.md`

---

## 1. Purpose

Not all documentation is equal. Some files are immutable historical records; others are living state trackers that must be updated daily. This policy defines the difference.

---

## 2. Documentation Classifications

### Class A: Living Documents (High Frequency)
These files MUST be updated frequently (often daily or at the end of an AI session).
- `docs/17_ai_context/ACTIVE_TASKS.md`
- `docs/17_ai_context/COMPLETED_TASKS.md`
- `docs/17_ai_context/PROJECT_STATE.md`
- Local artifact `walkthrough.md`

### Class B: Dynamic Architecture (Medium Frequency)
These files reflect the current codebase. Update them ONLY when the underlying architecture or database changes.
- `docs/03_database/TABLES.md`
- `docs/03_database/ERD.md`
- `docs/02_architecture/SYSTEM_ARCHITECTURE.md`

### Class C: Frozen Governance (Low Frequency)
These are constitutional documents. Do not change them unless the fundamental nature of the project shifts (e.g., changing from Razorpay to Stripe, or abandoning Multi-Tenancy).
- `docs/01_project/PROJECT_BIBLE.md`
- `docs/18_bootstrap/01_READ_THIS_FIRST.md`
- `docs/03_database/SCHEMA_RULES.md`

---

## 3. When NOT to Update

- Do NOT update a Class C document to fix a typo without authorization.
- Do NOT rewrite architecture documents just because you refactored a function. Only update if the *system design* changes.
- Do NOT delete historical completed tasks from `COMPLETED_TASKS.md` — append only.
