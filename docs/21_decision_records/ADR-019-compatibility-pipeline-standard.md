---
KnowledgeID: DOC-ADR-019
Version: 1.0
Status: ACTIVE
Owner: Architecture Team
Dependencies: [DOC-META-BASELINE, DOC-ADR-012, DOC-ADR-018]
---

# ADR-019: Compatibility Pipeline Standard

## Status
ACTIVE — Ratified Phase 16.5A5-J (2026-07-17)

## Context

The Enterprise Data Model Freeze envisages an industry-agnostic default pipeline
with `internal_key` examples such as `"lead"`, `"demo"`, `"admitted"`,
`"purchased"`. Phase 16.5A6 discovery showed that adopting those keys would break
production on the first inbound message.

### Repository evidence

`app/bot/router.py` is a state machine that dispatches on **exact legacy stage
string literals**:

```python
stage = st["stage"]                                   # router.py:214
if low in GREETING_WORDS and stage in ("new", "done", "enrolled", "goal_selection"):
    ...                                               # router.py:230
```

`st` is a `StateProxy` built from `ConversationState.to_dict()`, which reads
`self.stage` — the Phase 16.5A5-I adapter. Once a row is relationally activated
(`pipeline_stage_id IS NOT NULL`), that adapter returns
`PipelineStage.internal_key`. If `internal_key` is not byte-identical to the legacy
string:

1. **Reads break** — `to_dict()["stage"]` returns `"lead"` where the router expects
   `"new"`; every branch mis-fires.
2. **Writes break** — `st["stage"] = "demo_booked"` routes to the setter, whose
   `_sync_stage_link("demo_booked")` looks up a `PipelineStage` by
   `internal_key="demo_booked"`. If the seeded key is `"demo"`, the lookup returns
   `None`, `pipeline_stage_id` keeps pointing at the stale stage, and the row now
   reads back a value that disagrees with the legacy column it just wrote.
3. **Analytics break** — `app/state.py:136` (`get_stage_breakdown`) counts by the
   twelve literal legacy strings; `app/routes/admin.py:731` filters on `stage`.

### The complete legacy stage vocabulary

Sourced from `app/state.py:130-134` and every `st["stage"] = ...` write site:

```
new, goal_selection, course_recommendation, course_viewed,
demo_time_ask, demo_date_ask, demo_booked,
offer_menu, payment_pending, enrolled, not_sure, done
```

All twelve satisfy the frozen `internal_key` regex `^[a-z0-9_]+$`.

## Decision

**The first Enterprise Pipeline created for any existing tenant is a
*Compatibility Pipeline*: its `PipelineStage.internal_key` values MUST be exactly
the twelve legacy router stage strings, verbatim.**

- **No renaming.** No normalization. No lowercasing. No slug rewriting. No merging.
- `display_name` may be human-friendly; `internal_key` is the compatibility
  contract and is immutable.
- This pipeline exists **solely to preserve production behavior**. It is a
  compatibility artifact, not an exemplar of enterprise pipeline design.
- New tenants, and future tenant-authored pipelines, are free to use any
  vocabulary — they carry no legacy router dependency.
- `stage_category` on these stages is informational only. Per ADR-018 it does not
  drive `is_admitted`.

### Offering identity (companion rule)

`ConversationState.course` reads back `Offering.name` through the
`conversation_state_offerings` bridge. Therefore:

- `Offering.name` MUST store the **exact** legacy `course` string.
- Deduplicate on the exact `(tenant_id, name)` string — **never** on a slug.
  Slug-based dedup would merge `"Python"` and `"python"` into one winning name and
  silently rewrite the other rows' course value.
- `internal_key` may be a derived slug; collisions are resolved with a numeric
  suffix. Display names must remain identical to the legacy strings.

## Consequences

- Phase 16.5A6 backfill seeds the Compatibility Pipeline with the twelve legacy
  keys and links `pipeline_stage_id` by exact string match on the legacy `stage`.
- `ConversationState.stage` round-trips identically before and after backfill:
  read returns `internal_key` == legacy string; write resolves the same key back.
- The enterprise model gains the relational pipeline without the router noticing.
- The education-specific vocabulary is quarantined in the Compatibility Pipeline
  and does not constrain other tenants or verticals.
- Any future attempt to "clean up" these stage keys is a **breaking change** to the
  router and requires its own ADR plus a coordinated router refactor.

## References
- Phase 16.5A6 Discovery Report
- ADR-018 (Business Conversion Independence)
- `app/bot/router.py`, `app/bot/objections.py`, `app/state.py`
- `docs/04_DATABASE/OXFORD_CRM_ENTERPRISE_DATA_MODEL_FREEZE_v1.0.md` (v1.2)
