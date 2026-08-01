"""
Phase 8.2E.9-B — Audience preview route tests (ADR-025 D6/D7).

Routes under test:
    GET /crm/campaigns/v2/segments
    GET /crm/campaigns/v2/<campaign_id>/preview

Proves:
  - flag OFF -> both routes return 404
  - auth OFF -> 403
  - segments: returns the resolver's segment list, no tenant/DB touch beyond auth
  - preview: 403 with no tenant, 404 for unknown/wrong-tenant campaign
  - preview: 400 when segment query param missing
  - preview: 400 when AudienceResolutionError is raised (unknown segment)
  - preview: breakdown fields passed through verbatim from the resolver
  - preview: template diagnostics passed through verbatim from
    describe_campaign_template()
  - preview: expected_failed == 0 when template ready, == template_required
    when not ready
  - preview: requires_acknowledgement == (template_required > 0), independent
    of template readiness (D6.2's literal wording)
  - preview never materialises anything — no repository writes referenced
  - read-only: no CampaignService mutation methods called

Follows the _load() isolation pattern established in
test_marketing_routes_read.py — no Flask test client, no SQLAlchemy. The
resolver and describe_campaign_template are stubbed via injected fakes so
this file never imports the real audience_resolver.py or campaign_service.py
logic; those are covered by test_audience_resolver.py and
test_campaign_service.py respectively. This file proves only the ROUTE'S
wiring and HTTP-shape decisions.
"""
import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MKT_PATH = os.path.join(_ROOT, "app", "routes", "marketing.py")
_MKT_SRC = open(_MKT_PATH, encoding="utf-8").read()


# ── Stub infrastructure (matches test_marketing_routes_read.py) ─────────────

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
        "app.marketing.audience_resolver",
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


def _load_module(alias, path):
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


_ensure_stubs()

# Real campaign_service so CampaignService/error classes are genuine objects;
# describe_campaign_template is stubbed per-test on this module.
_svc_mod = _load_module(
    "_p82e9b_svc", os.path.join(_ROOT, "app", "marketing", "campaign_service.py")
)
sys.modules["app.marketing.campaign_service"] = _svc_mod

# audience_resolver stub — the route imports list_segments, preview and
# AudienceResolutionError from it. A real AudienceResolutionError class is
# needed so `except AudienceResolutionError` in the route matches what tests
# raise; everything else is a controllable MagicMock.
_resolver_stub = sys.modules["app.marketing.audience_resolver"]


class _AudienceResolutionError(ValueError):
    pass


_resolver_stub.AudienceResolutionError = _AudienceResolutionError
_resolver_stub.list_segments = MagicMock(return_value=(
    "HOT Leads", "WARM Leads", "Demo Requested", "Fees Requested",
    "Placement Interested", "Needs Reply", "Critical Leads", "All Leads",
))
_resolver_stub.preview = MagicMock(return_value={
    "segment": "All Leads", "total_audience": 0,
    "opted_out_excluded": 0, "reachable_now": 0, "template_required": 0,
})

_mkt = _load_module("_p82e9b_mkt", _MKT_PATH)


# ── Fixtures / helpers ────────────────────────────────────────────────────────

def _make_campaign(id=1, template_id=None):
    c = MagicMock()
    c.id = id
    c.template_id = template_id
    return c


def _make_service_stub(campaign=None):
    svc = MagicMock()
    svc.get_campaign.return_value = campaign
    return svc


def _with_flag(value, fn):
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


def _with_service(svc, fn):
    orig = _mkt._make_service
    _mkt._make_service = lambda: svc
    try:
        return fn()
    finally:
        _mkt._make_service = orig


def _with_resolver_preview(result_or_side_effect, fn):
    """Same live-sys.modules-lookup rationale as _with_template_description:
    the route's audience_resolver import is also lazy/inside-function."""
    live = sys.modules["app.marketing.audience_resolver"]
    orig = live.preview
    if isinstance(result_or_side_effect, dict):
        live.preview = MagicMock(return_value=result_or_side_effect)
    else:
        live.preview = MagicMock(side_effect=result_or_side_effect)
    try:
        return fn()
    finally:
        live.preview = orig


def _with_template_description(result, fn):
    """Patch describe_campaign_template on whichever module object is
    CURRENTLY live in sys.modules — the route's `from
    app.marketing.campaign_service import describe_campaign_template` is a
    lazy, inside-function import, so it resolves via sys.modules at CALL
    time, not via the _svc_mod reference this file captured at load time.
    Another test file loaded later in the same session can replace that
    sys.modules entry with a different module object; patching only
    _svc_mod would then silently miss the real call. This mirrors the same
    live-lookup fix already applied to resolve_campaign_template patching in
    test_campaign_worker.py."""
    live = sys.modules["app.marketing.campaign_service"]
    orig = live.describe_campaign_template
    live.describe_campaign_template = MagicMock(return_value=result)
    try:
        return fn()
    finally:
        live.describe_campaign_template = orig


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
    req = MagicMock()
    req.args = _FakeArgs(args or {})
    flask_mod.request = req


