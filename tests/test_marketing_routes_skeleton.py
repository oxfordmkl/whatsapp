"""
Phase 8.2D.1 — Marketing blueprint skeleton tests.

Proves:
  - marketing_bp imports without circular dependencies
  - blueprint is registered in app/__init__.py
  - flag OFF → all decorated routes return 404
  - flag ON → decorated routes pass through to the handler
  - _require_tenant() refuses None tenant and passes real tenant_id
  - _map_campaign_error() maps each exception type to the correct HTTP status
  - no business logic in this module (no CampaignService calls at module level)
  - no legacy admin/broadcast routes changed

Strategy: source-level assertions + _load() isolation to avoid bootstrapping
Flask+SQLAlchemy (same pattern as campaign worker / reconciliation tests).
Runtime flag and tenant behaviour are tested via _simulate_* helpers that
call the decorated functions directly.
"""
import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MKT_PATH = os.path.join(_ROOT, "app", "routes", "marketing.py")
_INIT_PATH = os.path.join(_ROOT, "app", "__init__.py")
_MKT_SRC = open(_MKT_PATH, encoding="utf-8").read()
_INIT_SRC = open(_INIT_PATH, encoding="utf-8").read()


# ── Module loader (same pattern as campaign test suite) ───────────────────────

def _stub_modules():
    """Pre-populate sys.modules with the minimum stubs needed to load marketing.py."""
    for name in [
        "app", "app.flags", "app.routes", "app.routes.admin",
        "app.marketing", "app.marketing.campaign_service",
        "flask", "flask_login",
    ]:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)

    # flask stub: Blueprint, jsonify
    flask_mod = sys.modules["flask"]
    if not hasattr(flask_mod, "Blueprint"):
        def _Blueprint(name, *a, **kw):
            bp = MagicMock(name=f"bp_{name}")
            bp.name = name
            return bp
        flask_mod.Blueprint = _Blueprint
        flask_mod.jsonify = lambda d, **kw: (d, kw)  # returns (dict, kwargs)
        flask_mod.request = MagicMock()
        flask_mod.current_app = MagicMock()

    # app.flags stub
    sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: False

    # app.routes.admin stub
    admin_mod = sys.modules["app.routes.admin"]
    admin_mod._actor_tenant_id = lambda: None
    admin_mod.check_auth = lambda: True
    admin_mod.admin_required = lambda f: f   # identity decorator for testing

    # campaign_service stub — real exception classes needed for _map_campaign_error
    _svc_mod = _load_module("_p82d1_svc", os.path.join(_ROOT, "app", "marketing", "campaign_service.py"))
    sys.modules["app.marketing.campaign_service"] = _svc_mod


def _load_module(module_name, file_path):
    """Load a .py source file as a module with an isolated namespace."""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


_stub_modules()
_mkt = _load_module("_p82d1_mkt", _MKT_PATH)

# Pull real exception classes from the loaded service
_svc = sys.modules["_p82d1_svc"]
CampaignEngineDisabled = _svc.CampaignEngineDisabled
CampaignValidationError = _svc.CampaignValidationError
CampaignTransitionError = _svc.CampaignTransitionError
ValidationResult = _svc.ValidationResult


# ── Structural tests (source-level) ──────────────────────────────────────────

class TestSourceStructure:
    """Assert the wiring in marketing.py is structurally correct."""

    def test_blueprint_defined(self):
        assert "marketing_bp = Blueprint" in _MKT_SRC

    def test_url_prefix_set(self):
        assert 'url_prefix="/crm/campaigns/v2"' in _MKT_SRC

    def test_require_campaign_engine_decorator_defined(self):
        assert "def require_campaign_engine" in _MKT_SRC

    def test_require_tenant_defined(self):
        assert "def _require_tenant" in _MKT_SRC

    def test_map_campaign_error_defined(self):
        assert "def _map_campaign_error" in _MKT_SRC

    def test_no_top_level_app_imports(self):
        """All app.* imports must be inside functions (lazy) — no circular import risk."""
        top_level = [
            line for line in _MKT_SRC.splitlines()
            if (line.startswith("from app.") or line.startswith("import app."))
        ]
        assert not top_level, (
            f"marketing.py must not import from app at module level: {top_level}"
        )

    def test_no_campaign_service_at_module_level(self):
        """CampaignService must not be imported at module scope."""
        top_imports = [
            line for line in _MKT_SRC.splitlines()
            if line.startswith("from ") or line.startswith("import ")
        ]
        for line in top_imports:
            assert "campaign_service" not in line.lower(), (
                f"campaign_service must not be a module-level import: {line!r}"
            )

    def test_no_business_logic_at_module_level(self):
        """CampaignService() must not appear at module scope (only inside functions)."""
        module_scope_lines = []
        indent = 0
        for line in _MKT_SRC.splitlines():
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            # Any non-indented non-def/class line is module-scope code
            if not line[0].isspace() and not line.startswith("def ") \
                    and not line.startswith("class ") \
                    and not line.startswith("@") \
                    and not line.startswith("import ") \
                    and not line.startswith("from ") \
                    and not line.startswith('"""'):
                module_scope_lines.append(line)
        for line in module_scope_lines:
            assert "CampaignService()" not in line, (
                f"CampaignService() must not appear at module scope: {line!r}"
            )


