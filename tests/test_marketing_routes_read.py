"""
Phase 8.2D.2 — Campaign read route tests.

Routes under test:
    GET /crm/campaigns/v2
    GET /crm/campaigns/v2/<campaign_id>
    GET /crm/campaigns/v2/<campaign_id>/progress

Proves:
  - flag OFF → all three routes return 404
  - flag ON + no tenant → 403
  - flag ON + tenant → delegate to CampaignService (no direct DB)
  - list: returns campaigns, total, page, limit, pages
  - list: status filter forwarded to service
  - list: pagination arithmetic correct (ceiling division)
  - get: 404 when campaign not found or wrong tenant
  - get: detail fields present
  - progress: 404 when campaign not found
  - progress: breakdown + derived total returned
  - auth: 403 when check_auth() returns False
  - no legacy routes changed
  - no direct DB queries in route code
  - service methods called with correct tenant_id

All tests use the _load() isolation pattern — no Flask test client,
no SQLAlchemy, no app bootstrap. CampaignService is replaced by a
configurable stub injected via monkeypatch of _make_service.
"""
import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock, call, patch

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MKT_PATH  = os.path.join(_ROOT, "app", "routes", "marketing.py")
_MKT_SRC   = open(_MKT_PATH, encoding="utf-8").read()


# ── Stub infrastructure ───────────────────────────────────────────────────────

class _FakeBlueprint:
    """Minimal Blueprint stub that preserves decorated functions.

    The real Flask Blueprint.route() returns a decorator that registers the
    function AND returns it unchanged. MagicMock() does not do that — it
    returns another MagicMock, losing the real function. This stub replicates
    the real behaviour so _mkt.list_campaigns etc. stay callable.
    """
    def __init__(self, name, *a, **kw):
        self.name = name

    def route(self, *a, **kw):
        """Return an identity decorator (register but preserve the function)."""
        def decorator(f):
            return f
        return decorator


def _ensure_stubs():
    """Guarantee the minimum sys.modules stubs before loading marketing.py."""
    for name in [
        "app", "app.flags", "app.routes", "app.routes.admin",
        "app.marketing", "app.marketing.campaign_service",
        "flask", "flask_login",
    ]:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)

    flask_mod = sys.modules["flask"]
    # Always set Blueprint to our identity-preserving stub, overriding whatever
    # a previously loaded test file may have put there (e.g. a MagicMock from
    # test_marketing_routes_skeleton.py). If flask_mod already has jsonify/
    # request/current_app from that earlier load, they are still valid and we
    # leave them unless they are missing.
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


