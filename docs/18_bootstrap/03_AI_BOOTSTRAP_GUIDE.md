# Oxford CRM — AI Bootstrap Guide
## Step-by-Step Onboarding for New Assistants

> **Version:** 1.0 | **Phase:** 15B | **Audience:** All AI Assistants
> **Reading Time:** 2 minutes | **Expected Knowledge:** How to ingest context
> **Last Updated:** 2026-07-03 | **Update Trigger:** Core documentation changes
> **Dependencies:** `01_READ_THIS_FIRST.md`
> **Related Documents:** `12_QUICK_START.md`

---

## 1. Purpose

If you are an AI assistant who just woke up in a new conversation, you are missing crucial context. This guide provides the exact sequence of documents you must read to reconstruct your memory and prevent catastrophic errors.

---

## 2. The 12-Step Bootstrap Sequence

**Step 1:** Read `docs/18_bootstrap/01_READ_THIS_FIRST.md` (You have done this).
**Step 2:** Read `docs/18_bootstrap/02_MASTER_CONTINUATION_PROMPT.md` to adopt your persona.
**Step 3:** Read `docs/17_ai_context/AI_MEMORY.md` for the comprehensive system summary.
**Step 4:** Read `docs/17_ai_context/PROJECT_STATE.md` to understand exactly where development paused.
**Step 5:** Read `docs/01_project/PROJECT_BIBLE.md` for business goals and definitions.
**Step 6:** Read `docs/02_architecture/SYSTEM_ARCHITECTURE.md` to map the codebase structure.
**Step 7:** Read `docs/02_architecture/TENANT_ARCHITECTURE.md` to understand isolation rules.
**Step 8:** Read `docs/03_database/DATABASE_BIBLE.md` for the data model.
**Step 9:** Read `docs/03_database/SCHEMA_RULES.md` before even thinking about modifying a model.
**Step 10:** Scan `docs/17_ai_context/ACTIVE_TASKS.md` for current blockers.
**Step 11:** Wait for user instruction. Confirm you have completed the bootstrap.
**Step 12:** ONLY THEN begin formulation of an Implementation Plan.

---

## 3. Fast-Track Exceptions

If the user gives you a specific, highly localized bug (e.g., "Fix CSS padding on the login button"), you may use the Quick Start flow defined in `12_QUICK_START.md`. However, any architectural or database change demands the full 12-Step Sequence.
