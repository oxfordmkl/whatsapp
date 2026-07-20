---
KnowledgeID: DOC-BOOT-TECHDEBT
Version: 1.0
Status: ACTIVE
Owner: Architecture Team
---

# KNOWN TECHNICAL DEBT

## 1. `ConversationState` Bloat
- **Current Debt**: The `ConversationState` table contains multi-domain data (marketing tags, education courses, pipeline stages).
- **Deferred Items**: Dropping the physical columns.
- **Legacy Coupling**: Admin UI `.group_by()` queries rely on these columns.
- **Future Refactors**: Drop columns entirely and enforce pure relational joins.
- **Priority**: Medium
- **Risk**: High
- **Target Phase**: Phase 17

## 2. Global State Dependencies
- **Current Debt**: Heavy reliance on `app.state` and in-memory dictionaries for AI conversational context.
- **Deferred Items**: Moving session tracking to Redis.
- **Legacy Coupling**: Bot router uses local memory.
- **Future Refactors**: Implement `Redis` session backend for multi-worker scaling.
- **Priority**: Medium
- **Risk**: Low
- **Target Phase**: Phase 18

## 3. UI Template Duplication
- **Current Debt**: `admin.py` renders huge blocks of HTML inline or reuses fragmented snippets.
- **Deferred Items**: Componentizing the frontend.
- **Legacy Coupling**: Existing JS relies on hardcoded DOM IDs.
- **Future Refactors**: Adopt a structural component framework (e.g., HTMX or React).
- **Priority**: Low
- **Risk**: Low
- **Target Phase**: Phase 19

## 4. Residual `_get_default_tenant_id()` Write Sites (TD-P0-1)
- **Current Debt**: `_get_default_tenant_id()` resolves to `Tenant.query.first()` — in production (10 tenants) this is `amboori`, not the primary tenant. ~20 call sites remain across 9 files (`app/bot/router.py`, `app/state.py`, `app/routes/admin.py`, `app/services/{campaign,followup,log,notification,task,whatsapp}_service.py`) after Phase 17.1-B closed Category B only.
- **Interest Rate**: Already caused a production incident — 25 mis-filed `lead_event` rows, repaired in Phase 17.1-C.
- **Future Refactors**: Thread explicit `tenant_id` through all remaining call paths; forbid the helper in new write paths (ADR-021 rule).
- **Priority**: Critical
- **Risk**: Critical
- **Target Phase**: Phase 0 — Sprint 2

## 5. Missing Tenant-Isolation Test Suite (TD-P0-2)
- **Current Debt**: No test anywhere asserts that cross-tenant read/write fails. Constitution I.1 requires isolation proven by automated tests on every deploy.
- **Interest Rate**: Item 4's defect class is unverifiable; every deploy is an untested isolation bet.
- **Future Refactors**: Isolation suite covering all tenant-scoped models; blocking in CI once CI exists.
- **Priority**: Critical
- **Risk**: Critical
- **Target Phase**: Phase 0 — Sprint 2

## 6. Missing Automated Backup (TD-P0-3)
- **Current Debt**: Backups are manual (runbook `docs/13_operations/BACKUP_RESTORE_RUNBOOK.md`, first restore drill PASSED 2026-07-20). No scheduled nightly backup; no second-provider off-machine copy (Constitution VI.5).
- **Interest Rate**: Data-loss window equals time since last manual dump.
- **Future Refactors**: Scheduled nightly `pg_dump` + second-provider storage + retention ladder.
- **Priority**: High
- **Risk**: Critical
- **Target Phase**: Phase 0 — Sprint 3

## 7. Missing CI Pipeline (TD-P0-4)
- **Current Debt**: No CI of any kind (`.github/workflows/` absent). Constitution VII.4 requires lint → tests → blocking isolation suite on every push.
- **Interest Rate**: Every push deploys straight to production tenants unverified.
- **Future Refactors**: GitHub Actions pipeline; isolation suite (item 5) as blocking gate.
- **Priority**: High
- **Risk**: High
- **Target Phase**: Phase 0 — Sprint 2

## 8. Missing PostgreSQL Row-Level Security (TD-P0-5)
- **Current Debt**: Tenant isolation has one wall (application-level `tenant_id` filtering). Constitution I.1 requires two independent mechanisms; RLS (the second wall) is absent.
- **Interest Rate**: A single missed filter (see item 4) is a cross-tenant exposure with no backstop.
- **Future Refactors**: Enable RLS keyed to a session variable on all tenant-scoped tables; verify via the isolation suite.
- **Priority**: High
- **Risk**: Critical
- **Target Phase**: Phase 0 — Sprint 3 (after items 4 and 5)

## 9. Temporary Diagnostic Logging (TD-P0-6) — RESOLVED
- **Current Debt**: Commit `ab60031` shipped temporary `[DIAG]` routing/Gemini instrumentation to production, logging full inbound message bodies (lead PII) to Railway logs.
- **Resolution**: All `[DIAG]` logging removed in Phase 0 — Sprint 1 (2026-07-20); verified zero occurrences remain in `app/`.
- **Priority**: —
- **Risk**: —
- **Status**: RESOLVED (Sprint 1)

## Required Cross-References
- [Repository Manifest](../REPOSITORY_MANIFEST.md)
- [Master Index](../MASTER_INDEX.md)
- [Knowledge Baseline](../00_meta/KNOWLEDGE_BASELINE_v1.0.md)
- [AI Boot Order](../AI_BOOT_ORDER.md)
- [ADR Index](../21_decision_records/ADR_INDEX.md)
