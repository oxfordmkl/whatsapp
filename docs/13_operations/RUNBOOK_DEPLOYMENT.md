---
KnowledgeID: DOC-OPS-RUNBOOK-DEPLOY
Version: 1.0
Status: ACTIVE
Owner: Operations
Phase: Phase 0 — Sprint 4
---

# Runbook — Production Deployment

Railway auto-deploys `main` on push. Deployment discipline therefore lives in
what happens **before and after** the push.

## Pre-Deploy Checklist

- [ ] CI green on the commit (isolation suite is blocking — never bypass)
- [ ] Full local regression run if CI unavailable (5 suites, 179+ checks)
- [ ] Migration in the release? → its downgrade is written AND was tested
      against a restored production copy (pattern: e4a91b2c5f77)
- [ ] Backup fresh (< 24h). If not: `powershell -File scripts\backup_production.ps1`
- [ ] Staging soak done (once staging Postgres is provisioned — see
      STAGING_ENVIRONMENT.md; until then, note the skip)
- [ ] Low-traffic window: NOT month-end, NOT admission-season mornings
      (checklist I.3); avoid Fri-evening deploys — nobody watches the window
- [ ] Rollback path identified for THIS release's riskiest change (< 10 min)

## Deploy

1. `git push origin main`
2. Watch build: `railway status` until `● Online` (build ~1–3 min).
3. If the release includes a migration: confirm it ran in deploy logs
   (`railway logs | grep alembic`), or run manually: `railway run flask db upgrade`.

## Post-Deploy Smoke (within 10 minutes)

- [ ] `curl https://web-production-d03fb.up.railway.app/health` → 200,
      `database: connected`, `whatsapp_token: valid`, `scheduler: running`
- [ ] Boot logs clean: `railway logs` shows Gemini init, scheduler, token valid
- [ ] CRM login works
- [ ] One WhatsApp round-trip observed (send "hi" from a test number)
- [ ] Sentry: no new error types (once DSN configured)

## Watch Window (T+24–72h, checklist I.4)

- [ ] Error rates vs baseline · p95 vs budget · background-job lag normal
- [ ] `[tenant] implicit resolution` warnings reviewed (each one maps a caller
      that should pass tenant_id explicitly)
- [ ] Canary/dogfood tenant feedback

Anything red → RUNBOOK_ROLLBACK.md. Never "watch it and hope" a red check.
