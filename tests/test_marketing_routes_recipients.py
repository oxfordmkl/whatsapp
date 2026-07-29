"""
Phase 9.4 — Recipient Inspector route tests.

Route under test:
    GET /crm/campaigns/v2/<campaign_id>/recipients

Proves:
  - flag OFF -> 404
  - unauthenticated -> 403
  - STAFF role -> 403 (RBAC: this GET is @campaign_admin_required, unlike the
    other GET routes on this blueprint which are authn-only — Phase 9.4
    discovery flagged that an authn-only recipient list would let STAFF
    enumerate phone numbers for leads they are not assigned to)
  - ADMIN / SUPER_ADMIN -> allowed
  - None/empty tenant -> 403
  - campaign not found (or wrong tenant) -> 404, before any recipient query
  - default pagination: page=1, limit=50
  - limit is capped at 100
  - status query param is forwarded to the service unchanged
  - response shape: campaign_id, recipients[], total, page, limit, pages
  - recipient serialisation: tenant_id and campaign_id withheld; all other
    CampaignRecipient fields present; datetimes isoformatted; None-safe
  - CampaignService.list_recipients/count_recipients called with matching
    tenant_id, campaign_id, status, limit, offset

Follows the _load() isolation pattern established in
test_marketing_routes_launch.py / test_marketing_routes_preview.py — no
Flask test client, no SQLAlchemy, no real DB. CampaignService is a MagicMock
injected via _make_service; this file proves only the ROUTE's wiring and
HTTP-shape decisions.
"""
import importlib.util
import os
import sys
import types
from datetime import datetime
from unittest.mock import MagicMock

import pytest

_ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MKT_PATH = os.path.join(_ROOT, "app", "routes", "marketing.py")
_MKT_SRC  = open(_MKT_PATH, encoding="utf-8").read()


# ── Stub infrastructure (matches test_marketing_routes_launch.py) ───────────

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

    sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: True
    sys.modules["app.routes.admin"]._actor_tenant_id    = lambda: "T1"
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
    "_p94_svc", os.path.join(_ROOT, "app", "marketing", "campaign_service.py")
)
sys.modules["app.marketing.campaign_service"] = _svc_mod

_mkt = _load_module("_p94_mkt", _MKT_PATH)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_campaign(id=1):
    c = MagicMock()
    c.id = id
    return c


def _make_recipient(id=1, phone="919999999999", name="Jane", status="sent",
                     retry_count=0, failure_reason=None, wa_message_id="wamid.1",
                     send_at=None, last_attempt_at=None, sent_at=None,
                     delivered_at=None, read_at=None, created_at=None):
    r = MagicMock()
    r.id = id
    r.phone = phone
    r.name = name
    r.status = status
    r.retry_count = retry_count
    r.failure_reason = failure_reason
    r.wa_message_id = wa_message_id
    r.send_at = send_at
    r.last_attempt_at = last_attempt_at
    r.sent_at = sent_at
    r.delivered_at = delivered_at
    r.read_at = read_at
    r.created_at = created_at
    return r


def _make_service(campaign=None, recipients=None, total=0):
    svc = MagicMock()
    svc.get_campaign.return_value = campaign
    svc.list_recipients.return_value = recipients or []
    svc.count_recipients.return_value = total
    return svc


def _with_flag(value, fn):
    sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: value
    try:
        return fn()
    finally:
        sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: True


def _with_tenant(tid, fn):
    sys.modules["app.routes.admin"]._actor_tenant_id = lambda: tid
    try:
        return fn()
    finally:
        sys.modules["app.routes.admin"]._actor_tenant_id = lambda: "T1"


def _with_auth(value, fn):
    sys.modules["app.routes.admin"].check_auth = lambda: value
    try:
        return fn()
    finally:
        sys.modules["app.routes.admin"].check_auth = lambda: True


def _with_role(role, fn):
    orig = sys.modules["app.routes.admin"].get_current_actor
    sys.modules["app.routes.admin"].get_current_actor = lambda: {
        "authenticated": True, "username": "u", "role": role, "source": "SESSION",
    }
    try:
        return fn()
    finally:
        sys.modules["app.routes.admin"].get_current_actor = orig


