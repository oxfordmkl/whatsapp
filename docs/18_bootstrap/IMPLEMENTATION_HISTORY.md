---
KnowledgeID: DOC-BOOT-HISTORY
Version: 1.0
Status: ACTIVE
Owner: Architecture Team
---

# IMPLEMENTATION HISTORY

Summary of engineering phases from inception to the present.

## Phase 1 - 15 (Education CRM Era)
- **Objective**: Build a single-tenant CRM to manage student admissions via WhatsApp AI.
- **Implementation**: Flask backend, SQLite, Meta API webhook, Gemini LLM router.
- **Result**: Functional product. 
- **Production Status**: Legacy (Transitioned).
- **Related ADR**: `ADR-001`, `ADR-003`.

## Phase 16.0 - 16.4 (Multi-Tenant Foundation)
- **Objective**: Support multiple businesses on the same instance.
- **Implementation**: Introduced `Tenant` model, RBAC `User` roles, Tenant Isolation filtering in `admin_bp`. Migrated to PostgreSQL on Railway.
- **Result**: Successfully isolated data per tenant.
- **Production Status**: Active in Production.
- **Related ADR**: `ADR-004`, `ADR-007`.

## Phase 16.5A1 - 16.5A4 (Enterprise Config Foundation)
- **Objective**: Transition away from hardcoded education logic (courses, admitted status) to dynamic SaaS configurations.
- **Implementation**: Drafted schema for `PipelineDefinition`, `PipelineStage`, `Offering`, `TagDefinition`, `AudienceRule`.
- **Result**: Data model frozen. No physical DB migrations yet.
- **Production Status**: Architecturally Approved.
- **Related ADR**: Phase 16.5 Architecture Docs.

## Phase 16.5A5 + H1/H2/I (Enterprise ORM Adapter Layer)
- **Objective**: Bridge legacy `ConversationState` string columns to the new relational config models without breaking any live query.
- **Implementation**:
  - `16.5A5`: schema expansion — `Offering`, `ConversationOffering`, `ConversationTag`, `pipeline_stage_id`, `custom_attributes` (migrations `a5f0c3e91b7d`, `b6e1d4f82c9e`).
  - `H1`: Enterprise Baseline v1.1 — `db.JSON` standard (ADR-013), `custom_attributes` naming (ADR-014), `Offering.price` nullable.
  - `H2`: query compatibility refactor — `filter_by()` → `.filter()` on hybrid-property fields.
  - `I`: dual-write `hybrid_property` adapters on `ConversationState` for `stage`, `is_admitted`, `course`, `offer_course`, `batch_time`. Legacy columns retained under `_`-prefixed names (same DB column names — no migration). Reads prefer the relational model when `pipeline_stage_id` is set; otherwise fall back to the legacy column. Writes always update the legacy column; JSON dual-write is live for `offer_course`/`batch_time`; pipeline/offering link-sync activates post-backfill (16.5A6).
- **Result**: Transparent compatibility layer. Zero production behaviour change (every expression reduces exactly to the legacy column while `pipeline_stage_id` is NULL). No data migration, no schema change in Phase I.
- **Production Status**: Active. Adapter dormant-until-backfill.
- **Related ADR**: `ADR-013`, `ADR-014`; Data Model Freeze v1.1 §7.

## Phase 16.5A5-J (Enterprise Architecture Correction)
- **Objective**: Correct an architectural assumption disproven by Phase 16.5A6 discovery, before any production data was migrated.
- **Trigger**: Phase 16.5A6 returned **NO-GO**. Discovery proved `stage` (AI-router-owned) and `is_admitted` (staff-form-owned) are independent columns that legitimately disagree; one `pipeline_stage_id` FK cannot reproduce both. Backfilling would have broken either the admissions analytics or the router state machine.
- **Implementation**:
  - **ADR-018 Business Conversion Independence** — `is_admitted` is an independent business attribute, NEVER derived from `stage_category`. The 16.5A5-I hybrid adapter and `_sync_admitted_link` removed; reverted to a plain `db.Boolean` column.
  - **ADR-019 Compatibility Pipeline Standard** — the first pipeline per legacy tenant must use the exact 12 legacy router stage strings as `internal_key`; `Offering.name` must preserve the exact legacy course string (no slug dedup).
  - Data Model Freeze corrected v1.1 → **v1.2** (§2, §7). `PipelineStage`/`Offering` docstrings corrected.
