# Oxford CRM — Platform Modules
## All Modules and Their Responsibilities

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Architecture Team
> **Last Updated:** 2026-07-02

---

## Active Modules

| Module | Blueprint | URL Prefix | Source File | Status |
|--------|-----------|-----------|-------------|--------|
| CRM Admin | `admin_bp` | (root) | `app/routes/admin.py` | ✅ Active |
| Tenant Portal | `tenant_bp` | `/tenant` | `app/routes/tenant.py` | ✅ Active |
| WhatsApp Webhook | `webhook_bp` | (root) | `app/routes/webhook.py` | ✅ Active |
| Broadcast / Marketing | `broadcast_bp` | (root) | `app/routes/broadcast.py` | ✅ Active |
| Health Check | `health_bp` | (root) | `app/routes/health.py` | ✅ Active |
| Public Registration | `public_bp` | (root) | `app/routes/public.py` | ⚠️ Partial |
| Billing Webhooks | `billing_bp` | `/webhooks` | `app/routes/billing.py` | ✅ Registered |

---

## Module Descriptions

### CRM Admin (`admin_bp`)
The primary CRM interface. Handles:
- All `/crm/*` routes
- Authentication (`/crm/login`, `/crm/logout`, `/crm/super/login`)
- Leads, staff, analytics, campaigns
- Super Admin routes (`/crm/super/*`)
- Impersonation
- **Important:** This file is 4,800+ lines. Handle with extreme care.

### Tenant Portal (`tenant_bp`)
The tenant self-service settings interface. Handles:
- All `/tenant/*` routes
- Company profile, staff, AI settings, WhatsApp config, billing view
- Protected by `@tenant_admin_required`

### WhatsApp Webhook (`webhook_bp`)
Receives all incoming WhatsApp messages from Meta. Handles:
- `GET /webhook` — Meta webhook verification
- `POST /webhook` — Inbound message processing, tenant routing, AI response

### Broadcast (`broadcast_bp`)
Marketing and campaign management. Handles:
- Campaign broadcasts to lead lists
- Template message sending

### Billing Webhooks (`billing_bp`)
Receives payment provider webhook notifications. Handles:
- `POST /webhooks/razorpay` — Razorpay payment events
- `POST /webhooks/stripe` — Stripe events (deferred, endpoint exists)

### Public (`public_bp`)
Tenant registration and public-facing pages. Currently partial.

---

## Service Layer

| Service | File | Purpose |
|---------|------|---------|
| WhatsApp sending | `app/services/whatsapp_service.py` | Sends messages via Meta API |
| Log service | `app/services/log_service.py` | Writes `MessageLog`, `ConversationMessage`, `LeadEvent` |
| Follow-up scheduler | `app/services/followup_service.py` | Manages `FollowUpJob` queue |
| CRM service | `app/services/crm_service.py` | Lead sheet export (legacy) |

## AI / Bot Layer

| Component | File | Purpose |
|-----------|------|---------|
| Router | `app/bot/router.py` | Main AI dispatch via `smart_reply()` |
| Prompts | `app/bot/prompts.py` | Default AALIZA_PROMPT |

---

*Oxford CRM Documentation — docs/01_project/MODULES.md*
*Cross-references: `04_backend/BLUEPRINTS.md` · `02_architecture/SYSTEM_ARCHITECTURE.md`*
