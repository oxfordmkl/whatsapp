---
KnowledgeID: DOC-BOOT-PHASE
Version: 1.0
Status: ACTIVE
Owner: Architecture Team
---

# CURRENT PHASE

This document tracks the immediate, active, and upcoming execution phases.

## Phase Tracking
- **Current Phase**: Phase 16.5A6-J (Course Adapter Synchronization Correction) - *Complete*
- **Completed Phases**: Phase 1-15, Phase 16.0-16.4, Phase 16.5A1-16.5A4.1, Phase K1.1-K1.3D, K2.1, Phase 16.5A5 + H1/H2/I/J, Phase 16.5A6-P, Phase 16.5A6-LA, Phase 16.5A6-J
- **Active Phase**: Phase 16.5A6 (Enterprise Data Backfill) — **LIVE NOT APPROVED**
- **Blocked Phases**: Phase 16.5A6 LIVE — gated on re-running Phase 16.5A6-LA against the ADR-020 correction

## Phase 16.5A6 Status (updated by 16.5A6-J)
- Production discovery: **COMPLETE** (both NO-GO gates passed — `orphan_leads=0`, `pipeline_stage_id IS NOT NULL=0`).
- Dry run: **COMPLETE** (1 pipeline, 12 stages, 8 offerings, 25 bridges, 29 links, 15 JSON merges; every number cross-checked against SQL).
- LIVE Readiness Audit (16.5A6-LA): **FAIL** — blocking `course` staleness defect.
- Correction (16.5A6-J / ADR-020): **COMPLETE** — 30/30 mutation checks pass.
- **Next gate**: re-run Phase 16.5A6-LA against ADR-020. LIVE stays blocked until it returns PASS.
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
