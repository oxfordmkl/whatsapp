# Oxford CRM — Next Phase
## Phase 15C.3 — Tenant Management Discovery

> **Version:** 15.1 | **Phase:** 15C.2 (Completed) | **Owner:** Engineering Team
> **Last Updated:** 2026-07-10 | **Status:** DISCOVERY — Awaiting Approval
> **Update Rule:** Fully rewrite this document after every phase completion

---

## Next Phase Summary

| Field | Value |
|-------|-------|
| **Phase ID** | 15C.3 |
| **Phase Name** | Tenant Management Discovery |
| **Status** | DISCOVERY — Not yet approved |
| **Priority** | HIGH |
| **Prerequisite** | Phase 15C.2 (Dashboard Discovery) completion |
| **Risk Level** | HIGH (Data Permanence Risk) |

---

## Why This Phase Is Discovery First

The current Super Admin dashboard has a disabled "Provision Tenant" button and no Delete or Archive controls. Before implementing these, a thorough discovery must determine:

- What existing tenant lifecycle states the model already supports.
- What existing tenant creation/registration mechanisms exist.
- Whether existing CLI provisioning overlaps with a UI provisioning flow.
- What foreign-key-linked data (leads, conversations, messages, users) depends on a Tenant row.
- Whether hard DELETE is architecturally safe or prohibited by FK constraints.
- Whether soft-delete (status = 'DELETED') is the only safe path.
- Whether Archive requires different semantics from Suspend.
- What billing dependencies (subscription IDs, invoice records) exist on a Tenant row.
- What WABA dependencies exist on a Tenant row.

> [!CAUTION]
> **Hard delete must not be assumed safe.** The production database contains live tenant-linked data. Deleting a Tenant row without understanding FK behavior could cascade silently or fail. Discovery must precede any implementation decision.

---

## Known Context from Phase 15C.2 Audit

The following capabilities ALREADY EXIST and must NOT be duplicated:

| Existing Capability | Route | Status |
|---------------------|-------|--------|
| Approve | `/crm/super/tenant/<id>/approve` [POST] | ✅ Production Verified |
| Suspend | `/crm/super/tenant/<id>/suspend` [POST] | ✅ Production Verified |
| Reactivate | `/crm/super/tenant/<id>/reactivate` [POST] | ✅ Production Verified |
| Impersonate | `/crm/super/impersonate/<id>` [POST] | ✅ Production Verified |

Known Tenant model status values: `TRIAL | ACTIVE | PAST_DUE | SUSPENDED | CANCELLED | DELETED`

---

## Proposed Discovery Deliverables

| # | Deliverable | Type | Risk |
|---|-------------|------|------|
| 1 | Tenant Lifecycle Semantics Audit | Discovery | LOW |
| 2 | Data Permanence & FK Dependency Map | Discovery | LOW |
| 3 | Existing Registration / Provisioning Code Audit | Discovery | LOW |
| 4 | Billing and WABA Dependency Audit | Discovery | LOW |
| 5 | Gap Analysis & Implementation Plan | Artifact | LOW |

---

## Estimated Discovery Order

```
Step 1: Read app/models.py — all FK references to Tenant
Step 2: Read app/routes/admin.py — existing Super Admin section
Step 3: Read app/routes/public.py — existing registration flow
Step 4: Assess hard-delete feasibility vs. soft-delete semantics
Step 5: Assess Create-Tenant provisioning path
Step 6: Produce Gap Analysis artifact
Step 7: Write Implementation Plan
Step 8: Await user approval ← MANDATORY
```

---

## Phase Boundary Preservation

| Phase | Responsibility |
|-------|----------------|
| **15C.3** | Tenant lifecycle management (Create, Delete, Archive) — pending audit |
| **15C.4** | Tenant Registration (public/self-service onboarding) |
| **15C.5** | Tenant Approval (existing Approve route — audit if already satisfied) |
| **15C.6** | Subscription Management (billing placeholders) |
| **15C.7** | Platform Monitoring (global analytics, aggregate metrics) |
| **15C.8** | Audit Logs |

---

## What Comes After Phase 15C.3

**Phase 15C.4 — Tenant Registration**

---

*Oxford CRM Documentation — docs/17_ai_context/NEXT_PHASE.md*
*Cross-references: `PROJECT_STATE.md` · `ACTIVE_TASKS.md` · `19_chat_memory/08_NEXT_SESSION.md`*
*This document is fully rewritten after every phase completion.*