def _with_service(svc, fn):
    orig = _mkt._make_service
    _mkt._make_service = lambda: svc
    try:
        return fn()
    finally:
        _mkt._make_service = orig


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


def _unpack(raw):
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], int):
        inner, status = raw
        if isinstance(inner, tuple) and len(inner) == 2:
            body, _kw = inner
            return body, status
        return inner, status
    if isinstance(raw, tuple) and len(raw) == 2:
        body, _kw = raw
        return body, 200
    return raw, 200


def _run(campaign_id=1, tenant="T1", auth=True, flag=True, role="ADMIN",
          args=None, svc=None, campaign=None):
    _set_request(args or {})
    svc = svc if svc is not None else _make_service(
        campaign if campaign is not None else _make_campaign()
    )

    def go():
        return _with_service(svc, lambda: _mkt.list_campaign_recipients(campaign_id))

    return _with_flag(flag, lambda: _with_role(role, lambda:
           _with_auth(auth, lambda: _with_tenant(tenant, go))))


# ── Source-level assertions ───────────────────────────────────────────────────

class TestSourceStructure:
    def test_route_defined(self):
        assert "def list_campaign_recipients" in _MKT_SRC

    def test_route_path(self):
        assert '"/<int:campaign_id>/recipients"' in _MKT_SRC

    def test_decorator_order(self):
        idx = _MKT_SRC.index("def list_campaign_recipients")
        preceding = _MKT_SRC[:idx]
        # last two decorators before the def must be require_campaign_engine
        # then campaign_admin_required, matching cancel/archive precedent.
        assert preceding.rstrip().endswith(
            '@require_campaign_engine\n@campaign_admin_required'
        )

    def test_uses_service_get_campaign_first(self):
        idx = _MKT_SRC.index("def list_campaign_recipients")
        body = _MKT_SRC[idx:idx + 1600]
        assert "svc.get_campaign" in body
        assert body.index("svc.get_campaign") < body.index("svc.list_recipients")

    def test_no_direct_db_access(self):
        idx = _MKT_SRC.index("def list_campaign_recipients")
        body = _MKT_SRC[idx:idx + 1600]
        assert "db.session" not in body

    def test_no_mutation_calls(self):
        idx = _MKT_SRC.index("def list_campaign_recipients")
        body = _MKT_SRC[idx:idx + 1600]
        for forbidden in ("svc.create_campaign", "svc.mark_running",
                          "svc.cancel", "svc.archive", ".commit()"):
            assert forbidden not in body

    def test_serializer_withholds_tenant_and_campaign_id(self):
        idx = _MKT_SRC.index("def _recipient_summary")
        body = _MKT_SRC[idx:idx + 900]
        assert '"tenant_id"' not in body
        assert '"campaign_id"' not in body


# ── Behavioural tests ─────────────────────────────────────────────────────────

class TestFlagAndAuth:
    def test_flag_off_returns_404(self):
        raw = _run(flag=False)
        body, status = _unpack(raw)
        assert status == 404

    def test_unauthenticated_returns_403(self):
        raw = _run(auth=False)
        body, status = _unpack(raw)
        assert status == 403

    def test_staff_role_returns_403(self):
        raw = _run(role="STAFF")
        body, status = _unpack(raw)
        assert status == 403

    def test_admin_role_allowed(self):
        raw = _run(role="ADMIN")
        _, status = _unpack(raw)
        assert status == 200

    def test_super_admin_role_allowed(self):
        raw = _run(role="SUPER_ADMIN")
        _, status = _unpack(raw)
        assert status == 200

    def test_missing_tenant_returns_403(self):
        raw = _run(tenant=None)
        _, status = _unpack(raw)
        assert status == 403

    def test_empty_tenant_returns_403(self):
        raw = _run(tenant="")
        _, status = _unpack(raw)
        assert status == 403


class TestCampaignLookup:
    def test_campaign_not_found_returns_404(self):
        svc = _make_service(campaign=None)
        raw = _run(svc=svc)
        _, status = _unpack(raw)
        assert status == 404

    def test_recipients_not_queried_when_campaign_missing(self):
        svc = _make_service(campaign=None)
        _run(svc=svc)
        svc.list_recipients.assert_not_called()
        svc.count_recipients.assert_not_called()


