"""Phase RC2.4.4a — the six /crm/ write roots fail closed without a tenant.

THE ROOT
--------
_actor_tenant_id() returns None for a non-impersonating SUPER_ADMIN (its own
docstring: "callers must refuse to write"). Six /crm/ routes fed that result
straight into log_lead_event() / log_message() / save_conversation_message() /
start_campaign(), none of which guard a falsy tenant — they resolve through
resolve_tenant_id() leg 2 to PRIMARY_TENANT_ID. Another tenant's lead activity
would then be filed under the primary tenant while RC2.4.1 correctly sends the
message on the LEAD's WABA identity: the RC2.4.0 transport/record disagreement,
inverted.

WHY tenant_query() DOES NOT ALREADY STOP IT
-------------------------------------------
tenant_query() fails closed for everyone EXCEPT a SUPER_ADMIN: that branch is
evaluated BEFORE tenant_id is consulted and returns an UNFILTERED query when
not impersonating. So the primitive the codebase trusts for isolation is
bypassed on exactly this path. TestTenantQueryCharacterization pins that
surprising behaviour so a future change to it cannot pass silently.

WHAT ACTUALLY BLOCKED IT BEFORE
-------------------------------
admin_security_guard redirects a non-impersonating SUPER_ADMIN away from /crm/,
and AUTH_MODE=SESSION_ONLY closes the historical API-key path. Both are real,
and both are OUTSIDE the write. A middleware refactor or a config change
silently re-opens the hole. RC2.4.4a moves the contract to the write itself, so
the two layers are independent — that independence is what
TestGuardIsIndependentOfMiddleware asserts.

NOT THIS PHASE
--------------
resolve_tenant_id() leg 2 is deliberately UNTOUCHED and still resolves
tenant_id=None to PRIMARY_TENANT_ID. Retiring it is RC2.4.4b and needs its own
authorization after a production observation window. These tests do not assume
leg 2 is gone; they assert the callers never hand it a None.
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

_DB = os.path.join(tempfile.gettempdir(), "phase_rc244a_roots.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc244a-admin-key")
os.environ.setdefault("SECRET_KEY", "rc244a-secret-key")
os.environ["BROADCAST_API_KEY"] = "rc244a-broadcast-key"
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from flask_login import login_user                                       # noqa: E402
from werkzeug.security import generate_password_hash                     # noqa: E402

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import (Tenant, User, ConversationState, LeadEvent,      # noqa: E402
                        MessageLog, ConversationMessage)
from app.routes import admin as admin_mod                                # noqa: E402
from app.services import whatsapp_service as wa                          # noqa: E402
from app.services.log_service import resolve_tenant_id                   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_PY = os.path.join(ROOT, "app", "routes", "admin.py")
WA_PY = os.path.join(ROOT, "app", "services", "whatsapp_service.py")

OX = "t-ox"        # primary tenant
TB = "t-beta"      # a second, independently operating tenant
OX_LEAD = "919000044001"
TB_LEAD = "919000044002"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False
_APP.config["PRIMARY_TENANT_ID"] = OX

SIX_ROUTES = ["crm_lead_send", "crm_lead_update", "crm_course_admissions",
              "crm_marketing_start_job", "campaign_send", "crm_tasks_complete"]


@pytest.fixture()
def seeded():
    """Two live tenants and a tenant-less SUPER_ADMIN.

    The app context is RELEASED before yielding: flask_login caches the resolved
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
            "super": mk("platform_super", "SUPER_ADMIN", None),   # tenant_id NULL
            "ox_admin": mk("ox_admin", "ADMIN", OX),
        }
        for tid, ph, nm in ((OX, OX_LEAD, "OxfordCustomer"),
                            (TB, TB_LEAD, "BetaCustomer")):
            db.session.add(ConversationState(
                phone=ph, tenant_id=tid, name=nm, lead_status="Lead",
                assigned_staff="ox_admin" if tid == OX else "tb_admin",
                lead_score=10, is_admitted=False))
        db.session.commit()
    yield ids
    with _APP.app_context():
        db.session.remove()


def _counts():
    """Row counts on every table the six routes can write through."""
    with _APP.app_context():
        return (LeadEvent.query.count(),
                MessageLog.query.count(),
                ConversationMessage.query.count())


def _as_super(user_id, path, method="POST", data=None, json_body=None,
              impersonate=None):
    """A request context with a REAL logged-in SUPER_ADMIN.

    Calling the view function directly (rather than through test_client) is
    deliberate: it bypasses admin_security_guard WITHOUT weakening it, which is
    the only honest way to show the write-boundary guard stands on its own.
    """
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


