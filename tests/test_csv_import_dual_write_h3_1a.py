"""Phase H3-1A — CSV import dual-write fix.

THE BUG
-------
`assigned_staff` is in LEAD_IMPORT_WRITABLE, so crm_leads_import writes it —
but through `setattr(lead, field, val)` inside a loop rather than a static
attribute assignment. That is why every AST audit from RC2.3D onward missed
it: it was the EIGHTH assigned_staff write path and the only one that never
mirrored into assigned_user_id.

Every CSV import carrying an owner left the FK NULL, whether or not the name
was valid. Harmless while nothing reads the FK; after RC2.3E flips reads, a
NULL FK means the lead disappears from every per-staff view and shows only
under Unassigned. One import of a few hundred assigned rows would have
produced a few hundred invisible leads.

SCOPE — one bug, nothing else
-----------------------------
No validator wiring (H3-1B decides the reject-vs-warn policy), no other write
path, no reader migration, no schema, no flags. An INVALID owner still imports
exactly as it does today and still leaves the FK NULL — that is unchanged
behaviour, asserted below rather than assumed.

Import isolation follows test_assignment_validator_h3_0.py.
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

_DB = os.path.join(tempfile.gettempdir(), "phase_h31a_import.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "h31a-admin-key")
os.environ.setdefault("SECRET_KEY", "h31a-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "h31a-broadcast")
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
    """The fix is a no-op unless dual-write is ON — which it is in production
    (STAFF_IDENTITY_DUAL_WRITE=True since RC2.3D). Tests exercise the real
    configuration; test_noop_when_dual_write_is_off covers the other side."""
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
               "ravi": _mk(OTHER, "Ravi").id}
        admin = _mk(OX, "admin_ox", role="ADMIN")
        ids["admin"] = admin.id
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
    return r


def lead(phone, tenant=OX):
    with _APP.app_context():
        return ConversationState.query.filter_by(phone=phone,
                                                 tenant_id=tenant).first()


# ═══ The fix ═════════════════════════════════════════════════════════════════

class TestValidOwnerPopulatesTheFk:
    def test_new_lead_with_owner_gets_the_fk(self, seeded):
        """THE BUG. Before H3-1A this imported with assigned_user_id NULL."""
        upload(seeded["admin"], ["919000000001,Test One,Anju"])
        row = lead("919000000001")
        assert row is not None
        assert row.assigned_staff == "Anju"
        assert row.assigned_user_id == seeded["anju"]

    def test_existing_lead_updated_with_owner_gets_the_fk(self, seeded):
        with _APP.app_context():
            db.session.add(ConversationState(phone="919000000002",
                                             tenant_id=OX, lead_status="Lead"))
            db.session.commit()
        upload(seeded["admin"], ["919000000002,Test Two,Kiran"])
        row = lead("919000000002")
        assert row.assigned_staff == "Kiran"
        assert row.assigned_user_id == seeded["kiran"]

    def test_case_insensitive_owner_resolves(self, seeded):
        """Production stores both 'Anju' and 'anju'."""
        upload(seeded["admin"], ["919000000003,Test Three,anju"])
        assert lead("919000000003").assigned_user_id == seeded["anju"]

    def test_inactive_staff_resolves(self, seeded):
        """Consistent with dual-write everywhere else: an inactive member is
        a real user whose FK populates."""
        upload(seeded["admin"], ["919000000004,Test Four,Old Staff"])
        assert lead("919000000004").assigned_user_id == seeded["gone"]

    def test_many_rows_all_populate(self, seeded):
        """The scenario that motivated the fix: a bulk import."""
        rows = [f"91900001{i:04d},Bulk {i},{'Anju' if i % 2 else 'Kiran'}"
                for i in range(10)]
        upload(seeded["admin"], rows)
        with _APP.app_context():
            got = ConversationState.query.filter(
                ConversationState.phone.like("91900001%")).all()
        assert len(got) == 10
        assert all(r.assigned_user_id is not None for r in got)
        assert {r.assigned_user_id for r in got} == {seeded["anju"], seeded["kiran"]}


class TestUnchangedBehaviour:
    def test_blank_owner_leaves_the_lead_unassigned(self, seeded):
        """The loop skips falsy values ("blank == no opinion, never clear
        it"), so an import cannot unassign. Nothing to mirror."""
        upload(seeded["admin"], ["919000000010,No Owner,"])
        row = lead("919000000010")
        assert row.assigned_staff is None
        assert row.assigned_user_id is None

    def test_blank_owner_does_not_clear_an_existing_assignment(self, seeded):
        upload(seeded["admin"], ["919000000011,Owned,Anju"])
        assert lead("919000000011").assigned_user_id == seeded["anju"]
        upload(seeded["admin"], ["919000000011,Renamed,"])
        row = lead("919000000011")
        assert row.assigned_staff == "Anju", "import must not clear an owner"
        assert row.assigned_user_id == seeded["anju"]

    def test_invalid_owner_is_dropped_and_the_row_still_imports(self, seeded):
        """SUPERSEDED BY H3-1B-c.

        H3-1A asserted an unresolvable name imported as-is with a NULL FK,
        because at that point whether to refuse it was still H3-1B's open
        policy decision. That decision was made: CSV WARNS and drops the
        field. The row still imports — which is the part H3-1A cared about —
        but the bad owner is no longer written, so an import can no longer
        create a lead that becomes invisible under RC2.3E's FK reads.
        Full coverage lives in test_csv_import_warn_h3_1b_c.py.
        """
        upload(seeded["admin"], ["919000000012,Phantom,Anju_display"])
        row = lead("919000000012")
        assert row is not None, "the row must still import"
        assert row.name == "Phantom"
        assert row.assigned_staff is None
        assert row.assigned_user_id is None

    def test_foreign_tenant_owner_is_never_linked(self, seeded):
        """Naming another tenant's staff must never link to that user.

        H3-1B-c strengthened this: the name is now DROPPED rather than stored
        with a NULL FK, so the lead does not even carry a misleading owner.
        """
        upload(seeded["admin"], ["919000000013,Foreign,Ravi"])
        row = lead("919000000013")
        assert row.assigned_staff is None
        assert row.assigned_user_id is None
        assert row.tenant_id == OX

    def test_import_of_other_fields_does_not_touch_the_fk(self, seeded):
        """The sync is gated on `changed`, so an import that never writes an
        owner leaves the FK exactly as it was."""
        upload(seeded["admin"], ["919000000014,Original,Anju"])
        before = lead("919000000014").assigned_user_id
        upload(seeded["admin"], ["919000000014,Renamed"],
               header="phone,name")
        row = lead("919000000014")
        assert row.name == "Renamed"
        assert row.assigned_user_id == before == seeded["anju"]

    def test_reimport_is_idempotent(self, seeded):
        """The route's stated contract: the same file twice converges."""
        for _ in range(3):
            upload(seeded["admin"], ["919000000015,Same,Anju"])
        row = lead("919000000015")
        assert row.assigned_staff == "Anju"
        assert row.assigned_user_id == seeded["anju"]

    def test_noop_when_dual_write_is_off(self, seeded):
        """sync_assigned_user is flag-gated; with the flag OFF the import
        behaves exactly as it did before RC2.3D."""
        os.environ.pop("STAFF_IDENTITY_DUAL_WRITE", None)
        upload(seeded["admin"], ["919000000016,FlagOff,Anju"])
        row = lead("919000000016")
        assert row.assigned_staff == "Anju"
        assert row.assigned_user_id is None

    def test_import_stays_tenant_scoped(self, seeded):
        """A phone that exists in another tenant must not be touched."""
        with _APP.app_context():
            db.session.add(ConversationState(phone="919000000017",
                                             tenant_id=OTHER,
                                             lead_status="Lead",
                                             assigned_staff="Ravi"))
            db.session.commit()
        upload(seeded["admin"], ["919000000017,Mine,Anju"])
        mine = lead("919000000017", OX)
        theirs = lead("919000000017", OTHER)
        assert mine.assigned_user_id == seeded["anju"]
        assert theirs.assigned_staff == "Ravi"
        assert theirs.assigned_user_id is None


# ═══ Scope containment ═══════════════════════════════════════════════════════

def _tree():
    with open(os.path.join(ROOT, "app", "routes", "admin.py"),
              encoding="utf-8") as fh:
        return ast.parse(fh.read())


class TestScopeContainment:
    def test_the_gap_is_closed(self):
        """Replaces test_the_csv_import_dual_write_gap_is_still_open, which
        recorded this gap so closing it would be a visible, deliberate
        change rather than something that quietly stopped being true."""
        fn = next(n for n in ast.walk(_tree())
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "crm_leads_import")
        calls = {ast.unparse(c.func) for c in ast.walk(fn)
                 if isinstance(c, ast.Call)}
        assert "_sync_assigned_user" in calls

    def test_every_write_path_now_dual_writes(self):
        """All eight. This is the invariant RC2.3D's F2 finding asked for and
        that the import path silently broke."""
        tree = _tree()
        for name in ("crm_lead_new", "crm_lead_update", "crm_unassigned_assign",
                     "crm_auto_assign_confirm", "crm_reassignment_confirm",
                     "crm_leads_import"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            calls = {ast.unparse(c.func) for c in ast.walk(fn)
                     if isinstance(c, ast.Call)}
            assert "_sync_assigned_user" in calls, name
        from app.services import task_service
        src = open(task_service.__file__, encoding="utf-8").read()
        assert src.count("sync_assigned_user(task, tenant_id)") == 2

    def test_the_sync_is_gated_on_changed(self):
        """Ungated, an import touching only `name` would re-sync every row it
        read — a behaviour change outside this phase's scope."""
        fn = next(n for n in ast.walk(_tree())
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "crm_leads_import")
        src = ast.unparse(fn)
        assert "if 'assigned_staff' in changed:" in src.replace('"', "'")

    def test_the_sync_happens_before_the_commit(self):
        """Both columns must land in ONE transaction, as at the other seven
        write sites ("set before the commit so both columns land in one
        INSERT").

        Found by mutation testing: moving the sync AFTER db.session.commit()
        passed every behavioural test in this file, because SQLAlchemy keeps
        the row dirty and a later commit flushes it — the end state matches
        while the atomicity guarantee is gone. A per-row failure between the
        two commits would then leave the string written and the FK NULL,
        recreating the exact bug this phase fixes. Structural, because the
        property is about ordering rather than outcome.
        """
        fn = next(n for n in ast.walk(_tree())
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "crm_leads_import")
        syncs = [n.lineno for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and ast.unparse(n.func) == "_sync_assigned_user"]
        commits = [n.lineno for n in ast.walk(fn)
                   if isinstance(n, ast.Call)
                   and ast.unparse(n.func) == "db.session.commit"]
        assert syncs, "the dual-write call is gone"
        assert commits, "the commit is gone"
        assert min(syncs) < min(c for c in commits if c > min(syncs) - 50), \
            "dual-write must precede the commit it belongs to"

    def test_validator_is_now_wired_by_h3_1b_c(self):
        """INVERTED by H3-1B-c.

        H3-1A asserted the validator was NOT wired here, so that wiring it
        would be a deliberate, visible change rather than something that
        quietly happened. It has now been wired — as WARN-and-drop, not
        reject. Inverted rather than deleted so the suite still states what
        is true about this path.
        """
        fn = next(n for n in ast.walk(_tree())
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "crm_leads_import")
        src = ast.unparse(fn)
        assert "resolve_assignment" in src
        assert "summary['errors'].append" in src.replace('"', "'")

    def test_no_schema_or_migration_change(self):
        versions = os.path.join(ROOT, "migrations", "versions")
        assert not [f for f in os.listdir(versions) if "h3" in f.lower()]

    def test_read_fk_untouched(self):
        from app import flags
        os.environ.pop("STAFF_IDENTITY_READ_FK", None)
        assert flags.staff_identity_read_fk_enabled() is False
