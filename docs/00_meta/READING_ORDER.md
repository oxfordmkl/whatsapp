# Oxford CRM — Documentation Reading Order
## Master Navigation Guide

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Documentation Engineering Division
> **Last Updated:** 2026-07-02 | **Next Review:** After Phase 15B completion

---

## Table of Contents

1. [How To Use This Guide](#how-to-use)
2. [Track 1 — AI Assistant Bootstrap (15 minutes)](#track-1)
3. [Track 2 — Emergency Onboarding (30 minutes)](#track-2)
4. [Track 3 — Full Technical Onboarding (4 hours)](#track-3)
5. [Track 4 — Security Review](#track-4)
6. [Track 5 — New Feature Implementation](#track-5)
7. [Track 6 — Documentation Maintenance](#track-6)

---

## How To Use This Guide {#how-to-use}

Choose the reading track that matches your purpose. Start with the documents marked **REQUIRED** for your track. Documents marked **RECOMMENDED** provide deeper context. Documents marked **REFERENCE** are for lookup only — do not read start-to-finish.

---

## Track 1 — AI Assistant Bootstrap {#track-1}
**Time:** 15 minutes | **Purpose:** Minimum viable context for an AI assistant to proceed safely

| Order | Document | Status | Time |
|-------|---------|--------|------|
| 1 | `17_ai_context/AI_MEMORY.md` | **REQUIRED** | 5 min |
| 2 | `17_ai_context/PROJECT_STATE.md` | **REQUIRED** | 3 min |
| 3 | `00_meta/READ_FIRST.md` | **REQUIRED** | 2 min |
| 4 | `00_meta/NEW_CHAT_BOOTSTRAP.md` | **REQUIRED** | 5 min |
| 5 | `17_ai_context/NEXT_PHASE.md` | RECOMMENDED | 3 min |
| 6 | `10_engineering/AI_DEVELOPMENT_CONSTITUTION.md` | RECOMMENDED | 5 min |

---

## Track 2 — Emergency Onboarding {#track-2}
**Time:** 30 minutes | **Purpose:** Minimum context for a developer to safely make a fix

| Order | Document | Status |
|-------|---------|--------|
| 1 | All of Track 1 | **REQUIRED** |
| 2 | `01_project/PROJECT_BIBLE.md` | **REQUIRED** |
| 3 | `14_history/PHASE_LEDGER.md` | **REQUIRED** |
| 4 | `02_architecture/SYSTEM_ARCHITECTURE.md` | **REQUIRED** |
| 5 | `07_security/TENANT_ISOLATION.md` | **REQUIRED** |
| 6 | `08_deployment/ENVIRONMENT_VARIABLES.md` | RECOMMENDED |

---

## Track 3 — Full Technical Onboarding {#track-3}
**Time:** 4 hours | **Purpose:** Complete understanding for a new engineer

| Order | Document | Status |
|-------|---------|--------|
| 1 | All of Track 2 | **REQUIRED** |
| 2 | `03_database/DATABASE_BIBLE.md` | **REQUIRED** |
| 3 | `02_architecture/AUTH_ARCHITECTURE.md` | **REQUIRED** |
| 4 | `02_architecture/TENANT_ARCHITECTURE.md` | **REQUIRED** |
| 5 | `02_architecture/WHATSAPP_ARCHITECTURE.md` | **REQUIRED** |
| 6 | `02_architecture/AI_ARCHITECTURE.md` | **REQUIRED** |
| 7 | `02_architecture/BILLING_ARCHITECTURE.md` | **REQUIRED** |
| 8 | `04_backend/BLUEPRINTS.md` | **REQUIRED** |
| 9 | `07_security/RBAC.md` | **REQUIRED** |
| 10 | `09_testing/REGRESSION_CHECKLIST.md` | **REQUIRED** |
| 11 | `10_engineering/CODING_STANDARD.md` | **REQUIRED** |
| 12 | `08_deployment/RAILWAY_DEPLOYMENT.md` | RECOMMENDED |
| 13 | `06_api/API_REFERENCE.md` | REFERENCE |
| 14 | `11_reference/GLOSSARY.md` | REFERENCE |

---

## Track 4 — Security Review {#track-4}
**Time:** 2 hours | **Purpose:** Security audit of the platform

| Order | Document | Status |
|-------|---------|--------|
| 1 | `07_security/SECURITY_GUIDE.md` | **REQUIRED** |
| 2 | `07_security/RBAC.md` | **REQUIRED** |
| 3 | `07_security/TENANT_ISOLATION.md` | **REQUIRED** |
| 4 | `07_security/SECRETS.md` | **REQUIRED** |
| 5 | `02_architecture/AUTH_ARCHITECTURE.md` | **REQUIRED** |
| 6 | `03_database/SCHEMA_RULES.md` | RECOMMENDED |
| 7 | `08_deployment/ENVIRONMENT_VARIABLES.md` | **REQUIRED** |

---

## Track 5 — New Feature Implementation {#track-5}
**Time:** Variable | **Purpose:** Safely implement a new feature

| Order | Document | Status |
|-------|---------|--------|
| 1 | `00_meta/NEW_CHAT_BOOTSTRAP.md` | **REQUIRED** |
| 2 | `17_ai_context/ACTIVE_TASKS.md` | **REQUIRED** |
| 3 | `17_ai_context/NEXT_PHASE.md` | **REQUIRED** |
| 4 | `10_engineering/IMPLEMENTATION_WORKFLOW.md` | **REQUIRED** |
| 5 | `09_testing/REGRESSION_CHECKLIST.md` | **REQUIRED** |
| 6 | `08_deployment/ROLLBACK.md` | **REQUIRED** |
| 7 | Affected module document | **REQUIRED** |

---

## Track 6 — Documentation Maintenance {#track-6}
**Time:** 1 hour | **Purpose:** Update docs after a completed phase

| Order | Document | Status |
|-------|---------|--------|
| 1 | `00_meta/VERSION_POLICY.md` | **REQUIRED** |
| 2 | `14_history/PHASE_LEDGER.md` | **REQUIRED** |
| 3 | `17_ai_context/COMPLETED_TASKS.md` | **REQUIRED** |
| 4 | `17_ai_context/PROJECT_STATE.md` | **REQUIRED** |
| 5 | `17_ai_context/PROJECT_STATE.json` | **REQUIRED** |
| 6 | `12_release/CHANGELOG.md` | **REQUIRED** |
| 7 | All affected module docs | **REQUIRED** |

---

## Document Map by Importance

### CRITICAL (Read These Always)
- `17_ai_context/AI_MEMORY.md`
- `01_project/PROJECT_BIBLE.md`
- `07_security/TENANT_ISOLATION.md`
- `10_engineering/AI_DEVELOPMENT_CONSTITUTION.md`
- `03_database/DATABASE_BIBLE.md`

### HIGH (Read for Your Track)
- `02_architecture/SYSTEM_ARCHITECTURE.md`
- `02_architecture/AUTH_ARCHITECTURE.md`
- `14_history/PHASE_LEDGER.md`
- `07_security/RBAC.md`
- `08_deployment/ENVIRONMENT_VARIABLES.md`

### REFERENCE (Lookup Only)
- `11_reference/GLOSSARY.md`
- `06_api/API_REFERENCE.md`
- `03_database/ERD.md`
- `03_database/TABLES.md`

---

*Oxford CRM Documentation — docs/00_meta/READING_ORDER.md*
*Cross-references: `READ_FIRST.md` · `NEW_CHAT_BOOTSTRAP.md` · `DEPENDENCY_GRAPH.md`*
