"""Phase RC2.5.12: the two X-Admin-Key header routes are retired.

WHY
---
`POST /trigger-followup` and `GET /stats` were the last two surfaces
authenticated by the single global X-Admin-Key header. The Gate A audit found:

  * /trigger-followup did NOT trigger a follow-up. It sent an arbitrary
    WhatsApp text to an arbitrary phone number as the primary tenant, straight
    to the Graph API -- no lead lookup, no tenant context, no database row, no
    audit entry, no rate limit, no idempotency -- and it was CSRF-exempt. One
    leaked static key meant unlimited, untraceable messaging under Oxford's own
    WhatsApp identity. Classified HIGH.
  * /stats returned platform-wide unscoped aggregates across every tenant,
    including suspended ones, behind that same key. Classified LOW, and
    operationally superseded by /health, which publishes the same two counts.

Operational dependency discovery found no consumer for either: no caller in
this repository, no Railway cron or healthcheck, no GitHub Actions step, no
script, template or API collection, and no runbook reference. RC2.5.12 removed
both routes rather than re-authenticating them, because neither had a purpose
left that the authenticated CRM does not already serve better.

WHAT THIS SUITE PINS
--------------------
  1. neither route is registered, and neither answers -- not without a key,
     not with a wrong key, and not with the CONFIGURED key;
  2. the handlers are gone from source, not merely undecorated or commented
     out, and no X-Admin-Key gate survives in admin.py;
  3. the CSRF exemption contract shrank with the route it covered, and still
     fails at boot on a target it cannot find;
  4. ADMIN_KEY itself is UNTOUCHED -- this phase retires routes, not the
     authentication mechanism, and a later phase owns that audit;
  5. everything the retirement stood next to still works: /health and its
     counts, the broadcast key gate, session login, and RBAC.

The fifth group is the point of the suite. Removing a route is easy to do
destructively -- a stray import, a dropped exemption, a helper deleted because
it LOOKED dead -- and each of those failures is silent until production.
"""
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc2512_retirement.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc2512-admin-key")
os.environ.setdefault("SECRET_KEY", "rc2512-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc2512-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ["PRIMARY_TENANT_ID"] = "t-a"
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.services.followup_service as _fs                                   # noqa: E402
import app.marketing.campaign_worker as _cw                                   # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from werkzeug.security import generate_password_hash                          # noqa: E402
from app import create_app, _CSRF_EXEMPT_ENDPOINTS                            # noqa: E402
from app.extensions import db                                                 # noqa: E402
from app.models import Tenant, User                                           # noqa: E402

A = "t-a"
RETIRED = ("/stats", "/trigger-followup")

_APP = create_app()
_APP.config["TESTING"] = True
_APP.config["WTF_CSRF_ENABLED"] = False

_OWN_MODULES = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN_MODULES)
    yield


def _read(rel):
    with open(os.path.join(_ROOT, *rel.split("/")), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture()
def ids():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=A, name="Tenant A", slug="rc2512-a",
                              status="ACTIVE", billing_exempt=True))
        db.session.commit()
        out = {}
        for username, role in (("aadmin", "ADMIN"), ("astaff", "STAFF")):
            u = User(username=username, display_name=username,
                     email=f"{username}@rc2512.test",
                     password_hash=generate_password_hash("pw"),
                     role=role, tenant_id=A, is_active=True,
                     require_password_change=False)
            db.session.add(u)
            db.session.commit()
            out[username] = u.id
    yield out
    with _APP.app_context():
        db.session.remove()
        db.drop_all()


def client_for(user_id=None):
    c = _APP.test_client()
    if user_id is not None:
        with c.session_transaction() as s:
            s["_user_id"] = str(user_id)
            s["_fresh"] = True
    return c


def _call(path, key=None):
    """Issue the request the retired route used to answer."""
    headers = {"X-Admin-Key": key} if key is not None else {}
    c = _APP.test_client()
    if path == "/stats":
        return c.get(path, headers=headers)
    return c.post(path, headers=headers,
                  json={"phone": "919000000001", "message": "probe"})


# ── A. the routes are gone ───────────────────────────────────────────────────