- **Result**: Enterprise Baseline now matches production reality. No data migration, no schema change, no behaviour change. `is_admitted` SQL reverts to a direct indexed column read (cheaper than the interim CASE).
- **Production Status**: Active. Phase 16.5A6 unblocked.
- **Related ADR**: `ADR-018`, `ADR-019`.

## Phase 16.5A7-B (Task Engine Completion)
- **Objective**: Fix ONLY the two blocking defects from the Phase 16.5A7-A deployment audit. No redesign, no schema change, no migration change.
- **B1 — staff-to-staff privilege escalation (security)**: `_get()` scoped by tenant alone, so any staff member could re-status, overwrite the notes of, and **claim completion credit** for a colleague's task (`completed_by` feeds `staff_productivity`). Tenant scoping is not authorization — it only blocks tenant-to-tenant. **Fix**: `_authorize_mutation(task, actor, is_admin)` gates `staff_update()` / `complete_task()` — admin → any task in tenant; staff → only their own; **unassigned → admin-only** (closes the back door). Routes pass `_actor_is_admin()`; denial returns **403**. `_actor_tenant_id()` now honours `session['impersonate_tenant_id']`, matching `tenant_query()` — a SUPER_ADMIN (tenant_id NULL) could previously neither create tasks nor read notifications while impersonating.
- **B2 — Task Engine was write-only**: `list_tasks()` had **zero callers**; all five read sites replayed LeadEvents, so admin edits were invisible, deletes left **zombies**, and priority / IN_PROGRESS / staff_notes / standalone tasks never rendered. **Fix**: `get_all_tasks()`'s *data source* rewritten to Task-table-first with a legacy-replay compatibility layer (Task row always wins); its **return contract is unchanged**, so all four callers and their templates keep working. Lead-detail `task_summary` likewise sources from Task + legacy. `crm_my_tasks.html` renders priority / IN_PROGRESS / notes / staff_notes and guards the `/crm/lead/None` link for standalone tasks. `crm_tasks_complete` no longer requires `phone` when a Task row exists.
- **ADR-021 amended** (D1a, D2a): the legacy mirror is now **deleted with the Task row** — keeping it as "audit history" was what created the zombie. For a 16.5A7 task the events are a mirror of the record, not independent history; leaving it made it a phantom source of truth (the ADR-020 failure mode). Pre-16.5A7 tasks are untouched.
- **Validation**: 42/42 (`tests/test_task_engine_16_5a7b.py`) — every test asserts a forbidden path *fails* or that the reader reflects the Task table. The original 16.5A7-A B1 probe re-run unchanged: all **SAFE**. B2 verified against the **real** `get_all_tasks()` (the audit's probe embedded a copy of the pre-fix algorithm and is now a fossil).
- **Regression**: 136 checks green (42 + 64 16.5A7 + 30 16.5A6-J); 32/32 templates compile; app boots, 84 routes.
- **Production Status**: Code only — **not deployed**. Migration `c7a2f19d4e88` still pending. No production data written.
- **Related ADR**: `ADR-021` (amended D1a, D2a).

## Phase 16.5A7 (Enterprise Task & Notification Foundation)
- **Objective**: Establish the Admin↔Staff operational workflow: Admin-owned tasks and in-app notifications.
- **Discovery**: (a) tasks were **event-sourced**, not a table — `LeadEvent` rows (`FOLLOW_UP_TASK` / `FOLLOW_UP_COMPLETED`) replayed by `get_all_tasks()`, read by **15 sites**; append-only, so priority/edit/delete were impossible. (b) **No notification system existed** — greenfield. (c) `course` write funnel confirmed single-path. (d) **Two live defects found**, both pre-existing.
- **Live defect 1 — RBAC**: `crm_tasks_create` called only `check_auth()`, so **any STAFF member could create tasks**. The `admin_required` decorator already existed and was simply not applied.
- **Live defect 2 — tenant misassignment**: writes used `_get_default_tenant_id()` = `Tenant.query.first()` ("safe while only one tenant exists" — production has **10**). It resolves to `amboori` while all leads live in `oxford-computers`, so **18 production `lead_event` rows are mis-filed**: 2 `FOLLOW_UP_TASK` (invisible to the CRM that created them), 4 `FOLLOW_UP_COMPLETED` (those tasks appear open forever), 7 `LEAD_REASSIGNED`, 5 `MANUAL_MESSAGE`. Reads used `current_user.tenant_id`; writes guessed. Also a cross-tenant PII exposure (ACTIVE_CONSTRAINTS §2).
- **Implementation** (ADR-021):
  - New `tasks` + `notifications` tables (migration `c7a2f19d4e88`). Enterprise Data Layer untouched.
  - `task_service` / `notification_service` — all logic out of routes (CODE_CONVENTIONS §3).
  - **Dual-write** (ACTIVE_CONSTRAINTS §4): legacy events still emitted on create/complete, so all 15 readers work unchanged. Edit/delete do NOT rewrite history — the log is an audit trail. Deliberately avoids the ADR-020 staleness trap: legacy events are not a read path for the Task Engine.
  - `@admin_required` on create/edit/delete; `staff_update()` structurally cannot reassign or retitle.
  - `notify()` **raises** without an explicit tenant_id rather than guessing; both touched legacy write sites corrected to the actor's tenant.
  - Notification bell in `crm_sidebar.html` (reaches all 27 pages from one include) + `crm_notifications.html` centre.
  - 7 notification types; click → open Lead / open Task; mark read / mark all read.
- **Validation**: 64/64 checks (`tests/test_task_notification_16_5a7.py`) driving the full mandated chain. 32/32 templates compile; sidebar + centre render. Migration upgrade/downgrade round-trip clean. Regression: 30/30 adapter suite (16.5A6-J) still passes; app boots; 84 routes.
- **Production Status**: Code only — **not deployed**. Migration `c7a2f19d4e88` is pending deployment approval; no production data written this phase.
- **Not closed**: the 18 already-mis-filed `lead_event` rows need their own approved data repair.
- **Related ADR**: `ADR-021`.

## Phase 16.5A6 (Enterprise Data Backfill — EXECUTED)
- **Objective**: Populate the Enterprise Configuration layer from the legacy `ConversationState` string columns.
- **Executed**: 2026-07-17, LIVE against Railway production. Online — no downtime, no maintenance window, no DDL.
- **Pre-flight**: verified `pg_dump` logical backup (`oxfordcrm_before_backfill.dump`, custom format, all 19 tables, every row count matched production, full-archive decode passed). Both NO-GO gates passed (`orphan_leads=0`, `pipeline_stage_id IS NOT NULL=0`). Migration head `b6e1d4f82c9e`. ADR-020 confirmed deployed to the live instance (commit `9acf6c2`, container boot +33s).
- **Result** (1 tenant, 29 leads, single batch, 84.4s):
  - 1 `legacy_compat` pipeline · 12 canonical stages · 8 Offerings · 25 bridges · 29 `pipeline_stage_id` links · 15 JSON enrichments (2 pre-existing keys preserved).
  - 0 unlinkable, 0 slug collisions, 0 extra stages — matching the audited dry-run plan exactly.
- **Validation**: V1–V11 all pass. **V6 (ADR-018 hard gate): `admitted_total` 7 == 7.** **V8 (ADR-019 hard gate): `internal_key` == legacy `stage` drift = 0.** V10 cross-tenant leak detectors both 0. Stage breakdown, `lead_status` group_by, and admissions count all byte-identical to pre-migration.
- **V11 idempotency**: 2nd LIVE run reported 0 creates / 0 links / 0 enrichments (all reused/skipped). Running the migration again produces an identical database.
- **ADR-018 vindicated in production data**: 6 of 7 admitted leads have `stage <> 'enrolled'` (e.g. lead id=5: `demo_time_ask` + `is_admitted=True`). The original pre-ADR-018 design would have erased **86% of all admissions** on first run.
- **Production Status**: Active. Enterprise layer populated; legacy behaviour unchanged. Public site, CRM login, webhook, scheduler, WhatsApp and AI Router all healthy.
- **Related ADR**: `ADR-018`, `ADR-019`, `ADR-020`.

## Phase 16.5A6-J (Course Adapter Synchronization Correction)
- **Objective**: Fix the blocking defect found by the Phase 16.5A6-LA LIVE Readiness Audit, before any production data was migrated.
- **Trigger**: Phase 16.5A6-LA returned **FAIL**. The `course` adapter returned a **stale** `Offering.name` after backfill: Step 5 opens the adapter gate, the router writes `course` at 4 sites (`router.py:219,396,443,457`), and `_sync_offering_link` was a documented no-op — so the bridge was never repointed. Reproduced deterministically (3 of 4 adapters passed; only `course` failed). Blast radius: the 25 of 29 production leads holding a bridge.
- **Severity**: The corruption materialises **after** migration, on the next bot course-write. Phase 16.5A6 would have reported 100% parity and GO, then silently corrupted course data with no detector watching.
- **Discovery**: `course` has exactly **one** write funnel — `st["course"]=…` → `StateProxy.__setitem__` → `_db_save` → `setattr(row,'course',…)` → the setter. No direct attribute writes, no bulk `.update({})`, no raw SQL, no import/CLI/scheduler/automation path. `admin.py:1319` is a read; `normalize_course_name` is read-time-only.
- **Implementation**:
  - **ADR-020 Course–Offering Synchronization** — `_sync_offering_link` implemented symmetrically with `_sync_stage_link`: no-op while the gate is closed; otherwise repoint the bridge to the Offering matching the exact `(tenant_id, name)`; reuse only (never mint); at most one bridge; remove obsolete/duplicate links.
  - **Deliberate divergence**: on no-match the bridge is **removed** (not left, as `_sync_stage_link` does) so the legacy fallback engages. Justified: the bot can assign any of 10 `ALL_COURSES` entries while only 8 have Offerings — `GST & Payroll` and `DCA Fast Track` are assignable with no Offering.
  - Data Model Freeze corrected v1.2 → **v1.3** (§7): bridge-backed adapters must now declare a **write** contract, not only a read strategy.
  - Runbook §7 gained an explicit note that the validation matrix cannot detect this defect class; Rollback Checklist rationale amended (the bot can now write bridges, but only on linked rows — the DELETE stays correct, and unlink must precede it).
  - `tests/test_adapter_sync_16_5a6j.py` — 30 deterministic mutation checks driving the real `setattr` write path.
- **Result**: All four adapters round-trip under the gate. 30/30 checks pass. Both original 16.5A6-LA reproductions flip from FAIL to PASS unchanged. No production data touched; no schema change; no migration.
- **Production Status**: Active. Phase 16.5A6 LIVE remains **NOT APPROVED** pending a re-run of Phase 16.5A6-LA against this correction.
- **Related ADR**: `ADR-020` (depends on `ADR-019`; supersedes the 16.5A5-I `_sync_offering_link` no-op contract).

## Phase K1.x (Enterprise Knowledge Architecture)
- **Objective**: Make the repository AI-native and autonomous.
- **Implementation**: Generated Constitutional specs, Registries (Capability, Domain, Implementation), Boot Orders, and Manifests.
- **Result**: Repository successfully frozen at Knowledge Baseline v1.0.
- **Production Status**: Active documentation standard.
- **Related ADR**: `ADR-010`, `ADR-012`.

## Required Cross-References
- [Repository Manifest](../REPOSITORY_MANIFEST.md)
- [Master Index](../MASTER_INDEX.md)
- [Knowledge Baseline](../00_meta/KNOWLEDGE_BASELINE_v1.0.md)
- [AI Boot Order](../AI_BOOT_ORDER.md)
- [ADR Index](../21_decision_records/ADR_INDEX.md)
