---
KnowledgeID: DOC-ADR-018
Version: 1.0
Status: ACTIVE
Owner: Architecture Team
Dependencies: [DOC-META-BASELINE, DOC-ADR-012, DOC-ADR-013, DOC-ADR-014]
Supersedes: Enterprise Data Model Freeze v1.1 §7 (is_admitted row)
---

# ADR-018: Business Conversion Independence

## Status
ACTIVE — Ratified Phase 16.5A5-J (2026-07-17)

## Context

The Enterprise Data Model Freeze (§7 Backward Compatibility Matrix) and the
`PipelineStage` model docstring both asserted:

> `is_admitted` → `PipelineStage` → `@property` checks `stage.category == 'won'`
> Legacy `is_admitted=True` → `stage_category='won'`

Phase 16.5A5-I implemented this as a `hybrid_property` derived from
`pipeline_stage_id`. Phase 16.5A6 discovery **disproved the underlying assumption**
before any production data was migrated.

### Repository evidence

Ownership of the two attributes is completely disjoint:

| Attribute | Written by | Never written by |
| :--- | :--- | :--- |
| `stage` | AI router only — 20 call sites in `app/bot/router.py`, 1 in `app/bot/objections.py` (`st["stage"] = ...`) | admin / staff |
| `is_admitted` | Staff form only — `app/routes/admin.py:1335` (`lead.is_admitted = new_admitted`) | the bot |

No code path couples them. `app/routes/admin.py:1332` promotes `lead_status` on
admission — **not** `stage`. The bot sets `stage="enrolled"`
(`app/bot/router.py:434`) without ever touching `is_admitted`.

### The disproof

`stage` and `is_admitted` are independent facts that legitimately disagree:

- `stage="new"`, `is_admitted=True` — staff admitted a lead the bot never advanced.
- `stage="enrolled"`, `is_admitted=False` — the bot completed its flow; staff have
  not yet ticked the admission box.

`ConversationState` has exactly **one** `pipeline_stage_id` FK. A single FK cannot
reproduce two independent values. Every possible `stage_category` assignment breaks
one of them:

| Row | Link by `stage` | Link to a `'won'` stage |
| :--- | :--- | :--- |
| `stage="new"`, `is_admitted=True` | category `'open'` → `is_admitted` reads **False** → admissions count drops; revenue analytics break | `stage` reads `"enrolled"` ≠ `"new"` → router state machine breaks |
| `stage="enrolled"`, `is_admitted=False` | if `"enrolled"` is `'won'` → reads **True** ≠ False → phantom admissions inflate analytics | if no stage is `'won'` → every real admission erased |

This is arithmetic, not an implementation-quality problem.

## Decision

**`is_admitted` is an independent business attribute and is NEVER derived from
`PipelineStage.stage_category`.**

- `ConversationState.is_admitted` is a plain mapped `db.Boolean` column.
- It has **no** ORM adapter, no `hybrid_property`, and no relational counterpart.
- `PipelineStage.stage_category` is retained and remains valid for future
  relational KPI dashboards, but it **does not and must not drive `is_admitted`**.

Conceptually: the pipeline models *"where in the funnel is this lead"*.
`is_admitted` records *"did this lead convert"* — a separate, staff-owned business
fact. Conflating position with conversion was the error.

## Consequences

- Freeze §7 `is_admitted` row is superseded by this ADR.
- The Phase 16.5A5-I `is_admitted` hybrid adapter (getter/setter/expression) and
  the `_sync_admitted_link` helper are removed; the column reverts to its original
  plain form. SQL emitted for `is_admitted` returns to a direct indexed column
  read — identical to pre-16.5A5-I production, and cheaper than the interim CASE.
- Phase 16.5A6 backfill may safely set `pipeline_stage_id` from the legacy `stage`
  alone; `is_admitted` is untouched by the backfill.
- Admissions analytics (`admin.py:550`, `:4219`, `:4400`, `:4579`) are unaffected.
- Any future "conversion" semantics on the pipeline must be introduced as an
  additive, separately-named concept — never by redefining `is_admitted`.

## Governance note

This ADR is the required outcome of the standing rule that enterprise correctness
outranks historical documentation. The freeze was authored before the adapter was
implemented; implementation discovery disproved it. Reality was not changed to
satisfy the document — the document was corrected to match reality.

## References
- Phase 16.5A6 Discovery Report (NO-GO — blocking conflict)
- Phase 16.5A5-I Implementation Report (adapter now partially superseded)
- ADR-019 (Compatibility Pipeline Standard)
- `docs/04_DATABASE/OXFORD_CRM_ENTERPRISE_DATA_MODEL_FREEZE_v1.0.md` (v1.2)
