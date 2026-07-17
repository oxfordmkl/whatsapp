"""Phase 16.5A7-B — Task Engine completion tests.

Proves the two blocking defects from the Phase 16.5A7-A audit are closed:

  B1  staff-to-staff privilege escalation — any staff could re-status,
      overwrite the notes of, and claim completion credit for a colleague's
      task. Tenant scoping never covered this.
  B2  the Task Engine was write-only — no UI read the Task table, so edits were
      invisible, deletes left zombies, and priority / IN_PROGRESS / staff_notes
      / standalone tasks never rendered.

The 16.5A7 suite passed while both were live because it only tested the happy
path. Every test here asserts a FORBIDDEN path fails, or that the UI reader
actually reflects the Task table.

Isolated in-memory SQLite. No production access. No pytest dependency.

    python tests/test_task_engine_16_5a7b.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("ADMIN_KEY", "test_admin_key_not_a_secret_x9")
os.environ.setdefault("SECRET_KEY", "test_secret_key_not_a_secret_x9")
os.environ.setdefault("BROADCAST_API_KEY", "test_broadcast_key_not_a_secret_x9")
os.environ.setdefault("WABA_ENCRYPTION_KEY",
                      "FZsAc8GY_ayHq0cAxKXMMlUvSbJO2hKhpZOdGnaxO18=")
os.environ.setdefault("GEMINI_API_KEY", "placeholder")

import json                                                   # noqa: E402
import uuid                                                   # noqa: E402

from flask import Flask                                       # noqa: E402
from app.extensions import db                                 # noqa: E402
from app.models import (ConversationState, LeadEvent,         # noqa: E402
                        Notification, Task, Tenant)

# A minimal app, not create_app(): the factory starts the follow-up scheduler
# thread, which races the in-memory SQLite and drops the schema out from under
# the test. get_all_tasks() is a plain function and tenant_query() falls back to
# its explicit tenant_id argument outside a request context, so the blueprint is
# not needed here.

_results = []
T1, T2 = "tenant-one", "tenant-two"
ADMIN, KIRAN, BIBIN = "Admin One", "Kiran", "Bibin"


def check(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}]  {name}")
    if not cond and detail:
        print(f"         {detail}")
    return bool(cond)


def main():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["TESTING"] = True
    db.init_app(app)

    with app.app_context():
        db.create_all()
        from app.routes.admin import get_all_tasks
        from app.services import task_service as ts
        from app.services import notification_service as ns

        db.session.add_all([Tenant(id=T1, name="One", slug="one"),
                            Tenant(id=T2, name="Two", slug="two")])
        db.session.add(ConversationState(phone="911", tenant_id=T1, name="Asha",
                                         stage="new", course="PGDCA"))
        db.session.commit()

        def ui(tenant=T1):
            """What every task UI renders (dashboard KPIs, /crm/tasks/my,
            /crm/tasks/admin, staff performance all call this)."""
            o, c = get_all_tasks(tenant)
            return {t["task_id"]: t for t in o}, {t["task_id"]: t for t in c}

        # ══════════════════════════════════════════════════════════════
        print("=" * 72)
        print("B1 — STAFF CANNOT TOUCH ANOTHER STAFF MEMBER'S TASK")
        print("=" * 72)
        t = ts.create_task(tenant_id=T1, title="Kiran task", created_by=ADMIN,
                           lead_phone="911", assigned_staff=KIRAN)

        try:
            ts.staff_update(T1, t.id, actor=BIBIN, status="IN_PROGRESS",
                            staff_notes="Bibin was here", is_admin=False)
            check("Staff A cannot modify Staff B's task", False,
                  "Bibin mutated Kiran's task")
        except ts.TaskForbidden:
            fresh = db.session.get(Task, t.id)
            check("Staff A cannot modify Staff B's task",
                  fresh.status == "OPEN" and not fresh.staff_notes)

        try:
            ts.complete_task(T1, t.id, actor=BIBIN, is_admin=False)
            check("Staff A cannot complete Staff B's task", False,
                  "Bibin completed Kiran's task")
        except ts.TaskForbidden:
            fresh = db.session.get(Task, t.id)
            check("Staff A cannot complete Staff B's task",
                  fresh.status != "COMPLETED" and fresh.completed_by is None)

        check("credit cannot be stolen (completed_by unset)",
              db.session.get(Task, t.id).completed_by is None)

        # Assignee CAN
        ts.staff_update(T1, t.id, actor=KIRAN, status="IN_PROGRESS",
                        staff_notes="Left voicemail", is_admin=False)
        check("assignee CAN update their own task",
              db.session.get(Task, t.id).status == "IN_PROGRESS")

        # Admin CAN
        ts.staff_update(T1, t.id, actor=ADMIN, status="OPEN", is_admin=True)
        check("Admin CAN update any task in the tenant",
              db.session.get(Task, t.id).status == "OPEN")
        ts.complete_task(T1, t.id, actor=ADMIN, is_admin=True)
        check("Admin CAN complete any task in the tenant",
              db.session.get(Task, t.id).status == "COMPLETED")

        # Unassigned task is admin-only (no back-door hijack)
        u = ts.create_task(tenant_id=T1, title="Unassigned", created_by=ADMIN,
                           lead_phone="911")
        try:
            ts.staff_update(T1, u.id, actor=BIBIN, status="IN_PROGRESS",
                            is_admin=False)
            check("unassigned task is not staff-grabbable", False,
                  "Bibin took an unassigned task")
        except ts.TaskForbidden:
            check("unassigned task is not staff-grabbable", True)
        ts.staff_update(T1, u.id, actor=ADMIN, status="IN_PROGRESS",
                        is_admin=True)
        check("admin CAN work an unassigned task",
              db.session.get(Task, u.id).status == "IN_PROGRESS")

        # ══════════════════════════════════════════════════════════════
        print()
        print("=" * 72)
        print("B2 — TASK TABLE IS THE SYSTEM OF RECORD")
        print("=" * 72)

        # Legacy event-only task (pre-16.5A7): no Task row.
        legacy_uid = uuid.uuid4().hex
        db.session.add(LeadEvent(
            phone="911", tenant_id=T1, event_type="FOLLOW_UP_TASK",
            event_data=json.dumps({"task_id": legacy_uid, "task": "Legacy task",
                                   "due_date": "2026-07-25", "staff": KIRAN,
                                   "created_by": "Old Admin"})))
        db.session.commit()

        openm, _ = ui()
        check("legacy event-only task still appears (compat layer)",
              legacy_uid in openm, f"keys={list(openm)[:4]}")
        check("legacy task flagged is_legacy",
              openm.get(legacy_uid, {}).get("is_legacy") is True)
        check("legacy task has no Task PK",
              openm.get(legacy_uid, {}).get("id") is None)

        # New task appears
        n = ts.create_task(tenant_id=T1, title="Call Asha", created_by=ADMIN,
                           lead_phone="911", due_date="2026-07-20",
                           priority="NORMAL", assigned_staff=KIRAN,
                           notes="Discuss fees")
        openm, _ = ui()
        check("new Task-table task appears", n.task_uid in openm)
        check("new task carries its Task PK",
              openm[n.task_uid]["id"] == n.id)
        check("new task NOT flagged legacy",
              openm[n.task_uid]["is_legacy"] is False)

        # EDIT immediately visible
        ts.update_task(T1, n.id, actor=ADMIN, title="URGENT: Call Asha NOW",
                       priority="URGENT", due_date="2026-07-18")
        openm, _ = ui()
        check("edit is immediately visible in the UI",
              openm[n.task_uid]["task"] == "URGENT: Call Asha NOW",
              f'UI shows {openm[n.task_uid]["task"]!r}')
        check("edited due_date visible",
              openm[n.task_uid]["due_date"] == "2026-07-18")
        check("priority visible", openm[n.task_uid]["priority"] == "URGENT")
        check("admin notes visible", openm[n.task_uid]["notes"] == "Discuss fees")

        # IN_PROGRESS + staff notes visible
        ts.staff_update(T1, n.id, actor=KIRAN, status="IN_PROGRESS",
                        staff_notes="Rang, will call back", is_admin=False)
        openm, _ = ui()
        check("IN_PROGRESS visible via task_status",
              openm[n.task_uid]["task_status"] == "IN_PROGRESS")
        check("IN_PROGRESS still counted OPEN (legacy contract preserved)",
              openm[n.task_uid]["status"] == "OPEN")
        check("staff_notes visible",
              openm[n.task_uid]["staff_notes"] == "Rang, will call back")

        # Standalone task visible
        s = ts.create_task(tenant_id=T1, title="Team meeting", created_by=ADMIN,
                           assigned_staff=KIRAN)
        openm, _ = ui()
        check("standalone task (no lead) is visible", s.task_uid in openm)
        check("standalone task has no phone",
              openm.get(s.task_uid, {}).get("phone") is None)

        # DELETE immediately disappears — the zombie check
        d = ts.create_task(tenant_id=T1, title="Delete me", created_by=ADMIN,
                           lead_phone="911", assigned_staff=KIRAN)
        d_uid = d.task_uid
        check("precondition: task visible before delete", d_uid in ui()[0])
        ts.delete_task(T1, d.id, ADMIN)
        openm, closedm = ui()
        check("deleted task disappears from the UI (no zombie)",
              d_uid not in openm and d_uid not in closedm,
              "ZOMBIE: deleted task still rendered")
        check("legacy mirror removed with the Task row",
              not any(json.loads(e.event_data or "{}").get("task_id") == d_uid
                      for e in LeadEvent.query.all()))
        check("deleting one task does not remove another's mirror",
              any(json.loads(e.event_data or "{}").get("task_id") == n.task_uid
                  for e in LeadEvent.query.all()))
        check("pre-16.5A7 legacy task untouched by deletes",
              legacy_uid in ui()[0])

        # Completion moves it to the completed bucket
        ts.complete_task(T1, n.id, actor=KIRAN, is_admin=False)
        openm, closedm = ui()
        check("completed task leaves the open list", n.task_uid not in openm)
        check("completed task enters the completed list", n.task_uid in closedm)
        check("completed_by recorded as the assignee",
              closedm[n.task_uid]["completed_by"] == KIRAN)

        # Legacy completion still replays
        db.session.add(LeadEvent(
            phone="911", tenant_id=T1, event_type="FOLLOW_UP_COMPLETED",
            event_data=json.dumps({"task_id": legacy_uid,
                                   "completed_by": KIRAN})))
        db.session.commit()
        openm, closedm = ui()
        check("legacy completion still replays", legacy_uid in closedm)
        check("legacy task left the open list", legacy_uid not in openm)

        # ══════════════════════════════════════════════════════════════
        print()
        print("=" * 72)
        print("REGRESSION — TENANT ISOLATION / NOTIFICATIONS")
        print("=" * 72)
        t2 = ts.create_task(tenant_id=T2, title="T2 task", created_by="T2 Admin",
                            assigned_staff=KIRAN)
        o1, c1 = ui(T1)
        check("T2 task invisible to T1 reader",
              t2.task_uid not in o1 and t2.task_uid not in c1)
        o2, _ = ui(T2)
        check("T1 tasks invisible to T2 reader",
              n.task_uid not in o2 and legacy_uid not in o2)
        try:
            ts.staff_update(T2, n.id, actor=ADMIN, status="OPEN", is_admin=True)
            check("cross-tenant mutation blocked", False, "T2 mutated a T1 task")
        except ts.TaskError:
            check("cross-tenant mutation blocked", True)

        check("notifications still delivered on assignment",
              ns.unread_count(T1, KIRAN) > 0)
        before = ns.unread_count(T1, KIRAN)
        rec = ns.recent(T1, KIRAN, limit=1)
        ns.mark_read(T1, KIRAN, rec[0].id)
        check("mark_read still decrements badge",
              ns.unread_count(T1, KIRAN) == before - 1)
        check("mark_all_read still works",
              ns.mark_all_read(T1, KIRAN) >= 0 and
              ns.unread_count(T1, KIRAN) == 0)
        check("TASK_COMPLETED notification still fires",
              any(x.notif_type == "TASK_COMPLETED"
                  for x in ns.recent(T1, ADMIN, limit=50)))
        check("cross-tenant notification isolation holds",
              ns.unread_count(T2, KIRAN) == 1)

        # Lead-assignment notification path (unchanged by B1/B2)
        ns.notify(T1, KIRAN, Notification.TYPE_NEW_LEAD_ASSIGNED, "Lead x",
                  lead_phone="911")
        check("lead assignment notification unaffected",
              ns.unread_count(T1, KIRAN) == 1)

    print()
    print("=" * 72)
    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    failed = [n for n, ok in _results if not ok]
    print(f"RESULT: {passed}/{total} checks passed")
    if failed:
        print()
        for f in failed:
            print(f"  FAILED: {f}")
        return 1
    print("ALL TASK ENGINE (16.5A7-B) TESTS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
