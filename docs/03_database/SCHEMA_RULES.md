# Oxford CRM — Schema Rules
## Database Governance, Naming Standards, and Safety Constitution

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Database Engineering
> **Audience:** All Engineers, AI Assistants, DBAs
> **Last Updated:** 2026-07-02 | **Status:** MANDATORY — Read before any schema change
> **Source Authority:** Derived from patterns in `app/models.py` and all 11 migration files

---

## Table of Contents

1. [Purpose and Authority](#1-purpose-and-authority)
2. [Naming Standards](#2-naming-standards)
3. [Primary Key Rules](#3-primary-key-rules)
4. [Foreign Key Rules](#4-foreign-key-rules)
5. [Nullable Policy](#5-nullable-policy)
6. [Index Policy](#6-index-policy)
7. [Unique Constraint Policy](#7-unique-constraint-policy)
8. [Enum Policy](#8-enum-policy)
9. [Audit Column Standards](#9-audit-column-standards)
10. [Tenant Isolation Rules](#10-tenant-isolation-rules)
11. [Migration Governance](#11-migration-governance)
12. [Forbidden Operations](#12-forbidden-operations)
13. [Data Type Standards](#13-data-type-standards)
14. [Quick Reference Checklist](#14-quick-reference-checklist)

---

## 1. Purpose and Authority

This document is the **database governance constitution** for Oxford CRM. Every schema change — whether a new column, new table, new migration, or changed constraint — must comply with the rules defined here.

**These rules exist because:**
- Oxford CRM stores real customer data (lead phone numbers, conversation history, payment records)
- The production database cannot be easily recreated
- Mistakes in schema design compound over time and become expensive to fix
- Multi-tenant isolation failures are a **critical security vulnerability**

**This document is mandatory.** There are no exceptions without explicit approval from the project architect.

---

## 2. Naming Standards

### Table Names

| Rule | Standard | Example |
|------|---------|---------|
| Use lowercase snake_case | ✅ | `conversation_state`, `follow_up_jobs` |
| Use plural nouns | ✅ | `tenants`, `users`, `billing_invoices` |
| No CamelCase in table names | ❌ | `ConversationState` |
| No abbreviations | ❌ | `conv_state`, `msg_log` |
| Exception: short universally understood words OK | ✅ | `users`, `tenants` |

**Verified examples from production:**
```
tenants                    ✅ plural noun, snake_case
users                      ✅
conversation_state         ✅ (exception: not plural — one row per lead by design)
conversation_message       ✅
message_log                ✅
lead_event                 ✅
follow_up_jobs             ✅
pending_messages           ✅
billing_invoices           ✅
```

### Column Names

| Rule | Standard | Example |
|------|---------|---------|
| Use lowercase snake_case | ✅ | `tenant_id`, `created_at`, `lead_score` |
| Boolean columns: use `is_` prefix | ✅ | `is_active`, `is_admitted`, `is_opted_out` |
| DateTime columns: use `_at` suffix | ✅ | `created_at`, `updated_at`, `last_login` |
| FK columns: use `_id` suffix | ✅ | `tenant_id`, `user_id` |
| No single-letter column names | ❌ | `t`, `n` |
| No abbreviations | ❌ | `crt_dt`, `upd_at` |

### Index Names

| Rule | Pattern | Example |
|------|---------|---------|
| Regular indexes | `ix_{table}_{column}` | `ix_conversation_state_phone` |
| Composite indexes | `idx_{table}_{col1}_{col2}` | `idx_conv_msg_phone_created` |
| Unique constraints | `uq_{table}_{description}` | `uq_users_tenant_username` |
| FK indexes | `ix_{table}_{fk_column}` | `ix_users_tenant_id` |

---

## 3. Primary Key Rules

### Two Allowed Patterns

**Pattern A — Integer auto-increment** (for all operational tables)
```python
id = db.Column(db.Integer, primary_key=True)
```
Used by: `users`, `conversation_state`, `conversation_message`, `message_log`, `lead_event`, `follow_up_jobs`, `pending_messages`, `billing_invoices`

**Pattern B — UUID string** (for the root entity: `tenants` only)
```python
id = db.Column(db.String(36), primary_key=True, default=lambda: uuid.uuid4().hex)
```
Used by: `tenants`

**Why UUID for tenants?** The `tenant_id` foreign key is passed through URLs, session keys, and API responses. UUID prevents enumeration attacks (attacker cannot guess tenant IDs by incrementing).

**Why Integer for operational tables?** Integer PKs are faster for joins and range queries. Operational tables are never exposed directly in URLs.

### Rules

- Every table MUST have a primary key
- Never use a natural key (phone number, email, slug) as a primary key
- Never use composite primary keys — use a surrogate PK + unique constraint instead

---

## 4. Foreign Key Rules

### Mandatory FK Pattern

Every table that belongs to a tenant MUST have:
```python
tenant_id = db.Column(db.String(36), db.ForeignKey('tenants.id'), nullable=False, index=True)
```

**Rules:**
- FK column is always `nullable=False` after backfill (never NULL in production)
- FK column always has an index (`index=True`) for join performance
- FK constraint references the parent table's PK
- No `ondelete='CASCADE'` — Oxford CRM uses soft deletes, not cascaded deletes

### FK Naming

The FK column name must match the pattern `{referenced_table_singular}_id`:
```
tenants.id   → tenant_id   ✅
users.id     → user_id     ✅ (future reference pattern)
```

### FK Exception: Application-Level Links

`ConversationState` is linked to `ConversationMessage`, `MessageLog`, `LeadEvent`, `FollowUpJob`, and `PendingMessage` via `phone + tenant_id` at the **application layer** — not via database FK.

This is a deliberate design decision for performance and decoupling. Do not add FK constraints between these tables without explicit architect approval.

---

## 5. Nullable Policy

### Default Position: Nullable for New Columns

When adding a column to an existing table that already has rows:
```python
# Phase 1: Always add as nullable first to avoid PostgreSQL lock
batch_op.add_column(sa.Column('new_column', sa.String(50), nullable=True))

# Phase 2: Backfill all existing rows
# Phase 3: Validate backfill (assert count == 0)
# Phase 4: Enforce NOT NULL
batch_op.alter_column('new_column', nullable=False)
```

**Never add a NOT NULL column without a `server_default` in a single migration step** on a table with existing rows. PostgreSQL will fail or lock.

### Nullable Decision Guide

| Column Type | Nullable? | Reason |
|-------------|---------|--------|
| Required identity fields (name, slug, status) | No | Business invariant |
| FK columns (tenant_id) | No | Isolation invariant — exception: users.tenant_id for SUPER_ADMIN |
| Timestamps (created_at, updated_at) | No | Audit invariant |
| Optional metadata (notes, email for staff) | Yes | Not always applicable |
| Phase-added new fields | Yes → then No after backfill | Safe migration pattern |

---

## 6. Index Policy

### Mandatory Indexes

Every column that appears in:
- `WHERE` clauses of frequent queries
- `JOIN` conditions
- `ORDER BY` clauses on large tables
- FK columns

...must have an index.

**Guaranteed indexed columns in production:**

| Table | Indexed Columns |
|-------|----------------|
| `tenants` | `id` (PK), `slug` (unique), `billing_subscription_id` (unique) |
| `users` | `id` (PK), `email` (unique), `tenant_id`, `(tenant_id, username)` (composite unique) |
| `conversation_state` | `id` (PK), `phone`, `tenant_id`, `(phone, tenant_id)` (composite unique) |
| `conversation_message` | `id` (PK), `tenant_id`, `(phone, created_at)` composite, `wa_message_id` |
| `message_log` | `id` (PK), `phone`, `tenant_id`, `(phone, created_at)` composite |
| `lead_event` | `id` (PK), `phone`, `tenant_id`, `(phone, created_at)` composite |
| `follow_up_jobs` | `id` (PK), `phone`, `done`, `tenant_id` |
| `pending_messages` | `id` (PK), `phone`, `tenant_id` |
| `billing_invoices` | `id` (PK), `tenant_id`, `provider_invoice_id` (unique) |

### Index Decision Rules

| Situation | Action |
|-----------|--------|
| FK column | Always index |
| Column used in `WHERE phone=?` | Index |
| Column used in `WHERE done=False` | Index (as with `follow_up_jobs.done`) |
| `ORDER BY created_at` on high-volume table | Composite index `(phone, created_at)` |
| String column used only in SELECT (not WHERE) | Do NOT index (wastes write overhead) |
| Boolean column with very skewed distribution | Index only if the minority value is queried |

---

## 7. Unique Constraint Policy

### Naming Convention

All unique constraints MUST be named:
```
uq_{table}_{description}
```

Examples:
```
uq_users_tenant_username           → UNIQUE (tenant_id, username)
uq_conversation_state_phone_tenant → UNIQUE (phone, tenant_id)
uq_tenants_billing_subscription_id → UNIQUE (billing_subscription_id)
```

**Never rely on unnamed unique constraints.** Named constraints can be dropped by name in migrations.

### Rules

- Single-column unique constraints can use `unique=True` on the column definition
- Multi-column unique constraints must use `db.UniqueConstraint(...)` in `__table_args__`
- Never create a unique constraint on a column that you plan to make non-unique later

---

## 8. Enum Policy

Oxford CRM does NOT use SQL ENUM types. All enum-like values are stored as `String` columns with application-level validation.

**Reason:** PostgreSQL ENUM types require `ALTER TYPE` to add new values, which can lock tables. String columns are flexible and easier to migrate.

### Allowed Values Documentation

All allowed values for string "enum" columns MUST be documented in:
1. The SQLAlchemy model column comment
2. This document (below)
3. `DATABASE_BIBLE.md`

### Production Enum Values

| Table | Column | Allowed Values |
|-------|--------|---------------|
| `tenants` | `status` | `PENDING`, `TRIAL`, `ACTIVE`, `PAST_DUE`, `SUSPENDED`, `CANCELLED`, `DELETED` |
| `tenants` | `plan` | `STARTER`, `GROWTH`, `PROFESSIONAL`, `ENTERPRISE` |
| `tenants` | `industry` | `Education`, `Healthcare`, `Real Estate`, `Insurance`, `Retail`, `Custom` |
| `tenants` | `billing_provider` | `razorpay`, `stripe` |
| `tenants` | `currency` | `INR`, `USD` (others possible in future) |
| `users` | `role` | `SUPER_ADMIN`, `ADMIN`, `STAFF` |
| `conversation_message` | `direction` | `incoming`, `outgoing` |
| `conversation_message` | `message_type` | `text`, `interactive`, `button`, `template`, `system` |
| `conversation_message` | `source` | `user`, `ai`, `manual`, `followup`, `system` |
| `message_log` | `direction` | `inbound`, `outbound` |
| `message_log` | `message_type` | `user`, `ai`, `followup`, `manual`, `system` |
| `billing_invoices` | `provider` | `razorpay`, `stripe` |
| `billing_invoices` | `status` | `paid`, `failed`, `open` |

---

## 9. Audit Column Standards

### Every new table MUST include:

```python
created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
```

### Mutable tables MUST also include:

```python
updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
```

### Append-only tables do NOT need `updated_at`:

Append-only tables (MessageLog, ConversationMessage, LeadEvent, BillingInvoice) have only `created_at` — they are never updated.

### Timestamp Rules

- All timestamps are stored in **UTC**
- Use `datetime.utcnow` as the default — never `datetime.now()` (timezone-aware)
- Frontend converts UTC to local time for display
- Never store timezone-aware datetime objects in the database (SQLAlchemy strips timezone info)

---

## 10. Tenant Isolation Rules

### The Isolation Contract

> **Every table that stores business data MUST be scoped by `tenant_id`.**

No exceptions. If a new model is created that stores data belonging to a tenant (leads, messages, events, billing, staff), it MUST have a `tenant_id` FK column.

### The Three Isolation Layers

**Layer 1 — Database Level:** `tenant_id` FK on every tenant-scoped table with NOT NULL constraint

**Layer 2 — Application Level:** `tenant_query(Model)` in `admin.py` adds `.filter_by(tenant_id=...)` to every query

**Layer 3 — Composite Key Level:** `UNIQUE (phone, tenant_id)` ensures the same phone can be a lead in multiple tenants independently

### Isolation Verification Checklist (for new models)

```
□ Does the model store tenant-specific data?
    YES → Add tenant_id column
    NO  → Only platform-wide models (e.g., Tenant itself)

□ Does tenant_id have FK to tenants.id?  ✅
□ Is tenant_id nullable=False?           ✅
□ Does tenant_id have an index?          ✅
□ Is the model queried via tenant_query()? ✅
□ Are unique constraints composite with tenant_id? ✅
```

### Models Exempt From tenant_id

Only one model is exempt:
- `Tenant` itself — it IS the tenant

---

## 11. Migration Governance

### Before Writing a Migration

1. Read `MIGRATIONS.md` to understand the current chain
2. Confirm `flask db current` shows the expected HEAD
3. Plan the migration as a multi-step operation if data backfill is needed
4. Write `downgrade()` before `upgrade()`

### Migration Checklist

```
□ New columns added as nullable=True first
□ Backfill logic included (if converting nullable→NOT NULL)
□ Validation guard added (assert NULL count == 0 before enforcing NOT NULL)
□ downgrade() is complete and correct
□ Index names follow the naming convention
□ Unique constraint names follow the naming convention
□ Migration message is descriptive (not "auto-generated changes")
□ Migration file committed to git before deploy
□ Database snapshot taken before running on production
□ Verified with flask db current after upgrade
```

---

## 12. Forbidden Operations

**The following operations are FORBIDDEN in production without explicit written approval:**

| Operation | Why Forbidden |
|-----------|-------------|
| `DROP TABLE` | Irreversible data loss |
| `DROP COLUMN` | Irreversible data loss |
| `DELETE FROM` (without `WHERE tenant_id=`) | Cross-tenant data deletion |
| `TRUNCATE` | Irreversible data loss |
| Adding FK with `ondelete='CASCADE'` | Cascaded deletes are uncontrolled data loss |
| Renaming a column in one step | Breaks all code referencing the old name simultaneously |
| Renaming a table in one step | Breaks all ORM mappings |
| Running migrations without a database backup | No rollback path if migration fails |
| Modifying data in `billing_invoices` | Immutable financial record |

---

## 13. Data Type Standards

| Python/ORM Type | PostgreSQL Type | Use Case |
|-----------------|-----------------|---------|
| `db.Integer` | `INTEGER` | Auto-increment PKs, counts, scores |
| `db.String(n)` | `VARCHAR(n)` | Bounded text — always specify length |
| `db.Text` | `TEXT` | Unbounded text (messages, prompts, notes) |
| `db.Boolean` | `BOOLEAN` | Flags (`is_active`, `done`, `billing_exempt`) |
| `db.DateTime` | `TIMESTAMP` | All timestamps — UTC only |
| `db.Integer` for money | `INTEGER` | **Store money in minor units (paise/cents)** — Never use FLOAT |

### Money Storage Rule

**NEVER store monetary amounts as FLOAT.** Floating-point arithmetic is imprecise and will cause billing errors.

```python
# ✅ CORRECT — store in paise (for INR) or cents (for USD)
amount_paid = db.Column(db.Integer, nullable=False)  # 999900 = ₹9,999.00

# ❌ FORBIDDEN
amount_paid = db.Column(db.Float)  # Never use float for money
amount_paid = db.Column(db.Numeric)  # Acceptable but Integer is preferred for simplicity
```

---

## 14. Quick Reference Checklist

Use this checklist before committing any schema change:

```
DATABASE SCHEMA CHANGE CHECKLIST
═══════════════════════════════════════════════════════

NAMING
□ Table name: lowercase plural snake_case
□ Column names: lowercase snake_case
□ Boolean columns: is_ prefix
□ Timestamp columns: _at suffix
□ FK columns: _id suffix
□ Index names: ix_ or idx_ prefix per convention
□ Unique constraint names: uq_ prefix

STRUCTURE
□ Every table has a single primary key
□ FK columns have index=True
□ FK columns reference parent PK
□ No CASCADE deletes
□ Money stored as Integer (minor units)
□ All timestamps use datetime.utcnow (UTC)

TENANT ISOLATION
□ Tenant-scoped models have tenant_id FK
□ tenant_id is NOT NULL (after backfill)
□ tenant_id has an index
□ Unique constraints are composite with tenant_id where appropriate

MIGRATION
□ New columns added nullable=True first
□ Backfill logic written
□ Validation guard written (assert NULL count == 0)
□ downgrade() function implemented
□ Constraint names follow convention
□ Database backup taken before production deploy

AUDIT
□ new tables have created_at
□ Mutable tables also have updated_at
□ Append-only tables have only created_at (no updated_at)
```

---

*Oxford CRM Documentation — docs/03_database/SCHEMA_RULES.md*
*This document is the highest authority for all database schema decisions.*
*Cross-references: `DATABASE_BIBLE.md` · `MIGRATIONS.md` · `ERD.md` · `07_security/TENANT_ISOLATION.md`*
