---
KnowledgeID: DOC-SEC-RLS-READINESS
Version: 1.0
Status: ACTIVE (audit only — RLS is NOT enabled)
Owner: Security
Phase: Phase 0 — Sprint 3
Constitution: I.1 (dual-wall isolation), I.4 (reversible migrations)
---

# PostgreSQL RLS Readiness Audit

**Purpose:** prepare the second isolation wall (Constitution I.1 requires two
independent mechanisms; today only application-level `tenant_id` filtering
exists). **This document is an audit and plan only. No RLS is enabled by this
sprint.** Implementation is a later sprint, gated on this plan's approval.

Inventory generated from live ORM metadata on 2026-07-20 (22 tables total,
including the new `audit_log`).

---

## 1. Affected Tables

### 1a. Tenant-scoped — RLS candidates (17)

| Table | `tenant_id` nullable | Note |
|---|---|---|
| audience_rules | no | |
| billing_invoices | no | Money path — test class 2 coverage required before enabling |
| conversation_message | no | Highest-volume PII table |
| conversation_state | no | Core lead table |
| follow_up_jobs | no | Written by scheduler (no request context — see §3 R2) |
| lead_event | no | |
| message_log | no | |
| message_templates | no | |
| notifications | no | |
| offering | no | |
| pending_messages | no | |
| pipeline_definitions | no | |
| tag_definitions | no | |
| tasks | no | |
| tenant_settings | no | |
| **audit_log** | **yes** | NULL rows = platform-level events (super-admin login). Policy must allow NULL-tenant rows for the platform role only |
| **users** | **yes** | NULL rows = SUPER_ADMIN accounts. Same NULL-handling requirement; also read at login **before** any tenant context exists — see §3 R3 |

### 1b. Not tenant-scoped (5)

| Table | Why RLS does not apply directly |
|---|---|
| tenants | The tenant registry itself; platform-level |
| pipeline_stages | Scoped transitively via `pipeline_definitions.tenant_id` — an RLS policy would need a subquery (performance risk) or a denormalized `tenant_id` column (schema change, expand-phase) |
| conversation_state_offerings | Association table; scoped transitively via `conversation_state` |
| conversation_state_tags | Association table; scoped transitively via `conversation_state` |
| alembic_version | Migration bookkeeping |

**Finding:** the three transitively-scoped tables are the hard 20% of this
migration. Recommendation: denormalize `tenant_id` onto them (additive
expand-phase columns, backfilled from parents) *before* enabling RLS, rather
than subquery policies. Decision needs an ADR at implementation time.

---

## 2. Required Policies

Design: session-variable pattern, the standard for shared-connection Flask/
SQLAlchemy apps (per the Blueprint §5.2).

```sql
-- One-time, per tenant-scoped table:
ALTER TABLE conversation_state ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON conversation_state
    USING (tenant_id = current_setting('app.tenant_id', true));

-- Platform/maintenance role (migrations, backfills, super-admin, backups)
-- connects as a role with BYPASSRLS — it is NOT subject to policies.
```

Application side (implementation sprint, not now):
- On each request/unit-of-work, after tenant resolution:
  `SET LOCAL app.tenant_id = '<resolved id>'` via a SQLAlchemy event hook
  (`checkout`/`begin`), inside the transaction so `SET LOCAL` auto-clears.
- `current_setting('app.tenant_id', true)` returns NULL when unset → policy
  matches nothing → **fail closed** (a query without tenant context returns
  zero rows instead of leaking).
- `users` / `audit_log` NULL-tenant rows: policy variant
  `USING (tenant_id = current_setting(...) OR tenant_id IS NULL)` is **not**
  acceptable for `users` (would expose SUPER_ADMIN rows to every tenant
  session); instead the login path runs on the platform role. Decide per
  table in the implementation ADR.
