"""Phase RC2.5.4c-x-6f2b-B1e+3: /panel authorization containment.

WHY
---
/panel returns the legacy broadcast panel with the platform-wide
BROADCAST_API_KEY injected into the page. That key sends WhatsApp templates
and text as the PRIMARY tenant, from anywhere, with no user attribution.
Before this phase the route only called check_auth(), which under the
production SESSION_ONLY mode means "any signed-in user": STAFF, any tenant,
users mid forced-password-change, and users of a suspended tenant whose
session was still live could all read the key.

WHAT THIS SUITE PINS
--------------------
  1. only a SUPER_ADMIN or an ADMIN of the primary tenant gets the page;
  2. every other signed-in user gets 403, and anonymous keeps its existing 403;
  3. the forced-password-change and billing guards now apply to /panel;
  4. who may load it is decided from the signed-in session only -- no request
     parameter, header or planted impersonation value can select the tenant;
  5. no /panel response may be cached (Cache-Control: no-store).

SECRET HYGIENE: the key is a dummy set below. Assertions compare booleans
only, so no key value can appear in a failure message.
"""
import ast
import inspect
import os
import sys
import tempfile
from datetime import datetime

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DUMMY_KEY = "b1e3-test-dummy-broadcast-key"
_DB = os.path.join(tempfile.gettempdir(), "rc254cx6f2b_b1e3_panel.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "testkey")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ["BROADCAST_API_KEY"] = _DUMMY_KEY
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ["PRIMARY_TENANT_ID"] = "t-primary"
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.services.followup_service as _fs                               # noqa: E402
import app.marketing.campaign_worker as _cw                               # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from flask import url_for                                                # noqa: E402
from werkzeug.security import generate_password_hash                     # noqa: E402

from app import create_app                                               # noqa: E402
from app.extensions import db                                            # noqa: E402
from app.models import Tenant, User                                      # noqa: E402

_APP = create_app()
_APP.config["TESTING"] = True
PRIMARY, OTHER = "t-primary", "t-other"
ANON_TEXT = "URL-il ?key=YOUR_ADMIN_KEY add cheyyuka"

USERS = [
    # username,  role,          tenant,  require_password_change
    ("padmin",   "ADMIN",       PRIMARY, False),
    ("pstaff",   "STAFF",       PRIMARY, False),
    ("oadmin",   "ADMIN",       OTHER,   False),
    ("ostaff",   "STAFF",       OTHER,   False),
    ("super",    "SUPER_ADMIN", None,    False),
    ("pchange",  "ADMIN",       PRIMARY, True),
    ("schange",  "STAFF",       PRIMARY, True),
    ("gone",     "ADMIN",       PRIMARY, False),
    ("noten",    "ADMIN",       None,    False),
]


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=PRIMARY, name="Primary", slug="b1e3-primary", status="ACTIVE"))
        db.session.add(Tenant(id=OTHER, name="Other", slug="b1e3-other", status="ACTIVE"))
        db.session.flush()
        for uname, role, tid, rpc in USERS:
            db.session.add(User(username=uname, email=uname + "@b1e3.test", display_name=uname,
                                password_hash=generate_password_hash("x"), role=role, tenant_id=tid,
                                is_active=True, require_password_change=rpc,
                                email_verified_at=datetime.utcnow()))
        db.session.commit()
        ids = {u.username: u.id for u in User.query.all()}
    yield ids
    with _APP.app_context():
        db.session.remove()


def client_for(ids, username, **session_extra):
    c = _APP.test_client()
    if username:
        with c.session_transaction() as s:
            s["_user_id"] = str(ids[username])
            s["_fresh"] = True
            s.update(session_extra)
    return c


def carries_key(resp):
    """True when the response body holds the broadcast key currently in force.
    Read at call time, so the check follows whatever app.config is loaded."""
    key = sys.modules["app.config"].BROADCAST_API_KEY
    return bool(key) and key in resp.get_data(as_text=True)


