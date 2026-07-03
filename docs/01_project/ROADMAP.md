# Oxford CRM — Product Roadmap
## Future Phases and Timeline

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Project Leadership
> **Last Updated:** 2026-07-02 | **Next Review:** After Phase 15C

---

## Roadmap Philosophy

The Oxford CRM roadmap follows a **Kerala-first, India-second, Global-third** strategy.

Every phase must be backward compatible with the previous phase. No phase should require database recreation, architectural redesign, or breaking changes to the existing tenant (Oxford Computers).

---

## Current Phase

**Phase 15B — Documentation & Enterprise Memory Layer**
Status: IN PROGRESS

---

## Upcoming Phases

### Phase 15C — Super Admin Platform
**Goal:** Complete the Super Admin system with delete, archive, and tenant creation capabilities.

| Deliverable | Description |
|------------|-------------|
| Delete Tenant | Soft-delete a tenant (status = DELETED) |
| Archive Tenant | Archive a tenant (status = ARCHIVED) |
| Create Tenant | Super Admin creates a new tenant without public registration |
| Enhanced Dashboard | Improved super admin UI with full lifecycle controls |

**Dependencies:** Phase 15B (documentation) must be complete.
**Risk:** Medium — involves `admin.py` which is 4,800+ lines.

---

### Phase 16 — Subscription Engine
**Goal:** Activate live Razorpay subscription processing for SaaS tenants.

| Deliverable | Description |
|------------|-------------|
| Razorpay subscription creation | Auto-create subscriptions on tenant approval |
| Webhook processing | Handle payment success, failure, renewal |
| Trial-to-paid conversion | Automatic conversion on payment |
| Subscription dashboard | Tenant billing self-service |
| Invoice generation | `BillingInvoice` records for every payment |

**Dependencies:** Phase 15C (Super Admin with tenant creation).
**Note:** Stripe remains explicitly deferred. Razorpay only.

---

### Phase 17 — LMS (Learning Management System)
**Goal:** Add a learning management module for Oxford Computers' course delivery.

| Deliverable | Description |
|------------|-------------|
| Course catalogue | Per-tenant course management |
| Batch management | Schedule and track batches |
| Student-course linking | Connect admitted leads to courses |
| Basic attendance | Track student attendance |

**Dependencies:** Phase 16 (subscription active for LMS licensing).

---

### Phase 18 — Student Portal
**Goal:** A self-service portal for admitted students.

| Deliverable | Description |
|------------|-------------|
| Student login | Separate auth for students (not staff/admin) |
| Course progress | View enrolled courses and progress |
| Fee receipts | View and download invoices |
| WhatsApp notifications | Automated student updates |

**Dependencies:** Phase 17 (LMS must exist).

---

### Phase 19 — Mobile App
**Goal:** React Native or Flutter mobile app for staff CRM access.

| Deliverable | Description |
|------------|-------------|
| Staff mobile login | Mobile-native auth |
| Lead management | View and update leads on mobile |
| WhatsApp quick-reply | Send manual replies from mobile |
| Push notifications | Alert on new leads |

**Dependencies:** Phase 17 + REST API expansion.

---

### Phase 20 — Enterprise AI
**Goal:** Expand the AI layer beyond WhatsApp conversations.

| Deliverable | Description |
|------------|-------------|
| AI lead scoring | Predictive lead priority scoring |
| AI follow-up suggestions | Recommended next actions |
| AI reporting | Natural language analytics queries |
| AI persona marketplace | Tenants choose from pre-built personas |

**Dependencies:** Phase 16 (billing needed for AI feature tiers).

---

## Global SaaS Timeline (Estimated)

| Milestone | Estimated Phase | Notes |
|-----------|---------------|-------|
| Kerala launch (1 tenant) | Phase 15B/15C | Oxford Computers |
| Kerala SaaS (5–10 tenants) | Phase 16 | Subscription active |
| India SaaS (50+ tenants) | Phase 16–17 | Multi-region marketing |
| Global SaaS (100+ tenants) | Phase 18–20 | Stripe enabled |

---

*Oxford CRM Documentation — docs/01_project/ROADMAP.md*
*Cross-references: `PROJECT_BIBLE.md` · `PROJECT_SCOPE.md` · `17_ai_context/NEXT_PHASE.md`*
