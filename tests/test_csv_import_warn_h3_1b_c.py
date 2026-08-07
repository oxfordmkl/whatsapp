"""Phase H3-1B-c — CSV import warns and drops an invalid owner.

The last of the eight write paths, and the ONLY one that warns rather than
rejects.

WHY CSV IS DIFFERENT
--------------------
The four form/JSON lead paths (H3-1B-a) and the two task paths (H3-1B-b) take
their owner from a dropdown listing only valid options, so an invalid value
there is a crafted request or a stale page. A spreadsheet is different: a typo
in one cell is an honest mistake, and failing a 500-row import over it — or
silently dropping the whole row — costs the operator far more than the bad
cell is worth.

So this follows the two precedents already in the same loop:

    lead_score  "not a number — ignored"
    lead_status "not a recognised status — ignored"

The row imports, the bad field is not written, and the reason lands in
summary["errors"], which the template already renders as "Rows needing
attention".

WHY DROPPING MATTERS BEYOND TIDINESS
------------------------------------
Writing an unresolvable owner would leave assigned_user_id NULL, and under
RC2.3E's FK reads that lead becomes INVISIBLE — gone from every per-staff view
and showing only under Unassigned. Not writing it keeps whatever valid owner
the lead already had.

Import isolation follows test_csv_import_dual_write_h3_1a.py.
"""
import ast
import io
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_h31bc_import.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "h31bc-admin-key")
os.environ.setdefault("SECRET_KEY", "h31bc-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "h31bc-broadcast")
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
URL = "/crm/leads/import"
OX = "t-ox"
OTHER = "t-other"

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
        yield ids
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def upload(uid, rows, header="phone,name,assigned_staff"):
    csv = header + "\n" + "\n".join(rows) + "\n"
    data = {"file": (io.BytesIO(csv.encode()), "leads.csv")}
    r = client(uid).post(URL, data=data, content_type="multipart/form-data",
                         follow_redirects=True)
    assert r.status_code == 200, r.status_code
    return r.get_data(as_text=True)


def lead(phone, tenant=OX):
    with _APP.app_context():
        return ConversationState.query.filter_by(phone=phone,
                                                 tenant_id=tenant).first()


# ═══ Warn and drop ═══════════════════════════════════════════════════════════

class TestInvalidOwnerIsDroppedNotRejected:
    def test_row_still_imports(self, seeded):
        """THE POLICY. A bad owner must not cost the operator the row."""
        upload(seeded["admin"], ["919000000001,Imported Anyway,ghost"])
        row = lead("919000000001")
        assert row is not None, "the row was dropped instead of the field"
        assert row.name == "Imported Anyway"

    def test_the_bad_owner_is_not_written(self, seeded):
        upload(seeded["admin"], ["919000000002,No Owner,ghost"])
        row = lead("919000000002")
        assert row.assigned_staff is None
        assert row.assigned_user_id is None

    def test_the_warning_is_reported(self, seeded):
        html = upload(seeded["admin"], ["919000000003,Warned,ghost"])
        assert "ignored" in html
        assert "ghost" in html

    def test_the_warning_names_the_row(self, seeded):
        """The operator has to be able to find the offending line."""
        html = upload(seeded["admin"], [
            "919000000010,Good,Anju",
            "919000000011,Bad,ghost",
        ])
        assert "Row 3" in html, "the warning must identify the CSV line"

    def test_it_matches_the_existing_precedents(self, seeded):
        """lead_score and lead_status already say '... — ignored'. This must
        read the same way, because it IS the same behaviour."""
        html = upload(seeded["admin"], ["919000000004,X,ghost"])
        assert "is not a current staff member" in html
        assert "ignored" in html

    def test_an_existing_valid_owner_is_preserved(self, seeded):
        """Not writing beats writing garbage: the lead keeps the owner it
        already had rather than becoming invisible under FK reads."""
        upload(seeded["admin"], ["919000000005,First,Anju"])
        assert lead("919000000005").assigned_user_id == seeded["anju"]
        upload(seeded["admin"], ["919000000005,Second,ghost"])
        row = lead("919000000005")
        assert row.name == "Second", "other fields must still import"
        assert row.assigned_staff == "Anju", "a bad cell wiped a good owner"
        assert row.assigned_user_id == seeded["anju"]

    def test_foreign_tenant_owner_is_dropped(self, seeded):
        upload(seeded["admin"], ["919000000006,Foreign,Ravi"])
        row = lead("919000000006")
        assert row is not None
        assert row.assigned_staff is None
        assert row.assigned_user_id is None

    def test_the_phantom_is_dropped(self, seeded):
        """'Anju_display' — the production value that RC2.3E would make
        invisible. An import can no longer introduce another one."""
        upload(seeded["admin"], ["919000000007,Phantom,Anju_display"])
        assert lead("919000000007").assigned_staff is None


class TestOneBadCellDoesNotFailTheFile:
    def test_good_rows_still_import(self, seeded):
        upload(seeded["admin"], [
            "919000001001,A,Anju",
            "919000001002,B,ghost",
            "919000001003,C,Kiran",
        ])
        assert lead("919000001001").assigned_user_id == seeded["anju"]
        assert lead("919000001002").assigned_staff is None
        assert lead("919000001003").assigned_user_id == seeded["kiran"]

    def test_all_three_rows_exist(self, seeded):
        upload(seeded["admin"], [
            "919000001001,A,Anju",
            "919000001002,B,ghost",
            "919000001003,C,Kiran",
        ])
        with _APP.app_context():
            assert ConversationState.query.filter(
                ConversationState.phone.like("91900000100%")).count() == 3

    def test_a_large_file_is_not_abandoned(self, seeded):
        rows = [f"9190000200{i:02d},Bulk {i},{'ghost' if i % 5 == 0 else 'Anju'}"
                for i in range(20)]
        upload(seeded["admin"], rows)
        with _APP.app_context():
            got = ConversationState.query.filter(
                ConversationState.phone.like("9190000200%")).all()
            assert len(got) == 20
            owned = [r for r in got if r.assigned_user_id is not None]
            assert len(owned) == 16, "valid rows must still be assigned"
            assert all(r.assigned_user_id == seeded["anju"] for r in owned)


class TestValidOwnersUnaffected:
    def test_valid_owner_still_populates_the_fk(self, seeded):
        """H3-1A's behaviour must survive this phase."""
        upload(seeded["admin"], ["919000003001,Good,Anju"])
        row = lead("919000003001")
        assert row.assigned_staff == "Anju"
        assert row.assigned_user_id == seeded["anju"]

    def test_case_variant_accepted(self, seeded):
        upload(seeded["admin"], ["919000003002,Case,anju"])
        assert lead("919000003002").assigned_user_id == seeded["anju"]

    def test_stored_as_typed_not_canonicalised(self, seeded):
        upload(seeded["admin"], ["919000003003,AsTyped,anju"])
        assert lead("919000003003").assigned_staff == "anju"

    def test_inactive_staff_accepted(self, seeded):
        upload(seeded["admin"], ["919000003004,Inactive,Old Staff"])
        assert lead("919000003004").assigned_user_id == seeded["gone"]

    def test_blank_owner_unchanged(self, seeded):
        upload(seeded["admin"], ["919000003005,Blank,"])
        row = lead("919000003005")
        assert row.assigned_staff is None and row.assigned_user_id is None

    def test_blank_does_not_clear_an_existing_owner(self, seeded):
        upload(seeded["admin"], ["919000003006,Owned,Anju"])
        upload(seeded["admin"], ["919000003006,Renamed,"])
        row = lead("919000003006")
        assert row.name == "Renamed"
        assert row.assigned_staff == "Anju"

    def test_no_warning_for_a_clean_file(self, seeded):
        html = upload(seeded["admin"], ["919000003007,Clean,Anju"])
        assert "is not a current staff member" not in html

    def test_reimport_is_idempotent(self, seeded):
        for _ in range(3):
            upload(seeded["admin"], ["919000003008,Same,Anju"])
        row = lead("919000003008")
        assert row.assigned_staff == "Anju"
        assert row.assigned_user_id == seeded["anju"]


class TestNoFkNullRowsFromImport:
    def test_an_import_can_no_longer_create_an_orphan(self, seeded):
        """The whole point: after H3-1B-c, no CSV import can leave a row with
        an owner string that resolves to nobody."""
        upload(seeded["admin"], [
            "919000004001,A,ghost",
            "919000004002,B,Ravi",
            "919000004003,C,Anju_display",
            "919000004004,D,Anju",
        ])
        with _APP.app_context():
            orphans = ConversationState.query.filter(
                ConversationState.assigned_staff.isnot(None),
                ConversationState.assigned_staff != "",
                ConversationState.assigned_user_id.is_(None)).all()
        assert orphans == [], [(o.phone, o.assigned_staff) for o in orphans]


# ═══ Scope containment ═══════════════════════════════════════════════════════

class TestScopeContainment:
    def _tree(self):
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            return ast.parse(fh.read())

    def test_import_warns_and_never_rejects_the_row(self):
        """CSV must not adopt the reject policy — that is the whole
        distinction between this phase and H3-1B-a."""
        fn = next(n for n in ast.walk(self._tree())
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "crm_leads_import")
        src = ast.unparse(fn)
        i = src.index("resolve_assignment")
        window = src[i:i + 600]
        assert "summary['errors'].append" in window
        assert "TaskError" not in window
        assert "400" not in window

    def test_seven_of_eight_write_paths_validate(self):
        """SEVEN of the eight. crm_unassigned_assign is still open.

        Recorded honestly rather than asserted away: the H3-1B discovery
        report listed crm_unassigned_assign in its write-path INVENTORY but
        omitted it from the reject/warn recommendation table, so H3-1B-a was
        scoped and approved against an incomplete list and wired four paths
        instead of five. H3-1B-c does not silently widen its own scope to
        cover it; the gap needs its own approval.

        Flip this to eight when that lands.
        """
        tree = self._tree()
        for name in ("crm_lead_new", "crm_lead_update",
                     "crm_auto_assign_confirm", "crm_reassignment_confirm",
                     "crm_leads_import"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            assert "resolve_assignment" in ast.unparse(fn), name
        src = open(os.path.join(ROOT, "app/services/task_service.py"),
                   encoding="utf-8").read()
        assert src.count("resolve_assignment") >= 2

        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "crm_unassigned_assign")
        assert "resolve_assignment" not in ast.unparse(fn), \
            "crm_unassigned_assign is now wired — update this test to assert 8/8"

    def test_dual_write_still_gated_and_present(self):
        fn = next(n for n in ast.walk(self._tree())
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "crm_leads_import")
        src = ast.unparse(fn)
        assert "_sync_assigned_user" in src
        assert "if 'assigned_staff' in changed:" in src.replace('"', "'")

    def test_unassigned_assign_route_untouched(self):
        """crm_unassigned_assign was never in H3-1B scope — its target comes
        from the same dropdown, but wiring it was not approved here."""
        fn = next(n for n in ast.walk(self._tree())
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "crm_unassigned_assign")
        assert "resolve_assignment" not in ast.unparse(fn)

    def test_no_schema_or_migration_change(self):
        versions = os.path.join(ROOT, "migrations", "versions")
        assert not [f for f in os.listdir(versions) if "h3" in f.lower()]

    def test_read_fk_untouched(self):
        from app import flags
        os.environ.pop("STAFF_IDENTITY_READ_FK", None)
        assert flags.staff_identity_read_fk_enabled() is False
