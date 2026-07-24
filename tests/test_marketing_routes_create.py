"""
Phase 8.2D.3 — Campaign create route tests.

Route under test:
    POST /crm/campaigns/v2

Proves:
  - flag OFF → 404
  - unauthenticated → 403
  - None/empty tenant → 403
  - successful creation → 201 + campaign detail
  - created_by populated from actor
  - all payload fields forwarded to service (no duplication)
  - CampaignValidationError → 400 with structured errors
  - CampaignEngineDisabled → 404 (defence-in-depth)
  - unexpected exception re-raises through _map_campaign_error
  - route never touches db.session or Campaign model directly
  - rollback is the service's responsibility (not tested here)
  - legacy routes unchanged
"""
import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock, call

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MKT_PATH = os.path.join(_ROOT, "app", "routes", "marketing.py")
_MKT_SRC  = open(_MKT_PATH, encoding="utf-8").read()


# ── Stub infrastructure (same pattern as read tests) ─────────────────────────

class _FakeBlueprint:
    def __init__(self, name, *a, **kw):
        self.name = name

    def route(self, *a, **kw):
        def decorator(f):
            return f
        return decorator


def _ensure_stubs():
    for name in [
        "app", "app.flags", "app.routes", "app.routes.admin",
        "app.marketing", "app.marketing.campaign_service",
        "flask", "flask_login",
    ]:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)

    flask_mod = sys.modules["flask"]
    flask_mod.Blueprint   = _FakeBlueprint
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
        "authenticated": True, "username": "test_admin", "role": "ADMIN", "source": "SESSION"
    }


def _load_module(alias, path):
    spec = importlib.util.spec_from_file_location(alias, path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


_ensure_stubs()

_svc_mod = _load_module(
    "_p82d3_svc",
    os.path.join(_ROOT, "app", "marketing", "campaign_service.py"),
)
sys.modules["app.marketing.campaign_service"] = _svc_mod

_mkt = _load_module("_p82d3_mkt", _MKT_PATH)

CampaignEngineDisabled = _svc_mod.CampaignEngineDisabled
CampaignValidationError = _svc_mod.CampaignValidationError
ValidationResult = _svc_mod.ValidationResult


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_campaign(id=1, name="Test", status="draft",
                   total_recipients=0, sent_count=0, failed_count=0,
                   created_at=None, updated_at=None, created_by="test_admin",
                   scheduled_at=None, started_at=None, completed_at=None,
                   description="A promo", message_body="Hello",
                   template_id=None, audience_rule_id=None, failure_reason=None):
    c = MagicMock()
    c.id = id; c.name = name; c.status = status
    c.total_recipients = total_recipients
    c.sent_count = sent_count; c.failed_count = failed_count
    c.created_at = created_at; c.updated_at = updated_at
    c.created_by = created_by
    c.scheduled_at = scheduled_at; c.started_at = started_at
    c.completed_at = completed_at
    c.description = description; c.message_body = message_body
    c.template_id = template_id; c.audience_rule_id = audience_rule_id
    c.failure_reason = failure_reason
    return c


def _make_svc_stub(campaign=None, raise_exc=None):
    svc = MagicMock()
    if raise_exc is not None:
        svc.create_campaign.side_effect = raise_exc
    else:
        svc.create_campaign.return_value = campaign or _make_campaign()
    return svc


def _set_request(json_body=None):
    flask_mod = sys.modules["flask"]
    req = MagicMock()
    req.get_json = lambda silent=False: json_body
    flask_mod.request = req


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


def _with_actor(actor_dict, fn):
    sys.modules["app.routes.admin"].get_current_actor = lambda: actor_dict
    try:
        return fn()
    finally:
        sys.modules["app.routes.admin"].get_current_actor = lambda: {
            "authenticated": True, "username": "test_admin",
            "role": "ADMIN", "source": "SESSION",
        }


def _unpack(raw):
    """Normalise route return values to (body_dict, http_status).

    The stub jsonify returns (dict, {}). Routes that set an explicit HTTP
    status return (jsonify_result, int) = ((dict, {}), int). Routes that
    return errors from _map_campaign_error or the guard helpers use the same
    shape. This helper unwraps both layers to give (dict, int).
    """
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], int):
        jsonify_result, status = raw
        body = jsonify_result[0] if isinstance(jsonify_result, tuple) else jsonify_result
        return body, status
    # Should not happen for the create route, but handle gracefully.
    body = raw[0] if isinstance(raw, tuple) else raw
    return body, 200


