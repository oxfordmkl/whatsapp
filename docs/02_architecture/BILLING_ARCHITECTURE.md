# Oxford CRM — Billing Architecture
## SaaS Billing Foundation and Razorpay Strategy

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Architecture Team
> **Audience:** Engineers, AI Assistants, Business Stakeholders
> **Last Updated:** 2026-07-02 | **Next Review:** Phase 16 (Subscription Engine)
> **Source Authority:** Verified against `app/models.py`, `app/routes/billing.py`, `app/routes/tenant.py`

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [Billing Architecture Overview](#2-billing-architecture-overview)
3. [Provider-Agnostic Design](#3-provider-agnostic-design)
4. [Current Provider — Razorpay](#4-current-provider--razorpay)
5. [Billing Data Model](#5-billing-data-model)
6. [Billing Status State Machine](#6-billing-status-state-machine)
7. [Billing Middleware](#7-billing-middleware)
8. [Webhook Foundation](#8-webhook-foundation)
9. [BillingInvoice Ledger](#9-billinginvoice-ledger)
10. [Oxford Computers Exemption](#10-oxford-computers-exemption)
11. [Trial Lifecycle](#11-trial-lifecycle)
12. [Current Production Status](#12-current-production-status)
13. [Known Limitations](#13-known-limitations)
14. [Future Roadmap — Phase 16](#14-future-roadmap--phase-16)
15. [Related Documents](#15-related-documents)

---

## 1. Purpose and Scope

This document describes the billing architecture of Oxford CRM as it currently exists in production (Phase 13-B4.1C foundation). It covers the data model, billing middleware, Razorpay strategy, and the billing state machine.

**Current State:** The billing **foundation** is implemented and live. This means:
- The database schema exists
- The billing middleware is enforcing access control based on tenant status
- The Razorpay webhook endpoint is registered and reachable
- **No live subscription processing is active** — payment events are received but handlers are stubs

**Stripe:** Explicitly deferred. The architecture is designed to support Stripe in future. No Stripe-specific code should be implemented until Phase 16+ global SaaS expansion.

---

## 2. Billing Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     Billing Architecture                         │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                  Billing Data Model                      │   │
│  │  Tenant.billing_provider         (razorpay / stripe)   │   │
│  │  Tenant.billing_subscription_status (ACTIVE/TRIAL/etc) │   │
│  │  Tenant.billing_exempt           (Oxford bypass)        │   │
│  │  BillingInvoice                  (append-only ledger)   │   │
│  └─────────────────────────────────────────────────────────┘   │
│                          │                                       │
│                          ▼                                       │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                  Billing Middleware                      │   │
│  │  check_billing_status() in admin_bp / tenant_bp         │   │
│  │  Blocks access if status = SUSPENDED / CANCELLED        │   │
│  │  Exempt: billing_exempt=True AND slug='oxford'          │   │
│  └─────────────────────────────────────────────────────────┘   │
│                          │                                       │
│  ┌──────────────────────────────────────────────────────┐      │
│  │              Webhook Endpoints (billing_bp)           │      │
│  │  POST /webhooks/razorpay ← Razorpay sends events     │      │
│  │  POST /webhooks/stripe   ← Stripe sends events (stub)│      │
│  │  Both currently log and return 200 OK (stubs)        │      │
│  └──────────────────────────────────────────────────────┘      │
│                                                                  │
│  ┌──────────────────────────────────────────────────────┐      │
│  │           External: Razorpay Dashboard               │      │
│  │  (India billing, INR, UPI, cards, EMI support)       │      │
│  └──────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Provider-Agnostic Design

The billing architecture was designed from Phase 13-B4.1C to support any payment provider without schema changes.

**Key design decision:** Billing provider identity is stored in `Tenant.billing_provider` as a string (`'razorpay'` or `'stripe'`). The rest of the billing fields (`billing_customer_id`, `billing_subscription_id`, etc.) are provider-neutral — they store whatever the provider calls these values.

This means:
- Adding Stripe in Phase 16 requires **no schema migration** for the billing fields
- The billing middleware checks `billing_subscription_status` — a normalized value regardless of provider
- Invoice records in `BillingInvoice` include a `provider` field for traceability

### Current Provider

```python
# Razorpay events handled (foundation stubs):
'subscription.activated'  → tenant.status = 'ACTIVE' (TODO: implement)
'subscription.charged'    → create BillingInvoice (TODO: implement)
'subscription.halted'     → tenant.status = 'PAST_DUE' (TODO: implement)
'subscription.cancelled'  → tenant.status = 'CANCELLED' (TODO: implement)
```

---

## 4. Current Provider — Razorpay

**Razorpay** is the production billing provider for the India/Kerala release.

| Property | Value |
|----------|-------|
| Provider | Razorpay |
| Currency | INR (Indian Rupee) |
| Payment methods | UPI, cards, net banking, EMI |
| Subscription model | Recurring billing |
| Webhook URL | `POST /webhooks/razorpay` |
| Webhook secret | `RAZORPAY_WEBHOOK_SECRET` env var (not yet set) |

**Stripe:** Endpoint exists (`/webhooks/stripe`) as a stub. Stripe is **explicitly deferred** to the Global SaaS phase (Phase 20+). Do not implement Stripe-specific code without explicit user approval.

---

## 5. Billing Data Model

All billing fields live directly on the `Tenant` model. There is no separate subscription table.

**Source:** `app/models.py` — `class Tenant(db.Model):` (Phase 13-B4.1 fields)

| Field | Type | Description |
|-------|------|-------------|
| `billing_provider` | String(20) | `'razorpay'` or `'stripe'` |
| `billing_customer_id` | String(100) | Provider-assigned customer ID |
| `billing_subscription_id` | String(100) | Provider-assigned subscription ID (unique) |
| `billing_subscription_status` | String(50) | Provider subscription status |
| `current_period_end` | DateTime | Next billing date |
| `past_due_at` | DateTime | Date payment became overdue |
| `billing_exempt` | Boolean | Bypass billing enforcement (Oxford Computers) |
| `currency` | String(3) | Default `'USD'` — ⚠️ Razorpay uses INR (known gap) |

**BillingInvoice Model:**
```
id                  Integer PK
tenant_id           FK → tenants.id
amount              Numeric
currency            String
status              String (paid / failed)
provider            String (razorpay / stripe)
provider_invoice_id String
created_at          DateTime
```

---

## 6. Billing Status State Machine

The `Tenant.status` field drives access control. Billing events transition tenants between states.

```
                  ┌─────────┐
                  │ PENDING │  ← New registration
                  └────┬────┘
                       │ Super Admin approves
                       ▼
                  ┌─────────┐
                  │  TRIAL  │  ← trial_ends_at set
                  └────┬────┘
                       │ Trial expires / payment made
                       ▼
                  ┌─────────┐
         ┌────────│  ACTIVE │◄───────────┐
         │        └────┬────┘            │
         │             │ payment fails    │
         │             ▼                  │ payment recovered
         │        ┌──────────┐           │
         │        │ PAST_DUE │───────────┘
         │        └────┬─────┘
         │             │ continues failing
         │             ▼
         │        ┌───────────┐
         │        │ SUSPENDED │◄── Super Admin action
         │        └────┬──────┘
         │             │ Super Admin deletes
         │             ▼
         │        ┌─────────┐
         └───────►│CANCELLED│
                  └────┬────┘
                       │ Soft delete
                       ▼
                  ┌─────────┐
                  │ DELETED │
                  └─────────┘
```

### Access Control by Status

| Status | CRM Login | WhatsApp | AI | Billing Page |
|--------|-----------|---------|-----|-------------|
| PENDING | ❌ | ❌ | ❌ | ❌ |
| TRIAL | ✅ | ✅ | ✅ | ✅ |
| ACTIVE | ✅ | ✅ | ✅ | ✅ |
| PAST_DUE | ✅ (limited) | ⚠️ | ⚠️ | ✅ |
| SUSPENDED | ❌ | ❌ | ❌ | ❌ |
| CANCELLED | ❌ | ❌ | ❌ | ❌ |

---

## 7. Billing Middleware

**Source:** `app/routes/tenant.py`, lines 29–45 — `@tenant_bp.before_request`

```python
@tenant_bp.before_request
def tenant_security_guard():
    # Skip billing check for the billing page itself
    if request.path.startswith('/tenant/billing'):
        return

    # Allow read-only WhatsApp page during suspension (to view config, not configure)
    if request.path == '/tenant/whatsapp' and request.method == 'GET':
        return

    billing_redirect = check_billing_status()
    if billing_redirect:
        return billing_redirect
```

The `check_billing_status()` function (in `admin.py`) checks the current tenant's status and returns a redirect response if access should be blocked.

### Oxford Computers Exemption

Oxford Computers has `billing_exempt = True` set in the database. The billing check respects this flag:
```python
if tenant.billing_exempt and tenant.slug == 'oxford':
    return None  # No billing enforcement — pass through
```

Both conditions must be True — the `slug == 'oxford'` check ensures this exemption cannot be accidentally applied to other tenants.

---

## 8. Webhook Foundation

**Source:** `app/routes/billing.py`

The billing blueprint (`billing_bp`) is registered with `url_prefix='/webhooks'`.

### Razorpay Endpoint

**Route:** `POST /webhooks/razorpay`

```
Current Implementation:
1. Parse JSON payload
2. Log the event type
3. Route to event handler (currently stubs)
4. Return 200 OK (always — prevents Razorpay from retrying infinitely)

Event Routing Structure (stubs — not yet active):
  'subscription.activated' → TODO: set tenant.status = ACTIVE
  'subscription.charged'   → TODO: create BillingInvoice
  'subscription.halted'    → TODO: set tenant.status = PAST_DUE
  'subscription.cancelled' → TODO: set tenant.status = CANCELLED
```

### Stripe Endpoint (Stub — DEFERRED)

**Route:** `POST /webhooks/stripe`

Endpoint exists to prevent 404 errors if Stripe is accidentally configured. The handler is a stub that logs and returns 200 OK. No Stripe processing logic is implemented.

**Do not implement Stripe logic until explicitly approved for Global SaaS phase.**

---

## 9. BillingInvoice Ledger

The `BillingInvoice` model provides an **append-only financial record** for all billing events.

### Design Principles
- Records are never deleted
- Records are never modified after creation
- Every payment event creates a new record
- Failed payments create failed invoice records (not deleted)

### Current State

The `BillingInvoice` table exists in the database schema but has **no records** because live payment processing is not active. Records will be created in Phase 16 when `handle_subscription_charged()` is implemented.

---

## 10. Oxford Computers Exemption

Oxford Computers is the founding customer. They are **grandfathered** — exempt from billing enforcement.

| Field | Value |
|-------|-------|
| `billing_exempt` | `True` |
| `slug` | `'oxford'` |
| `plan` | `ENTERPRISE` |
| `status` | `ACTIVE` |

The exemption is enforced in `check_billing_status()` with both conditions:
```python
# BOTH must be True for exemption
if tenant.billing_exempt and tenant.slug == 'oxford':
    return None  # ← Bypass billing
```

This double-check ensures that even if `billing_exempt = True` is accidentally set on another tenant, the `slug == 'oxford'` prevents unintended bypass.

---

## 11. Trial Lifecycle

### Current State

The trial lifecycle fields (`trial_ends_at`, `status = TRIAL`) exist in the data model but **no automated trial expiry logic is implemented yet**. Trials are managed manually by Super Admin (status transitions via dashboard).

### Future Implementation (Phase 16)

A background job will check `trial_ends_at` daily and transition expired trials:
```
trial_ends_at < now AND status == 'TRIAL'
    → Check if payment method on file
    → If yes: charge via Razorpay
    → If no: status = 'SUSPENDED'
```

---

## 12. Current Production Status

| Component | Status |
|-----------|--------|
| Billing data model (schema) | ✅ Implemented |
| `billing_exempt` flag (Oxford) | ✅ Active |
| Billing middleware (access guard) | ✅ Active |
| `billing_bp` blueprint registered | ✅ Registered |
| `/webhooks/razorpay` endpoint | ✅ Reachable |
| Razorpay event handlers | ❌ Stub only — not active |
| `BillingInvoice` table | ✅ Schema exists, no records |
| Live subscription processing | ❌ Not active |
| Trial expiry automation | ❌ Not active |
| Stripe integration | 🚫 Explicitly deferred |

---

## 13. Known Limitations

| Limitation | Impact | Resolution Phase |
|-----------|--------|-----------------|
| Razorpay event handlers are stubs | No actual subscription processing | Phase 16 |
| `currency` defaults to USD, Razorpay uses INR | Cosmetic inconsistency in data | Phase 16 |
| No trial expiry automation | Trials must be managed manually | Phase 16 |
| No webhook signature verification | Razorpay webhooks not verified (commented out) | Phase 16 |
| No Razorpay API key configured | Cannot create subscriptions | Phase 16 |

---

## 14. Future Roadmap — Phase 16

**Phase 16 — Subscription Engine** will activate the full billing layer:

1. **Configure Razorpay API credentials** (`RAZORPAY_API_KEY`, `RAZORPAY_WEBHOOK_SECRET`)
2. **Implement webhook signature verification** for `POST /webhooks/razorpay`
3. **Implement event handlers:**
   - `subscription.activated` → `tenant.status = 'ACTIVE'`
   - `subscription.charged` → Create `BillingInvoice` record
   - `subscription.halted` → `tenant.status = 'PAST_DUE'`
   - `subscription.cancelled` → `tenant.status = 'CANCELLED'`
4. **Auto-create subscriptions** when Super Admin approves a new tenant
5. **Trial expiry automation** — background job checks `trial_ends_at`
6. **Fix `currency` field** to default to `'INR'` for Indian deployments
7. **Stripe integration** — only if Global SaaS phase is approved

---

## 15. Related Documents

| Document | Relationship |
|----------|-------------|
| `TENANT_ARCHITECTURE.md` | Tenant status and lifecycle |
| `SYSTEM_ARCHITECTURE.md` | Blueprint registration context |
| `08_deployment/ENVIRONMENT_VARIABLES.md` | Billing-related env vars |
| `01_project/ROADMAP.md` | Phase 16 timeline |
| `07_security/SECRETS.md` | RAZORPAY_WEBHOOK_SECRET |

---

*Oxford CRM Documentation — docs/02_architecture/BILLING_ARCHITECTURE.md*
*Source-verified against: `app/models.py`, `app/routes/billing.py`, `app/routes/tenant.py`*
*Stripe: NOT implemented. Explicitly deferred to Global SaaS phase.*
