"""Phase H3-0 — assigned_staff write validator.

DORMANT. No write path calls resolve_assignment() yet; H3-1 wires the eight.

THE DEFECT IT EXISTS TO CLOSE
-----------------------------
All eight assigned_staff write paths accept a free string. The ROW is
tenant-scoped everywhere (Phase 14B.2 C1), but the VALUE is checked against
nothing: an admin can store another tenant's staff name, a deleted member, or
'asdf'. Production already carries one such row ('Anju_display').

Today that is cosmetic. After RC2.3E flips reads to the FK it becomes
INVISIBLE — the FK is NULL, so the lead disappears from every per-staff view
and shows only under Unassigned. A lead someone believes is assigned stops
being shown to its owner.

THE PROPERTY THAT MATTERS MOST
------------------------------
test_agrees_with_sync_assigned_user. The validator delegates to the SAME
resolver dual-write uses. If it accepted a value the dual-write could not
resolve, validation would pass and the FK would still land NULL — strictly
worse than no validation. That test drives real rows through both and
compares.

Import isolation follows test_staff_identity_read_rc23e0.py.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_h30_validator.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "h30-admin-key")
os.environ.setdefault("SECRET_KEY", "h30-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "h30-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState                  # noqa: E402
from app.services import staff_identity_service as sid                  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX = "t-ox"
OTHER = "t-other"
_APP = create_app()

_JUNK = ["asdf", "12345", "Unassigned", "Anju Kumar", "anju@x.test",
         "'; DROP TABLE users; --", "<script>alert(1)</script>"]


@pytest.fixture(autouse=True)
def clean_flags():
    before = {k: os.environ.get(k) for k in
              ("STAFF_IDENTITY_READ_FK", "STAFF_IDENTITY_DUAL_WRITE")}
    for k in before:
        os.environ.pop(k, None)
    yield
    for k, v in before.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _mk(tenant, username, display=None, role="STAFF", active=True):
    u = User(username=username, display_name=display,
             email=f"{username}.{tenant}@x.test".replace(" ", "_"),
             password_hash=generate_password_hash("pw"),
             role=role, tenant_id=tenant, is_active=active,
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
        ids = {"anju": _mk(OX, "Anju").id,
               "kiran": _mk(OX, "Kiran").id,
               "gone": _mk(OX, "Old Staff", active=False).id,
               "admin": _mk(OX, "admin_ox", role="ADMIN").id,
               "ravi": _mk(OTHER, "Ravi").id}
        db.session.add(ConversationState(phone="+919000000001", tenant_id=OX,
                                         lead_status="Lead"))
        db.session.commit()
        yield ids
        db.session.remove()


# ═══ Accepting what should be accepted ═══════════════════════════════════════

class TestValidValues:
    def test_resolves_a_real_staff_member(self, seeded):
        with _APP.app_context():
            r = sid.resolve_assignment(OX, "Anju")
        assert r.ok is True
        assert r.user_id == seeded["anju"]
        assert r.canonical == "Anju"
        assert r.is_unassignment is False

    def test_case_and_whitespace_insensitive(self, seeded):
        with _APP.app_context():
            for v in ("anju", "ANJU", "  Anju  ", "AnJu"):
                r = sid.resolve_assignment(OX, v)
                assert r.ok is True, v
                assert r.user_id == seeded["anju"], v
                assert r.canonical == "Anju", v

    def test_accepts_inactive_staff(self, seeded):
        """Deliberate: an inactive member is a REAL user whose FK populates.
        crm_staff_management blocks deactivating someone who still holds
        leads, so inactive-but-assigned is an established state — and
        refusing it here would diverge from what dual-write writes."""
        with _APP.app_context():
            r = sid.resolve_assignment(OX, "Old Staff")
        assert r.ok is True
        assert r.user_id == seeded["gone"]

    def test_accepts_an_admin(self, seeded):
        """resolve_user_id's pool is every User in the tenant. An admin
        assigning a lead to themselves resolves, and the FK populates."""
        with _APP.app_context():
            r = sid.resolve_assignment(OX, "admin_ox")
        assert r.ok is True and r.user_id == seeded["admin"]


class TestBlankIsUnassignment:
    def test_blank_is_legal_not_a_failure(self, seeded):
        with _APP.app_context():
            for blank in (None, "", "   ", "\t"):
                r = sid.resolve_assignment(OX, blank)
                assert r.ok is True, repr(blank)
                assert r.user is None and r.user_id is None
                assert r.is_unassignment is True
                assert r.value is None and r.reason is None

    def test_blank_needs_no_tenant(self, seeded):
        """Clearing an owner is valid even with no tenant context."""
        with _APP.app_context():
            assert sid.resolve_assignment(None, "").ok is True

    def test_unassignment_is_distinguishable_from_failure(self, seeded):
        """A caller must be able to tell "clear it" from "I could not resolve
        this" — both have user_id None."""
        with _APP.app_context():
            blank = sid.resolve_assignment(OX, "")
            bad = sid.resolve_assignment(OX, "asdf")
        assert blank.user_id is bad.user_id is None
        assert blank.is_unassignment is True
        assert bad.is_unassignment is False
        assert blank.ok is not bad.ok


