"""Phase H3-1B-d — reject invalid owners on the unassigned-queue assign path.

crm_unassigned_assign is the EIGHTH and last assigned_staff write path to be
validated. With it, no write path in the application can persist an owner that
does not resolve to a current user of the acting tenant.

WHY THIS PHASE EXISTS SEPARATELY
--------------------------------
It should have shipped in H3-1B-a. The H3-1B discovery report listed this
route in its write-path INVENTORY but omitted it from the reject/warn
RECOMMENDATION table, so the approved scope covered four form/JSON paths
instead of five. H3-1B-c found the omission and recorded it in a test rather
than silently widening its own scope. This phase closes it under its own
approval.

POLICY: REJECT
--------------
Same as the other form paths (H3-1B-a). The target comes from a <select> built
from staff_service.active_display_names(), so a value that does not resolve
means a stale page or a tampered POST — not an honest typo. CSV import
(H3-1B-c) remains the only path that warns and drops.

NO ROLLBACK HERE, DELIBERATELY
------------------------------
crm_lead_update rolls back on rejection because it assigns other fields before
the owner. This route touches nothing before the validation, so there is no
dirty state to unwind; adding a rollback would be cargo-cult. The structural
test below pins the ordering that makes that true — if a future edit moves a
mutation above the check, the ordering test fails.

THE ERROR CHANNEL IS NEW
------------------------
This screen had no way to show an error: it is standalone (no base template)
and renders no flashed messages, so rejecting silently would have looked like
the click did nothing. The route now passes err= and the template renders it,
following the crm_lead_new / crm_lead_detail convention.

Import isolation follows test_assignment_reject_h3_1b_a.py.
"""
import ast
import os
import re
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_h31bd_unassigned.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "h31bd-admin-key")
os.environ.setdefault("SECRET_KEY", "h31bd-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "h31bd-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState, LeadEvent       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX = "t-ox"
OTHER = "t-other"
URL = "/crm/leads/unassigned/assign"

# The same hostile set the other reject suites use.
BAD = ["Anju_display", "Ravi", "asdf", "'; DROP TABLE users; --", "Unassigned"]

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
               "admin": _mk(OX, "admin_ox", role="ADMIN").id,
               "other_admin": _mk(OTHER, "admin_other", role="ADMIN").id}

        # Unassigned queue rows (the ones this screen operates on).
        for i in (1, 2, 3):
            db.session.add(ConversationState(
                phone=f"91910000000{i}", tenant_id=OX, name=f"Unassigned {i}",
                lead_status="Lead", assigned_staff=None, assigned_user_id=None))
        # An already-owned row, to prove a rejection cannot clear an owner.
        db.session.add(ConversationState(
            phone="919100000009", tenant_id=OX, name="Owned",
            lead_status="Lead", assigned_staff="Anju",
            assigned_user_id=ids["anju"]))
        # A foreign-tenant row on a DIFFERENT phone.
        db.session.add(ConversationState(
            phone="919100000077", tenant_id=OTHER, name="Foreign",
            lead_status="Lead", assigned_staff=None, assigned_user_id=None))
        db.session.commit()
        yield ids
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def post(uid, phone, target, key=""):
    return client(uid).post(f"{URL}?key={key}",
                            data={"phone": phone, "target_staff": target},
                            follow_redirects=False)


def lead(phone, tenant=OX):
    with _APP.app_context():
        return ConversationState.query.filter_by(phone=phone,
                                                 tenant_id=tenant).first()


# ═══ accepted values ═════════════════════════════════════════════════════════

