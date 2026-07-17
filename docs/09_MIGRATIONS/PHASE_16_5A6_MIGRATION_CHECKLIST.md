---
KnowledgeID: DOC-MIG-16A6-CHECKLIST
Version: 1.0
Status: ACTIVE
Owner: Architecture Team
Dependencies: [DOC-MIG-16A6-RUNBOOK, DOC-ADR-018, DOC-ADR-019]
---

# Phase 16.5A6 — Migration Checklist

Companion to `PHASE_16_5A6_ENTERPRISE_BACKFILL_RUNBOOK.md`. Do not deviate.
**If any box cannot be ticked, STOP.**

## A. Pre-flight (blocking)

```
□ Railway PostgreSQL snapshot taken and restore verified
□ §9 discovery SQL executed on production; results recorded below
□ D8 orphan_leads == 0                          (else NO-GO — F4)
□ D7 pipeline_stage_id IS NOT NULL == 0         (else NO-GO — investigate)
□ D2/D3 stage vocabulary reviewed; unlinkable values acknowledged
□ D6 slug-collision census reviewed (confirms ADR-019 suffix logic)
□ D4 admitted_total recorded ........................ = ______  ← V6 anchor
□ D1 total_leads recorded ........................... = ______
□ Migration head confirmed: flask db current == b6e1d4f82c9e
□ Deployed commit includes ADR-018 correction (a2fdb55 or later)
□ Confirmed: engine code contains ZERO references to is_admitted (ADR-018)
□ Confirmed: stage internal_keys are the exact 12 legacy strings (ADR-019)
□ Confirmed: Offering.name written verbatim; dedup by exact (tenant_id, name)
□ Dry-run executed against a restored snapshot; Step 7 parity == 100%
```

## B. Code review gates (ADR compliance)

```
□ Step 5 does NOT read or write is_admitted            ← ADR-018 (hard gate)
□ Step 5 does NOT read stage_category                  ← ADR-018 (hard gate)
□ Step 5 leaves pipeline_stage_id NULL when stage is NULL/''/unlinkable
□ Step 3 performs NO lowercasing/trimming/normalization of name
□ Step 3 dedup key is exact (tenant_id, name) — NOT internal_key, NOT slug
□ Step 6 MERGES custom_attributes; never overwrites an existing key
□ Stage/Offering maps preloaded once per tenant (no N+1)
□ Keyset pagination (id > last_seen), not OFFSET
□ Every query filtered by tenant_id
□ Single-threaded execution; one tenant at a time
□ Batch size == 500; one commit per batch
```

## C. Execution

```
□ Run order is exactly Step 1 → 2 → 3 → 4 → 5 → 6 → 7
□ Legacy snapshot captured BEFORE Step 5 (parity baseline)
□ Per-tenant progress logged (created/reused/skipped/unlinkable)
□ No maintenance window needed — online-safe (F8); bot may run
□ Halt immediately on any Step 7 mismatch → Rollback Checklist
```

## D. Post-execution (see Production Verification Checklist)

```
□ V1–V11 all pass
□ V6 admitted_total == pre-flight value ............. EXACT MATCH REQUIRED
□ V10 cross-tenant leak detectors both return 0
□ V11 second run reports 0 creates / 0 links (idempotency proof)
□ Results appended to IMPLEMENTATION_HISTORY.md
□ REPOSITORY_MANIFEST.md phase pointer updated
```

## E. Recorded discovery values

| Key | Value | Source |
| :--- | :--- | :--- |
| total_leads | | D1 |
| active_tenants | | D1 |
| admitted_total | | D4 |
| distinct_courses | | D6 |
| unlinkable_stage_values | | D3 |
| null_or_empty_stage | | D3 |
| rows_with_custom_attributes | | D7 |
| would_have_been_erased | | D5 |
| would_have_been_phantom | | D5 |

> D5 values quantify the production damage ADR-018 prevented. Record for the
> architecture record even though they no longer represent risk.
