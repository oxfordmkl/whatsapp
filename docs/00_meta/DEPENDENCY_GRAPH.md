# Oxford CRM — Document Dependency Graph
## Which Documents Depend on Which

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Documentation Engineering Division
> **Last Updated:** 2026-07-02 | **Next Review:** When any new document is added

---

## Purpose

This document maps the dependencies between all Oxford CRM documentation files. It ensures:
- No document is orphaned (referenced by no other document)
- No circular dependencies exist
- Maintainers know which documents must be updated together

---

## Dependency Tree

```
[ROOT: docs/]
│
├── 00_meta/READ_FIRST.md
│     └── references → 17_ai_context/AI_MEMORY.md
│     └── references → 00_meta/NEW_CHAT_BOOTSTRAP.md
│     └── references → 01_project/PROJECT_BIBLE.md
│
├── 00_meta/NEW_CHAT_BOOTSTRAP.md
│     └── references → 17_ai_context/AI_MEMORY.md
│     └── references → 17_ai_context/PROJECT_STATE.md
│     └── references → 10_engineering/AI_DEVELOPMENT_CONSTITUTION.md
│     └── references → 01_project/PROJECT_BIBLE.md
│
├── 01_project/PROJECT_BIBLE.md
│     └── references → 14_history/PHASE_LEDGER.md
│     └── references → 01_project/ROADMAP.md
│     └── references → 17_ai_context/PROJECT_STATE.md
│
├── 17_ai_context/AI_MEMORY.md          [BOOTSTRAP ANCHOR — no dependencies]
│
├── 17_ai_context/PROJECT_STATE.md
│     └── referenced by → AI_MEMORY.md
│     └── mirrored by  → PROJECT_STATE.json
│
├── 17_ai_context/PROJECT_STATE.json
│     └── depends on → PROJECT_STATE.md (must stay synchronized)
│
├── 17_ai_context/ACTIVE_TASKS.md
│     └── depends on → PROJECT_STATE.md
│
├── 17_ai_context/COMPLETED_TASKS.md
│     └── depends on → ACTIVE_TASKS.md
│     └── mirrors    → 14_history/PHASE_LEDGER.md
│
├── 17_ai_context/KNOWN_RISKS.md
│     └── depends on → PROJECT_STATE.md
│
├── 17_ai_context/NEXT_PHASE.md
│     └── depends on → PROJECT_STATE.md
│     └── depends on → ACTIVE_TASKS.md
│
├── 03_database/DATABASE_BIBLE.md
│     └── referenced by → 02_architecture/SYSTEM_ARCHITECTURE.md
│     └── referenced by → 02_architecture/TENANT_ARCHITECTURE.md
│     └── extended by  → 03_database/TABLES.md
│     └── extended by  → 03_database/ERD.md
│
├── 02_architecture/AUTH_ARCHITECTURE.md
│     └── referenced by → 07_security/RBAC.md
│     └── referenced by → 07_security/SECURITY_GUIDE.md
│     └── referenced by → 04_backend/BLUEPRINTS.md
│
├── 07_security/TENANT_ISOLATION.md
│     └── depends on → 03_database/DATABASE_BIBLE.md
│     └── depends on → 02_architecture/TENANT_ARCHITECTURE.md
│
├── 10_engineering/AI_DEVELOPMENT_CONSTITUTION.md
│     └── depends on → 01_project/PROJECT_BIBLE.md
│     └── referenced by → 00_meta/NEW_CHAT_BOOTSTRAP.md
│
└── 14_history/PHASE_LEDGER.md
      └── referenced by → 01_project/PROJECT_BIBLE.md
      └── mirrored by  → 17_ai_context/COMPLETED_TASKS.md
```

---

## Synchronization Rules

The following document pairs MUST be updated together:

| Primary Document | Must Also Update |
|-----------------|-----------------|
| `17_ai_context/PROJECT_STATE.md` | `17_ai_context/PROJECT_STATE.json` |
| `17_ai_context/ACTIVE_TASKS.md` | `17_ai_context/COMPLETED_TASKS.md` |
| `14_history/PHASE_LEDGER.md` | `17_ai_context/COMPLETED_TASKS.md` |
| `17_ai_context/KNOWN_RISKS.md` | `17_ai_context/PROJECT_STATE.md` |
| Any new document added | `00_meta/DEPENDENCY_GRAPH.md` (this file) |
| Any new document added | `00_meta/READING_ORDER.md` |

---

## Orphan Detection Rule

An orphaned document is one that is not referenced by any other document. Orphans are documentation defects.

**Current orphaned documents:** None (verified 2026-07-02)

---

## Anti-Patterns

The following dependency patterns are prohibited:
- `AI_MEMORY.md` depending on any other document (it must be self-contained)
- `PROJECT_STATE.json` containing information not present in `PROJECT_STATE.md`
- Any document depending on source code files (documents describe code, they do not import it)

---

*Oxford CRM Documentation — docs/00_meta/DEPENDENCY_GRAPH.md*
*Cross-references: `READING_ORDER.md` · `VERSION_POLICY.md`*
