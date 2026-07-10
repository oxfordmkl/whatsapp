# ADR-011: Tenant Lifecycle and Data Permanence Policy
## Architectural Decision Record

> **Version:** 1.0 | **Phase:** 15C.3 | **Audience:** Architects, Developers
> **Reading Time:** 2 min | **Owner:** Project Leadership
> **Last Updated:** 2026-07-10 | **Dependencies:** None

---

## 1. Context
Phase 15C.3 initiated discovery on how to manage the lifecycle of a production tenant (Create, Archive, Delete). The production Tenant model functions as the unbreakable, non-nullable root of the entire database hierarchy (Users, Conversations, Leads, Messages, Billing).

## 2. Technical Constraints
The database uses strict Foreign Key constraints without cascading deletes. Hard-deleting a Tenant row throws an `IntegrityError` unless all dependent records are manually destroyed first.

## 3. Decision
Project Leadership formally approves the following Data Permanence and Lifecycle Policy:
- **No Hard Delete:** Normal Tenant Management must not hard-delete tenants.
- **No Soft Delete:** Phase 15C.3 will not implement soft delete (`status='DELETED'`).
- **No Archive Status:** Phase 15C.3 will not introduce an `ARCHIVED` status.
- **Operational Closure:** The existing `SUSPENDED` ↔ `ACTIVE` lifecycle remains the primary reversible operational disable/restore mechanism.
- **Data Preservation:** Tenant-linked historical data is preserved during operational suspension.
- **No Manual Provisioning:** Phase 15C.3 will not implement manual Super Admin provisioning.
- **Creation Deferred:** Tenant creation policy is deferred to Phase 15C.4 (Tenant Registration) discovery.

## 4. Explicit Non-Decisions
- Billing behavior during suspension is not verified or decided in Phase 15C.3 (deferred to 15C.6).
- Legal or compliance retention periods (GDPR/DPDP) are not defined here.
- Public registration (`/register`) is not approved for launch; it merely exists and requires Phase 15C.4 audit.

## 5. Consequences
- **Pros:** Guarantees zero data loss from accidental clicks. Safely uses existing robust models and middleware.
- **Cons:** A suspended tenant still exists in the database and list views. 

## 6. Known Risks
- Some background outbound workers may not consistently enforce `Tenant.status` before execution. A SUSPENDED tenant may still have previously scheduled outbound work processed (Verified Code-Path Risk).
- The `Tenant.status` contract has inconsistencies (e.g., `PENDING` used but not documented, `DELETED` documented but not used). This is recorded but left untouched.

## 7. Future Review Boundaries
Full tenant data erasure is not normal Tenant Management. It belongs to a separate future DATA GOVERNANCE / PRIVACY / ERASURE WORKFLOW phase.