def _run(svc_stub, json_body=None, tenant="T1"):
    # Use sentinel to distinguish "caller passed {}" from "caller passed nothing"
    payload = json_body if json_body is not None else {"name": "Promo", "message_body": "Hello"}
    _set_request(payload)
    orig = _mkt._make_service
    _mkt._make_service = lambda: svc_stub
    try:
        raw = _with_flag(True, lambda: _with_tenant(tenant,
            lambda: _mkt.create_campaign()))
        return _unpack(raw)
    finally:
        _mkt._make_service = orig


# ── Source-level assertions ───────────────────────────────────────────────────

class TestSourceStructure:
    def test_post_route_defined(self):
        assert "def create_campaign" in _MKT_SRC

    def test_no_db_session_in_route(self):
        assert "db.session" not in _MKT_SRC

    def test_no_direct_model_instantiation(self):
        assert "Campaign(" not in _MKT_SRC

    def test_service_create_called(self):
        assert "svc.create_campaign" in _MKT_SRC

    def test_map_campaign_error_used_in_create(self):
        # The create route wraps the service call in try/except → _map_campaign_error
        assert "_map_campaign_error(exc)" in _MKT_SRC

    def test_tenant_resolved_for_create(self):
        # At least 4 _require_tenant() calls: 3 read routes + 1 create
        assert _MKT_SRC.count("_require_tenant()") >= 4

    def test_auth_checked_for_create(self):
        assert _MKT_SRC.count("_check_auth()") >= 4

    def test_created_by_populated(self):
        assert "created_by" in _MKT_SRC
        assert "get_current_actor" in _MKT_SRC

    def test_no_validation_duplicated(self):
        """Route must not re-implement campaign name/body checks."""
        assert "len(name)" not in _MKT_SRC
        assert "name is required" not in _MKT_SRC
        assert "message_body is required" not in _MKT_SRC

    def test_legacy_service_not_imported(self):
        assert "app.services.campaign_service" not in _MKT_SRC


# ── Flag guard ────────────────────────────────────────────────────────────────

class TestFlagGuard:
    def test_returns_404_when_flag_off(self):
        _set_request({"name": "X", "message_body": "Y"})
        raw = _with_flag(False, lambda: _mkt.create_campaign())
        _, status = _unpack(raw)
        assert status == 404

    def test_does_not_call_service_when_flag_off(self):
        svc = _make_svc_stub()
        _set_request({"name": "X", "message_body": "Y"})
        orig = _mkt._make_service
        _mkt._make_service = lambda: svc
        try:
            _with_flag(False, lambda: _mkt.create_campaign())
        finally:
            _mkt._make_service = orig
        svc.create_campaign.assert_not_called()


# ── Auth guard ────────────────────────────────────────────────────────────────

class TestAuthGuard:
    def test_returns_403_when_auth_fails(self):
        _set_request({"name": "X", "message_body": "Y"})
        def go():
            return _with_auth(False, lambda: _mkt.create_campaign())
        raw = _with_flag(True, go)
        _, status = _unpack(raw)
        assert status == 403

    def test_does_not_call_service_when_auth_fails(self):
        svc = _make_svc_stub()
        _set_request({"name": "X", "message_body": "Y"})
        orig = _mkt._make_service
        _mkt._make_service = lambda: svc
        try:
            def go():
                return _with_auth(False, lambda: _mkt.create_campaign())
            _with_flag(True, go)
        finally:
            _mkt._make_service = orig
        svc.create_campaign.assert_not_called()


