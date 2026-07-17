"""Phase 16.5A7-D — ROUTE-LEVEL tests for legacy completion authorization (B1-R).

Drives the real HTTP endpoint with a logged-in session. Service-level tests
cannot prove route-level authorization — that is exactly how B1-R survived
Phase 16.5A7-B: its 42 service tests were green while the route's legacy branch
had no authorization at all (16.5A7-C proved HTTP 200 + credit theft).

Covers both completion paths so they cannot drift apart again:
  * Task path   (16.5A7 task, Task row exists)
  * Legacy path (pre-16.5A7 task, event only, no Task row)

File-based SQLite: create_app() starts the follow-up scheduler thread, which
races an in-memory DB. No production access. No pytest dependency.

    python tests/test_legacy_completion_auth_16_5a7d.py
"""
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "_test_16_5a7d.db")
if os.path.exists(_DB):
    os.remove(_DB)

os.environ["DATABASE_URL"] = "sqlite:///" + _DB.replace("\\", "/")
os.environ.setdefault("ADMIN_KEY", "test_admin_key_not_a_secret_x9")
os.environ.setdefault("SECRET_KEY", "test_secret_key_not_a_secret_x9")
os.environ.setdefault("BROADCAST_API_KEY", "test_broadcast_not_a_secret_x9")
os.environ.setdefault("WABA_ENCRYPTION_KEY",
                      "FZsAc8GY_ayHq0cAxKXMMlUvSbJO2hKhpZOdGnaxO18=")
os.environ.setdefault("GEMINI_API_KEY", "placeholder")
os.environ["AUTH_MODE"] = "SESSION_ONLY"

from werkzeug.security import generate_password_hash          # noqa: E402

from app import create_app                                     # noqa: E402
from app.extensions import db                                  # noqa: E402
from app.models import (ConversationState, LeadEvent,          # noqa: E402
                        Notification, Task, Tenant, User)

_results = []
T = "tenant-one"


def check(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}]  {name}")
    if not cond and detail:
        print(f"         {detail}")
    return bool(cond)


def seed_legacy(phone, staff, title="Legacy task"):
    """A pre-16.5A7 task: FOLLOW_UP_TASK event only, no Task row."""
    uid = uuid.uuid4().hex
    db.session.add(LeadEvent(
        phone=phone, tenant_id=T, event_type="FOLLOW_UP_TASK",
        event_data=json.dumps({"task_id": uid, "task": title,
                               "due_date": "2026-07-25", "staff": staff,
                               "created_by": "Old Admin"})))
    db.session.commit()
    return uid


def completed_payloads():
    out = []
    for e in LeadEvent.query.filter_by(event_type="FOLLOW_UP_COMPLETED").all():
        try:
            out.append(json.loads(e.event_data or "{}"))
        except ValueError:
            pass
    return out


