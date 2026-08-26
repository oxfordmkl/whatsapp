"""Phase RC2.4.4b — resolve_tenant_id() leg 2 becomes a hard failure.

WHY
---
RC2.4.4a's discovery traced every reachable caller of resolve_tenant_id() to a
root that already supplies an explicit tenant_id: the six /crm/ write routes
it guarded, four more independently guarded since ADR-021, and every webhook/
worker path that resolves its own tenant before calling in. A 5-day passive
production window then confirmed it empirically: 661 organic requests, 221
webhook deliveries, 0 leg-2 firings.

Leg 2 (tenant_id=None + PRIMARY_TENANT_ID configured -> silently return it)
was therefore a safety net for a defect that no longer has a way to occur
through the current call graph. Silently returning PRIMARY_TENANT_ID let a
future unguarded caller recreate a TD-P0-1-class mis-filing with nothing but
a log line to notice it. This phase makes that failure loud instead: leg 2
now raises ValueError. Leg 1 (explicit tenant_id) and leg 3 (nothing resolves
-> None) are UNCHANGED — H4-c's decision for leg 3 is not reopened here.

SECOND FIX IN THIS PHASE
-------------------------
Three admin.py routes (crm_unassigned_assign, crm_auto_assign_confirm,
crm_reassignment_confirm) already guarded correctly with
`_tid = _actor_tenant_id(); if not _tid: return ...`, but then called
log_lead_event(tenant_id=_actor_tenant_id(), ...) -- RE-INVOKING the resolver
instead of reusing _tid. Provably safe today (nothing mutates session between
the two calls), but fragile: nothing enforces the two stay equal, so a future
edit could silently reopen the exact hole RC2.4.4a closed elsewhere. All three
now pass tenant_id=_tid.
"""
import ast
import logging
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc244b_leg2.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc244b-admin-key")
os.environ.setdefault("SECRET_KEY", "rc244b-secret-key")
os.environ["BROADCAST_API_KEY"] = "rc244b-broadcast-key"
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from flask_login import login_user                                       # noqa: E402
from werkzeug.security import generate_password_hash                     # noqa: E402

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState                   # noqa: E402
from app.routes import admin as admin_mod                                # noqa: E402
from app.services.log_service import resolve_tenant_id                   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGSVC = os.path.join(ROOT, "app", "services", "log_service.py")
ADMIN_PY = os.path.join(ROOT, "app", "routes", "admin.py")

OX = "t-ox"
TB = "t-beta"
LEAD_PHONE = "919000055001"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False
_APP.config["PRIMARY_TENANT_ID"] = OX

THREE_ROUTES = ["crm_unassigned_assign", "crm_auto_assign_confirm",
                "crm_reassignment_confirm"]

# The 12 direct resolve_tenant_id() call sites established by RC2.4.4b
# discovery -- (file, enclosing function). Structural regression anchor: none
# of these should move, disappear, or gain a new bare-None call.
TWELVE_SITES = [
    ("app/state.py", "_db_save"),
    ("app/state.py", "get_or_create_state"),
    ("app/state.py", "resolve_is_new_lead"),
    ("app/state.py", "phone_exists"),
    ("app/bot/router.py", "smart_reply"),
    ("app/services/campaign_service.py", "start_campaign"),
    ("app/services/followup_service.py", "schedule_followups"),
    ("app/services/log_service.py", "log_message"),
    ("app/services/log_service.py", "save_conversation_message"),
    ("app/services/log_service.py", "log_lead_event"),
    ("app/services/whatsapp_service.py", "_get_waba_credentials"),
    ("app/services/whatsapp_service.py", "send_automation"),
]


