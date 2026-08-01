"""
Phase 8.2E.2 — campaign_admin_required RBAC tests.

Proves for every mutation route:
  1. Flag OFF  → 404 (before RBAC runs — flag is outermost)
  2. Unauthn   → 403  (check_auth fails)
  3. STAFF     → 403  (authenticated but wrong role)
  4. ADMIN     → pass through to handler
  5. SUPER_ADMIN with tenant → pass through to handler
  6. GET routes are unaffected (no role gate added)

Mutation routes under test:
  POST /crm/campaigns/v2                    create_campaign
  POST /crm/campaigns/v2/<id>/validate      validate_campaign
  POST /crm/campaigns/v2/<id>/schedule      schedule_campaign
  POST /crm/campaigns/v2/<id>/launch        launch_campaign
  POST /crm/campaigns/v2/<id>/cancel        cancel_campaign
  POST /crm/campaigns/v2/<id>/archive       archive_campaign

GET routes confirmed unaffected:
  GET  /crm/campaigns/v2                    list_campaigns
  GET  /crm/campaigns/v2/<id>               get_campaign
  GET  /crm/campaigns/v2/<id>/progress      campaign_progress
"""
import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

_ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MKT_PATH = os.path.join(_ROOT, "app", "routes", "marketing.py")
_MKT_SRC  = open(_MKT_PATH, encoding="utf-8").read()


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

    # safe defaults — tests override per-call
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
    "_rbac_svc",
    os.path.join(_ROOT, "app", "marketing", "campaign_service.py"),
)
sys.modules["app.marketing.campaign_service"] = _svc_mod

_mkt = _load_module("_rbac_mkt", _MKT_PATH)


# ── Context helpers ───────────────────────────────────────────────────────────

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


def _with_auth(authn, fn):
    sys.modules["app.routes.admin"].check_auth = lambda: authn
    try:
        return fn()
    finally:
        sys.modules["app.routes.admin"].check_auth = lambda: True


def _with_role(role, fn):
    """Set get_current_actor to return a specific role (authn=True)."""
    actor = {
        "authenticated": True,
        "username": "testuser",
        "role": role,
        "source": "SESSION",
    }
    sys.modules["app.routes.admin"].get_current_actor = lambda: actor
    try:
        return fn()
    finally:
        sys.modules["app.routes.admin"].get_current_actor = lambda: {
            "authenticated": True, "username": "admin",
            "role": "ADMIN", "source": "SESSION",
        }


def _inject(svc, fn):
    orig = _mkt._make_service
    _mkt._make_service = lambda: svc
    try:
        return fn()
    finally:
        _mkt._make_service = orig


def _with_auth_mode(mode, fn):
    """Override _mkt._auth_mode() for the duration of fn().

    Replaces the module-level helper so current_app is never consulted —
    the test controls the resolved mode directly.
    """
    orig = _mkt._auth_mode
    _mkt._auth_mode = lambda: mode
    try:
        return fn()
    finally:
        _mkt._auth_mode = orig


def _unpack(raw):
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], int):
        inner, status = raw
        body = inner[0] if isinstance(inner, tuple) else inner
        return body, status
    body = raw[0] if isinstance(raw, tuple) else raw
    return body, 200


# ── Campaign / service factories ──────────────────────────────────────────────

def _make_campaign(id=1, status="draft"):
    c = MagicMock()
    c.id = id; c.name = "Test"; c.status = status
    c.total_recipients = 0; c.sent_count = 0; c.failed_count = 0
    c.created_at = None; c.updated_at = None; c.created_by = "admin"
    c.scheduled_at = None; c.started_at = None; c.completed_at = None
    c.description = None; c.message_body = "Hello"
    c.template_id = None; c.audience_rule_id = None; c.failure_reason = None
    return c