class TestPagination:
    def test_default_pagination(self):
        svc = _make_service(campaign=_make_campaign(), total=0)
        _run(svc=svc)
        _, kwargs = svc.list_recipients.call_args
        assert kwargs.get("limit") == 50
        assert kwargs.get("offset") == 0

    def test_limit_capped_at_100(self):
        svc = _make_service(campaign=_make_campaign(), total=0)
        _run(svc=svc, args={"limit": "500"})
        _, kwargs = svc.list_recipients.call_args
        assert kwargs.get("limit") == 100

    def test_page_2_offset(self):
        svc = _make_service(campaign=_make_campaign(), total=0)
        _run(svc=svc, args={"page": "2", "limit": "20"})
        _, kwargs = svc.list_recipients.call_args
        assert kwargs.get("offset") == 20

    def test_status_filter_forwarded(self):
        svc = _make_service(campaign=_make_campaign(), total=0)
        _run(svc=svc, args={"status": "failed"})
        _, kwargs = svc.list_recipients.call_args
        assert kwargs.get("status") == "failed"

    def test_pages_ceiling_division(self):
        svc = _make_service(campaign=_make_campaign(), total=101)
        raw = _run(svc=svc, args={"limit": "50"})
        body, _ = _unpack(raw)
        assert body["pages"] == 3

    def test_tenant_and_campaign_id_scoped(self):
        svc = _make_service(campaign=_make_campaign(id=7), total=0)
        _run(campaign_id=7, tenant="T9", svc=svc)
        args, kwargs = svc.list_recipients.call_args
        assert "T9" in args
        assert 7 in args


class TestResponseShape:
    def test_response_has_expected_keys(self):
        svc = _make_service(campaign=_make_campaign(id=3), total=0)
        raw = _run(campaign_id=3, svc=svc)
        body, status = _unpack(raw)
        assert status == 200
        for key in ("campaign_id", "recipients", "total", "page", "limit", "pages"):
            assert key in body
        assert body["campaign_id"] == 3

    def test_recipient_serialised_fields(self):
        rec = _make_recipient(
            id=5, phone="919888877766", name="Anjana", status="failed",
            retry_count=2, failure_reason="window closed",
            wa_message_id="wamid.5",
            send_at=datetime(2026, 1, 1, 9, 0),
            last_attempt_at=datetime(2026, 1, 1, 9, 5),
            sent_at=None, delivered_at=None, read_at=None,
            created_at=datetime(2026, 1, 1, 8, 0),
        )
        svc = _make_service(campaign=_make_campaign(), recipients=[rec], total=1)
        raw = _run(svc=svc)
        body, _ = _unpack(raw)
        item = body["recipients"][0]

        assert item["id"] == 5
        assert item["phone"] == "919888877766"
        assert item["name"] == "Anjana"
        assert item["status"] == "failed"
        assert item["retry_count"] == 2
        assert item["failure_reason"] == "window closed"
        assert item["wa_message_id"] == "wamid.5"
        assert item["send_at"] == "2026-01-01T09:00:00"
        assert item["last_attempt_at"] == "2026-01-01T09:05:00"
        assert item["sent_at"] is None
        assert item["delivered_at"] is None
        assert item["read_at"] is None
        assert item["created_at"] == "2026-01-01T08:00:00"

        # tenant_id / campaign_id must never leak into the response
        assert "tenant_id" not in item
        assert "campaign_id" not in item

    def test_recipient_with_no_name_or_failure(self):
        rec = _make_recipient(name=None, failure_reason=None)
        svc = _make_service(campaign=_make_campaign(), recipients=[rec], total=1)
        raw = _run(svc=svc)
        body, _ = _unpack(raw)
        item = body["recipients"][0]
        assert item["name"] is None
        assert item["failure_reason"] is None

    def test_empty_recipients_list(self):
        svc = _make_service(campaign=_make_campaign(), recipients=[], total=0)
        raw = _run(svc=svc)
        body, status = _unpack(raw)
        assert status == 200
        assert body["recipients"] == []
        assert body["total"] == 0