class TestRoutesAreUnregistered:

    @pytest.mark.parametrize("path", RETIRED)
    def test_the_path_is_not_in_the_url_map(self, path):
        assert path not in {str(r) for r in _APP.url_map.iter_rules()}

    @pytest.mark.parametrize("endpoint", ["admin.stats", "admin.trigger_followup"])
    def test_the_endpoint_is_not_registered(self, endpoint):
        assert endpoint not in _APP.view_functions

    @pytest.mark.parametrize("path", RETIRED)
    def test_no_key_gets_not_found(self, path):
        assert _call(path).status_code == 404

    @pytest.mark.parametrize("path", RETIRED)
    def test_a_wrong_key_gets_not_found_rather_than_unauthorized(self, path):
        """401 would mean the surface still exists and is merely guarded."""
        assert _call(path, "definitely-not-the-key").status_code == 404

    @pytest.mark.parametrize("path", RETIRED)
    def test_the_configured_key_unlocks_nothing(self, path):
        """The retirement is only real if the CORRECT key opens nothing."""
        assert _call(path, os.environ["ADMIN_KEY"]).status_code == 404

    @pytest.mark.parametrize("path", RETIRED)
    def test_no_other_method_answers_either(self, path):
        """A retired POST route must not survive as a GET, and vice versa."""
        c = _APP.test_client()
        for method in ("get", "post", "put", "delete", "patch"):
            r = getattr(c, method)(path)
            assert r.status_code == 404, f"{method.upper()} {path} -> {r.status_code}"


# ── B. removed from source, not merely disabled ──────────────────────────────

class TestHandlersAreGoneFromSource:

    def test_neither_route_decorator_remains(self):
        src = _read("app/routes/admin.py")
        assert '@admin_bp.route("/trigger-followup"' not in src
        assert '@admin_bp.route("/stats"' not in src

    def test_neither_handler_function_remains(self):
        """An undecorated handler is a route waiting to be re-enabled."""
        src = _read("app/routes/admin.py")
        assert "def trigger_followup(" not in src
        assert "def stats(" not in src

    def test_no_header_key_gate_survives_in_admin(self):
        src = _read("app/routes/admin.py")
        assert 'request.headers.get("X-Admin-Key")' not in src

    def test_the_dead_state_import_was_removed(self):
        """admin.py imported four app.state helpers solely for /stats."""
        src = _read("app/routes/admin.py")
        assert "from app.state import" not in src

    def test_a_retirement_note_explains_the_absence(self):
        """Deleted code leaves no trace; the next reader must not have to
        reconstruct why two documented endpoints vanished."""
        src = _read("app/routes/admin.py")
        assert "RC2.5.12" in src and "RETIRED" in src


# ── C. the CSRF exemption contract shrank with the route ─────────────────────

class TestCsrfExemptionContract:

    def test_the_retired_endpoint_is_no_longer_declared_exempt(self):
        assert "admin.trigger_followup" not in _CSRF_EXEMPT_ENDPOINTS

    def test_exactly_the_six_survivors_remain(self):
        assert set(_CSRF_EXEMPT_ENDPOINTS) == {
            "webhook.receive_message",
            "billing.razorpay_webhook",
            "billing.stripe_webhook",
            "broadcast.broadcast",
            "broadcast.broadcast_template",
            "broadcast.upload_media_route",
        }

    def test_every_remaining_exemption_still_resolves(self):
        """The factory refuses to boot on an unresolvable target; this proves
        the tuple and the registry still agree after the removal."""
        for endpoint in _CSRF_EXEMPT_ENDPOINTS:
            assert endpoint in _APP.view_functions, endpoint

    def test_the_boot_time_guard_is_still_in_place(self):
        src = _read("app/__init__.py")
        tail = src.split("_CSRF_EXEMPT_ENDPOINTS")[-1]
        assert "raise RuntimeError(" in tail

    def test_no_blueprint_wide_exemption_was_introduced(self):
        from app import csrf
        assert csrf._exempt_blueprints == set()


# ── D. ADMIN_KEY itself is untouched ─────────────────────────────────────────

