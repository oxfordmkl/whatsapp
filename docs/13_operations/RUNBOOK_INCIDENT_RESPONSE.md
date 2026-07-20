---
KnowledgeID: DOC-OPS-RUNBOOK-INCIDENT
Version: 1.0
Status: ACTIVE
Owner: Operations
Phase: Phase 0 — Sprint 4
Constitution: VIII.1 (pre-written comms), checklist I.6
---

# Runbook — Incident Response

## 1. Classify (first 5 minutes)

| Severity | Definition | Examples |
|---|---|---|
| **SEV-1** | Tenant-isolation or data incident — REGARDLESS of size | Any cross-tenant read/write anomaly · data loss · credential leak |
| SEV-2 | Core path down for all tenants | Webhook dead · login dead · DB down |
| SEV-3 | Degraded | Gemini fallback active · slow pages · one feature broken |

SEV-1 additions: full stop on deploys; preserve evidence (dump the DB before
any fix); DPDP breach assessment mandatory before closing.

## 2. Stabilize (before diagnosing)

In order of preference: feature flag off (when flags exist) → rollback
(RUNBOOK_ROLLBACK.md) → scale to 0 / stop writes (SEV-1 data corruption) →
rate-limit. Pick the smallest move that stops the bleeding.

## 3. Timeline Log

Start immediately, in `docs/16_reports/incidents/INC_<date>_<slug>.md`:
timestamped entries, WRITTEN DURING the incident. What you observed, what you
did, what changed. Memory reconstructs badly under stress.

## 4. Communications (pre-written — fill blanks, don't draft prose mid-incident)

**Tenant notice — degraded service:**
> Oxford CRM update: some features ([WhatsApp replies / dashboard]) are
> currently degraded. Your data is safe. We are actively working on it and
> will update you by [TIME]. — The Oxford Computers

**Tenant notice — resolved:**
> Resolved: the issue affecting [FEATURE] between [START] and [END] is fixed.
> [One sentence: impact, e.g. "Some WhatsApp replies were delayed; none were
> lost."] Thank you for your patience.

**Data-incident notice (SEV-1, send only after legal/DPDP assessment):**
> We are writing to inform you of a data incident on [DATE] affecting
> [SCOPE]. What happened: [FACTS]. What we have done: [ACTIONS]. What you
> should do: [STEPS]. Contact: [CHANNEL].

## 5. Resolve & Verify

Fix → verify with the same evidence that detected the incident → post-deploy
smoke (RUNBOOK_DEPLOYMENT.md) → fresh backup (post-incident state is the new
baseline).

## 6. Post-Mortem (within 72h, blameless)

ADR format: timeline summary · root cause (5 whys, systems not people) ·
what detection missed · ≥ 1 systemic fix SCHEDULED (not "noted") · whether
any runbook was wrong (a wrong runbook is a SEV of its own — fix it now).
