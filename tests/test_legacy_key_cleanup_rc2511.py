"""Phase RC2.5.11 (P2-13, R3): the dead ?key= plumbing is gone.

WHY
---
Production runs AUTH_MODE=SESSION_ONLY, and app/config.py refuses to boot
under DUAL or ADMIN_KEY_ONLY when DEBUG is false, so check_auth() and
get_current_actor() ignore ?key= entirely. Despite that, 20 route handlers
still passed `key=request.args.get("key","")` into their templates and six
templates echoed it into 34 links, forms and fetch URLs -- every internal CRM
link carried an empty `key=` parameter, and two 403 pages still instructed
users to "add ?key=YOUR_ADMIN_KEY", advice that cannot work.

R3 removes that presentation plumbing ONLY. The authentication model is
untouched: check_auth(), get_current_actor(), ADMIN_KEY, AUTH_MODE and the
two X-Admin-Key header routes are all out of scope and pinned here as
UNCHANGED, so a later phase cannot quietly alter them under cover of cleanup.

RC2.5.12 later retired those two header routes deliberately, after its own
audit. Section F below is inverted in place to match, and check_auth(),
get_current_actor(), ADMIN_KEY and AUTH_MODE remain pinned UNCHANGED -- route
retirement is not authentication-mechanism removal, and this file still fails
if a phase conflates the two.

WHAT THIS SUITE PINS
--------------------
  1. none of the six cleaned templates emits a key query parameter, and none
     still builds one in Jinja or JavaScript;
  2. no route hands a `key` into render_template or a redirect;
  3. those pages still render, with their navigation intact;
  4. session/RBAC behaviour is unchanged -- anonymous is refused, STAFF is
     refused where it was before, ADMIN passes;
  5. the P2-14 marketing gate still holds;
  6. check_auth(), get_current_actor(), ADMIN_KEY, AUTH_MODE and the two
     header-key routes are exactly as they were.
"""
import os
import re
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc2511_legacy_key.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc2511-admin-key")
os.environ.setdefault("SECRET_KEY", "rc2511-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc2511-broadcast")
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
from app import create_app                                                    # noqa: E402
from app.extensions import db                                                 # noqa: E402
from app.models import Tenant, User                                           # noqa: E402