# ── Tenant guard ──────────────────────────────────────────────────────────────

class TestTenantGuard:
    def test_returns_403_when_tenant_none(self):
        _set_request({"name": "X", "message_body": "Y"})
        def go():
            return _with_tenant(None, lambda: _mkt.create_campaign())
        raw = _with_flag(True, go)
        _, status = _unpack(raw)
        assert status == 403

    def test_returns_403_when_tenant_empty_string(self):
        _set_request({"name": "X", "message_body": "Y"})
        def go():
            return _with_tenant("", lambda: _mkt.create_campaign())
        raw = _with_flag(True, go)
        _, status = _unpack(raw)
        assert status == 403

    def test_does_not_call_service_when_tenant_none(self):
        svc = _make_svc_stub()
        _set_request({"name": "X", "message_body": "Y"})
        orig = _mkt._make_service
        _mkt._make_service = lambda: svc
        try:
            _with_flag(True, lambda: _with_tenant(None, lambda: _mkt.create_campaign()))
        finally:
            _mkt._make_service = orig
        svc.create_campaign.assert_not_called()


# ── Successful creation ───────────────────────────────────────────────────────

class TestSuccessfulCreation:
    def test_returns_201(self):
        svc = _make_svc_stub(campaign=_make_campaign(id=7))
        _, status = _run(svc)
        assert status == 201

    def test_response_contains_id(self):
        svc = _make_svc_stub(campaign=_make_campaign(id=42, name="Oxford Promo"))
        body, _ = _run(svc, json_body={"name": "Oxford Promo", "message_body": "Hi"})
        assert body["id"] == 42

    def test_response_contains_name(self):
        svc = _make_svc_stub(campaign=_make_campaign(name="Oxford Promo"))
        body, _ = _run(svc, json_body={"name": "Oxford Promo", "message_body": "Hi"})
        assert body["name"] == "Oxford Promo"

    def test_response_contains_status(self):
        svc = _make_svc_stub(campaign=_make_campaign(status="draft"))
        body, _ = _run(svc)
        assert body["status"] == "draft"

    def test_response_contains_created_at(self):
        svc = _make_svc_stub(campaign=_make_campaign())
        body, _ = _run(svc)
        assert "created_at" in body

    def test_response_is_detail_not_summary(self):
        """create returns full detail (message_body, description) not just summary."""
        svc = _make_svc_stub(campaign=_make_campaign(message_body="Hello"))
        body, _ = _run(svc)
        assert "message_body" in body
        assert "description" in body

    def test_response_contains_updated_at(self):
        svc = _make_svc_stub(campaign=_make_campaign())
        body, _ = _run(svc)
        assert "updated_at" in body


# ── Service call contract ─────────────────────────────────────────────────────

