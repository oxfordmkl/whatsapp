"""Phase RC2.5.6a (P0): staff management may assign only STAFF or ADMIN.

WHY
---
/crm/staff-management wrote the submitted `role` verbatim on both its ADD and
EDIT branches. The page's dropdown offers STAFF and ADMIN, but a crafted POST
is not bound by it, so a tenant ADMIN could set SUPER_ADMIN on a user of their
own tenant -- one whose password they had chosen at creation -- and sign in
with it at /crm/super/login as the PLATFORM operator: every tenant listed,
any tenant impersonated, that tenant's leads read.

WHAT THIS SUITE PINS
--------------------
  1. ADD and EDIT accept only STAFF and ADMIN; SUPER_ADMIN and any other
     non-blank value are refused, on each branch independently;
  2. a refusal writes nothing and audits nothing;
  3. valid behaviour is unchanged: STAFF/ADMIN creation, STAFF<->ADMIN edits,
     the blank-role fallbacks (STAFF on add, current role on edit), and the
     ROLE_CHANGE audit rows they already produced;
  4. the escalation chain is closed end to end: after a refused attempt the
     account still cannot sign in at /crm/super/login;
  5. tenant isolation of this screen is unchanged.

Import isolation and fixtures follow test_staff_management_stage2_rc22d.py.
Every secret below is a dummy.
"""
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "rc256a_staff_role_allowlist.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc256a-admin-key")
os.environ.setdefault("SECRET_KEY", "rc256a-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc256a-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-a")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.services.followup_service as _fs                                   # noqa: E402
import app.marketing.campaign_worker as _cw                                   # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from werkzeug.security import generate_password_hash                         # noqa: E402
from app import create_app                                                   # noqa: E402
from app.extensions import db                                                # noqa: E402
from app.models import AuditLog, Tenant, User                                # noqa: E402

A, B = "t-a", "t-b"
URL = "/crm/staff-management"
STAFF_PW = "Rc256a#Staff-pw"
INVALID_ROLES = ("SUPER_ADMIN", "super_admin", "OWNER", "admin", "staff",
                 "ROOT", "SUPER_ADMIN,ADMIN", "PLATFORM_ADMIN")

_APP = create_app()
_APP.config["TESTING"] = True
_APP.config["WTF_CSRF_ENABLED"] = False

# Routes resolve some imports at call time from sys.modules. Another test
# module that purges and re-imports `app` would otherwise leave those imports
# pointing at ITS copies; re-pinning this module's own copies before every
# test makes a combined run behave exactly like CI's file-by-file run.
_OWN_MODULES = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN_MODULES)
    yield


