# docs/03_database — Database Documentation Layer

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Database Engineering
> **Last Updated:** 2026-07-02

---

## Purpose

The `03_database` folder contains the **authoritative database documentation** for Oxford CRM. All content is verified against the actual SQLAlchemy models in `app/models.py` and the Alembic migration chain in `migrations/versions/`.

Nothing is invented. If something does not exist in the source code, it is labelled **Future Roadmap**.

---

## Contents

| File | Purpose | Importance |
|------|---------|-----------|
| `README.md` | This file — folder orientation | MEDIUM |
| `DATABASE_BIBLE.md` | Master reference for every model | CRITICAL |
| `ERD.md` | Entity-relationship diagrams | CRITICAL |
| `TABLES.md` | Per-table technical specification | HIGH |
| `MIGRATIONS.md` | Migration history and Alembic strategy | HIGH |
| `SCHEMA_RULES.md` | Naming standards, governance, safety rules | HIGH |

---

## Source Files Verified

| File | Purpose |
|------|---------|
| `app/models.py` | SQLAlchemy model definitions (297 lines) |
| `migrations/versions/d269f81c1d24_initial_postgres_state.py` | Migration 1 |
| `migrations/versions/322eeddc7246_crm_schema_expansion.py` | Migration 2 |
| `migrations/versions/d3c2ce4aa446_phase_4d_message_logging.py` | Migration 3 |
| `migrations/versions/a1b2c3d4e5f6_phase_5a_conversation_message.py` | Migration 4 |
| `migrations/versions/b2c3d4e5f6a7_phase_6a_lead_event.py` | Migration 5 |
| `migrations/versions/5d03593d42b4_add_users_table.py` | Migration 6 |
| `migrations/versions/002e57d59f03_phase_11_d1_opt_out_safety.py` | Migration 7 |
| `migrations/versions/623e5fa136ef_phase11_d3b2.py` | Migration 8 |
| `migrations/versions/17f210d813df_phase12_tenant_foundation.py` | Migration 9 |
| `migrations/versions/a3f1b2c4d5e6_phase_13_a2b_identity_schema.py` | Migration 10 |
| `migrations/versions/5a4dedcee918_add_provider_agnostic_billing_columns.py` | Migration 11 |

---

## Reading Order

1. `DATABASE_BIBLE.md` — understand every model first
2. `ERD.md` — visualize the relationships
3. `TABLES.md` — deep-dive per table
4. `MIGRATIONS.md` — understand the evolution history
5. `SCHEMA_RULES.md` — governance before any changes

---

## Critical Rules Before Touching The Database

> **NEVER run `flask db upgrade` or any DDL command without verifying the complete migration chain first.**
> **NEVER drop a column, table, or constraint in production without a documented rollback plan.**
> **ALWAYS add new columns as `nullable=True` first, then backfill, then enforce NOT NULL.**

See `SCHEMA_RULES.md` for the complete governance policy.

---

*Cross-references: `02_architecture/TENANT_ARCHITECTURE.md` · `02_architecture/SYSTEM_ARCHITECTURE.md` · `10_engineering/AI_DEVELOPMENT_CONSTITUTION.md`*
