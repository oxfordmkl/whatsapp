# Oxford CRM — Enterprise Governance
## Implementation & Safety Workflows

> **Version:** 1.0 | **Phase:** 15B | **Audience:** All Developers & AI
> **Reading Time:** 3 minutes | **Expected Knowledge:** Rules of Engagement
> **Last Updated:** 2026-07-03 | **Update Trigger:** Governance change
> **Dependencies:** None
> **Related Documents:** `02_MASTER_CONTINUATION_PROMPT.md`

---

## 1. Purpose

This document outlines the strict workflows that govern all changes to the Oxford CRM repository. Ignoring these rules will result in regression or production failure.

---

## 2. The 8-Step Implementation Workflow

Every feature implementation MUST follow this process:

1. **Audit:** Read the relevant architecture and database documentation.
2. **Research:** Use read-only tools to examine current source code.
3. **Plan:** Write `implementation_plan.md` in your artifact directory.
4. **Approval:** Stop and wait for user explicitly to approve the plan.
5. **Execute:** Implement the changes surgically.
6. **Verify:** Check logs and database structure (if applicable) to ensure success.
7. **Document:** Update `walkthrough.md` with what was done.
8. **Update Memory:** Update `17_ai_context/ACTIVE_TASKS.md` and `PROJECT_STATE.md`.

---

## 3. The Never-Do Rules

1. **NEVER drop tables or destructive-rename columns.**
2. **NEVER implement Stripe (Razorpay only right now).**
3. **NEVER bypass `tenant_query()` or `tenant_id` filtering.**
4. **NEVER assume a clean database.**
5. **NEVER create a new `docs/` folder structure outside the approved Phase list.**

---

## 4. Rollback & Regression Philosophy

- **Rollbacks are a last resort.** If a migration or code deploy breaks production, our first instinct is to patch forward if possible.
- If a database migration must be rolled back, use the `downgrade()` function in the Alembic script carefully.
- Zero-regression is expected. If you touch auth, WABA, or billing, you must ensure existing tenant flows do not break.

---

## 5. Deployment Philosophy

- The `main` branch deploys automatically to Railway.
- Alembic migrations DO NOT run automatically. They must be executed manually via Railway CLI after deploy.
- Always assume environment variables (`DATABASE_URL`, `GEMINI_API_KEY`, `RAZORPAY_KEY`) are already set in production.
