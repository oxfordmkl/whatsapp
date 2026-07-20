# Oxford CRM — Database Bible
## Master Reference for Every Production Database Model

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Database Engineering
> **Audience:** Engineers, AI Assistants, DBAs
> **Last Updated:** 2026-07-02 | **Next Review:** After any schema migration
> **Source Authority:** Verified line-by-line against `app/models.py` (297 lines)

---

## Table of Contents

1. [Overview](#1-overview)
2. [Model Registry](#2-model-registry)
3. [Tenant](#3-tenant)
4. [User](#4-user)
5. [ConversationState (Lead)](#5-conversationstate-lead)
6. [ConversationMessage](#6-conversationmessage)
7. [MessageLog](#7-messagelog)
8. [LeadEvent](#8-leadevent)
9. [FollowUpJob](#9-followupjob)
10. [PendingMessage](#10-pendingmessage)
11. [BillingInvoice](#11-billinginvoice)
12. [Model Relationship Summary](#12-model-relationship-summary)
13. [Soft Delete Policy](#13-soft-delete-policy)
14. [Audit Considerations](#14-audit-considerations)
15. [Models Not Yet Implemented](#15-models-not-yet-implemented)

---

## 1. Overview

Oxford CRM uses **PostgreSQL** as its production database. The schema is managed via **Alembic** (Flask-Migrate). All models are defined as SQLAlchemy ORM classes in `app/models.py`.

**Current state:** 8 production tables in use. 11 migrations applied. No pending migrations.

**Database engine:** PostgreSQL (Railway-hosted)
**ORM:** SQLAlchemy 2.x (Flask-SQLAlchemy)
**Migration tool:** Alembic (Flask-Migrate)
**Connection pool:** `pool_pre_ping=True`, `pool_recycle=1800`

---

## 2. Model Registry

| Model Class | Table Name | Phase Added | Purpose |
|-------------|-----------|-------------|---------|
| `Tenant` | `tenants` | Phase 12 / 13-A2B | Platform tenant root |
| `User` | `users` | Phase 10 | Authenticated CRM users |
| `ConversationState` | `conversation_state` | Initial | Lead / WhatsApp contact state |
| `ConversationMessage` | `conversation_message` | Phase 5A | CRM-renderable message timeline |
| `MessageLog` | `message_log` | Phase 4D | Raw technical event log |
| `LeadEvent` | `lead_event` | Phase 6A | Named sales funnel events |
| `FollowUpJob` | `follow_up_jobs` | Initial | Scheduled follow-up messages |
| `PendingMessage` | `pending_messages` | Phase 11-D3B2 | 24-hour window fallback queue |
| `BillingInvoice` | `billing_invoices` | Phase 13-B4.1 | Immutable billing ledger |

**Total: 9 models / 9 tables**

---

## 3. Tenant

**Source:** `app/models.py` lines 6–69
**Table:** `tenants`
**Phase:** 12 (foundation), 13-A2B (SaaS expansion), 13-B4.1 (billing)

### Purpose

The root entity of the entire platform. Every other tenant-owned record references a `Tenant` row via `tenant_id`. This is the platform-level organizational unit.

### Full Column Reference

| Column | Type | Nullable | Default | Description |
|--------|------|---------|---------|-------------|
| `id` | String(36) | No | `uuid4().hex` | **Primary Key** — UUID hex string |
| `name` | String(100) | No | — | Display name (e.g., "Oxford Computers") |
| `created_at` | DateTime | Yes | `utcnow` | Creation timestamp |
| `slug` | String(30) | No | — | URL-safe identifier. **Immutable after creation.** |
| `status` | String(20) | No | `'ACTIVE'` | Lifecycle state — see status values |
| `plan` | String(20) | No | `'ENTERPRISE'` | Subscription tier |
| `trial_ends_at` | DateTime | Yes | NULL | Trial expiry. NULL = not on trial |
| `billing_email` | String(100) | Yes | NULL | Billing contact email |
| `industry` | String(50) | No | `'Education'` | Vertical for AI prompt defaults |
| `waba_phone_number_id` | String(50) | Yes | NULL | Meta WABA phone number ID |
| `waba_access_token_encrypted` | Text | Yes | NULL | Fernet-encrypted Meta token |
| `ai_persona_name` | String(50) | Yes | NULL | Bot display name. NULL = "Oxford Nova" |
| `ai_prompt_override` | Text | Yes | NULL | Custom system prompt. NULL = system default |
| `billing_provider` | String(20) | Yes | NULL | `'razorpay'` or `'stripe'` |
| `billing_customer_id` | String(100) | Yes | NULL | Provider customer ID |
| `billing_subscription_id` | String(100) | Yes | NULL | Provider subscription ID |
| `billing_subscription_status` | String(50) | Yes | NULL | Provider subscription status |
| `current_period_end` | DateTime | Yes | NULL | Next billing date |
| `past_due_at` | DateTime | Yes | NULL | Date became past due |
| `billing_exempt` | Boolean | No | `False` | Bypass billing enforcement |
| `currency` | String(3) | No | `'USD'` | Billing currency |
| `updated_at` | DateTime | No | `utcnow` | Auto-updated on change |

### Constraints and Indexes

| Type | Name | Columns |
|------|------|---------|
| PRIMARY KEY | (auto) | `id` |
| UNIQUE | (auto) | `slug` |
| UNIQUE | `uq_tenants_billing_subscription_id` | `billing_subscription_id` |

### Status Values

`PENDING` | `TRIAL` | `ACTIVE` | `PAST_DUE` | `SUSPENDED` | `CANCELLED` | `DELETED`

### Plan Values

`STARTER` | `GROWTH` | `PROFESSIONAL` | `ENTERPRISE`

### Relationships

- **One-to-Many** with `User` — a tenant has many users
- **One-to-Many** with `ConversationState` — a tenant has many leads
- **One-to-Many** with `ConversationMessage` — a tenant has many messages
- **One-to-Many** with `MessageLog` — a tenant has many log entries
- **One-to-Many** with `LeadEvent` — a tenant has many events
- **One-to-Many** with `FollowUpJob` — a tenant has many follow-up jobs
- **One-to-Many** with `PendingMessage` — a tenant has many pending messages
- **One-to-Many** with `BillingInvoice` — a tenant has many invoices

### Lifecycle

Tenants progress through status values as business events occur. Deletion is **soft-only** — setting `status = 'DELETED'`. No rows are hard-deleted.

### Tenant Isolation Role

The `id` field (UUID) is the foreign key referenced by every tenant-scoped model as `tenant_id`. This is the foundation of all data isolation in Oxford CRM.

---

## 4. User

**Source:** `app/models.py` lines 72–99
**Table:** `users`
**Phase:** 10 (foundation), 13-A2B (email + composite uniqueness)

### Purpose

Represents authenticated CRM users — either platform Super Admins (no tenant) or tenant-level Admins and Staff.

### Full Column Reference

| Column | Type | Nullable | Default | Description |
|--------|------|---------|---------|-------------|
| `id` | Integer | No | auto | **Primary Key** |
| `username` | String(64) | No | — | Login username |
| `password_hash` | String(256) | No | — | Werkzeug PBKDF2 hash |
| `role` | String(20) | No | `'STAFF'` | `SUPER_ADMIN` \| `ADMIN` \| `STAFF` |
| `is_active` | Boolean | No | `True` | Deactivated users cannot log in |
| `require_password_change` | Boolean | No | `False` | Forces setup-password on next login |
| `tenant_id` | String(36) | Yes | NULL | FK → `tenants.id`. NULL for SUPER_ADMIN |
| `created_at` | DateTime | Yes | `utcnow` | Account creation timestamp |
| `last_login` | DateTime | Yes | NULL | Last successful login |
| `email` | String(120) | Yes | NULL | Required for SUPER_ADMIN login |

### Constraints and Indexes

| Type | Name | Columns |
|------|------|---------|
| PRIMARY KEY | (auto) | `id` |
| UNIQUE | (auto) | `email` |
| UNIQUE | `uq_users_tenant_username` | `(tenant_id, username)` |
| INDEX | `ix_users_tenant_id` | `tenant_id` |

### Foreign Keys

| Column | References | On Delete |
|--------|-----------|----------|
| `tenant_id` | `tenants.id` | Not cascaded (manual management) |

### Tenant Ownership

- `SUPER_ADMIN` users have `tenant_id = NULL` — they belong to the platform, not any tenant
- `ADMIN` and `STAFF` users always have a non-null `tenant_id`

### Soft Delete Policy

No hard delete. Deactivation via `is_active = False`. The user record is preserved for audit.

---

## 5. ConversationState (Lead)

**Source:** `app/models.py` lines 101–154
**Table:** `conversation_state`
**Phase:** Initial → Phase 4A (CRM expansion) → Phase 11-D1 (opt-out) → Phase 12-B (tenant_id)

### Purpose

This is effectively the **Lead record** in Oxford CRM. One row per WhatsApp phone number per tenant. Stores the complete current state of a lead — their conversation stage, course selection, CRM status, staff assignment, and all CRM metadata.

### Full Column Reference

| Column | Type | Nullable | Default | Description |
|--------|------|---------|---------|-------------|
| `id` | Integer | No | auto | **Primary Key** |
| `phone` | String(20) | No | — | WhatsApp phone number (E.164 format) |
| `name` | String(200) | No | `""` | Lead's WhatsApp display name |
| `stage` | String(50) | No | `"new"` | Current conversation stage |
| `course` | String(200) | No | `""` | Currently selected course |
| `goal` | String(50) | No | `""` | Lead's stated career goal |
| `batch_time` | String(100) | No | `""` | Preferred demo batch time |
| `offer_course` | String(50) | No | `""` | Course offered in payment link |
| `last_msg` | String(50) | No | `""` | ISO timestamp of last message |
| `last_text` | Text | No | `""` | Full text of last message |
| `updated_at` | DateTime | No | `utcnow` | Auto-updated on change |
| `created_at` | DateTime | No | `utcnow` | Lead creation timestamp |
| `lead_status` | String(50) | Yes | `"Lead"` | CRM pipeline status label |
| `assigned_staff` | String(100) | Yes | NULL | Assigned staff member name |
| `lead_score` | Integer | Yes | `0` | Intelligence score (0–100+) |
| `is_admitted` | Boolean | Yes | `False` | True when lead is admitted |
| `notes` | Text | Yes | NULL | Free-form notes by staff |
| `is_opted_out` | Boolean | Yes | `False` | True if lead sent STOP |
| `tenant_id` | String(36) | No | — | FK → `tenants.id` |

### Constraints and Indexes

| Type | Name | Columns |
|------|------|---------|
| PRIMARY KEY | (auto) | `id` |
| UNIQUE | `uq_conversation_state_phone_tenant` | `(phone, tenant_id)` |
| INDEX | `ix_conversation_state_phone` | `phone` |
| INDEX | `ix_conversation_state_tenant_id` | `tenant_id` |

### Foreign Keys

| Column | References |
|--------|-----------|
| `tenant_id` | `tenants.id` |

### Tenant Ownership

Fully tenant-scoped. The composite unique constraint `(phone, tenant_id)` ensures the same phone number can exist as a lead in two different tenants without conflict.

### Soft Delete Policy

No delete. Leads are archived by setting `lead_status = 'Archived'`. Opted-out leads have `is_opted_out = True`.

---

## 6. ConversationMessage

**Source:** `app/models.py` lines 219–254
**Table:** `conversation_message`
**Phase:** 5A

### Purpose

The **CRM-renderable conversation timeline**. Every message shown in the lead detail view comes from this table. Distinct from `MessageLog` — this table is richer and structured for display.

### Full Column Reference

| Column | Type | Nullable | Default | Description |
|--------|------|---------|---------|-------------|
| `id` | Integer | No | auto | **Primary Key** |
| `phone` | String(20) | No | — | Lead's phone number |
| `direction` | String(10) | No | — | `"incoming"` \| `"outgoing"` |
| `message` | Text | Yes | NULL | Message content |
| `message_type` | String(20) | Yes | NULL | `"text"` \| `"interactive"` \| `"template"` \| `"system"` |
| `source` | String(20) | Yes | NULL | `"user"` \| `"ai"` \| `"manual"` \| `"followup"` \| `"system"` |
| `staff_name` | String(100) | Yes | NULL | Set only for manual CRM sends |
| `wa_message_id` | String(100) | Yes | NULL | WhatsApp message ID (dedup key) |
| `created_at` | DateTime | No | `utcnow` | Message timestamp |
| `tenant_id` | String(36) | No | — | FK → `tenants.id` |

### Constraints and Indexes

| Type | Name | Columns |
|------|------|---------|
| PRIMARY KEY | (auto) | `id` |
| INDEX | `idx_conv_msg_phone_created` | `(phone, created_at)` |
| INDEX | `idx_conv_msg_wa_id` | `wa_message_id` |
| INDEX | `ix_conversation_message_tenant_id` | `tenant_id` |

**Note:** `wa_message_id` has an index (not unique constraint) — deduplication logic in `webhook.py` queries this index.

### Foreign Keys

| Column | References |
|--------|-----------|
| `tenant_id` | `tenants.id` |

### Lifecycle

Append-only. Messages are never updated or deleted. The `direction` and `source` fields combined tell the full story of who sent what and why.

---

## 7. MessageLog

**Source:** `app/models.py` lines 197–216
**Table:** `message_log`
**Phase:** 4D

### Purpose

**Raw technical event log** — lightweight and immutable. Every inbound and outbound message event is recorded here. This is the audit trail layer. Distinct from `ConversationMessage` (which is for CRM display).

### Full Column Reference

| Column | Type | Nullable | Default | Description |
|--------|------|---------|---------|-------------|
| `id` | Integer | No | auto | **Primary Key** |
| `phone` | String(20) | No | — | Phone number |
| `direction` | String(10) | No | — | `"inbound"` \| `"outbound"` |
| `message_type` | String(20) | No | — | `"user"` \| `"ai"` \| `"followup"` \| `"manual"` |
| `message_text` | Text | Yes | NULL | Message content |
| `meta_json` | Text | Yes | NULL | Optional JSON metadata string |
| `created_at` | DateTime | No | `utcnow` | Event timestamp |
| `tenant_id` | String(36) | No | — | FK → `tenants.id` |

### Constraints and Indexes

| Type | Name | Columns |
|------|------|---------|
| PRIMARY KEY | (auto) | `id` |
| INDEX | `idx_msg_phone_created` | `(phone, created_at)` |
| INDEX | `ix_message_log_tenant_id` | `tenant_id` |

### Foreign Keys

| Column | References |
|--------|-----------|
| `tenant_id` | `tenants.id` |

### Lifecycle

Append-only. No updates. No deletes. Immutable audit record.

---

## 8. LeadEvent

**Source:** `app/models.py` lines 257–275
**Table:** `lead_event`
**Phase:** 6A

### Purpose

**Named business events** for the sales funnel intelligence engine. High-signal moments that drive the lead scoring algorithm. Each event adds points to `ConversationState.lead_score`.

### Full Column Reference

| Column | Type | Nullable | Default | Description |
|--------|------|---------|---------|-------------|
| `id` | Integer | No | auto | **Primary Key** |
| `phone` | String(20) | No | — | Lead's phone number |
| `event_type` | String(50) | No | — | Named event (see event types) |
| `event_data` | Text | Yes | NULL | Optional context (e.g., course name) |
| `created_at` | DateTime | No | `utcnow` | Event timestamp |
| `tenant_id` | String(36) | No | — | FK → `tenants.id` |

### Event Types and Scores

| Event Type | Score |
|-----------|-------|
| `LEAD_CREATED` | +2 |
| `FIRST_MESSAGE_RECEIVED` | +3 |
| `AI_RESPONSE_SENT` | +5 |
| `COURSE_VIEWED` | +10 |
| `PLACEMENT_ASKED` | +15 |
| `FEES_REQUESTED` | +20 |
| `DEMO_REQUESTED` | +25 |
| `PAYMENT_PENDING` | +30 |

### Constraints and Indexes

| Type | Name | Columns |
|------|------|---------|
| PRIMARY KEY | (auto) | `id` |
| INDEX | `idx_lead_event_phone_created` | `(phone, created_at)` |
| INDEX | `ix_lead_event_tenant_id` | `tenant_id` |

### Lifecycle

Append-only. Events are never updated or deleted. Each qualifying action creates a new row.

---

## 9. FollowUpJob

**Source:** `app/models.py` lines 157–179
**Table:** `follow_up_jobs`
**Phase:** Initial → Phase 12-B (tenant_id) → Phase 11-D2C (retry metadata)

### Purpose

Persistent queue for scheduled follow-up messages. When a new lead is created, 3 jobs are inserted (Day 1, Day 3, Day 7). The follow-up daemon thread polls this table every 5 minutes.

### Full Column Reference

| Column | Type | Nullable | Default | Description |
|--------|------|---------|---------|-------------|
| `id` | Integer | No | auto | **Primary Key** |
| `phone` | String(20) | No | — | Lead's phone number |
| `name` | String(200) | No | `""` | Lead's name |
| `send_at` | DateTime | No | — | Scheduled send time |
| `message` | Text | No | — | Pre-formatted message text |
| `day` | Integer | No | — | Follow-up day number (1, 3, or 7) |
| `done` | Boolean | No | `False` | True when sent or permanently failed |
| `created_at` | DateTime | Yes | `utcnow` | Job creation timestamp |
| `tenant_id` | String(36) | No | — | FK → `tenants.id` |
| `retry_count` | Integer | No | `0` | Number of send attempts |
| `last_attempt_at` | DateTime | Yes | NULL | Timestamp of last attempt |
| `failure_reason` | Text | Yes | NULL | Error message if failed |

### Constraints and Indexes

| Type | Name | Columns |
|------|------|---------|
| PRIMARY KEY | (auto) | `id` |
| INDEX | `ix_follow_up_jobs_done` | `done` |
| INDEX | `ix_follow_up_jobs_phone` | `phone` |
| INDEX | `ix_follow_up_jobs_tenant_id` | `tenant_id` |

### Retry Logic

- Max retries: 3
- Backoff: `send_at = now + timedelta(minutes = 15 * retry_count)`
- After 3 failures: `done = True`, `failure_reason` stores the error

### Lifecycle

Jobs are created on new lead creation. They transition:
`done=False` (pending) → `done=True` (sent or permanently failed)

---

## 10. PendingMessage

**Source:** `app/models.py` lines 182–194
**Table:** `pending_messages`
**Phase:** 11-D3B2

### Purpose

**24-hour Meta window fallback queue.** When a follow-up or campaign message cannot be sent because the 24-hour Meta messaging window has closed (lead hasn't sent a message in 24 hours), the message is stored here and delivered instantly when the lead next sends a message.

### Full Column Reference

| Column | Type | Nullable | Default | Description |
|--------|------|---------|---------|-------------|
| `id` | Integer | No | auto | **Primary Key** |
| `phone` | String(20) | No | — | Lead's phone number |
| `text` | Text | No | — | Message to deliver |
| `created_at` | DateTime | Yes | `utcnow` | Queue entry timestamp |
| `tenant_id` | String(36) | No | — | FK → `tenants.id` |

### Constraints and Indexes

| Type | Name | Columns |
|------|------|---------|
| PRIMARY KEY | (auto) | `id` |
| INDEX | `ix_pending_messages_phone` | `phone` |
| INDEX | `ix_pending_messages_tenant_id` | `tenant_id` |

**Current State:** The model and table exist. The wiring to deliver pending messages on the next inbound message is **partial** — the model is created but not fully integrated in webhook processing.

---

## 11. BillingInvoice

**Source:** `app/models.py` lines 277–296
**Table:** `billing_invoices`
**Phase:** 13-B4.1

### Purpose

**Immutable billing ledger.** Records every payment event for audit and reconciliation. Append-only — no records are ever modified after creation.

### Full Column Reference

| Column | Type | Nullable | Default | Description |
|--------|------|---------|---------|-------------|
| `id` | Integer | No | auto | **Primary Key** |
| `tenant_id` | String(36) | No | — | FK → `tenants.id` |
| `provider` | String(20) | No | — | `'razorpay'` or `'stripe'` |
| `provider_invoice_id` | String(100) | No | — | Provider-assigned invoice ID |
| `amount_paid` | Integer | No | — | Amount in minor units (paise for INR, cents for USD) |
| `tax_amount` | Integer | No | `0` | Tax in minor units |
| `currency` | String(3) | No | — | ISO currency code (`INR`, `USD`) |
| `status` | String(20) | No | — | `'paid'` \| `'failed'` \| `'open'` |
| `hosted_invoice_url` | String(500) | Yes | NULL | Provider-hosted PDF URL |
| `billing_period_start` | DateTime | Yes | NULL | Billing period start |
| `billing_period_end` | DateTime | Yes | NULL | Billing period end |
| `created_at` | DateTime | Yes | `utcnow` | Record creation timestamp |

### Constraints and Indexes

| Type | Name | Columns |
|------|------|---------|
| PRIMARY KEY | (auto) | `id` |
| UNIQUE | (auto) | `provider_invoice_id` |
| INDEX | `ix_billing_invoices_tenant_id` | `tenant_id` |

**Current State:** Table exists. No records. Handlers to create records on payment events are stubs (Phase 16).

---

## 12. Model Relationship Summary

```
Tenant (tenants)
  │── [1:N] User (users) — via users.tenant_id
  │── [1:N] ConversationState (conversation_state) — via .tenant_id
  │── [1:N] ConversationMessage (conversation_message) — via .tenant_id
  │── [1:N] MessageLog (message_log) — via .tenant_id
  │── [1:N] LeadEvent (lead_event) — via .tenant_id
  │── [1:N] FollowUpJob (follow_up_jobs) — via .tenant_id
  │── [1:N] PendingMessage (pending_messages) — via .tenant_id
  └── [1:N] BillingInvoice (billing_invoices) — via .tenant_id

ConversationState — linked by phone+tenant_id (no FK, by convention):
  ├── ConversationMessage (phone, tenant_id)
  ├── MessageLog (phone, tenant_id)
  ├── LeadEvent (phone, tenant_id)
  ├── FollowUpJob (phone, tenant_id)
  └── PendingMessage (phone, tenant_id)
```

**Important:** `phone` is used as a natural key to link conversation records. It is NOT a formal foreign key — all cross-model joins on `phone` are application-level (not database-enforced). This is intentional for performance and WhatsApp messaging flexibility.

---

## 13. Soft Delete Policy

| Model | Soft Delete Mechanism |
|-------|----------------------|
| `Tenant` | `status = 'DELETED'` |
| `User` | `is_active = False` |
| `ConversationState` | `lead_status = 'Archived'` or `is_opted_out = True` |
| `ConversationMessage` | No delete — append-only |
| `MessageLog` | No delete — append-only |
| `LeadEvent` | No delete — append-only |
| `FollowUpJob` | `done = True` (completed or failed) |
| `PendingMessage` | Row deleted after delivery |
| `BillingInvoice` | No delete — immutable ledger |

**Rule:** No `DROP` or hard `DELETE` is ever performed on any tenant-scoped data in production.

---

## 14. Audit Considerations

| Model | Audit Fields |
|-------|-------------|
| `Tenant` | `created_at`, `updated_at` |
| `User` | `created_at`, `last_login` |
| `ConversationState` | `created_at`, `updated_at` |
| `ConversationMessage` | `created_at`, `staff_name` |
| `MessageLog` | `created_at` |
| `LeadEvent` | `created_at` |
| `FollowUpJob` | `created_at`, `last_attempt_at`, `failure_reason`, `retry_count` |
| `PendingMessage` | `created_at` |
| `BillingInvoice` | `created_at` |

All timestamps are **UTC**. No timezone conversion is performed in the database. Frontend display converts to local time.

---

## 15. Models Not Yet Implemented

The following models are in the **future roadmap** and do not yet exist in `app/models.py`:

| Planned Model | Target Phase | Purpose |
|--------------|-------------|---------|
| `TenantPlan` | Phase 16 | Detailed plan limit configuration |
| `AuditLog` | Phase 16 | Platform-wide action audit trail |
| `Course` | Phase 17 | LMS course catalogue |
| `Batch` | Phase 17 | LMS course batch scheduling |
| `Enrollment` | Phase 17 | Student-course link |
| `Student` | Phase 18 | Student portal user |
| `Notification` | Phase 18 | In-app notifications |

---

*Oxford CRM Documentation — docs/03_database/DATABASE_BIBLE.md*
*Source-verified line-by-line against `app/models.py` (297 lines, 2026-07-02)*
*Cross-references: `ERD.md` · `TABLES.md` · `MIGRATIONS.md` · `SCHEMA_RULES.md`*
