---
KnowledgeID: DOC-MIG-16A6-ROLLBACK
Version: 1.0
Status: ACTIVE
Owner: Architecture Team
Dependencies: [DOC-MIG-16A6-RUNBOOK]
---

# Phase 16.5A6 — Rollback Checklist

**Guiding invariant:** rollback removes ONLY enterprise structures created by the
backfill. Legacy columns (`stage`, `course`, `offer_course`, `batch_time`,
`is_admitted`, `lead_status`) are **never written by the backfill and never touched by
rollback**.

**Why rollback is inherently safe:** setting `pipeline_stage_id = NULL` closes the
adapter gate. Every adapter immediately falls back to its legacy column — which was never
modified. The system returns byte-identical to pre-migration state.

**ADR-020 note:** closing the gate also makes `_sync_offering_link` inert again, so no
further bridge writes can occur once step 2 has run. This is why the unlink MUST precede
the bridge delete.

## Trigger conditions

```
□ Step 7 parity mismatch (F7)                       → MANDATORY rollback
□ Cross-tenant leak detected (V10 > 0)              → MANDATORY rollback
□ V6 admitted_total changed                         → MANDATORY rollback (ADR-018 violated)
□ Analytics deviation reported post-run             → rollback + investigate
□ Operator discretion                               → safe at any time
```

## Order — STRICTLY REVERSE (1 → 6)

No FK uses CASCADE, so an out-of-order delete **fails loudly** instead of destroying data.
Do not "fix" such an error by adding CASCADE.

```
□ 1. Step 6 — custom_attributes .................... NO-OP (do nothing)
       Rationale: written values are byte-identical to the legacy columns, so leaving
       them is read-identical. Backfilled keys are indistinguishable from live bot
       writes (16.5A5-I setters) — deleting them would destroy production data.
       DO NOT "clean up" this table.

□ 2. Step 5 — unlink pipeline_stage_id
       UPDATE conversation_state SET pipeline_stage_id = NULL
       WHERE pipeline_stage_id IN (
         SELECT ps.id FROM pipeline_stages ps
         JOIN pipeline_definitions pd ON pd.id = ps.pipeline_id
         WHERE pd.internal_key = 'legacy_compat'
       );
       ✓ Scoped to compat stages only — never blanket-NULLs the column
       ✓ Legacy stage column untouched
       □ Verify: adapters now read legacy values (spot-check 10 rows)

□ 3. Step 4 — delete bridges
       DELETE FROM conversation_state_offerings
       WHERE offering_id IN (SELECT id FROM offering WHERE tenant_id = :tid);
       ✓ Safe: every bridge belongs to a backfilled row.
         AMENDED (ADR-020): _sync_offering_link is no longer a no-op — the bot
         now creates, repoints, and removes bridges when a lead's course changes.
         It does so ONLY when pipeline_stage_id IS NOT NULL, i.e. only on rows
         this backfill linked. So the claim "all bridges are backfill-created" is
         superseded, but the DELETE remains correct and complete: no bridge can
         exist on a row the backfill did not link.
       ✓ Run this AFTER step 2 (unlink) so no further bridge writes can occur —
         once pipeline_stage_id is NULL the sync hook is inert again.

□ 4. Step 3 — delete unreferenced offerings
       DELETE FROM offering
       WHERE tenant_id = :tid
         AND id NOT IN (SELECT offering_id FROM conversation_state_offerings);
       ✓ Guard clause prevents deleting anything still referenced

□ 5. Step 2 — delete compat stages
       DELETE FROM pipeline_stages
       WHERE pipeline_id IN (SELECT id FROM pipeline_definitions
                             WHERE internal_key = 'legacy_compat' AND tenant_id = :tid);
       ⚠ Requires step 2 above to have completed, else FK violation

□ 6. Step 1 — delete compat pipeline
       DELETE FROM pipeline_definitions
       WHERE internal_key = 'legacy_compat' AND tenant_id = :tid;
```

## Post-rollback verification

```
□ SELECT COUNT(*) FROM conversation_state WHERE pipeline_stage_id IS NOT NULL;  == 0
□ SELECT COUNT(*) FROM conversation_state_offerings;                            == 0
□ SELECT COUNT(*) FROM offering;                                                == 0
□ SELECT COUNT(*) FROM pipeline_stages;                                         == 0
□ SELECT COUNT(*) FROM pipeline_definitions WHERE internal_key='legacy_compat'; == 0
□ SELECT COUNT(*) FILTER (WHERE is_admitted) FROM conversation_state;
      == pre-flight admitted_total  ← MUST be unchanged (never touched)
□ SELECT COUNT(*) FROM conversation_state;   == pre-flight total_leads
□ get_stage_breakdown() matches pre-migration distribution
□ Admin dashboard admissions count matches pre-migration
□ Bot round-trip smoke test: send a message, confirm stage advances
```

## Escalation

```
□ If rollback itself fails → STOP. Do not improvise. Restore the pre-flight snapshot.
□ Never add ON DELETE CASCADE to force a delete through (SCHEMA_RULES §12 FORBIDDEN).
□ Never DELETE/UPDATE a legacy column to "restore" a value — they were never changed.
```
