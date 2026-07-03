# Oxford CRM — New Chat Bootstrap Guide
## AI Assistant Onboarding Protocol

> **Version:** 15.1 | **Phase:** 15B | **Audience:** AI Assistants
> **Last Updated:** 2026-07-02 | **Reading Time:** 10–15 minutes
> **Applies To:** Gemini, Claude, ChatGPT, Cursor, GitHub Copilot, Antigravity, any future AI

---

## STOP — Read This Before Anything Else

If you are an AI assistant joining this project in a new conversation, this is your onboarding checklist. Do NOT write a single line of code until you have read and confirmed every item below.

---

## Part 1 — Critical Facts (Memorize These)

### 1.1 What This Project Is

```
Project      : Oxford CRM
Business     : Oxford Computers, Kerala, India
Architecture : Enterprise Multi-Tenant SaaS
Framework    : Python Flask
Database     : PostgreSQL (live production data)
Hosting      : Railway
AI Engine    : Google Gemini
Messaging    : WhatsApp Cloud API (Meta)
Billing      : Razorpay (India only, no Stripe)
Release      : Kerala Production Candidate v1.0
Phase        : 15B (as of 2026-07-02)
```

### 1.2 What This Project Is NOT

- NOT a fresh greenfield project
- NOT a simple CRUD app
- NOT open to architectural redesign
- NOT using Stripe (explicitly deferred)
- NOT a global multi-region deployment yet

### 1.3 Current Production State

- The application is **live on Railway**
- Oxford Computers is the **single active tenant**
- Real WhatsApp leads are flowing through the system
- The PostgreSQL database contains **live production data**
- All major systems are **verified working**

---

## Part 2 — The Architecture You Must Know

### 2.1 Multi-Tenant Hierarchy

```
SUPER_ADMIN
    └── Platform (Oxford CRM SaaS)
            └── Tenants
                    └── Tenant Admin (ADMIN role)
                            └── Staff (STAFF role)
```

### 2.2 Tenant Isolation Rule (NEVER Violate)

Every database query **must** be scoped to `tenant_id`.
The pattern is `tenant_query(Model, tenant_id)`.
Cross-tenant data access is a critical security violation.

### 2.3 Authentication System

- Two login gateways: `/crm/login` (ADMIN/STAFF) and `/crm/super/login` (SUPER_ADMIN)
- Built on **Flask-Login** (`LoginManager`, `user_loader`, `login_user`)
- Roles: `SUPER_ADMIN`, `ADMIN`, `STAFF` (READ_ONLY planned but not implemented)
- Deep-link redirect via `?next=` parameter (patched Phase 14B.3)

### 2.4 Blueprint Registry

| Blueprint | URL Prefix | Responsibility |
|-----------|-----------|---------------|
| `admin_bp` | (none) | CRM dashboard, login, leads, analytics |
| `tenant_bp` | `/tenant` | Tenant admin portal |
| `webhook_bp` | (none) | WhatsApp Cloud API webhook |
| `broadcast_bp` | (none) | Marketing campaigns |
| `health_bp` | (none) | System health check |
| `public_bp` | (none) | Registration, public pages |
| `billing_bp` | `/webhooks` | Razorpay/Stripe webhook endpoints |

### 2.5 Key Files

| File | What It Does |
|------|-------------|
| `app/__init__.py` | App factory, blueprint registration, Flask-Login setup |
| `app/models.py` | All SQLAlchemy models (Tenant, User, ConversationState, etc.) |
| `app/config.py` | Environment variable loading and validation |
| `app/routes/admin.py` | Main CRM routes (~4,800 lines — handle with caution) |
| `app/routes/tenant.py` | Tenant portal routes |
| `app/routes/webhook.py` | WhatsApp webhook processing |
| `app/bot/router.py` | AI conversation engine (Gemini) |
| `app/services/followup_service.py` | Follow-up automation scheduler |
| `app/services/whatsapp_service.py` | WhatsApp API send functions |

---

## Part 3 — The 15 Laws You Must Never Break

1. **Never recreate the database** — production data exists
2. **Never drop tables** — destructive and irreversible
3. **Never rename production columns** — breaks live queries
4. **Never rewrite authentication** — it is verified and working
5. **Never bypass tenant isolation** — critical security violation
6. **Never create duplicate routes** — causes silent overrides
7. **Never implement Stripe** — explicitly deferred to global SaaS phase
8. **Never modify unrelated modules** — surgical changes only
9. **Never code without user approval** — governance protocol is mandatory
10. **Never touch working WhatsApp webhook routes** — live traffic flows through them
11. **Never mass-format or refactor** — any change outside scope is forbidden
12. **Never invent architecture** — document what exists, implement what is approved
13. **Never simplify the architecture** — it is complex by design
14. **Never assume** — verify by reading the actual code
15. **Never skip the 8-step workflow** — audit → plan → approval → implement → verify → document

---

## Part 4 — The Mandatory Pre-Coding Checklist

Before you write a single line of code, confirm:

- [ ] I have read `00_meta/READ_FIRST.md`
- [ ] I have read `17_ai_context/AI_MEMORY.md`
- [ ] I have read `17_ai_context/PROJECT_STATE.md`
- [ ] I understand the current phase and status
- [ ] I have read the affected source files
- [ ] I have produced an architecture audit
- [ ] I have produced an implementation plan
- [ ] I have received explicit user approval
- [ ] I know my rollback procedure

---

## Part 5 — The 8-Step Implementation Workflow

```
Step 1: Read affected files
Step 2: Architecture audit
Step 3: Risk analysis
Step 4: Implementation plan (artifact)
Step 5: Await user approval  ← NEVER skip this
Step 6: Implement (surgical, minimal changes)
Step 7: Regression audit
Step 8: Update documentation
```

---

## Part 6 — Where To Find Everything

| What You Need | Where It Is |
|--------------|------------|
| Complete project history | `14_history/PHASE_LEDGER.md` |
| Current status snapshot | `17_ai_context/PROJECT_STATE.md` |
| AI-optimized knowledge base | `17_ai_context/AI_MEMORY.md` |
| Database models reference | `03_database/DATABASE_BIBLE.md` |
| Authentication details | `02_architecture/AUTH_ARCHITECTURE.md` |
| Security rules | `07_security/SECURITY_GUIDE.md` |
| Deployment guide | `08_deployment/RAILWAY_DEPLOYMENT.md` |
| Coding standards | `10_engineering/CODING_STANDARD.md` |
| Role permissions | `07_security/RBAC.md` |
| Next planned phase | `17_ai_context/NEXT_PHASE.md` |

---

## Part 7 — Confirmation Acknowledgement

After reading this document, an AI assistant should be able to confirm:

> "I understand that Oxford CRM is an existing enterprise multi-tenant SaaS platform built on Flask + PostgreSQL, deployed on Railway. The current release is the Kerala Production Candidate targeting Oxford Computers as the single tenant. I will never modify production code without explicit approval, never bypass tenant isolation, and never implement Stripe or global SaaS features at this stage. I will follow the 8-step workflow for every implementation."

---

*Oxford CRM Documentation — docs/00_meta/NEW_CHAT_BOOTSTRAP.md*
*Cross-references: `READ_FIRST.md` · `17_ai_context/AI_MEMORY.md` · `10_engineering/AI_DEVELOPMENT_CONSTITUTION.md` · `01_project/PROJECT_BIBLE.md`*
