# Oxford CRM — Project Bible
## Master Project Constitution

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Project Leadership
> **Last Updated:** 2026-07-02 | **Next Review:** After Phase 16.0
> **Authority:** HIGHEST — this document overrides all other documents on matters of project identity

---

## Table of Contents

1. [Project Identity](#identity)
2. [Business Context](#business)
3. [Project Vision](#vision)
4. [Current Release Target](#release)
5. [Architecture Philosophy](#architecture)
6. [Development Governance](#governance)
7. [The Never-Do Laws](#never-do)
8. [The 8-Step Workflow](#workflow)
9. [Current Status Summary](#status)
10. [Future Roadmap Summary](#roadmap)
11. [AI Assistant Constitution](#ai-constitution)

---

## 1. Project Identity {#identity}

| Field | Value |
|-------|-------|
| **Project Name** | Oxford CRM |
| **Business** | Oxford Computers |
| **Client Type** | Rutronix Authorised Training Centre |
| **Location** | Kerala, India |
| **Primary Deployment** | Railway (cloud hosting) |
| **Database** | PostgreSQL |
| **Backend Framework** | Python Flask |
| **Current Release** | Kerala Production Candidate v1.0 |
| **Architecture Pattern** | Enterprise Multi-Tenant SaaS |
| **Documentation Version** | 15.1 |
| **Current Phase** | 15B |

---

## 2. Business Context {#business}

Oxford Computers is a technology training centre in Kerala. They run admissions, manage student leads, and communicate with prospective students via WhatsApp. Oxford CRM was built to automate and scale their admissions pipeline.

**Business Problems Solved:**
- Manual WhatsApp conversations → Automated AI-driven lead qualification
- Spreadsheet-based lead tracking → Real-time CRM pipeline
- No marketing automation → Broadcast campaigns to segmented lead lists
- No analytics → Revenue and funnel analytics dashboards
- No staff management → Staff allocation, assignment, and performance tracking

---

## 3. Project Vision {#vision}

Oxford CRM is **not a simple CRM**. The long-term vision is:

```
Phase 1 (Now):  Oxford Computers' internal CRM
Phase 2:        Kerala SaaS — multiple institutions on one platform
Phase 3:        India SaaS  — national expansion
Phase 4:        Global SaaS — international education institutions
```

The platform is designed to scale from 1 tenant to 1,000+ tenants without redesign. The multi-tenant architecture, tenant isolation, and billing foundation are already in place for this expansion.

**Current Reality:** We are in Phase 1. Single production tenant. India-only. Razorpay-only.

---

## 4. Current Release Target {#release}

| Constraint | Value |
|-----------|-------|
| **Release Name** | Kerala Production Candidate v1.0 |
| **Scope** | Single tenant — Oxford Computers |
| **Country** | India only |
| **Payment Gateway** | Razorpay only |
| **Stripe** | Explicitly deferred — DO NOT IMPLEMENT |
| **International** | No |
| **Multi-Tenancy** | Architecture ready, single tenant in use |
| **Public Registration** | Not live — manual tenant onboarding |

---

## 5. Architecture Philosophy {#architecture}

**Key Principles:**

1. **Tenant isolation first.** Every data access is scoped to `tenant_id`. No exceptions.
2. **Surgical changes only.** Modify the minimum number of files to achieve any goal.
3. **Backward compatibility always.** New code never breaks existing APIs or routes.
4. **Production safety above all.** The existing production database is sacred.
5. **Multi-tenant by default.** All new features must support multiple tenants, even if only one is active.
6. **Provider-agnostic billing.** Billing layer supports any provider. Currently Razorpay. Stripe is deferred.

---

## 6. Development Governance {#governance}

Before touching **any** code, every contributor (human or AI) MUST:

1. Read the affected source files in full
2. Perform an architecture audit
3. Assess risk and regression impact
4. Create a written implementation plan
5. Identify a rollback procedure
6. Obtain explicit user approval
7. Implement surgically (minimum file changes)
8. Run a regression audit
9. Update documentation

**NO EXCEPTIONS. NO SHORTCUTS.**

---

## 7. The Never-Do Laws {#never-do}

These rules are **absolute**. They cannot be overridden by any other instruction:

| # | Law |
|---|-----|
| 1 | Never recreate the database |
| 2 | Never drop tables |
| 3 | Never rename production columns |
| 4 | Never rewrite working authentication |
| 5 | Never bypass tenant isolation |
| 6 | Never create duplicate routes |
| 7 | Never implement Stripe without explicit approval |
| 8 | Never modify unrelated modules |
| 9 | Never code without user approval |
| 10 | Never modify working WhatsApp webhook routes |
| 11 | Never mass-format or refactor outside scope |
| 12 | Never invent architecture |
| 13 | Never assume — always verify against source code |
| 14 | Never skip documentation updates after implementation |
| 15 | Never treat this as a new project |

---

## 8. The 8-Step Workflow {#workflow}

```
Step 1: Read affected files (verify, don't assume)
Step 2: Architecture audit (what is the current state?)
Step 3: Risk analysis (what could break?)
Step 4: Implementation plan (exact files, exact changes)
Step 5: Await user approval ← MANDATORY GATE
Step 6: Implement (surgical, minimum changes)
Step 7: Regression audit (verify nothing broke)
Step 8: Update documentation
```

---

## 9. Current Status Summary {#status}

| System | Status |
|--------|--------|
| CRM Dashboard | ✅ Complete |
| Authentication | ✅ Complete + Patched |
| Lead Management | ✅ Complete |
| Staff Management | ✅ Complete |
| WhatsApp Integration | ✅ Complete |
| AI (Gemini) | ✅ Complete |
| Marketing Hub | ✅ Complete |
| Analytics | ✅ Complete |
| Tenant Portal | ✅ Complete |
| Billing Foundation | ✅ Complete (no live subscriptions) |
| Tenant Isolation | ✅ Complete |
| Super Admin | ⚠️ Partial (view/approve/suspend — no delete/archive) |
| Public Registration | ⚠️ Partial (form exists — no automated approval) |
| Subscription Automation | ❌ Not started |
| LMS | ❌ Planned (Phase 17) |

---

## 10. Future Roadmap Summary {#roadmap}

| Phase | Name | Status |
|-------|------|--------|
| Phase 15B | Documentation + Super Admin Platform | In Progress |
| Phase 15C | Tenant Registration | Planned |
| Phase 16 | Subscription Engine | Planned |
| Phase 17 | LMS | Planned |
| Phase 18 | Student Portal | Planned |
| Phase 19 | Mobile App | Planned |
| Phase 20 | Enterprise AI | Planned |

See `01_project/ROADMAP.md` for full details.

---

## 11. AI Assistant Constitution {#ai-constitution}

If you are an AI assistant reading this document:

**You are NOT starting a new project.**

This is a living, production enterprise system with real users, real data, and real business consequences. Every change you make has risk.

Your role is to:
- Understand before acting
- Audit before implementing
- Ask before assuming
- Document after completing

For detailed AI governance, read: `10_engineering/AI_DEVELOPMENT_CONSTITUTION.md`
For current project state, read: `17_ai_context/AI_MEMORY.md`
For pre-coding checklist, read: `00_meta/NEW_CHAT_BOOTSTRAP.md`

---

*Oxford CRM Documentation — docs/01_project/PROJECT_BIBLE.md*
*This document is the highest authority for the Oxford CRM project.*
*Cross-references: `17_ai_context/AI_MEMORY.md` · `14_history/PHASE_LEDGER.md` · `10_engineering/AI_DEVELOPMENT_CONSTITUTION.md`*
