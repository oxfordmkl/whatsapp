"""
Phase 8.2D.5 — Campaign launch route tests.

Route under test:
    POST /crm/campaigns/v2/<campaign_id>/launch

Proves:
  - flag OFF → 404
  - unauthenticated → 403
  - None/empty tenant → 403
  - campaign not found → 404
  - CampaignTransitionError (illegal state) → 409
  - CampaignValidationError → 400
  - successful launch → 200 with campaign detail in running state
  - CampaignService.mark_running() called with correct tenant and id
  - worker is NOT started (no thread, no send_automation, no init_campaign_worker)
  - audit remains inside CampaignService (no audit call in route source)
  - db.session not touched in route
  - all legacy files unchanged
"""
import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

_ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MKT_PATH = os.path.join(_ROOT, "app", "routes", "marketing.py")
_MKT_SRC  = open(_MKT_PATH, encoding="utf-8").read()


def _strip_comments(src: str) -> str:
    """Remove docstrings and # comments — leaves only executable code."""
    import ast, tokenize, io
    result = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenError:
        return src
    for tok_type, tok_str, _, _, _ in tokens:
        if tok_type in (tokenize.COMMENT, tokenize.STRING):
            continue
        result.append(tok_str)
    return " ".join(result)


_MKT_CODE = _strip_comments(_MKT_SRC)


# ── Stub infrastructure ───────────────────────────────────────────────────────

class _FakeBlueprint:
    def __init__(self, name, *a, **kw):
        self.name = name
    def route(self, *a, **kw):
        return lambda f: f


def _ensure_stubs():
    for name in [
        "app", "app.flags", "app.routes", "app.routes.admin",
        "app.marketing", "app.marketing.campaign_service",
        "flask", "flask_login",
    ]:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)

    flask_mod = sys.modules["flask"]
    flask_mod.Blueprint = _FakeBlueprint
    if not hasattr(flask_mod, "jsonify"):
        flask_mod.jsonify = lambda d, **kw: (d, kw)
    if not hasattr(flask_mod, "request"):
        flask_mod.request = MagicMock()
    if not hasattr(flask_mod, "current_app"):
        flask_mod.current_app = MagicMock()

    sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: False
    sys.modules["app.routes.admin"]._actor_tenant_id    = lambda: None
    sys.modules["app.routes.admin"].check_auth          = lambda: True
    sys.modules["app.routes.admin"].admin_required      = lambda f: f
    sys.modules["app.routes.admin"].get_current_actor   = lambda: {
        "authenticated": True, "username": "admin",
        "role": "ADMIN", "source": "SESSION",
    }


def _load_module(alias, path):
    spec = importlib.util.spec_from_file_location(alias, path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


_ensure_stubs()

_svc_mod = _load_module(
    "_p82d5_svc",
    os.path.join(_ROOT, "app", "marketing", "campaign_service.py"),
)
sys.modules["app.marketing.campaign_service"] = _svc_mod

_mkt = _load_module("_p82d5_mkt", _MKT_PATH)

CampaignTransitionError = _svc_mod.CampaignTransitionError
CampaignValidationError = _svc_mod.CampaignValidationError
ValidationResult        = _svc_mod.ValidationResult


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_campaign(id=1, status="running", **kw):
    c = MagicMock()
    c.id = id; c.name = kw.get("name", "Test"); c.status = status
    c.total_recipients = kw.get("total_recipients", 0)
    c.sent_count = 0; c.failed_count = 0
    c.created_at = None; c.updated_at = None
    c.created_by = "admin"; c.scheduled_at = None
    c.started_at = None; c.completed_at = None
    c.description = None; c.message_body = "Hello"
    c.template_id = None; c.audience_rule_id = None
    c.failure_reason = None
    return c


def _make_svc(campaign=None, raises=None):
    svc = MagicMock()
    svc.get_campaign.return_value = campaign
    if raises is not None:
        svc.mark_running.side_effect = raises
    else:
        svc.mark_running.return_value = campaign or _make_campaign()
    return svc


def _unpack(raw):
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], int):
        inner, status = raw
        body = inner[0] if isinstance(inner, tuple) else inner
        return body, status
    body = raw[0] if isinstance(raw, tuple) else raw
    return body, 200


def _with_flag(val, fn):
    sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: val
    try:
        return fn()
    finally:
        sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: False


def _with_tenant(tid, fn):
    sys.modules["app.routes.admin"]._actor_tenant_id = lambda: tid
    try:
        return fn()
    finally:
        sys.modules["app.routes.admin"]._actor_tenant_id = lambda: None


def _with_auth(val, fn):
    sys.modules["app.routes.admin"].check_auth = lambda: val
    try:
        return fn()
    finally:
        sys.modules["app.routes.admin"].check_auth = lambda: True


def _inject(svc, fn):
    orig = _mkt._make_service
    _mkt._make_service = lambda: svc
    try:
        return fn()
    finally:
        _mkt._make_service = orig


