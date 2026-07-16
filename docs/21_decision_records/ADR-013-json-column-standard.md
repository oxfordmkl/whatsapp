---
KnowledgeID: DOC-ADR-013
Version: 1.0
Status: ACTIVE
Owner: Architecture Team
Dependencies: [DOC-META-BASELINE, DOC-ADR-012]
---

# ADR-013: JSON Column Standard — SQLAlchemy JSON over db.Text

## Status
ACTIVE — Ratified Phase K2.1 (2026-07-16)

## Context

The Enterprise Data Model Freeze v1.0 (Phase 16.5A3) specified `JSONB` as the column
type for all structured JSON fields (`TenantSettings.settings`, `Offering.metadata`,
`MessageTemplate.variables`, `AudienceRule.logic_tree`). The existing codebase stored
these as `db.Text` (matching `meta_json` and other legacy patterns) to preserve
SQLite + PostgreSQL parity.

The Phase 16.5A5 implementation followed the `db.Text` convention, adding
`ConversationState.custom_attributes` and planning `Offering.custom_attributes`
as plain text blobs.

Three positions existed:
- `SCHEMA_RULES.md §13`: No JSON/JSONB type listed — `db.Text` convention by omission
- `DATA_MODEL_FREEZE_v1.0 §3`: Specifies `JSONB` — queryable, indexed, typed
- `Phase 16.5A5 implementation`: Used `db.Text` — matches historical convention

## Decision

**All structured JSON columns in Enterprise Configuration models use `SQLAlchemy JSON`
(i.e., `db.JSON` in Flask-SQLAlchemy).**

- On PostgreSQL (Railway): maps to `JSON` column type — queryable, type-safe
- On SQLite (local/test): maps to `TEXT` — transparent, no schema difference from prior `db.Text`

`db.JSON` is preferred over raw `JSONB` because:
1. SQLAlchemy `JSON` provides transparent Python dict/list serialization without manual `json.loads`
2. Maintains SQLite test parity (SQLite has no JSONB; `db.JSON` gracefully degrades to TEXT)
3. Avoids the `sa.Dialects.postgresql.JSONB` import that breaks SQLite compatibility
4. Subsumes the `db.Text` JSON convention with no behavioral change on SQLite

`SCHEMA_RULES.md §13` will be updated in a future documentation phase to add `db.JSON`
to the data type table.

## Scope

**Immediately affected (Phase 16.5A5-H1):**
- `ConversationState.custom_attributes`: `db.Text` → `db.JSON`
- `Offering.custom_attributes`: created as `db.JSON` (new column)

**Deferred (future migration phase):**
- `TenantSettings.settings`, `MessageTemplate.variables`, `AudienceRule.logic_tree`,
  `AudienceRule.rule_json`: remain `db.Text` until a dedicated JSON-retrofit migration
  runs (safe when those columns are lightly populated)

## Consequences

- `ConversationState.custom_attributes` access changes from `json.loads(cs.custom_attributes or '{}')`
  to direct dict access: `cs.custom_attributes or {}` — no callers exist yet (column added dormant)
- Future Audience Engine queries can use PostgreSQL JSON operators directly
- SQLite test suite remains fully compatible

## References
- Phase K2.1 Enterprise Architecture Baseline v1.1 governance review
- ADR-014 (reserved ORM attribute naming)
- `docs/04_DATABASE/OXFORD_CRM_ENTERPRISE_DATA_MODEL_FREEZE_v1.0.md` (updated to v1.1)
