"""Phase H3-1B-a — reject invalid owners on the form and JSON lead paths.

Four write paths now refuse an assigned_staff value that is not a current
staff member of the acting tenant:

    crm_lead_new              form  -> redirect + err=
    crm_lead_update           form  -> redirect + err=  (after rollback)
    crm_auto_assign_confirm   JSON  -> 400, batch rejected
    crm_reassignment_confirm  JSON  -> 400, nothing written

WHY REJECT HERE AND WARN ON CSV
-------------------------------
These four take the owner from a dropdown that already lists only valid
options, so an invalid value is a crafted request or a stale page — not an
honest typo. CSV import (H3-1B-c) warns and drops the field instead, matching
the precedent already in that function for lead_score and lead_status.

WHAT REJECTION MUST NOT DO
--------------------------
Change anything. crm_lead_update assigns lead_status and notes BEFORE the
owner, so a rejection there has to roll back or it leaves a partial edit — the
same reason the admission hard-block rolls back. crm_auto_assign_confirm
carries a distinct owner per row, so it validates the whole batch up front:
rejecting mid-loop would leave some leads reassigned and others not, with a
400 that says nothing about which.

STILL ACCEPTED
--------------
Blank (unassignment), case variants, and INACTIVE staff — they are real users
whose FK populates, and BLOCK_DEACTIVATION already makes inactive-but-assigned
a supported state. Values are stored as the operator typed them; `.canonical`
is deliberately not written (that would silently rewrite operator input).

Import isolation follows test_csv_import_dual_write_h3_1a.py.
"""
import ast
import json
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_h31ba_reject.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "h31ba-admin-key")
os.environ.setdefault("SECRET_KEY", "h31ba-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "h31ba-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState                  # noqa: E402

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
        for i, owner in ((1, "Anju"), (2, "Kiran"), (3, None)):
            db.session.add(ConversationState(
                phone=f"91900000000{i}", tenant_id=OX, name=f"Lead {i}",
                lead_status="Lead", assigned_staff=owner,
                assigned_user_id=ids["anju"] if owner == "Anju"
                else (ids["kiran"] if owner == "Kiran" else None)))
        db.session.commit()
        yield ids
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def lead(phone, tenant=OX):
    with _APP.app_context():
        return ConversationState.query.filter_by(phone=phone,
                                                 tenant_id=tenant).first()


# ═══ crm_lead_new (form) ═════════════════════════════════════════════════════

class TestLeadNew:
    URL = "/crm/lead/new"

    def post(self, uid, **form):
        return client(uid).post(self.URL, data=form, follow_redirects=False)

    def test_valid_owner_creates_the_lead(self, seeded):
        r = self.post(seeded["admin"], phone="919000001001",
                      name="Valid", assigned_staff="Anju")
        assert r.status_code in (302, 303)
        row = lead("919000001001")
        assert row.assigned_staff == "Anju"
        assert row.assigned_user_id == seeded["anju"]

    def test_invalid_owner_is_rejected_and_creates_nothing(self, seeded):
        for bad in BAD:
            r = self.post(seeded["admin"], phone="919000001002",
                          name="Rejected", assigned_staff=bad)
            assert r.status_code in (302, 303), bad
            assert "err=" in r.headers.get("Location", ""), bad
            assert lead("919000001002") is None, f"{bad!r} created a lead"

    def test_blank_owner_still_creates_an_unassigned_lead(self, seeded):
        self.post(seeded["admin"], phone="919000001003", name="Blank",
                  assigned_staff="")
        row = lead("919000001003")
        assert row is not None
        assert row.assigned_staff is None and row.assigned_user_id is None

    def test_inactive_staff_is_accepted(self, seeded):
        self.post(seeded["admin"], phone="919000001004", name="Inactive",
                  assigned_staff="Old Staff")
        assert lead("919000001004").assigned_user_id == seeded["gone"]

    def test_case_variant_accepted_and_stored_as_typed(self, seeded):
        """`.value`, not `.canonical` — the operator's spelling is preserved."""
        self.post(seeded["admin"], phone="919000001005", name="Case",
                  assigned_staff="anju")
        row = lead("919000001005")
        assert row.assigned_staff == "anju"
        assert row.assigned_user_id == seeded["anju"]

    def test_foreign_tenant_staff_is_rejected(self, seeded):
        r = self.post(seeded["admin"], phone="919000001006", name="Foreign",
                      assigned_staff="Ravi")
        assert "err=" in r.headers.get("Location", "")
        assert lead("919000001006") is None


# ═══ crm_lead_update (form) ══════════════════════════════════════════════════

class TestLeadUpdate:
    def post(self, uid, phone, **form):
        return client(uid).post(f"/crm/lead/{phone}/update", data=form,
                                follow_redirects=False)

    def test_valid_owner_updates(self, seeded):
        self.post(seeded["admin"], "919000000001", lead_status="Contacted",
                  assigned_staff="Kiran")
        row = lead("919000000001")
        assert row.assigned_staff == "Kiran"
        assert row.assigned_user_id == seeded["kiran"]

    def test_invalid_owner_is_rejected(self, seeded):
        for bad in BAD:
            r = self.post(seeded["admin"], "919000000001",
                          lead_status="Contacted", assigned_staff=bad)
            assert "err=" in r.headers.get("Location", ""), bad
            row = lead("919000000001")
            assert row.assigned_staff == "Anju", bad
            assert row.assigned_user_id == seeded["anju"], bad

    def test_rejection_rolls_back_the_whole_edit(self, seeded):
        """lead_status and notes are assigned BEFORE the owner. A rejection
        must not leave those written — the partial-edit trap."""
        before = lead("919000000002")
        assert before.lead_status == "Lead"
        self.post(seeded["admin"], "919000000002", lead_status="Enrolled",
                  notes="should not persist", assigned_staff="asdf")
        after = lead("919000000002")
        assert after.lead_status == "Lead", "lead_status leaked through a rejection"
        assert after.notes != "should not persist", "notes leaked through"
        assert after.assigned_staff == "Kiran"

    def test_blank_owner_unassigns(self, seeded):
        self.post(seeded["admin"], "919000000001", lead_status="Lead",
                  assigned_staff="")
        row = lead("919000000001")
        assert row.assigned_staff is None
        assert row.assigned_user_id is None

    def test_inactive_staff_accepted(self, seeded):
        self.post(seeded["admin"], "919000000001", lead_status="Lead",
                  assigned_staff="Old Staff")
        assert lead("919000000001").assigned_user_id == seeded["gone"]


# ═══ crm_auto_assign_confirm (JSON, batch) ═══════════════════════════════════

class TestAutoAssignConfirm:
    URL = "/crm/leads/unassigned/auto-assign-confirm"

    def post(self, uid, assignments):
        return client(uid).post(self.URL,
                                data=json.dumps({"assignments": assignments}),
                                content_type="application/json")

    def test_valid_batch_applies(self, seeded):
        r = self.post(seeded["admin"],
                      [{"phone": "919000000003", "target_staff": "Anju"}])
        assert r.status_code == 200, r.get_data(as_text=True)
        assert lead("919000000003").assigned_user_id == seeded["anju"]

    def test_invalid_owner_returns_400(self, seeded):
        r = self.post(seeded["admin"],
                      [{"phone": "919000000003", "target_staff": "asdf"}])
        assert r.status_code == 400
        body = r.get_json()
        assert "rejected" in body and body["rejected"][0]["target_staff"] == "asdf"

    def test_one_bad_row_rejects_the_WHOLE_batch(self, seeded):
        """All-or-nothing. A per-row reject mid-loop would leave some leads
        reassigned and others not, with a 400 saying nothing about which."""
        r = self.post(seeded["admin"], [
            {"phone": "919000000003", "target_staff": "Anju"},
            {"phone": "919000000001", "target_staff": "Anju_display"},
        ])
        assert r.status_code == 400
        assert lead("919000000003").assigned_staff is None, \
            "a valid row was written despite the batch being rejected"
        assert lead("919000000001").assigned_staff == "Anju"

    def test_rejection_names_every_offender(self, seeded):
        r = self.post(seeded["admin"], [
            {"phone": "919000000003", "target_staff": "asdf"},
            {"phone": "919000000001", "target_staff": "Ravi"},
        ])
        assert {x["target_staff"] for x in r.get_json()["rejected"]} == {"asdf", "Ravi"}

    def test_foreign_tenant_staff_rejected(self, seeded):
        r = self.post(seeded["admin"],
                      [{"phone": "919000000003", "target_staff": "Ravi"}])
        assert r.status_code == 400
        assert lead("919000000003").assigned_user_id is None


# ═══ crm_reassignment_confirm (JSON, single target) ══════════════════════════

class TestReassignmentConfirm:
    URL = "/crm/reassignment-center/confirm"

    def post(self, uid, phones, target):
        return client(uid).post(self.URL,
                                data=json.dumps({"phones": phones,
                                                 "target_staff": target}),
                                content_type="application/json")

    def test_valid_target_applies(self, seeded):
        r = self.post(seeded["admin"], ["919000000001"], "Kiran")
        assert r.status_code == 200, r.get_data(as_text=True)
        assert lead("919000000001").assigned_user_id == seeded["kiran"]

    def test_invalid_target_returns_400_and_writes_nothing(self, seeded):
        for bad in BAD:
            r = self.post(seeded["admin"],
                          ["919000000001", "919000000002"], bad)
            assert r.status_code == 400, bad
            assert lead("919000000001").assigned_staff == "Anju", bad
            assert lead("919000000002").assigned_staff == "Kiran", bad

    def test_error_body_is_structured(self, seeded):
        body = self.post(seeded["admin"], ["919000000001"], "asdf").get_json()
        assert body["target_staff"] == "asdf"
        assert body["reason"]
        assert "nothing was reassigned" in body["error"]

    def test_inactive_target_accepted(self, seeded):
        r = self.post(seeded["admin"], ["919000000001"], "Old Staff")
        assert r.status_code == 200
        assert lead("919000000001").assigned_user_id == seeded["gone"]


# ═══ Cross-cutting ═══════════════════════════════════════════════════════════

class TestNoFkNullRowsCanBeCreated:
    def test_no_path_can_store_an_unresolvable_owner(self, seeded):
        """The point of the phase: after H3-1B-a none of these four paths can
        add a row with an owner that resolves to nobody."""
        client(seeded["admin"]).post("/crm/lead/new", data={
            "phone": "919000009001", "name": "X", "assigned_staff": "ghost"})
        client(seeded["admin"]).post("/crm/lead/919000000001/update", data={
            "lead_status": "Lead", "assigned_staff": "ghost"})
        client(seeded["admin"]).post(
            "/crm/leads/unassigned/auto-assign-confirm",
            data=json.dumps({"assignments": [
                {"phone": "919000000003", "target_staff": "ghost"}]}),
            content_type="application/json")
        client(seeded["admin"]).post(
            "/crm/reassignment-center/confirm",
            data=json.dumps({"phones": ["919000000002"],
                             "target_staff": "ghost"}),
            content_type="application/json")
        with _APP.app_context():
            orphans = ConversationState.query.filter(
                ConversationState.assigned_staff.isnot(None),
                ConversationState.assigned_staff != "",
                ConversationState.assigned_user_id.is_(None)).all()
        assert orphans == [], [(o.phone, o.assigned_staff) for o in orphans]


class TestScopeContainment:
    WIRED = {"crm_lead_new", "crm_lead_update",
             "crm_auto_assign_confirm", "crm_reassignment_confirm"}
    LATER = {"crm_leads_import", "crm_tasks_create", "crm_tasks_edit"}

    def _tree(self):
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            return ast.parse(fh.read())

    def test_exactly_the_approved_paths_are_wired(self):
        tree = self._tree()
        users = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for c in ast.walk(fn):
                if isinstance(c, ast.Call) and \
                        ast.unparse(c.func).endswith("resolve_assignment"):
                    users.add(fn.name)
        assert users == self.WIRED, users

    def test_later_phases_are_untouched(self):
        """CSV (H3-1B-c) and tasks (H3-1B-b) must not be wired here — CSV in
        particular gets warn-and-drop, not reject."""
        tree = self._tree()
        for name in ("crm_leads_import", "crm_tasks_create", "crm_tasks_edit"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            assert "resolve_assignment" not in ast.unparse(fn), name

    def test_dual_write_still_on_every_path(self):
        tree = self._tree()
        for name in ("crm_lead_new", "crm_lead_update", "crm_unassigned_assign",
                     "crm_auto_assign_confirm", "crm_reassignment_confirm",
                     "crm_leads_import"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            calls = {ast.unparse(c.func) for c in ast.walk(fn)
                     if isinstance(c, ast.Call)}
            assert "_sync_assigned_user" in calls, name

    def test_lead_update_rejection_rolls_back_explicitly(self):
        """The rejection in crm_lead_update must call db.session.rollback().

        Found by mutation testing: deleting the rollback passed every
        behavioural test, because without a commit Flask-SQLAlchemy discards
        the session at teardown and the dirty lead_status/notes never persist.
        So the rollback is defensive rather than load-bearing TODAY — and that
        is exactly why it needs a structural guard. Add one query between the
        mutation and the return and SQLAlchemy autoflushes the partial edit;
        the admission hard-block a few lines below carries the same rollback
        for the same reason. Behaviour cannot distinguish it, so assert the
        code shape.
        """
        tree = self._tree()
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "crm_lead_update")
        # locate the resolve_assignment guard and require a rollback inside it
        found = False
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            src = ast.unparse(node)
            if "_owner.ok" in src and "redirect" in src:
                assert "db.session.rollback()" in src, \
                    "rejection must roll back the partial edit"
                found = True
        assert found, "the owner-rejection guard is gone from crm_lead_update"

    def test_canonical_is_not_written(self):
        """Storing `.canonical` would silently rewrite operator input
        ('anju' -> 'Anju'). Deferred to the contract phase."""
        tree = self._tree()
        for name in self.WIRED:
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