def _call(svc, campaign_id=1, tenant="T1"):
    """Call launch_campaign with flag ON, real tenant, injected svc."""
    return _unpack(
        _inject(svc, lambda: _with_flag(True, lambda:
            _with_tenant(tenant, lambda: _mkt.launch_campaign(campaign_id))))
    )


# ── Source-level assertions ───────────────────────────────────────────────────

class TestSourceStructure:
    def test_launch_route_defined(self):
        assert "def launch_campaign" in _MKT_SRC

    def test_mark_running_called(self):
        assert "svc.mark_running" in _MKT_SRC

    def test_no_threading_in_route(self):
        assert "threading" not in _MKT_SRC
        assert "Thread(" not in _MKT_SRC

    def test_no_send_automation_in_route(self):
        assert "send_automation" not in _MKT_CODE

    def test_no_init_campaign_worker_in_route(self):
        assert "init_campaign_worker" not in _MKT_CODE

    def test_no_db_session_in_route(self):
        assert "db.session" not in _MKT_CODE

    def test_no_audit_call_in_route(self):
        """Audit is CampaignService's responsibility; route must not duplicate it."""
        assert "log_audit" not in _MKT_CODE
        assert "_audit_status_changed" not in _MKT_CODE

    def test_run_lifecycle_reused(self):
        """launch_campaign must delegate to _run_lifecycle, not duplicate guards."""
        assert "_run_lifecycle" in _MKT_SRC

    def test_legacy_service_not_imported(self):
        assert "app.services.campaign_service" not in _MKT_SRC


# ── Flag guard ────────────────────────────────────────────────────────────────

class TestFlagGuard:
    def test_returns_404_when_flag_off(self):
        raw = _with_flag(False, lambda: _mkt.launch_campaign(1))
        _, status = _unpack(raw)
        assert status == 404

    def test_service_not_called_when_flag_off(self):
        svc = _make_svc(campaign=_make_campaign())
        _inject(svc, lambda: _with_flag(False, lambda: _mkt.launch_campaign(1)))
        svc.mark_running.assert_not_called()


# ── Auth guard ────────────────────────────────────────────────────────────────

class TestAuthGuard:
    def test_returns_403_when_auth_fails(self):
        raw = _with_flag(True, lambda: _with_auth(False,
            lambda: _mkt.launch_campaign(1)))
        _, status = _unpack(raw)
        assert status == 403

    def test_service_not_called_when_auth_fails(self):
        svc = _make_svc(campaign=_make_campaign())
        _inject(svc, lambda: _with_flag(True, lambda: _with_auth(False,
            lambda: _mkt.launch_campaign(1))))
        svc.mark_running.assert_not_called()


# ── Tenant guard ──────────────────────────────────────────────────────────────

class TestTenantGuard:
    def test_returns_403_when_tenant_none(self):
        raw = _with_flag(True, lambda: _with_tenant(None,
            lambda: _mkt.launch_campaign(1)))
        _, status = _unpack(raw)
        assert status == 403

    def test_returns_403_when_tenant_empty_string(self):
        raw = _with_flag(True, lambda: _with_tenant("",
            lambda: _mkt.launch_campaign(1)))
        _, status = _unpack(raw)
        assert status == 403

    def test_service_not_called_when_tenant_none(self):
        svc = _make_svc(campaign=_make_campaign())
        _inject(svc, lambda: _with_flag(True, lambda: _with_tenant(None,
            lambda: _mkt.launch_campaign(1))))
        svc.mark_running.assert_not_called()


# ── Existence guard ───────────────────────────────────────────────────────────

class TestExistenceGuard:
    def test_returns_404_when_campaign_not_found(self):
        svc = _make_svc(campaign=None)
        _, status = _call(svc)
        assert status == 404

    def test_404_body_says_not_found(self):
        svc = _make_svc(campaign=None)
        body, _ = _call(svc)
        assert "not found" in str(body).lower()

    def test_mark_running_not_called_when_not_found(self):
        svc = _make_svc(campaign=None)
        _call(svc)
        svc.mark_running.assert_not_called()

    def test_get_campaign_called_with_correct_tenant_and_id(self):
        campaign = _make_campaign(id=7)
        svc = _make_svc(campaign=campaign)
        _call(svc, campaign_id=7, tenant="T_EXIST")
        svc.get_campaign.assert_called_once_with("T_EXIST", 7)


# ── Successful launch ─────────────────────────────────────────────────────────

class TestSuccessfulLaunch:
    def test_returns_200(self):
        campaign = _make_campaign(status="running")
        svc = _make_svc(campaign=campaign)
        _, status = _call(svc)
        assert status == 200

    def test_response_status_is_running(self):
        campaign = _make_campaign(status="running")
        svc = _make_svc(campaign=campaign)
        body, _ = _call(svc)
        assert body["status"] == "running"

    def test_response_is_full_detail(self):
        campaign = _make_campaign(status="running", total_recipients=5)
        svc = _make_svc(campaign=campaign)
        body, _ = _call(svc)
        assert "message_body" in body
        assert "description" in body
        assert "updated_at" in body

    def test_mark_running_called_with_correct_tenant(self):
        campaign = _make_campaign()
        svc = _make_svc(campaign=campaign)
        _call(svc, campaign_id=3, tenant="T_RUN")
        svc.mark_running.assert_called_once_with("T_RUN", 3)

    def test_mark_running_called_once(self):
        campaign = _make_campaign()
        svc = _make_svc(campaign=campaign)
        _call(svc)
        assert svc.mark_running.call_count == 1