# ═══ GAP A — a tenant-less actor cannot reach a writer ═══════════════════════

class TestWriteBoundaryRefusesTenantlessActor:

    def _code(self, resp):
        return resp[1] if isinstance(resp, tuple) else getattr(resp, "status_code", 200)

    def _assert_refused(self, resp, expect):
        """Assert the SPECIFIC refusal code this route's guard returns.

        Deliberately NOT a permissive "any 3xx/4xx" check: crm_lead_update and
        crm_course_admissions redirect on SUCCESS too, so accepting 302 made the
        assertion blind — removing the guard still 'passed'. Mutation M2 caught
        that and the check was tightened to the exact code.
        """
        code = self._code(resp)
        assert code == expect, f"expected refusal {expect}, got {code}"

    def test_crm_lead_send_refuses(self, seeded):
        before = _counts()
        ctx = _as_super(seeded["super"], f"/crm/lead/{TB_LEAD}/send",
                        data={"message": "hello"})
        try:
            self._assert_refused(admin_mod.crm_lead_send(TB_LEAD), 403)
        finally:
            ctx.pop()
        assert _counts() == before, "partial persistence occurred"

    def test_crm_lead_update_refuses(self, seeded):
        before = _counts()
        ctx = _as_super(seeded["super"], f"/crm/lead/{TB_LEAD}/update",
                        data={"notes": "x", "assigned_staff": "someone"})
        try:
            self._assert_refused(admin_mod.crm_lead_update(TB_LEAD), 403)
        finally:
            ctx.pop()
        assert _counts() == before, "partial persistence occurred"

    def test_crm_course_admissions_refuses(self, seeded):
        before = _counts()
        ctx = _as_super(seeded["super"], f"/crm/course-admissions/{TB_LEAD}",
                        data={"admitted_courses[]": "DCA"})
        try:
            self._assert_refused(admin_mod.crm_course_admissions(TB_LEAD), 403)
        finally:
            ctx.pop()
        assert _counts() == before, "partial persistence occurred"

    def test_crm_marketing_start_job_refuses(self, seeded, monkeypatch):
        fired = []
        monkeypatch.setattr("app.services.campaign_service.start_campaign",
                            lambda *a, **k: fired.append(k.get("tenant_id")))
        ctx = _as_super(seeded["super"], "/crm/marketing/start_job",
                        json_body={"phones": [TB_LEAD], "message": "hi",
                                   "campaign_name": "c"})
        try:
            self._assert_refused(admin_mod.crm_marketing_start_job(), 400)
        finally:
            ctx.pop()
        assert fired == [], f"campaign dispatched without a tenant: {fired}"

    def test_campaign_send_refuses(self, seeded, monkeypatch):
        fired = []
        monkeypatch.setattr("app.services.campaign_service.start_campaign",
                            lambda *a, **k: fired.append(k.get("tenant_id")))
        monkeypatch.setattr(admin_mod, "_calculate_audiences",
                            lambda *a, **k: {"all": {TB_LEAD}})
        ctx = _as_super(seeded["super"], "/crm/campaigns/send",
                        data={"campaign_name": "c", "audience": "all",
                              "message": "hi"})
        try:
            resp = admin_mod.campaign_send()
        finally:
            ctx.pop()
        assert fired == [], f"campaign dispatched without a tenant: {fired}"

    def test_crm_tasks_complete_refuses(self, seeded):
        """Already guarded before RC2.4.4a — pinned so it stays that way."""
        before = _counts()
        ctx = _as_super(seeded["super"], "/crm/tasks/complete",
                        json_body={"task_id": "abc", "phone": TB_LEAD})
        try:
            self._assert_refused(admin_mod.crm_tasks_complete(), 400)
        finally:
            ctx.pop()
        assert _counts() == before, "partial persistence occurred"

    def test_no_row_was_attributed_to_the_primary_tenant(self, seeded):
        """The specific damage leg 2 would have caused."""
        with _APP.app_context():
            assert LeadEvent.query.filter_by(tenant_id=OX).count() == 0
            assert MessageLog.query.filter_by(tenant_id=OX).count() == 0

    def test_all_six_routes_carry_the_guard(self):
        """Structural: the guard exists in every one of the six, after the
        tenant is resolved, and none still passes _actor_tenant_id() inline."""
        tree = ast.parse(open(ADMIN_PY, encoding="utf-8").read())
        lines = open(ADMIN_PY, encoding="utf-8").read().split("\n")
        seen = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name in SIX_ROUTES:
                seg = list(enumerate(lines[n.lineno - 1:n.end_lineno], start=n.lineno))
                res = [i for i, l in seg if "_tid = _actor_tenant_id()" in l]
                grd = [i for i, l in seg if "if not _tid:" in l]
                inline = [i for i, l in seg if "tenant_id=_actor_tenant_id()" in l]
                seen[n.name] = (res, grd, inline)
        assert set(seen) == set(SIX_ROUTES), f"routes missing: {set(SIX_ROUTES) - set(seen)}"
        for name, (res, grd, inline) in seen.items():
            assert res, f"{name}: tenant is not resolved into _tid"
            assert grd, f"{name}: no `if not _tid:` guard"
            assert grd[0] > res[0], f"{name}: guard precedes resolution"
            assert not inline, f"{name}: still passes _actor_tenant_id() inline"