def _make_svc():
    """Service stub that succeeds on every call."""
    svc = MagicMock()
    camp = _make_campaign()
    svc.get_campaign.return_value = camp
    svc.create_campaign.return_value = camp
    svc.mark_validated.return_value = camp
    svc.schedule.return_value = camp
    svc.mark_running.return_value = camp
    svc.cancel.return_value = camp
    svc.archive.return_value = camp
    svc.list_campaigns.return_value = []
    svc.repository = MagicMock()
    svc.repository.count_for_tenant.return_value = 0
    svc.progress.return_value = {}
    return svc


# ── Shared test runner for mutation routes ────────────────────────────────────

def _run_mutation(fn, *, authn=True, role="ADMIN", tenant="T1", flag=True,
                  svc=None, mode="SESSION_ONLY"):
    """Run a mutation route function under controlled auth/flag/tenant/mode context.

    Returns (body, status_code).
    """
    if svc is None:
        svc = _make_svc()
    # Patch request.get_json for routes that read the body
    sys.modules["flask"].request.get_json = MagicMock(
        return_value={"name": "Camp", "scheduled_at": "2027-01-01T10:00:00"}
    )
    sys.modules["flask"].request.args = MagicMock()
    sys.modules["flask"].request.args.get = MagicMock(return_value=None)

    def _inner():
        return _with_auth_mode(mode, lambda:
               _with_auth(authn, lambda:
               _with_role(role, lambda:
               _with_tenant(tenant, lambda:
               _inject(svc, fn)))))

    return _unpack(_with_flag(flag, _inner))


# ─────────────────────────────────────────────────────────────────────────────
# TEST: SOURCE STRUCTURE
# ─────────────────────────────────────────────────────────────────────────────

class TestRbacSourceStructure:
    """Verify the decorator is defined and applied correctly in source."""

    def test_decorator_defined(self):
        assert "def campaign_admin_required" in _MKT_SRC

    def test_decorator_applied_to_create(self):
        assert "@campaign_admin_required" in _MKT_SRC

    def test_get_current_actor_used_in_decorator(self):
        assert "get_current_actor" in _MKT_SRC

    def test_require_engine_still_present(self):
        assert "require_campaign_engine" in _MKT_SRC

    def test_mutation_routes_have_rbac_applied(self):
        """All six mutation route definitions must follow campaign_admin_required."""
        lines = _MKT_SRC.splitlines()
        mutation_defs = {
            "def create_campaign", "def validate_campaign",
            "def schedule_campaign", "def launch_campaign",
            "def cancel_campaign", "def archive_campaign",
        }
        for i, line in enumerate(lines):
            stripped = line.strip()
            if any(stripped.startswith(d) for d in mutation_defs):
                # Look back for @campaign_admin_required within 3 lines
                lookback = "\n".join(lines[max(0, i-3):i])
                assert "@campaign_admin_required" in lookback, (
                    f"{stripped}: @campaign_admin_required not found in preceding lines:\n{lookback}"
                )

    def test_get_routes_lack_rbac_decorator(self):
        """GET routes must not have campaign_admin_required applied."""
        lines = _MKT_SRC.splitlines()
        get_defs = {"def list_campaigns", "def get_campaign", "def campaign_progress"}
        for i, line in enumerate(lines):
            stripped = line.strip()
            if any(stripped.startswith(d) for d in get_defs):
                lookback = "\n".join(lines[max(0, i-4):i])
                assert "@campaign_admin_required" not in lookback, (
                    f"{stripped}: @campaign_admin_required incorrectly applied"
                )

    def test_no_new_auth_logic(self):
        """Decorator must delegate; must not reimplement check_password or raw key comparison."""
        assert "check_password" not in _MKT_SRC
        # ADMIN_KEY must not appear outside a comment/docstring — i.e. not in executable code.
        import tokenize, io
        tokens = list(tokenize.generate_tokens(io.StringIO(_MKT_SRC).readline))
        code_tokens = [
            tok_str for tok_type, tok_str, _, _, _ in tokens
            if tok_type not in (tokenize.COMMENT, tokenize.STRING, tokenize.ENCODING,
                                tokenize.NEWLINE, tokenize.NL, tokenize.INDENT,
                                tokenize.DEDENT, tokenize.ENDMARKER)
        ]
        assert "ADMIN_KEY" not in code_tokens, "ADMIN_KEY must not appear in executable code"

    def test_json_403_not_redirect(self):
        """Decorator returns JSON 403, not an HTML redirect."""
        assert "redirect(" not in _MKT_SRC
        assert "url_for(" not in _MKT_SRC


