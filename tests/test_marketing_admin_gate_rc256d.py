"""Phase RC2.5.6d (P2-14): /crm/marketing is ADMIN-only.

WHY
---
GET /crm/marketing carried no role gate. Its only check was check_auth(),
which under the production AUTH_MODE=SESSION_ONLY answers "is someone signed
in" and nothing else, so every STAFF session rendered the Marketing Hub --
contact import, the text-campaign Send button and the template-broadcast form
included. The RC2.5.6b second-tenant canary observed it live: a STAFF session
of the canary tenant received HTTP 200.

The RC2.5.6c investigation classified it UI/ROUTE EXPOSURE WITHOUT ACTION
PRIVILEGE: every send the page can reach was already refused independently --
/crm/marketing/start_job is @admin_required, and the legacy /broadcast*
endpoints authenticate an X-API-Key and are hard-scoped to PRIMARY_TENANT_ID.

The template's stale credential-shaped default (a placeholder, NOT the rotated
production key) is carried forward unchanged as a separate observation and is
deliberately outside this suite: RC2.5.6d ships the authorization fix only.

WHAT THIS SUITE PINS
--------------------
  1. STAFF gets 403 from GET /crm/marketing, on any tenant;
  2. ADMIN of either tenant still gets 200, with the page intact;
  3. an anonymous request is redirected to the sign-in page, never served;
  4. SUPER_ADMIN routing is unchanged: admin_security_guard still sends a
     non-impersonating platform operator back to /crm/super/dashboard, and an
     impersonating one is admitted exactly as before;
  5. the mutation below it stays refused: STAFF POST /crm/marketing/start_job
     is still 403, and nothing it could have written exists.

Import isolation and fixtures follow test_staff_role_allowlist_rc256a.py.
Every secret below is a dummy.
"""
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "rc256d_marketing_admin_gate.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc256d-admin-key")
os.environ.setdefault("SECRET_KEY", "rc256d-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc256d-broadcast")
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
from app.models import Campaign, ConversationMessage, Tenant, User           # noqa: E402

A, B = "t-a", "t-b"
URL = "/crm/marketing"
START_JOB = "/crm/marketing/start_job"

_APP = create_app()
_APP.config["TESTING"] = True
_APP.config["WTF_CSRF_ENABLED"] = False

# Routes resolve some imports at call time from sys.modules; re-pinning this
# module's own copies before every test makes a combined run behave exactly
# like CI's file-by-file run.
_OWN_MODULES = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN_MODULES)
    yield


def _mk(tenant, username, role="STAFF", active=True):
    u = User(username=username, display_name=username,
             email=f"{username}@{tenant}.rc256d.test",
             password_hash=generate_password_hash("pw"),
             role=role, tenant_id=tenant, is_active=active,
             require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u.id


@pytest.fixture()
def ids():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, name in ((A, "Tenant A"), (B, "Tenant B")):
            db.session.add(Tenant(id=tid, name=name, slug=f"rc256d-{tid}",
                                  status="ACTIVE", billing_exempt=True))
        db.session.commit()
        out = {
            "aadmin": _mk(A, "aadmin", role="ADMIN"),
            "astaff": _mk(A, "astaff"),
            "badmin": _mk(B, "badmin", role="ADMIN"),
            "bstaff": _mk(B, "bstaff"),
        }
        su = User(username="platform_root", email="root@rc256d.test",
                  password_hash=generate_password_hash("pw"), role="SUPER_ADMIN",
                  tenant_id=None, is_active=True)
        db.session.add(su)
        db.session.commit()
        out["super"] = su.id
    yield out
    with _APP.app_context():
        db.session.remove()
        db.drop_all()


def client_for(user_id, **session_extra):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
        s.update(session_extra)
    return c


def get(user_id, **session_extra):
    return client_for(user_id, **session_extra).get(URL, follow_redirects=False)


# ── 1. STAFF is refused ──────────────────────────────────────────────────────

class TestStaffRefused:

    @pytest.mark.parametrize("who", ["astaff", "bstaff"])
    def test_staff_gets_403(self, ids, who):
        assert get(ids[who]).status_code == 403

    def test_staff_refusal_renders_no_marketing_page(self, ids):
        body = get(ids["astaff"]).get_data(as_text=True)
        assert "apiKey" not in body
        assert "start_job" not in body

    def test_refusal_is_not_a_redirect_to_the_page(self, ids):
        """403, not a bounce an operator could follow back in."""
        r = get(ids["astaff"])
        assert r.status_code == 403
        assert "Location" not in r.headers

    def test_admin_key_query_param_cannot_lift_the_gate(self, ids):
        """SESSION_ONLY ignores ?key=, and the decorator runs before the view."""
        c = client_for(ids["astaff"])
        assert c.get(f"{URL}?key={os.environ['ADMIN_KEY']}").status_code == 403


# ── 2. ADMIN is unaffected ───────────────────────────────────────────────────

class TestAdminUnaffected:

    @pytest.mark.parametrize("who", ["aadmin", "badmin"])
    def test_admin_gets_200(self, ids, who):
        assert get(ids[who]).status_code == 200

    def test_admin_still_receives_the_marketing_page(self, ids):
        body = get(ids["aadmin"]).get_data(as_text=True)
        assert 'id="apiKey"' in body
        assert START_JOB in body

    def test_admin_of_a_non_primary_tenant_is_admitted(self, ids):
        """The gate is role-based, not primary-tenant-based: unlike /panel,
        this page carries no platform key, so tenant B's ADMIN keeps it."""
        assert get(ids["badmin"]).status_code == 200


# ── 3. anonymous ─────────────────────────────────────────────────────────────

class TestAnonymous:

    def test_anonymous_is_redirected_to_login_not_served(self):
        r = _APP.test_client().get(URL, follow_redirects=False)
        assert r.status_code in (301, 302, 303)
        assert "/crm/login" in (r.headers.get("Location") or "")


# ── 4. SUPER_ADMIN routing is unchanged ──────────────────────────────────────

class TestSuperAdminRoutingUnchanged:

    def test_non_impersonating_super_admin_is_sent_to_the_platform_console(self, ids):
        """admin_security_guard confines a non-impersonating SUPER_ADMIN to
        /crm/super/*. That happens BEFORE the view, and the decorator must not
        change it."""
        r = get(ids["super"])
        assert r.status_code in (301, 302, 303)
        assert "/crm/super/dashboard" in (r.headers.get("Location") or "")

    def test_impersonating_super_admin_is_admitted(self, ids):
        r = get(ids["super"], impersonate_tenant_id=A, impersonate_tenant_name="Tenant A")
        assert r.status_code == 200


# ── 5. the mutation below it stays refused ───────────────────────────────────

class TestStartJobStillRefused:

    def test_staff_post_start_job_is_403(self, ids):
        r = client_for(ids["astaff"]).post(
            START_JOB, json={"phones": ["919999000111"], "message": "x",
                             "campaign_name": "rc256d"})
        assert r.status_code == 403

    def test_refused_start_job_wrote_nothing(self, ids):
        client_for(ids["astaff"]).post(
            START_JOB, json={"phones": ["919999000111"], "message": "x",
                             "campaign_name": "rc256d"})
        with _APP.app_context():
            assert Campaign.query.count() == 0
            assert ConversationMessage.query.count() == 0