A = "t-a"
CLEANED = [
    "templates/crm_sidebar.html",
    "templates/crm_home.html",
    "templates/crm_home_staff.html",
    "templates/crm_notifications.html",
    "templates/crm_pipeline_stage.html",
    "templates/crm_sales_pipeline.html",
]
ADMIN_PY = os.path.join(_ROOT, "app", "routes", "admin.py")

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
        db.session.add(Tenant(id=A, name="Tenant A", slug="rc2511-a",
                              status="ACTIVE", billing_exempt=True))
        db.session.commit()
        out = {}
        for username, role in (("aadmin", "ADMIN"), ("astaff", "STAFF")):
            u = User(username=username, display_name=username,
                     email=f"{username}@rc2511.test",
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


# ── A. no template emits a key parameter ────────────────────────────────────

class TestTemplatesCarryNoKey:

    @pytest.mark.parametrize("rel", CLEANED)
    def test_no_key_query_parameter_is_emitted(self, rel):
        src = _read(rel)
        assert not re.search(r'[?&]key=', src), rel

    @pytest.mark.parametrize("rel", CLEANED)
    def test_no_jinja_expression_still_builds_one(self, rel):
        """Catches the indirect forms: {{ key }}, 'key=' ~ key, |tojson."""
        src = _read(rel)
        assert not re.search(r'\{\{ ?key ?\}\}', src), rel
        assert not re.search(r"'key=' ?~", src), rel
        assert not re.search(r'\bkey or\b', src), rel

    @pytest.mark.parametrize("rel", CLEANED)
    def test_no_javascript_helper_appends_one(self, rel):
        src = _read(rel)
        assert "encodeURIComponent(KEY)" not in src, rel
        assert not re.search(r'\bvar KEY\b', src), rel
        assert not re.search(r"'key=' \+", src), rel

    def test_the_sidebar_fetches_are_now_plain_paths(self):
        """The notification fetches used to be wrapped in qs(); they must now
        address the endpoint directly and still carry their CSRF header."""
        src = _read("templates/crm_sidebar.html")
        assert "qs(" not in src
        for path in ("/crm/notifications/recent", "/crm/notifications/unread-count",
                     "/crm/notifications/read-all"):
            assert f"fetch('{path}'" in src, path


# ── B. no route hands a key to a template or a redirect ─────────────────────

class TestRoutesPassNoKey:

    def test_no_render_template_receives_a_key_argument(self):
        src = _read("app/routes/admin.py")
        assert 'key=request.args.get("key", "")' not in src
        assert not re.search(r'^\s*key=key,\s*$', src, re.M)

    def test_no_redirect_propagates_a_key(self):
        src = _read("app/routes/admin.py")
        assert not re.search(r'url_for\([^)]*\bkey=', src)

    def test_the_only_remaining_key_reads_are_the_auth_comparisons(self):
        """Exactly two, both inside the authentication helpers this phase must
        not touch. A third would mean the plumbing crept back."""
        src = _read("app/routes/admin.py")
        reads = [l.strip() for l in src.splitlines()
                 if "request.args" in l and '"key"' in l]
        assert len(reads) == 2, reads
        assert all("ADMIN_KEY" in l for l in reads), reads

    def test_deny_pages_no_longer_advertise_the_parameter(self):
        src = _read("app/routes/admin.py")
        assert "YOUR_ADMIN_KEY" not in src
        assert src.count("Please sign in to continue.") == 2


# ── C. the cleaned pages still render ───────────────────────────────────────

class TestPagesStillRender:

    @pytest.mark.parametrize("path", ["/crm/home", "/crm/leads", "/crm/pipeline",
                                      "/crm/notifications"])
    def test_admin_pages_render(self, ids, path):
        r = client_for(ids["aadmin"]).get(path)
        assert r.status_code == 200, path

    def test_staff_home_renders(self, ids):
        r = client_for(ids["astaff"]).get("/crm/home")
        assert r.status_code == 200

    def test_rendered_navigation_survives_and_carries_no_key(self, ids):
        html = client_for(ids["aadmin"]).get("/crm/home").get_data(as_text=True)
        for href in ("/crm/home", "/crm/leads", "/crm/notifications"):
            assert f'href="{href}"' in html, href
        assert "key=" not in html

    def test_staff_home_carries_no_key_either(self, ids):
        html = client_for(ids["astaff"]).get("/crm/home").get_data(as_text=True)
        assert "key=" not in html


# ── D. session / RBAC behaviour is unchanged ────────────────────────────────

class TestAuthUnchanged:

    def test_anonymous_is_still_refused(self):
        r = client_for().get("/crm/leads")
        assert r.status_code in (302, 303, 403)

    def test_a_key_in_the_query_string_still_grants_nothing(self):
        """SESSION_ONLY ignored it before and must ignore it now -- including
        the real configured value."""
        r = client_for().get(f"/crm/leads?key={os.environ['ADMIN_KEY']}")
        assert r.status_code in (302, 303, 403)
        assert "crm-sidebar" not in r.get_data(as_text=True)

    def test_staff_is_still_refused_admin_surfaces(self, ids):
        assert client_for(ids["astaff"]).get("/crm/staff-management").status_code == 403

    def test_admin_still_passes(self, ids):
        assert client_for(ids["aadmin"]).get("/crm/staff-management").status_code == 200


# ── E. the P2-14 gate still holds ───────────────────────────────────────────

class TestP214Unchanged:

    def test_staff_is_refused_the_marketing_hub(self, ids):
        assert client_for(ids["astaff"]).get("/crm/marketing").status_code == 403

    def test_admin_reaches_the_marketing_hub(self, ids):
        assert client_for(ids["aadmin"]).get("/crm/marketing").status_code == 200

    def test_the_admin_required_decorator_is_still_applied(self):
        src = _read("app/routes/admin.py")
        i = src.index('@admin_bp.route("/crm/marketing", methods=["GET"])')
        assert "@admin_required" in src[i:i + 200]


# ── F. out-of-scope surfaces are untouched ──────────────────────────────────

def _fn_body(rel, name):
    """The WHOLE of one top-level function, not a fixed-width slice.

    REPAIRED BY RC2.5.13. These two checks originally sliced 900 and 1400
    characters from the `def`, which silently assumed the docstring would never
    grow. RC2.5.13 added explanatory lines to check_auth() and the assertion
    below fell off the end of the window -- a green-to-red flip with no change
    in behaviour whatsoever. Every assertion is unchanged; only the slice is,
    and it now covers the function however long its docstring gets.
    """
    src = _read(rel)
    start = src.index(f"def {name}(")
    rest = src[start + 1:]
    nxt = min((p for p in (rest.find("\ndef "), rest.find("\n@")) if p != -1),
              default=-1)
    return rest[:nxt] if nxt != -1 else rest


class TestOutOfScopeUntouched:

    def test_check_auth_still_has_its_three_modes(self):
        body = _fn_body("app/routes/admin.py", "check_auth")
        for mode in ("ADMIN_KEY_ONLY", "DUAL", "SESSION_ONLY"):
            assert mode in body, mode
        assert 'request.args.get("key", "") == ADMIN_KEY' in body

    def test_get_current_actor_still_resolves_the_key_source(self):
        body = _fn_body("app/routes/admin.py", "get_current_actor")
        assert 'is_key = request.args.get("key", "") == ADMIN_KEY' in body
        assert '"source": "ADMIN_KEY"' in body

    def test_the_two_header_key_routes_were_retired_by_a_later_phase(self):
        """RC2.5.11 left both header-key routes deliberately untouched, and
        this test pinned that. RC2.5.12 retired them outright after a Gate A
        audit, so the pin is inverted rather than deleted: R3's own cleanup
        must still not be what removes them, and nothing may reintroduce a
        header-key route into this file without a phase deciding to.
        """
        src = _read("app/routes/admin.py")
        assert 'request.headers.get("X-Admin-Key")' not in src
        for route in ('@admin_bp.route("/trigger-followup"',
                      '@admin_bp.route("/stats"'):
            assert route not in src, route

    def test_admin_key_and_auth_mode_config_are_untouched(self):
        cfg = _read("app/config.py")
        assert 'ADMIN_KEY            = os.environ.get("ADMIN_KEY", "oxford_admin_2026")' in cfg
        assert "_VALID_AUTH_MODES = (\"SESSION_ONLY\", \"DUAL\", \"ADMIN_KEY_ONLY\")" in cfg
        assert 'AUTH_MODE = os.environ.get(\'AUTH_MODE\', \'SESSION_ONLY\')' in cfg
        assert 'if AUTH_MODE in ("ADMIN_KEY_ONLY", "DUAL"):' in cfg