_READY_TEMPLATE = {
    "configured": True, "found": True, "tenant_match": True,
    "status": "approved", "provider_template_id_present": True,
    "channel": "whatsapp", "ready": True,
}
_UNCONFIGURED_TEMPLATE = {
    "configured": False, "found": False, "tenant_match": None,
    "status": None, "provider_template_id_present": None,
    "channel": None, "ready": False,
}


def _run_preview(campaign_id=1, tenant="T1", auth=True, flag=True,
                  args=None, svc=None, campaign=None):
    _set_request(args if args is not None else {"segment": "All Leads"})
    svc = svc if svc is not None else _make_service_stub(
        campaign if campaign is not None else _make_campaign()
    )

    def go():
        return _with_service(svc, lambda: _mkt.campaign_audience_preview(campaign_id))

    return _with_flag(flag, lambda: _with_auth(auth, lambda: _with_tenant(tenant, go)))


# ── Source-level assertions ───────────────────────────────────────────────────

class TestSourceStructure:
    def test_segments_route_defined(self):
        assert "def list_audience_segments" in _MKT_SRC

    def test_preview_route_defined(self):
        assert "def campaign_audience_preview" in _MKT_SRC

    def test_preview_uses_service_get_campaign(self):
        assert "svc.get_campaign" in _MKT_SRC

    def test_preview_uses_resolver_preview(self):
        assert "resolve_preview(" in _MKT_SRC

    def test_preview_uses_describe_campaign_template(self):
        assert "describe_campaign_template(" in _MKT_SRC

    def test_no_direct_db_in_new_routes(self):
        assert "db.session" not in _MKT_SRC

    def test_new_routes_do_not_add_mutation_calls(self):
        """The new preview/segments routes themselves must not mutate.
        svc.create_campaign / mark_running legitimately exist elsewhere in
        this file for the create/launch routes — this checks the NEW route
        bodies specifically, not the whole file."""
        start = _MKT_SRC.index("def list_audience_segments")
        new_routes_src = _MKT_SRC[start:]
        for forbidden in ("svc.create_campaign", "svc.mark_running",
                          "add_recipients", ".commit()"):
            assert forbidden not in new_routes_src


# ── Flag guard ────────────────────────────────────────────────────────────────

class TestFlagGuard:
    def test_segments_404_when_flag_off(self):
        _, status = _with_flag(False, lambda: _mkt.list_audience_segments())
        assert status == 404

    def test_preview_404_when_flag_off(self):
        _, status = _run_preview(flag=False)
        assert status == 404


# ── Auth guard ────────────────────────────────────────────────────────────────

class TestAuthGuard:
    def test_segments_403_when_unauthenticated(self):
        body, status = _with_flag(True, lambda: _with_auth(
            False, lambda: _mkt.list_audience_segments()
        ))
        assert status == 403

    def test_preview_403_when_unauthenticated(self):
        _, status = _run_preview(auth=False)
        assert status == 403


# ── segments route ────────────────────────────────────────────────────────────

class TestListAudienceSegments:
    def test_returns_resolver_segment_list(self):
        body, _ = _with_flag(True, lambda: _with_auth(
            True, lambda: _mkt.list_audience_segments()
        ))
        assert body["segments"] == list(_resolver_stub.list_segments.return_value)


# ── preview route: tenant / campaign guards ───────────────────────────────────

class TestPreviewGuards:
    def test_403_when_no_tenant(self):
        body, status = _with_flag(True, lambda: _with_auth(True, lambda:
            _with_tenant(None, lambda: _mkt.campaign_audience_preview(1))
        ))
        assert status == 403

    def test_404_when_campaign_not_found(self):
        svc = _make_service_stub(campaign=None)
        body, status = _run_preview(svc=svc)
        assert status == 404
        assert "campaign not found" in str(body).lower()

    def test_get_campaign_called_with_tenant_and_id(self):
        svc = _make_service_stub(campaign=_make_campaign(id=42))
        _run_preview(campaign_id=42, tenant="T9", svc=svc)
        svc.get_campaign.assert_called_once_with("T9", 42)

    def test_400_when_segment_missing(self):
        body, status = _run_preview(args={})
        assert status == 400
        assert "segment" in str(body).lower()

    def test_400_when_segment_blank(self):
        body, status = _run_preview(args={"segment": ""})
        assert status == 400

    def test_400_on_audience_resolution_error(self):
        def raise_it(*a, **kw):
            raise _AudienceResolutionError("unknown audience segment 'Nope'")

        body, status = _with_resolver_preview(raise_it, lambda: _run_preview(
            args={"segment": "Nope"}
        ))
        assert status == 400
        assert "detail" in str(body)


# ── preview route: breakdown passthrough ──────────────────────────────────────

