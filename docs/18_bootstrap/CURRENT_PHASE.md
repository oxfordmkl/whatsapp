---
KnowledgeID: DOC-BOOT-PHASE
Version: 1.0
Status: ACTIVE
Owner: Architecture Team
---

# CURRENT PHASE

This document tracks the immediate, active, and upcoming execution phases.

## Phase Tracking
- **Current Phase**: Phase 16.5A7-B (Task Engine Completion) - *Complete, pending deployment*
- **Completed Phases**: Phase 1-15, Phase 16.0-16.4, Phase 16.5A1-16.5A4.1, Phase K1.1-K1.3D, K2.1, Phase 16.5A5 + H1/H2/I/J, Phase 16.5A6-P, Phase 16.5A6-LA, Phase 16.5A6-J, Phase 16.5A6 (LIVE), Phase 16.5A7, Phase 16.5A7-A (audit), **Phase 16.5A7-B**
- **Active Phase**: None
- **Blocked Phases**: None

## Phase 16.5A7 / 16.5A7-B Status — COMPLETE (code), PENDING DEPLOYMENT
- Migration `c7a2f19d4e88` (tasks + notifications) is **not yet applied to production**. Requires `flask db upgrade` + deployment approval. Unchanged by 16.5A7-B.
- 16.5A7 closed two live pre-existing defects: task-creation RBAC gap and task/reassignment tenant misassignment.
- **16.5A7-A audit returned NO-GO** on two blocking defects; **16.5A7-B closed both**:
  - **B1** staff-to-staff escalation (hijack + credit theft) → `_authorize_mutation()`; unassigned tasks are admin-only; SUPER_ADMIN impersonation now consistent with `tenant_query()`.
  - **B2** Task Engine write-only (invisible edits, zombie deletes) → `get_all_tasks()` is Task-table-first with a legacy compatibility layer; return contract unchanged.
- Validation: 136 checks green (42 16.5A7-B + 64 16.5A7 + 30 16.5A6-J); 32/32 templates; app boots.
- **Re-audit (16.5A7-A) recommended before deployment.**

## Non-blocking items deferred by 16.5A7-B (explicitly out of scope)
N2 notification retention · N3 60s badge polling · N4 `REMINDER_DUE` has no producer · websockets.

## Open Item — Data Repair Required (NOT done by 16.5A7)
`_get_default_tenant_id()` (`Tenant.query.first()` → `amboori`) mis-filed **18 production `lead_event` rows**
under the wrong tenant: 2 `FOLLOW_UP_TASK`, 4 `FOLLOW_UP_COMPLETED`, 7 `LEAD_REASSIGNED`, 5 `MANUAL_MESSAGE`.
The write paths are fixed going forward (ADR-021), but the existing rows remain invisible to
`oxford-computers` and exposed to `amboori`. Repairing them needs its own approved migration.
**Systemic scope (larger than 16.5A7):** `tenant_id=_get_default_tenant_id()` remains on **12 other write
sites** (`admin.py` ×9, `campaign_service`, `followup_service`, `state.py`, `whatsapp_service`, `bot/router.py`).
16.5A7 corrected only the 2 sites on the task / lead-reassignment path. Some remaining sites may be legitimate
(the bot has no `current_user` and must resolve its tenant from the WABA phone_number_id instead), others are
likely the same defect. A dedicated tenant-resolution audit is recommended before further phases.

## Phase 16.5A6 Status — COMPLETE (executed 2026-07-17)
- Production discovery: **COMPLETE** — both NO-GO gates passed.
- Dry run: **COMPLETE** — plan cross-checked against SQL.
- LIVE Readiness Audit (16.5A6-LA): **FAIL** → blocking `course` staleness defect.
- Correction (16.5A6-J / ADR-020): **COMPLETE** — 30/30 mutation checks.
- 16.5A6-LA re-run: **PASS — LIVE APPROVED**.
- Verified backup: `oxfordcrm_before_backfill.dump` (integrity + completeness verified).
- **LIVE EXECUTED**: 1 pipeline, 12 stages, 8 offerings, 25 bridges, 29 links, 15 JSON merges. 84.4s, online, zero downtime.
- **V1–V11 all pass.** V6 (`admitted_total` 7==7) and V8 (`internal_key` drift 0) hard gates confirmed. V11 idempotency proven by a zero-delta 2nd run.

## Next Phase
- **Phase 16.5A7 (Audience Engine)** — unblocked; the relational layer it depends on is now populated.
- **Deferred Phases**: Dropping legacy `course`/`stage` columns from `ConversationState` table (Deferred to K2/Phase 17 to preserve `group_by` reporting).
- **Upcoming Phases**: Phase 16.5A6 (Data Backfill), Phase 16.5A7 (Audience Engine)

## Phase 16.5A6 Preconditions (from the 16.5A5-J correction)
1. Seed the **Compatibility Pipeline** using the exact 12 legacy stage keys (ADR-019).
2. Link `pipeline_stage_id` from the legacy `stage` string ONLY. **Do not touch `is_admitted`** (ADR-018).
3. Create `Offering` rows with `name` = exact legacy `course` string; dedupe on exact `(tenant_id, name)` (ADR-019).
4. Obtain production discovery first: row counts, distinct `stage` values per tenant (including any outside the known 12), and `is_admitted=True` totals. No production DB access exists in the dev environment.

## Current Milestone
- **Milestone**: Transition the legacy `ConversationState` entity to the new multi-tenant relational models without breaking the current UI queries, utilizing a dual-write ORM adapter pattern.

## Exit Criteria
- `filter_by()` refactored to `.filter()`.
- Dual-write `@property` setters safely deployed.
- AI Router correctly sets `stage` which propagates to `pipeline_stage_id`.
- Dashboard list views maintain performance without N+1 regression (`joinedload`).

## Required Cross-References
- [Repository Manifest](../REPOSITORY_MANIFEST.md)
- [Master Index](../MASTER_INDEX.md)
- [Knowledge Baseline](../00_meta/KNOWLEDGE_BASELINE_v1.0.md)
- [AI Boot Order](../AI_BOOT_ORDER.md)
- [ADR Index](../21_decision_records/ADR_INDEX.md)
