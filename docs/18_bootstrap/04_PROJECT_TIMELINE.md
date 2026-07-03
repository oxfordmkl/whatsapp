# Oxford CRM — Project Timeline
## Historical Evolution and Milestones

> **Version:** 1.0 | **Phase:** 15B | **Audience:** Architects, Historians, AI
> **Reading Time:** 4 minutes | **Expected Knowledge:** How we got here
> **Last Updated:** 2026-07-03 | **Update Trigger:** Phase completion
> **Dependencies:** None
> **Related Documents:** `05_PROJECT_EVOLUTION.md`

---

## 1. Purpose

This document tracks the chronological history of Oxford CRM. Understanding why decisions were made is critical to avoiding regressions.

---

## 2. Chronological Milestones

### Pre-Phase 10: The Bot Era
- **What Happened:** Started as a simple, single-tenant WhatsApp Bot (`router.py`).
- **Core Features:** Basic keyword routing, Gemini fallback, and single database tables (`conversation_state`).
- **Status:** Integrated.

### Phase 10: Web CRM Foundation
- **What Happened:** Added Flask-Login and basic web interface for staff.
- **Core Features:** Admin login, `users` table, role-based access (SUPER_ADMIN, STAFF).
- **Status:** Integrated.

### Phase 11: Lead Management & Opt-Outs
- **What Happened:** Expanded into full CRM pipeline.
- **Core Features:** Added `lead_event`, `conversation_message` structured timeline, and WhatsApp STOP/opt-out compliance.
- **Status:** Integrated.

### Phase 12: Tenant Foundation
- **What Happened:** The critical pivot to SaaS.
- **Core Features:** Created `tenants` table. Backfilled all data. Added `tenant_id` foreign keys to every table.
- **Status:** Integrated.

### Phase 13: SaaS Identity & Billing Architecture
- **What Happened:** Identity schema expansion.
- **Core Features:** WABA routing per tenant, Razorpay/Stripe agnostic billing schema (`billing_invoices`).
- **Status:** Integrated.

### Phase 14A & 14B: Production Safety & Audits
- **What Happened:** Massive security and stability push.
- **Core Features:** Deep-link auth fixes, route prefix hardening, WABA deduplication, regression patching.
- **Status:** Integrated.

### Phase 15A & 15B: Enterprise Documentation
- **What Happened:** Documentation generation (Current Phase).
- **Core Features:** Master Bible, Architecture Docs, Database Docs, AI Memory, and this Bootstrap Pack.
- **Status:** Integrated (Active).

---

## 3. Future Timeline

### Phase 15C: Super Admin Platform
- **Goal:** Platform God-mode dashboard.
- **Status:** Next approved phase.

### Phase 16: Billing & Portal Activation
- **Goal:** Turn on live Razorpay webhooks, activate Tenant Portal settings.
- **Status:** Deferred.