# ═══ GAP B — the guard does not depend on middleware ════════════════════════

class TestGuardIsIndependentOfMiddleware:

    def test_middleware_layer_still_blocks(self, seeded):
        """Layer 1 unchanged: admin_security_guard redirects the same actor."""
        c = _APP.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(seeded["super"])
            s["_fresh"] = True
        r = c.post(f"/crm/lead/{TB_LEAD}/send", data={"message": "hi"})
        assert r.status_code in (301, 302), \
            f"middleware no longer redirects the tenant-less SUPER_ADMIN: {r.status_code}"

    def test_write_boundary_blocks_with_middleware_bypassed(self, seeded):
        """Layer 2, the point of this phase: with the view invoked directly —
        middleware never consulted — the write is STILL refused."""
        before = _counts()
        ctx = _as_super(seeded["super"], f"/crm/lead/{TB_LEAD}/send",
                        data={"message": "hello"})
        try:
            resp = admin_mod.crm_lead_send(TB_LEAD)
        finally:
            ctx.pop()
        code = resp[1] if isinstance(resp, tuple) else getattr(resp, "status_code", 200)
        assert code == 403, f"write boundary did not refuse: {code}"
        assert _counts() == before

    def test_impersonating_super_admin_still_works(self, seeded):
        """The guard must not break the legitimate path: an IMPERSONATING
        SUPER_ADMIN has a tenant and must get past the guard."""
        ctx = _as_super(seeded["super"], f"/crm/lead/{OX_LEAD}/send",
                        data={"message": "hello"}, impersonate=OX)
        try:
            assert admin_mod._actor_tenant_id() == OX
        finally:
            ctx.pop()

    def test_ordinary_admin_is_unaffected(self, seeded):
        """A tenant-bound ADMIN resolves normally — no behaviour change."""
        ctx = _as_super(seeded["ox_admin"], f"/crm/lead/{OX_LEAD}/send",
                        data={"message": "hello"})
        try:
            assert admin_mod._actor_tenant_id() == OX
        finally:
            ctx.pop()


# ═══ GAP C — characterize tenant_query()'s SUPER_ADMIN bypass ═══════════════

class TestTenantQueryCharacterization:
    """NOT a redesign request (that is out of RC2.4.4a scope). This pins the
    behaviour so the write-boundary guard's necessity stays visible: without
    it, `lead` is found across tenants."""

    def test_super_admin_branch_is_unfiltered_when_not_impersonating(self, seeded):
        ctx = _as_super(seeded["super"], "/crm/leads", method="GET")
        try:
            rows = admin_mod.tenant_query(ConversationState, None).all()
            tenants = {r.tenant_id for r in rows}
        finally:
            ctx.pop()
        assert tenants == {OX, TB}, (
            "tenant_query no longer returns every tenant for a non-impersonating "
            f"SUPER_ADMIN (got {tenants}). If this was fixed deliberately, update "
            "this characterization test and the RC2.4.4 discovery record.")

    def test_super_admin_branch_ignores_an_explicit_tenant_id(self, seeded):
        """The argument is not consulted at all on that branch."""
        ctx = _as_super(seeded["super"], "/crm/leads", method="GET")
        try:
            rows = admin_mod.tenant_query(ConversationState, TB).all()
            tenants = {r.tenant_id for r in rows}
        finally:
            ctx.pop()
        assert tenants == {OX, TB}, f"expected the argument to be ignored, got {tenants}"

    def test_impersonation_does_filter(self, seeded):
        ctx = _as_super(seeded["super"], "/crm/leads", method="GET", impersonate=TB)
        try:
            tenants = {r.tenant_id for r in admin_mod.tenant_query(ConversationState, None).all()}
        finally:
            ctx.pop()
        assert tenants == {TB}


