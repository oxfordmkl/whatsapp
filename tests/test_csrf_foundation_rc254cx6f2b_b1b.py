"""Phase RC2.5.4c-x-6f2b-B1b: CSRF factory initialisation and the exemption contract.

WHAT THIS PHASE IS
-------------------
The platform had no CSRF mechanism (x-6f2b-A). B1a pinned Flask-WTF and left
it unused; B1b initialises CSRFProtect in the application factory and exempts
exactly seven non-browser endpoints. B1c put the token into every browser POST
(38 forms, 10 protected fetch calls); B1b/B1c/B1d ship together because
enforcement without tokens would lock every operator out of the CRM.

So these tests prove three things:
  1. enforcement exists, runs BEFORE any view body, and rejects missing or
     invalid tokens (TestEnforcement);
  2. exactly seven endpoints are exempt, by name, with no blueprint-wide or
     wildcard exemption anywhere (TestExemptionContract);
  3. the pre-existing authenticity model -- Meta HMAC, the API-key headers --
     and tenant isolation are untouched (TestAuthenticityPreserved,
     TestTenantIsolationUnaffected).

B1d inverted this suite's own B1c tripwire (templates carried no token yet)
into the final token contract, and the rc254c tripwire that asserted
CSRFProtect was ABSENT into one that pins its presence.
"""
import ast
import os
import subprocess
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc254cx6f2b_b1b_csrf.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "testkey")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ.setdefault("BROADCAST_API_KEY", "testbroadcast")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.services.followup_service as _fs                               # noqa: E402
import app.marketing.campaign_worker as _cw                               # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from app import create_app, csrf, _CSRF_EXEMPT_ENDPOINTS                  # noqa: E402
from app.extensions import db                                            # noqa: E402

# CSRF stays ENABLED here on purpose: this suite exists to prove enforcement.
_APP = create_app()
_APP.config["TESTING"] = True

EXPECTED_EXEMPT = {
    "webhook.receive_message",
    "billing.razorpay_webhook",
    "billing.stripe_webhook",
    "broadcast.broadcast",
    "broadcast.broadcast_template",
    "broadcast.upload_media_route",
}
# Phase RC2.5.12 retired admin.trigger_followup, taking the seventh exemption
# with it. The count assertion below is the point of this constant: a NEW
# exemption must never appear unnoticed, and a removed one must be removed
# deliberately, here, alongside its route.

# Endpoints that must NEVER be exempt -- browser/session surfaces, including
# the public auth forms the audit recommended protecting.
MUST_BE_PROTECTED = [
    "public.register", "admin.crm_login", "admin.crm_super_login",
    "public.forgot_password", "public.resend_verification",
    "public.reset_password", "admin.crm_lead_update", "admin.crm_lead_new",
    "admin.crm_staff_management", "admin.crm_notifications_read_all",
    "tenant.tenant_profile", "tenant.tenant_course_create",
    "tenant.tenant_whatsapp_save", "marketing.create_campaign",
]


