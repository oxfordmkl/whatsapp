"""Phase 16.5A7 — Task & Notification foundation tests (ADR-021).

Drives the exact validation chain the phase mandates:

    Admin creates task -> Staff receives notification -> badge increments
    -> notification opens Lead -> task visible -> Staff updates
    -> Admin sees update -> task complete -> notification generated

Plus the guarantees that make it safe: tenant isolation, RBAC ownership,
legacy dual-write compatibility, and read-state correctness.

Isolated in-memory SQLite. No production access. No pytest dependency.

    python tests/test_task_notification_16_5a7.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("ADMIN_KEY", "test_admin_key_not_a_secret_x9")
os.environ.setdefault("SECRET_KEY", "test_secret_key_not_a_secret_x9")
os.environ.setdefault("BROADCAST_API_KEY", "test_broadcast_key_not_a_secret_x9")
os.environ.setdefault("WABA_ENCRYPTION_KEY",
                      "FZsAc8GY_ayHq0cAxKXMMlUvSbJO2hKhpZOdGnaxO18=")

from flask import Flask                                       # noqa: E402
from app.extensions import db                                 # noqa: E402
from app.models import (ConversationState, LeadEvent,         # noqa: E402
                        Notification, Task, Tenant)

_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}]  {name}")
    if not cond and detail:
        print(f"         {detail}")
    return bool(cond)


def make_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


T1 = "tenant-one"
T2 = "tenant-two"
ADMIN = "Admin One"
STAFF = "Kiran"
OTHER = "Bibin"


def main():
    app = make_app()
    with app.app_context():
        db.create_all()
        from app.services import notification_service as ns
        from app.services import task_service as ts

        db.session.add_all([
            Tenant(id=T1, name="One", slug="one"),
            Tenant(id=T2, name="Two", slug="two"),
        ])
        db.session.add(ConversationState(phone="911", tenant_id=T1,
                                         name="Asha", stage="new", course="PGDCA"))
        db.session.add(ConversationState(phone="922", tenant_id=T2,
                                         name="Other", stage="new", course="PGDCA"))
        db.session.commit()

        print("=" * 72)
        print("STEP 1 — ADMIN CREATES TASK")
        print("=" * 72)
        task = ts.create_task(tenant_id=T1, title="Call Asha about PGDCA",
                              created_by=ADMIN, lead_phone="911",
                              notes="Discuss fees", due_date="2026-07-20",
                              priority="HIGH", assigned_staff=STAFF)
        check("task persisted", task.id is not None)
        check("task owned by creating admin", task.created_by == ADMIN)
        check("task assigned to staff", task.assigned_staff == STAFF)
        check("priority stored", task.priority == "HIGH")
        check("status defaults OPEN", task.status == "OPEN")
        check("task is tenant-scoped", task.tenant_id == T1)

        print()
        print("=" * 72)
        print("STEP 2 — STAFF RECEIVES NOTIFICATION")
        print("=" * 72)
        notifs = ns.recent(T1, STAFF)
        check("staff has exactly 1 notification", len(notifs) == 1,
              f"got {len(notifs)}")
        n = notifs[0]
        check("type is TASK_ASSIGNED", n.notif_type == "TASK_ASSIGNED")
        check("notification unread by default", n.is_read is False)
        check("links to the lead (click -> open Lead)", n.lead_phone == "911")
        check("links to the task (click -> open Task)", n.task_id == task.id)

        print()
        print("=" * 72)
        print("STEP 3 — DASHBOARD BADGE INCREMENTS")
        print("=" * 72)
        check("staff unread count == 1", ns.unread_count(T1, STAFF) == 1)
        check("admin badge NOT incremented by own action",
              ns.unread_count(T1, ADMIN) == 0)

        print()
        print("=" * 72)
        print("STEP 4 — TASK VISIBLE TO STAFF")
        print("=" * 72)
        mine = ts.list_tasks(T1, assigned_staff=STAFF)
        check("task appears in staff task list", len(mine) == 1)
        check("other staff sees nothing", len(ts.list_tasks(T1, assigned_staff=OTHER)) == 0)

        print()
        print("=" * 72)
        print("STEP 5 — STAFF UPDATES -> ADMIN SEES UPDATE")
        print("=" * 72)
        ts.staff_update(T1, task.id, actor=STAFF, status="IN_PROGRESS",
                        staff_notes="Called, asked to ring back")
        fresh = Task.query.get(task.id)
        check("status updated", fresh.status == "IN_PROGRESS")
        check("staff notes saved", "ring back" in (fresh.staff_notes or ""))
        admin_notifs = ns.recent(T1, ADMIN)
        check("admin notified of progress", len(admin_notifs) == 1)
        check("progress type is TASK_UPDATED",
              admin_notifs and admin_notifs[0].notif_type == "TASK_UPDATED")
        check("admin badge == 1", ns.unread_count(T1, ADMIN) == 1)

        print()
        print("=" * 72)
        print("STEP 6 — TASK COMPLETE -> NOTIFICATION GENERATED")
        print("=" * 72)
        ts.complete_task(T1, task.id, actor=STAFF)
        fresh = Task.query.get(task.id)
        check("status COMPLETED", fresh.status == "COMPLETED")
        check("completed_by recorded", fresh.completed_by == STAFF)
        check("completed_at set", fresh.completed_at is not None)
        done = [x for x in ns.recent(T1, ADMIN) if x.notif_type == "TASK_COMPLETED"]
        check("admin notified of completion", len(done) == 1)

        print()
        print("=" * 72)
        print("STEP 7 — LEGACY DUAL-WRITE (ADR-021 back-compat)")
        print("=" * 72)
        created = LeadEvent.query.filter_by(event_type="FOLLOW_UP_TASK").all()
        completed = LeadEvent.query.filter_by(event_type="FOLLOW_UP_COMPLETED").all()
        check("legacy FOLLOW_UP_TASK event emitted", len(created) == 1)
        check("legacy FOLLOW_UP_COMPLETED event emitted", len(completed) == 1)
        if created:
            p = json.loads(created[0].event_data)
            check("legacy payload carries task_id == task_uid",
                  p.get("task_id") == task.task_uid)
            check("legacy payload carries staff", p.get("staff") == STAFF)
            check("legacy payload carries due_date", p.get("due_date") == "2026-07-20")
            check("legacy event tenant == actor tenant (NOT default tenant)",
                  created[0].tenant_id == T1, f"got {created[0].tenant_id}")

        print()
        print("=" * 72)
        print("STEP 8 — READ STATE")
        print("=" * 72)
        n1 = ns.recent(T1, STAFF)[0]
        check("mark_read works", ns.mark_read(T1, STAFF, n1.id) is True)
        check("badge decrements", ns.unread_count(T1, STAFF) == 0)
        check("read_at stamped", Notification.query.get(n1.id).read_at is not None)
        ns.notify(T1, STAFF, Notification.TYPE_SYSTEM_ALERT, "A")
        ns.notify(T1, STAFF, Notification.TYPE_SYSTEM_ALERT, "B")
        check("badge back to 2", ns.unread_count(T1, STAFF) == 2)
        check("mark_all_read returns 2", ns.mark_all_read(T1, STAFF) == 2)
        check("badge cleared", ns.unread_count(T1, STAFF) == 0)
        check("mark_read is idempotent", ns.mark_read(T1, STAFF, n1.id) is True)

        print()
        print("=" * 72)
        print("STEP 9 — TENANT ISOLATION")
        print("=" * 72)
        t2task = ts.create_task(tenant_id=T2, title="T2 task", created_by="T2 Admin",
                                lead_phone="922", assigned_staff=STAFF)
        check("T2 staff notification not visible to T1",
              len(ns.recent(T1, STAFF, limit=100)) == 3,
              f"T1 sees {len(ns.recent(T1, STAFF, limit=100))}")
        check("T1 badge unaffected by T2 activity", ns.unread_count(T1, STAFF) == 0)
        check("T2 badge is 1", ns.unread_count(T2, STAFF) == 1)
        check("T1 task list excludes T2 tasks",
              all(t.tenant_id == T1 for t in ts.list_tasks(T1)))
        # cross-tenant fetch must fail
        try:
            ts._get(T1, t2task.id)
            check("cross-tenant task fetch blocked", False, "T1 read a T2 task!")
        except ts.TaskError:
            check("cross-tenant task fetch blocked", True)
        # cross-tenant mark_read must fail
        t2n = ns.recent(T2, STAFF)[0]
        check("cross-tenant mark_read blocked",
              ns.mark_read(T1, STAFF, t2n.id) is False)
        check("cross-recipient mark_read blocked",
              ns.mark_read(T2, OTHER, t2n.id) is False)

        print()
        print("=" * 72)
        print("STEP 10 — GUARDRAILS")
        print("=" * 72)
        try:
            ns.notify(None, STAFF, Notification.TYPE_SYSTEM_ALERT, "x")
            check("notify refuses missing tenant_id", False, "it did not raise")
        except ns.NotificationError:
            check("notify refuses missing tenant_id (no default-tenant guess)", True)
        try:
            ns.notify(T1, STAFF, "BOGUS_TYPE", "x")
            check("notify rejects unknown type", False, "it did not raise")
        except ns.NotificationError:
            check("notify rejects unknown type", True)
        check("notify suppresses empty recipient",
              ns.notify(T1, "", Notification.TYPE_SYSTEM_ALERT, "x") is None)
        check("notify suppresses 'Unassigned'",
              ns.notify(T1, "Unassigned", Notification.TYPE_SYSTEM_ALERT, "x") is None)
        try:
            ts.create_task(tenant_id=T1, title="   ", created_by=ADMIN)
            check("empty title rejected", False, "it did not raise")
        except ts.TaskError:
            check("empty title rejected", True)
        check("completing twice is idempotent",
              ts.complete_task(T1, task.id, STAFF).status == "COMPLETED")
        try:
            ts.update_task(T1, task.id, actor=ADMIN, title="edit me")
            check("editing a completed task rejected", False, "it did not raise")
        except ts.TaskError:
            check("editing a completed task rejected", True)

        print()
        print("=" * 72)
        print("STEP 11 — ADMIN EDIT / REASSIGN / DELETE")
        print("=" * 72)
        t = ts.create_task(tenant_id=T1, title="Original", created_by=ADMIN,
                           lead_phone="911", assigned_staff=STAFF)
        ns.mark_all_read(T1, STAFF)
        ts.update_task(T1, t.id, actor=ADMIN, title="Edited", priority="URGENT")
        t = Task.query.get(t.id)
        check("edit changes title", t.title == "Edited")
        check("edit changes priority", t.priority == "URGENT")
        check("assignee notified of edit", ns.unread_count(T1, STAFF) == 1)

        ns.mark_all_read(T1, STAFF)
        ts.update_task(T1, t.id, actor=ADMIN, assigned_staff=OTHER)
        t = Task.query.get(t.id)
        check("reassign changes assignee", t.assigned_staff == OTHER)
        check("new assignee notified", ns.unread_count(T1, OTHER) == 1)
        check("previous assignee notified of handover",
              ns.unread_count(T1, STAFF) == 1)

        tid = t.id
        ts.delete_task(T1, tid, ADMIN)
        check("task deleted", Task.query.get(tid) is None)
        orphan = Notification.query.filter_by(task_id=tid).count()
        check("notifications detached, not cascaded (task_id -> NULL)", orphan == 0)
        check("delete notified assignee",
              any(x.title.startswith("Task cancelled")
                  for x in ns.recent(T1, OTHER, limit=50)))
        check("legacy FOLLOW_UP_TASK audit trail survives delete",
              LeadEvent.query.filter_by(event_type="FOLLOW_UP_TASK").count() >= 2)

        print()
        print("=" * 72)
        print("STEP 12 — STANDALONE TASK (no lead)")
        print("=" * 72)
        # Measure the DELTA: a leadless task must emit no legacy event, because
        # lead_event.phone is NOT NULL and the legacy readers are lead-scoped.
        before = LeadEvent.query.filter_by(event_type="FOLLOW_UP_TASK").count()
        s = ts.create_task(tenant_id=T1, title="Team meeting", created_by=ADMIN,
                           assigned_staff=STAFF)
        after = LeadEvent.query.filter_by(event_type="FOLLOW_UP_TASK").count()
        check("standalone task created", s.id is not None and s.lead_phone is None)
        check("no legacy event for leadless task (lead_event.phone is NOT NULL)",
              after == before, f"emitted {after - before} event(s)")
        check("standalone task still notifies its assignee",
              any(x.task_id == s.id for x in ns.recent(T1, STAFF, limit=50)))

    print()
    print("=" * 72)
    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    failed = [n for n, ok in _results if not ok]
    print(f"RESULT: {passed}/{total} checks passed")
    if failed:
        print()
        for n in failed:
            print(f"  FAILED: {n}")
        return 1
    print("ALL TASK & NOTIFICATION TESTS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
