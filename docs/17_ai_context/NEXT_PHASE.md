# Oxford CRM — Next Phase
## Phase 15C — Super Admin Platform

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Engineering Team
> **Last Updated:** 2026-07-02 | **Status:** PLANNED — Awaiting Approval
> **Update Rule:** Fully rewrite this document after every phase completion

---

## Next Phase Summary

| Field | Value |
|-------|-------|
| **Phase ID** | 15C |
| **Phase Name** | Super Admin Platform |
| **Status** | PLANNED — Not yet approved |
| **Priority** | HIGH |
| **Prerequisite** | Phase 15B (Documentation) completion |
| **Risk Level** | MEDIUM |

---

## Why This Phase Is Needed

The current Super Admin system (implemented through Phase 15A) supports:
- ✅ View all tenants
- ✅ Approve PENDING tenants
- ✅ Suspend tenants
- ✅ Reactivate tenants
- ✅ Impersonate tenants

It is **missing:**
- ❌ Delete Tenant (soft-delete to `DELETED` status)
- ❌ Archive Tenant (soft-archive to `ARCHIVED` status)
- ❌ Create New Tenant (without public registration)

Without delete/archive, removing a test tenant or decommissioning a client requires direct database intervention — which is dangerous in production.

---

## Proposed Deliverables

| # | Deliverable | Type | Risk |
|---|-------------|------|------|
| 1 | `DELETE /crm/super/tenant/<id>/delete` route | Route addition | LOW |
| 2 | `DELETE /crm/super/tenant/<id>/archive` route | Route addition | LOW |
| 3 | Super Admin dashboard UI buttons for delete/archive | Template update | LOW |
| 4 | Confirmation modal before delete/archive | Template update | LOW |

---

## Dependencies

| Dependency | Status |
|-----------|--------|
| Phase 15B documentation complete | In Progress |
| No new database schema required | Confirmed |
| `Tenant.status` already supports `DELETED` value | ✅ Confirmed in `app/models.py` |
| `admin_bp` already has Super Admin section | ✅ Confirmed (lines 4765–4836) |

---

## Risk Analysis

| Risk | Severity | Mitigation |
|------|---------|-----------|
| Editing `admin.py` (4,800+ lines) | MEDIUM | Surgical edits to Super Admin section only |
| Accidental tenant deletion | MEDIUM | Soft-delete only (status = 'DELETED', data preserved) |
| Template regression | LOW | Only modify Super Admin dashboard template |

**No database schema changes required.** `Tenant.status` already supports `DELETED`.

---

## Rollback Plan

| Step | Action |
|------|--------|
| 1 | Remove the two new route functions from `admin.py` |
| 2 | Remove the two UI buttons from `crm_super_dashboard.html` |
| 3 | No database rollback needed (no migration) |
| **Time to rollback** | < 2 minutes |

---

## Testing Plan

After implementation, verify:

| Test | Expected Result |
|------|----------------|
| SUPER_ADMIN login | ✅ Still works |
| Super Admin dashboard loads | ✅ Still works |
| Existing approve/suspend/reactivate | ✅ Still works |
| Impersonation flow | ✅ Still works |
| Delete tenant (test) | ✅ Status changes to DELETED |
| Archive tenant (test) | ✅ Status changes to ARCHIVED |
| Deleted tenant cannot log in | ✅ Login blocked by tenant status check |
| ADMIN/STAFF login unaffected | ✅ No changes to login flow |

---

## Estimated Implementation Order

```
Step 1: Read app/routes/admin.py — Super Admin section (lines 4765–4836)
Step 2: Read templates/crm_super_dashboard.html
Step 3: Write implementation plan artifact
Step 4: Await user approval ← MANDATORY
Step 5: Add crm_super_delete_tenant() route to admin.py
Step 6: Add crm_super_archive_tenant() route to admin.py
Step 7: Update crm_super_dashboard.html with new buttons + confirmation modal
Step 8: Run py_compile verification
Step 9: Regression test matrix
Step 10: Update documentation
```

---

## What Comes After Phase 15C

**Phase 16 — Subscription Engine**

- Activate live Razorpay subscription processing
- Auto-create subscriptions when Super Admin creates a tenant
- Webhook processing for payment events
- Trial-to-paid conversion flow

---

*Oxford CRM Documentation — docs/17_ai_context/NEXT_PHASE.md*
*Cross-references: `PROJECT_STATE.md` · `ACTIVE_TASKS.md` · `01_project/ROADMAP.md`*
*This document is fully rewritten after every phase completion.*