# ─────────────────────────────────────────────────────────────────────────────
# TEST: FLAG OUTERMOST (fires before RBAC)
# ─────────────────────────────────────────────────────────────────────────────

MUTATION_FNS = [
    lambda: _mkt.create_campaign(),
    lambda: _mkt.validate_campaign(1),
    lambda: _mkt.schedule_campaign(1),
    lambda: _mkt.launch_campaign(1),
    lambda: _mkt.cancel_campaign(1),
    lambda: _mkt.archive_campaign(1),
]

MUTATION_NAMES = [
    "create_campaign", "validate_campaign", "schedule_campaign",
    "launch_campaign", "cancel_campaign", "archive_campaign",
]


class TestFlagBeforeRbac:
    """Flag OFF must return 404 regardless of role — RBAC must never run first."""

    @pytest.mark.parametrize("name,fn", zip(MUTATION_NAMES, MUTATION_FNS))
    def test_flag_off_returns_404_for_staff(self, name, fn):
        _, status = _run_mutation(fn, flag=False, role="STAFF")
        assert status == 404, f"{name}: expected 404 when flag OFF (STAFF), got {status}"

    @pytest.mark.parametrize("name,fn", zip(MUTATION_NAMES, MUTATION_FNS))
    def test_flag_off_returns_404_for_admin(self, name, fn):
        _, status = _run_mutation(fn, flag=False, role="ADMIN")
        assert status == 404, f"{name}: expected 404 when flag OFF (ADMIN), got {status}"

    @pytest.mark.parametrize("name,fn", zip(MUTATION_NAMES, MUTATION_FNS))
    def test_flag_off_does_not_return_403(self, name, fn):
        """If 403 is returned with flag OFF, RBAC ran before the flag check — wrong order."""
        _, status = _run_mutation(fn, flag=False, role="STAFF")
        assert status != 403, f"{name}: got 403 with flag OFF — decorator order wrong"


# ─────────────────────────────────────────────────────────────────────────────
# TEST: UNAUTHENTICATED (flag ON, no valid session/key)
# ─────────────────────────────────────────────────────────────────────────────

class TestUnauthenticated:

    @pytest.mark.parametrize("name,fn", zip(MUTATION_NAMES, MUTATION_FNS))
    def test_unauthenticated_returns_403(self, name, fn):
        _, status = _run_mutation(fn, authn=False, flag=True)
        assert status == 403, f"{name}: unauthenticated should get 403, got {status}"

    @pytest.mark.parametrize("name,fn", zip(MUTATION_NAMES, MUTATION_FNS))
    def test_service_not_called_when_unauthenticated(self, name, fn):
        svc = _make_svc()
        _run_mutation(fn, authn=False, flag=True, svc=svc)
        svc.create_campaign.assert_not_called()
        svc.mark_validated.assert_not_called()
        svc.schedule.assert_not_called()
        svc.mark_running.assert_not_called()
        svc.cancel.assert_not_called()
        svc.archive.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# TEST: STAFF RECEIVES 403
# ─────────────────────────────────────────────────────────────────────────────

