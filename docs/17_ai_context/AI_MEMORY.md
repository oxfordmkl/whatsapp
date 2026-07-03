# Oxford CRM — AI Memory
## Primary AI Bootstrap Document

> **Version:** 15.1 | **Phase:** 15B | **Audience:** AI Assistants (All)
> **Last Updated:** 2026-07-02 | **Next Review:** After every phase completion
> **CRITICAL:** This document is the single most important file for any AI joining this project.

---

## ⚡ READ THIS FIRST — 60-SECOND SUMMARY

Oxford CRM is an **existing, live enterprise SaaS application** built for Oxford Computers in Kerala, India. It is deployed on Railway with a live PostgreSQL database containing real customer data. The application uses Flask, SQLAlchemy, WhatsApp Cloud API, and Google Gemini.

**You are NOT starting a new project. Do NOT rewrite, redesign, or refactor anything without explicit approval.**

---

## Table of Contents

1. [Project Identity](#1-project-identity)
2. [Architecture Summary](#2-architecture-summary)
3. [Current Production Status](#3-current-production-status)
4. [Completed Modules](#4-completed-modules)
5. [Pending Modules](#5-pending-modules)
6. [Critical Design Decisions](#6-critical-design-decisions)
7. [Deployment Target](#7-deployment-target)
8. [Authentication Model](#8-authentication-model)
9. [Tenant Architecture](#9-tenant-architecture)
10. [Billing Architecture](#10-billing-architecture)
11. [Current Roadmap](#11-current-roadmap)
12. [Known Risks](#12-known-risks)
13. [The Never-Do Rules](#13-the-never-do-rules)
14. [Critical Files Reference](#14-critical-files-reference)
15. [Current Priorities](#15-current-priorities)
16. [Where To Find Everything](#16-where-to-find-everything)

---

## 1. Project Identity

```
Project:     Oxford CRM
Client:      Oxford Computers
Business:    Rutronix Authorised Training Centre
Location:    Kerala, India
Framework:   Python Flask
Database:    PostgreSQL (live, Railway-hosted)
ORM:         SQLAlchemy
Auth:        Flask-Login
AI:          Google Gemini
Messaging:   WhatsApp Cloud API (Meta)
Billing:     Razorpay (India only)
Hosting:     Railway
Release:     Kerala Production Candidate v1.0
Phase:       15B
Version:     15.1
```

---

## 2. Architecture Summary

```
SUPER_ADMIN (platform god-mode)
    └── Platform (Oxford CRM SaaS)
            └── Tenants (institutions)
                    └── ADMIN (Tenant Admin)
                            └── STAFF (CRM users)
```

**Pattern:** Multi-Tenant SaaS with per-tenant data isolation.

**Every tenant owns:**
- Users (Admin + Staff)
- Leads (ConversationState)
- WhatsApp credentials (WABA)
- AI persona and custom prompt
- Billing subscription
- Analytics data

**Request lifecycle:**
```
Browser → Flask (Gunicorn) → Blueprint → Route Handler
       → tenant_query(Model, tenant_id) → PostgreSQL
       → Response
```

**Blueprint registry:**
| Blueprint | Prefix | File |
|-----------|--------|------|
| `admin_bp` | (root) | `app/routes/admin.py` |
| `tenant_bp` | `/tenant` | `app/routes/tenant.py` |
| `webhook_bp` | (root) | `app/routes/webhook.py` |
| `broadcast_bp` | (root) | `app/routes/broadcast.py` |
| `billing_bp` | `/webhooks` | `app/routes/billing.py` |
| `health_bp` | (root) | `app/routes/health.py` |
| `public_bp` | (root) | `app/routes/public.py` |

---

## 3. Current Production Status

| Dimension | Status |
|-----------|--------|
| Overall | ✅ CONDITIONAL GO — Kerala RC |
| Architecture Score | 90/100 (Phase 15A audit) |
| Isolation Score | 95/100 (Phase 15A audit) |
| Security Score | 95/100 (Phase 14B patches) |
| Deployment | ✅ Live on Railway |
| Database | ✅ Stable, no pending migrations |
| WhatsApp | ✅ Live traffic flowing |
| AI Engine | ✅ Active |
| Critical Blockers | None |

---

## 4. Completed Modules

| Module | Completed In | Description |
|--------|-------------|-------------|
| CRM Dashboard | Phase 9 | Home, leads, analytics |
| Authentication | Phase 10 | Flask-Login, 3 roles |
| Lead Management | Phase 10 | Full pipeline |
| Staff Management | Phase 10 | Allocation, performance |
| KPI Analytics | Phase 10M | Revenue, funnel |
| Intelligence Engine | Phase 10N | AI-driven insights |
| Communication Hub | Phase 11 | Marketing, follow-ups |
| WhatsApp Automation | Phase 12 | Campaigns, opt-out |
| Multi-Tenant Foundation | Phase 12 | tenant_id isolation |
| SaaS Identity Schema | Phase 13-A2B | Slug, status, plan fields |
| Tenant Registration Flow | Phase 13-B2B | Public + approval |
| Tenant Portal | Phase 13-B3B | Admin self-service |
| WABA Encryption | Phase 13-B4B2 | Fernet credential storage |
| Webhook Tenant Routing | Phase 13-B4D2 | Dynamic WABA routing |
| Billing Foundation | Phase 13-B4.1C | Razorpay, provider-agnostic |
| Production Audit | Phase 14A | Full audit, findings |
| Security Hardening | Phase 14B | Secret validation, route fix |
| Auth Stabilization | Phase 14B.3 | Deep-link, sidebar fix |
| Architecture Audit | Phase 15A | Scored 90/100 |
| Enterprise Docs | Phase 15B | This document system |

---

## 5. Pending Modules

| Module | Target Phase | Priority |
|--------|-------------|---------|
| Super Admin — Delete/Archive Tenant | 15C | HIGH |
| Super Admin — Create Tenant UI | 15C | HIGH |
| Subscription Engine (Razorpay live) | 16 | HIGH |
| LMS | 17 | MEDIUM |
| Student Portal | 18 | MEDIUM |
| Mobile App | 19 | LOW |
| Enterprise AI expansion | 20 | LOW |
| Stripe integration | Post-20 | 🚫 DEFERRED |
| READ_ONLY role | 15C | LOW |

---

## 6. Critical Design Decisions

These decisions were made deliberately and must NOT be reversed without explicit approval:

| # | Decision | Phase | Reason |
|---|---------|-------|--------|
| 1 | Razorpay only (no Stripe) | 13-B4.1 | India-first strategy |
| 2 | `tenant_query()` pattern for all DB access | 12 | Isolation enforcement |
| 3 | Two separate login gateways (ADMIN vs SUPER) | 10 | Security isolation |
| 4 | WABA credentials encrypted with Fernet | 13-B4B2 | Security compliance |
| 5 | Dynamic WABA routing via `phone_number_id` | 13-B4D2 | Multi-tenant WhatsApp |
| 6 | `billing_exempt` flag for Oxford | 13-B4.1C | Grandfathering protection |
| 7 | `next` URL validated as local path only | 14B.3 | Open redirect prevention |
| 8 | No hard delete of production records | All | Data integrity |

---

## 7. Deployment Target

```
Platform:  Railway
Database:  PostgreSQL (Railway managed)
Country:   India only
Tenant:    Oxford Computers (single)
Payment:   Razorpay only
Stripe:    DEFERRED — do not implement
Region:    Single region
Scale:     1 tenant now, 1000+ in future
```

---

## 8. Authentication Model

**Two Login Gateways:**
- `GET/POST /crm/login` — for ADMIN and STAFF users
- `GET/POST /crm/super/login` — for SUPER_ADMIN only

**Session:** Flask-Login (`login_user()`, `logout_user()`, `current_user`)

**Roles:**
- `SUPER_ADMIN` — platform-wide access, `tenant_id = None`
- `ADMIN` — tenant admin, full tenant access
- `STAFF` — limited CRM access (leads, follow-ups)
- `READ_ONLY` — planned, not implemented

**Key Decorators:**
- `@login_required` — basic auth gate
- `@super_admin_required` — SUPER_ADMIN only
- `@tenant_admin_required` — ADMIN only

**Tenant Status Check:**
Non-SUPER_ADMIN users cannot log in if `tenant.status != 'ACTIVE'`.

**Deep Links:**
`?next=/path` parameter is honored post-login (patched Phase 14B.3).
Security: only local paths (starting with `/`) are accepted.

---

## 9. Tenant Architecture

**Tenant Model Fields (key):**
```
id                          UUID primary key
name                        Display name
slug                        URL-safe identifier (immutable)
status                      PENDING|TRIAL|ACTIVE|PAST_DUE|SUSPENDED|CANCELLED|DELETED
plan                        STARTER|GROWTH|PROFESSIONAL|ENTERPRISE
billing_exempt              Boolean (True = bypass billing, Oxford uses this)
waba_phone_number_id        WhatsApp Business phone ID
waba_access_token_encrypted Fernet-encrypted Meta access token
ai_persona_name             Per-tenant AI bot name
ai_prompt_override          Per-tenant custom system prompt
billing_provider            'razorpay' | 'stripe'
```

**Isolation Pattern:**
```python
# ALWAYS scope queries like this:
tenant_query(ConversationState, tenant_id).filter_by(phone=phone).first()

# NEVER query without tenant scope (except SUPER_ADMIN views):
ConversationState.query.all()  # ← FORBIDDEN in non-super-admin context
```

**Webhook Routing:**
```python
# From app/routes/webhook.py:
tenant = Tenant.query.filter_by(waba_phone_number_id=phone_number_id).first()
# Routes message to correct tenant by their registered WABA phone number
```

---

## 10. Billing Architecture

**Model:** Provider-agnostic (supports any payment provider)

**Current Provider:** Razorpay (India)

**Stripe Status:** Endpoints registered but explicitly deferred

**Oxford Protection:**
```python
# BOTH conditions must be True to bypass billing:
tenant.billing_exempt == True  AND  tenant.slug == 'oxford'
```

**Billing Status Values:**
`ACTIVE` | `TRIAL` | `PAST_DUE` | `SUSPENDED` | `CANCELLED`

**Invoice Ledger:** `BillingInvoice` model — append-only, immutable

---

## 11. Current Roadmap

| Phase | Name | Status |
|-------|------|--------|
| 15B | Documentation | IN PROGRESS |
| 15C | Super Admin Platform | PLANNED |
| 16 | Subscription Engine | PLANNED |
| 17 | LMS | PLANNED |
| 18 | Student Portal | PLANNED |
| 19 | Mobile App | PLANNED |
| 20 | Enterprise AI | PLANNED |

---

## 12. Known Risks

| Risk | Severity | Mitigation |
|------|---------|-----------|
| `admin.py` is 4,800+ lines | MEDIUM | Surgical edits only, never mass-edit |
| No DELETE/ARCHIVE tenant UI | MEDIUM | Manual DB until Phase 15C |
| No READ_ONLY role | LOW | Deferred to Phase 15C |
| `_get_default_tenant_id()` fallback in background threads | LOW | Kerala-safe, single tenant |
| Missing `WABA_ENCRYPTION_KEY` breaks dev boot | LOW | Set env var or use local dev mode |

---

## 13. The Never-Do Rules

```
1.  Never recreate the database
2.  Never drop tables
3.  Never rename production columns
4.  Never rewrite working authentication
5.  Never bypass tenant isolation
6.  Never create duplicate routes
7.  Never implement Stripe without approval
8.  Never modify unrelated modules
9.  Never code without user approval
10. Never touch working WhatsApp webhook routes
11. Never mass-format or refactor outside scope
12. Never invent architecture
13. Never assume — read the source code
14. Never skip documentation updates after implementation
15. Never treat this as a new project
```

---

## 14. Critical Files Reference

| File | Purpose |
|------|---------|
| `app/__init__.py` | App factory, all blueprint registrations |
| `app/models.py` | All database models (297 lines, read entirely) |
| `app/config.py` | All environment variables and validation |
| `app/routes/admin.py` | Main CRM (~4,800 lines — caution) |
| `app/routes/tenant.py` | Tenant portal |
| `app/routes/webhook.py` | WhatsApp inbound |
| `app/bot/router.py` | AI conversation engine |
| `app/services/followup_service.py` | Follow-up automation |
| `app/services/whatsapp_service.py` | WhatsApp API send layer |
| `templates/crm_sidebar.html` | Main navigation sidebar |

---

## 15. Current Priorities

1. ✅ Complete `docs/17_ai_context/` folder (this document)
2. ✅ Complete all docs folders (Phase 15B ongoing)
3. 🔲 Phase 15C — Super Admin complete lifecycle
4. 🔲 Phase 16 — Subscription Engine

---

## 16. Where To Find Everything

| What | Document |
|------|---------|
| Project constitution | `01_project/PROJECT_BIBLE.md` |
| Phase history | `14_history/PHASE_LEDGER.md` |
| Current tasks | `17_ai_context/ACTIVE_TASKS.md` |
| Completed history | `17_ai_context/COMPLETED_TASKS.md` |
| Known risks | `17_ai_context/KNOWN_RISKS.md` |
| Next phase spec | `17_ai_context/NEXT_PHASE.md` |
| Machine state | `17_ai_context/PROJECT_STATE.json` |
| Database models | `03_database/DATABASE_BIBLE.md` |
| Auth deep dive | `02_architecture/AUTH_ARCHITECTURE.md` |
| Tenant isolation | `07_security/TENANT_ISOLATION.md` |
| Deployment | `08_deployment/RAILWAY_DEPLOYMENT.md` |
| Coding rules | `10_engineering/AI_DEVELOPMENT_CONSTITUTION.md` |

---

*Oxford CRM Documentation — docs/17_ai_context/AI_MEMORY.md*
*This is the primary bootstrap document for all AI assistants.*
*It must be kept current. Update after every completed phase.*
