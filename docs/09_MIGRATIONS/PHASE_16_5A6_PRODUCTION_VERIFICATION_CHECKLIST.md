---
KnowledgeID: DOC-MIG-16A6-VERIFY
Version: 1.0
Status: ACTIVE
Owner: Architecture Team
Dependencies: [DOC-MIG-16A6-RUNBOOK, DOC-ADR-018, DOC-ADR-019]
---

# Phase 16.5A6 — Production Verification Checklist

Run after every backfill execution. **Any failure → Rollback Checklist.**

## V1–V6 — Data parity (vs pre-Step-5 snapshot)

```
□ V1  Row count unchanged
      SELECT COUNT(*) FROM conversation_state;  == pre-flight total_leads
      (the backfill adds ZERO leads — it only UPDATEs)

□ V2  Stage parity — 100% of migrated rows
      adapter cs.stage == snapshot.stage      for every row

□ V3  Course parity
      adapter cs.course == snapshot.course

□ V4  Offer parity
      adapter cs.offer_course == snapshot.offer_course

□ V5  Batch-time parity
      adapter cs.batch_time == snapshot.batch_time

□ V6  is_admitted parity  ★ ADR-018 HARD GATE ★
      SELECT COUNT(*) FILTER (WHERE is_admitted) FROM conversation_state;
      == pre-flight admitted_total  → MUST BE EXACTLY EQUAL
      The backfill never writes is_admitted. Any drift means the engine
      violated ADR-018 → IMMEDIATE ROLLBACK.
```

## V7 — Analytics parity

```
□ Admissions count (admin.py:550)
    tenant_query(ConversationState, tid).filter(ConversationState.is_admitted == True).count()
    == pre-migration value

□ Staff analytics sum (admin.py:4400)
    func.sum(case((ConversationState.is_admitted == True, 1), else_=0))
    == pre-migration value

□ Stage breakdown (state.py:136)
    get_stage_breakdown() == pre-migration distribution

□ Distinct stages (admin.py:824)
    SELECT DISTINCT stage  → returns legacy strings ONLY (no internal_key drift)

□ group_by(assigned_staff, lead_status) (admin.py:3550/3599)
    unchanged — lead_status is not in scope
```

## V8 — Router compatibility ★ ADR-019 HARD GATE ★

```
□ Every linked row's stage internal_key == its legacy stage string:
    SELECT COUNT(*) FROM conversation_state cs
    JOIN pipeline_stages ps ON ps.id = cs.pipeline_stage_id
    WHERE ps.internal_key IS DISTINCT FROM cs.stage;
    → MUST BE 0

□ Live smoke test (staging or a disposable production number):
    □ Send "hi"      → bot replies; stage advances to goal_selection
    □ Send "demo"    → stage advances to demo_time_ask
    □ Re-read lead   → stage round-trips to the exact legacy string
    □ Confirm pipeline_stage_id followed the legacy write (setter sync)
```

## V9 — Scheduler / marketing compatibility

```
□ follow_up_jobs processing unaffected (no coupling to migrated fields)
□ Pending follow-ups still fire
□ Campaign/broadcast audience counts unchanged
```

## V10 — Tenant isolation (both MUST return 0)

```
□ Lead linked to another tenant's stage:
    SELECT COUNT(*) FROM conversation_state cs
    JOIN pipeline_stages ps      ON ps.id = cs.pipeline_stage_id
    JOIN pipeline_definitions pd ON pd.id = ps.pipeline_id
    WHERE pd.tenant_id <> cs.tenant_id;

□ Lead bridged to another tenant's offering:
    SELECT COUNT(*) FROM conversation_state_offerings b
    JOIN conversation_state cs ON cs.id = b.conversation_state_id
    JOIN offering o            ON o.id  = b.offering_id
    WHERE o.tenant_id <> cs.tenant_id;
```

## V11 — Idempotency proof

```
□ Re-run the FULL migration immediately after a successful run
□ Report must show:
    pipelines_created   == 0
    stages_created      == 0
    offerings_created   == 0
    bridges_created     == 0
    rows_linked         == 0
    attrs_enriched      == 0
□ Row counts identical across all 5 enterprise tables
□ Run a 3rd time — still zero deltas
   ("Running the migration 10 times must produce exactly the same database.")
```

## Integrity / structural

```
□ Exactly one 'legacy_compat' pipeline per active tenant
    SELECT tenant_id, COUNT(*) FROM pipeline_definitions
    WHERE internal_key='legacy_compat' GROUP BY tenant_id HAVING COUNT(*) > 1;  → 0 rows

□ No duplicate Offering names within a tenant (ADR-019 dedup held):
    SELECT tenant_id, name, COUNT(*) FROM offering
    GROUP BY tenant_id, name HAVING COUNT(*) > 1;  → 0 rows

□ Unlinkable rows accounted for:
    SELECT COUNT(*) FROM conversation_state WHERE pipeline_stage_id IS NULL;
    == (null_or_empty_stage + unlinkable_rows) from discovery
    ✓ These rows are CORRECT by design (fail-safe fallback), not failures.
```

## Sign-off

```
□ All V1–V11 pass
□ V6 and V8 (the two hard gates) explicitly confirmed
□ Results recorded in IMPLEMENTATION_HISTORY.md
□ Snapshot retained ≥7 days post-migration
```
