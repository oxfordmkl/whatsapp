"""
Phase 8.2D.4 — Campaign lifecycle route tests.

Routes under test:
    POST /crm/campaigns/v2/<id>/validate
    POST /crm/campaigns/v2/<id>/schedule
    POST /crm/campaigns/v2/<id>/cancel
    POST /crm/campaigns/v2/<id>/archive

Each route proves:
  - flag OFF → 404
  - unauthenticated → 403
  - None/empty tenant → 403
  - campaign not found (wrong tenant / missing) → 404
  - service method called with correct args
  - CampaignTransitionError → 409
  - CampaignValidationError → 400
  - no db.session, no commit/rollback in route
  - response is full campaign detail

Schedule route additionally proves:
  - scheduled_at required → 400
  - invalid ISO datetime → 400
  - valid datetime forwarded as datetime object to service

No worker interaction, no WhatsApp calls, no legacy route changes.
"""
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MKT_PATH = os.path.join(_ROOT, "app", "routes", "marketing.py")
_MKT_SRC  = open(_MKT_PATH, encoding="utf-8").read()


def _strip_comments(src: str) -> str:
    """Remove docstrings and # comments — leaves only executable code."""
    import tokenize, io
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
        "authenticated": True, "username": "admin", "role": "ADMIN", "source": "SESSION",
    }


def _load_module(alias, path):
    spec = importlib.util.spec_from_file_location(alias, path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


_ensure_stubs()

_svc_mod = _load_module(
    "_p82d4_svc",
    os.path.join(_ROOT, "app", "marketing", "campaign_service.py"),
)
sys.modules["app.marketing.campaign_service"] = _svc_mod

_mkt = _load_module("_p82d4_mkt", _MKT_PATH)

CampaignEngineDisabled  = _svc_mod.CampaignEngineDisabled
CampaignValidationError = _svc_mod.CampaignValidationError
CampaignTransitionError = _svc_mod.CampaignTransitionError
ValidationResult        = _svc_mod.ValidationResult


# ── Test utilities ────────────────────────────────────────────────────────────

def _make_campaign(id=1, name="Test", status="draft", **kw):
    c = MagicMock()
    c.id = id; c.name = name; c.status = status
    c.total_recipients = kw.get("total_recipients", 0)
    c.sent_count = kw.get("sent_count", 0)
    c.failed_count = kw.get("failed_count", 0)
    c.created_at = kw.get("created_at"); c.updated_at = kw.get("updated_at")
    c.created_by = kw.get("created_by", "admin")
    c.scheduled_at = kw.get("scheduled_at"); c.started_at = kw.get("started_at")
    c.completed_at = kw.get("completed_at"); c.description = kw.get("description")
    c.message_body = kw.get("message_body", "Hello")
    c.template_id = kw.get("template_id"); c.audience_rule_id = kw.get("audience_rule_id")
    c.failure_reason = kw.get("failure_reason")
    return c


def _make_svc(campaign=None, action_return=None, action_raises=None):
    """Service stub. get_campaign controls the existence pre-check."""
    svc = MagicMock()
    svc.get_campaign.return_value = campaign
    for method in ("mark_validated", "cancel", "archive", "schedule"):
        stub = getattr(svc, method)
        if action_raises is not None:
            stub.side_effect = action_raises
        else:
            stub.return_value = action_return or campaign or _make_campaign()
    return svc


def _unpack(raw):
    """Normalise all route returns to (body_dict, http_status)."""
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], int):
        inner, status = raw
        body = inner[0] if isinstance(inner, tuple) else inner
        return body, status
    body = raw[0] if isinstance(raw, tuple) else raw
    return body, 200


def _set_request(json_body=None):
    req = MagicMock()
    req.get_json = lambda silent=False: json_body
    sys.modules["flask"].request = req


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


def _inject_svc(svc, fn):
    orig = _mkt._make_service
    _mkt._make_service = lambda: svc
    try:
        return fn()
    finally:
        _mkt._make_service = orig


def _call(route_fn, svc, tenant="T1", json_body=None):
    """Call a lifecycle route with flag ON, real tenant, and injected svc."""
    _set_request(json_body)
    return _unpack(
        _inject_svc(svc, lambda: _with_flag(True, lambda:
            _with_tenant(tenant, route_fn)))
    )


# ── Source-level assertions ───────────────────────────────────────────────────

