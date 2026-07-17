---
KnowledgeID: DOC-ADR-020
Version: 1.0
Status: ACTIVE
Owner: Architecture Team
Dependencies: [DOC-META-BASELINE, DOC-ADR-013, DOC-ADR-018, DOC-ADR-019]
Supersedes: Phase 16.5A5-I `_sync_offering_link` no-op contract
---

# ADR-020: Course–Offering Synchronization

## Status
ACTIVE — Ratified Phase 16.5A6-J (2026-07-17)

## Context

Phase 16.5A5-I shipped `ConversationState.course` as a dual-write
`hybrid_property` adapter. Its sync hook was deliberately left inert:

```python
def _sync_offering_link(self, value):
    """...nothing to keep in sync here yet, so this is a documented no-op
    until the relational layer is active."""
    return
```

The reasoning was sound *at the time*: every production row had
`pipeline_stage_id = NULL`, so the adapter gate was closed, the getter always
returned the legacy `_course` column, and the bridge was irrelevant. The no-op
was also load-bearing for the Phase 16.5A6 Rollback Checklist, which asserts
"all bridges are backfill-created, so the bot never creates bridges."

The Phase 16.5A6-LA LIVE Readiness Audit disproved the assumption that this
remains safe **after** backfill.

### The defect

Phase 16.5A6 Step 5 sets `pipeline_stage_id`, which **opens the adapter gate**.
The `course` getter then resolves through the bridge:

```python
if self.pipeline_stage_id is not None:
    off = self._first_offering()
    if off is not None:
        return off.name          # <- relational read
return self._course              # <- legacy fallback
```

The router writes `course` at four sites (`app/bot/router.py:219, 396, 443,
457`). The write funnels through `StateProxy.__setitem__` → `_db_save` →
`setattr(row, 'course', v)` (`app/state.py:49`) → the `course` setter →
`_sync_offering_link()` → **no-op**. The bridge is never repointed, so the getter
keeps returning the **previous** `Offering.name`.

Reproduced deterministically:

```
adapter        wrote                  read back              verdict
stage          demo_booked            demo_booked            PASS
course         Python Programming     PGDCA                  *** FAIL — STALE ***
offer_course   NEWOFFER               NEWOFFER               PASS
batch_time     Evening (6-8 PM)       Evening (6-8 PM)       PASS
```

Only `course` breaks, because it is the only adapter whose sync hook is inert:
`stage` has a working `_sync_stage_link`; `offer_course` and `batch_time` fully
resync through `_set_custom_attr`.

### Why migration validation cannot catch it

The corruption does not exist at migration time — bridge and column agree when
Step 7 runs. It materialises on the **next bot course-write**, hours or days
later. Phase 16.5A6 would report 100% parity and GO, then silently corrupt data
afterwards with no detector watching. Severity is therefore higher than the
symptom suggests: a green migration is not evidence of safety.

### Repository evidence

`course` has exactly one write funnel. Discovery found **no** direct attribute
assignment, **no** bulk `.update({...})`, **no** raw SQL `UPDATE`, and no
import/CLI/scheduler/automation write path. `app/routes/admin.py:1319` reads
`lead.course`; `normalize_course_name` (`app/bot/constants.py:40`) is documented
read-time-only with zero DB writes. Correcting the setter therefore corrects
every path.

## Decision

**`_sync_offering_link` is implemented symmetrically with `_sync_stage_link`: the
course→Offering bridge MUST track the legacy `course` column whenever the row is
relationally activated.**

Contract:

1. **Gate-closed no-op.** `pipeline_stage_id IS NULL` → return immediately. The
   getter reads `_course`; the bridge is irrelevant. Pre-backfill behaviour is
   bit-for-bit unchanged, and the sync can never activate on un-migrated rows.
2. **Unsaved no-op.** `id IS NULL` → return. No bridge can exist to be stale.
3. **Exact-name reuse (ADR-019).** Offerings are matched on the exact
   `(tenant_id, name)` pair — no normalization, no case-folding, no slug. The
   `tenant_id` filter is the tenant-isolation boundary.
4. **Never mints an Offering.** Creating enterprise rows is backfill's job.
   Offerings are only ever reused.
5. **At most one bridge while the gate is open** — the Offering matching
   `_course`. Obsolete and duplicate links are removed.
6. **Fail-safe on no-match.** When no Offering matches (empty course, or a course
   with no Offering), the stale bridge is **removed** so `_first_offering()`
   returns `None` and the getter falls back to `_course`.

### Deliberate divergence from `_sync_stage_link`

`_sync_stage_link` leaves the existing link untouched when no stage matches.
`_sync_offering_link` **removes** it instead. This asymmetry is intentional:

- Every stage the router writes is guaranteed to exist — Phase 16.5A6 seeds all
  twelve canonical keys, so a stage no-match cannot occur in practice.
- `course` carries **no such guarantee**. The bot can assign any of the ten
  `ALL_COURSES` entries (`app/bot/constants.py:133`), while only courses actually
  present in production data receive an Offering. At ratification, `GST & Payroll`
  and `DCA Fast Track` are assignable but have **no** Offering.

Leaving the link on a no-match would return a stale course. Removing it engages
the legacy fallback, which is always correct. This is the Fail-Safe Property
(Runbook §1) applied to writes: **when in doubt, fall back.**

## Consequences

- The Phase 16.5A6-LA blocking defect is resolved. All four adapters round-trip
  under the gate; verified by 30 deterministic mutation checks in
  `tests/test_adapter_sync_16_5a6j.py`.
- **The Rollback Checklist rationale changes.** The bot can now create and delete
  bridges — but only for rows the backfill already linked (`pipeline_stage_id IS
  NOT NULL`). Every bridge still belongs to a backfilled row, so the rollback
  action (delete bridges for tenant leads) remains correct; only its stated
  justification is amended. Rollback safety is **preserved**, not weakened:
  unlinking still restores exact legacy behaviour because no legacy column is
  ever written.
- **Multi-offering semantics are constrained while the gate is open.** The legacy
  `course` adapter is a 1:1 shim over an M:M bridge and cannot round-trip a
  multi-bridge state. Any genuine multi-offering capability must be introduced as
  an additive, separately-named concept — never by overloading the legacy `course`
  shim. (This mirrors ADR-018's rule for `is_admitted`.) No production data is
  affected: all bridges today are backfill-created, exactly one per lead.
- Write cost while the gate is open: one indexed bridge SELECT plus one indexed
  Offering SELECT per `course` write. Course writes are user-paced (one per
  inbound WhatsApp message), so this is immaterial.
- `is_admitted` is untouched (ADR-018) and `stage` logic is unchanged.
- Phase 16.5A6 LIVE remains **NOT APPROVED** until Phase 16.5A6-LA is re-run
  against this correction.

## Governance note

This is the second time implementation discovery has disproven a frozen
assumption in this workstream (after ADR-018). The pattern is identical and the
rule held both times: enterprise correctness outranks historical documentation.
The audit was designed to be capable of failing, and it did — before production
data was touched, not after.

## References
- Phase 16.5A6-LA LIVE Readiness Audit (FAIL — blocking defect D1)
- ADR-018 (Business Conversion Independence)
- ADR-019 (Compatibility Pipeline Standard)
- `app/models.py` (`_sync_offering_link`, `course` adapter)
- `app/bot/router.py:219,396,443,457`, `app/state.py:35-52`
- `tests/test_adapter_sync_16_5a6j.py`
- `docs/04_DATABASE/OXFORD_CRM_ENTERPRISE_DATA_MODEL_FREEZE_v1.0.md` (v1.3)
