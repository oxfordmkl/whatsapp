---
KnowledgeID: DOC-OPS-DR-PLAYBOOK
Version: 1.0
Status: ACTIVE
Owner: Operations
Phase: Phase 0 — Sprint 4
Constitution: VI.4 (DR strategy), I.5 (proven restores)
Objectives: RPO ≤ 24h (nightly encrypted backups) · RTO ≤ 8h (Stage S targets)
---

# Disaster Recovery Playbook

Rule zero (from checklist I.6): **stabilize first, diagnose second.** Every
scenario below opens with the stabilizing move. Log a timeline *during* the
incident; post-mortem ADR within 72h.

Key assets: encrypted backups in `<repo>/backups/` (nightly, keep-8, passphrase
`backups/.backup_passphrase` — copy MUST exist off-machine) · GitHub Actions
artifact backups (once secrets configured) · restore automation
`scripts/restore_verify.ps1` · Railway CLI + dashboard access.

---

## Scenario 1 — Database Failure (corruption / data loss / bad migration)

**Detect:** 500s on all DB routes, `/health` shows database error, Sentry burst.

1. **Stabilize:** stop writes — Railway dashboard → `web` → Settings → remove
   deployment (or scale to 0). A corrupted DB that keeps taking writes gets
   worse.
2. Take a snapshot of the damaged DB if the instance still responds
   (`pg_dump` — it is forensic evidence AND your newest data).
3. Restore the newest good backup into a **new** database on the instance
   (never `--clean` over the damaged one):
   `powershell -File scripts\restore_verify.ps1` proves the backup restores;
   then repeat the restore targeting `oxfordcrm_restored` and keep it.
4. If the damage was a migration: `flask db downgrade` on the restored copy
   to the last good revision instead of replaying the bad one.
5. Point `DATABASE_URL` (service variable) at the restored database; redeploy.
6. Verify: `/health` 200 · login · one webhook round-trip · tenant count = 10.
7. Data-loss window = time since backup (≤ 24h). Reconcile from `message_log`
   in the damaged copy if recoverable. Keep the damaged DB ≥ 7 days.

## Scenario 2 — Bad Deployment (rollback)

**Detect:** post-deploy errors, failed boot, Sentry spike within watch window.

1. **Stabilize:** Railway dashboard → Deployments → last good deployment →
   **Redeploy** (target < 10 min). Code-only releases end here.
2. If the release carried a migration: run `flask db downgrade` via
   `railway run` (all migrations ship proven downgrades; expand-phase leftovers
   are safe to leave).
3. Verify (§1.6 checks). Root-cause on `main` before any re-deploy.
4. Full procedure: RUNBOOK_ROLLBACK.md.

## Scenario 3 — Railway Outage

**Detect:** dashboard unreachable / status.railway.app incident / app down but
DNS resolves.

1. **Confirm scope:** check status.railway.app. If regional/platform: nothing
   to fix in our code — do NOT thrash-deploy into an outage.
2. WhatsApp messages during the outage: Meta retries webhook delivery with
   backoff for a limited window; short outages self-heal. For a long outage
   (> ~4h), inbound messages may be lost — plan customer comms.
3. **Long outage / provider loss (the real DR case):** rebuild elsewhere from
   assets we own — this is the Blueprint §3.6 "can I redeploy to a bare VPS?"
   drill: provision Postgres anywhere → decrypt + restore newest backup →
   deploy repo (Procfile: gunicorn) → set env vars from the off-machine
   secrets copy → repoint the Meta webhook URL and the domain. RTO target 8h.
   Prereq gap (registered): env-var inventory must exist off-machine, not
   only in Railway.
4. After recovery: post-mortem + reassess provider risk (ADR if migrating).

## Scenario 4 — WhatsApp / Meta Outage

**Detect:** `send_*` non-200 spikes in logs, token-invalid alerts, Meta status.

1. **Confirm:** business.facebook.com status + one manual Graph API call
   (`validate_token` path). Distinguish outage vs **token expiry/ban** —
   token issues are OURS: rotate token in Railway variables, redeploy.
2. During a Meta outage: inbound stops arriving (Meta-side), outbound fails.
   The follow-up scheduler retries ×3 with backoff and then marks jobs
   permanently failed — after a long outage, review `follow_up_jobs` where
   `done=true AND failure_reason IS NOT NULL` and manually re-queue.
3. Do not spam retries into a down API (rate-limit risk on recovery).
4. Broadcast runs planned during an outage: postpone — never fire into
   degraded delivery.
5. Strategic mitigation (registered, Blueprint RR3): notification adapter
   with SMS/email parity — not built yet.

## Scenario 5 — Gemini Outage

**Detect:** `Gemini error/quota` warnings in logs; leads receive
`smart_fallback` static replies.

1. **Impact = degraded, not down:** the router's deterministic branches
   (menus, courses, fees, demo booking) work fully; only free-form AI replies
   fall back to `smart_fallback`. **No emergency action required.**
2. Distinguish quota (429 — resets on window; consider key/plan) from API
   outage (Google status page).
3. If prolonged: no code change needed — fallback is the designed behavior.
   Optionally notify staff to watch conversations more actively.
4. Never hot-swap model/provider mid-incident (Constitution XIV.6: model
   changes are dependency changes — ADR, not a panic move).

---

## After Every Scenario

- [ ] Timeline logged during the incident (not reconstructed)
- [ ] Customer/tenant comms if user-visible (pre-written templates —
      RUNBOOK_INCIDENT_RESPONSE.md)
- [ ] Blameless post-mortem ADR within 72h, ≥ 1 systemic fix scheduled
- [ ] Backup taken AFTER recovery (post-incident state is a new baseline)
- [ ] DPDP breach assessment if any personal data was exposed