class TestSourceStructure:
    def test_validate_route_defined(self):
        assert "def validate_campaign" in _MKT_SRC

    def test_schedule_route_defined(self):
        assert "def schedule_campaign" in _MKT_SRC

    def test_cancel_route_defined(self):
        assert "def cancel_campaign" in _MKT_SRC

    def test_archive_route_defined(self):
        assert "def archive_campaign" in _MKT_SRC

    def test_run_lifecycle_helper_defined(self):
        assert "def _run_lifecycle" in _MKT_SRC

    def test_no_db_session_in_routes(self):
        assert "db.session" not in _MKT_SRC

    def test_no_commit_in_routes(self):
        assert "session.commit" not in _MKT_SRC

    def test_no_rollback_in_routes(self):
        assert "session.rollback" not in _MKT_SRC

    def test_no_direct_model_access(self):
        assert "Campaign.query" not in _MKT_SRC

    def test_no_transition_logic_in_routes(self):
        """Routes must not reference ALLOWED_TRANSITIONS or status constants."""
        assert "ALLOWED_TRANSITIONS" not in _MKT_SRC
        assert "can_transition" not in _MKT_SRC

    def test_schedule_parses_datetime(self):
        assert "fromisoformat" in _MKT_SRC

    def test_schedule_requires_scheduled_at(self):
        assert "scheduled_at is required" in _MKT_SRC

    def test_existence_check_before_action(self):
        """Route must guard with get_campaign() before calling lifecycle methods."""
        assert "svc.get_campaign" in _MKT_SRC
        assert "Campaign not found" in _MKT_SRC

    def test_no_worker_import(self):
        assert "campaign_worker" not in _MKT_SRC

    def test_legacy_service_not_imported(self):
        assert "app.services.campaign_service" not in _MKT_SRC


# ── Parametrised: common guards apply to all four routes ─────────────────────

_SIMPLE_ROUTES = [
    ("validate_campaign", lambda: _mkt.validate_campaign(1)),
    ("cancel_campaign",   lambda: _mkt.cancel_campaign(1)),
    ("archive_campaign",  lambda: _mkt.archive_campaign(1)),
]


class TestCommonGuards:
    """Flag, auth, tenant, and not-found guards for validate/cancel/archive."""

    @pytest.mark.parametrize("name,route_fn", _SIMPLE_ROUTES)
    def test_flag_off_returns_404(self, name, route_fn):
        _set_request()
        raw = _with_flag(False, route_fn)
        _, status = _unpack(raw)
        assert status == 404, f"{name}: expected 404 when flag off"

    @pytest.mark.parametrize("name,route_fn", _SIMPLE_ROUTES)
    def test_auth_failure_returns_403(self, name, route_fn):
        _set_request()
        raw = _with_flag(True, lambda: _with_auth(False, route_fn))
        _, status = _unpack(raw)
        assert status == 403, f"{name}: expected 403 when auth fails"

    @pytest.mark.parametrize("name,route_fn", _SIMPLE_ROUTES)
    def test_none_tenant_returns_403(self, name, route_fn):
        _set_request()
        raw = _with_flag(True, lambda: _with_tenant(None, route_fn))
        _, status = _unpack(raw)
        assert status == 403, f"{name}: expected 403 when tenant is None"

    @pytest.mark.parametrize("name,route_fn", _SIMPLE_ROUTES)
    def test_campaign_not_found_returns_404(self, name, route_fn):
        svc = _make_svc(campaign=None)
        body, status = _call(route_fn, svc)
        assert status == 404, f"{name}: expected 404 when campaign not found"
        assert "not found" in str(body).lower()

    @pytest.mark.parametrize("name,route_fn", _SIMPLE_ROUTES)
    def test_transition_error_returns_409(self, name, route_fn):
        exc = CampaignTransitionError("running", "draft")
        campaign = _make_campaign()
        svc = _make_svc(campaign=campaign, action_raises=exc)
        _, status = _call(route_fn, svc)
        assert status == 409, f"{name}: expected 409 on illegal transition"

    @pytest.mark.parametrize("name,route_fn", _SIMPLE_ROUTES)
    def test_validation_error_returns_400(self, name, route_fn):
        result = ValidationResult(errors=("something invalid",))
        exc = CampaignValidationError(result)
        campaign = _make_campaign()
        svc = _make_svc(campaign=campaign, action_raises=exc)
        _, status = _call(route_fn, svc)
        assert status == 400, f"{name}: expected 400 on validation error"

    @pytest.mark.parametrize("name,route_fn", _SIMPLE_ROUTES)
    def test_action_not_called_when_not_found(self, name, route_fn):
        svc = _make_svc(campaign=None)
        _call(route_fn, svc)
        svc.mark_validated.assert_not_called()
        svc.cancel.assert_not_called()
        svc.archive.assert_not_called()


# ── validate_campaign ─────────────────────────────────────────────────────────

