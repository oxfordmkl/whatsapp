# Oxford CRM — Feature Matrix
## Complete Feature Availability by Module

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Product Team
> **Last Updated:** 2026-07-02 | **Next Review:** After Phase 15C

---

## Feature Status Legend

| Symbol | Meaning |
|--------|---------|
| ✅ | Live in production |
| ⚠️ | Partially implemented |
| 🔒 | Architecture exists, not yet activated |
| ❌ | Not implemented |
| 🚫 | Explicitly out of scope (deferred) |

---

## CRM Core

| Feature | Status | Notes |
|---------|--------|-------|
| Lead intake via WhatsApp | ✅ | Automatic via AI |
| Lead list view | ✅ | Tenant-scoped |
| Lead detail view | ✅ | Full conversation history |
| Lead status tracking | ✅ | Multi-stage pipeline |
| Lead assignment to staff | ✅ | Manual assignment |
| Lead scoring | ✅ | Automatic via events |
| Admission recording | ✅ | `is_admitted` flag + LeadEvent |
| Lead notes | ✅ | Free text per lead |
| My Leads (staff view) | ✅ | Staff-scoped filter |
| Unassigned leads | ✅ | Admin view |
| Lead export | ⚠️ | Google Sheets export (legacy) |

---

## Staff Management

| Feature | Status | Notes |
|---------|--------|-------|
| Staff user creation | ✅ | Admin creates staff accounts |
| Staff role assignment | ✅ | STAFF role |
| Staff workload view | ✅ | Admin dashboard |
| Staff performance analytics | ✅ | Per-staff metrics |
| Staff lead allocation | ✅ | Allocation center |
| Staff reassignment | ✅ | Reassignment center |
| Staff deactivation | ✅ | `is_active = False` |

---

## WhatsApp & AI

| Feature | Status | Notes |
|---------|--------|-------|
| Inbound message processing | ✅ | Via `/webhook` |
| AI reply (Gemini) | ✅ | `smart_reply()` function |
| Per-tenant AI persona | ✅ | `ai_persona_name` field |
| Per-tenant AI prompt | ✅ | `ai_prompt_override` field |
| Manual staff reply | ✅ | CRM lead detail page |
| Message history | ✅ | `ConversationMessage` table |
| Opt-out detection | ✅ | `is_opted_out` flag |
| Opt-in recovery | ✅ | On next inbound message |
| Dynamic WABA routing | ✅ | Per `waba_phone_number_id` |
| Deduplication | ✅ | `wa_message_id` check |
| Pending message queue | ✅ | 24-hour window fallback |

---

## Marketing & Campaigns

| Feature | Status | Notes |
|---------|--------|-------|
| Marketing Hub | ✅ | `/crm/marketing` |
| Campaign center | ✅ | `/crm/campaigns` |
| Broadcast to lead list | ✅ | Template messages |
| Campaign scheduling | ⚠️ | Manual trigger |
| Campaign analytics | ✅ | Open rate tracking |

---

## Analytics

| Feature | Status | Notes |
|---------|--------|-------|
| Funnel analytics | ✅ | Stage conversion rates |
| Revenue analytics | ✅ | Per-period revenue |
| Admission analytics | ✅ | Admission metrics |
| Source analytics | ✅ | Lead source breakdown |
| Staff performance | ✅ | Per-staff analytics |
| Action center | ✅ | Priority actions |

---

## Tenant System

| Feature | Status | Notes |
|---------|--------|-------|
| Tenant portal home | ✅ | `/tenant/home` |
| Tenant profile edit | ✅ | Name, industry, billing email |
| Tenant staff management | ✅ | Add/edit/deactivate staff |
| Tenant AI settings | ✅ | Persona + custom prompt |
| Tenant WhatsApp setup | ✅ | WABA credentials |
| Tenant billing view | ✅ | Read-only |
| Tenant sidebar navigation | ✅ | Fixed Phase 14B.3 |

---

## Super Admin

| Feature | Status | Notes |
|---------|--------|-------|
| Super Admin login | ✅ | `/crm/super/login` |
| View all tenants | ✅ | Dashboard table |
| Approve PENDING tenant | ✅ | Status → ACTIVE |
| Suspend tenant | ✅ | Status → SUSPENDED |
| Reactivate tenant | ✅ | Status → ACTIVE |
| Impersonate tenant | ✅ | Session-based |
| Exit impersonation | ✅ | Session cleared |
| Delete tenant | ❌ | Not implemented |
| Archive tenant | ❌ | Not implemented |
| Create tenant | ❌ | Not implemented |
| Tenant billing management | ❌ | Future |

---

## Billing

| Feature | Status | Notes |
|---------|--------|-------|
| Billing architecture | ✅ | Provider-agnostic model |
| Razorpay webhook endpoint | ✅ | `/webhooks/razorpay` |
| Stripe webhook endpoint | 🔒 | Endpoint exists, not used |
| Billing middleware | ✅ | Access control by status |
| Oxford exemption | ✅ | `billing_exempt` flag |
| Live subscription | ❌ | Not configured |
| Invoice records | 🔒 | `BillingInvoice` model ready |

---

## Security

| Feature | Status | Notes |
|---------|--------|-------|
| Flask-Login sessions | ✅ | Phase 10 |
| WABA credential encryption | ✅ | Fernet, Phase 13-B4B2 |
| Production secret validation | ✅ | Phase 14B |
| Auth-debug endpoint secured | ✅ | `@login_required`, Phase 14B |
| Open redirect prevention | ✅ | `next` URL validation, 14B.3 |
| Tenant isolation | ✅ | `tenant_query()` everywhere |

---

*Oxford CRM Documentation — docs/01_project/FEATURE_MATRIX.md*
*Cross-references: `PROJECT_STATUS.md` · `PROJECT_SCOPE.md` · `02_architecture/SYSTEM_ARCHITECTURE.md`*