def is_completed(uid):
    return [p for p in completed_payloads() if p.get("task_id") == uid]


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        db.session.add(Tenant(id=T, name="One", slug="one"))
        db.session.flush()
        for uname, role in [("Kiran", "STAFF"), ("Bibin", "STAFF"),
                            ("admin1", "ADMIN")]:
            db.session.add(User(username=uname, email=f"{uname}@x.com",
                                password_hash=generate_password_hash("x"),
                                role=role, tenant_id=T, is_active=True))
        db.session.add(ConversationState(phone="911", tenant_id=T, name="Asha",
                                         stage="new", course="PGDCA"))
        db.session.commit()
        uids = {u.username: u.id for u in User.query.all()}

    def login(c, username):
        with c.session_transaction() as s:
            s["_user_id"] = str(uids[username])
            s["_fresh"] = True

    def post_complete(username, task_id, phone="911"):
        with app.test_client() as c:
            login(c, username)
            return c.post("/crm/tasks/complete",
                          json={"task_id": task_id, "phone": phone})

    print("=" * 72)
    print("LEGACY PATH — the B1-R defect (no Task row)")
    print("=" * 72)
    with app.app_context():
        uid = seed_legacy("911", "Kiran")
    r = post_complete("Bibin", uid)
    with app.app_context():
        stolen = is_completed(uid)
    check("other staff CANNOT complete a legacy task -> 403",
          r.status_code == 403 and not stolen,
          f"HTTP {r.status_code}; completed_by="
          f"{stolen[0].get('completed_by') if stolen else None!r}")
    check("no completion event written on denial", not stolen)

    r = post_complete("Kiran", uid)
    with app.app_context():
        done = is_completed(uid)
    check("assigned staff CAN complete their legacy task -> 200",
          r.status_code == 200 and bool(done), f"HTTP {r.status_code}")
    check("credit recorded to the assignee, not the caller",
          done and done[0].get("completed_by") == "Kiran",
          f"completed_by={done[0].get('completed_by')!r}" if done else "none")

    with app.app_context():
        uid2 = seed_legacy("911", "Kiran", "Legacy 2")
    r = post_complete("admin1", uid2)
    with app.app_context():
        done2 = is_completed(uid2)
    check("admin CAN complete any legacy task -> 200",
          r.status_code == 200 and bool(done2), f"HTTP {r.status_code}")

    # Unassigned legacy task is admin-only (no back-door hijack)
    with app.app_context():
        uid3 = seed_legacy("911", "", "Legacy unassigned")
    r = post_complete("Bibin", uid3)
    with app.app_context():
        grabbed = is_completed(uid3)
    check("unassigned legacy task is NOT staff-grabbable -> 403",
          r.status_code == 403 and not grabbed, f"HTTP {r.status_code}")
    r = post_complete("admin1", uid3)
    check("admin CAN complete an unassigned legacy task -> 200",
          r.status_code == 200)

    # Raw-case legacy payload must still match its owner (payloads written by
    # the pre-16.5A7 route were never normalized).
    with app.app_context():
        uid4 = seed_legacy("911", "kiran", "Legacy raw case")
    r = post_complete("Kiran", uid4)
    with app.app_context():
        done4 = is_completed(uid4)
    check("raw-case legacy assignee still matches its owner -> 200",
          r.status_code == 200 and bool(done4),
          f"HTTP {r.status_code} — 'kiran' vs 'Kiran' must not lock the owner out")

    # Unknown task_id must not fabricate a completion
    r = post_complete("admin1", "does-not-exist-" + uuid.uuid4().hex)
    check("unknown task_id -> 404, no completion fabricated",
          r.status_code == 404, f"HTTP {r.status_code}")

    print()
    print("=" * 72)
    print("TASK PATH — must remain identical (regression)")
    print("=" * 72)
    with app.app_context():
        from app.services import task_service as ts
        t = ts.create_task(tenant_id=T, title="New: Call Asha",
                           created_by="Admin1", lead_phone="911",
                           assigned_staff="Kiran")
        t_uid, t_id = t.task_uid, t.id

    r = post_complete("Bibin", t_uid)
    with app.app_context():
        st = Task.query.filter_by(task_uid=t_uid).first().status
    check("other staff CANNOT complete a Task-row task -> 403",
          r.status_code == 403 and st != "COMPLETED", f"HTTP {r.status_code}")

    r = post_complete("Kiran", t_uid)
    with app.app_context():
        row = Task.query.filter_by(task_uid=t_uid).first()
    check("assigned staff CAN complete a Task-row task -> 200",
          r.status_code == 200 and row.status == "COMPLETED",
          f"HTTP {r.status_code}")
    check("Task-row credit recorded to the assignee",
          row.completed_by == "Kiran", f"completed_by={row.completed_by!r}")

    print()
    print("=" * 72)
    print("BOTH PATHS REACH THE SAME DECISION")
    print("=" * 72)
    with app.app_context():
        legacy_uid = seed_legacy("911", "Kiran", "Parity legacy")
        t2 = ts.create_task(tenant_id=T, title="Parity task",
                            created_by="Admin1", lead_phone="911",
                            assigned_staff="Kiran")
        t2_uid = t2.task_uid
    for who, expect in [("Bibin", 403), ("admin1", 200)]:
        rl = post_complete(who, legacy_uid).status_code
        rt = post_complete(who, t2_uid).status_code
        check(f"{who}: legacy={rl} task={rt} -> identical ({expect})",
              rl == rt == expect, f"legacy={rl} task={rt}")

    print()
    print("=" * 72)
    print("REGRESSION — update / notifications / staff productivity inputs")
    print("=" * 72)
    with app.app_context():
        t3 = ts.create_task(tenant_id=T, title="Update me", created_by="Admin1",
                            lead_phone="911", assigned_staff="Kiran")
        t3_id = t3.id
    with app.test_client() as c:
        login(c, "Bibin")
        r = c.post(f"/crm/tasks/{t3_id}/update", json={"status": "IN_PROGRESS"})
    check("task update still 403 for other staff", r.status_code == 403,
          f"HTTP {r.status_code}")
    with app.test_client() as c:
        login(c, "Kiran")
        r = c.post(f"/crm/tasks/{t3_id}/update",
                   json={"status": "IN_PROGRESS", "staff_notes": "on it"})
    with app.app_context():
        row3 = Task.query.get(t3_id)
    check("task update still 200 for the assignee",
          r.status_code == 200 and row3.status == "IN_PROGRESS",
          f"HTTP {r.status_code}")

    with app.test_client() as c:
        login(c, "Kiran")
        r = c.get("/crm/notifications/unread-count")
    check("notification badge endpoint still works",
          r.status_code == 200 and "count" in (r.get_json() or {}),
          f"HTTP {r.status_code}")

    with app.app_context():
        from app.routes.admin import get_all_tasks
        o, cdone = get_all_tasks(T)
        check("get_all_tasks still returns both legacy and Task rows",
              any(x["is_legacy"] for x in o + cdone)
              and any(not x["is_legacy"] for x in o + cdone))
        check("completed legacy tasks carry completed_by (staff productivity)",
              all(x.get("completed_by") for x in cdone if x["is_legacy"]),
              f"{[x.get('completed_by') for x in cdone if x['is_legacy']]}")

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
    print("ALL LEGACY COMPLETION AUTHORIZATION TESTS PASS")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        try:
            if os.path.exists(_DB):
                os.remove(_DB)
        except OSError:
            pass
    sys.exit(code)