class TestStaffForbidden:
    """STAFF is authenticated but lacks ADMIN/SUPER_ADMIN role — must get 403."""

    @pytest.mark.parametrize("name,fn", zip(MUTATION_NAMES, MUTATION_FNS))
    def test_staff_returns_403(self, name, fn):
        _, status = _run_mutation(fn, authn=True, role="STAFF", flag=True)
        assert status == 403, f"{name}: STAFF should get 403, got {status}"

    @pytest.mark.parametrize("name,fn", zip(MUTATION_NAMES, MUTATION_FNS))
    def test_403_body_says_forbidden(self, name, fn):
        body, status = _run_mutation(fn, authn=True, role="STAFF", flag=True)
        assert status == 403
        assert "Forbidden" in str(body) or "forbidden" in str(body).lower(), (
            f"{name}: 403 body should mention Forbidden, got {body!r}"
        )

    @pytest.mark.parametrize("name,fn", zip(MUTATION_NAMES, MUTATION_FNS))
    def test_service_not_called_for_staff(self, name, fn):
        svc = _make_svc()
        _run_mutation(fn, authn=True, role="STAFF", flag=True, svc=svc)
        svc.create_campaign.assert_not_called()
        svc.mark_validated.assert_not_called()
        svc.schedule.assert_not_called()
        svc.mark_running.assert_not_called()
        svc.cancel.assert_not_called()
        svc.archive.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# TEST: ADMIN SUCCEEDS
# ─────────────────────────────────────────────────────────────────────────────

class TestAdminAllowed:
    """ADMIN role must pass the decorator and reach the handler."""

    def test_create_campaign_admin_passes(self):
        _, status = _run_mutation(lambda: _mkt.create_campaign(),
                                  role="ADMIN", tenant="T1")
        assert status in (200, 201), f"ADMIN create_campaign got {status}"

    def test_validate_campaign_admin_passes(self):
        _, status = _run_mutation(lambda: _mkt.validate_campaign(1),
                                  role="ADMIN", tenant="T1")
        assert status == 200, f"ADMIN validate_campaign got {status}"

    def test_schedule_campaign_admin_passes(self):
        _, status = _run_mutation(lambda: _mkt.schedule_campaign(1),
                                  role="ADMIN", tenant="T1")
        assert status == 200, f"ADMIN schedule_campaign got {status}"

    def test_launch_campaign_admin_passes(self):
        _, status = _run_mutation(lambda: _mkt.launch_campaign(1),
                                  role="ADMIN", tenant="T1")
        assert status == 200, f"ADMIN launch_campaign got {status}"

    def test_cancel_campaign_admin_passes(self):
        _, status = _run_mutation(lambda: _mkt.cancel_campaign(1),
                                  role="ADMIN", tenant="T1")
        assert status == 200, f"ADMIN cancel_campaign got {status}"

    def test_archive_campaign_admin_passes(self):
        _, status = _run_mutation(lambda: _mkt.archive_campaign(1),
                                  role="ADMIN", tenant="T1")
        assert status == 200, f"ADMIN archive_campaign got {status}"


# ─────────────────────────────────────────────────────────────────────────────
# TEST: SUPER_ADMIN WITH TENANT SUCCEEDS
# ─────────────────────────────────────────────────────────────────────────────

class TestSuperAdminWithTenantAllowed:
    """SUPER_ADMIN impersonating a tenant must pass the decorator."""

    def test_create_campaign_super_admin_passes(self):
        _, status = _run_mutation(lambda: _mkt.create_campaign(),
                                  role="SUPER_ADMIN", tenant="T1")
        assert status in (200, 201), f"SUPER_ADMIN create_campaign got {status}"

    def test_launch_campaign_super_admin_passes(self):
        _, status = _run_mutation(lambda: _mkt.launch_campaign(1),
                                  role="SUPER_ADMIN", tenant="T1")
        assert status == 200, f"SUPER_ADMIN launch_campaign got {status}"

    def test_cancel_campaign_super_admin_passes(self):
        _, status = _run_mutation(lambda: _mkt.cancel_campaign(1),
                                  role="SUPER_ADMIN", tenant="T1")
        assert status == 200, f"SUPER_ADMIN cancel_campaign got {status}"

    def test_super_admin_no_tenant_blocked_at_tenant_guard(self):
        """SUPER_ADMIN without impersonation gets 403 from _require_tenant, not RBAC."""
        _, status = _run_mutation(lambda: _mkt.create_campaign(),
                                  role="SUPER_ADMIN", tenant=None)
        assert status == 403


# ─────────────────────────────────────────────────────────────────────────────
# TEST: GET ROUTES UNAFFECTED
# ─────────────────────────────────────────────────────────────────────────────

