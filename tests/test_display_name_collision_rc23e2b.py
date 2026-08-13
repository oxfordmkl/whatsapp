"""Phase RC2.3E-2B — display_name must be unique in the resolver's namespace.

THE DEFECT
----------
Add Staff validated only the CODE (username) namespace:

    if staff_service.resolve_code(_tenant, code, ...): -> "code already exists"

Edit Staff validated NOTHING about the name — a straight assignment, so an
admin could rename any staff member onto any other's label.

But resolve() reads the UNION of username and display_name. A write could
therefore be "unique" by the code check and still produce a label that
resolve() reports as ambiguous. Production holds exactly that:

    id=18  username='NIBU'    display_name='nibu'   ADMIN
    id=19  username='NIBU01'  display_name='nibu'   STAFF

'NIBU01' was a free code and the display name was never examined. Afterwards
'nibu' matches two users, resolve() refuses to guess, and every H3 write path
REJECTS assignment to that staff member — the dropdown offers a name the
validators will not accept.

WHY THIS IS THE COLLISION VALIDATION, NOT THE RESOLVER
------------------------------------------------------
Both rows carry the SAME display_name. Collapsing resolve() to a single
namespace would still return two matches, so the shared namespace is not what
creates the ambiguity. It is also deliberate: assigned_staff historically
holds display names while ownership checks compared usernames, and resolve()
bridges that. The defect is that nothing enforced uniqueness on the field the
whole identity system resolves on — not at the DB (only
uq_users_tenant_username exists), not on add, not on edit.

WHY NOT IMPLEMENTED WITH resolve()
----------------------------------
resolve() returns None BOTH when nothing matches and when several do. A
caller using it as a conflict check would read an EXISTING ambiguity as "no
conflict" — the opposite of the truth. display_name_conflict() returns the
offending user instead, so the error can name them.

Import isolation follows test_staff_management_stage1_rc22d.py.
"""
import ast
import os
import sys
import tempfile
import zlib

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23e2b_collision.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc23e2b-admin-key")
os.environ.setdefault("SECRET_KEY", "rc23e2b-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc23e2b-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User                                     # noqa: E402
from app.services import staff_service                                  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX = "t-ox"
OTHER = "t-other"
URL = "/crm/staff-management"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False


def _mk(tenant, username, role="STAFF", display_name=None, active=True):
    u = User(username=username,
             email=f"{username}.{tenant}@x.test".replace(" ", "_"),
             password_hash=generate_password_hash("pw"), role=role,
             tenant_id=tenant, is_active=active, display_name=display_name,
             require_password_change=False)
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
        ids = {
            "anju":  _mk(OX, "ANJU", display_name="Anju").id,
            "kiran": _mk(OX, "KIRAN", display_name="Kiran Kumar").id,
            # A user with NO display_name — its label is the username.
            "nisha": _mk(OX, "Nisha").id,
            "admin": _mk(OX, "admin_ox", role="ADMIN").id,
            # Another tenant, same strings: must NOT collide across tenants.
            "other": _mk(OTHER, "ANJU", display_name="Anju").id,
        }
        yield ids
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def add(uid, code, display_name, role="STAFF", active=True):
    data = {"action": "add", "staff_code": code,
            "display_name": display_name, "role": role}
    if active:
        data["active"] = "on"
    return client(uid).post(URL, data=data, follow_redirects=False)


def edit(uid, code, display_name, role="STAFF", active=True):
    data = {"action": "edit", "staff_code": code,
            "display_name": display_name, "role": role}
    if active:
        data["active"] = "on"
    return client(uid).post(URL, data=data, follow_redirects=False)


def rejected(resp):
    return "err=" in resp.headers.get("Location", "")


def user(username, tenant=OX):
    with _APP.app_context():
        return User.query.filter_by(username=username, tenant_id=tenant).first()


# ═══ ADD ═════════════════════════════════════════════════════════════════════

class TestAddStaff:

    def test_the_production_case_is_now_refused(self, seeded):
        """THE defect. A free code plus an already-taken name — exactly how
        NIBU01/'nibu' was created alongside NIBU."""
        _mk_code = "NIBU"
        with _APP.app_context():
            _mk(OX, _mk_code, display_name="nibu")
        r = add(seeded["admin"], "NIBU01", "nibu")
        assert rejected(r)
        assert user("NIBU01") is None, "the colliding staff member was created"

    def test_display_name_matching_another_display_name_is_refused(self, seeded):
        r = add(seeded["admin"], "ANJU2", "Anju")
        assert rejected(r)
        assert user("ANJU2") is None

    def test_display_name_matching_another_USERNAME_is_refused(self, seeded):
        """Cross-namespace: 'Nisha' is a username, not a display_name."""
        r = add(seeded["admin"], "NISHA2", "Nisha")
        assert rejected(r)
        assert user("NISHA2") is None

    @pytest.mark.parametrize("variant", ["anju", "ANJU", "  Anju  ", "AnJu"])
    def test_case_and_whitespace_insensitive(self, seeded, variant):
        """Deterministic codes, one variant per case.

        The first version looped inside one test using
        f"X{abs(hash(variant)) % 999}" as the code. hash() is salted per
        process, so the codes were nondeterministic, and asserting only
        "rejected" could not tell a NAME collision from a CODE collision —
        which is why a case-sensitivity mutant survived this test.
        """
        code = "ZZ" + str(abs(zlib.crc32(variant.encode())) % 9999)
        r = add(seeded["admin"], code, variant)
        assert rejected(r), variant
        assert user(code) is None, f"{variant!r} was created despite the clash"

    def test_a_unique_name_is_still_accepted(self, seeded):
        r = add(seeded["admin"], "RAVI", "Ravi Kumar")
        assert not rejected(r), r.headers.get("Location")
        u = user("RAVI")
        assert u is not None and u.display_name == "Ravi Kumar"

    def test_another_tenants_name_is_not_a_collision(self, seeded):
        """Tenant-scoped, like every other check on this screen. The other
        tenant also has ANJU/'Anju'; that must not block Oxford."""
        with _APP.app_context():
            other_admin = _mk(OTHER, "admin_other", role="ADMIN").id
        r = add(other_admin, "KIRAN", "Kiran Kumar")
        assert not rejected(r), r.headers.get("Location")
        assert user("KIRAN", tenant=OTHER) is not None

    def test_the_code_check_still_works(self, seeded):
        """Pre-existing validation must survive."""
        r = add(seeded["admin"], "ANJU", "Totally Different")
        assert rejected(r)

    def test_the_error_names_the_conflicting_staff(self, seeded):
        r = add(seeded["admin"], "ANJU2", "Anju")
        loc = r.headers.get("Location", "").replace("+", " ")
        assert "ANJU" in loc


# ═══ EDIT ════════════════════════════════════════════════════════════════════

class TestEditStaff:

    def test_renaming_onto_another_label_is_refused(self, seeded):
        r = edit(seeded["admin"], "KIRAN", "Anju")
        assert rejected(r)
        with _APP.app_context():
            assert User.query.get(seeded["kiran"]).display_name == "Kiran Kumar"

    def test_renaming_onto_another_USERNAME_is_refused(self, seeded):
        r = edit(seeded["admin"], "KIRAN", "Nisha")
        assert rejected(r)
        with _APP.app_context():
            assert User.query.get(seeded["kiran"]).display_name == "Kiran Kumar"

    def test_SELF_EXCLUSION_keeping_your_own_name_is_allowed(self, seeded):
        """The regression this check could easily have caused: a staff member
        must not collide with themselves."""
        r = edit(seeded["admin"], "KIRAN", "Kiran Kumar")
        assert not rejected(r), r.headers.get("Location")
        with _APP.app_context():
            assert User.query.get(seeded["kiran"]).display_name == "Kiran Kumar"

    def test_self_exclusion_is_case_insensitive(self, seeded):
        r = edit(seeded["admin"], "KIRAN", "kiran kumar")
        assert not rejected(r), r.headers.get("Location")

    def test_a_users_own_USERNAME_is_an_allowed_display_name(self, seeded):
        """Setting display_name to your own username is self-collision too."""
        r = edit(seeded["admin"], "KIRAN", "KIRAN")
        assert not rejected(r), r.headers.get("Location")

    def test_renaming_to_a_free_name_still_works(self, seeded):
        r = edit(seeded["admin"], "KIRAN", "Kiran S")
        assert not rejected(r), r.headers.get("Location")
        with _APP.app_context():
            assert User.query.get(seeded["kiran"]).display_name == "Kiran S"

    def test_blank_submission_is_unchanged_behaviour(self, seeded):
        """Pre-existing: a blank field falls back to the current label, so it
        changes nothing and cannot introduce a clash. Left alone by this
        phase — the fallback is why 'clear the display name' is not
        achievable through this screen."""
        r = edit(seeded["admin"], "KIRAN", "")
        assert not rejected(r), r.headers.get("Location")
        with _APP.app_context():
            assert User.query.get(seeded["kiran"]).display_name == "Kiran Kumar"

    def test_role_options_are_untouched(self, seeded):
        """Approved scope explicitly preserved the role dropdown."""
        r = edit(seeded["admin"], "KIRAN", "Kiran Kumar", role="ADMIN")
        assert not rejected(r)
        with _APP.app_context():
            assert User.query.get(seeded["kiran"]).role == "ADMIN"


# ═══ the helper itself ═══════════════════════════════════════════════════════

class TestHelper:

    def test_returns_the_conflicting_user(self, seeded):
        with _APP.app_context():
            u = staff_service.display_name_conflict(OX, "Anju")
            assert u is not None and u.username == "ANJU"

    @pytest.mark.parametrize("variant", ["anju", "ANJU", "  Anju  ", "AnJu",
                                         "nisha", "NISHA", " Nisha "])
    def test_helper_is_case_and_whitespace_insensitive(self, seeded, variant):
        """Direct on the helper — no route, no code, no DB-write ambiguity.

        Stored data is 'ANJU'/'Anju' and the username 'Nisha'. Every casing of
        either must collide. A mutant that lowercases only the STORED side
        (leaving the needle as typed) passes 'anju' and fails the rest, which
        is precisely the hole the route-level test could not see.
        """
        assert staff_service.display_name_conflict(OX, variant) is not None, variant

    def test_matches_usernames_too(self, seeded):
        with _APP.app_context():
            u = staff_service.display_name_conflict(OX, "Nisha")
            assert u is not None and u.username == "Nisha"

    def test_none_when_free(self, seeded):
        with _APP.app_context():
            assert staff_service.display_name_conflict(OX, "Brand New") is None

    def test_exclude_user_id_skips_that_row(self, seeded):
        with _APP.app_context():
            assert staff_service.display_name_conflict(
                OX, "Anju", exclude_user_id=seeded["anju"]) is None

    def test_tenant_scoped(self, seeded):
        with _APP.app_context():
            assert staff_service.display_name_conflict(OTHER, "Kiran Kumar") is None

    def test_blank_and_missing_tenant_are_not_conflicts(self, seeded):
        with _APP.app_context():
            assert staff_service.display_name_conflict(OX, "") is None
            assert staff_service.display_name_conflict(OX, None) is None
            assert staff_service.display_name_conflict(None, "Anju") is None

    def test_it_detects_an_EXISTING_ambiguity(self, seeded):
        """Why resolve() could not be reused: resolve() returns None for an
        existing duplicate, which a caller would read as 'no conflict'."""
        with _APP.app_context():
            _mk(OX, "NIBU", display_name="nibu")
            _mk(OX, "NIBU01", display_name="nibu")
            assert staff_service.resolve(OX, "nibu") is None      # ambiguous
            assert staff_service.display_name_conflict(OX, "nibu") is not None


# ═══ structural ══════════════════════════════════════════════════════════════

class TestStructure:

    def _fn(self, path, name):
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        m = ast.parse(ast.unparse(fn)).body[0]
        if (m.body and isinstance(m.body[0], ast.Expr)
                and isinstance(m.body[0].value, ast.Constant)):
            m.body.pop(0)
        return ast.unparse(m)

    def test_both_paths_call_the_check(self):
        src = self._fn(os.path.join(ROOT, "app/routes/admin.py"),
                       "crm_staff_management")
        assert src.count("display_name_conflict") == 2, \
            "add and edit must both validate"

    def test_edit_passes_exclude_user_id(self):
        src = self._fn(os.path.join(ROOT, "app/routes/admin.py"),
                       "crm_staff_management")
        assert "exclude_user_id=staff.id" in src

    def test_resolve_dual_namespace_preserved(self):
        """Approved scope: do NOT narrow the migration bridge."""
        src = self._fn(os.path.join(ROOT, "app/services/staff_service.py"),
                       "resolve")
        assert "username" in src and "display_name" in src

    def test_helper_does_not_delegate_to_resolve(self):
        src = self._fn(os.path.join(ROOT, "app/services/staff_service.py"),
                       "display_name_conflict")
        assert "resolve(" not in src, \
            "resolve() returns None for an existing ambiguity — see the docstring"

    def test_no_schema_or_migration_change(self):
        import subprocess
        out = subprocess.run(["git", "status", "--porcelain", "--", "migrations/"],
                             cwd=ROOT, capture_output=True, text=True).stdout.strip()
        assert out == "", out

    def test_display_name_still_has_no_db_constraint(self):
        """HONEST RECORD: this phase is application-level only. Option B (a
        partial unique index) was NOT taken, so the DB still permits a
        duplicate written by any other path. Update this when B lands."""
        assert User.__table__.c.display_name.unique in (None, False)
