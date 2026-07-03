# Oxford CRM — Entity Relationship Diagrams
## Visual Data Model Reference

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Database Engineering
> **Audience:** Engineers, AI Assistants, Architects
> **Last Updated:** 2026-07-02 | **Next Review:** After any schema change
> **Source Authority:** Verified against `app/models.py` — only real models are shown

---

## Table of Contents

1. [Complete ERD — All Models](#1-complete-erd--all-models)
2. [Tenant and User Relationship](#2-tenant-and-user-relationship)
3. [Lead (ConversationState) and Related Tables](#3-lead-conversationstate-and-related-tables)
4. [Messaging and Logging Layer](#4-messaging-and-logging-layer)
5. [Billing Layer](#5-billing-layer)
6. [Phone-Based Link Convention](#6-phone-based-link-convention)
7. [Relationship Summary Table](#7-relationship-summary-table)

---

## 1. Complete ERD — All Models

```
┌────────────────────────────────────────────────────────────────────────────┐
│                         OXFORD CRM — COMPLETE ERD                          │
│                    (All models from app/models.py)                         │
└────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────┐
│  TENANTS                                 │
│  id (PK, String 36, UUID)               │◄──────────────────┐
│  name (String 100, NOT NULL)            │                   │
│  slug (String 30, UNIQUE, NOT NULL)     │                   │
│  status (String 20, NOT NULL)           │                   │
│  plan (String 20, NOT NULL)             │                   │
│  trial_ends_at (DateTime, NULL)         │                   │
│  billing_email (String 100, NULL)       │                   │
│  industry (String 50, NOT NULL)         │                   │
│  waba_phone_number_id (String 50, NULL) │                   │
│  waba_access_token_encrypted (Text, NULL│                   │
│  ai_persona_name (String 50, NULL)      │                   │
│  ai_prompt_override (Text, NULL)        │                   │
│  billing_provider (String 20, NULL)     │                   │
│  billing_customer_id (String 100, NULL) │                   │
│  billing_subscription_id (String 100)   │                   │
│  billing_subscription_status (NULL)     │                   │
│  current_period_end (DateTime, NULL)    │                   │
│  past_due_at (DateTime, NULL)           │                   │
│  billing_exempt (Boolean, NOT NULL)     │                   │
│  currency (String 3, NOT NULL)          │                   │
│  created_at (DateTime)                  │                   │
│  updated_at (DateTime, NOT NULL)        │                   │
└──────────────────┬───────────────────────┘                   │
                   │ 1                                          │
              ─────┼───────────────────────────────────────────┤
              │    │    │    │    │    │    │    │              │
              │    │    │    │    │    │    │    │              │
              N    N    N    N    N    N    N    N              │
              ▼    ▼    ▼    ▼    ▼    ▼    ▼    ▼              │
                                                               │
 ┌──────────┐ ┌─────────────────┐ ┌──────────────────┐        │
 │ USERS    │ │CONVERSATION_    │ │CONVERSATION_     │        │
 │(FK:      │ │STATE (Lead)     │ │MESSAGE           │        │
 │tenant_id)│ │(FK: tenant_id)  │ │(FK: tenant_id)   │        │
 └──────────┘ └─────────────────┘ └──────────────────┘        │
                                                               │
 ┌──────────┐ ┌─────────────────┐ ┌──────────────────┐        │
 │MESSAGE_  │ │LEAD_EVENT       │ │FOLLOW_UP_JOBS    │        │
 │LOG       │ │(FK: tenant_id)  │ │(FK: tenant_id)   │        │
 │(FK:      │ └─────────────────┘ └──────────────────┘        │
 │tenant_id)│                                                  │
 └──────────┘ ┌─────────────────┐ ┌──────────────────┐        │
              │PENDING_MESSAGES │ │BILLING_INVOICES  │────────┘
              │(FK: tenant_id)  │ │(FK: tenant_id)   │
              └─────────────────┘ └──────────────────┘
```

---

## 2. Tenant and User Relationship

```
┌─────────────────────────────────────┐
│  tenants                            │
│  ─────────────────────────────────  │
│  id (PK) String(36) UUID            │
│  name    String(100) NOT NULL        │
│  slug    String(30)  UNIQUE NOT NULL │
│  status  String(20)  NOT NULL        │
│  plan    String(20)  NOT NULL        │
│  ...                                │
└────────────────────┬────────────────┘
                     │ 1
                     │
                     │ FK: users.tenant_id → tenants.id
                     │
                     ▼ N
┌─────────────────────────────────────┐
│  users                              │
│  ─────────────────────────────────  │
│  id (PK) Integer AUTO               │
│  username    String(64) NOT NULL    │
│  password_hash String(256) NOT NULL │
│  role        String(20)  NOT NULL   │  Values: SUPER_ADMIN | ADMIN | STAFF
│  is_active   Boolean                │
│  require_password_change Boolean    │
│  tenant_id   String(36) FK NULL     │  NULL for SUPER_ADMIN
│  created_at  DateTime               │
│  last_login  DateTime NULL          │
│  email       String(120) UNIQUE NULL│
│                                     │
│  CONSTRAINT: uq_users_tenant_username│
│  UNIQUE (tenant_id, username)       │
└─────────────────────────────────────┘
```

**Notes:**
- SUPER_ADMIN users have `tenant_id = NULL` — they are platform-level, not tenant-level
- The composite unique constraint allows "admin" username to exist in multiple tenants
- `email` has a global unique constraint — used for SUPER_ADMIN login

---

## 3. Lead (ConversationState) and Related Tables

```
┌────────────────────────────────────────────────────┐
│  conversation_state  (The "Lead" Record)            │
│  ──────────────────────────────────────────────    │
│  id (PK) Integer                                   │
│  phone      String(20) INDEX                       │
│  name       String(200)                            │
│  stage      String(50)  Default: "new"             │
│  course     String(200)                            │
│  goal       String(50)                             │
│  batch_time String(100)                            │
│  offer_course String(50)                           │
│  last_msg   String(50)  — ISO timestamp            │
│  last_text  Text                                   │
│  updated_at DateTime    AUTO-UPDATE                │
│  created_at DateTime                               │
│  lead_status    String(50)  Default: "Lead"        │
│  assigned_staff String(100) NULL                   │
│  lead_score     Integer     Default: 0             │
│  is_admitted    Boolean     Default: False         │
│  notes          Text        NULL                   │
│  is_opted_out   Boolean     Default: False         │
│  tenant_id      String(36)  FK INDEX NOT NULL      │
│                                                    │
│  CONSTRAINT: uq_conversation_state_phone_tenant   │
│  UNIQUE (phone, tenant_id)                         │
└───────────────────┬────────────────────────────────┘
                    │
         phone + tenant_id (application-level link)
         ─────────────────────────────────
         │               │              │
         ▼               ▼              ▼
┌──────────────┐ ┌──────────────┐ ┌────────────────┐
│ lead_event   │ │follow_up_jobs│ │pending_messages│
│              │ │              │ │                │
│ id (PK)      │ │ id (PK)      │ │ id (PK)        │
│ phone  INDEX │ │ phone  INDEX │ │ phone  INDEX   │
│ event_type   │ │ send_at      │ │ text           │
│ event_data   │ │ message      │ │ created_at     │
│ created_at   │ │ day          │ │ tenant_id FK   │
│ tenant_id FK │ │ done   INDEX │ │                │
│              │ │ retry_count  │ │                │
│ APPEND-ONLY  │ │ last_attempt │ │                │
│              │ │ failure_rsn  │ │                │
│              │ │ tenant_id FK │ │                │
└──────────────┘ └──────────────┘ └────────────────┘
```

---

## 4. Messaging and Logging Layer

Two distinct message tables with different purposes:

```
                    ┌─────────────────────────────┐
                    │  WhatsApp Message Arrives    │
                    │  (inbound from Meta)         │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    │                             │
                    ▼                             ▼
┌──────────────────────────────┐  ┌──────────────────────────────────┐
│  message_log                 │  │  conversation_message            │
│  ────────────────────────    │  │  ────────────────────────────    │
│  PURPOSE: Technical audit    │  │  PURPOSE: CRM display timeline   │
│  PHASE: 4D                   │  │  PHASE: 5A                       │
│                              │  │                                  │
│  id (PK) Integer             │  │  id (PK) Integer                 │
│  phone   String(20) INDEX    │  │  phone    String(20)             │
│  direction  String(10)       │  │  direction String(10)            │
│    "inbound" | "outbound"    │  │    "incoming" | "outgoing"       │
│  message_type String(20)     │  │  message      Text               │
│    "user"|"ai"|"followup"... │  │  message_type String(20)         │
│  message_text Text           │  │  source       String(20)         │
│  meta_json    Text (JSON)    │  │  staff_name   String(100) NULL   │
│  created_at   DateTime       │  │  wa_message_id String(100) NULL  │
│  tenant_id    FK             │  │  created_at   DateTime           │
│                              │  │  tenant_id    FK                 │
│  COMPOSITE INDEX:            │  │                                  │
│  (phone, created_at)         │  │  COMPOSITE INDEXES:              │
│                              │  │  (phone, created_at)             │
│  APPEND-ONLY                 │  │  (wa_message_id) ← dedup check  │
│  NO UPDATES                  │  │                                  │
│  NO DELETES                  │  │  APPEND-ONLY                     │
└──────────────────────────────┘  └──────────────────────────────────┘
```

**Key Difference:**
- `message_log` → raw technical record, `direction` = `inbound/outbound`
- `conversation_message` → CRM display record, `direction` = `incoming/outgoing`
- Both are append-only. Neither has foreign keys to `conversation_state` (linked by `phone + tenant_id` at application level).

---

## 5. Billing Layer

```
┌─────────────────────────────────────────────┐
│  tenants (billing fields subset)            │
│  ─────────────────────────────────────────  │
│  id (PK)                                   │
│  billing_provider          String(20) NULL  │  'razorpay' | 'stripe'
│  billing_customer_id       String(100) NULL │
│  billing_subscription_id   String(100) NULL │  UNIQUE constraint
│  billing_subscription_status String(50) NULL│
│  current_period_end        DateTime NULL    │
│  past_due_at               DateTime NULL    │
│  billing_exempt            Boolean NOT NULL │
│  currency                  String(3) NOT NULL│
└──────────────────────────┬──────────────────┘
                           │ 1
                           │ FK: billing_invoices.tenant_id → tenants.id
                           │
                           ▼ N
┌─────────────────────────────────────────────┐
│  billing_invoices                           │
│  ─────────────────────────────────────────  │
│  id (PK) Integer                           │
│  tenant_id         String(36) FK INDEX      │
│  provider          String(20) NOT NULL      │  'razorpay' | 'stripe'
│  provider_invoice_id String(100) UNIQUE     │
│  amount_paid       Integer NOT NULL         │  In minor units (paise/cents)
│  tax_amount        Integer NOT NULL         │  Default: 0
│  currency          String(3) NOT NULL       │
│  status            String(20) NOT NULL      │  'paid' | 'failed' | 'open'
│  hosted_invoice_url String(500) NULL        │
│  billing_period_start DateTime NULL         │
│  billing_period_end   DateTime NULL         │
│  created_at           DateTime              │
│                                             │
│  IMMUTABLE LEDGER — NO UPDATES, NO DELETES │
│  Current status: EMPTY (Phase 16 activates)│
└─────────────────────────────────────────────┘
```

---

## 6. Phone-Based Link Convention

Many tables are linked by `phone + tenant_id` rather than formal foreign keys. This is a deliberate design decision.

```
conversation_state  (phone="919447XXXXXX", tenant_id="abc123")
        │
        │ Application-level join (NOT database FK)
        ├──► conversation_message  (phone="919447XXXXXX", tenant_id="abc123")
        ├──► message_log           (phone="919447XXXXXX", tenant_id="abc123")
        ├──► lead_event            (phone="919447XXXXXX", tenant_id="abc123")
        ├──► follow_up_jobs        (phone="919447XXXXXX", tenant_id="abc123")
        └──► pending_messages      (phone="919447XXXXXX", tenant_id="abc123")
```

**Why no FK to conversation_state?**
1. **Decoupling:** Message logs can exist before a `ConversationState` row is created
2. **Performance:** High-write tables (message_log, conversation_message) don't need FK constraint overhead
3. **Flexibility:** Phone number format changes don't cascade

**Implication for developers:** Always filter by both `phone` AND `tenant_id` when joining these tables at the application layer. Never join by `phone` alone.

---

## 7. Relationship Summary Table

| Parent Table | Child Table | Join Column | Type | Enforced By |
|-------------|------------|------------|------|------------|
| `tenants` | `users` | `users.tenant_id` | 1:N | DB FK constraint |
| `tenants` | `conversation_state` | `.tenant_id` | 1:N | DB FK constraint |
| `tenants` | `conversation_message` | `.tenant_id` | 1:N | DB FK constraint |
| `tenants` | `message_log` | `.tenant_id` | 1:N | DB FK constraint |
| `tenants` | `lead_event` | `.tenant_id` | 1:N | DB FK constraint |
| `tenants` | `follow_up_jobs` | `.tenant_id` | 1:N | DB FK constraint |
| `tenants` | `pending_messages` | `.tenant_id` | 1:N | DB FK constraint |
| `tenants` | `billing_invoices` | `.tenant_id` | 1:N | DB FK constraint |
| `conversation_state` | `conversation_message` | `phone + tenant_id` | 1:N | Application-level |
| `conversation_state` | `message_log` | `phone + tenant_id` | 1:N | Application-level |
| `conversation_state` | `lead_event` | `phone + tenant_id` | 1:N | Application-level |
| `conversation_state` | `follow_up_jobs` | `phone + tenant_id` | 1:N | Application-level |
| `conversation_state` | `pending_messages` | `phone + tenant_id` | 1:N | Application-level |

---

*Oxford CRM Documentation — docs/03_database/ERD.md*
*All models shown are REAL — no invented or hypothetical tables.*
*Cross-references: `DATABASE_BIBLE.md` · `TABLES.md` · `02_architecture/TENANT_ARCHITECTURE.md`*