class TestValidateCampaign:
    def test_calls_mark_validated_with_correct_args(self):
        campaign = _make_campaign(id=5, status="validated")
        svc = _make_svc(campaign=campaign)
        _call(lambda: _mkt.validate_campaign(5), svc, tenant="T99")
        svc.mark_validated.assert_called_once_with("T99", 5)

    def test_returns_campaign_detail_on_success(self):
        campaign = _make_campaign(id=5, status="validated", message_body="Hi")
        svc = _make_svc(campaign=campaign)
        body, status = _call(lambda: _mkt.validate_campaign(5), svc)
        assert status == 200
        assert body["status"] == "validated"
        assert "message_body" in body

    def test_unexpected_exception_propagates(self):
        campaign = _make_campaign()
        svc = _make_svc(campaign=campaign, action_raises=RuntimeError("db down"))
        with pytest.raises(RuntimeError, match="db down"):
            _call(lambda: _mkt.validate_campaign(1), svc)


# ── schedule_campaign ─────────────────────────────────────────────────────────

class TestScheduleCampaign:
    _FUTURE = (datetime.utcnow() + timedelta(hours=2)).isoformat()

    def _run(self, svc, json_body=None, tenant="T1"):
        _set_request(json_body)
        return _unpack(
            _inject_svc(svc, lambda: _with_flag(True, lambda:
                _with_tenant(tenant, lambda: _mkt.schedule_campaign(1))))
        )

    def test_flag_off_returns_404(self):
        _set_request({"scheduled_at": self._FUTURE})
        raw = _with_flag(False, lambda: _mkt.schedule_campaign(1))
        _, status = _unpack(raw)
        assert status == 404

    def test_auth_failure_returns_403(self):
        _set_request({"scheduled_at": self._FUTURE})
        raw = _with_flag(True, lambda: _with_auth(False,
            lambda: _mkt.schedule_campaign(1)))
        _, status = _unpack(raw)
        assert status == 403

    def test_none_tenant_returns_403(self):
        _set_request({"scheduled_at": self._FUTURE})
        raw = _with_flag(True, lambda: _with_tenant(None,
            lambda: _mkt.schedule_campaign(1)))
        _, status = _unpack(raw)
        assert status == 403

    def test_missing_scheduled_at_returns_400(self):
        svc = _make_svc(campaign=_make_campaign())
        _, status = self._run(svc, json_body={})
        assert status == 400

    def test_missing_body_returns_400(self):
        svc = _make_svc(campaign=_make_campaign())
        _, status = self._run(svc, json_body=None)
        assert status == 400

    def test_invalid_datetime_returns_400(self):
        svc = _make_svc(campaign=_make_campaign())
        _, status = self._run(svc, json_body={"scheduled_at": "not-a-date"})
        assert status == 400

    def test_invalid_datetime_body_says_iso_format(self):
        svc = _make_svc(campaign=_make_campaign())
        body, _ = self._run(svc, json_body={"scheduled_at": "not-a-date"})
        assert "iso" in str(body).lower()

    def test_campaign_not_found_returns_404(self):
        svc = _make_svc(campaign=None)
        _, status = self._run(svc, json_body={"scheduled_at": self._FUTURE})
        assert status == 404

    def test_calls_service_with_datetime_object(self):
        campaign = _make_campaign(status="scheduled")
        svc = _make_svc(campaign=campaign)
        self._run(svc, json_body={"scheduled_at": self._FUTURE})
        svc.schedule.assert_called_once()
        # scheduled_at is passed positionally: schedule(tenant_id, campaign_id, scheduled_at)
        pos_args, kw_args = svc.schedule.call_args
        scheduled_at_val = pos_args[2] if len(pos_args) > 2 else kw_args.get("scheduled_at")
        assert isinstance(scheduled_at_val, datetime), (
            "scheduled_at must be a datetime object, not a string"
        )

    def test_calls_service_with_correct_tenant_and_id(self):
        campaign = _make_campaign(status="scheduled")
        svc = _make_svc(campaign=campaign)
        _set_request({"scheduled_at": self._FUTURE})
        _inject_svc(svc, lambda: _with_flag(True, lambda:
            _with_tenant("T_SCHED", lambda: _mkt.schedule_campaign(7))))
        svc.schedule.assert_called_once()
        args, _ = svc.schedule.call_args
        assert args[0] == "T_SCHED"
        assert args[1] == 7

    def test_transition_error_returns_409(self):
        exc = CampaignTransitionError("draft", "scheduled")
        campaign = _make_campaign()
        svc = _make_svc(campaign=campaign, action_raises=exc)
        _, status = self._run(svc, json_body={"scheduled_at": self._FUTURE})
        assert status == 409

    def test_validation_error_returns_400(self):
        result = ValidationResult(errors=("scheduled_at must be in the future",))
        exc = CampaignValidationError(result)
        campaign = _make_campaign()
        svc = _make_svc(campaign=campaign, action_raises=exc)
        _, status = self._run(svc, json_body={"scheduled_at": self._FUTURE})
        assert status == 400

    def test_returns_campaign_detail_on_success(self):
        campaign = _make_campaign(status="scheduled")
        svc = _make_svc(campaign=campaign)
        body, status = self._run(svc, json_body={"scheduled_at": self._FUTURE})
        assert status == 200
        assert body["status"] == "scheduled"
        assert "message_body" in body

    def test_datetime_not_called_before_existence_check(self):
        """A valid datetime but missing campaign must return 404, not 409."""
        svc = _make_svc(campaign=None)
        _, status = self._run(svc, json_body={"scheduled_at": self._FUTURE})
        assert status == 404
        svc.schedule.assert_not_called()


