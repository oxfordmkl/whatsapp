---
KnowledgeID: DOC-MIG-16A6-OPS
Version: 1.0
Status: ACTIVE
Owner: DevOps / Architecture Team
Dependencies: [DOC-MIG-16A6-RUNBOOK, DOC-MIG-16A6-CHECKLIST, DOC-MIG-16A6-ROLLBACK, DOC-MIG-16A6-VERIFY]
---

# Phase 16.5A6 — Operational Runbook

Operator-facing. For design rationale see
`PHASE_16_5A6_ENTERPRISE_BACKFILL_RUNBOOK.md`.

## 1. What this migration does

Populates the Enterprise Configuration layer from legacy `ConversationState` strings:

| Creates | Updates | **Never touches** |
| :--- | :--- | :--- |
| `pipeline_definitions` (1/tenant) | `conversation_state.pipeline_stage_id` | `stage`, `course`, `offer_course`, `batch_time` |
| `pipeline_stages` (12+/tenant) | `conversation_state.custom_attributes` (merge) | **`is_admitted`** (ADR-018) |
| `offering` (1/distinct course) | | `lead_status` |
| `conversation_state_offerings` | | any legacy column |

**It adds zero leads and deletes nothing.**

## 2. Operational properties

| Property | Value |
| :--- | :--- |
| Downtime required | **None** — safe to run online |
| Maintenance window | **Not required** |
| Bot may run during migration | **Yes** (F8 — the 16.5A5-I setters dual-write and stay consistent) |
| Schema changes | **None** — no DDL, no Alembic revision |
| Locks | Row-level only, ≤500 rows per batch, released each commit |
| Restartable | **Yes** — safe to Ctrl-C at any point and re-run |
| Re-runnable | **Yes** — idempotent; 10 runs == 1 run |

## 3. The safety property operators must understand

Adapters are gated on `pipeline_stage_id`:
- `pipeline_stage_id IS NULL` → reads the **legacy column** (today's behaviour)
- `pipeline_stage_id IS NOT NULL` → reads the **relational model**

Therefore **an unmigrated row is a correct row.** If the engine skips a row, aborts a
tenant, or crashes mid-run, everything it did not migrate still behaves exactly as it does
today. There is no half-broken state.

**Corollary:** if anything looks wrong, unlinking (`SET pipeline_stage_id = NULL`)
instantly restores legacy behaviour. That is the whole of rollback Step 5.

## 4. Pre-flight

```bash
# 1. Snapshot production and VERIFY THE RESTORE
#    (Railway → Database → Backups). Non-negotiable — SCHEMA_RULES §11.

# 2. Confirm migration head
flask db current          # expect: b6e1d4f82c9e (head)

# 3. Confirm the ADR-018 correction is deployed
git log --oneline -1      # expect a2fdb55 or later

# 4. Run the read-only discovery (Runbook §9) and complete the
#    Migration Checklist §E table. THIS IS BLOCKING.
```

**Stop conditions:** `orphan_leads > 0`, or `pipeline_stage_id IS NOT NULL > 0`.

## 5. Execution

```bash
# Dry run against a RESTORED SNAPSHOT first — never first-run on production.
#   → Step 7 parity must be 100%

# Production run (online; no window needed):
#   Order is fixed: Step 1 → 2 → 3 → 4 → 5 → 6 → 7
#   Batch: 500 · single-threaded · one tenant at a time
#   A legacy snapshot MUST be captured before Step 5 (parity baseline)
```

Monitor per tenant:

```
tenant=<id> pipelines(created/reused)=… stages(created/reused/extra)=…
offerings(created/reused/collisions)=… bridges(created/skipped)=…
linked=… skipped_already=… unlinkable=… attrs(enriched/preserved)=…
```

## 6. Interpreting the report

| Metric | Healthy | Investigate |
| :--- | :--- | :--- |
| `rows_unlinkable` > 0 | ✅ Expected if D3 found values | Cross-check against D3 count |
| `attrs_preserved` > 0 | ✅ Expected — live bot JSON since 16.5A5-I | — |
| `*_reused` > 0 on first run | ⚠️ | Enterprise tables were not empty |
| `slug_collisions_resolved` > 0 | ✅ Expected if D6 found collisions | Confirms ADR-019 |
| `rows_linked` == 0 on 2nd run | ✅ Idempotency proven | If > 0 → **defect** |
| **Any `is_admitted` metric** | ❌ **Must not exist** | Engine violates ADR-018 → halt |

## 7. Abort / rollback

```
Ctrl-C at any time → current batch rolls back; completed batches persist and are
                     correct (fail-safe property §3). Re-run to resume.

Parity mismatch (Step 7)  → STOP. Execute PHASE_16_5A6_ROLLBACK_CHECKLIST.md
is_admitted count drifted → STOP. ADR-018 violated. Roll back immediately.
Cross-tenant leak (V10)   → STOP. Roll back immediately.
```

Fastest safe mitigation (restores legacy behaviour without deleting anything):

```sql
UPDATE conversation_state SET pipeline_stage_id = NULL
WHERE pipeline_stage_id IN (
  SELECT ps.id FROM pipeline_stages ps
  JOIN pipeline_definitions pd ON pd.id = ps.pipeline_id
  WHERE pd.internal_key = 'legacy_compat'
);
```

Then run the full Rollback Checklist to remove the enterprise rows.

## 8. Post-migration

```
□ Complete PHASE_16_5A6_PRODUCTION_VERIFICATION_CHECKLIST.md (V1–V11)
□ Hard gates: V6 (is_admitted unchanged) and V8 (internal_key == legacy stage)
□ Bot smoke test: send "hi" → stage advances → re-read round-trips
□ Watch admissions count + stage breakdown for 24h
□ Retain snapshot ≥7 days
□ Update IMPLEMENTATION_HISTORY.md and REPOSITORY_MANIFEST.md
```

## 9. Escalation

| Symptom | Action |
| :--- | :--- |
| Bot replies with wrong flow / restarts conversations | `stage` round-trip broken → unlink (§7 SQL) immediately → V8 |
| Admissions count changed | ADR-018 violated → roll back → audit engine for `is_admitted` |
| Course names changed in CRM | ADR-019 violated → check for normalization/slug dedup in Step 3 → roll back |
| Rollback fails | **STOP. Do not improvise. Restore snapshot.** Never add CASCADE. |

## 10. Known-safe conditions (do not escalate)

- `pipeline_stage_id IS NULL` on some rows → **by design** (empty/unlinkable stage §3.3)
- `custom_attributes` non-NULL before the run → **expected** (live bot writes)
- `stage_category='open'` on `done`/`not_sure` → **deliberate** (Runbook §3.2)
- All stages `open` except `enrolled` → **deliberate**; categories are informational
  and cannot affect `is_admitted` (ADR-018)