# ── Error mapping ─────────────────────────────────────────────────────────────

class TestErrorMapping:
    @staticmethod
    def _live_svc():
        return sys.modules["app.marketing.campaign_service"]

    def test_transition_error_returns_409(self):
        exc = CampaignTransitionError("draft", "running")
        campaign = _make_campaign(status="draft")
        svc = _make_svc(campaign=campaign, raises=exc)
        _, status = _call(svc)
        assert status == 409

    def test_transition_error_body_contains_detail(self):
        exc = CampaignTransitionError("draft", "running")
        campaign = _make_campaign(status="draft")
        svc = _make_svc(campaign=campaign, raises=exc)
        body, _ = _call(svc)
        assert "transition" in str(body).lower()

    def test_validation_error_returns_400(self):
        result = ValidationResult(errors=("engine is off",))
        exc = self._live_svc().CampaignValidationError(result)
        campaign = _make_campaign()
        svc = _make_svc(campaign=campaign, raises=exc)
        _, status = _call(svc)
        assert status == 400

    def test_engine_disabled_returns_404(self):
        exc = self._live_svc().CampaignEngineDisabled("off")
        campaign = _make_campaign()
        svc = _make_svc(campaign=campaign, raises=exc)
        _, status = _call(svc)
        assert status == 404

    def test_unexpected_exception_propagates(self):
        campaign = _make_campaign()
        svc = _make_svc(campaign=campaign, raises=RuntimeError("db down"))
        with pytest.raises(RuntimeError, match="db down"):
            _call(svc)


# ── Worker isolation ──────────────────────────────────────────────────────────

class TestWorkerIsolation:
    """The launch route must not interact with the worker or WhatsApp at all."""

    def test_no_thread_created(self):
        """Launching a campaign does not start a thread in the route layer."""
        assert "threading" not in _MKT_CODE

    def test_no_whatsapp_call(self):
        assert "send_automation" not in _MKT_CODE
        assert "whatsapp_service" not in _MKT_CODE.lower()

    def test_no_worker_start(self):
        assert "init_campaign_worker" not in _MKT_CODE
        assert "campaign_worker" not in _MKT_CODE

    def test_service_does_not_return_worker_ref(self):
        """mark_running returns the Campaign object, not a thread or worker handle."""
        campaign = _make_campaign(status="running")
        svc = _make_svc(campaign=campaign)
        body, _ = _call(svc)
        # Response is a campaign detail dict, not a worker/thread reference.
        assert isinstance(body, dict)
        assert "id" in body
        assert "status" in body


# ── Audit ownership ───────────────────────────────────────────────────────────

class TestAuditOwnership:
    def test_route_does_not_call_log_audit(self):
        assert "log_audit" not in _MKT_CODE

    def test_route_does_not_call_audit_status_changed(self):
        assert "_audit_status_changed" not in _MKT_CODE

    def test_service_owns_audit(self):
        """CampaignService._audit_status_changed must exist (not removed)."""
        svc_src = open(
            os.path.join(_ROOT, "app", "marketing", "campaign_service.py"),
            encoding="utf-8",
        ).read()
        assert "_audit_status_changed" in svc_src


# ── Tenant isolation ──────────────────────────────────────────────────────────

class TestTenantIsolation:
    def test_wrong_tenant_campaign_returns_404(self):
        """Service returns None when campaign belongs to a different tenant."""
        svc = _make_svc(campaign=None)
        body, status = _call(svc, tenant="WRONG_TENANT")
        assert status == 404

    def test_mark_running_not_called_for_wrong_tenant(self):
        svc = _make_svc(campaign=None)
        _call(svc, tenant="WRONG_TENANT")
        svc.mark_running.assert_not_called()


# ── Legacy protection ─────────────────────────────────────────────────────────

class TestLegacyProtection:
    def test_admin_campaign_send_unchanged(self):
        src = open(
            os.path.join(_ROOT, "app", "routes", "admin.py"), encoding="utf-8"
        ).read()
        assert "from app.services.campaign_service import start_campaign" in src

    def test_broadcast_not_modified(self):
        src = open(
            os.path.join(_ROOT, "app", "routes", "broadcast.py"), encoding="utf-8"
        ).read()
        assert "marketing_bp" not in src
        assert "launch_campaign" not in src

    def test_followup_service_not_modified(self):
        src = open(
            os.path.join(_ROOT, "app", "services", "followup_service.py"),
            encoding="utf-8",
        ).read()
        assert "launch_campaign" not in src
        assert "mark_running" not in src

    def test_campaign_worker_not_modified(self):
        src = open(
            os.path.join(_ROOT, "app", "marketing", "campaign_worker.py"),
            encoding="utf-8",
        ).read()
        assert "launch_campaign" not in src