- **Not FORCE RLS initially:** table owner bypasses policies. The app
  currently connects as the table owner (Railway default `postgres`), which
  means plain `ENABLE` is a no-op until a dedicated app role exists — see
  §3 R1, the single biggest prerequisite.

## 3. Migration Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | **App connects as table owner** — owner bypasses non-FORCE RLS, so enabling RLS changes nothing until a dedicated non-owner `app_user` role is created and `DATABASE_URL` switched. Doing both at once couples two failure modes | High | Two-step rollout: create role + switch connection first (observe one full week), enable RLS after |
| R2 | **Background workers have no request context** (follow-up scheduler, campaign worker, threads). If `app.tenant_id` is never SET on their connections, every read returns 0 rows → scheduler silently stops sending | High | Workers iterate jobs that carry `tenant_id`; each loop iteration must SET the variable. Must be built and tested before enabling |
| R3 | **Login reads `users` before tenant context exists** (email lookup, no tenant known yet) | High | Login runs on platform role, or `users` is excluded from wave 1 |
| R4 | Connection pooling: a plain `SET` (not `SET LOCAL`) leaks tenant context across pooled checkouts | Medium | `SET LOCAL` inside transactions only; add an isolation test that proves a fresh checkout has no tenant variable |
| R5 | Transitively-scoped tables (§1b) silently unprotected | Medium | Denormalize `tenant_id` first (expand-phase) or explicitly document them as wall-1-only |
| R6 | Restore drills: `pg_restore` needs a role that bypasses RLS | Low | Backup runbook already uses the owner role; note added at implementation |
| R7 | Performance: policy predicate on every query | Low | Predicate is an indexed equality (`tenant_id` is already indexed everywhere); measure p95 before/after on staging restore |

## 4. Rollout Plan (implementation sprint — evidence-gated)

1. **Wave 0 — prerequisites:** create `app_user` role (no BYPASSRLS, not owner);
   grant table privileges; switch `DATABASE_URL`; run one week in production.
   Extend the isolation suite with an RLS test class (runs against a real
   PostgreSQL scratch DB restored from backup — the drill infrastructure from
   Sprint 1 is the rehearsal environment).
2. **Wave 1 — low-risk tables:** enable RLS + policy on `message_log`,
   `lead_event`, `audience_rules`, `tag_definitions` (low write concurrency,
   no background writers). Canary: dogfood tenant. Watch 72h.
3. **Wave 2 — core lead tables:** `conversation_state`, `conversation_message`,
   `tasks`, `notifications`, `pending_messages`, `follow_up_jobs` — only after
   R2 (worker SET) is proven in staging.
4. **Wave 3 — decisions needed:** `users`, `audit_log` (NULL-tenant policy ADR),
   association tables (denormalization ADR), `billing_invoices` (after money-
   path tests exist).
5. Each wave: expand → observe → (only much later) consider FORCE RLS.

## 5. Rollback Plan

Per-table, instant, non-destructive — RLS is metadata only, zero data change:

```sql
ALTER TABLE <table> DISABLE ROW LEVEL SECURITY;   -- immediate, reversible
-- or fully:
DROP POLICY tenant_isolation ON <table>;
```

- Rollback of Wave 0 = point `DATABASE_URL` back at the owner role (redeploy,
  < 10 min per the Sprint 1 rollback target).
- No expand/contract concerns: policies are additive metadata; `DISABLE` never
  touches rows. The only irreversible step in the whole plan would be dropping
  legacy columns — which is not part of this plan.
- Trigger for rollback: any tenant-visible 0-row anomaly or scheduler stall
  (RR4 incident runbook: stabilize first — disable RLS on the affected table,
  diagnose second).

---

**Verdict:** RLS is feasible with the session-variable pattern, but **R1 (app
role) and R2 (background workers) are hard prerequisites** — enabling RLS today
would be a silent no-op (owner bypass) or a scheduler outage (worker context).
The implementation sprint must start with Wave 0, not with `ENABLE ROW LEVEL
SECURITY`.
