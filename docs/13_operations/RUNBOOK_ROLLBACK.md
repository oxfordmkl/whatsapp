---
KnowledgeID: DOC-OPS-RUNBOOK-ROLLBACK
Version: 1.0
Status: ACTIVE
Owner: Operations
Phase: Phase 0 — Sprint 4
---

# Runbook — Rollback

Target: **< 10 minutes** from decision to previous version serving.
Principle (C.3): flag off / roll back FIRST, investigate second.

## Decision Triggers

Roll back immediately when, inside the watch window: boot failure · error-rate
spike · any tenant-isolation anomaly (SEV-1, also see incident runbook) ·
broken money/lead path (webhook, login, broadcast) · migration failure mid-run.

## A. Code-Only Release

1. Railway dashboard → `web` → **Deployments** → last good deployment →
   **⋮ → Redeploy**. (This redeploys the previous image — no git needed.)
2. Verify: `/health` 200 + boot logs clean + one webhook round-trip.
3. `git revert` the bad commit(s) on `main` afterwards so the branch matches
   reality (never force-push; never leave main deploying a known-bad HEAD).

## B. Release With Migration

1. Redeploy previous image (step A1) — old code first, so nothing writes
   new-schema data while you decide.
2. Assess the migration:
   - **Expand-phase only** (new table/column, e.g. audit_log): leave it in
     place — old code ignores it. This is the designed outcome (I.4).
   - **Anything destructive**: should not exist (blocked by policy). If one
     slipped through: STOP, treat as Scenario 1 in the DR playbook (restore
     path), not as a rollback.
3. Optional tidy (only if the expand artifact itself misbehaves):
   `railway run flask db downgrade` — every migration ships a tested
   downgrade.
4. Verify as in A2.

## C. Config/Variable Rollback

Railway → service → Variables → revert the changed value → redeploy trigger.
Variables are not versioned by Railway — the env-var inventory (off-machine
secrets copy, DR playbook Scenario 3 prereq) is the source of previous values.

## After Any Rollback

- [ ] Incident note (what, when, trigger, duration) — even for "boring" ones
- [ ] Root cause on `main` before re-deploying
- [ ] If rollback took > 10 min: that gap is itself a finding — fix the
      procedure, not just the bug
