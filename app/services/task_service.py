"""Phase 16.5A7 — Task engine service (ADR-021).

Owns the Task lifecycle and the notification side effects. Routes parse input
and enforce RBAC; all business logic lives here (CODE_CONVENTIONS §3).

Two invariants this module exists to hold:

  1. Legacy compatibility (ADR-021 dual-write). `FOLLOW_UP_TASK` and
     `FOLLOW_UP_COMPLETED` LeadEvents are still emitted on create and complete,
     because 15 existing readers (staff productivity, activity feed, task
     intelligence, lead detail) reconstruct tasks from them. Those events stay
     an immutable audit trail: edit/delete do NOT rewrite history, which is the
     correct semantic for a feed showing what happened at the time.

  2. Tenant safety (ACTIVE_CONSTRAINTS §2). Every write takes an explicit
     tenant_id. Phase 16.5A7 discovery proved `_get_default_tenant_id()`
     (Tenant.query.first()) resolves to an unrelated tenant in production and
     had already mis-filed 18 lead_event rows, so it is never used here.
"""

import json
import logging
import uuid

from datetime import datetime

from app.extensions import db
from app.models import Notification, Task
from app.services import notification_service

logger = logging.getLogger(__name__)

VALID_PRIORITIES = ("LOW", "NORMAL", "HIGH", "URGENT")
VALID_STATUSES = ("OPEN", "IN_PROGRESS", "COMPLETED")


class TaskError(Exception):
    """Raised when a task operation is invalid or not permitted."""


def _delete_legacy_mirror(tenant_id, task_uid):
    """Remove the mirrored legacy events for one task_uid — Phase 16.5A7-B (B2).

    Tenant-scoped, and matches on the JSON payload's task_id. Never raises: a
    mirror-cleanup failure must not block the delete of the Task itself.
    """
    try:
        from app.models import LeadEvent
        rows = (LeadEvent.query
                .filter(LeadEvent.tenant_id == tenant_id)
                .filter(LeadEvent.event_type.in_(
                    ("FOLLOW_UP_TASK", "FOLLOW_UP_COMPLETED")))
                .all())
        for ev in rows:
            try:
                if json.loads(ev.event_data or "{}").get("task_id") == task_uid:
                    db.session.delete(ev)
            except (ValueError, TypeError):
                continue
    except Exception:
        logger.exception("legacy mirror cleanup failed for task_uid=%s", task_uid)


def _log_legacy_event(tenant_id, phone, event_type, payload):
    """Mirror to the legacy event log (ADR-021 dual-write).

    Skipped for standalone tasks: lead_event.phone is NOT NULL, and the legacy
    readers are lead-scoped, so a task with no lead has nothing to mirror to.
    Never raises — a mirror failure must not lose the Task itself.
    """
    if not phone:
        return
    try:
        from app.services.log_service import log_lead_event
        log_lead_event(tenant_id=tenant_id, phone=phone,
                       event_type=event_type, event_data=json.dumps(payload))
    except Exception:
        logger.exception("legacy %s mirror failed (task kept)", event_type)


def create_task(tenant_id, title, created_by, lead_phone=None, notes=None,
                due_date=None, priority="NORMAL", assigned_staff=None,
                remind_at=None):
    """Admin creates and assigns a task. Returns the Task.

    Side effects: legacy FOLLOW_UP_TASK event + TASK_ASSIGNED notification.
    """
    if not tenant_id:
        raise TaskError("create_task requires an explicit tenant_id")
    title = (title or "").strip()
    if not title:
        raise TaskError("task title is required")

    priority = (priority or "NORMAL").upper()
    if priority not in VALID_PRIORITIES:
        priority = "NORMAL"

    staff = (assigned_staff or "").strip() or None

    task = Task(
        tenant_id=tenant_id,
        task_uid=uuid.uuid4().hex,
        lead_phone=(lead_phone or None),
        title=title[:200],
        notes=(notes or None),
        priority=priority,
        status="OPEN",
        due_date=(due_date or None),
        remind_at=remind_at,
        reminder_sent=False,
        assigned_staff=staff,
        created_by=created_by,
    )
    db.session.add(task)
    db.session.commit()

    # Legacy mirror — keeps every pre-16.5A7 task reader working unchanged.
    _log_legacy_event(tenant_id, task.lead_phone, "FOLLOW_UP_TASK", {
        "task_id": task.task_uid,
        "lead_phone": task.lead_phone,
        "task": task.title,
        "due_date": task.due_date or "",
        "staff": staff or "Unassigned",
        "created_by": created_by,
        **({"notes": task.notes} if task.notes else {}),
    })

    if staff:
        notification_service.notify(
            tenant_id=tenant_id, recipient=staff,
            notif_type=Notification.TYPE_TASK_ASSIGNED,
            title=f"New task: {task.title}",
            body=f"Due {task.due_date}" if task.due_date else None,
            lead_phone=task.lead_phone, task_id=task.id,
        )
    return task