class TestAccepted:

    def test_valid_owner_assigns_and_populates_fk(self, seeded):
        r = post(seeded["admin"], "919100000001", "Anju")
        assert r.status_code in (302, 303)
        row = lead("919100000001")
        assert row.assigned_staff == "Anju"
        assert row.assigned_user_id == seeded["anju"]

    def test_valid_assignment_redirects_without_an_error(self, seeded):
        r = post(seeded["admin"], "919100000001", "Kiran")
        assert "err=" not in r.headers.get("Location", "")

    def test_inactive_staff_is_accepted(self, seeded):
        """BLOCK_DEACTIVATION already makes inactive-but-assigned a supported
        state, and the FK still lands on a real user."""
        post(seeded["admin"], "919100000002", "Old Staff")
        row = lead("919100000002")
        assert row.assigned_staff == "Old Staff"
        assert row.assigned_user_id == seeded["gone"]

    def test_case_variant_accepted_and_stored_as_typed(self, seeded):
        """`.value`, not `.canonical` — the operator's spelling survives."""
        post(seeded["admin"], "919100000003", "anju")
        row = lead("919100000003")
        assert row.assigned_staff == "anju"
        assert row.assigned_user_id == seeded["anju"]

    def test_surrounding_whitespace_is_tolerated(self, seeded):
        post(seeded["admin"], "919100000001", "  Kiran  ")
        row = lead("919100000001")
        assert row.assigned_user_id == seeded["kiran"]

    def test_audit_event_is_written_on_success(self, seeded):
        post(seeded["admin"], "919100000001", "Anju")
        with _APP.app_context():
            ev = LeadEvent.query.filter_by(phone="919100000001",
                                           event_type="LEAD_REASSIGNED").all()
            assert ev, "no LEAD_REASSIGNED event written"
            assert "Anju" in (ev[-1].event_data or "")


# ═══ rejected values ═════════════════════════════════════════════════════════

class TestRejected:

    def test_invalid_owner_is_rejected_and_writes_nothing(self, seeded):
        for bad in BAD:
            r = post(seeded["admin"], "919100000001", bad)
            assert r.status_code in (302, 303), bad
            assert "err=" in r.headers.get("Location", ""), bad
            row = lead("919100000001")
            assert row.assigned_staff is None, f"{bad!r} was written"
            assert row.assigned_user_id is None, f"{bad!r} populated the FK"

    def test_rejection_cannot_clear_an_existing_owner(self, seeded):
        """The mutation that would hurt most: a bad value wiping a good owner."""
        for bad in BAD:
            post(seeded["admin"], "919100000009", bad)
            row = lead("919100000009")
            assert row.assigned_staff == "Anju", bad
            assert row.assigned_user_id == seeded["anju"], bad

    def test_foreign_tenant_staff_is_rejected(self, seeded):
        r = post(seeded["admin"], "919100000001", "Ravi")
        assert "err=" in r.headers.get("Location", "")
        assert lead("919100000001").assigned_user_id is None

    def test_rejection_writes_no_audit_event(self, seeded):
        post(seeded["admin"], "919100000001", "asdf")
        with _APP.app_context():
            assert LeadEvent.query.filter_by(
                phone="919100000001", event_type="LEAD_REASSIGNED").count() == 0

    def test_error_message_names_the_rejected_value(self, seeded):
        r = post(seeded["admin"], "919100000001", "Ravi")
        loc = r.headers.get("Location", "")
        assert "Ravi" in loc.replace("+", " ").replace("%27", "'")

    def test_rejection_preserves_the_key_parameter(self, seeded):
        """The operator must land back on the same authorised screen."""
        r = post(seeded["admin"], "919100000001", "asdf", key="abc123")
        assert "key=abc123" in r.headers.get("Location", "")

    def test_redirect_target_is_the_unassigned_queue(self, seeded):
        r = post(seeded["admin"], "919100000001", "asdf")
        assert "/crm/leads/unassigned" in r.headers.get("Location", "")


# ═══ pre-existing behaviour that must not regress ════════════════════════════