class TestGetRoutesUnaffected:
    """GET routes require only authentication, not ADMIN role."""

    def _get_svc(self):
        svc = _make_svc()
        return svc

    def test_list_campaigns_staff_allowed(self):
        svc = self._get_svc()
        # req.args.get("page", 1, type=int) must return an int, not None
        req_mock = MagicMock()
        req_mock.args.get = MagicMock(side_effect=lambda k, default=None, **kw: default)
        sys.modules["flask"].request = req_mock
        _, status = _unpack(
            _with_flag(True, lambda:
            _with_auth(True, lambda:
            _with_role("STAFF", lambda:
            _with_tenant("T1", lambda:
            _inject(svc, lambda: _mkt.list_campaigns())))))
        )
        assert status == 200, f"STAFF list_campaigns should pass, got {status}"

    def test_get_campaign_staff_allowed(self):
        svc = self._get_svc()
        _, status = _unpack(
            _with_flag(True, lambda:
            _with_auth(True, lambda:
            _with_role("STAFF", lambda:
            _with_tenant("T1", lambda:
            _inject(svc, lambda: _mkt.get_campaign(1))))))
        )
        assert status == 200, f"STAFF get_campaign should pass, got {status}"

    def test_campaign_progress_staff_allowed(self):
        svc = self._get_svc()
        _, status = _unpack(
            _with_flag(True, lambda:
            _with_auth(True, lambda:
            _with_role("STAFF", lambda:
            _with_tenant("T1", lambda:
            _inject(svc, lambda: _mkt.campaign_progress(1))))))
        )
        assert status == 200, f"STAFF campaign_progress should pass, got {status}"

    def test_list_campaigns_unauthn_still_blocked(self):
        """Authentication is still required on GET routes."""
        svc = self._get_svc()
        _, status = _unpack(
            _with_flag(True, lambda:
            _with_auth(False, lambda:
            _inject(svc, lambda: _mkt.list_campaigns())))
        )
        assert status == 403


# ─────────────────────────────────────────────────────────────────────────────
# TEST: ROLE BOUNDARY — unknown / empty roles
# ─────────────────────────────────────────────────────────────────────────────

class TestRoleBoundary:

    @pytest.mark.parametrize("role", ["", None, "VIEWER", "staff", "Admin", "superadmin"])
    def test_non_admin_roles_are_forbidden(self, role):
        _, status = _run_mutation(lambda: _mkt.launch_campaign(1),
                                  authn=True, role=role, flag=True)
        assert status == 403, f"role={role!r} should get 403, got {status}"

    @pytest.mark.parametrize("role", ["ADMIN", "SUPER_ADMIN"])
    def test_exact_role_strings_pass(self, role):
        _, status = _run_mutation(lambda: _mkt.launch_campaign(1),
                                  authn=True, role=role, tenant="T1", flag=True)
        assert status == 200, f"role={role!r} should pass, got {status}"


# ─────────────────────────────────────────────────────────────────────────────
# TEST: AUTH_MODE GATE — ADR-023 D1
# ─────────────────────────────────────────────────────────────────────────────

_MUTATION_FNS = [
    ("create_campaign",   lambda: _mkt.create_campaign()),
    ("validate_campaign", lambda: _mkt.validate_campaign(1)),
    ("schedule_campaign", lambda: _mkt.schedule_campaign(1)),
    ("launch_campaign",   lambda: _mkt.launch_campaign(1)),
    ("cancel_campaign",   lambda: _mkt.cancel_campaign(1)),
    ("archive_campaign",  lambda: _mkt.archive_campaign(1)),
]