@pytest.fixture()
def seeded():
    """Two tenants, a tenant-bound ADMIN, and a tenant-less SUPER_ADMIN.

    App context released before yielding: flask_login caches the resolved
    user on flask.g, bound to the APPLICATION context, so holding one across
    test_client requests leaks identity between them (the 14B.1 fixture bug).
    """
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((OX, "Oxford"), (TB, "Beta Institute")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()

        def mk(username, role, tid):
            u = User(username=username, email=f"{username}@x.test",
                     password_hash=generate_password_hash("pw"), role=role,
                     tenant_id=tid, is_active=True, require_password_change=False)
            db.session.add(u)
            db.session.commit()
            return u.id

        ids = {
            "super": mk("platform_super", "SUPER_ADMIN", None),
            "ox_admin": mk("ox_admin", "ADMIN", OX),
        }
        db.session.add(ConversationState(
            phone=LEAD_PHONE, tenant_id=OX, name="TestLead", lead_status="Lead",
            assigned_staff="old_staff", lead_score=10, is_admitted=False))
        db.session.commit()
    yield ids
    with _APP.app_context():
        db.session.remove()


def _as_super(user_id, path, method="POST", data=None, json_body=None,
              impersonate=None):
    """A request context with a REAL logged-in user (see rc244a for rationale:
    calling the view directly bypasses admin_security_guard WITHOUT weakening
    it, the only honest way to prove the write boundary stands on its own)."""
    from flask import session as flask_session
    kw = {"method": method}
    if json_body is not None:
        kw["json"] = json_body
    else:
        kw["data"] = data or {}
    ctx = _APP.test_request_context(path, **kw)
    ctx.push()
    with _APP.app_context():
        u = User.query.get(user_id)
    login_user(u)
    if impersonate:
        flask_session["impersonate_tenant_id"] = impersonate
    return ctx


def _fn_body(path, name):
    tree = ast.parse(open(path, encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    L = open(path, encoding="utf-8").read().splitlines()
    return "\n".join(L[fn.lineno - 1:fn.end_lineno])


# ═══ leg 2 — hard failure ═════════════════════════════════════════════════

class TestLegTwoHardFailure:

    def test_leg2_raises_when_primary_configured(self, seeded):
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = OX
            with pytest.raises(ValueError, match="resolve_tenant_id"):
                resolve_tenant_id(None)

    def test_leg2_no_longer_returns_primary_silently(self, seeded):
        """The specific old behaviour this phase removes."""
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = OX
            try:
                result = resolve_tenant_id(None)
            except ValueError:
                result = "RAISED"
        assert result == "RAISED", f"leg 2 returned {result!r} instead of raising"

    def test_leg2_error_message_names_the_problem(self, seeded):
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = OX
            with pytest.raises(ValueError) as exc_info:
                resolve_tenant_id(None)
        msg = str(exc_info.value)
        assert "tenant_id=None" in msg or "None" in msg
        assert "explicit" in msg.lower()

    def test_leg2_raise_is_uncaught_by_resolve_tenant_id_itself(self, seeded):
        """The raise must propagate OUT of resolve_tenant_id(), not be
        swallowed by its own internal exception handling."""
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = OX
            raised = False
            try:
                resolve_tenant_id(None)
            except ValueError:
                raised = True
            except Exception as e:
                pytest.fail(f"wrong exception type escaped: {type(e).__name__}: {e}")
        assert raised

    def test_old_implicit_resolution_log_message_is_gone(self, seeded, caplog):
        """The ERROR-level log line this phase retires from leg 2's success
        path (it now raises instead of logging-and-returning)."""
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = OX
            with caplog.at_level(logging.ERROR):
                with pytest.raises(ValueError):
                    resolve_tenant_id(None)
        assert not any("implicit resolution" in r.message for r in caplog.records), \
            "leg 2 still logs the old silent-success message"


# ═══ leg 1 — explicit tenant, unchanged ═════════════════════════════════════

class TestLegOnePreserved:

    def test_explicit_tenant_always_wins(self, seeded):
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = OX
            assert resolve_tenant_id(TB) == TB

    def test_explicit_tenant_wins_even_with_primary_configured(self, seeded):
        """Leg 1 must short-circuit BEFORE leg 2 is ever consulted."""
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = OX
            assert resolve_tenant_id(OX) == OX  # does not raise

    def test_explicit_tenant_wins_with_no_primary_configured(self, seeded):
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = ""
            assert resolve_tenant_id(TB) == TB

    def test_leg1_never_raises(self, seeded):
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = OX
            try:
                resolve_tenant_id(TB)
            except Exception as e:
                pytest.fail(f"explicit tenant_id raised: {e}")


# ═══ leg 3 — genuine non-resolution, unchanged in kind ══════════════════════

class TestLegThreeUnchanged:

    def test_no_tenant_no_primary_still_returns_none(self, seeded):
        """THE INVARIANT THIS PHASE DOES NOT TOUCH: leg 3 stays a silent
        return, not a raise. H4-c's trade (lost log line > cross-tenant
        write) is not reopened by RC2.4.4b."""
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = ""
            assert resolve_tenant_id(None) is None

    def test_leg3_does_not_raise(self, seeded):
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = ""
            try:
                result = resolve_tenant_id(None)
            except Exception as e:
                pytest.fail(f"leg 3 raised instead of returning None: {e}")
            assert result is None

    def test_unresolved_still_logged_at_error(self, seeded, caplog):
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = ""
            with caplog.at_level(logging.ERROR):
                resolve_tenant_id(None)
        assert any("UNRESOLVED" in r.message for r in caplog.records), caplog.text

    def test_config_lookup_failure_falls_through_to_leg3(self):
        """No app context -> the config lookup itself fails -> must still
        fall through to leg 3 (None), not crash with an unrelated error."""
        try:
            result = resolve_tenant_id(None)
        except Exception as e:
            pytest.fail(f"config-lookup failure raised the wrong thing: {e}")
        assert result is None


# ═══ structural regression — all 12 call sites unchanged in shape ══════════

class TestTwelveCallSitesRegression:

    def test_all_twelve_sites_present_and_call_resolve_tenant_id(self):
        for relpath, fname in TWELVE_SITES:
            path = os.path.join(ROOT, relpath)
            tree = ast.parse(open(path, encoding="utf-8").read())
            fn = next((n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == fname), None)
            assert fn is not None, f"{fname} missing from {relpath}"
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                     and ast.unparse(n.func).split(".")[-1] == "resolve_tenant_id"]
            assert calls, f"{fname} in {relpath} no longer calls resolve_tenant_id()"

    def test_no_call_site_passes_a_bare_none_literal(self):
        """Nobody should call resolve_tenant_id(None) directly in production
        code -- that was the whatsapp_service.py:58 defect RC2.4.4a fixed.
        Guards against it reappearing anywhere in app/."""
        SKIP = {"__pycache__"}
        bad = []
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, "app")):
            dirnames[:] = [d for d in dirnames if d not in SKIP]
            for f in filenames:
                if not f.endswith(".py"):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    tree = ast.parse(open(p, encoding="utf-8").read())
                except SyntaxError:
                    continue
                for n in ast.walk(tree):
                    if (isinstance(n, ast.Call)
                            and ast.unparse(n.func).split(".")[-1] == "resolve_tenant_id"
                            and n.args and isinstance(n.args[0], ast.Constant)
                            and n.args[0].value is None):
                        bad.append(f"{os.path.relpath(p, ROOT)}:{n.lineno}")
        assert bad == [], f"resolve_tenant_id(None) reintroduced: {bad}"

    def test_whatsapp_service_still_reads_primary_directly(self):
        """RC2.4.4a's fix (not this phase's job to touch) must still be
        intact: the backward-compat comparison reads PRIMARY_TENANT_ID
        directly. (No executable resolve_tenant_id(None) call anywhere in
        app/ is separately proven, AST-based, by
        test_no_call_site_passes_a_bare_none_literal above -- a plain
        substring check here would false-positive on the explanatory
        comments this file legitimately carries.)"""
        src = open(os.path.join(ROOT, "app", "services", "whatsapp_service.py"),
                   encoding="utf-8").read()
        assert 'current_app.config.get("PRIMARY_TENANT_ID")' in src