def update_task(tenant_id, task_id, actor, title=None, notes=None,
                due_date=None, priority=None, assigned_staff=None,
                remind_at=None):
    """Admin edits a task. Only supplied fields change.

    Reassignment notifies the new assignee; other edits notify the current one.
    No legacy event is emitted: the event log is an immutable audit trail of
    what was originally created (ADR-021).
    """
    task = _get(tenant_id, task_id)
    if task.status == "COMPLETED":
        raise TaskError("cannot edit a completed task")

    previous_staff = task.assigned_staff

    if title is not None:
        t = title.strip()
        if not t:
            raise TaskError("task title cannot be empty")
        task.title = t[:200]
    if notes is not None:
        task.notes = notes or None
    if due_date is not None:
        task.due_date = due_date or None
    if remind_at is not None:
        task.remind_at = remind_at
        task.reminder_sent = False          # new time -> allow a fresh reminder
    if priority is not None:
        p = priority.upper()
        if p in VALID_PRIORITIES:
            task.priority = p
    if assigned_staff is not None:
        task.assigned_staff = assigned_staff.strip() or None

    db.session.commit()

    new_staff = task.assigned_staff
    if assigned_staff is not None and new_staff != previous_staff:
        if new_staff:
            notification_service.notify(
                tenant_id=tenant_id, recipient=new_staff,
                notif_type=Notification.TYPE_TASK_ASSIGNED,
                title=f"Task reassigned to you: {task.title}",
                body=f"Due {task.due_date}" if task.due_date else None,
                lead_phone=task.lead_phone, task_id=task.id)
        if previous_staff:
            notification_service.notify(
                tenant_id=tenant_id, recipient=previous_staff,
                notif_type=Notification.TYPE_TASK_UPDATED,
                title=f"Task reassigned away: {task.title}",
                body=f"Now assigned to {new_staff or 'Unassigned'}",
                lead_phone=task.lead_phone, task_id=task.id)
    elif new_staff:
        notification_service.notify(
            tenant_id=tenant_id, recipient=new_staff,
            notif_type=Notification.TYPE_TASK_UPDATED,
            title=f"Task updated: {task.title}",
            body=f"Updated by {actor}",
            lead_phone=task.lead_phone, task_id=task.id)
    return task


def staff_update(tenant_id, task_id, actor, status=None, staff_notes=None,
                 is_admin=False):
    """Staff updates progress: status and/or notes. Cannot reassign or retitle.

    Completing via this path routes to complete_task() so the legacy event and
    the TASK_COMPLETED notification always fire.

    Phase 16.5A7-B (B1): a non-admin actor must be the task's assignee.
    """
    task = _get(tenant_id, task_id)
    _authorize_mutation(task, actor, is_admin)

    if status is not None and status.upper() == "COMPLETED":
        return complete_task(tenant_id, task_id, actor,
                             staff_notes=staff_notes, is_admin=is_admin)

    if task.status == "COMPLETED":
        raise TaskError("cannot update a completed task")

    changed = False
    if staff_notes is not None:
        task.staff_notes = staff_notes or None
        changed = True
    if status is not None:
        s = status.upper()
        if s not in VALID_STATUSES:
            raise TaskError(f"invalid status {status!r}")
        task.status = s
        changed = True

    if not changed:
        return task
    db.session.commit()

    # Progress reports upward: notify the admin who created the task.
    if task.created_by and task.created_by != actor:
        notification_service.notify(
            tenant_id=tenant_id, recipient=task.created_by,
            notif_type=Notification.TYPE_TASK_UPDATED,
            title=f"Task progress: {task.title}",
            body=f"{actor} set status to {task.status}",
            lead_phone=task.lead_phone, task_id=task.id)
    return task


def complete_task(tenant_id, task_id, actor, staff_notes=None, is_admin=False):
    """Complete a task. Idempotent: completing twice is a no-op.

    Side effects: legacy FOLLOW_UP_COMPLETED event + TASK_COMPLETED notification
    to the admin who created it.

    Phase 16.5A7-B (B1): a non-admin actor must be the task's assignee.
    `completed_by` is the credit record, so allowing a non-assignee to complete
    let one staff member claim another's work and skewed staff_productivity.
    """
    task = _get(tenant_id, task_id)
    _authorize_mutation(task, actor, is_admin)
    if task.status == "COMPLETED":
        return task                      # idempotent

    if staff_notes is not None:
        task.staff_notes = staff_notes or None
    task.status = "COMPLETED"
    task.completed_by = actor
    task.completed_at = datetime.utcnow()
    db.session.commit()

    _log_legacy_event(tenant_id, task.lead_phone, "FOLLOW_UP_COMPLETED", {
        "task_id": task.task_uid,
        "completed_by": actor,
    })

    if task.created_by and task.created_by != actor:
        notification_service.notify(
            tenant_id=tenant_id, recipient=task.created_by,
            notif_type=Notification.TYPE_TASK_COMPLETED,
            title=f"Task completed: {task.title}",
            body=f"Completed by {actor}",
            lead_phone=task.lead_phone, task_id=task.id)
    return task