class TestPreviewBreakdown:
    def test_breakdown_fields_passed_through(self):
        breakdown = {
            "segment": "HOT Leads", "total_audience": 48,
            "opted_out_excluded": 1, "reachable_now": 8, "template_required": 40,
        }
        body, _ = _with_resolver_preview(breakdown, lambda:
            _with_template_description(_UNCONFIGURED_TEMPLATE, lambda:
                _run_preview(args={"segment": "HOT Leads"})
            )
        )
        for k, v in breakdown.items():
            assert body[k] == v

    def test_campaign_id_included(self):
        body, _ = _with_template_description(_UNCONFIGURED_TEMPLATE, lambda:
            _run_preview(campaign_id=7)
        )
        assert body["campaign_id"] == 7

    def test_resolver_called_with_tenant_and_segment(self):
        seen = {}

        def spy(tenant_id, segment):
            seen["args"] = (tenant_id, segment)
            return {"segment": segment, "total_audience": 0,
                   "opted_out_excluded": 0, "reachable_now": 0,
                   "template_required": 0}

        _with_resolver_preview(spy, lambda:
            _with_template_description(_UNCONFIGURED_TEMPLATE, lambda:
                _run_preview(tenant="T5", args={"segment": "WARM Leads"})
            )
        )
        assert seen["args"] == ("T5", "WARM Leads")


# ── preview route: template + derived fields (D6.1 / D6.2 / D7) ──────────────

class TestPreviewTemplateAndDerivedFields:
    def test_template_diagnostics_passed_through(self):
        body, status = _with_resolver_preview(
            {"segment": "All Leads", "total_audience": 1, "opted_out_excluded": 0,
             "reachable_now": 0, "template_required": 1},
            lambda: _with_template_description(_READY_TEMPLATE, lambda: _run_preview())
        )
        assert body["template"] == _READY_TEMPLATE

    def test_expected_failed_zero_when_template_ready(self):
        body, _ = _with_resolver_preview(
            {"segment": "All Leads", "total_audience": 10, "opted_out_excluded": 0,
             "reachable_now": 2, "template_required": 8},
            lambda: _with_template_description(_READY_TEMPLATE, lambda: _run_preview())
        )
        assert body["expected_failed"] == 0

    def test_expected_failed_equals_template_required_when_not_ready(self):
        body, _ = _with_resolver_preview(
            {"segment": "All Leads", "total_audience": 10, "opted_out_excluded": 0,
             "reachable_now": 2, "template_required": 8},
            lambda: _with_template_description(_UNCONFIGURED_TEMPLATE, lambda: _run_preview())
        )
        assert body["expected_failed"] == 8

    def test_expected_failed_zero_when_no_recipients_need_a_template(self):
        body, _ = _with_resolver_preview(
            {"segment": "All Leads", "total_audience": 5, "opted_out_excluded": 0,
             "reachable_now": 5, "template_required": 0},
            lambda: _with_template_description(_UNCONFIGURED_TEMPLATE, lambda: _run_preview())
        )
        assert body["expected_failed"] == 0

    def test_requires_acknowledgement_true_when_template_required_positive(self):
        """D6.2's literal wording: required whenever recipients need a
        template — independent of whether that template happens to be ready."""
        body, _ = _with_resolver_preview(
            {"segment": "All Leads", "total_audience": 10, "opted_out_excluded": 0,
             "reachable_now": 2, "template_required": 8},
            lambda: _with_template_description(_READY_TEMPLATE, lambda: _run_preview())
        )
        assert body["requires_acknowledgement"] is True

    def test_requires_acknowledgement_false_when_fully_reachable(self):
        body, _ = _with_resolver_preview(
            {"segment": "All Leads", "total_audience": 5, "opted_out_excluded": 0,
             "reachable_now": 5, "template_required": 0},
            lambda: _with_template_description(_UNCONFIGURED_TEMPLATE, lambda: _run_preview())
        )
        assert body["requires_acknowledgement"] is False

    def test_describe_template_called_with_campaigns_template_id(self):
        seen = {}

        def spy(tenant_id, template_id):
            seen["args"] = (tenant_id, template_id)
            return _READY_TEMPLATE

        campaign = _make_campaign(id=3, template_id=55)
        svc = _make_service_stub(campaign=campaign)
        live = sys.modules["app.marketing.campaign_service"]
        orig = live.describe_campaign_template
        live.describe_campaign_template = spy
        try:
            _run_preview(campaign_id=3, tenant="T2", svc=svc)
        finally:
            live.describe_campaign_template = orig
        assert seen["args"] == ("T2", 55)


# ── Read-only contract ────────────────────────────────────────────────────────

class TestReadOnlyContract:
    def test_preview_never_calls_mutation_methods(self):
        svc = _make_service_stub(campaign=_make_campaign())
        _with_template_description(_UNCONFIGURED_TEMPLATE, lambda: _run_preview(svc=svc))
        svc.create_campaign.assert_not_called()
        assert not hasattr(svc, "mark_running") or not svc.mark_running.called

    def test_segments_route_does_not_touch_service(self):
        """list_audience_segments needs no tenant/campaign context at all."""
        svc = _make_service_stub()
        with_svc = lambda: _mkt.list_audience_segments()
        _with_flag(True, lambda: _with_auth(True, with_svc))
        svc.get_campaign.assert_not_called()