def _src(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _head(rel):
    out = subprocess.run(["git", "show", "HEAD:" + rel], cwd=_ROOT,
                         capture_output=True)
    return "\n".join(out.stdout.decode("utf-8").splitlines())


def _work(rel):
    return "\n".join(_src(rel).splitlines())


def _func(src, name):
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    return None


@pytest.fixture()
def client():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
    yield _APP.test_client()
    with _APP.app_context():
        db.session.remove()


def _token(client):
    """A valid session-scoped token bound to THIS client's session.

    generate_csrf() stores the raw secret in the Flask session and returns the
    signed token; validation compares the two. So the raw value has to be
    planted in the test client's own session, or the token belongs to nobody.
    """
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf
    with _APP.test_request_context():
        token = generate_csrf()
        raw = flask_session["csrf_token"]
    with client.session_transaction() as sess:
        sess["csrf_token"] = raw
    return token


# ── the extension is initialised, exactly once ──────────────────────────────

class TestInitialisation:

    def test_csrfprotect_is_initialised(self):
        assert "csrf" in _APP.extensions
        from flask_wtf.csrf import CSRFProtect
        assert isinstance(csrf, CSRFProtect)

    def test_the_extension_is_registered_exactly_once(self):
        """One app-wide before_request hook, not one per create_app() call."""
        app_wide = _APP.before_request_funcs.get(None, [])
        assert len(app_wide) == 1, f"expected one app-wide hook, got {app_wide}"

    def test_a_second_app_initialises_cleanly(self):
        other = create_app()
        assert "csrf" in other.extensions
        assert len(other.before_request_funcs.get(None, [])) == 1

    def test_enforcement_is_enabled(self):
        assert _APP.config.get("WTF_CSRF_ENABLED") is True

    def test_the_token_is_session_scoped_not_time_limited(self):
        """The CRM is long-lived and multi-tab; a token that expires while the
        operator is still logged in fails a form that looks fine. The token
        still dies with the session (logout calls session.clear())."""
        assert _APP.config.get("WTF_CSRF_TIME_LIMIT") is None

    def test_no_module_level_app_was_created_for_csrf(self):
        """The singleton is the EXTENSION, not an app."""
        tree = ast.parse(_src("app/__init__.py"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                fn = node.value.func
                name = getattr(fn, "id", getattr(fn, "attr", ""))
                assert name != "Flask", "a module-level Flask app was created"

    def test_routes_are_unchanged(self):
        """115 until RC2.5.12 retired GET /stats and POST /trigger-followup.

        The number is the point: adding CSRF must not register, unregister or
        reshape a single route, and any future change to the surface has to
        come here and say so. Two were removed deliberately, so this is 113.
        """
        # RC2.5.19-E: 113 -> 116, three approved Embedded Signup routes:
        # /tenant/whatsapp/es/start, /tenant/whatsapp/es/complete,
        # /tenant/whatsapp/es/activate.
        assert len(list(_APP.url_map.iter_rules())) == 116


# ── enforcement happens before any view body ────────────────────────────────

class TestEnforcement:

    def test_a_protected_post_without_a_token_is_rejected(self, client):
        r = client.post("/crm/notifications/read-all")
        assert r.status_code == 400

    def test_an_invalid_token_is_rejected(self, client):
        r = client.post("/crm/notifications/read-all",
                        data={"csrf_token": "not-a-real-token"})
        assert r.status_code == 400

    def test_a_token_from_another_session_is_rejected(self, client):
        stolen = _token(client)
        other = _APP.test_client()          # a different session
        _token(other)                       # ...with its own, different secret
        r = other.post("/crm/notifications/read-all",
                       data={"csrf_token": stolen})
        assert r.status_code == 400

    def test_a_valid_token_passes_the_csrf_layer(self, client):
        """It must then meet authentication/authorisation on its own merits --
        a valid token is never authority, only origin."""
        r = client.post("/crm/notifications/read-all",
                        data={"csrf_token": _token(client)})
        assert r.status_code != 400, "a valid token was rejected by CSRF"

    def test_rejection_precedes_the_business_mutation(self, client):
        """The lead-update route commits; a tokenless POST must not reach it."""
        from app.models import ConversationState, Tenant
        with _APP.app_context():
            db.session.add(Tenant(id="t-ox", name="Oxford", slug="ox",
                                  status="ACTIVE", billing_exempt=True))
            db.session.add(ConversationState(phone="+911", tenant_id="t-ox",
                                             name="Probe", _stage="new",
                                             lead_status="Lead", notes=None))
            db.session.commit()
        r = client.post("/crm/lead/+911/update",
                        data={"lead_status": "Enrolled", "notes": "hacked"})
        assert r.status_code == 400
        with _APP.app_context():
            row = ConversationState.query.filter_by(phone="+911").one()
            assert row.lead_status == "Lead", "the view body ran before CSRF"
            assert row.notes is None

    def test_enforcement_is_a_before_request_hook_not_route_code(self):
        """No route may perform its own CSRF check."""
        for base, _dirs, files in os.walk(os.path.join(_ROOT, "app", "routes")):
            for name in files:
                if name.endswith(".py"):
                    rel = os.path.relpath(os.path.join(base, name), _ROOT) \
                        .replace(os.sep, "/")
                    assert "validate_csrf" not in _src(rel), rel

    def test_get_requests_are_not_blocked(self, client):
        assert client.get("/health").status_code == 200


# ── the exemption contract ──────────────────────────────────────────────────

class TestExemptionContract:

    def test_exactly_seven_endpoints_are_declared_exempt(self):
        """Six since RC2.5.12 retired admin.trigger_followup. The NAME is kept
        deliberately: tests/test_csrf_templates_rc254cx6f2b_b1c.py pins it by
        name as part of the B1b contract, and renaming it would let the
        contract check pass while this assertion had quietly disappeared."""
        assert set(_CSRF_EXEMPT_ENDPOINTS) == EXPECTED_EXEMPT
        assert len(_CSRF_EXEMPT_ENDPOINTS) == 6

    def test_the_registered_exempt_views_match_the_declaration(self):
        registered = {v.rsplit(".", 1)[-1] for v in csrf._exempt_views}
        expected = {e.split(".")[-1] for e in EXPECTED_EXEMPT}
        assert registered == expected, f"registered={sorted(registered)}"

    def test_no_blueprint_is_exempt(self):
        """A blueprint-wide exemption would unprotect every session-
        authenticated CRM route sharing that blueprint."""
        assert csrf._exempt_blueprints == set(), csrf._exempt_blueprints

    def test_no_wildcard_or_prefix_exemption_exists(self):
        src = _src("app/__init__.py")
        for banned in ("exempt_blueprint", 'csrf.exempt(admin_bp',
                       "csrf.exempt(app", "*", "startswith("):
            if banned == "*":
                block = src.split("_CSRF_EXEMPT_ENDPOINTS")[1].split(")")[0]
                assert "*" not in block
            else:
                assert banned not in src, banned

    @pytest.mark.parametrize("endpoint", MUST_BE_PROTECTED)
    def test_browser_endpoints_are_not_exempt(self, endpoint):
        view = _APP.view_functions[endpoint]
        dotted = f"{view.__module__}.{view.__name__}"
        assert dotted not in csrf._exempt_views, endpoint
        assert endpoint not in _CSRF_EXEMPT_ENDPOINTS

    def test_public_auth_forms_are_protected(self):
        """Unauthenticated does not mean exempt -- the audit recommended CSRF
        on the public auth forms too."""
        for endpoint in ("public.register", "admin.crm_login",
                         "public.reset_password"):
            assert endpoint not in _CSRF_EXEMPT_ENDPOINTS

    def test_a_missing_exemption_target_fails_at_boot(self):
        """A renamed endpoint must not degrade into 'silently not exempt'."""
        src = _src("app/__init__.py")
        assert "raise RuntimeError(" in src.split("_CSRF_EXEMPT_ENDPOINTS")[-1]


# ── the exempt endpoints really are reachable without a token ───────────────

class TestExemptEndpointsAcceptTokenlessPosts:
    """Each must fail on its OWN authenticity check (403/401), never on CSRF
    (400) -- which proves both that the exemption works and that the
    pre-existing mechanism still runs."""

    def test_meta_webhook_is_exempt_and_still_signature_checked(self, client):
        r = client.post("/webhook", json={"entry": []})
        assert r.status_code == 403, "expected the Meta HMAC gate, not CSRF"

    @pytest.mark.parametrize("path", ["/webhooks/razorpay", "/webhooks/stripe"])
    def test_billing_webhooks_are_exempt(self, client, path):
        r = client.post(path, json={"event": "probe"})
        assert r.status_code != 400, "a provider webhook was blocked by CSRF"

    @pytest.mark.parametrize("path", ["/broadcast", "/broadcast-template"])
    def test_broadcast_endpoints_are_exempt_and_still_key_checked(self, client, path):
        r = client.post(path, json={})
        assert r.status_code == 401, "expected X-API-Key auth, not CSRF"

    def test_upload_media_is_exempt_and_still_key_checked(self, client):
        r = client.post("/upload-media", data={})
        assert r.status_code == 401

    def test_trigger_followup_is_retired_and_exempts_nothing(self, client):
        """RC2.5.12. A tokenless POST must now fail on the route not existing
        (404) rather than on its key gate (401) -- and critically NOT on CSRF
        (400), which would mean a phantom exemption entry had been left behind
        for a path that no longer has an authenticity mechanism of its own."""
        r = client.post("/trigger-followup", json={})
        assert r.status_code == 404, r.status_code
        assert "admin.trigger_followup" not in _CSRF_EXEMPT_ENDPOINTS


# ── the pre-existing authenticity model is untouched ────────────────────────

class TestAuthenticityPreserved:

    @pytest.mark.parametrize("rel", [
        "app/routes/webhook.py", "app/routes/broadcast.py",
        "app/routes/billing.py", "app/routes/admin.py", "app/routes/tenant.py",
        "app/routes/public.py", "app/routes/marketing.py",
    ])
    def test_no_route_file_was_modified_by_this_phase(self, rel):
        assert _head(rel) == _work(rel), f"{rel} changed -- B1b touches only the factory"

    def test_meta_signature_verification_is_byte_identical(self):
        assert _func(_head("app/routes/webhook.py"), "verify_meta_signature") == \
               _func(_work("app/routes/webhook.py"), "verify_meta_signature")

    def test_meta_secret_still_fails_closed(self):
        src = _func(_work("app/routes/webhook.py"), "verify_meta_signature")
        assert "if not secret:" in src and "return False" in src

    def test_api_key_checks_remain(self):
        src = _src("app/routes/broadcast.py")
        # Four checks: /broadcast, /broadcast-template, /upload-media and the
        # /templates listing route.
        assert src.count('request.headers.get("X-API-Key") != BROADCAST_API_KEY') == 4
        # RC2.5.12: admin.py carried exactly two X-Admin-Key gates, one per
        # retired route. Both routes are gone, so no gate remains to preserve.
        # Asserted as ZERO rather than deleted: a header-key surface must not
        # reappear in this file without a phase deciding to put it there.
        assert 'request.headers.get("X-Admin-Key")' not in \
               _src("app/routes/admin.py")


# ── CSRF is orthogonal to tenant isolation and authorisation ────────────────

class TestTenantIsolationUnaffected:

    def test_the_csrf_block_never_touches_tenancy_or_identity(self):
        """Scoped to the CSRF code ITSELF -- the rest of the factory (CLI
        commands, provisioning) legitimately mentions tenants, and slicing to
        end-of-file would match those instead."""
        src = _src("app/__init__.py")
        declaration = src[src.index("_CSRF_EXEMPT_ENDPOINTS = ("):]
        declaration = declaration[:declaration.index(")\n") + 1]
        init = src[src.index('app.config.setdefault("WTF_CSRF_TIME_LIMIT"'):]
        init = init[:init.index("csrf.exempt(_view)") + len("csrf.exempt(_view)")]
        block = declaration + init
        for banned in ("tenant_id", "tenant_query", "tenant_filter",
                       "_get_current_tenant", "_actor_tenant_id",
                       "current_user", "role"):
            assert banned not in block, f"CSRF code references {banned}"

    @pytest.mark.parametrize("name,rel", [
        ("tenant_query", "app/routes/admin.py"),
        ("tenant_filter", "app/routes/admin.py"),
        ("_actor_tenant_id", "app/routes/admin.py"),
        ("_get_current_tenant", "app/routes/tenant.py"),
        ("tenant_admin_required", "app/routes/tenant.py"),
        ("admin_required", "app/routes/admin.py"),
        ("super_admin_required", "app/routes/admin.py"),
        ("check_auth", "app/routes/admin.py"),
    ])
    def test_authorisation_and_tenant_helpers_are_byte_identical(self, name, rel):
        assert _func(_head(rel), name) == _func(_work(rel), name)

    def test_csrf_does_not_read_tenant_from_the_request(self):
        """A token proves origin, never which tenant a row belongs to."""
        from flask_wtf.csrf import CSRFProtect
        import inspect
        src = inspect.getsource(CSRFProtect.protect)
        assert "tenant" not in src.lower()


# ── scope: only the factory changed ─────────────────────────────────────────

class TestScope:

    def test_only_the_factory_and_this_suite_changed(self):
        out = subprocess.run(["git", "status", "--porcelain", "--",
                              "app/", "migrations/"],
                             cwd=_ROOT, capture_output=True, text=True).stdout
        changed = sorted(ln[3:].strip('"') for ln in out.splitlines() if ln.strip())
        allowed = ["app/__init__.py", "app/bot/screens.py"]  # screens = pre-existing
        # CORRECTED BY RC2.5.4c-x-6f2b-B1d: containment, not equality. Equality
        # held only while app/__init__.py was an uncommitted change; once B1b is
        # committed it leaves `git status`, and a CI checkout has neither file
        # modified. Containment still rejects every path outside `allowed`.
        assert set(changed) <= set(allowed), changed

    def test_every_post_form_carries_exactly_one_token(self):
        """INVERTED BY RC2.5.4c-x-6f2b-B1d (was: test_templates_carry_no_token_yet).

        The original asserted that no template carried a token yet, so that this
        suite could not silently pass once B1c changed the templates. B1c has
        landed, so it now pins the final contract: exactly 38 POST forms in
        exactly the expected 22 templates, each with exactly one canonical token
        field, no GET form tokenised, and no token ever in a form tag or URL.
        """
        import re

        field = '<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">'
        expected = {
            "templates/campaigns.html": 1, "templates/crm_lead_detail.html": 6,
            "templates/crm_lead_import.html": 1, "templates/crm_lead_new.html": 1,
            "templates/crm_login.html": 1, "templates/crm_notifications.html": 2,
            "templates/crm_setup_password.html": 1, "templates/crm_sidebar.html": 1,
            "templates/crm_staff_management.html": 3,
            "templates/crm_super_dashboard.html": 5,
            "templates/crm_super_login.html": 1, "templates/crm_unassigned_leads.html": 1,
            "templates/public/forgot_password.html": 1,
            "templates/public/register.html": 1,
            "templates/public/resend_verification.html": 1,
            "templates/public/reset_password.html": 1, "templates/tenant/ai.html": 1,
            "templates/tenant/course_detail.html": 2,
            "templates/tenant/course_form.html": 1, "templates/tenant/profile.html": 1,
            "templates/tenant/staff.html": 2, "templates/tenant/whatsapp.html": 3,
        }
        form_re = re.compile(r"<form\b[^>]*>", re.IGNORECASE | re.DOTALL)
        post_counts, problems = {}, []
        for base, _dirs, files in os.walk(os.path.join(_ROOT, "templates")):
            for name in files:
                if not name.endswith(".html"):
                    continue
                rel = os.path.relpath(os.path.join(base, name), _ROOT).replace(os.sep, "/")
                text = _src(rel)
                for m in form_re.finditer(text):
                    tag = m.group(0)
                    end = text.lower().find("</form>", m.end())
                    body = text[m.end(): end if end != -1 else len(text)]
                    if "csrf" in tag.lower():
                        problems.append((rel, "token in a form tag or action URL"))
                    if re.search(r"\bmethod\s*=\s*['\"]?post", tag, re.IGNORECASE):
                        post_counts[rel] = post_counts.get(rel, 0) + 1
                        if body.count(field) != 1 or body.count('name="csrf_token"') != 1:
                            problems.append((rel, "POST form without exactly one token"))
                    elif "csrf_token" in body:
                        problems.append((rel, "GET form carries a token"))
        assert sum(post_counts.values()) == 38, post_counts
        assert post_counts == expected
        assert problems == [], problems

    def test_the_b1c_template_contract_suite_is_present(self):
        """ADDED BY RC2.5.4c-x-6f2b-B1d. Deleting a test file is invisible to
        every guard that inspects CHANGES -- a deleted suite simply stops
        running, and the gate stays green. The two CSRF suites therefore pin
        each other: removing the B1c template contract, or gutting its core
        tests, fails here; removing this suite fails there."""
        rel = "tests/test_csrf_templates_rc254cx6f2b_b1c.py"
        path = os.path.join(_ROOT, *rel.split("/"))
        assert os.path.exists(path), f"{rel} was removed"
        names = {n.name for n in ast.walk(ast.parse(_src(rel)))
                 if isinstance(n, ast.FunctionDef)}
        for required in (
                "test_every_post_form_has_exactly_one_canonical_token_field",
                "test_no_get_form_received_a_token",
                "test_every_protected_fetch_carries_exactly_one_header",
                "test_the_six_exempt_fetches_carry_no_header",
                "test_removing_the_insertions_reproduces_the_pre_b1c_template",
                "test_no_template_outside_the_b1c_set_changed",
                "test_the_rendered_token_is_session_bound",
                "test_rendered_fetch_calls_are_valid_javascript",
                "test_the_b1b_enforcement_suite_is_present"):
            assert required in names, f"B1c contract test {required} was removed"

    def test_requirements_unchanged_since_b1a(self):
        assert _head("requirements.txt") == _work("requirements.txt")
