"""Phase H3-1B-b — reject invalid assignees on the task paths.

create_task and update_task now refuse an assigned_staff value that is not a
current staff member of the acting tenant, raising TaskError — the mechanism
this service already uses at eight validation sites.

WHY THE SERVICE, NOT THE ROUTES
-------------------------------
task_service is the choke point both routes go through (discovery confirmed
crm_tasks_create and crm_tasks_edit are its ONLY callers — no background job
or automation creates assigned tasks), it already owns every other field's
validation, and it already signals refusal the same way.

TWO THINGS THIS PHASE HAD TO FIX BEYOND ADDING THE CHECK
--------------------------------------------------------
1. update_task assigned title/notes/priority BEFORE reaching the assignee, so
   a TaskError there left a partial edit on the instance — surviving only
   because the caller does not commit. That is the fragility mutation testing
   exposed in crm_lead_update. The validation is hoisted above every mutation.

2. crm_tasks_create SWALLOWED TaskError — logged it and fell through to the
   same redirect as success. A rejected create looked like a task that simply
   never appeared. Harmless while the only refusal was "title required" (which
   the form's own `required` prevents), but this phase adds a refusal a real
   operator can trigger. It now surfaces on lead detail, which renders `err`.

Import isolation follows test_assignment_reject_h3_1b_a.py.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_h31bb_tasks.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "h31bb-admin-key")
os.environ.setdefault("SECRET_KEY", "h31bb-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "h31bb-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState, Task            # noqa: E402
from app.services import task_service                                   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX = "t-ox"
OTHER = "t-other"
BAD = ["Anju_display", "Ravi", "asdf", "'; DROP TABLE users; --"]

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False


@pytest.fixture(autouse=True)
def dual_write_on():
    before = os.environ.get("STAFF_IDENTITY_DUAL_WRITE")
    os.environ["STAFF_IDENTITY_DUAL_WRITE"] = "true"
    os.environ.pop("STAFF_IDENTITY_READ_FK", None)
    yield
    if before is None:
        os.environ.pop("STAFF_IDENTITY_DUAL_WRITE", None)
    else:
        os.environ["STAFF_IDENTITY_DUAL_WRITE"] = before


def _mk(tenant, username, role="STAFF", active=True):
    u = User(username=username, email=f"{username}.{tenant}@x.test".replace(" ", "_"),
             password_hash=generate_password_hash("pw"), role=role,
             tenant_id=tenant, is_active=active, require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((OX, "Oxford"), (OTHER, "Other")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        ids = {"anju": _mk(OX, "Anju").id,
               "kiran": _mk(OX, "Kiran").id,
               "gone": _mk(OX, "Old Staff", active=False).id,
               "ravi": _mk(OTHER, "Ravi").id,
               "admin": _mk(OX, "admin_ox", role="ADMIN").id}
        db.session.add(ConversationState(phone="919000000001", tenant_id=OX,
                                         name="Lead One", lead_status="Lead"))
        db.session.commit()
        yield ids
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


# ═══ create_task ═════════════════════════════════════════════════════════════

class TestCreateTask:
    def test_valid_assignee_creates_and_populates_the_fk(self, seeded):
        with _APP.app_context():
            t = task_service.create_task(tenant_id=OX, title="Call back",
                                         created_by="tester",
                                         assigned_staff="Anju")
            assert t.assigned_staff == "Anju"
            assert t.assigned_user_id == seeded["anju"]

    def test_invalid_assignee_raises_and_creates_nothing(self, seeded):
        with _APP.app_context():
            for bad in BAD:
                before = Task.query.count()
                with pytest.raises(task_service.TaskError) as exc:
                    task_service.create_task(tenant_id=OX, title="Nope",
                                             created_by="tester",
                                             assigned_staff=bad)
                assert "not a current staff member" in str(exc.value), bad
                db.session.rollback()
                assert Task.query.count() == before, bad

    def test_blank_assignee_creates_an_unassigned_task(self, seeded):
        with _APP.app_context():
            for blank in (None, "", "   "):
                t = task_service.create_task(tenant_id=OX, title="Unowned",
                                             created_by="tester",
                                             assigned_staff=blank)
                assert t.assigned_staff is None
                assert t.assigned_user_id is None

    def test_inactive_staff_accepted(self, seeded):
        with _APP.app_context():
            t = task_service.create_task(tenant_id=OX, title="Legacy",
                                         created_by="tester",
                                         assigned_staff="Old Staff")
            assert t.assigned_user_id == seeded["gone"]

    def test_case_variant_accepted_and_stored_as_given(self, seeded):
        with _APP.app_context():
            t = task_service.create_task(tenant_id=OX, title="Case",
                                         created_by="tester",
                                         assigned_staff="anju")
            assert t.assigned_staff == "anju"
            assert t.assigned_user_id == seeded["anju"]

    def test_foreign_tenant_assignee_rejected(self, seeded):
        with _APP.app_context():
            with pytest.raises(task_service.TaskError):
                task_service.create_task(tenant_id=OX, title="Foreign",
                                         created_by="tester",
                                         assigned_staff="Ravi")

    def test_no_notification_is_sent_for_a_rejected_task(self, seeded):
        """The refusal happens before the Task exists, so the TASK_ASSIGNED
        notification cannot fire for a staff member who does not exist."""
        from app.models import Notification
        with _APP.app_context():
            before = Notification.query.count()
            with pytest.raises(task_service.TaskError):
                task_service.create_task(tenant_id=OX, title="X",
                                         created_by="tester",
                                         assigned_staff="ghost")
            db.session.rollback()
            assert Notification.query.count() == before


# ═══ update_task ═════════════════════════════════════════════════════════════

class TestUpdateTask:
    def _task(self, owner="Anju"):
        return task_service.create_task(tenant_id=OX, title="Original",
                                        created_by="tester",
                                        assigned_staff=owner, notes="keep")

    def test_valid_reassignment(self, seeded):
        with _APP.app_context():
            t = self._task()
            task_service.update_task(tenant_id=OX, task_id=t.id, actor="tester",
                                     assigned_staff="Kiran")
            assert t.assigned_staff == "Kiran"
            assert t.assigned_user_id == seeded["kiran"]

    def test_invalid_assignee_raises(self, seeded):
        with _APP.app_context():
            t = self._task()
            for bad in BAD:
                with pytest.raises(task_service.TaskError):
                    task_service.update_task(tenant_id=OX, task_id=t.id,
                                             actor="tester", assigned_staff=bad)
                db.session.rollback()
                assert t.assigned_staff == "Anju", bad
                assert t.assigned_user_id == seeded["anju"], bad

    def test_rejection_leaves_other_fields_untouched(self, seeded):
        """THE HOISTING TEST. Validation runs before title/notes are written,
        so a rejected edit cannot have mutated the task at all — not even on
        the in-memory instance."""
        with _APP.app_context():
            t = self._task()
            tid_ = t.id
            with pytest.raises(task_service.TaskError):
                task_service.update_task(
                    tenant_id=OX, task_id=tid_, actor="tester",
                    title="REWRITTEN", notes="REWRITTEN",
                    priority="URGENT", assigned_staff="ghost")
            # the instance itself must be clean, before any rollback
            assert t.title == "Original", "title was written despite rejection"
            assert t.notes == "keep", "notes were written despite rejection"
            assert t.priority == "NORMAL", "priority was written despite rejection"

    def test_blank_assignee_unassigns(self, seeded):
        with _APP.app_context():
            t = self._task()
            task_service.update_task(tenant_id=OX, task_id=t.id, actor="tester",
                                     assigned_staff="")
            assert t.assigned_staff is None
            assert t.assigned_user_id is None

    def test_omitting_the_field_leaves_the_owner_alone(self, seeded):
        """assigned_staff=None means "not supplied", NOT "unassign"."""
        with _APP.app_context():
            t = self._task()
            task_service.update_task(tenant_id=OX, task_id=t.id, actor="tester",
                                     title="New title")
            assert t.title == "New title"
            assert t.assigned_staff == "Anju"
            assert t.assigned_user_id == seeded["anju"]

    def test_inactive_staff_accepted(self, seeded):
        with _APP.app_context():
            t = self._task()
            task_service.update_task(tenant_id=OX, task_id=t.id, actor="tester",
                                     assigned_staff="Old Staff")
            assert t.assigned_user_id == seeded["gone"]


# ═══ Route behaviour — the swallowed-error fix ═══════════════════════════════

class TestRoutesSurfaceTheRefusal:
    def test_create_route_surfaces_the_error(self, seeded):
        """Previously this logged and redirected exactly as on success, so a
        rejected create looked like a task that never appeared."""
        r = client(seeded["admin"]).post("/crm/tasks/create", data={
            "phone": "919000000001", "task": "Call", "due_date": "2026-12-01",
            "staff": "ghost", "priority": "NORMAL"}, follow_redirects=False)
        assert r.status_code in (302, 303)
        loc = r.headers.get("Location", "")
        assert "err=" in loc, loc
        with _APP.app_context():
            assert Task.query.count() == 0

    def test_create_route_still_works_for_a_valid_assignee(self, seeded):
        r = client(seeded["admin"]).post("/crm/tasks/create", data={
            "phone": "919000000001", "task": "Call", "due_date": "2026-12-01",
            "staff": "Anju", "priority": "NORMAL"}, follow_redirects=False)
        assert "err=" not in r.headers.get("Location", "")
        with _APP.app_context():
            t = Task.query.one()
            assert t.assigned_user_id == seeded["anju"]

    def test_create_route_allows_an_unassigned_task(self, seeded):
        """H3-1A's operator decision: a zero-staff tenant must still be able
        to create tasks. Blank must not start rejecting."""
        r = client(seeded["admin"]).post("/crm/tasks/create", data={
            "phone": "919000000001", "task": "Call", "due_date": "2026-12-01",
            "staff": "", "priority": "NORMAL"}, follow_redirects=False)
        assert "err=" not in r.headers.get("Location", "")
        with _APP.app_context():
            assert Task.query.one().assigned_staff is None

    def test_edit_route_returns_400(self, seeded):
        with _APP.app_context():
            t = task_service.create_task(tenant_id=OX, title="T",
                                         created_by="tester",
                                         assigned_staff="Anju")
            tid_ = t.id
        r = client(seeded["admin"]).post(f"/crm/tasks/{tid_}/edit",
                                         data={"staff": "ghost"})
        assert r.status_code == 400
        assert "not a current staff member" in r.get_json()["error"]
        with _APP.app_context():
            assert Task.query.get(tid_).assigned_staff == "Anju"


# ═══ Scope containment ═══════════════════════════════════════════════════════

class TestScopeContainment:
    def _tree(self, path):
        with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
            return ast.parse(fh.read())

    def test_both_task_functions_validate(self):
        tree = self._tree("app/services/task_service.py")
        for name in ("create_task", "update_task"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            assert "resolve_assignment" in ast.unparse(fn), name

    def test_update_task_validates_before_any_mutation(self):
        """Structural, because behaviour cannot distinguish it once the caller
        declines to commit — the M1 lesson from H3-1B-a."""
        tree = self._tree("app/services/task_service.py")
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "update_task")
        src = ast.unparse(fn)
        validate_at = src.index("resolve_assignment")
        first_write = min(src.index(w) for w in
                          ("task.title = ", "task.notes = ", "task.priority = "))
        assert validate_at < first_write, \
            "validation must precede every field write"

    def test_csv_import_still_unwired(self):
        """H3-1B-c owns CSV, and it gets warn-and-drop, not reject."""
        tree = self._tree("app/routes/admin.py")
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "crm_leads_import")
        assert "resolve_assignment" not in ast.unparse(fn)

    def test_dual_write_still_on_both_task_paths(self):
        src = open(os.path.join(ROOT, "app/services/task_service.py"),
                   encoding="utf-8").read()
        assert src.count("sync_assigned_user(task, tenant_id)") == 2

    def test_canonical_is_not_written(self):
        tree = self._tree("app/services/task_service.py")
        for name in ("create_task", "update_task"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            assert ".canonical" not in ast.unparse(fn), name

    def test_no_schema_or_migration_change(self):
        versions = os.path.join(ROOT, "migrations", "versions")
        assert not [f for f in os.listdir(versions) if "h3" in f.lower()]

    def test_read_fk_untouched(self):
        from app import flags
        os.environ.pop("STAFF_IDENTITY_READ_FK", None)
        assert flags.staff_identity_read_fk_enabled() is False