def no_store(resp):
    return "no-store" in (resp.headers.get("Cache-Control") or "")


def set_tenant_status(tid, status):
    with _APP.app_context():
        db.session.get(Tenant, tid).status = status
        db.session.commit()


# ── 1-2. the access matrix ───────────────────────────────────────────────────

class TestAccessMatrix:

    def test_anonymous_keeps_the_existing_denial(self, seeded):
        r = client_for(seeded, None).get("/panel")
        assert r.status_code == 403
        assert ANON_TEXT in r.get_data(as_text=True)
        assert carries_key(r) is False
        assert no_store(r)

    def test_primary_admin_gets_the_panel(self, seeded):
        r = client_for(seeded, "padmin").get("/panel")
        assert r.status_code == 200
        assert carries_key(r) is True, "an allowed user must still receive a working panel"
        assert no_store(r)

    def test_primary_staff_is_refused(self, seeded):
        r = client_for(seeded, "pstaff").get("/panel")
        assert r.status_code == 403
        assert carries_key(r) is False

    def test_non_primary_admin_is_refused(self, seeded):
        r = client_for(seeded, "oadmin").get("/panel")
        assert r.status_code == 403
        assert carries_key(r) is False

    def test_non_primary_staff_is_refused(self, seeded):
        r = client_for(seeded, "ostaff").get("/panel")
        assert r.status_code == 403
        assert carries_key(r) is False

    def test_super_admin_keeps_access(self, seeded):
        r = client_for(seeded, "super").get("/panel")
        assert r.status_code == 200
        assert carries_key(r) is True
        assert no_store(r)

    def test_super_admin_impersonating_another_tenant_keeps_access(self, seeded):
        r = client_for(seeded, "super", impersonate_tenant_id=OTHER).get("/panel")
        assert r.status_code == 200
        assert carries_key(r) is True

    def test_deactivated_user_is_refused(self, seeded):
        c = client_for(seeded, "gone")
        with _APP.app_context():
            db.session.get(User, seeded["gone"]).is_active = False
            db.session.commit()
        r = c.get("/panel")
        assert r.status_code == 403
        assert carries_key(r) is False

    def test_refusals_are_not_cacheable(self, seeded):
        for who in ("pstaff", "oadmin", "ostaff", "gone"):
            if who == "gone":
                with _APP.app_context():
                    db.session.get(User, seeded["gone"]).is_active = False
                    db.session.commit()
            assert no_store(client_for(seeded, who).get("/panel")), who


# ── 3. the forced-password-change and billing guards ─────────────────────────

class TestGuards:

    def _setup_url(self):
        with _APP.test_request_context():
            return url_for("admin.crm_setup_password")

    def test_forced_password_change_admin_is_sent_to_setup(self, seeded):
        r = client_for(seeded, "pchange").get("/panel")
        assert r.status_code == 302
        assert r.headers["Location"].endswith(self._setup_url())
        assert carries_key(r) is False
        assert no_store(r)

    def test_forced_password_change_staff_is_sent_to_setup(self, seeded):
        r = client_for(seeded, "schange").get("/panel")
        assert r.status_code == 302
        assert r.headers["Location"].endswith(self._setup_url())
        assert carries_key(r) is False

    def test_suspended_primary_tenant_admin_is_sent_to_billing(self, seeded):
        set_tenant_status(PRIMARY, "SUSPENDED")
        with _APP.test_request_context():
            billing = url_for("tenant.tenant_billing")
        r = client_for(seeded, "padmin").get("/panel")
        assert r.status_code == 302
        assert r.headers["Location"].endswith(billing)
        assert carries_key(r) is False
        assert no_store(r)

    def test_past_due_primary_tenant_admin_is_warned_but_served(self, seeded):
        """PAST_DUE only warns on CRM pages; /panel behaves the same."""
        set_tenant_status(PRIMARY, "PAST_DUE")
        r = client_for(seeded, "padmin").get("/panel")
        assert r.status_code == 200
        assert carries_key(r) is True

    def test_super_admin_is_not_blocked_by_a_tenant_billing_state(self, seeded):
        set_tenant_status(OTHER, "SUSPENDED")
        r = client_for(seeded, "super", impersonate_tenant_id=OTHER).get("/panel")
        assert r.status_code == 200


