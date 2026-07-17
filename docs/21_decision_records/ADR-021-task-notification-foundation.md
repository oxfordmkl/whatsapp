---
KnowledgeID: DOC-ADR-021
Version: 1.0
Status: ACTIVE
Owner: Architecture Team
Dependencies: [DOC-META-BASELINE, DOC-ADR-018, DOC-ADR-019, DOC-ADR-020]
---

# ADR-021: Task & Notification Foundation

## Status
ACTIVE — Ratified Phase 16.5A7 (2026-07-17)

## Context

Phase 16.5A7 establishes the operational workflow between Admin and Staff:
Admin-owned tasks, and in-app notifications for lead/task events. Discovery
found three facts that shaped every decision below.

### 1. Tasks were event-sourced, not a table

There was no `Task` model. A task was a `LeadEvent` row with
`event_type='FOLLOW_UP_TASK'` and a JSON payload (`task_id`, `task`, `due_date`,
`staff`, `created_by`); completion was a separate `FOLLOW_UP_COMPLETED` event.
Task state was reconstructed by replaying the event log (`get_all_tasks()`,
`admin.py:3888`).

That model **cannot** satisfy the phase requirements: priority, edit and delete
are impossible against an append-only log. But it is read by **15 sites** —
staff productivity, the activity feed, task intelligence, lead detail — so it
cannot simply be replaced.

### 2. There was no notification system at all

Greenfield. Zero prior art: no model, no service, no unread state.

### 3. `crm_tasks_create` had no admin RBAC, and wrote to the wrong tenant

The route called only `check_auth()`, so **any authenticated STAFF member could
create tasks** — directly contrary to the required ownership model. The
`admin_required` decorator already existed (`admin.py:60`) and simply was not
applied.

Worse, the write used `_get_default_tenant_id()`, which is
`Tenant.query.first()` — documented as *"safe while only one tenant exists"*.
**Production has 10 tenants.** It resolves to `amboori`, while every lead lives
in `oxford-computers`:

```
FOLLOW_UP_TASK       'amboori'   2   <-- invisible to the CRM that created them
FOLLOW_UP_COMPLETED  'amboori'   4   <-- those tasks appear OPEN forever
LEAD_REASSIGNED      'amboori'   7
MANUAL_MESSAGE       'amboori'   5
```

18 production `lead_event` rows are mis-filed. Reads use
`tenant_query(...)` → `current_user.tenant_id`; writes used
`Tenant.query.first()`. That asymmetry silently voids task data **and** leaks
one tenant's lead PII into another tenant's scope — ACTIVE_CONSTRAINTS §2
("fatal security vulnerability").

## Decision

### D1 — `Task` is the system of record; legacy events are dual-written

A first-class `tasks` table owns the Task Engine. On **create** and **complete**
the service also emits the legacy `FOLLOW_UP_TASK` / `FOLLOW_UP_COMPLETED`
events, so all 15 existing readers keep working byte-for-byte unchanged.

This is the repository's established pattern (ACTIVE_CONSTRAINTS §4, Dual-Write
Policy), applied here to bridge a legacy event log rather than a legacy column.

**Edit and delete do NOT rewrite the event log.** The log is an immutable audit
trail; an activity feed entry reading *"Admin created task: Call Asha"* remains
true even after the task is retitled. Rewriting history to match current state
would be the defect, not the fix. This deliberately avoids the ADR-020 trap: the
legacy events are **not** a read path for the Task Engine, so they cannot go
stale relative to it.

### D2 — Ownership is enforced at the route, not the model

`@admin_required` gates create / edit / delete. Staff may view, set status, add
notes, and complete. Staff **cannot** create, reassign, retitle, or delete.
`staff_update()` structurally cannot reassign — it accepts only `status` and
`staff_notes`.

### D3 — `notify()` refuses to guess a tenant

Every service function takes an explicit `tenant_id` and **raises** when it is
missing, rather than falling back to a default. Given finding (3), a silent
fallback is precisely how the 18 mis-filed rows happened. Routes resolve the
tenant via `_actor_tenant_id()` (`current_user.tenant_id`) and refuse to write
without it. `_get_default_tenant_id()` is never used on any 16.5A7 path.

The two legacy write sites this phase touches — `crm_tasks_create` and the
`LEAD_REASSIGNED` audit in `crm_lead_update` — are corrected to the actor's
tenant.

### D4 — `recipient` is a normalized staff name, not a User FK

Staff identity is already expressed as a normalized display name across the CRM
(`ConversationState.assigned_staff`, task `staff`, the staff registry). A `User`
FK would not match how leads and tasks are actually assigned today, and would
require reconciling a name-based assignment model with an id-based one — a CRM
redesign, which this phase forbids.

**Consequence, accepted:** notification routing is only as reliable as staff-name
normalization. `normalize_staff_name()` (`.strip().title()`) is the single
funnel, so `kiran`, `KIRAN`, ` Kiran ` all resolve to `Kiran`. A staff **rename**
would orphan prior notifications. Logged as technical debt; a future phase may
introduce a stable staff identity.

### D5 — Notifications are detached on task delete, never cascaded

`notifications.task_id` is nullable with **no** `ondelete=CASCADE`
(SCHEMA_RULES §12 forbids it). `delete_task()` nulls the FK at the application
layer. A delivered notification records something that genuinely happened and
survives the deletion of its subject.

### D6 — Delivery is in-app only

No email or WhatsApp fan-out. Out of scope, and it would couple the notification
layer to the Meta 24-hour window and the Brevo provider.

## Consequences

- Task Engine gains priority, due date, reminder field, edit and delete —
  impossible under the event-sourced model.
- All 15 legacy task readers keep working with **zero** changes.
- The pre-16.5A7 event-sourced tasks (8 in production) have no `Task` row.
  `crm_tasks_complete` bridges this: it looks up `Task.task_uid` first and falls
  back to the legacy event path, so old tasks still complete.
- **Two live defects are closed:** staff can no longer create tasks, and task /
  reassignment writes now land in the actor's tenant.
- **Not closed by this phase:** the 18 already-mis-filed `lead_event` rows. That
  is a data repair requiring its own approved migration; this phase writes no
  production data. The 2 orphaned `FOLLOW_UP_TASK` and 4 orphaned
  `FOLLOW_UP_COMPLETED` rows remain invisible until then.
- `tasks` and `notifications` are new tables. The Phase 16.5A6 Enterprise Data
  Layer is untouched.
- `remind_at` / `reminder_sent` are stored but **no dispatcher is wired**.
  `REMINDER_DUE` is a supported type with no producer yet; a scheduler hook is a
  separate phase. Storing the field now avoids a later migration.

## References
- Phase 16.5A7 Discovery (task event-sourcing; RBAC gap; tenant misassignment)
- ACTIVE_CONSTRAINTS §2 (Tenant Isolation), §4 (Dual-Write Policy)
- CODE_CONVENTIONS §2 (tenant filtering), §3 (logic out of routes), §9 (downgrade)
- ADR-020 (Course–Offering Synchronization — the staleness trap avoided here)
- `app/models.py` (`Task`, `Notification`), `app/services/task_service.py`,
  `app/services/notification_service.py`
- `tests/test_task_notification_16_5a7.py` (64 checks)