# ── cancel_campaign ───────────────────────────────────────────────────────────

class TestCancelCampaign:
    def test_calls_cancel_with_correct_args(self):
        campaign = _make_campaign(status="cancelled")
        svc = _make_svc(campaign=campaign)
        _call(lambda: _mkt.cancel_campaign(3), svc, tenant="TCAN")
        svc.cancel.assert_called_once_with("TCAN", 3)

    def test_returns_campaign_detail_on_success(self):
        campaign = _make_campaign(status="cancelled")
        svc = _make_svc(campaign=campaign)
        body, status = _call(lambda: _mkt.cancel_campaign(3), svc)
        assert status == 200
        assert body["status"] == "cancelled"

    def test_409_on_illegal_transition(self):
        exc = CampaignTransitionError("completed", "cancelled")
        campaign = _make_campaign(status="completed")
        svc = _make_svc(campaign=campaign, action_raises=exc)
        _, status = _call(lambda: _mkt.cancel_campaign(3), svc)
        assert status == 409


# ── archive_campaign ──────────────────────────────────────────────────────────

class TestArchiveCampaign:
    def test_calls_archive_with_correct_args(self):
        campaign = _make_campaign(status="archived")
        svc = _make_svc(campaign=campaign)
        _call(lambda: _mkt.archive_campaign(9), svc, tenant="TARC")
        svc.archive.assert_called_once_with("TARC", 9)

    def test_returns_campaign_detail_on_success(self):
        campaign = _make_campaign(status="archived")
        svc = _make_svc(campaign=campaign)
        body, status = _call(lambda: _mkt.archive_campaign(9), svc)
        assert status == 200
        assert body["status"] == "archived"

    def test_409_on_illegal_transition(self):
        exc = CampaignTransitionError("draft", "archived")
        campaign = _make_campaign(status="draft")
        svc = _make_svc(campaign=campaign, action_raises=exc)
        _, status = _call(lambda: _mkt.archive_campaign(9), svc)
        assert status == 409


# ── Tenant isolation ──────────────────────────────────────────────────────────

class TestTenantIsolation:
    """get_campaign must be called with the actor's own tenant_id."""

    def test_validate_passes_tenant_to_get_campaign(self):
        campaign = _make_campaign()
        svc = _make_svc(campaign=campaign)
        _call(lambda: _mkt.validate_campaign(1), svc, tenant="T_ISO")
        svc.get_campaign.assert_called_once_with("T_ISO", 1)

    def test_cancel_passes_tenant_to_get_campaign(self):
        campaign = _make_campaign()
        svc = _make_svc(campaign=campaign)
        _call(lambda: _mkt.cancel_campaign(2), svc, tenant="T_ISO")
        svc.get_campaign.assert_called_once_with("T_ISO", 2)

    def test_archive_passes_tenant_to_get_campaign(self):
        campaign = _make_campaign()
        svc = _make_svc(campaign=campaign)
        _call(lambda: _mkt.archive_campaign(3), svc, tenant="T_ISO")
        svc.get_campaign.assert_called_once_with("T_ISO", 3)

    def test_different_tenant_cannot_access_campaign(self):
        """Service returns None for a campaign belonging to a different tenant."""
        svc = _make_svc(campaign=None)  # correct tenant check returns nothing
        body, status = _call(lambda: _mkt.validate_campaign(99), svc, tenant="T_WRONG")
        assert status == 404


# ── No worker interaction ─────────────────────────────────────────────────────

class TestNoWorkerInteraction:
    def test_worker_not_imported(self):
        assert "campaign_worker" not in _MKT_CODE

    def test_init_campaign_worker_not_referenced(self):
        assert "init_campaign_worker" not in _MKT_CODE

    def test_send_automation_not_referenced(self):
        assert "send_automation" not in _MKT_CODE


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

    def test_campaign_worker_not_modified(self):
        src = open(
            os.path.join(_ROOT, "app", "marketing", "campaign_worker.py"),
            encoding="utf-8",
        ).read()
        assert "validate_campaign" not in src
        assert "cancel_campaign" not in src
        assert "archive_campaign" not in src
