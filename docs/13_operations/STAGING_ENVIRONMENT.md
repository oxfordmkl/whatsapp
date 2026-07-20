---
KnowledgeID: DOC-OPS-STAGING
Version: 1.0
Status: ACTIVE
Owner: Operations
Phase: Phase 0 — Sprint 4
---

# Staging Environment

## Current State (2026-07-20)

| Item | Status |
|---|---|
| Railway environment `staging` | ✅ **Created** (env id `c87e243a-e4d4-434a-8a7f-10c5f43f85e9`), empty shell |
| Staging Postgres | ❌ Not provisioned — **founder decision** (ongoing Railway usage cost) |
| Staging web service | ❌ Not deployed — depends on Postgres + variables |
| Production link | ✅ CLI relinked to `production` after creation (verified — backup automation depends on this link) |

## Provisioning (founder actions, Railway dashboard)

1. Dashboard → project `whatsapp_API` → environment `staging`.
2. Add a PostgreSQL database service (this starts billing for staging).
3. Add the `web` service to staging, source = same GitHub repo, **branch =
   `staging`** (create the branch from `main`). Production stays on `main`.
4. Set staging variables — copy from production **except**:
   - `DATABASE_URL`: staging Postgres (auto-set by Railway)
   - `PHONE_NUMBER_ID` / `ACCESS_TOKEN` / `WABA_ID`: **leave empty or use a
     Meta test number.** Never point staging at the production WABA — replies
     would come from the real Oxford number.
   - `GEMINI_API_KEY`: optional; empty = AI disabled, `smart_fallback` serves.
   - `SENTRY_DSN`: empty, or a separate staging Sentry project.
   - `PRIMARY_TENANT_ID`: set after seeding (step 5).
5. Seed data: restore the latest encrypted backup into the staging Postgres
   (scripts/restore_verify.ps1 §2A pattern, target = staging DATABASE_URL),
   **then anonymize** or accept that staging holds production PII under the
   same access controls (Constitution VI.1 prefers anonymized).

## Promotion Procedure (once provisioned)

1. All work lands on `main` locally; push to the **`staging` branch** first
   (`git push origin main:staging`). Railway deploys staging automatically.
2. Staging soak: run checklist I.3 smoke items against the staging URL
   (health, login, one webhook simulation via curl).
3. CI green on the commit (isolation suite blocking).
4. Promote = push the same commit to `main` (`git push origin main`).
   Production deploys the exact commit that passed staging — no cherry-picks,
   no staging-only fixes (fix on main, re-promote).
5. Post-deploy: checklist I.4 (error rates, p95, canary watch).

## Rollback Procedure

Same for staging and production (staging rehearses production's rollback):

1. **Code rollback:** Railway dashboard → service → Deployments → previous
   successful deployment → "Redeploy". Target < 10 min. CLI alternative:
   `railway redeploy` on the prior deployment id.
2. **Migration rollback:** only if the release contained one:
   `railway run flask db downgrade` (every migration ships a tested
   downgrade — proven pattern: e4a91b2c5f77 verified both directions on a
   production copy). Expand-phase artifacts may be left in place safely.
3. **Verify:** `/health` 200, login works, webhook round-trip observed.
4. Record the rollback in the incident log (RUNBOOK_INCIDENT_RESPONSE.md).

## Interim Position (until Postgres is provisioned)

Production deploys continue directly from `main` — unchanged from today. The
risk this leaves open is registered (TD: no staging soak). The environment
shell costs nothing while empty.