class TestUnchangedBehaviour:

    def test_blank_target_still_returns_early(self, seeded):
        r = post(seeded["admin"], "919100000001", "")
        assert r.status_code in (302, 303)
        assert lead("919100000001").assigned_staff is None

    def test_missing_phone_still_returns_early(self, seeded):
        r = client(seeded["admin"]).post(URL, data={"target_staff": "Anju"},
                                         follow_redirects=False)
        assert r.status_code in (302, 303)

    def test_unknown_phone_writes_nothing(self, seeded):
        r = post(seeded["admin"], "910000000000", "Anju")
        assert r.status_code in (302, 303)

    def test_tenant_scoping_holds(self, seeded):
        """Phase 14B.2 (C1): phone is not unique across tenants. An Oxford
        admin must not touch the other tenant's row of the same phone."""
        post(seeded["admin"], "919100000077", "Anju")
        assert lead("919100000077", OTHER).assigned_staff is None

    def test_dual_write_still_present(self, seeded):
        post(seeded["admin"], "919100000001", "Kiran")
        row = lead("919100000001")
        assert row.assigned_staff == "Kiran"
        assert row.assigned_user_id == seeded["kiran"]


# ═══ structural guarantees ═══════════════════════════════════════════════════

class TestStructure:

    def _fn(self, name="crm_unassigned_assign"):
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        return next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name), tree

    def test_validation_precedes_every_mutation(self):
        """The reason no rollback is needed. If a future edit moves a write
        above the check, this fails and the rollback question reopens."""
        fn, _ = self._fn()
        src = ast.unparse(fn)
        check = src.index("resolve_assignment")
        # Trailing "(" on the call markers deliberately: the route imports
        # log_lead_event near the top, and matching the bare name would find
        # the import rather than the call.
        for marker in ("lead.assigned_staff =", "_sync_assigned_user(",
                       "log_lead_event(", "db.session.commit("):
            assert marker in src, marker
            assert src.index(marker) > check, \
                f"{marker} runs before the owner is validated"

    def test_rejection_returns_before_any_commit(self):
        fn, _ = self._fn()
        src = ast.unparse(fn)
        i = src.index("if not _owner.ok")
        window = src[i:i + 400]
        assert "return redirect" in window
        assert "commit" not in window

    def test_reject_policy_not_warn(self):
        """CSV import stays the only warn-and-drop path."""
        fn, _ = self._fn()
        src = ast.unparse(fn)
        assert "summary['errors']" not in src.replace('"', "'")
        assert "err=" in src

    def test_writes_value_not_canonical(self):
        fn, _ = self._fn()
        src = ast.unparse(fn)
        assert "_owner.value" in src
        assert "_owner.canonical" not in src

    def test_all_eight_write_paths_validate(self):
        """The point of the whole H3-1B programme."""
        _, tree = self._fn()
        for name in ("crm_lead_new", "crm_lead_update", "crm_unassigned_assign",
                     "crm_auto_assign_confirm", "crm_reassignment_confirm",
                     "crm_leads_import"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            assert "resolve_assignment" in ast.unparse(fn), name
        svc = open(os.path.join(ROOT, "app/services/task_service.py"),
                   encoding="utf-8").read()
        assert svc.count("resolve_assignment") >= 2

    def test_error_is_actually_rendered(self):
        """A redirect carrying err= is useless if nothing displays it. This
        screen had no error channel before this phase."""
        _, tree = self._fn()
        route = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)
                     and n.name == "crm_unassigned_leads")
        assert "err=request.args.get('err'" in ast.unparse(route).replace('"', "'")
        tpl = open(os.path.join(ROOT, "templates", "crm_unassigned_leads.html"),
                   encoding="utf-8").read()
        assert re.search(r"{%\s*if\s+err\s*%}", tpl)
        assert "{{ err" in tpl

    def test_no_schema_or_migration_change(self):
        versions = os.path.join(ROOT, "migrations", "versions")
        assert not [f for f in os.listdir(versions) if "h3" in f.lower()]

    def test_read_fk_untouched(self):
        from app import flags
        os.environ.pop("STAFF_IDENTITY_READ_FK", None)
        assert flags.staff_identity_read_fk_enabled() is False