class TestServiceCallContract:
    def test_tenant_id_passed_correctly(self):
        svc = _make_svc_stub()
        _run(svc, tenant="TENANT_XYZ")
        args, _ = svc.create_campaign.call_args
        assert args[0] == "TENANT_XYZ"

    def test_name_forwarded(self):
        svc = _make_svc_stub()
        _run(svc, json_body={"name": "Summer Promo", "message_body": "Hi"})
        _, kwargs = svc.create_campaign.call_args
        assert kwargs.get("name") == "Summer Promo"

    def test_message_body_forwarded(self):
        svc = _make_svc_stub()
        _run(svc, json_body={"name": "X", "message_body": "Buy now!"})
        _, kwargs = svc.create_campaign.call_args
        assert kwargs.get("message_body") == "Buy now!"

    def test_description_forwarded(self):
        svc = _make_svc_stub()
        _run(svc, json_body={"name": "X", "message_body": "Y", "description": "Q1 promo"})
        _, kwargs = svc.create_campaign.call_args
        assert kwargs.get("description") == "Q1 promo"

    def test_template_id_forwarded(self):
        svc = _make_svc_stub()
        _run(svc, json_body={"name": "X", "template_id": 5})
        _, kwargs = svc.create_campaign.call_args
        assert kwargs.get("template_id") == 5

    def test_audience_rule_id_forwarded(self):
        svc = _make_svc_stub()
        _run(svc, json_body={"name": "X", "message_body": "Y", "audience_rule_id": 3})
        _, kwargs = svc.create_campaign.call_args
        assert kwargs.get("audience_rule_id") == 3

    def test_created_by_set_from_actor_username(self):
        svc = _make_svc_stub()
        actor = {"authenticated": True, "username": "alice", "role": "ADMIN", "source": "SESSION"}
        def go():
            return _with_actor(actor, lambda: _run(svc))
        _with_flag(True, go)
        _, kwargs = svc.create_campaign.call_args
        assert kwargs.get("created_by") == "alice"

    def test_created_by_none_when_unauthenticated_actor(self):
        svc = _make_svc_stub()
        actor = {"authenticated": False, "username": None, "role": None, "source": None}
        def go():
            return _with_actor(actor, lambda: _run(svc))
        _with_flag(True, go)
        _, kwargs = svc.create_campaign.call_args
        assert kwargs.get("created_by") is None

    def test_missing_fields_passed_as_none(self):
        """Fields absent from the JSON body must not raise — service handles validation."""
        svc = _make_svc_stub()
        _run(svc, json_body={})   # empty body — name=None, message_body=None etc.
        _, kwargs = svc.create_campaign.call_args
        assert kwargs.get("name") is None
        assert kwargs.get("message_body") is None

    def test_null_json_body_treated_as_empty(self):
        """If Content-Type is wrong or body is absent, get_json returns None → {} fallback."""
        svc = _make_svc_stub()
        _set_request(None)  # simulates get_json() returning None
        orig = _mkt._make_service
        _mkt._make_service = lambda: svc
        try:
            _with_flag(True, lambda: _with_tenant("T1", lambda: _mkt.create_campaign()))
        finally:
            _mkt._make_service = orig
        svc.create_campaign.assert_called_once()


# ── Error mapping ─────────────────────────────────────────────────────────────

class TestErrorMapping:
    def _make_validation_error(self, *msgs):
        result = ValidationResult(errors=tuple(msgs))
        return CampaignValidationError(result)

    def test_validation_error_returns_400(self):
        exc = self._make_validation_error("name is required")
        svc = _make_svc_stub(raise_exc=exc)
        _, status = _run(svc)
        assert status == 400

    def test_validation_error_body_contains_errors(self):
        exc = self._make_validation_error("name is required")
        svc = _make_svc_stub(raise_exc=exc)
        body, _ = _run(svc)
        assert "name is required" in str(body)

    def test_validation_error_body_has_detail_key(self):
        exc = self._make_validation_error("message_body is required")
        svc = _make_svc_stub(raise_exc=exc)
        body, _ = _run(svc)
        assert "detail" in body

    def test_engine_disabled_returns_404(self):
        exc = CampaignEngineDisabled("off")
        svc = _make_svc_stub(raise_exc=exc)
        _, status = _run(svc)
        assert status == 404

    def test_unexpected_exception_propagates(self):
        svc = _make_svc_stub(raise_exc=RuntimeError("db exploded"))
        with pytest.raises(RuntimeError, match="db exploded"):
            _run(svc)

    def test_no_db_rollback_in_route(self):
        """Route must not call db.session.rollback() — that is the service's job."""
        assert "session.rollback" not in _MKT_SRC
        assert "db.rollback" not in _MKT_SRC

    def test_no_db_commit_in_route(self):
        """Route must not call db.session.commit() — that is the service's job."""
        assert "session.commit" not in _MKT_SRC
        assert "db.commit" not in _MKT_SRC


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

    def test_campaign_service_not_modified(self):
        """marketing.py must not import from legacy app.services.campaign_service."""
        assert "app.services.campaign_service" not in _MKT_SRC
