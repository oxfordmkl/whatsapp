# Oxford CRM — Quick Start
## Bootstrap Overrides for Localized Tasks

> **Version:** 1.0 | **Phase:** 15B | **Audience:** All AI Assistants
> **Reading Time:** 1 minute | **Expected Knowledge:** How to bypass full bootstrap
> **Last Updated:** 2026-07-03 | **Update Trigger:** None
> **Dependencies:** None
> **Related Documents:** `03_AI_BOOTSTRAP_GUIDE.md`

---

## 1. Purpose

The full 12-Step Bootstrap sequence is required for any architectural, database, or cross-cutting feature development. However, if the user asks for a highly localized, isolated task, you may use these Quick Start flows to save time.

---

## 2. The 10-Minute Bootstrap (UI/CSS Fixes)

**Use case:** "Change the padding on the login button" or "Fix the typo on the dashboard."

1. Read `docs/18_bootstrap/02_MASTER_CONTINUATION_PROMPT.md`
2. Read `docs/17_ai_context/PROJECT_STATE.md`
3. Execute the fix.
4. (You do not need to read Database or Architecture docs for frontend tweaks).

---

## 3. The 30-Minute Bootstrap (Single Route Fixes)

**Use case:** "The POST /tenant/save route is throwing a 500."

1. Read `docs/18_bootstrap/02_MASTER_CONTINUATION_PROMPT.md`
2. Read `docs/17_ai_context/PROJECT_STATE.md`
3. Read `docs/02_architecture/SYSTEM_ARCHITECTURE.md`
4. Read `docs/02_architecture/TENANT_ARCHITECTURE.md` (Crucial for route fixes).
5. Formulate plan and execute.

---

## 4. The Full Audit Bootstrap

**Use case:** Any task involving a new database table, a new third-party integration (e.g., Stripe), or modifying Auth/WABA routing.

1. You MUST execute the full 12-Step sequence in `03_AI_BOOTSTRAP_GUIDE.md`. No exceptions.
