# Oxford CRM — Tenant Architecture
## Multi-Tenant Design and Isolation Reference

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Architecture Team
> **Audience:** Engineers, AI Assistants, Security Reviewers
> **Last Updated:** 2026-07-02 | **Next Review:** After Phase 16.0
> **Source Authority:** Verified against `app/models.py`, `app/routes/admin.py`, `app/routes/webhook.py`

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [Multi-Tenant Design](#2-multi-tenant-design)
3. [Tenant Model — Field Reference](#3-tenant-model--field-reference)
4. [Tenant Lifecycle](#4-tenant-lifecycle)
5. [Tenant Isolation Rules](#5-tenant-isolation-rules)
6. [Query Isolation — `tenant_query()`](#6-query-isolation--tenant_query)
7. [Session Isolation — Impersonation](#7-session-isolation--impersonation)
8. [WABA Isolation](#8-waba-isolation)
9. [AI Isolation](#9-ai-isolation)
10. [Billing Isolation](#10-billing-isolation)
11. [Current Production State](#11-current-production-state)
12. [Known Limitations](#12-known-limitations)
13. [Future Scaling Strategy](#13-future-scaling-strategy)
14. [Related Documents](#14-related-documents)

---

## 1. Purpose and Scope

This document defines the multi-tenant architecture of Oxford CRM — how tenants are defined, how their data is isolated, and how the system enforces boundaries between tenants at every layer.

**Scope:** Current implementation in Kerala Release Candidate v1.0.

---

## 2. Multi-Tenant Design

Oxford CRM uses a **shared-database, shared-schema** multi-tenancy pattern. All tenants share:
- The same PostgreSQL database instance
- The same table schema
- The same Flask application process

Isolation is enforced at the **application layer** via `tenant_id` foreign keys on every data model, and at the **query layer** via the `tenant_query()` function.

### Tenant Hierarchy

```
SUPER_ADMIN (platform owner)
    │
    └── Tenant A (e.g., Oxford Computers)
    │       ├── ADMIN user (Tenant Admin)
    │       └── STAFF users
    │
    └── Tenant B (future: another institution)
            ├── ADMIN user
            └── STAFF users
```

### Design Rationale

The shared-schema approach was chosen because:
- **Simpler deployment** — one database instance on Railway
- **Easier migrations** — one Alembic migration applies to all tenants
- **Lower cost** — one PostgreSQL service instead of N services
- **Acceptable for Kerala scale** (1–10 tenants)

At 1,000+ tenants, a schema-per-tenant or database-per-tenant approach may be evaluated.

---

## 3. Tenant Model — Field Reference

**Source:** `app/models.py` — `class Tenant(db.Model):`

| Field | Type | Description |
|-------|------|-------------|
| `id` | String(36) | UUID primary key — generated at creation |
| `name` | String(100) | Display name (e.g., "Oxford Computers") |
| `slug` | String(30) | URL-safe identifier — **immutable after creation** |
| `status` | String(20) | Lifecycle state (see Section 4) |
| `plan` | String(20) | Subscription tier: `STARTER\|GROWTH\|PROFESSIONAL\|ENTERPRISE` |
| `trial_ends_at` | DateTime | Trial expiry. NULL = not on trial |
| `billing_email` | String(100) | Billing contact email |
| `industry` | String(50) | Vertical for AI prompt defaults. Default: `Education` |
| `waba_phone_number_id` | String(50) | Meta Phone Number ID for WhatsApp routing |
| `waba_access_token_encrypted` | Text | Fernet-encrypted Meta access token |
| `ai_persona_name` | String(50) | Bot display name. NULL = system default (Aaliza) |
| `ai_prompt_override` | Text | Custom system prompt. NULL = system default |
| `billing_provider` | String(20) | `razorpay` or `stripe` |
| `billing_customer_id` | String(100) | Provider customer ID |
| `billing_subscription_id` | String(100) | Provider subscription ID (unique) |
| `billing_subscription_status` | String(50) | Provider subscription status |
| `current_period_end` | DateTime | Next billing date |
| `past_due_at` | DateTime | Date became past due |
| `billing_exempt` | Boolean | `True` = bypass billing. Oxford Computers uses this. |
| `currency` | String(3) | Default `'USD'` (note: Razorpay uses INR — this is a known gap) |
| `created_at` | DateTime | Tenant creation timestamp |
| `updated_at` | DateTime | Last update timestamp (auto-updated) |

---

## 4. Tenant Lifecycle

### Status Values

```
PENDING → TRIAL → ACTIVE → PAST_DUE → SUSPENDED → CANCELLED
                                                 ↘ DELETED
```

| Status | Meaning | Access |
|--------|---------|--------|
| `PENDING` | Registered, awaiting Super Admin approval | Blocked — cannot log in |
| `TRIAL` | Approved, free trial active | Full access |
| `ACTIVE` | Paying subscription active | Full access |
| `PAST_DUE` | Payment overdue | Limited access (read-only in some areas) |
| `SUSPENDED` | Suspended by Super Admin or billing failure | Blocked |
| `CANCELLED` | Subscription cancelled | Blocked |
| `DELETED` | Soft-deleted by Super Admin | Blocked |

### Status Transitions via Super Admin UI

| Action | Route | From → To |
|--------|-------|----------|
| Approve | `POST /crm/super/tenant/<id>/approve` | `PENDING` → `ACTIVE` |
| Suspend | `POST /crm/super/tenant/<id>/suspend` | Any → `SUSPENDED` |
| Reactivate | `POST /crm/super/tenant/<id>/reactivate` | `SUSPENDED` → `ACTIVE` |
| Delete | **Not yet implemented** (Phase 15C) | Any → `DELETED` |
| Archive | **Not yet implemented** (Phase 15C) | Any → `ARCHIVED` |

### Tenant Login Guard

When a non-SUPER_ADMIN user attempts to log in, the following check is applied:
```python
# From admin.py crm_login route:
if tenant.status not in ['ACTIVE', 'TRIAL']:
    flash("Your account is not active. Contact support.")
    return redirect(url_for('admin.crm_login'))
```

SUPER_ADMIN is **exempt** from this check.

---

## 5. Tenant Isolation Rules

### The Golden Rule

> **Every database query that returns tenant-specific data MUST be scoped to a `tenant_id`.**

There are no exceptions to this rule. Cross-tenant data access is a critical security violation.

### Enforcement Mechanism

All tenant-specific models carry a `tenant_id` column:

```python
# Pattern in every tenant-scoped model:
tenant_id = db.Column(db.String(36), db.ForeignKey('tenants.id'), nullable=True, index=True)
```

**Models with tenant isolation:**
- `User` — scoped to tenant via `tenant_id`
- `ConversationState` — scoped via `tenant_id`
- `ConversationMessage` — scoped via `tenant_id`
- `MessageLog` — scoped via `tenant_id`
- `LeadEvent` — scoped via `tenant_id`
- `FollowUpJob` — scoped via `tenant_id`
- `PendingMessage` — scoped via `tenant_id`
- `BillingInvoice` — scoped via `tenant_id`

**Composite unique constraints** prevent data collisions between tenants:
```python
# From User model:
db.UniqueConstraint('tenant_id', 'username', name='uq_users_tenant_username')
# This means: "admin" username can exist in Tenant A AND Tenant B without conflict
```

---

## 6. Query Isolation — `tenant_query()`

**Source:** `app/routes/admin.py`, lines 49–73

The `tenant_query()` function is the **single enforced isolation point** for all database reads.

### Implementation

```python
def tenant_query(model, tenant_id=None):
    try:
        from flask_login import current_user as _cu
        from flask import session

        if getattr(_cu, 'role', None) == 'SUPER_ADMIN':
            impersonate_id = session.get('impersonate_tenant_id')
            if impersonate_id:
                # SUPER_ADMIN impersonating a tenant — scope to that tenant
                return model.query.filter_by(tenant_id=impersonate_id)
            # SUPER_ADMIN not impersonating — unfiltered (sees all tenants)
            return model.query

        tid = tenant_id or getattr(_cu, 'tenant_id', None)
    except Exception:
        tid = tenant_id

    if tid:
        return model.query.filter_by(tenant_id=tid)
    return model.query  # fallback (should not occur in normal operation)
```

### Behavior Matrix

| Caller Role | Impersonating | Result |
|-------------|--------------|--------|
| `ADMIN` | N/A | Filtered to `current_user.tenant_id` |
| `STAFF` | N/A | Filtered to `current_user.tenant_id` |
| `SUPER_ADMIN` | Yes | Filtered to `session['impersonate_tenant_id']` |
| `SUPER_ADMIN` | No | Unfiltered (all tenants) |

### Companion Function: `tenant_filter()`

For complex `db.session.query(...)` chains:
```python
def tenant_filter(query_obj, model, tenant_id=None):
    # Same logic, appends .filter(model.tenant_id == tid) to existing query
```

### Correct Usage Pattern

```python
# ✅ CORRECT — always use tenant_query()
leads = tenant_query(ConversationState, tenant_id).filter_by(is_admitted=False).all()

# ❌ FORBIDDEN — direct unscoped query
leads = ConversationState.query.all()
```

---

## 7. Session Isolation — Impersonation

SUPER_ADMIN can impersonate any tenant. Impersonation state is stored in the Flask session.

### Impersonation Session Keys

| Key | Type | Value |
|-----|------|-------|
| `impersonate_tenant_id` | String | UUID of the impersonated tenant |
| `impersonate_tenant_name` | String | Display name of the impersonated tenant |

### How Impersonation Works

```
SUPER_ADMIN clicks "Impersonate" on tenant row
  │
  ▼
POST /crm/super/tenant/<id>/impersonate
  ├── session['impersonate_tenant_id'] = tenant.id
  └── session['impersonate_tenant_name'] = tenant.name
  │
  ▼
redirect to /crm/home (CRM now shows impersonated tenant's data)
  │
  ▼ tenant_query() reads session['impersonate_tenant_id']
  └── All data scoped to impersonated tenant
```

### Exiting Impersonation

```
SUPER_ADMIN clicks "Exit Impersonation"
  │
  ▼
GET /crm/super/exit-impersonation
  ├── session.pop('impersonate_tenant_id', None)
  └── session.pop('impersonate_tenant_name', None)
  │
  ▼
redirect to /crm/super/dashboard (back to platform view)
```

---

## 8. WABA Isolation

Each tenant has its own WhatsApp Business Account (WABA) credentials stored in the `Tenant` record.

### Inbound Routing (Webhook)

```python
# From app/routes/webhook.py (Phase 13-B4D2):
phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
tenant = Tenant.query.filter_by(waba_phone_number_id=phone_number_id).first()
if tenant:
    tenant_id = tenant.id  # Route message to correct tenant
```

Each tenant's WABA `phone_number_id` uniquely identifies their incoming messages. A message sent to Tenant A's WhatsApp number will never be processed by Tenant B.

### Outbound Sending

Outbound messages use the tenant's **own encrypted access token**:
```python
# whatsapp_service.py decrypts the tenant's token at send time:
token = decrypt_waba_token(tenant.waba_access_token_encrypted)
# API call uses tenant-specific token and phone_number_id
```

### Credential Security

- Tokens are stored encrypted using **Fernet symmetric encryption** (Phase 13-B4B2)
- The `WABA_ENCRYPTION_KEY` environment variable holds the Fernet key
- Only the application can decrypt tokens — they are never stored in plaintext

---

## 9. AI Isolation

Each tenant can configure their own AI persona and system prompt.

### Tenant AI Fields (from `app/models.py`)

| Field | Default | Description |
|-------|---------|-------------|
| `ai_persona_name` | NULL (→ "Aaliza") | Bot display name shown to leads |
| `ai_prompt_override` | NULL (→ `AALIZA_PROMPT`) | Custom system prompt |

### Configuration via Tenant Portal

Tenant admins set these via `/tenant/ai` (tenant portal AI settings page):
```python
# From app/routes/tenant.py:
tenant.ai_persona_name = request.form.get('ai_persona_name', '').strip() or None
tenant.ai_prompt_override = request.form.get('ai_prompt_override', '').strip() or None
```

### Current Implementation Gap

**⚠️ Important:** As of Phase 15A, `ai_prompt_override` is stored in the database and configurable via the portal, but the `gemini_reply()` function in `app/services/ai_service.py` currently uses only the global `AALIZA_PROMPT`. The per-tenant override is **not yet applied in the AI call path**.

This is documented in AI_ARCHITECTURE.md as a known limitation.

---

## 10. Billing Isolation

Each tenant has its own billing record within the `Tenant` model itself (no separate billing table for the subscription record).

**Billing fields are per-tenant:**
- `billing_provider` — which payment processor (razorpay/stripe)
- `billing_customer_id` — unique customer ID with that provider
- `billing_subscription_id` — unique subscription ID (database-level unique constraint)
- `billing_subscription_status` — current subscription state
- `billing_exempt` — overrides billing enforcement for grandfathered tenants

**Oxford Computers Exemption:**
```python
# Oxford Computers has billing_exempt = True
# The billing middleware checks both conditions:
if tenant.billing_exempt and tenant.slug == 'oxford':
    # skip billing check entirely
```

**Invoice Records:** `BillingInvoice` model has `tenant_id` — invoices are always scoped to a tenant.

---

## 11. Current Production State

| Dimension | Status |
|-----------|--------|
| Multi-tenant architecture | ✅ Implemented |
| Tenant isolation (`tenant_query`) | ✅ Implemented — score 95/100 |
| WABA isolation | ✅ Implemented (Phase 13-B4D2) |
| Credential encryption | ✅ Implemented (Fernet, Phase 13-B4B2) |
| Impersonation | ✅ Implemented |
| Per-tenant AI persona (stored) | ✅ Implemented |
| Per-tenant AI prompt (applied in AI calls) | ⚠️ Stored but not yet applied |
| Tenant DELETE/ARCHIVE via UI | ❌ Not implemented (Phase 15C) |
| Active tenants | 1 (Oxford Computers) |

---

## 12. Known Limitations

| Limitation | Impact | Phase |
|-----------|--------|-------|
| `ai_prompt_override` stored but not applied in `gemini_reply()` | All tenants use system default AI prompt | Fix in Phase 16 |
| No tenant DELETE/ARCHIVE via UI | Manual DB required to remove a tenant | Phase 15C |
| `billing_exempt` + slug check is Oxford-specific | Cannot generalize exemption easily | Phase 16 |
| `currency` field defaults to USD, but Razorpay uses INR | Cosmetic inconsistency | Phase 16 |

---

## 13. Future Scaling Strategy

### Phase 16 — AI Prompt Application

Apply `tenant.ai_prompt_override` in `gemini_reply()` when available:
```python
# Proposed change to ai_service.py:
system_prompt = tenant.ai_prompt_override or AALIZA_PROMPT
```

### Phase 17 — Plan Limits Enforcement

Enforce per-plan limits based on `tenant.plan`:
- `STARTER` — 500 leads/month
- `GROWTH` — 2,000 leads/month
- `PROFESSIONAL` — 10,000 leads/month
- `ENTERPRISE` — Unlimited

### Phase 20 — Schema-Per-Tenant Option

At 1,000+ tenants, evaluate migrating to a schema-per-tenant model for stronger database-level isolation. The `slug` field is designed to become the schema name.

---

## 14. Related Documents

| Document | Relationship |
|----------|-------------|
| `SYSTEM_ARCHITECTURE.md` | Overall system context |
| `AUTHENTICATION_ARCHITECTURE.md` | Login and role details |
| `WHATSAPP_ARCHITECTURE.md` | WABA routing details |
| `AI_ARCHITECTURE.md` | AI persona details |
| `BILLING_ARCHITECTURE.md` | Billing isolation details |
| `07_security/TENANT_ISOLATION.md` | Security audit of isolation |
| `03_database/DATABASE_BIBLE.md` | Model definitions |

---

*Oxford CRM Documentation — docs/02_architecture/TENANT_ARCHITECTURE.md*
*Source-verified against: `app/models.py`, `app/routes/admin.py`, `app/routes/webhook.py`, `app/routes/tenant.py`*