def _load_module(alias, path):
    spec = importlib.util.spec_from_file_location(alias, path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


_ensure_stubs()

# Load the real campaign_service so exception classes are genuine objects.
_svc_mod = _load_module(
    "_p82d2_svc",
    os.path.join(_ROOT, "app", "marketing", "campaign_service.py"),
)
sys.modules["app.marketing.campaign_service"] = _svc_mod

# Load marketing.py
_mkt = _load_module("_p82d2_mkt", _MKT_PATH)


# ── Fixtures / helpers ────────────────────────────────────────────────────────

def _make_campaign(id=1, name="Test", status="draft",
                   total_recipients=10, sent_count=3, failed_count=1,
                   scheduled_at=None, started_at=None, completed_at=None,
                   created_at=None, updated_at=None, created_by="admin",
                   description=None, message_body="Hello", template_id=None,
                   audience_rule_id=None, failure_reason=None):
    """Return a minimal Campaign-like object (no SQLAlchemy dependency)."""
    c = MagicMock()
    c.id               = id
    c.name             = name
    c.status           = status
    c.total_recipients = total_recipients
    c.sent_count       = sent_count
    c.failed_count     = failed_count
    c.scheduled_at     = scheduled_at
    c.started_at       = started_at
    c.completed_at     = completed_at
    c.created_at       = created_at
    c.updated_at       = updated_at
    c.created_by       = created_by
    c.description      = description
    c.message_body     = message_body
    c.template_id      = template_id
    c.audience_rule_id = audience_rule_id
    c.failure_reason   = failure_reason
    return c


def _make_service_stub(campaigns=None, campaign=None, breakdown=None,
                       total=None):
    """Return a CampaignService stub with controllable return values."""
    svc           = MagicMock()
    repo          = MagicMock()
    svc.repository = repo

    svc.list_campaigns.return_value = campaigns if campaigns is not None else []
    svc.get_campaign.return_value   = campaign
    svc.progress.return_value       = breakdown if breakdown is not None else {}

    # count_for_tenant supports the pagination total
    repo.count_for_tenant.return_value = total if total is not None else (
        len(campaigns) if campaigns else 0
    )
    return svc


def _with_flag(value, fn):
    """Run fn with the engine flag set to value, then restore."""
    sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: value
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


def _with_auth(value, fn):
    sys.modules["app.routes.admin"].check_auth = lambda: value
    try:
        return fn()
    finally:
        sys.modules["app.routes.admin"].check_auth = lambda: True


# Thin request stub so routes can read req.args
class _FakeArgs(dict):
    def get(self, key, default=None, type=None):
        val = super().get(key, default)
        if type is not None and val is not None:
            try:
                return type(val)
            except (ValueError, TypeError):
                return default
        return val


def _set_request(args=None):
    flask_mod = sys.modules["flask"]
    req       = MagicMock()
    req.args  = _FakeArgs(args or {})
    flask_mod.request = req


# ── Source-level assertions ───────────────────────────────────────────────────

class TestSourceStructure:
    def test_list_route_defined(self):
        assert "def list_campaigns" in _MKT_SRC

    def test_get_route_defined(self):
        assert "def get_campaign" in _MKT_SRC

    def test_progress_route_defined(self):
        assert "def campaign_progress" in _MKT_SRC

    def test_no_direct_db_in_routes(self):
        """Routes must not import db or call db.session directly."""
        assert "db.session" not in _MKT_SRC
        assert "from app.extensions import db" not in _MKT_SRC

    def test_no_direct_model_query_in_routes(self):
        """Routes must not call Campaign.query or session.query(Campaign)."""
        assert "Campaign.query" not in _MKT_SRC
        assert 'query(Campaign)' not in _MKT_SRC

    def test_list_campaigns_uses_service(self):
        assert "svc.list_campaigns" in _MKT_SRC

    def test_get_campaign_uses_service(self):
        assert "svc.get_campaign" in _MKT_SRC

    def test_progress_uses_service(self):
        assert "svc.progress" in _MKT_SRC

    def test_tenant_resolved_in_every_route(self):
        assert _MKT_SRC.count("_require_tenant()") >= 3

    def test_auth_checked_in_every_route(self):
        assert _MKT_SRC.count("_check_auth()") >= 3

    def test_no_aggregation_in_progress_route(self):
        """sum() on breakdown values is arithmetic — no SQL GROUP BY here."""
        assert "group_by" not in _MKT_SRC
        assert "func.count" not in _MKT_SRC


# ── Flag guard: all routes ────────────────────────────────────────────────────

class TestFlagGuardAllRoutes:
    def test_list_returns_404_when_flag_off(self):
        _set_request()
        _, status = _with_flag(False, lambda: _mkt.list_campaigns())
        assert status == 404

    def test_get_returns_404_when_flag_off(self):
        _, status = _with_flag(False, lambda: _mkt.get_campaign(1))
        assert status == 404

    def test_progress_returns_404_when_flag_off(self):
        _, status = _with_flag(False, lambda: _mkt.campaign_progress(1))
        assert status == 404


# ── Auth guard: all routes ────────────────────────────────────────────────────

class TestAuthGuardAllRoutes:
    def _run_list(self):
        _set_request()
        return _mkt.list_campaigns()

    def test_list_returns_403_when_auth_fails(self):
        def go():
            return _with_auth(False, self._run_list)
        _, status = _with_flag(True, go)
        assert status == 403

    def test_get_returns_403_when_auth_fails(self):
        def go():
            return _with_auth(False, lambda: _mkt.get_campaign(1))
        _, status = _with_flag(True, go)
        assert status == 403

    def test_progress_returns_403_when_auth_fails(self):
        def go():
            return _with_auth(False, lambda: _mkt.campaign_progress(1))
        _, status = _with_flag(True, go)
        assert status == 403


# ── Tenant guard: all routes ──────────────────────────────────────────────────

class TestTenantGuardAllRoutes:
    def test_list_returns_403_when_tenant_none(self):
        _set_request()
        def go():
            return _with_tenant(None, lambda: _mkt.list_campaigns())
        _, status = _with_flag(True, go)
        assert status == 403

    def test_get_returns_403_when_tenant_none(self):
        def go():
            return _with_tenant(None, lambda: _mkt.get_campaign(1))
        _, status = _with_flag(True, go)
        assert status == 403

    def test_progress_returns_403_when_tenant_none(self):
        def go():
            return _with_tenant(None, lambda: _mkt.campaign_progress(1))
        _, status = _with_flag(True, go)
        assert status == 403

    def test_list_returns_403_when_tenant_empty_string(self):
        _set_request()
        def go():
            return _with_tenant("", lambda: _mkt.list_campaigns())
        _, status = _with_flag(True, go)
        assert status == 403


# ── list_campaigns route ──────────────────────────────────────────────────────

class TestListCampaigns:

    def _run(self, svc_stub, args=None):
        """Call list_campaigns() with flag ON and tenant set."""
        _set_request(args)
        original = _mkt._make_service
        _mkt._make_service = lambda: svc_stub
        try:
            return _with_flag(True, lambda: _with_tenant("T1",
                lambda: _mkt.list_campaigns()))
        finally:
            _mkt._make_service = original

    def test_returns_200_on_success(self):
        svc = _make_service_stub(campaigns=[_make_campaign()])
        body, kwargs = self._run(svc)
        # jsonify stub returns (dict, {}); we check the dict
        assert "campaigns" in body

    def test_campaigns_list_present(self):
        svc = _make_service_stub(campaigns=[_make_campaign(id=1), _make_campaign(id=2)])
        body, _ = self._run(svc)
        assert len(body["campaigns"]) == 2

    def test_total_in_response(self):
        svc = _make_service_stub(campaigns=[], total=42)
        body, _ = self._run(svc)
        assert body["total"] == 42

    def test_page_defaults_to_1(self):
        svc = _make_service_stub()
        body, _ = self._run(svc)
        assert body["page"] == 1

    def test_limit_defaults_to_50(self):
        svc = _make_service_stub()
        body, _ = self._run(svc)
        assert body["limit"] == 50

    def test_pages_ceiling_division(self):
        svc = _make_service_stub(campaigns=[], total=51)
        body, _ = self._run(svc, args={"limit": "50"})
        assert body["pages"] == 2   # ceil(51/50) = 2

    def test_pages_exact_fit(self):
        svc = _make_service_stub(campaigns=[], total=50)
        body, _ = self._run(svc, args={"limit": "50"})
        assert body["pages"] == 1

    def test_pages_minimum_one_on_empty(self):
        svc = _make_service_stub(campaigns=[], total=0)
        body, _ = self._run(svc)
        assert body["pages"] == 1

    def test_status_filter_forwarded(self):
        svc = _make_service_stub()
        self._run(svc, args={"status": "running"})
        svc.list_campaigns.assert_called_once()
        _, kwargs = svc.list_campaigns.call_args
        assert kwargs.get("status") == "running"

    def test_service_called_with_tenant_id(self):
        svc = _make_service_stub()
        self._run(svc)
        svc.list_campaigns.assert_called_once()
        args, _ = svc.list_campaigns.call_args
        assert args[0] == "T1"

    def test_count_for_tenant_called_with_same_status(self):
        svc = _make_service_stub()
        self._run(svc, args={"status": "draft"})
        svc.repository.count_for_tenant.assert_called_once()
        _, kwargs = svc.repository.count_for_tenant.call_args
        assert kwargs.get("status") == "draft"

    def test_campaign_summary_has_required_fields(self):
        c = _make_campaign(id=7, name="Promo", status="running",
                           total_recipients=50, sent_count=20, failed_count=2)
        svc = _make_service_stub(campaigns=[c])
        body, _ = self._run(svc)
        row = body["campaigns"][0]
        for field in ("id", "name", "status", "total_recipients",
                      "sent_count", "failed_count", "created_at", "created_by"):
            assert field in row, f"summary missing field: {field!r}"

    def test_limit_capped_at_100(self):
        svc = _make_service_stub()
        self._run(svc, args={"limit": "9999"})
        _, kwargs = svc.list_campaigns.call_args
        assert kwargs.get("limit") <= 100

    def test_page_negative_clamped_to_1(self):
        svc = _make_service_stub()
        self._run(svc, args={"page": "-5"})
        _, kwargs = svc.list_campaigns.call_args
        assert kwargs.get("offset") >= 0


# ── get_campaign route ────────────────────────────────────────────────────────

class TestGetCampaign:

    def _run(self, svc_stub, campaign_id=1):
        original = _mkt._make_service
        _mkt._make_service = lambda: svc_stub
        try:
            return _with_flag(True, lambda: _with_tenant("T1",
                lambda: _mkt.get_campaign(campaign_id)))
        finally:
            _mkt._make_service = original

    def test_returns_campaign_body_when_found(self):
        c = _make_campaign(id=5, name="Oxford Special")
        svc = _make_service_stub(campaign=c)
        body, _ = self._run(svc, campaign_id=5)
        assert body["id"] == 5
        assert body["name"] == "Oxford Special"

    def test_detail_has_message_body(self):
        c = _make_campaign(message_body="Hello {{name}}")
        svc = _make_service_stub(campaign=c)
        body, _ = self._run(svc)
        assert "message_body" in body

    def test_detail_has_description(self):
        c = _make_campaign(description="Q1 promo")
        svc = _make_service_stub(campaign=c)
        body, _ = self._run(svc)
        assert "description" in body

    def test_detail_has_failure_reason(self):
        c = _make_campaign(failure_reason="zero sends")
        svc = _make_service_stub(campaign=c)
        body, _ = self._run(svc)
        assert "failure_reason" in body

    def test_returns_404_when_campaign_not_found(self):
        svc = _make_service_stub(campaign=None)
        body, status = self._run(svc)
        assert status == 404

    def test_404_body_says_not_found(self):
        svc = _make_service_stub(campaign=None)
        body, status = self._run(svc)
        assert "not found" in str(body).lower()

    def test_service_called_with_correct_tenant_and_id(self):
        svc = _make_service_stub(campaign=_make_campaign(id=99))
        self._run(svc, campaign_id=99)
        svc.get_campaign.assert_called_once_with("T1", 99)

    def test_wrong_tenant_campaign_returns_404(self):
        """Service returns None when campaign belongs to a different tenant."""
        svc = _make_service_stub(campaign=None)  # repository enforces tenant scope
        body, status = self._run(svc)
        assert status == 404


# ── campaign_progress route ───────────────────────────────────────────────────

class TestCampaignProgress:

    def _run(self, svc_stub, campaign_id=1):
        original = _mkt._make_service
        _mkt._make_service = lambda: svc_stub
        try:
            return _with_flag(True, lambda: _with_tenant("T1",
                lambda: _mkt.campaign_progress(campaign_id)))
        finally:
            _mkt._make_service = original

    def test_returns_breakdown_dict(self):
        c = _make_campaign()
        svc = _make_service_stub(
            campaign=c,
            breakdown={"sent": 8, "failed": 2, "queued": 5},
        )
        body, _ = self._run(svc)
        assert body["breakdown"] == {"sent": 8, "failed": 2, "queued": 5}

    def test_derived_total_is_sum_of_breakdown(self):
        c = _make_campaign()
        svc = _make_service_stub(
            campaign=c,
            breakdown={"sent": 8, "failed": 2, "queued": 5},
        )
        body, _ = self._run(svc)
        assert body["total"] == 15

    def test_total_is_zero_for_empty_breakdown(self):
        c = _make_campaign()
        svc = _make_service_stub(campaign=c, breakdown={})
        body, _ = self._run(svc)
        assert body["total"] == 0

    def test_campaign_id_in_response(self):
        c = _make_campaign(id=42)
        svc = _make_service_stub(campaign=c, breakdown={"sent": 1})
        body, _ = self._run(svc, campaign_id=42)
        assert body["campaign_id"] == 42

    def test_returns_404_when_campaign_not_found(self):
        svc = _make_service_stub(campaign=None)
        body, status = self._run(svc)
        assert status == 404

    def test_progress_not_called_when_campaign_not_found(self):
        """Route must not call svc.progress() if the campaign doesn't exist."""
        svc = _make_service_stub(campaign=None)
        self._run(svc)
        svc.progress.assert_not_called()

    def test_service_called_with_correct_tenant_and_id(self):
        c = _make_campaign(id=77)
        svc = _make_service_stub(campaign=c, breakdown={"sent": 1})
        self._run(svc, campaign_id=77)
        svc.get_campaign.assert_called_once_with("T1", 77)
        svc.progress.assert_called_once_with("T1", 77)


# ── Serialisation helpers ─────────────────────────────────────────────────────

class TestSerialisationHelpers:
    def test_summary_excludes_message_body(self):
        c = _make_campaign(message_body="Secret")
        result = _mkt._campaign_summary(c)
        assert "message_body" not in result

    def test_summary_excludes_description(self):
        c = _make_campaign(description="Long desc")
        result = _mkt._campaign_summary(c)
        assert "description" not in result

    def test_detail_includes_message_body(self):
        c = _make_campaign(message_body="Hello")
        result = _mkt._campaign_detail(c)
        assert result["message_body"] == "Hello"

    def test_detail_is_superset_of_summary(self):
        c = _make_campaign()
        summary = _mkt._campaign_summary(c)
        detail  = _mkt._campaign_detail(c)
        for key in summary:
            assert key in detail

    def test_none_datetimes_serialise_as_none(self):
        c = _make_campaign(scheduled_at=None, started_at=None, completed_at=None)
        result = _mkt._campaign_summary(c)
        assert result["scheduled_at"] is None
        assert result["started_at"]   is None
        assert result["completed_at"] is None


# ── Legacy route protection ───────────────────────────────────────────────────

class TestLegacyRoutesUnchanged:
    def test_admin_campaign_send_still_uses_legacy_service(self):
        src = open(
            os.path.join(_ROOT, "app", "routes", "admin.py"), encoding="utf-8"
        ).read()
        assert "from app.services.campaign_service import start_campaign" in src

    def test_broadcast_not_modified(self):
        src = open(
            os.path.join(_ROOT, "app", "routes", "broadcast.py"), encoding="utf-8"
        ).read()
        assert "marketing_bp" not in src
        assert "campaign_engine_v2" not in src.lower()

    def test_legacy_campaign_service_not_imported_in_marketing_routes(self):
        assert "app.services.campaign_service" not in _MKT_SRC