# ═══ Rejecting what should be rejected ═══════════════════════════════════════

class TestInvalidValues:
    def test_rejects_the_production_phantom(self, seeded):
        """'Anju_display' — the exact value that becomes INVISIBLE once
        RC2.3E flips reads to the FK."""
        with _APP.app_context():
            r = sid.resolve_assignment(OX, "Anju_display")
        assert r.ok is False
        assert r.user_id is None
        assert r.reason
        assert r.is_unassignment is False

    def test_rejects_junk(self, seeded):
        with _APP.app_context():
            for junk in _JUNK:
                assert sid.resolve_assignment(OX, junk).ok is False, junk

    def test_rejects_a_foreign_tenants_staff(self, seeded):
        """The row was always tenant-scoped; the VALUE was not."""
        with _APP.app_context():
            assert sid.resolve_assignment(OX, "Ravi").ok is False
            assert sid.resolve_assignment(OTHER, "Ravi").ok is True

    def test_rejection_carries_a_reason(self, seeded):
        with _APP.app_context():
            r = sid.resolve_assignment(OX, "nobody")
        assert isinstance(r.reason, str) and r.reason

    def test_missing_tenant_fails_closed(self, seeded):
        with _APP.app_context():
            r = sid.resolve_assignment(None, "Anju")
        assert r.ok is False and r.reason == "no tenant context"


# ═══ The correctness property ════════════════════════════════════════════════

