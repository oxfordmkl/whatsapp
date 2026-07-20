# ADR-022: Project Ratification — Governing Documents Adopted
## Architectural Decision Record

> **Version:** 1.0 | **Phase:** Phase 0 — Sprint 1 | **Audience:** Architects, Engineers
> **Reading Time:** 2 min | **Owner:** Project Leadership
> **Last Updated:** 2026-07-20 | **Dependencies:** None (this ADR is the root of the governance chain)

---

## 1. Context

Three governing documents were authored to direct the platform's next decade:

1. **Architecture Blueprint** — `docs/multi-tenant-platform-architecture-review.md`
2. **Platform Constitution** — `docs/platform-governance-evolution-constitution.md`
3. **Platform Master Execution Plan (PMEP)** — `docs/platform-master-execution-plan.md`

All three carried "Draft" status. A Phase 0 Readiness Audit (2026-07-20) compared
the production codebase against them and found the codebase **predates** the
documents: the CRM is a live system with 10 tenants and real PII, built before
this governance existed.

## 2. Problem

Unratified governing documents govern nothing. The audit's findings cannot be
prioritized, and future decisions cannot be held to a standard, until the
standard is formally adopted — including an honest statement of how a
pre-existing production system relates to it.

## 3. Decision

The following are **ratified**, effective 2026-07-20:

1. ✅ The **Architecture Blueprint** is ratified.
2. ✅ The **Platform Constitution** is ratified, including its seven-article
   Immutable Core (I.1–I.7).
3. ✅ The **PMEP** is ratified, including its corrected phase sequence
   (P0 Foundation → P1 Walking Skeleton → …).
4. ✅ These three documents **govern all future implementation**. Precedence on
   conflict: Constitution > Blueprint > PMEP (per PMEP's own hierarchy clause).
5. ✅ **Architectural deviations require ADR approval** before implementation
   (Constitution Art. IV triggering events apply).
6. ✅ **The existing production CRM predates these documents.** Gaps between the
   current system and the documents' requirements are classified as
   *implementation backlog* (gap analysis), not as violations — except where a
   gap constitutes an active production risk, which is triaged as such
   (see the reclassified Phase 0 audit, 2026-07-20).
7. ✅ **Future work aligns the system incrementally, without rewrite.**
   Constitution III.3 (strangler-fig only; no big-bang rewrites at Stage S)
   applies to the alignment itself.

## 4. Alternatives

- **Leave the documents as advisory drafts:** rejected — advisory governance is
  abandoned governance (Constitution Preamble, Truth 1).
- **Declare the existing system non-compliant and rebuild to spec:** rejected —
  violates Constitution III.3 and the Blueprint's own anti-rewrite doctrine;
  destroys a revenue-bearing system to satisfy paper.

## 5. Consequences

- **Pros:** every future decision has a written standard; the Phase 0 backlog has
  a legitimate basis; the Immutable Core is now binding.
- **Cons / accepted debts:** the codebase is Flask while the Blueprint's diagrams
  assume Django — this deviation is *known and accepted* at ratification (the
  system predates the Blueprint; see clause 6). Any future decision to alter or
  reaffirm the framework choice requires its own ADR; no rewrite is authorized.
- The known Phase 0 gaps (isolation tests, CI, RLS, automated backup, audit log,
  etc.) are registered in `docs/18_bootstrap/KNOWN_TECHNICAL_DEBT.md` and worked
  through the approved sprint backlog.

## 6. Ratification Record

| Item | Value |
|---|---|
| Ratified by | Founder (approval given in Phase 0 planning session) |
| Date | 2026-07-20 |
| First sprint under this governance | Phase 0 — Sprint 1 (Production Safety) |
| Constitution annual review due | 2027-07-20 (Art. II.5) |
