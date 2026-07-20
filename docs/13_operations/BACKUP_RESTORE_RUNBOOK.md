---
KnowledgeID: DOC-OPS-BACKUP-RESTORE
Version: 1.0
Status: ACTIVE
Owner: Operations
Phase: Phase 0 — Sprint 1 (Production Safety)
Constitution: Immutable Core I.5 — "A backup that has not been restored is not a backup."
---

# Backup & Restore Runbook

This runbook was written from an **actually executed** backup + restore cycle
(2026-07-20), not from theory. Every command below has been run successfully
against production. Constitution VI.5 governs; the drill cadence is monthly.

**Drill status:** First restore drill **PASSED 2026-07-20** (see §5 log).

---

## 1. Backup Runbook

**Prerequisites:** PostgreSQL client tools ≥ server version (`pg_dump --version`;
operator machine has 18.4), Railway CLI logged in and linked to project
`whatsapp_API` / environment `production`.

**Procedure (from repo root, Git Bash):**

```bash
mkdir -p backups
DBURL=$(railway variables --json | python -c "import sys,json; print(json.load(sys.stdin)['DATABASE_URL'])")
STAMP=$(date +%Y%m%d_%H%M)
pg_dump --format=custom --no-owner --file="backups/oxfordcrm_${STAMP}.dump" "$DBURL"
```

**Rules:**
- Custom format (`-Fc`) always — compressed, supports selective/parallel restore.
- **Never** echo, log, or commit `$DBURL` — it contains production credentials.
- Immediately verify integrity (§4 checklist) after every backup.
- Take an ad-hoc backup **before every schema migration and every data backfill**,
  in addition to the routine cadence.

**Cadence (Sprint 4 — AUTOMATED):** nightly at 02:00 via Windows Task Scheduler
(task `OxfordCRM-NightlyBackup` → `scripts/backup_production.ps1`): dump →
integrity check → **AES-256-CBC encryption** (passphrase
`backups/.backup_passphrase`, git-ignored — off-machine copy REQUIRED) →
plaintext deleted → rotation keep-8 → `backups/backup_log.txt` entry.
Manual runs remain mandatory before every migration deploy.
Off-machine second provider: `.github/workflows/backup.yml` (nightly GitHub
Actions artifact, 30-day retention) — dormant until repo secrets
`BACKUP_DATABASE_URL` + `BACKUP_PASSPHRASE` are set.
Restore automation: `scripts/restore_verify.ps1` (decrypt → verify → scratch
restore → spot check → cleanup; production never touched).

---

## 2. Restore Runbook

Two scenarios. Both were derived from the executed drill.

### 2A. Restore verification / drill (non-destructive — monthly)

Restores into a scratch database **on the same Railway Postgres instance**;
production database is never touched.

```bash
DBURL=$(railway variables --json | python -c "import sys,json; print(json.load(sys.stdin)['DATABASE_URL'])")
# Scratch URL = same URL with database name replaced:
SCRATCH=$(python -c "
u = '$DBURL'
base, _, db = u.rpartition('/')
q = ''
if '?' in db: db, _, q = db.partition('?')
print(base + '/oxford_restore_verify' + ('?' + q if q else ''))
")

psql "$DBURL" -c "DROP DATABASE IF EXISTS oxford_restore_verify;" \
              -c "CREATE DATABASE oxford_restore_verify;"
pg_restore --no-owner --no-privileges --dbname="$SCRATCH" backups/<DUMP_FILE>

# Verify (see §4), then ALWAYS clean up:
psql "$DBURL" -c "DROP DATABASE oxford_restore_verify;"
```

### 2B. Disaster restore (destructive — real data-loss incident only)

1. **STOP the app first** so nothing writes mid-restore: Railway dashboard →
   service `web` → remove/rename `DATABASE_URL` or scale to 0. Do not skip this.
2. Restore into a **new** database (`oxfordcrm_restored`) using the 2A procedure —
   never `--clean` over the damaged production DB; the damaged DB is forensic
   evidence and your fallback.
3. Run the §4 verification checklist against the restored database.
4. Point the app at the restored database (update `DATABASE_URL` in Railway
   service variables) and redeploy.
5. Smoke test: `/health` returns 200; login works; one WhatsApp webhook
   round-trip observed.
6. Keep the damaged database for ≥ 7 days before dropping.

**Measured restore time (drill, 2026-07-20):** < 1 minute for the current
~170 KB dataset. Re-measure as data grows; record in each drill log.

---

## 3. Backup Location Documentation

| Location | What | Retention |
|---|---|---|
| `<repo>/backups/` on operator machine (`D:\oxford\...\oxford-whatsapp_2\backups\`) | `oxfordcrm_YYYYMMDD_HHMM.dump` (custom format) | Keep last 8; prune older manually |
| *(none yet)* | Off-machine second-provider copy | **Registered debt** — Constitution VI.5 requires a different provider; Sprint 3 scope |

**Security:** dumps contain lead PII, user password hashes, and encrypted WABA
tokens. The `backups/` directory and `*.dump` are git-ignored (verified) and must
never be committed, emailed, or uploaded to unmanaged storage.

---

## 4. Backup Verification Checklist

Run after **every** backup (steps 1–3) and during **every** drill (all steps):

- [ ] 1. File exists and size > 0 (`ls -la backups/`)
- [ ] 2. `pg_restore --list <dump>` exits 0 (dump is structurally readable)
- [ ] 3. All 21 application tables present: `pg_restore --list <dump> | grep -c "TABLE DATA"` → expect **21** (update this number when migrations add tables)
- [ ] 4. Restore to scratch DB completes with **zero errors** (§2A)
- [ ] 5. Row counts match production for the 10 key tables (tenants, users, conversation_state, conversation_message, lead_event, message_log, tasks, notifications, billing_invoices, follow_up_jobs)
- [ ] 6. `alembic_version` in the restored DB equals production's migration head
- [ ] 7. Scratch database dropped after verification
- [ ] 8. Drill logged in §5 with date, dump file, duration, result

---

## 5. Drill Log

| Date | Dump file | Steps passed | Duration | Result | Operator |
|---|---|---|---|---|---|
| 2026-07-20 | `oxfordcrm_20260720_1532.dump` (171 KB, 239 TOC entries, 21 tables) | 1–8 | < 1 min restore | **PASS** — 10/10 table counts identical (tenants 10, users 14, conversation_state 31, conversation_message 1030, lead_event 360, message_log 1085, tasks 2, notifications 4, billing_invoices 0, follow_up_jobs 93); alembic head `c7a2f19d4e88` matched | Claude (Phase 0 Sprint 1), on founder instruction |

| 2026-07-20 | `oxfordcrm_20260720_1623.dump.enc` (encrypted, 21 tables) | automated: decrypt → list → scratch restore → spot check | < 3 min | **PASS** — tenants=10, conversation_state=31, scratch dropped (via `scripts/restore_verify.ps1`) | Claude (Phase 0 Sprint 4) |
| 2026-07-20 | rotation test (9 seeded dummies + real backups) | rotation | — | **PASS** — exactly 8 kept, oldest rotated out | Claude (Phase 0 Sprint 4) |

Next drill due: **2026-08-20** (monthly, per Constitution VI.5) — run
`powershell -File scripts\restore_verify.ps1` and append the log line here.
