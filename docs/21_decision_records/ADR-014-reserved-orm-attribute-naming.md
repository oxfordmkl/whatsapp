---
KnowledgeID: DOC-ADR-014
Version: 1.0
Status: ACTIVE
Owner: Architecture Team
Dependencies: [DOC-META-BASELINE, DOC-ADR-012, DOC-ADR-013]
---

# ADR-014: Reserved ORM Attribute Naming — `metadata` Forbidden

## Status
ACTIVE — Ratified Phase K2.1 (2026-07-16)

## Context

The Enterprise Data Model Freeze v1.0 §3 specified a column named `metadata` on the
`Offering` model:

```
| Offering | metadata | JSONB | No | {} | ...
```

SQLAlchemy's declarative base exposes `Model.metadata` as the `MetaData` object
(the schema introspection registry). Declaring a column attribute named `metadata`
on any SQLAlchemy model raises:

```
sqlalchemy.exc.InvalidRequestError:
  Attribute name 'metadata' is reserved when using the Declarative Base class.
```

This is not a style issue — it is a hard runtime failure. The freeze specification
is **un-implementable as written**.

## Decision

**The column name `metadata` is permanently forbidden as an ORM model attribute.**

**The enterprise-standard name for tenant-defined JSON extension blobs is `custom_attributes`.**

Rationale for `custom_attributes` over alternatives:
- `extra_data` — too generic, lacks domain context
- `attributes` — conflicts with Python `object.attributes` idiom
- `custom_metadata` — still contains the reserved word; confusing
- `custom_attributes` — self-documenting, collision-free, consistent with
  `ConversationState.custom_attributes` already in the codebase

## Scope

**Immediately affected:**
- `Offering.metadata` (specified in Freeze v1.0) → implemented as `Offering.custom_attributes`
- Enterprise Data Model Freeze updated to v1.1 reflecting this name

**Retroactive documentation:**
- All future model specifications must use `custom_attributes` for JSON extension blobs

## Constitutional Rule

No SQLAlchemy model column attribute may be named any of the following reserved names:
`metadata`, `query`, `query_class`, `_sa_class_manager`, `__table__`, `__tablename__`,
`__table_args__`, `__mapper__`.

## Consequences

- The Freeze v1.0 specification required correction before implementation was possible
- Zero migration impact: the column was never deployed as `metadata` — it is being
  created for the first time as `custom_attributes` in Phase 16.5A5-H1
- Future architects must not specify these reserved names in model specifications

## References
- SQLAlchemy declarative base reserved attributes documentation
- Phase K2.1 Enterprise Architecture Baseline v1.1 governance review
- ADR-013 (JSON column standard)
- `docs/04_DATABASE/OXFORD_CRM_ENTERPRISE_DATA_MODEL_FREEZE_v1.0.md` (updated to v1.1)
