# Oxford CRM — Tables Reference
## Per-Table Technical Specification

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Database Engineering
> **Audience:** Engineers, DBAs, AI Assistants
> **Last Updated:** 2026-07-02 | **Next Review:** After any schema change
> **Source Authority:** Verified against `app/models.py` and migration files

---

## Format

Each table entry follows this structure:
- **Purpose** — what business problem this table solves
- **Primary Key** — PK definition
- **Foreign Keys** — all FK references with direction
- **Indexes** — all indexes (PK, unique, composite, regular)
- **Unique Constraints** — constraint names and columns
- **Tenant Isolation** — how tenant scoping is enforced
- **Read Frequency** — estimated query pattern
- **Write Frequency** — estimated write pattern
- **Future Growth** — expected evolution

---

## Table Index

1. [tenants](#1-tenants)
2. [users](#2-users)
3. [conversation_state](#3-conversation_state)
4. [conversation_message](#4-conversation_message)
5. [message_log](#5-message_log)
6. [lead_event](#6-lead_event)
7. [follow_up_jobs](#7-follow_up_jobs)
8. [pending_messages](#8-pending_messages)
9. [billing_invoices](#9-billing_invoices)

---

## 1. `tenants`

| Property | Value |
|----------|-------|
| **SQLAlchemy Class** | `Tenant` |
| **Phase Added** | Phase 12 + Phase 13-A2B + Phase 13-B4.1 |
| **Row Count (Kerala)** | 1 (Oxford Computers) |

**Purpose:** Root platform entity. Every other tenant-owned record is ultimately scoped to a row in this table. Contains all per-tenant configuration: identity, WhatsApp credentials, AI persona, billing, and lifecycle status.

**Primary Key:**
```
id — String(36) — UUID hex (lambda: uuid4().hex) — NOT NULL
```

**Foreign Keys:** None. This is the root entity — it has no parent.

**Unique Constraints:**

| Constraint Name | Columns |
|----------------|---------|
| (auto) | `slug` |
| `uq_tenants_billing_subscription_id` | `billing_subscription_id` |

**Indexes:**
- Primary key index on `id`
- Unique index on `slug`
- Unique index on `billing_subscription_id`

**Tenant Isolation:** This IS the tenant. Isolation starts here.

**Read Frequency:** MEDIUM — read on every authenticated request (billing guard, impersonation check, WABA routing). Cached by Flask session after login.

**Write Frequency:** LOW — written only when tenant configures WABA, AI settings, or billing status changes.

**Future Growth:** One row per institution. At 1,000 tenants, this table has 1,000 rows. Well within PostgreSQL capacity. The `waba_access_token_encrypted` Text column grows per row but is bounded by Meta token size.

---

## 2. `users`

| Property | Value |
|----------|-------|
| **SQLAlchemy Class** | `User` |
| **Phase Added** | Phase 10 + Phase 13-A2B |
| **Row Count (Kerala)** | ~5–10 (Oxford staff + superadmin) |

**Purpose:** All authenticated users of the CRM — SUPER_ADMIN (platform), ADMIN (tenant admin), and STAFF (CRM users). Integrates with Flask-Login for session management.

**Primary Key:**
```
id — Integer — auto-increment — NOT NULL
```

**Foreign Keys:**

| Column | References | Nullable | Notes |
|--------|-----------|---------|-------|
| `tenant_id` | `tenants.id` | YES | NULL for SUPER_ADMIN |

**Unique Constraints:**

| Constraint Name | Columns |
|----------------|---------|
| (auto) | `email` |
| `uq_users_tenant_username` | `(tenant_id, username)` |

**Indexes:**
- Primary key on `id`
- Unique index on `email`
- Composite unique index on `(tenant_id, username)`
- Index on `tenant_id` (`ix_users_tenant_id`)

**Tenant Isolation:** Via `tenant_id` foreign key. `tenant_query(User)` always filters to the current tenant's users. SUPER_ADMIN (`tenant_id = NULL`) is excluded from tenant queries by role-based routing.

**Read Frequency:** HIGH — read on every login attempt, session load (`user_loader`), and all permission checks.

**Write Frequency:** LOW — written on login (`last_login`), password change, and user creation.

**Future Growth:** ~5–20 users per tenant. At 1,000 tenants = 5,000–20,000 rows. The `ix_users_tenant_id` index keeps queries fast.

---

## 3. `conversation_state`

| Property | Value |
|----------|-------|
| **SQLAlchemy Class** | `ConversationState` |
| **Phase Added** | Initial → Phase 4A → Phase 11-D1 → Phase 12-B |
| **Row Count (Kerala)** | Hundreds to thousands (one per WhatsApp lead) |

**Purpose:** The primary "Lead" record. One row per unique WhatsApp phone number per tenant. Stores the complete state of a lead: conversation stage, CRM pipeline status, staff assignment, intelligence score, and audit timestamps.

**Primary Key:**
```
id — Integer — auto-increment — NOT NULL
```

**Foreign Keys:**

| Column | References | Nullable |
|--------|-----------|---------|
| `tenant_id` | `tenants.id` | No |

**Unique Constraints:**

| Constraint Name | Columns |
|----------------|---------|
| `uq_conversation_state_phone_tenant` | `(phone, tenant_id)` |

**Indexes:**
- Primary key on `id`
- Index on `phone` (`ix_conversation_state_phone`)
- Index on `tenant_id` (`ix_conversation_state_tenant_id`)
- Composite unique index on `(phone, tenant_id)`

**Tenant Isolation:** Fully scoped. The composite unique constraint `(phone, tenant_id)` ensures that the same phone number is an independent lead in each tenant.

**Read Frequency:** VERY HIGH — read on every inbound WhatsApp message (state lookup), every CRM page load (lead list), and every follow-up worker cycle (recency check).

**Write Frequency:** HIGH — updated on every inbound message (`stage`, `last_msg`, `last_text`), CRM staff actions (`assigned_staff`, `notes`, `lead_status`), and admissions (`is_admitted`).

**Future Growth:** Unbounded — grows with lead volume. The primary growth table. Expect 10,000–100,000 rows at scale. The composite index keeps per-tenant queries fast. Consider archiving old leads at Phase 16.

---

## 4. `conversation_message`

| Property | Value |
|----------|-------|
| **SQLAlchemy Class** | `ConversationMessage` |
| **Phase Added** | Phase 5A |
| **Row Count (Kerala)** | Thousands (multiple messages per lead) |

**Purpose:** Structured, CRM-renderable conversation timeline. Shown in the lead detail view. Contains richer fields than `message_log` — `wa_message_id` for deduplication, `staff_name` for audit trail, `source` for identifying who sent the message.

**Primary Key:**
```
id — Integer — auto-increment — NOT NULL
```

**Foreign Keys:**

| Column | References | Nullable |
|--------|-----------|---------|
| `tenant_id` | `tenants.id` | No |

**Unique Constraints:** None.

**Indexes:**
- Primary key on `id`
- Composite index on `(phone, created_at)` (`idx_conv_msg_phone_created`)
- Index on `wa_message_id` (`idx_conv_msg_wa_id`)
- Index on `tenant_id` (`ix_conversation_message_tenant_id`)

**Tenant Isolation:** Via `tenant_id` FK. All queries filter by both `phone` and `tenant_id`.

**Read Frequency:** HIGH — read on every lead detail page load (full conversation history).

**Write Frequency:** VERY HIGH — written on every inbound AND outbound message (2 rows per conversation turn), plus follow-up sends, and manual staff replies.

**Future Growth:** Highest-growth table alongside `message_log`. Expect 5–20 rows per lead per day of active conversation. Consider partitioning or archiving at Phase 16.

---

## 5. `message_log`

| Property | Value |
|----------|-------|
| **SQLAlchemy Class** | `MessageLog` |
| **Phase Added** | Phase 4D |
| **Row Count (Kerala)** | Thousands |

**Purpose:** Lightweight, raw technical audit log. Every message event — inbound and outbound — is recorded here. Intentionally simpler than `conversation_message`. Used for system-level debugging, not CRM display.

**Primary Key:**
```
id — Integer — auto-increment — NOT NULL
```

**Foreign Keys:**

| Column | References | Nullable |
|--------|-----------|---------|
| `tenant_id` | `tenants.id` | No |

**Unique Constraints:** None.

**Indexes:**
- Primary key on `id`
- Composite index on `(phone, created_at)` (`idx_msg_phone_created`)
- Index on `tenant_id` (`ix_message_log_tenant_id`)

**Tenant Isolation:** Via `tenant_id` FK.

**Read Frequency:** LOW — primarily read for debugging and system audits. Not used in normal CRM UI.

**Write Frequency:** VERY HIGH — written on every message event from daemon threads (`log_message_in_thread`).

**Future Growth:** Grows at the same rate as `conversation_message` but with simpler rows. Consider a log rotation policy at Phase 16 (e.g., archive records older than 1 year).

---

## 6. `lead_event`

| Property | Value |
|----------|-------|
| **SQLAlchemy Class** | `LeadEvent` |
| **Phase Added** | Phase 6A |
| **Row Count (Kerala)** | Hundreds to thousands |

**Purpose:** Named, high-signal sales funnel events. Append-only record of every qualifying lead action (`DEMO_REQUESTED`, `FEES_REQUESTED`, etc.). Feeds the lead intelligence scoring engine that calculates `ConversationState.lead_score`.

**Primary Key:**
```
id — Integer — auto-increment — NOT NULL
```

**Foreign Keys:**

| Column | References | Nullable |
|--------|-----------|---------|
| `tenant_id` | `tenants.id` | No |

**Unique Constraints:** None.

**Indexes:**
- Primary key on `id`
- Composite index on `(phone, created_at)` (`idx_lead_event_phone_created`)
- Index on `tenant_id` (`ix_lead_event_tenant_id`)

**Tenant Isolation:** Via `tenant_id` FK.

**Read Frequency:** MEDIUM — read when calculating lead scores (analytics), and in lead detail view (event timeline).

**Write Frequency:** MEDIUM — written from daemon threads when qualifying events occur. ~3–10 events per lead lifetime.

**Future Growth:** Low individual row volume. Bounded by qualifying event count per lead. Well-managed.

---

## 7. `follow_up_jobs`

| Property | Value |
|----------|-------|
| **SQLAlchemy Class** | `FollowUpJob` |
| **Phase Added** | Initial → Phase 12-B → Phase 11-D2C |
| **Row Count (Kerala)** | 3× lead count (3 jobs per lead) |

**Purpose:** Persistent queue for the follow-up scheduler daemon. Three rows are created per new lead (Day 1 at 24h, Day 3 at 72h, Day 7 at 168h). The daemon thread polls `done=False AND send_at <= now` every 5 minutes.

**Primary Key:**
```
id — Integer — auto-increment — NOT NULL
```

**Foreign Keys:**

| Column | References | Nullable |
|--------|-----------|---------|
| `tenant_id` | `tenants.id` | No |

**Unique Constraints:** None.

**Indexes:**
- Primary key on `id`
- Index on `done` (`ix_follow_up_jobs_done`) — critical for worker poll performance
- Index on `phone` (`ix_follow_up_jobs_phone`)
- Index on `tenant_id` (`ix_follow_up_jobs_tenant_id`)

**Tenant Isolation:** Via `tenant_id` FK. Worker always passes `tenant_id` to `send_automation()`.

**Read Frequency:** HIGH for the daemon — polled every 5 minutes for pending jobs.

**Write Frequency:** MEDIUM — 3 inserts per new lead. Updates on `done=True` and retry metadata fields.

**Future Growth:** Grows 3× as fast as `conversation_state`. Consider purging completed (`done=True`) jobs older than 90 days at Phase 16.

---

## 8. `pending_messages`

| Property | Value |
|----------|-------|
| **SQLAlchemy Class** | `PendingMessage` |
| **Phase Added** | Phase 11-D3B2 |
| **Row Count (Kerala)** | Low (edge case table) |

**Purpose:** 24-hour Meta messaging window fallback queue. When an automated message (follow-up or campaign) cannot be sent because the lead hasn't initiated a conversation in the last 24 hours (Meta restriction), the message is stored here. It is delivered the next time the lead sends any message.

**Primary Key:**
```
id — Integer — auto-increment — NOT NULL
```

**Foreign Keys:**

| Column | References | Nullable |
|--------|-----------|---------|
| `tenant_id` | `tenants.id` | No |

**Unique Constraints:** None.

**Indexes:**
- Primary key on `id`
- Index on `phone` (`ix_pending_messages_phone`)
- Index on `tenant_id` (`ix_pending_messages_tenant_id`)

**Tenant Isolation:** Via `tenant_id` FK.

**Read Frequency:** LOW — read on inbound message (check if pending messages exist for phone+tenant).

**Write Frequency:** LOW — written only when 24-hour window is closed.

**Current Implementation Status:** The model and table exist. **The delivery wiring (checking for pending messages on inbound webhook) is partially implemented.** Full activation is a Phase 16 task.

**Future Growth:** Self-cleaning — rows are deleted after delivery. Should remain small.

---

## 9. `billing_invoices`

| Property | Value |
|----------|-------|
| **SQLAlchemy Class** | `BillingInvoice` |
| **Phase Added** | Phase 13-B4.1 |
| **Row Count (Kerala)** | 0 (no live billing yet) |

**Purpose:** Immutable financial ledger. Every payment event — successful or failed — creates a record here. Never updated or deleted. Provides audit trail for billing, reconciliation, and compliance.

**Primary Key:**
```
id — Integer — auto-increment — NOT NULL
```

**Foreign Keys:**

| Column | References | Nullable |
|--------|-----------|---------|
| `tenant_id` | `tenants.id` | No |

**Unique Constraints:**

| Constraint Name | Columns |
|----------------|---------|
| (auto) | `provider_invoice_id` |

**Indexes:**
- Primary key on `id`
- Index on `tenant_id` (`ix_billing_invoices_tenant_id`)
- Unique index on `provider_invoice_id`

**Tenant Isolation:** Via `tenant_id` FK. Billing pages and reports always filter by `tenant_id`.

**Read Frequency:** LOW — read only for billing pages and revenue reports.

**Write Frequency:** LOW — written once per billing event. At 1 payment/month/tenant × 1,000 tenants = 1,000 rows/month. Easily managed.

**Current State:** Table schema exists. **No rows exist because live billing is not active.** Rows will be created in Phase 16 when Razorpay webhook handlers are implemented.

**Future Growth:** Bounded and predictable — grows linearly with billing events. Immutability means no row is ever modified. Safe to keep indefinitely.

---

*Oxford CRM Documentation — docs/03_database/TABLES.md*
*All table specifications verified against `app/models.py` and `migrations/versions/`*
*Cross-references: `DATABASE_BIBLE.md` · `ERD.md` · `SCHEMA_RULES.md`*