# ── 4. the tenant comes from the signed-in session, never from the request ───

class TestTenantComesFromTheSession:

    # Both values matter: an attacker names the primary tenant to claim it, or
    # names THEIR OWN tenant to make it count as "primary".
    @pytest.mark.parametrize("value", [PRIMARY, OTHER])
    @pytest.mark.parametrize("name", ["tenant_id", "tenant", "impersonate_tenant_id", "primary_tenant_id"])
    def test_a_tenant_parameter_is_ignored(self, seeded, name, value):
        r = client_for(seeded, "oadmin").get("/panel", query_string={name: value})
        assert r.status_code == 403
        assert carries_key(r) is False

    @pytest.mark.parametrize("value", [PRIMARY, OTHER])
    @pytest.mark.parametrize("header", ["X-Tenant-ID", "X-Tenant", "X-Primary-Tenant"])
    def test_a_tenant_header_is_ignored(self, seeded, header, value):
        r = client_for(seeded, "oadmin").get("/panel", headers={header: value})
        assert r.status_code == 403

    @pytest.mark.parametrize("query", [{"role": "SUPER_ADMIN"}, {"role": "ADMIN"}, {"key": "testkey"}])
    def test_a_role_or_key_parameter_is_ignored(self, seeded, query):
        for who in ("oadmin", "pstaff"):
            r = client_for(seeded, who).get("/panel", query_string=query)
            assert r.status_code == 403, who

    @pytest.mark.parametrize("value", [PRIMARY, OTHER])
    def test_a_planted_impersonation_value_does_not_help_a_non_super_user(self, seeded, value):
        for who in ("oadmin", "pstaff"):
            r = client_for(seeded, who, impersonate_tenant_id=value).get("/panel")
            assert r.status_code == 403, who

    @pytest.mark.parametrize("unset", ["", None])
    @pytest.mark.parametrize("who", ["padmin", "noten"])
    def test_without_a_primary_tenant_configured_no_tenant_admin_passes(self, seeded, unset, who):
        """Fail closed: with PRIMARY_TENANT_ID unset, an ADMIN whose tenant is
        also empty must not match 'no tenant' to 'no tenant'."""
        saved = _APP.config.get("PRIMARY_TENANT_ID")
        _APP.config["PRIMARY_TENANT_ID"] = unset
        try:
            r = client_for(seeded, who).get("/panel")
        finally:
            _APP.config["PRIMARY_TENANT_ID"] = saved
        assert r.status_code == 403
        assert carries_key(r) is False

    def test_an_admin_without_a_tenant_is_refused(self, seeded):
        r = client_for(seeded, "noten").get("/panel")
        assert r.status_code == 403

    def test_the_route_reads_no_request_parameter(self):
        """Source contract: the decision uses current_user and app config only.
        The one request read is X-Forwarded-Proto, for the displayed origin."""
        from app.routes import admin as admin_routes
        src = inspect.getsource(admin_routes.admin_panel)
        tree = ast.parse(src.lstrip())
        request_attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                         and isinstance(n.value, ast.Name) and n.value.id == "request"}
        assert request_attrs <= {"headers", "scheme", "host"}, request_attrs
        header_reads = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                        and ast.unparse(n.func) == "request.headers.get"]
        assert [ast.unparse(c.args[0]) for c in header_reads] == ["'X-Forwarded-Proto'"]
        assert "session" not in {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert 'current_app.config.get("PRIMARY_TENANT_ID")' in src
        # The tenant goes through the canonical resolver (H4), never read here.
        assert "_actor_tenant_id()" in src
        assert "'tenant_id'" not in ast.unparse(tree)
