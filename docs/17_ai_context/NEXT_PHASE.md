# Oxford CRM — Next Phase
## Phase 15C.2 — Super Admin Dashboard Discovery

> **Version:** 15.1 | **Phase:** 15C.1 (Completed) | **Owner:** Engineering Team
> **Last Updated:** 2026-07-09 | **Status:** DISCOVERY — Awaiting Approval
> **Update Rule:** Fully rewrite this document after every phase completion

---

## Next Phase Summary

| Field | Value |
|-------|-------|
| **Phase ID** | 15C.2 |
| **Phase Name** | Super Admin Dashboard Discovery |
| **Status** | DISCOVERY — Not yet approved |
| **Priority** | HIGH |
| **Prerequisite** | Phase 15C.1 (Authentication) completion |
| **Risk Level** | LOW (Audit Only) |

---

## Why This Phase Is Needed

The current Super Admin system (implemented through Phase 15A) supports a "Platform Control Center" dashboard.
Before any new dashboard features are implemented, we must determine:
- What dashboard capability already exists.
- What Phase 15C requirements already exist.
- What is partial.
- What is missing.
- What should remain untouched.

**NO IMPLEMENTATION UNTIL AUDIT AND APPROVAL.**

Do not assume the dashboard must be newly built because an existing Platform Control Center already exists.

---

## Proposed Deliverables

| # | Deliverable | Type | Risk |
|---|-------------|------|------|
| 1 | Existing Platform Control Center Audit | Discovery | LOW |
| 2 | Existing Features Gap Analysis | Discovery | LOW |
| 3 | Phase 15C.3 Implementation Plan | Artifact | LOW |

---

## Dependencies

| Dependency | Status |
|-----------|--------|
| Phase 15C.1 authentication verification complete | ✅ Completed |
| Production SUPER_ADMIN count verified | ✅ Confirmed at 1 |

---

## Estimated Execution Order

```
Step 1: Read templates/crm_super_dashboard.html
Step 2: Read app/routes/admin.py (Super Admin sections)
Step 3: Document existing capabilities and gaps
Step 4: Prepare Phase 15C.3 Implementation Plan
Step 5: Await user approval ← MANDATORY
```

---

## What Comes After Phase 15C.2

**Phase 15C.3 — Super Admin Implementation**
- Implement targeted missing capabilities (e.g. Delete/Archive/Create Tenant).
- Only execute approved surgical updates.

---

*Oxford CRM Documentation — docs/17_ai_context/NEXT_PHASE.md*
*Cross-references: `PROJECT_STATE.md` · `ACTIVE_TASKS.md` · `19_chat_memory/08_NEXT_SESSION.md`*
*This document is fully rewritten after every phase completion.*