def _mk(tenant, username, role="STAFF", active=True, password="pw"):
    u = User(username=username, display_name=username,
             email=f"{username}@{tenant}.rc256a.test",
             password_hash=generate_password_hash(password),
             role=role, tenant_id=tenant, is_active=active,
             require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u.id


@pytest.fixture()
def ids():
    """Holds no app context across requests (see the 14B.1 fixture defect)."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, name in ((A, "Tenant A"), (B, "Tenant B")):
            db.session.add(Tenant(id=tid, name=name, slug=f"rc256a-{tid}",
                                  status="ACTIVE", billing_exempt=True))
        db.session.commit()
        out = {
            "aadmin": _mk(A, "aadmin", role="ADMIN"),
            "aadmin2": _mk(A, "aadmin2", role="ADMIN"),
            # A login-capable STAFF: a tenant ADMIN chose its password.
            "astaff": _mk(A, "astaff", password=STAFF_PW),
            "badmin": _mk(B, "badmin", role="ADMIN"),
            "bstaff": _mk(B, "bstaff"),
        }
        su = User(username="platform_root", email="root@rc256a.test",
                  password_hash=generate_password_hash("pw"), role="SUPER_ADMIN",
                  tenant_id=None, is_active=True)
        db.session.add(su)
        db.session.commit()
        out["super"] = su.id
    yield out
    with _APP.app_context():
        db.session.remove()


def client_for(user_id, **session_extra):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s.update(session_extra)
    return c


def post(user_id, **form):
    return client_for(user_id).post(URL, data=form, follow_redirects=False)


def users_snapshot():
    """Every column this screen can write, for every user."""
    with _APP.app_context():
        return sorted((u.id, u.username, u.display_name, u.role, u.is_active,
                       u.tenant_id, u.email, u.password_hash)
                      for u in User.query.all())


def audit_count():
    with _APP.app_context():
        return AuditLog.query.count()


def role_of(user_id):
    with _APP.app_context():
        return db.session.get(User, user_id).role


def refused(resp):
    return (resp.status_code in (302, 303)
            and "Invalid+role" in (resp.headers.get("Location") or "").replace("%20", "+"))


# ── 1-2. valid ADD behaviour is unchanged ────────────────────────────────────

class TestValidAdd:

    def test_staff_creation_remains_staff(self, ids):
        before = audit_count()
        r = post(ids["aadmin"], action="add", staff_code="NEWSTAFF",
                 display_name="New Staff", role="STAFF", active="on")
        assert r.status_code in (302, 303) and "msg=Staff+added" in r.headers["Location"]
        with _APP.app_context():
            u = User.query.filter_by(tenant_id=A, username="NEWSTAFF").one()
            assert u.role == "STAFF"
        assert audit_count() == before + 1

    def test_admin_creation_remains_admin(self, ids):
        r = post(ids["aadmin"], action="add", staff_code="NEWADMIN",
                 display_name="New Admin", role="ADMIN", active="on")
        assert "msg=Staff+added" in r.headers["Location"]
        with _APP.app_context():
            assert User.query.filter_by(tenant_id=A, username="NEWADMIN").one().role == "ADMIN"

    def test_blank_role_on_add_keeps_the_staff_fallback(self, ids):
        post(ids["aadmin"], action="add", staff_code="BLANKROLE",
             display_name="Blank Role", role="", active="on")
        with _APP.app_context():
            assert User.query.filter_by(tenant_id=A, username="BLANKROLE").one().role == "STAFF"

    def test_missing_role_on_add_keeps_the_staff_fallback(self, ids):
        post(ids["aadmin"], action="add", staff_code="NOROLE",
             display_name="No Role", active="on")
        with _APP.app_context():
            assert User.query.filter_by(tenant_id=A, username="NOROLE").one().role == "STAFF"


# ── 3, 5, 7, 8. ADD refuses everything else, with no write and no audit ─────

class TestAddRefusesOtherRoles:

    def test_super_admin_on_add_is_rejected(self, ids):
        snap, audits = users_snapshot(), audit_count()
        r = post(ids["aadmin"], action="add", staff_code="ESCALATE",
                 display_name="Escalate", role="SUPER_ADMIN", active="on")
        assert refused(r)
        assert users_snapshot() == snap, "a refused ADD wrote a user row"
        assert audit_count() == audits, "a refused ADD wrote an audit row"
        with _APP.app_context():
            assert User.query.filter_by(role="SUPER_ADMIN").count() == 1

    @pytest.mark.parametrize("bad", INVALID_ROLES)
    def test_unknown_role_on_add_is_rejected(self, ids, bad):
        snap, audits = users_snapshot(), audit_count()
        r = post(ids["aadmin"], action="add", staff_code="BADROLE",
                 display_name="Bad Role", role=bad, active="on")
        assert refused(r), bad
        assert users_snapshot() == snap, f"{bad!r}: a refused ADD wrote a user row"
        assert audit_count() == audits, f"{bad!r}: a refused ADD wrote an audit row"

    def test_whitespace_padded_super_admin_is_still_rejected(self, ids):
        snap = users_snapshot()
        r = post(ids["aadmin"], action="add", staff_code="PADDED",
                 display_name="Padded", role="  SUPER_ADMIN  ", active="on")
        assert refused(r)
        assert users_snapshot() == snap


# ── 4, 6, 7, 8. EDIT refuses everything else, with no write and no audit ────

class TestEditRefusesOtherRoles:

    def test_super_admin_on_edit_is_rejected(self, ids):
        snap, audits = users_snapshot(), audit_count()
        r = post(ids["aadmin"], action="edit", staff_code="ASTAFF",
                 display_name="Renamed", role="SUPER_ADMIN", active="")
        assert refused(r)
        assert role_of(ids["astaff"]) == "STAFF"
        assert users_snapshot() == snap, "a refused EDIT changed a user row"
        assert audit_count() == audits, "a refused EDIT wrote an audit row"

    @pytest.mark.parametrize("bad", INVALID_ROLES)
    def test_unknown_role_on_edit_is_rejected(self, ids, bad):
        snap, audits = users_snapshot(), audit_count()
        r = post(ids["aadmin"], action="edit", staff_code="ASTAFF",
                 display_name="Renamed", role=bad, active="on")
        assert refused(r), bad
        assert users_snapshot() == snap, f"{bad!r}: a refused EDIT changed a user row"
        assert audit_count() == audits, f"{bad!r}: a refused EDIT wrote an audit row"

    def test_an_admin_cannot_promote_themselves(self, ids):
        snap = users_snapshot()
        r = post(ids["aadmin"], action="edit", staff_code="AADMIN",
                 display_name="aadmin", role="SUPER_ADMIN", active="on")
        assert refused(r)
        assert role_of(ids["aadmin"]) == "ADMIN"
        assert users_snapshot() == snap

    def test_impersonating_super_admin_is_held_to_the_same_list(self, ids):
        snap = users_snapshot()
        r = client_for(ids["super"], impersonate_tenant_id=A).post(
            URL, data={"action": "edit", "staff_code": "ASTAFF",
                       "display_name": "astaff", "role": "SUPER_ADMIN", "active": "on"})
        assert refused(r)
        assert users_snapshot() == snap


# ── 9-10. valid EDIT behaviour is unchanged ──────────────────────────────────

class TestValidEdit:

    def test_staff_to_admin_remains_allowed(self, ids):
        before = audit_count()
        r = post(ids["aadmin"], action="edit", staff_code="ASTAFF",
                 display_name="astaff", role="ADMIN", active="on")
        assert "msg=Staff+updated" in r.headers["Location"]
        assert role_of(ids["astaff"]) == "ADMIN"
        assert audit_count() == before + 1
        with _APP.app_context():
            row = AuditLog.query.order_by(AuditLog.id.desc()).first()
            assert row.action == "ROLE_CHANGE" and '"to": "ADMIN"' in row.detail

    def test_admin_to_staff_remains_allowed(self, ids):
        r = post(ids["aadmin"], action="edit", staff_code="AADMIN2",
                 display_name="aadmin2", role="STAFF", active="on")
        assert "msg=Staff+updated" in r.headers["Location"]
        assert role_of(ids["aadmin2"]) == "STAFF"

    def test_blank_role_on_edit_keeps_the_current_role(self, ids):
        before = audit_count()
        post(ids["aadmin"], action="edit", staff_code="ASTAFF",
             display_name="Renamed Staff", role="", active="on")
        assert role_of(ids["astaff"]) == "STAFF"
        with _APP.app_context():
            assert db.session.get(User, ids["astaff"]).display_name == "Renamed Staff"
        assert audit_count() == before, "no role change, so no ROLE_CHANGE row"

    def test_last_admin_guard_still_applies(self, ids):
        post(ids["aadmin"], action="edit", staff_code="AADMIN2",
             display_name="aadmin2", role="STAFF", active="on")
        r = post(ids["aadmin"], action="edit", staff_code="AADMIN",
                 display_name="aadmin", role="STAFF", active="on")
        assert "only+active+admin" in r.headers["Location"].replace("%20", "+")
        assert role_of(ids["aadmin"]) == "ADMIN"


# ── the escalation chain, end to end ─────────────────────────────────────────

class TestEscalationChainIsClosed:

    def test_refused_promotion_leaves_super_login_closed(self, ids):
        post(ids["aadmin"], action="edit", staff_code="ASTAFF",
             display_name="astaff", role="SUPER_ADMIN", active="on")
        c = _APP.test_client()
        r = c.post("/crm/super/login",
                   data={"email": "astaff@t-a.rc256a.test", "password": STAFF_PW})
        assert "/crm/super/dashboard" not in (r.headers.get("Location") or "")
        assert c.get("/crm/super/dashboard").status_code != 200
        with _APP.app_context():
            assert User.query.filter_by(role="SUPER_ADMIN").count() == 1


# ── 11. tenant isolation of this screen is unchanged ─────────────────────────

class TestTenantIsolation:

    def test_cannot_edit_another_tenants_staff(self, ids):
        snap = users_snapshot()
        r = post(ids["aadmin"], action="edit", staff_code="BSTAFF",
                 display_name="hijacked", role="ADMIN", active="on")
        assert "Staff+not+found" in r.headers["Location"].replace("%20", "+")
        assert users_snapshot() == snap

    def test_add_lands_in_the_callers_tenant_only(self, ids):
        post(ids["badmin"], action="add", staff_code="BNEW",
             display_name="B New", role="STAFF", active="on")
        with _APP.app_context():
            rows = User.query.filter_by(username="BNEW").all()
            assert [r.tenant_id for r in rows] == [B]

    def test_other_tenant_untouched_by_a_refused_attempt(self, ids):
        with _APP.app_context():
            b_before = sorted((u.id, u.role) for u in User.query.filter_by(tenant_id=B))
        post(ids["aadmin"], action="edit", staff_code="BSTAFF",
             display_name="x", role="SUPER_ADMIN", active="on")
        with _APP.app_context():
            assert sorted((u.id, u.role) for u in User.query.filter_by(tenant_id=B)) == b_before