# ═══ the three admin.py routes now reuse _tid ═══════════════════════════════

class TestThreeRoutesReuseGuardedTid:

    def test_structural_no_inline_actor_tenant_id_in_log_lead_event(self):
        """None of the three routes may pass _actor_tenant_id() inline to
        log_lead_event() any more -- must use the already-guarded _tid."""
        for name in THREE_ROUTES:
            body = _fn_body(ADMIN_PY, name)
            assert "log_lead_event(tenant_id=_actor_tenant_id()" not in body, \
                f"{name} still re-invokes _actor_tenant_id() inline"

    def test_structural_log_lead_event_uses_tid(self):
        for name in THREE_ROUTES:
            body = _fn_body(ADMIN_PY, name)
            assert "log_lead_event(tenant_id=_tid" in body, \
                f"{name} does not bind log_lead_event to _tid"

    def test_structural_guard_still_precedes_the_call(self):
        for name in THREE_ROUTES:
            tree = ast.parse(open(ADMIN_PY, encoding="utf-8").read())
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            L = open(ADMIN_PY, encoding="utf-8").read().splitlines()
            seg = list(enumerate(L[fn.lineno - 1:fn.end_lineno], start=fn.lineno))
            res = [i for i, l in seg if "_tid = _actor_tenant_id()" in l]
            grd = [i for i, l in seg if "if not _tid:" in l]
            call = [i for i, l in seg if "log_lead_event(tenant_id=_tid" in l]
            assert res and grd and call, f"{name}: missing resolve/guard/call"
            assert grd[0] > res[0], f"{name}: guard precedes resolution"
            assert call[0] > grd[0], f"{name}: call precedes the guard"

    def test_crm_unassigned_assign_still_refuses_tenantless_actor(self, seeded):
        """Regression: the pre-existing guard (not new in this phase) still
        works after the tenant_id binding change."""
        before = None
        with _APP.app_context():
            before = ConversationState.query.filter_by(phone=LEAD_PHONE).first().assigned_staff
        ctx = _as_super(seeded["super"], "/crm/leads/unassigned/assign",
                        data={"phone": LEAD_PHONE, "target_staff": "someone"})
        try:
            admin_mod.crm_unassigned_assign()
        finally:
            ctx.pop()
        with _APP.app_context():
            after = ConversationState.query.filter_by(phone=LEAD_PHONE).first().assigned_staff
        assert after == before, "write occurred despite no resolved tenant"

    def test_crm_unassigned_assign_binds_log_event_to_actor_tenant(self, seeded, monkeypatch):
        """Functional: with a valid tenant, log_lead_event receives the SAME
        value _actor_tenant_id() would produce -- proving the binding change
        is behaviour-preserving for the reachable (valid-tenant) case."""
        captured = {}

        def fake_log_lead_event(tenant_id=None, **kw):
            captured["tenant_id"] = tenant_id

        monkeypatch.setattr("app.services.log_service.log_lead_event", fake_log_lead_event)
        monkeypatch.setattr(
            admin_mod.staff_identity_service, "resolve_assignment",
            lambda tid, val: type("R", (), {"ok": True, "value": val})())
        monkeypatch.setattr(admin_mod, "_sync_assigned_user", lambda lead, tid: None)

        ctx = _as_super(seeded["ox_admin"], "/crm/leads/unassigned/assign",
                        data={"phone": LEAD_PHONE, "target_staff": "new_staff"})
        try:
            admin_mod.crm_unassigned_assign()
        finally:
            ctx.pop()
        assert captured.get("tenant_id") == OX, \
            f"log_lead_event received {captured.get('tenant_id')!r}, expected {OX!r}"

    def test_crm_reassignment_confirm_binds_log_event_to_actor_tenant(self, seeded, monkeypatch):
        captured = {}

        def fake_log_lead_event(tenant_id=None, **kw):
            captured["tenant_id"] = tenant_id

        monkeypatch.setattr("app.services.log_service.log_lead_event", fake_log_lead_event)
        monkeypatch.setattr(
            admin_mod.staff_identity_service, "resolve_assignment",
            lambda tid, val: type("R", (), {"ok": True, "value": val})())
        monkeypatch.setattr(admin_mod, "_sync_assigned_user", lambda lead, tid: None)

        ctx = _as_super(seeded["ox_admin"], "/crm/reassignment-center/confirm",
                        json_body={"phones": [LEAD_PHONE], "target_staff": "new_staff"})
        try:
            admin_mod.crm_reassignment_confirm()
        finally:
            ctx.pop()
        assert captured.get("tenant_id") == OX, \
            f"log_lead_event received {captured.get('tenant_id')!r}, expected {OX!r}"

    def test_crm_auto_assign_confirm_binds_log_event_to_actor_tenant(self, seeded, monkeypatch):
        captured = {}

        def fake_log_lead_event(tenant_id=None, **kw):
            captured["tenant_id"] = tenant_id

        monkeypatch.setattr("app.services.log_service.log_lead_event", fake_log_lead_event)
        monkeypatch.setattr(
            admin_mod.staff_identity_service, "resolve_assignment",
            lambda tid, val: type("R", (), {"ok": True, "value": val})())
        monkeypatch.setattr(admin_mod, "_sync_assigned_user", lambda lead, tid: None)

        ctx = _as_super(seeded["ox_admin"], "/crm/leads/unassigned/auto-assign-confirm",
                        json_body={"assignments": [
                            {"phone": LEAD_PHONE, "target_staff": "new_staff"}]})
        try:
            admin_mod.crm_auto_assign_confirm()
        finally:
            ctx.pop()
        assert captured.get("tenant_id") == OX, \
            f"log_lead_event received {captured.get('tenant_id')!r}, expected {OX!r}"


# ═══ everything upstream of leg 2 remains intact ════════════════════════════

class TestUpstreamPhasesIntact:

    def test_rc241_fail_closed_guard_still_present(self):
        from app.services import whatsapp_service as wa
        src = open(os.path.join(ROOT, "app", "services", "whatsapp_service.py"),
                   encoding="utf-8").read()
        assert "Outbound WhatsApp requires an explicit tenant_id" in src

    def test_rc243_helper_still_absent(self):
        src = open(LOGSVC, encoding="utf-8").read()
        tree = ast.parse(src)
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert "_get_default_tenant_id" not in names

    def test_six_rc244a_guards_still_present(self):
        six = ["crm_lead_send", "crm_lead_update", "crm_course_admissions",
               "crm_marketing_start_job", "campaign_send", "crm_tasks_complete"]
        src = open(ADMIN_PY, encoding="utf-8").read()
        tree = ast.parse(src)
        for name in six:
            body = _fn_body(ADMIN_PY, name)
            assert "if not _tid:" in body, f"{name} lost its RC2.4.4a guard"
