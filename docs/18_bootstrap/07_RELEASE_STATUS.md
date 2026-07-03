# Oxford CRM — Release Status
## Module Stability and Target State

> **Version:** 1.0 | **Phase:** 15B | **Audience:** All Developers & AI
> **Reading Time:** 2 minutes | **Expected Knowledge:** What works and what doesn't
> **Last Updated:** 2026-07-03 | **Update Trigger:** Phase completion
> **Dependencies:** None
> **Related Documents:** `17_ai_context/PROJECT_STATE.md`

---

## 1. Purpose

This document provides a high-level overview of which modules in the system are stable, which are experimental, and which are completely deferred. Do not attempt to fix a "bug" in a deferred module.

---

## 2. Production Status Overview

**Current Release:** Kerala Production Candidate v1.0
**Status:** CONDITIONAL GO (Awaiting Super Admin Layer)

### Stable & Frozen Modules
These modules are complete and tested. Modify them only with extreme caution and a full regression plan.
- **Database Schema:** Foundation is locked. 11 migrations applied.
- **Tenant Isolation:** `tenant_query()` and foreign keys are active.
- **Authentication:** Flask-Login gateways (CRM and Tenant Portal).
- **AI Engine:** Gemini 2.0 Flash integration and state machine.

### Active Development Modules
These modules are currently being built or documented.
- **Documentation:** Phase 15B (Current).
- **Super Admin Platform:** Phase 15C (Next).

### Deferred Modules
Do NOT implement these. They are roadmapped for future phases.
- **Stripe Billing** (Phase 16+)
- **Live Razorpay Webhooks** (Phase 16)
- **LMS Integration** (Phase 17)
- **Student Portal** (Phase 18)
- **Mobile App** (Phase 19)
- **Global SaaS Expansion** (Phase 20)

---

## 3. The Next Approved Phase

**Phase 15C: Super Admin Platform**
The immediate next goal is to build the God-mode dashboard that allows Oxford platform owners to create, suspend, and manage Tenants without manual database intervention.
