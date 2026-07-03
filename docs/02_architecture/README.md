# docs/02_architecture — Architecture Documentation Layer

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Architecture Team
> **Last Updated:** 2026-07-02

---

## Purpose

The `02_architecture` folder contains the **authoritative technical architecture documentation** for Oxford CRM. Every document in this folder is derived from verified source code — not from assumptions.

---

## Contents

| File | Purpose | Importance |
|------|---------|-----------|
| `README.md` | This file — folder orientation | MEDIUM |
| `SYSTEM_ARCHITECTURE.md` | Full system overview and component design | CRITICAL |
| `TENANT_ARCHITECTURE.md` | Multi-tenant design and isolation guarantees | CRITICAL |
| `AUTHENTICATION_ARCHITECTURE.md` | Login flows, roles, session lifecycle | CRITICAL |
| `BILLING_ARCHITECTURE.md` | Billing foundation and Razorpay strategy | HIGH |
| `WHATSAPP_ARCHITECTURE.md` | WhatsApp Cloud API integration | CRITICAL |
| `AI_ARCHITECTURE.md` | Gemini AI integration and conversation engine | HIGH |

---

## Reading Order

1. `SYSTEM_ARCHITECTURE.md` — understand the whole before the parts
2. `TENANT_ARCHITECTURE.md` — isolation is fundamental
3. `AUTHENTICATION_ARCHITECTURE.md` — who can do what
4. `WHATSAPP_ARCHITECTURE.md` — primary input channel
5. `AI_ARCHITECTURE.md` — primary intelligence layer
6. `BILLING_ARCHITECTURE.md` — commercial foundation

---

## Source Authority

Every claim in these documents is verified against:
- `app/__init__.py`
- `app/models.py`
- `app/config.py`
- `app/routes/admin.py`
- `app/routes/tenant.py`
- `app/routes/webhook.py`
- `app/routes/billing.py`
- `app/bot/router.py`
- `app/services/ai_service.py`
- `app/services/followup_service.py`

---

*Cross-references: `00_meta/READING_ORDER.md` · `17_ai_context/AI_MEMORY.md` · `03_database/DATABASE_BIBLE.md`*