class TestRegistration:
    """Assert marketing_bp is wired into app/__init__.py correctly."""

    def test_marketing_bp_imported_in_init(self):
        assert "from app.routes.marketing import marketing_bp" in _INIT_SRC

    def test_marketing_bp_registered_in_init(self):
        assert "app.register_blueprint(marketing_bp)" in _INIT_SRC

    def test_registration_is_unconditional(self):
        """marketing_bp registration must NOT be inside the campaign flag block."""
        lines = _INIT_SRC.splitlines()
        flag_line = next(
            (i for i, l in enumerate(lines)
             if "campaign_engine_v2_enabled()" in l and l.lstrip().startswith("if ")),
            None,
        )
        assert flag_line is not None

        for i, line in enumerate(lines):
            if "register_blueprint(marketing_bp)" in line:
                # Must come before or at the same indent level as the flag check,
                # but not inside it (i.e. not indented further than the if block).
                assert not line.startswith("        "), (
                    "marketing_bp registration must not be inside the flag if block"
                )

    def test_registration_after_other_blueprints(self):
        """marketing_bp is registered after admin_bp (preserves existing boot order)."""
        admin_pos = _INIT_SRC.index("register_blueprint(admin_bp)")
        mkt_pos = _INIT_SRC.index("register_blueprint(marketing_bp)")
        assert admin_pos < mkt_pos


# ── Feature-flag guard tests ──────────────────────────────────────────────────

class TestRequireCampaignEngine:
    """require_campaign_engine returns 404 when OFF, passes through when ON."""

    def _make_view(self):
        """Return a dummy view wrapped by require_campaign_engine."""
        @_mkt.require_campaign_engine
        def dummy_view():
            return {"status": "ok"}, 200
        return dummy_view

    def test_returns_404_when_flag_off(self):
        sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: False
        view = self._make_view()
        _, status = view()
        assert status == 404

    def test_returns_404_body_says_not_found(self):
        sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: False
        view = self._make_view()
        body, _ = view()
        assert "not found" in str(body).lower()

    def test_passes_through_when_flag_on(self):
        sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: True
        view = self._make_view()
        result, status = view()
        assert status == 200
        assert result == {"status": "ok"}

    def test_does_not_expose_v2_detail_when_off(self):
        """404 body must not mention CAMPAIGN_ENGINE_V2 or v2."""
        sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: False
        view = self._make_view()
        body, _ = view()
        body_str = str(body).lower()
        assert "campaign_engine_v2" not in body_str
        assert "engine" not in body_str

    def teardown_method(self, _):
        # Reset flag to OFF (safe default)
        sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: False


# ── Tenant safety tests ───────────────────────────────────────────────────────

class TestRequireTenant:
    """_require_tenant() must refuse None and pass real tenant_id."""

    def test_refuses_none_tenant(self):
        sys.modules["app.routes.admin"]._actor_tenant_id = lambda: None
        tid, err = _mkt._require_tenant()
        assert tid is None
        assert err is not None

    def test_error_is_403_on_none_tenant(self):
        sys.modules["app.routes.admin"]._actor_tenant_id = lambda: None
        _, err = _mkt._require_tenant()
        _, status = err
        assert status == 403

    def test_returns_tenant_id_when_set(self):
        sys.modules["app.routes.admin"]._actor_tenant_id = lambda: "tenant_abc"
        tid, err = _mkt._require_tenant()
        assert tid == "tenant_abc"
        assert err is None

    def test_refuses_empty_string_tenant(self):
        sys.modules["app.routes.admin"]._actor_tenant_id = lambda: ""
        tid, err = _mkt._require_tenant()
        assert tid is None
        assert err is not None

    def teardown_method(self, _):
        sys.modules["app.routes.admin"]._actor_tenant_id = lambda: None


# ── Exception mapping tests ───────────────────────────────────────────────────