class TestAuthModeGate:
    """ADR-023 D1: mutation routes return 404 when AUTH_MODE != SESSION_ONLY.

    Ordering contract under test:
      require_campaign_engine (flag) is outermost → fires before mode check
      campaign_admin_required (mode) fires before authn and role checks

    So:
      flag OFF  → 404  (require_campaign_engine, regardless of mode)
      flag ON, mode wrong → 404  (campaign_admin_required mode gate)
      flag ON, mode OK, authn fail → 403
      flag ON, mode OK, authn OK, wrong role → 403
      flag ON, mode OK, authn OK, right role → 200
    """

    @pytest.mark.parametrize("name,fn", _MUTATION_FNS)
    def test_admin_key_only_returns_404(self, name, fn):
        """ADMIN_KEY_ONLY must yield 404 on every mutation route when flag is ON."""
        _, status = _run_mutation(fn, authn=True, role="ADMIN", flag=True,
                                  mode="ADMIN_KEY_ONLY")
        assert status == 404, f"{name}: expected 404 under ADMIN_KEY_ONLY, got {status}"

    @pytest.mark.parametrize("name,fn", _MUTATION_FNS)
    def test_dual_returns_404(self, name, fn):
        """DUAL must yield 404 on every mutation route when flag is ON (ADR-023 R1)."""
        _, status = _run_mutation(fn, authn=True, role="ADMIN", flag=True,
                                  mode="DUAL")
        assert status == 404, f"{name}: expected 404 under DUAL, got {status}"

    @pytest.mark.parametrize("name,fn", _MUTATION_FNS)
    def test_empty_mode_returns_404(self, name, fn):
        """An absent/empty AUTH_MODE must yield 404 — same non-disclosure posture."""
        _, status = _run_mutation(fn, authn=True, role="ADMIN", flag=True,
                                  mode="")
        assert status == 404, f"{name}: expected 404 under empty mode, got {status}"

    @pytest.mark.parametrize("name,fn", _MUTATION_FNS)
    def test_session_only_passes_through(self, name, fn):
        """SESSION_ONLY must not trigger the mode gate — allows authn+role to decide."""
        _, status = _run_mutation(fn, authn=True, role="ADMIN", tenant="T1",
                                  flag=True, mode="SESSION_ONLY")
        # create_campaign returns 201; all other mutations return 200
        assert status in (200, 201), f"{name}: expected 2xx under SESSION_ONLY, got {status}"

    @pytest.mark.parametrize("name,fn", _MUTATION_FNS)
    def test_flag_off_beats_mode_check(self, name, fn):
        """Flag OFF must return 404 before the mode check fires.

        Both require_campaign_engine (flag) and campaign_admin_required (mode)
        return 404, but flag is outermost — confirmed by verifying the result
        under ADMIN_KEY_ONLY with flag OFF still returns 404.
        """
        _, status = _run_mutation(fn, authn=True, role="ADMIN", flag=False,
                                  mode="ADMIN_KEY_ONLY")
        assert status == 404, f"{name}: flag OFF should 404 regardless of mode, got {status}"

    @pytest.mark.parametrize("name,fn", _MUTATION_FNS)
    def test_mode_404_not_403(self, name, fn):
        """Mode gate must return 404, not 403 (non-disclosure — ADR-023 D1 / R2)."""
        body, status = _run_mutation(fn, authn=True, role="ADMIN", flag=True,
                                     mode="DUAL")
        assert status == 404, f"{name}: mode gate must be 404, got {status}"
        # Must not leak RBAC information via the error body
        error_str = str(body).lower()
        assert "forbidden" not in error_str and "unauthorized" not in error_str, (
            f"{name}: mode-gate 404 body must not contain auth error words: {body}"
        )

    def test_mode_gate_precedes_authn(self):
        """Mode check fires before authn — an unauthn request under DUAL still gets 404."""
        _, status = _run_mutation(lambda: _mkt.launch_campaign(1),
                                  authn=False, role="ADMIN", flag=True,
                                  mode="DUAL")
        assert status == 404, f"mode gate must precede authn check, got {status}"

    def test_mode_gate_precedes_role_check(self):
        """Mode check fires before role — a STAFF user under DUAL still gets 404, not 403."""
        _, status = _run_mutation(lambda: _mkt.launch_campaign(1),
                                  authn=True, role="STAFF", flag=True,
                                  mode="DUAL")
        assert status == 404, f"mode gate must precede role check, got {status}"