class TestAgreesWithDualWrite:
    def test_agrees_with_sync_assigned_user(self, seeded):
        """THE property. If the validator accepted a value the dual-write
        could not resolve, validation would pass and the FK would still land
        NULL — worse than no validation. One resolver, so they cannot
        disagree. Driven through real rows, both regimes of the value space."""
        from app.services.staff_backfill_service import sync_assigned_user
        from app import flags
        os.environ["STAFF_IDENTITY_DUAL_WRITE"] = "true"
        with _APP.app_context():
            assert flags.staff_identity_dual_write_enabled() is True
            row = ConversationState.query.filter_by(
                phone="+919000000001").first()
            for value in (["Anju", "anju", "  Kiran ", "Old Staff",
                           "admin_ox", "Ravi", "Anju_display", "", None]
                          + _JUNK):
                verdict = sid.resolve_assignment(OX, value)
                row.assigned_staff = value
                sync_assigned_user(row, OX)
                assert row.assigned_user_id == verdict.user_id, (
                    f"{value!r}: validator says {verdict.user_id}, "
                    f"dual-write wrote {row.assigned_user_id}")
            db.session.rollback()

    def test_uses_the_same_resolver_not_a_copy(self):
        """AST: a second resolver would drift from dual-write."""
        with open(os.path.join(ROOT, "app", "services",
                               "staff_identity_service.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "resolve_assignment")
        src = ast.unparse(fn)
        assert "resolve_user_id" in src
        for reimpl in ("User.query.filter", ".lower()", "display_name =="):
            assert reimpl not in src, f"re-implemented matching: {reimpl}"


# ═══ Read-only, regime-independent ═══════════════════════════════════════════

class TestValidatorSemantics:
    def test_does_not_write(self, seeded):
        with _APP.app_context():
            row = ConversationState.query.filter_by(
                phone="+919000000001").first()
            before = row.assigned_staff
            sid.resolve_assignment(OX, "Anju")
            sid.resolve_assignment(OX, "asdf")
            db.session.expire_all()
            after = ConversationState.query.filter_by(
                phone="+919000000001").first().assigned_staff
        assert after == before

    def test_regime_independent(self, seeded):
        """Write-side validation asks the same question under both regimes."""
        with _APP.app_context():
            os.environ.pop("STAFF_IDENTITY_READ_FK", None)
            a = sid.resolve_assignment(OX, "Anju")
            os.environ["STAFF_IDENTITY_READ_FK"] = "true"
            b = sid.resolve_assignment(OX, "Anju")
        assert (a.ok, a.user_id, a.canonical) == (b.ok, b.user_id, b.canonical)

    def test_canonical_is_offered_not_applied(self, seeded):
        """Writing canonical would CHANGE stored values ('anju' -> 'Anju') —
        a behaviour change H3-1 must opt into per path, not inherit."""
        with _APP.app_context():
            r = sid.resolve_assignment(OX, "anju")
        assert r.value == "anju"
        assert r.canonical == "Anju"

    def test_repr_is_useful_and_leaks_nothing(self, seeded):
        with _APP.app_context():
            r = sid.resolve_assignment(OX, "Anju")
        text = repr(r)
        assert "Anju" in text and "ok=True" in text


# ═══ Dormancy and scope ══════════════════════════════════════════════════════

class TestDormancy:
    def test_no_write_path_calls_the_validator(self):
        """H3-1 wires the eight write paths. Until then, nothing."""
        offenders = []
        for dp, _d, fs in os.walk(os.path.join(ROOT, "app")):
            if "__pycache__" in dp:
                continue
            for f in fs:
                if not f.endswith(".py") or f == "staff_identity_service.py":
                    continue
                full = os.path.join(dp, f)
                try:
                    tree = ast.parse(open(full, encoding="utf-8").read())
                except SyntaxError:
                    continue
                for n in ast.walk(tree):
                    if isinstance(n, ast.Call) and \
                            ast.unparse(n.func).endswith("resolve_assignment"):
                        offenders.append(os.path.relpath(full, ROOT))
        assert offenders == [], f"write path migrated early: {offenders}"

    def test_the_csv_import_dual_write_gap_is_still_open(self):
        """H3 discovery found crm_leads_import writes assigned_staff through a
        dynamic setattr loop over LEAD_IMPORT_WRITABLE and never calls
        sync_assigned_user — the 8th write path, uncovered since before
        RC2.3D, which every earlier AST audit missed because the write is not
        a static attribute assignment.

        H3-1a closes it. Recording it here means closing it is a deliberate,
        visible change rather than something that quietly stops being true.
        """
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "crm_leads_import")
        calls = {ast.unparse(c.func) for c in ast.walk(fn)
                 if isinstance(c, ast.Call)}
        assert "_sync_assigned_user" not in calls, \
            "H3-1a has landed — flip this test to assert the gap is CLOSED"
        assert "assigned_staff" in ast.unparse(
            next(n for n in ast.walk(tree)
                 if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", "") == "LEAD_IMPORT_WRITABLE"
                         for t in n.targets)))

    def test_no_schema_or_migration_change(self):
        versions = os.path.join(ROOT, "migrations", "versions")
        assert not [f for f in os.listdir(versions) if "h3" in f.lower()]

    def test_validator_decides_no_policy(self):
        """It reports; the caller decides reject-vs-warn. The paths differ —
        a crafted JSON POST should probably be refused, while failing a whole
        CSV row over one bad cell may not be what an operator wants."""
        with open(os.path.join(ROOT, "app", "services",
                               "staff_identity_service.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "resolve_assignment")
        for n in ast.walk(fn):
            assert not isinstance(n, ast.Raise), "validator must not raise"