class TestMapCampaignError:
    """_map_campaign_error() maps each exception to the correct HTTP status.

    Exception instances are created from the service module loaded by THIS file
    (_p82d1_svc). _map_campaign_error() lazily imports from
    sys.modules["app.marketing.campaign_service"], which may be a different
    _load() instance when the full suite runs. We therefore match by class NAME
    rather than class identity — the same strategy used in TestReconcileGuards.
    """

    def _make_validation_error(self, *msgs):
        # Always pull from sys.modules["app.marketing.campaign_service"] so the
        # exception class identity matches what _map_campaign_error imports.
        svc = sys.modules["app.marketing.campaign_service"]
        result = svc.ValidationResult(errors=tuple(msgs))
        return svc.CampaignValidationError(result)

    def _make_engine_disabled(self):
        return sys.modules["app.marketing.campaign_service"].CampaignEngineDisabled("off")

    def _make_transition_error(self, f, t):
        return sys.modules["app.marketing.campaign_service"].CampaignTransitionError(f, t)

    def test_engine_disabled_maps_to_404(self):
        exc = self._make_engine_disabled()
        _, status = _mkt._map_campaign_error(exc)
        assert status == 404

    def test_engine_disabled_body_says_not_found(self):
        exc = self._make_engine_disabled()
        body, _ = _mkt._map_campaign_error(exc)
        assert "not found" in str(body).lower()

    def test_validation_error_maps_to_400(self):
        exc = self._make_validation_error("name is required")
        _, status = _mkt._map_campaign_error(exc)
        assert status == 400

    def test_validation_error_body_contains_detail(self):
        exc = self._make_validation_error("name is required")
        body, _ = _mkt._map_campaign_error(exc)
        assert "validation" in str(body).lower()

    def test_validation_error_body_has_errors_list(self):
        exc = self._make_validation_error("name is required", "message_body is required")
        body, _ = _mkt._map_campaign_error(exc)
        body_str = str(body)
        assert "name is required" in body_str
        assert "message_body is required" in body_str

    def test_transition_error_maps_to_409(self):
        exc = self._make_transition_error("draft", "running")
        _, status = _mkt._map_campaign_error(exc)
        assert status == 409

    def test_transition_error_body_contains_detail(self):
        exc = self._make_transition_error("draft", "running")
        body, _ = _mkt._map_campaign_error(exc)
        assert "transition" in str(body).lower()

    def test_unexpected_exception_is_reraised(self):
        exc = RuntimeError("unexpected")
        with pytest.raises(RuntimeError, match="unexpected"):
            _mkt._map_campaign_error(exc)


# ── Module-level object tests ─────────────────────────────────────────────────

class TestBlueprintObject:
    def test_blueprint_object_exists(self):
        assert hasattr(_mkt, "marketing_bp")

    def test_blueprint_name_is_marketing(self):
        assert _mkt.marketing_bp.name == "marketing"


# ── Circular import / isolation tests ─────────────────────────────────────────

class TestCircularImportGuard:
    def test_marketing_routes_importable_without_full_app(self):
        """marketing.py must not pull in app startup at load time."""
        src = open(_MKT_PATH, encoding="utf-8").read()
        top_imports = [
            line for line in src.splitlines()
            if line.startswith("from app.") or line.startswith("import app.")
        ]
        assert not top_imports

    def test_marketing_import_is_in_blueprint_registration_block(self):
        """marketing_bp import must be in the blueprint block, not after the flag check.

        Blueprint imports live in the blueprint registration section (before
        service init) — they must NOT be deferred inside the flag guard.
        """
        # Blueprint registration block comes before the flag check.
        mkt_import_pos = _INIT_SRC.index("from app.routes.marketing import marketing_bp")
        flag_pos = _INIT_SRC.index("campaign_engine_v2_enabled()")
        # The marketing import (blueprint) must precede the flag check (worker)
        assert mkt_import_pos < flag_pos, (
            "marketing_bp import must be in the blueprint block, before the flag check"
        )


# ── Forbidden modification guard ──────────────────────────────────────────────

class TestForbiddenModifications:
    """Legacy files must not reference marketing.py or be altered for V2."""

    FORBIDDEN = [
        ("app/services/campaign_service.py", "marketing_bp"),
        ("app/routes/broadcast.py",          "marketing_bp"),
        ("app/routes/broadcast.py",          "campaign_engine_v2"),
        ("app/services/followup_service.py", "marketing_bp"),
        ("app/marketing/campaign_worker.py", "marketing_bp"),
        ("app/persistence/campaign_repository.py", "marketing_bp"),
    ]

    def test_forbidden_cross_references_absent(self):
        for relpath, term in self.FORBIDDEN:
            full = os.path.join(_ROOT, relpath)
            if not os.path.exists(full):
                continue
            src = open(full, encoding="utf-8").read()
            assert term not in src, (
                f"{relpath} must not reference {term!r}"
            )

    def test_legacy_campaign_routes_unchanged(self):
        """admin.campaign_send must still call the legacy start_campaign."""
        admin_src = open(
            os.path.join(_ROOT, "app", "routes", "admin.py"), encoding="utf-8"
        ).read()
        assert "from app.services.campaign_service import start_campaign" in admin_src
