# Oxford CRM — Enterprise Bootstrap: Read This First
## The Absolute First Document Every AI Must Read

> **Version:** 1.0 | **Phase:** 15B | **Audience:** All AI Assistants (Current and Future)
> **Reading Time:** 2 minutes | **Expected Knowledge:** Complete project baseline context
> **Last Updated:** 2026-07-03 | **Update Trigger:** Major release boundary or phase change
> **Dependencies:** None
> **Related Documents:** `02_MASTER_CONTINUATION_PROMPT.md`, `03_AI_BOOTSTRAP_GUIDE.md`

---

## 1. Purpose

This document is the **absolute first file** you must read when joining the Oxford CRM project as an AI assistant. It provides the immutable baseline context required to understand your environment. 

You are entering a live, heavily structured, multi-phase Enterprise SaaS project. **Do not hallucinate architecture, do not simplify the design, and do not write code until you have completed the bootstrap process.**

---

## 2. Project Identity

| Property | Value |
|----------|-------|
| **Project Name** | Oxford CRM |
| **Client** | Oxford Computers (Rutronix Authorised Training Centre) |
| **Location** | Kerala, India |
| **Primary Deployment** | Railway (Cloud) |
| **Database** | PostgreSQL |
| **Core Framework** | Python / Flask / SQLAlchemy |
| **Integrations** | WhatsApp Cloud API, Google Gemini, Razorpay |

---

## 3. Current Release & Phase

| Metric | Status |
|--------|--------|
| **Current Release** | Kerala Production Candidate v1.0 |
| **Current Phase** | 15B (Enterprise Documentation Governance) |
| **Target Scale** | Single Tenant (Oxford Computers) — *Ready for Multi-Tenant Expansion* |
| **Payment Gateway** | Razorpay (India-only). Stripe is deferred. |

---

## 4. Repository Philosophy & Golden Rules

1. **Tenant Isolation is Sacred:** Every data access MUST be scoped to a `tenant_id`.
2. **Production is Live:** The database exists. Never drop tables, rename columns destructively, or recreate the database.
3. **No Assumptions:** If you are unsure of the architecture, read `docs/02_architecture/` and `docs/03_database/`.
4. **Surgical Interventions Only:** Modify the minimum number of files to achieve the goal.
5. **Zero Regression:** Existing working features (Auth, WABA routing, AI chat, Billing foundation) must NEVER break.

---

## 5. Mandatory Reading Order

To fully bootstrap your context, read these files in exact order:

1. `docs/18_bootstrap/01_READ_THIS_FIRST.md` (You are here)
2. `docs/18_bootstrap/02_MASTER_CONTINUATION_PROMPT.md`
3. `docs/18_bootstrap/03_AI_BOOTSTRAP_GUIDE.md`
4. `docs/17_ai_context/AI_MEMORY.md`
5. `docs/01_project/PROJECT_BIBLE.md`

Once you have read these, proceed to `03_AI_BOOTSTRAP_GUIDE.md` for your specific onboarding instructions.
