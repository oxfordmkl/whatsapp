# Oxford CRM — Documentation Version Policy
## How Documents Are Versioned and Maintained

> **Version:** 15.1 | **Phase:** 15B | **Owner:** Documentation Engineering Division
> **Last Updated:** 2026-07-02 | **Next Review:** After Phase 16.0

---

## Table of Contents

1. [Version Number Format](#format)
2. [When Versions Change](#when)
3. [Document-Level Versioning](#document-level)
4. [Phase-to-Version Mapping](#mapping)
5. [Update Obligations](#obligations)
6. [Stale Documentation Policy](#stale)
7. [Version History of This Document](#history)

---

## Version Number Format {#format}

Oxford CRM uses a three-tier version numbering system:

```
MAJOR.MINOR.PATCH

Examples:
  15.0    — Major documentation release (new module launched)
  15.1    — Minor update (phase completed)
  15.1.1  — Patch (typo fix, clarification, link repair)
```

### Major Version (X.0)
Incremented when a **new platform module is launched** or a **complete architectural overhaul** occurs.

| Phase | Trigger | Version Jump |
|-------|---------|-------------|
| Phase 15 (current) | Platform stabilization + docs system | 15.0 |
| Phase 16 (Subscription Engine) | New billing module live | 16.0 |
| Phase 17 (LMS) | New LMS module live | 17.0 |
| Phase 18 (Student Portal) | New student portal | 18.0 |
| Phase 19 (Mobile App) | Mobile app launch | 19.0 |
| Phase 20 (Enterprise AI) | AI expansion | 20.0 |

### Minor Version (X.Y)
Incremented when **any implementation phase is completed** or any significant document update occurs.

| Trigger | Example |
|---------|---------|
| Phase 14B.3 completed | 15.0 → 15.1 |
| Phase 15A audit completed | 15.1 → 15.2 |
| Phase 15B super admin implemented | 15.2 → 15.3 |

### Patch Version (X.Y.Z)
Incremented for:
- Typo corrections
- Broken link repairs
- Clarifications that don't change meaning
- Format improvements

---

## When Versions Change {#when}

### MUST increment version when:
- A new implementation phase is completed
- A new module is added to the platform
- A production incident changes architecture understanding
- A security decision is made
- A database migration runs

### SHOULD increment version when:
- A document gains a significant new section
- A risk is added or resolved
- A technical debt item is opened or closed

### Should NOT increment version for:
- Grammar corrections
- Markdown formatting tweaks
- Reordering of bullet points with no content change

---

## Document-Level Versioning {#document-level}

Every document carries its own version in the header block:

```markdown
> **Version:** 15.1 | **Phase:** 15B | **Owner:** [Team]
> **Last Updated:** YYYY-MM-DD | **Next Review:** [Trigger]
```

A document version does not need to match the project version exactly. A document that has not changed since version 15.0 remains at 15.0 even if the project is at 15.3.

---

## Phase-to-Version Mapping {#mapping}

| Phase | Name | Status | Doc Version |
|-------|------|--------|------------|
| Phase 9 | Workspace Redesign | COMPLETE | 9.0 |
| Phase 10 | Authentication | COMPLETE | 10.0 |
| Phase 10M | KPI Validation | COMPLETE | 10.1 |
| Phase 10N | Intelligence Engine | COMPLETE | 10.2 |
| Phase 11 | Communication Hub | COMPLETE | 11.0 |
| Phase 12 | WhatsApp Automation | COMPLETE | 12.0 |
| Phase 13 | Revenue Foundation | COMPLETE | 13.0 |
| Phase 14A | Production Audit | COMPLETE | 14.0 |
| Phase 14B | Production Stabilization | COMPLETE | 14.1 |
| Phase 14B.3 | Auth & Nav Stabilization | COMPLETE | 15.0 |
| Phase 15A | Architecture Audit | COMPLETE | 15.0 |
| Phase 15B | Documentation Generation | IN PROGRESS | 15.1 |
| Phase 15C | Super Admin Platform | PLANNED | — |
| Phase 16 | Subscription Engine | PLANNED | — |
| Phase 17 | LMS | PLANNED | — |

---

## Update Obligations {#obligations}

After every completed phase, these documents MUST be updated:

1. `17_ai_context/PROJECT_STATE.md` — update status dimensions
2. `17_ai_context/PROJECT_STATE.json` — sync with markdown
3. `17_ai_context/COMPLETED_TASKS.md` — add completion record
4. `17_ai_context/ACTIVE_TASKS.md` — remove completed item
5. `12_release/CHANGELOG.md` — add entry
6. `14_history/PHASE_LEDGER.md` — mark phase complete
7. Affected module documents — update content

---

## Stale Documentation Policy {#stale}

A document is classified as **stale** if it has not been updated within 2 completed phases of the current phase.

Stale documentation is a **documentation defect** and must be logged in `17_ai_context/KNOWN_RISKS.md` until resolved.

---

## Version History of This Document {#history}

| Version | Date | Change | Phase |
|---------|------|--------|-------|
| 15.1 | 2026-07-02 | Initial creation | 15B |

---

*Oxford CRM Documentation — docs/00_meta/VERSION_POLICY.md*
*Cross-references: `READING_ORDER.md` · `DEPENDENCY_GRAPH.md` · `12_release/CHANGELOG.md`*
