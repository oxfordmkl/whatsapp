# Oxford CRM — AI Handoff Protocol
## Seamless Transfer of Context Between Sessions

> **Version:** 1.0 | **Phase:** 15B | **Audience:** All AI Assistants
> **Reading Time:** 2 minutes | **Expected Knowledge:** How to end a shift
> **Last Updated:** 2026-07-03 | **Update Trigger:** Core documentation changes
> **Dependencies:** None
> **Related Documents:** `10_MEMORY_POLICY.md`

---

## 1. Purpose

AI conversation contexts are finite. When a session ends or a task is complete, the acting AI must persist its knowledge so the next AI (or a human) can resume work without lost context. This is the Handoff Protocol.

---

## 2. Required Handoff Checklist

Before ending your session or marking a Phase complete, you MUST execute the following updates:

### Step 1: Update Active Tasks
Modify `docs/17_ai_context/ACTIVE_TASKS.md`.
- Mark completed tasks as `[x] Complete`.
- Add any newly discovered blockers.

### Step 2: Update Completed Tasks
Modify `docs/17_ai_context/COMPLETED_TASKS.md`.
- Add a summary of what you just finished.
- Include the date and phase number.

### Step 3: Update Project State
Modify `docs/17_ai_context/PROJECT_STATE.md` and `PROJECT_STATE.json`.
- Increment version/phase if authorized.
- Update the "Current Blockers" section.

### Step 4: Write the Walkthrough (Implementation Only)
If you wrote code, update the local artifact `walkthrough.md`.
- Document exactly which files were changed.
- Note any new dependencies or database migrations.

---

## 5. What Must Always Be Documented

Never leave unspoken assumptions. If you made a critical design decision (e.g., "I decided not to index this column because..."), record it in the relevant architecture or database document immediately. The next AI cannot read your mind.