# ═══ GAP D — a legitimate primary send emits no leg-2 ERROR ═════════════════

LEG2 = "implicit resolution"


class TestPrimarySendEmitsNoImplicitResolutionError:

    def test_primary_tenant_send_logs_no_implicit_resolution(self, seeded, caplog,
                                                             monkeypatch):
        """OX is the primary tenant and has NO per-tenant WABA row, so this
        exercises the exact backward-compatibility comparison that used to call
        resolve_tenant_id(None) and fire leg 2's ERROR on every send."""
        monkeypatch.setattr(wa, "PHONE_NUMBER_ID", "111111111111111")
        monkeypatch.setattr(wa, "ACCESS_TOKEN", "env-token")
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = OX
            with caplog.at_level(logging.ERROR):
                phone_id, token = wa._get_waba_credentials(OX)
        assert (phone_id, token) == ("111111111111111", "env-token")
        offending = [r.message for r in caplog.records if LEG2 in r.message]
        assert offending == [], f"legitimate primary send still fired leg 2: {offending}"

    def test_detector_actually_works(self, seeded, caplog):
        """Negative control: the assertion above is only meaningful if this
        detector can see a real leg-2 firing."""
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = OX
            with caplog.at_level(logging.ERROR):
                assert resolve_tenant_id(None) == OX
        assert any(LEG2 in r.message for r in caplog.records), \
            "detector is blind — the no-error assertion above proves nothing"

    def test_blank_primary_still_fails_closed(self, seeded, monkeypatch):
        monkeypatch.setattr(wa, "PHONE_NUMBER_ID", "111111111111111")
        monkeypatch.setattr(wa, "ACCESS_TOKEN", "env-token")
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = ""
            with pytest.raises(ValueError):
                wa._get_waba_credentials(OX)

    def test_non_primary_tenant_without_credentials_still_raises(self, seeded,
                                                                 monkeypatch):
        monkeypatch.setattr(wa, "PHONE_NUMBER_ID", "111111111111111")
        monkeypatch.setattr(wa, "ACCESS_TOKEN", "env-token")
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = OX
            with pytest.raises(ValueError):
                wa._get_waba_credentials(TB)

    def test_rc241_fail_closed_guard_intact(self, seeded):
        """RC2.4.1 must survive this phase untouched."""
        with _APP.app_context():
            with pytest.raises(ValueError):
                wa._get_waba_credentials(None)

    def test_comparison_no_longer_calls_resolve_tenant_id_with_none(self):
        """Structural: the config read replaced the resolution call."""
        tree = ast.parse(open(WA_PY, encoding="utf-8").read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_get_waba_credentials")
        bad = [n.lineno for n in ast.walk(fn)
               if isinstance(n, ast.Call)
               and ast.unparse(n.func).split(".")[-1] == "resolve_tenant_id"
               and n.args and isinstance(n.args[0], ast.Constant)
               and n.args[0].value is None]
        assert bad == [], f"resolve_tenant_id(None) is back at {bad}"


# ═══ leg 2 is deliberately still live ═══════════════════════════════════════

class TestLegTwoStillLive:
    """RC2.4.4a does NOT retire leg 2. If RC2.4.4b ever does, INVERT these."""

    def test_leg2_still_resolves_none_to_primary(self, seeded):
        with _APP.app_context():
            _APP.config["PRIMARY_TENANT_ID"] = OX
            assert resolve_tenant_id(None) == OX

    def test_resolve_tenant_id_was_not_modified(self):
        src = open(os.path.join(ROOT, "app", "services", "log_service.py"),
                   encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "resolve_tenant_id")
        body = ast.parse(ast.unparse(fn)).body[0]
        if (body.body and isinstance(body.body[0], ast.Expr)
                and isinstance(body.body[0].value, ast.Constant)):
            body.body.pop(0)
        code = ast.unparse(body)
        assert "PRIMARY_TENANT_ID" in code, "leg 2 was removed — that is RC2.4.4b"
        assert "return tenant_id" in code, "leg 1 was removed"
        assert code.rstrip().endswith("return None"), "leg 3 was removed"
