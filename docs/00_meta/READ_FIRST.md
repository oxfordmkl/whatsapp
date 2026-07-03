# Oxford CRM — Read First
## The 5-Minute Project Introduction

> **Version:** 15.1 | **Phase:** 15B | **Audience:** Everyone
> **Last Updated:** 2026-07-02 | **Reading Time:** 5 minutes

---

## What Is Oxford CRM?

**Oxford CRM** is an Enterprise Business Platform built for **Oxford Computers**, a Rutronix Authorised Training Centre based in **Kerala, India**.

It is NOT a simple contact manager. It is a multi-tenant SaaS platform that manages:

- WhatsApp-driven **lead conversations** via Gemini AI
- Full **CRM pipeline** (lead intake → admission)
- **Marketing automation** and broadcast campaigns
- **Staff management** and workload distribution
- **Analytics** and revenue intelligence
- **Multi-tenant architecture** ready for SaaS expansion

---

## Current Release Target

| Item | Value |
|------|-------|
| **Release** | Kerala Production Candidate v1.0 |
| **Scope** | Single tenant — Oxford Computers only |
| **Country** | India |
| **Payment** | Razorpay only |
| **Stripe** | Explicitly deferred (future SaaS) |
| **Deployment** | Railway (cloud hosting) |

---

## What It Is NOT (Right Now)

- NOT a global SaaS platform (yet)
- NOT a multi-region deployment
- NOT using Stripe
- NOT open for public registration
- NOT a simple CRM

---

## The Three Things You Must Know Before Touching Code

1. **Tenant isolation is sacred.** Every database query must be scoped to a `tenant_id`. Never query data without a tenant scope unless you are writing SUPER_ADMIN code.

2. **The production database already exists.** Never recreate it, drop tables, or rename columns. Oxford Computers' live leads and WhatsApp data are in it.

3. **Never code without user approval.** Every implementation follows an 8-step workflow: audit → plan → approval → implement → verify → document.

---

## Where To Go Next

| Goal | Start With |
|------|-----------|
| Understand the full project | `01_project/PROJECT_BIBLE.md` |
| Onboard as an AI assistant | `00_meta/NEW_CHAT_BOOTSTRAP.md` |
| Understand current status | `17_ai_context/AI_MEMORY.md` |
| Understand the database | `03_database/DATABASE_BIBLE.md` |
| Understand authentication | `02_architecture/AUTH_ARCHITECTURE.md` |
| Understand deployment | `08_deployment/RAILWAY_DEPLOYMENT.md` |

---

## Project Health

| Dimension | Status |
|-----------|--------|
| Architecture | ✅ Stable (Phase 15A Score: 90/100) |
| Authentication | ✅ Complete + Patched (Phase 14B.3) |
| Tenant Isolation | ✅ Complete (Score: 95/100) |
| WhatsApp | ✅ Complete |
| AI (Gemini) | ✅ Complete |
| Billing Foundation | ✅ Complete (no live subscriptions yet) |
| Production Deployment | ✅ Live on Railway |

---

*Oxford CRM Documentation — docs/00_meta/READ_FIRST.md*
*Cross-references: `NEW_CHAT_BOOTSTRAP.md` · `17_ai_context/AI_MEMORY.md` · `01_project/PROJECT_BIBLE.md`*