def delete_task(tenant_id, task_id, actor):
    """Admin deletes a task.

    Notifications referencing it are detached (task_id -> NULL) rather than
    deleted: no FK uses CASCADE (SCHEMA_RULES §12), and a delivered
    notification is a record of something that genuinely happened.

    Phase 16.5A7-B (B2) — AMENDS ADR-021. The mirrored legacy events for this
    task_uid are now DELETED with the Task row.

    ADR-021 kept them as "audit history". The 16.5A7-A audit proved that choice
    produced a zombie: the unified reader replays any task_uid with no Task row
    as a legacy task, so a deleted task reappeared in every task list, still
    OPEN, and staff kept working it.

    For a 16.5A7 task the legacy events are a MIRROR of the Task row, not
    independent history — the Task table is the System of Record. Leaving the
    mirror behind lets it act as a phantom source of truth, which is exactly the
    ADR-020 failure mode. Pre-16.5A7 tasks are unaffected: they have no Task
    row, so delete_task() can never be invoked on them, and their events remain
    untouched history.
    """
    task = _get(tenant_id, task_id)
    staff = task.assigned_staff
    title = task.title
    uid = task.task_uid

    for n in Notification.query.filter_by(tenant_id=tenant_id,
                                          task_id=task.id).all():
        n.task_id = None

    _delete_legacy_mirror(tenant_id, uid)

    db.session.delete(task)
    db.session.commit()

    if staff:
        notification_service.notify(
            tenant_id=tenant_id, recipient=staff,
            notif_type=Notification.TYPE_TASK_UPDATED,
            title=f"Task cancelled: {title}",
            body=f"Cancelled by {actor}")
    return True


def list_tasks(tenant_id, assigned_staff=None, status=None, lead_phone=None):
    """Tenant-scoped task list, newest first."""
    if not tenant_id:
        return []
    q = Task.query.filter_by(tenant_id=tenant_id)
    if assigned_staff:
        q = q.filter(Task.assigned_staff == assigned_staff)
    if status:
        q = q.filter(Task.status == status)
    if lead_phone:
        q = q.filter(Task.lead_phone == lead_phone)
    return q.order_by(Task.created_at.desc(), Task.id.desc()).all()


def _get(tenant_id, task_id):
    """Tenant-scoped fetch. Never Task.query.get() — that would cross tenants.

    Tenant scoping alone is NOT authorization: it stops tenant A touching
    tenant B, but not staff A touching staff B inside the same tenant. Callers
    that mutate must also pass through _authorize_mutation() (B1).
    """
    if not tenant_id:
        raise TaskError("tenant_id is required")
    task = Task.query.filter_by(id=task_id, tenant_id=tenant_id).first()
    if task is None:
        raise TaskError(f"task {task_id} not found in this tenant")
    return task


class TaskForbidden(TaskError):
    """Raised when the actor may not mutate this task (403, not 400)."""


def authorize_assignee(assignee, actor, is_admin):
    """The single authorization rule for mutating a task — Phase 16.5A7-B/D.

    Admin / Super Admin  -> any task in their tenant.
    Assigned staff       -> only their own task.
    Everyone else        -> TaskForbidden (403).

    Closes the horizontal (staff-to-staff) escalation found by the 16.5A7-A
    audit, where any staff member could re-status, overwrite the notes of, and
    claim completion credit for a colleague's task. Tenant scoping never
    covered this — it only blocks tenant-to-tenant.

    An UNASSIGNED task is admin-only: leaving it open to any staff member would
    reintroduce the same hijack through the back door.

    Phase 16.5A7-D (B1-R): this takes the assignee as a plain string rather than
    a Task, so the legacy completion path — whose assignee lives in a LeadEvent
    JSON payload and has no Task row — reaches the SAME decision instead of a
    parallel reimplementation. 16.5A7-C proved the legacy branch had no
    authorization at all: HTTP 200 and completed_by set to the caller.

    Names are compared normalized. Legacy payloads were written by the
    pre-16.5A7 route, which did NOT normalize `staff`, so a raw-case value must
    still match its owner rather than lock them out of their own task.
    """
    if is_admin:
        return
    clean_assignee = _norm(assignee)
    if not clean_assignee or clean_assignee == "Unassigned":
        raise TaskForbidden(
            "this task is unassigned; only an admin may modify it")
    if clean_assignee != _norm(actor):
        raise TaskForbidden(
            f"task is assigned to {assignee!r}; {actor!r} may not modify it")


def _norm(name):
    """Mirror of admin.normalize_staff_name() — kept here so the service layer
    does not import from the routes layer (CODE_CONVENTIONS §3)."""
    cleaned = (name or "").strip()
    return cleaned.title() if cleaned else ""


def _authorize_mutation(task, actor, is_admin):
    """Authorize a mutation of a Task row. Delegates to authorize_assignee()."""
    authorize_assignee(task.assigned_staff, actor, is_admin)
