# Oxford CRM — Migration History and Alembic Strategy
## Complete Database Evolution Record

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Database Engineering
> **Audience:** Engineers, DBAs, AI Assistants
> **Last Updated:** 2026-07-02 | **Next Review:** Before any new migration
> **Source Authority:** Verified against all 11 files in `migrations/versions/`

---

## Table of Contents

1. [Overview](#1-overview)
2. [Alembic Strategy](#2-alembic-strategy)
3. [Migration Chain](#3-migration-chain)
4. [Migration Catalog](#4-migration-catalog)
5. [Current Database State](#5-current-database-state)
6. [Rollback Philosophy](#6-rollback-philosophy)
7. [Production Deployment Order](#7-production-deployment-order)
8. [Migration Safety Rules](#8-migration-safety-rules)
9. [Known Migration Risks](#9-known-migration-risks)
10. [How to Create a New Migration](#10-how-to-create-a-new-migration)

---

## 1. Overview

Oxford CRM uses **Alembic** (via Flask-Migrate) to manage all database schema changes. Every schema change has a corresponding migration file in `migrations/versions/`.

**Migration state as of Phase 15B:**
- Total migrations: **11**
- Current HEAD: **`5a4dedcee918`** (add provider agnostic billing columns)
- All migrations applied to production: ✅
- Pending migrations: **None**
- Last migration date: 2026-06-13

---

## 2. Alembic Strategy

### Single Linear Chain

Oxford CRM uses a **single linear migration chain** — no branches. Every migration has exactly one `down_revision` (its parent) and one successor.

```
d269f81c1d24 → 322eeddc7246 → d3c2ce4aa446 → a1b2c3d4e5f6
    → b2c3d4e5f6a7 → 5d03593d42b4 → 002e57d59f03 → 623e5fa136ef
    → 17f210d813df → a3f1b2c4d5e6 → 5a4dedcee918 (HEAD)
```

### Flask-Migrate Integration

Alembic is configured via Flask-Migrate:
```python
# In app/__init__.py:
migrate.init_app(app, db)
# In app/extensions.py:
migrate = Migrate()
```

**Key CLI commands:**
```bash
flask db migrate -m "description"  # Generate migration from model diff
flask db upgrade                   # Apply pending migrations
flask db downgrade <revision>      # Roll back to specific revision
flask db current                   # Show current applied revision
flask db history                   # Show all revisions
```

### Migration File Location

```
migrations/
├── alembic.ini         # Alembic configuration
├── env.py              # Runtime environment (SQLAlchemy setup)
├── script.py.mako      # Migration file template
└── versions/
    ├── d269f81c1d24_initial_postgres_state.py
    ├── 322eeddc7246_crm_schema_expansion.py
    ├── ... (all 11 files)
    └── 5a4dedcee918_add_provider_agnostic_billing_columns.py
```

---

## 3. Migration Chain

Complete verified chain from `migrations/versions/` (all `revision` and `down_revision` values confirmed):

```
NULL (initial)
    │
    ▼
d269f81c1d24  "initial postgres state"
  Created: 2026-05-26
    │
    ▼
322eeddc7246  "crm schema expansion"
  Created: 2026-05-28
    │
    ▼
d3c2ce4aa446  "phase 4d message logging"
    │
    ▼
a1b2c3d4e5f6  "phase 5a conversation message"
    │
    ▼
b2c3d4e5f6a7  "phase 6a lead event"
    │
    ▼
5d03593d42b4  "add users table"
    │
    ▼
002e57d59f03  "phase 11 d1 opt out safety"
    │
    ▼
623e5fa136ef  "phase11 d3b2"
    │
    ▼
17f210d813df  "phase12 tenant foundation"
  Created: 2026-06-10
    │
    ▼
a3f1b2c4d5e6  "phase 13 a2b identity schema"
  Created: 2026-06-11
    │
    ▼
5a4dedcee918  "add provider agnostic billing columns"
  Created: 2026-06-13
    │
    ▼
  ← HEAD (current)
```

---

## 4. Migration Catalog

### Migration 1: `d269f81c1d24` — Initial PostgreSQL State
**Date:** 2026-05-26 | **Phase:** Initial

**Creates:**
- `conversation_state` table (minimal: phone, name, stage, course, goal, batch_time, offer_course, last_msg, last_text, updated_at)
- `follow_up_jobs` table (phone, name, send_at, message, day, done, created_at)

**Indexes created:**
- `ix_conversation_state_phone` (unique at this point, later changed)
- `ix_follow_up_jobs_done`
- `ix_follow_up_jobs_phone`

**Significance:** The foundation — conversation state and follow-up queue from the original WhatsApp bot.

---

### Migration 2: `322eeddc7246` — CRM Schema Expansion
**Date:** 2026-05-28 | **Phase:** 4A

**Adds to `conversation_state`:**
- `created_at` DateTime (NOT NULL, server default: CURRENT_TIMESTAMP)
- `lead_status` String(50) nullable
- `assigned_staff` String(100) nullable
- `lead_score` Integer nullable
- `is_admitted` Boolean nullable
- `notes` Text nullable
- `updated_at` enforced NOT NULL

**Significance:** Transforms `conversation_state` from a bot state tracker into a proper CRM lead record.

---

### Migration 3: `d3c2ce4aa446` — Phase 4D Message Logging
**Phase:** 4D

**Creates:**
- `message_log` table (phone, direction, message_type, message_text, meta_json, created_at, `tenant_id` nullable initially → later enforced)

**Significance:** Adds the raw audit log layer.

---

### Migration 4: `a1b2c3d4e5f6` — Phase 5A Conversation Message
**Phase:** 5A

**Creates:**
- `conversation_message` table (phone, direction, message, message_type, source, staff_name, wa_message_id, created_at, `tenant_id`)

**Indexes:**
- `idx_conv_msg_phone_created` (phone, created_at)
- `idx_conv_msg_wa_id` (wa_message_id)

**Significance:** Adds the CRM-renderable conversation timeline, distinct from the raw log.

---

### Migration 5: `b2c3d4e5f6a7` — Phase 6A Lead Event
**Phase:** 6A

**Creates:**
- `lead_event` table (phone, event_type, event_data, created_at, `tenant_id`)

**Indexes:**
- `idx_lead_event_phone_created` (phone, created_at)

**Significance:** Adds the intelligence scoring event stream.

---

### Migration 6: `5d03593d42b4` — Add Users Table
**Phase:** 10

**Creates:**
- `users` table (id, username, password_hash, role, is_active, require_password_change, tenant_id, created_at, last_login)

**Significance:** Adds Flask-Login CRM user authentication.

---

### Migration 7: `002e57d59f03` — Phase 11-D1 Opt-Out Safety
**Phase:** 11-D1

**Adds to `conversation_state`:**
- `is_opted_out` Boolean nullable default False

**Significance:** Adds the WhatsApp opt-out compliance infrastructure.

---

### Migration 8: `623e5fa136ef` — Phase 11-D3B2 Automation Interceptor
**Phase:** 11-D3B2

**Creates:**
- `pending_messages` table (phone, text, created_at, tenant_id)

**Adds to `follow_up_jobs`:**
- `retry_count` Integer default 0
- `last_attempt_at` DateTime nullable
- `failure_reason` Text nullable

**Significance:** Adds the 24-hour Meta window fallback queue and retry tracking for failed follow-ups.

---

### Migration 9: `17f210d813df` — Phase 12 Tenant Foundation
**Date:** 2026-06-10 | **Phase:** 12

**Creates:**
- `tenants` table (id, name, created_at)
- Inserts the Oxford Computers root tenant row

**Adds `tenant_id` to:**
- `conversation_state`, `conversation_message`, `message_log`, `lead_event`, `follow_up_jobs`, `pending_messages`, `users`

**Process:**
1. Create `tenants` table
2. Insert Oxford Computers tenant with generated UUID
3. Add `tenant_id` column (nullable) to all 7 child tables
4. Backfill all existing rows with Oxford Computers tenant ID
5. Validate backfill (hard abort if any NULL remains)
6. Enforce NOT NULL on `tenant_id` in all child tables
7. Add FK constraints from all child tables to `tenants.id`

**Significance:** The most important migration in Oxford CRM history. Transforms a single-tenant bot into a multi-tenant SaaS architecture. Backward-compatible — no existing data was lost.

---

### Migration 10: `a3f1b2c4d5e6` — Phase 13-A2B Identity Schema
**Date:** 2026-06-11 | **Phase:** 13-A2B

**Adds to `tenants`:**
- `slug`, `status`, `plan`, `trial_ends_at`, `billing_email`, `industry`
- `waba_phone_number_id`, `waba_access_token_encrypted`
- `ai_persona_name`, `ai_prompt_override`
- `updated_at`

**Adds to `users`:**
- `email` String(120) nullable unique

**Modifies `users`:**
- Drops global unique index on `username`
- Creates composite unique constraint `uq_users_tenant_username` on `(tenant_id, username)`

**Backfills:**
- Oxford Computers tenant: `slug='oxford'`, `status='ACTIVE'`, `plan='ENTERPRISE'`, `industry='Education'`

**Significance:** Adds SaaS identity fields (slug, status, plan) and per-tenant WABA + AI configuration. Critical multi-step migration with validation guards.

---

### Migration 11: `5a4dedcee918` — Provider-Agnostic Billing
**Date:** 2026-06-13 | **Phase:** 13-B4.1

**Creates:**
- `billing_invoices` table (all billing ledger fields)

**Adds to `tenants`:**
- `billing_provider`, `billing_customer_id`, `billing_subscription_id`
- `billing_subscription_status`, `current_period_end`, `past_due_at`
- `billing_exempt` (Boolean, server_default=false)
- `currency` (String(3), server_default='USD')

**Constraints:**
- `uq_tenants_billing_subscription_id` unique constraint on `billing_subscription_id`

**Significance:** Adds the complete billing foundation — provider-agnostic by design. Both Razorpay and Stripe can use the same schema.

---

## 5. Current Database State

| Dimension | Value |
|-----------|-------|
| Current HEAD revision | `5a4dedcee918` |
| Total migrations | 11 |
| Pending migrations | 0 |
| Tables in schema | 9 |
| Production tenants | 1 (Oxford Computers) |
| Database server | PostgreSQL (Railway managed) |
| Last migration applied | 2026-06-13 |

---

## 6. Rollback Philosophy

### General Principle

**Rollbacks are a last resort.** Prefer forward migrations with fixes over rollbacks in production.

### When to Roll Back

Only roll back if:
1. A migration created an **unrecoverable data corruption**
2. A migration caused a **complete application failure** (500 on all routes)
3. A data-destroying bug was caught **within minutes** of deployment

### How to Roll Back

```bash
# Roll back one migration (to the previous revision):
flask db downgrade <down_revision_of_current>

# Roll back to a specific revision:
flask db downgrade 17f210d813df

# Roll back everything (EXTREME — almost never appropriate):
flask db downgrade base
```

### Rollback Risks

- **Migration 9 (`17f210d813df`)** — Cannot safely roll back in production. Rolling back would lose the `tenant_id` column and all tenant isolation data. If this migration must be reversed, use a database snapshot instead.
- **Migration 11 (`5a4dedcee918`)** — Safe to roll back (adds only nullable columns and a new table). The `downgrade()` function is implemented and tested.

### The Snapshot Rule

Before applying any migration in production:
> **Take a Railway database snapshot (manual backup) BEFORE running `flask db upgrade`.**

---

## 7. Production Deployment Order

Migrations are deployed as part of the application release on Railway.

### Current Procfile Strategy

```
# Procfile:
web: gunicorn run:app
```

Migrations are **not run automatically on deploy**. They must be run manually:

```bash
# 1. Deploy new code to Railway
# 2. Connect to Railway shell:
railway run flask db upgrade

# 3. Verify:
railway run flask db current
```

### Future Improvement (Phase 16)

Consider adding a `release` phase to Railway:
```
# Procfile:
release: flask db upgrade
web: gunicorn run:app
```

This ensures migrations run before the new web process starts.

---

## 8. Migration Safety Rules

These rules must be followed for every new migration. No exceptions.

| Rule | Reason |
|------|--------|
| **Always add new columns as `nullable=True` first** | Avoids PostgreSQL lock on non-nullable ALTER on large tables |
| **Always backfill before enforcing NOT NULL** | Never leave NULL data in a NOT NULL column |
| **Always validate backfill with a COUNT check** | Prevents silent failures on backfill |
| **Never rename a column in production** | Always add new column, backfill, and drop old column separately |
| **Never drop a column or table without approval** | Irreversible data loss |
| **Always include a `downgrade()` function** | Emergency rollback must be possible |
| **Never modify an existing unique constraint in-place** | Drop + recreate pattern required |
| **Test migrations on a clone of the production DB** | Never test migrations only on local |
| **Commit migration files before deploying** | Migration files must be in git before running on production |
| **Run `flask db current` after upgrade** | Confirm the HEAD revision matches expectations |

---

## 9. Known Migration Risks

| Risk | Migration | Severity | Mitigation |
|------|----------|---------|-----------|
| Rollback of migration 9 would destroy tenant isolation | `17f210d813df` | CRITICAL | Use Railway snapshot for rollback instead |
| Large table ALTER on `conversation_state` can lock briefly | Any future `ALTER` on this table | MEDIUM | Use `batch_alter_table` and nullable first |
| `a3f1b2c4d5e6` has a multi-step backfill with hard abort | `a3f1b2c4d5e6` | HIGH | Do not re-run this migration — it already ran |
| Oxford Computers `slug='oxford'` is hardcoded in migration 10 backfill | `a3f1b2c4d5e6` | LOW | Acceptable — Oxford is the founding tenant |
| `currency` defaults to 'USD' but Razorpay uses INR | `5a4dedcee918` | LOW | Fix in Phase 16 server_default |
| No automated migration in Procfile | All | MEDIUM | Manual `flask db upgrade` required per deploy |

---

## 10. How to Create a New Migration

Follow this exact process for every future migration:

```bash
# Step 1: Update your model in app/models.py
# Step 2: Generate the migration
flask db migrate -m "Brief description of change"

# Step 3: ALWAYS review the generated migration before applying:
# - Verify all ADD COLUMN operations
# - Add backfill logic if needed
# - Add validation guards for critical data
# - Ensure downgrade() is complete and correct
# - Add server_default for NOT NULL columns

# Step 4: Apply on development
flask db upgrade

# Step 5: Test thoroughly
# Step 6: Commit migration file to git
# Step 7: Apply to Railway (after backup)
railway run flask db upgrade

# Step 8: Verify
railway run flask db current
```

---

*Oxford CRM Documentation — docs/03_database/MIGRATIONS.md*
*Source-verified against all 11 files in `migrations/versions/` (2026-07-02)*
*Cross-references: `DATABASE_BIBLE.md` · `SCHEMA_RULES.md` · `08_deployment/DEPLOYMENT_GUIDE.md`*
