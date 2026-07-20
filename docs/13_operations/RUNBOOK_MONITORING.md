---
KnowledgeID: DOC-OPS-RUNBOOK-MONITORING
Version: 1.0
Status: ACTIVE
Owner: Operations
Phase: Phase 0 — Sprint 4
Constitution: IX.1 (golden signals, actionable alerts)
---

# Runbook — Production Monitoring

## Daily Glance (≤ 5 minutes, PMEP Part H)

1. **`/health`** — `https://web-production-d03fb.up.railway.app/health`
   Expect: `status: running`, `database: connected`, `whatsapp_token: valid`,
   `scheduler: running`, `gemini_active: true`.
2. **Railway logs** (`railway logs`) — scan for:
   - `ERROR` lines (structured format: `timestamp LEVEL logger message`)
   - `⚠️ Gemini quota` (degradation, not outage — DR Scenario 5)
   - `❌ Token invalid` (act TODAY — token death kills all messaging)
   - `[tenant] implicit resolution` (map of callers still passing None)
   - `[audit] FAILED` (audit write failures — investigate same day)
3. **Sentry** (once DSN set) — new issue types only; known noise is triaged
   or silenced (an alert ignored twice is deleted or fixed — IX.1).
4. **Backup log** — `backups/backup_log.txt` last line says `BACKUP OK` with
   today's date. Scheduled task: `schtasks /query /tn OxfordCRM-NightlyBackup`.

## Weekly (Friday ship-review)

- [ ] Backup log: 7 consecutive `BACKUP OK` lines; rotation holding at 8 files
- [ ] GitHub Actions: Nightly Backup workflow green (once secrets set)
- [ ] Error trend vs last week (Sentry / log grep)
- [ ] `follow_up_jobs` failures: `done=true AND failure_reason IS NOT NULL`
- [ ] Audit log spot check: recent LOGIN_FAILURE cluster = possible
      credential-stuffing — review IPs

## Monthly

- [ ] Restore drill: `powershell -File scripts\restore_verify.ps1` → log
      entry in BACKUP_RESTORE_RUNBOOK.md §5 (lapsed drill freezes features — I.5)
- [ ] One runbook verified by actually performing it (C.5 rotation)
- [ ] Passphrase off-machine copy still accessible (test retrieval, not trust)

## The Four Golden Signals — current sources (Stage S honest version)

| Signal | Source today | Gap registered |
|---|---|---|
| Errors | Sentry + structured logs | — |
| Latency | none (no p95 measurement) | Perf budgets = later sprint |
| Traffic | `/health` leads count, Railway metrics | — |
| Saturation | Railway CPU/mem dashboard | No alerting thresholds yet |

## Alert Discipline

Every alert must be actionable. Current alert inventory: Sentry new-issue
emails (once DSN set) + scheduled-task failure (visible in backup log gap).
No pager, no noise. Add alerts only with a written "when it fires, do X".