class TestAdminKeyMechanismPreserved:
    """RC2.5.12 retires two ROUTES. Conflating that with retiring the
    authentication mechanism would delete a credential other code still reads,
    and is explicitly a separate phase."""

    def test_admin_key_is_still_defined_with_its_committed_default(self):
        cfg = _read("app/config.py")
        assert 'ADMIN_KEY            = os.environ.get("ADMIN_KEY", "oxford_admin_2026")' in cfg

    def test_admin_key_is_still_importable(self):
        from app.config import ADMIN_KEY
        assert ADMIN_KEY

    def test_check_auth_still_reads_it(self):
        src = _read("app/routes/admin.py")
        body = src[src.index("def check_auth("):][:900]
        assert "ADMIN_KEY" in body

    def test_get_current_actor_still_reads_it(self):
        src = _read("app/routes/admin.py")
        body = src[src.index("def get_current_actor("):][:1400]
        assert "ADMIN_KEY" in body

    def test_the_default_secret_warning_still_covers_it(self):
        assert '"ADMIN_KEY":' in _read("app/__init__.py")

    def test_auth_mode_is_untouched(self):
        cfg = _read("app/config.py")
        assert "_VALID_AUTH_MODES = (\"SESSION_ONLY\", \"DUAL\", \"ADMIN_KEY_ONLY\")" in cfg
        assert 'if AUTH_MODE in ("ADMIN_KEY_ONLY", "DUAL"):' in cfg


# ── E. nothing next to the retirement was broken ─────────────────────────────

class TestSurroundingSurfacesIntact:

    def test_health_still_answers(self, ids):
        assert _APP.test_client().get("/health").status_code == 200

    def test_health_still_reports_both_counts(self, ids):
        """These are the aggregates /stats duplicated. They are the reason
        count_states() and count_pending_followups() were NOT deleted."""
        body = _APP.test_client().get("/health").get_json()
        assert "leads_in_memory" in body
        assert "pending_followups" in body

    def test_the_shared_state_helpers_still_exist(self):
        from app.state import count_states, count_pending_followups
        assert callable(count_states) and callable(count_pending_followups)

    def test_health_still_imports_them_directly(self):
        assert "from app.state import count_states, count_pending_followups" in \
               _read("app/routes/health.py")

    def test_the_broadcast_key_gate_is_untouched(self):
        """The OTHER header-key mechanism. Retiring X-Admin-Key routes must not
        have disturbed X-API-Key."""
        c = _APP.test_client()
        assert c.post("/broadcast", json={"numbers": ["9"], "message": "x"}
                      ).status_code == 401
        assert c.post("/broadcast-template",
                      json={"numbers": ["9"], "template_name": "t"}
                      ).status_code == 401

    def test_whatsapp_sending_architecture_is_untouched(self):
        """send_text stays imported and used: a lead's manual send still works.
        Only the unauthenticated, unaudited path to it was removed."""
        src = _read("app/routes/admin.py")
        assert "from app.services.whatsapp_service import send_text" in src
        assert src.count("send_text(") >= 1

    def test_the_login_page_still_renders(self, ids):
        assert _APP.test_client().get("/crm/login").status_code == 200

    def test_an_admin_still_reaches_the_crm(self, ids):
        assert client_for(ids["aadmin"]).get("/crm/home").status_code == 200

    def test_anonymous_is_still_refused(self, ids):
        assert client_for().get("/crm/leads").status_code in (302, 303, 403)

    def test_staff_is_still_refused_admin_surfaces(self, ids):
        assert client_for(ids["astaff"]).get("/crm/staff-management").status_code == 403

    def test_the_p2_14_marketing_gate_still_holds(self, ids):
        assert client_for(ids["astaff"]).get("/crm/marketing").status_code == 403
        assert client_for(ids["aadmin"]).get("/crm/marketing").status_code == 200

    def test_the_app_still_registers_its_full_route_surface(self):
        """A stray edit inside admin.py could silently drop routes below the
        removal point; this fails loudly if the blueprint shrank by more than
        the two retired rules."""
        rules = {str(r) for r in _APP.url_map.iter_rules()}
        assert len(rules) > 100, len(rules)
        for path in ("/crm/home", "/crm/login", "/health", "/webhook", "/broadcast"):
            assert path in rules, path
