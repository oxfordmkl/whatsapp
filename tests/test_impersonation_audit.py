"""
Phase 8.2E.6A — Impersonation audit event tests (ADR-023 D3 requirement 2).

Covers:
  - IMPERSONATION_START / IMPERSONATION_END are registered in VALID_ACTIONS.
  - Pre-existing actions are preserved (regression guard).
  - log_audit() actually writes an AuditLog row for the new actions.
  - log_audit() still rejects unknown actions and still never raises.
  - The enter route logs IMPERSONATION_START with the impersonated tenant_id.
  - The exit route logs IMPERSONATION_END.
  - The exit route captures the tenant BEFORE clearing the session — popping
    first would leave the audit entry with no subject. This ordering is the
    property most likely to regress, so it is asserted structurally.

Scope note: this phase adds audit events only. The Campaign model is
untouched and no `impersonated_by` column exists yet (Phase 8.2E.6B).
"""
import importlib.util
import os
import re
import sys
import types
from unittest.mock import MagicMock

import pytest

_ROOT        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AUDIT_PATH  = os.path.join(_ROOT, "app", "services", "audit_service.py")
_ADMIN_PATH  = os.path.join(_ROOT, "app", "routes", "admin.py")

with open(_ADMIN_PATH, encoding="utf-8") as _fh:
    _ADMIN_SRC = _fh.read()


# ── Loading audit_service standalone ─────────────────────────────────────────
#
# audit_service.py imports only json + logging at module level; every app
# import is lazy (inside log_audit). It therefore loads cleanly on its own,
# with no Flask app, no DB, and no conftest stubs.

def _load_audit_service():
    spec = importlib.util.spec_from_file_location("_test_audit_service", _AUDIT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_audit = _load_audit_service()


# ── VALID_ACTIONS registration ───────────────────────────────────────────────

def test_impersonation_start_is_valid_action():
    assert "IMPERSONATION_START" in _audit.VALID_ACTIONS


def test_impersonation_end_is_valid_action():
    assert "IMPERSONATION_END" in _audit.VALID_ACTIONS


@pytest.mark.parametrize("action", [
    "LOGIN_SUCCESS", "LOGIN_FAILURE", "ROLE_CHANGE",
    "BROADCAST_SEND", "DATA_EXPORT",
])
def test_preexisting_actions_preserved(action):
    """Adding new actions must not drop any existing one."""
    assert action in _audit.VALID_ACTIONS


def test_valid_actions_is_exactly_fifteen():
    """Guard against accidental additions slipping in unreviewed.

    7 through Phase 8.2E.6A. Phase 10.2A added the seven LEAD_* mutation
    actions (LEAD_CREATE, LEAD_UPDATE, LEAD_ASSIGN, LEAD_STATUS_CHANGE,
    LEAD_SCORE_CHANGE, LEAD_ADMISSION, LEAD_MESSAGE_SENT) so CRM record changes
    became auditable. Phase 10.3 added LEAD_IMPORT for bulk CSV import, and put
    the long-reserved DATA_EXPORT to use on the new lead export route.

    The count is intentionally asserted rather than derived: this guard exists
    so that widening the audit vocabulary is a deliberate, reviewed act. Update
    the number only alongside an approved phase that adds actions —
    test_preexisting_actions_preserved separately proves nothing was dropped.
    """
    assert len(_audit.VALID_ACTIONS) == 15, sorted(_audit.VALID_ACTIONS)


# ── log_audit() write behaviour ──────────────────────────────────────────────

class _RecordingAuditLog:
    """Stand-in for the AuditLog model that records constructor kwargs."""
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _RecordingAuditLog.instances.append(self)


def _install_model_stubs():
    """Stub app.models + app.extensions so log_audit's lazy imports resolve.

    Returns the stub db so callers can inspect session.add / session.commit.
    """
    _RecordingAuditLog.instances = []

    models_mod = types.ModuleType("app.models")
    models_mod.AuditLog = _RecordingAuditLog

    db_stub = MagicMock()
    ext_mod = types.ModuleType("app.extensions")
    ext_mod.db = db_stub

    if "app" not in sys.modules:
        sys.modules["app"] = types.ModuleType("app")
    sys.modules["app.models"] = models_mod
    sys.modules["app.extensions"] = ext_mod
    return db_stub


@pytest.fixture
def db_stub():
    saved = {k: sys.modules.get(k) for k in ("app", "app.models", "app.extensions")}
    stub = _install_model_stubs()
    try:
        yield stub
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


@pytest.mark.parametrize("action", ["IMPERSONATION_START", "IMPERSONATION_END"])
def test_log_audit_writes_row_for_impersonation_actions(db_stub, action):
    """The new actions must actually reach db.session.add — not be rejected.

    log_audit swallows every exception, so asserting the row was constructed
    is what distinguishes a real write from a silently swallowed failure.
    """
    _audit.log_audit(action, actor="super@platform.test",
                     tenant_id="tenant-abc", target="/crm/super/impersonate/x",
                     detail={"tenant_name": "Acme"}, ip="10.0.0.1")

    assert len(_RecordingAuditLog.instances) == 1, (
        f"{action} produced no AuditLog row — it was rejected or errored"
    )
    assert db_stub.session.add.called
    assert db_stub.session.commit.called


def test_impersonation_start_records_tenant_and_actor(db_stub):
    _audit.log_audit("IMPERSONATION_START", actor="super@platform.test",
                     tenant_id="tenant-abc", target="/crm/super/impersonate/tenant-abc",
                     detail={"tenant_name": "Acme"}, ip="10.0.0.1")

    kw = _RecordingAuditLog.instances[0].kwargs
    assert kw["action"] == "IMPERSONATION_START"
    assert kw["actor"] == "super@platform.test"
    assert kw["tenant_id"] == "tenant-abc", (
        "tenant_id must be the IMPERSONATED tenant so the entry is visible "
        "from the tenant's audit view"
    )
    assert "Acme" in kw["detail"]


def test_unknown_action_still_rejected(db_stub):
    """The allow-list must still reject anything unregistered."""
    _audit.log_audit("IMPERSONATION_SIDEWAYS", actor="x", tenant_id="t")
    assert _RecordingAuditLog.instances == []
    assert not db_stub.session.add.called


def test_log_audit_never_raises_on_broken_db(db_stub):
    """Audit failure must not break the business action it records."""
    db_stub.session.commit.side_effect = RuntimeError("db down")
    # Must not propagate
    _audit.log_audit("IMPERSONATION_START", actor="x", tenant_id="t")


# ── Route wiring (source-level) ──────────────────────────────────────────────
#
# admin.py is ~5400 lines and imports the whole app graph, so these are
# structural assertions on the source — the same approach used by
# tests/test_campaign_worker_startup.py.

def _function_source(name: str) -> str:
    """Return the source of a top-level def, up to the next decorator/def."""
    lines = _ADMIN_SRC.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"def {name}("):
            start = i
            break
    assert start is not None, f"{name} not found in admin.py"

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("@") or lines[j].startswith("def "):
            end = j
            break
    return "\n".join(lines[start:end])


def test_enter_route_logs_impersonation_start():
    src = _function_source("crm_super_impersonate")
    assert "IMPERSONATION_START" in src
    assert "log_audit(" in src


def test_enter_route_passes_impersonated_tenant_id():
    src = _function_source("crm_super_impersonate")
    assert re.search(r"tenant_id\s*=\s*tenant\.id", src), (
        "IMPERSONATION_START must record the impersonated tenant's id"
    )


def test_exit_route_logs_impersonation_end():
    src = _function_source("crm_super_impersonate_exit")
    assert "IMPERSONATION_END" in src
    assert "log_audit(" in src


def test_exit_route_captures_tenant_before_pop():
    """The ordering property: read the session BEFORE clearing it.

    If session.pop runs first, the audit entry loses its subject and
    IMPERSONATION_END records tenant_id=None — the exact failure this
    phase exists to prevent.
    """
    src = _function_source("crm_super_impersonate_exit")

    get_idx = src.find("session.get('impersonate_tenant_id')")
    pop_idx = src.find("session.pop('impersonate_tenant_id'")

    assert get_idx != -1, "exit route must read impersonate_tenant_id"
    assert pop_idx != -1, "exit route must still clear impersonate_tenant_id"
    assert get_idx < pop_idx, (
        "session.get must precede session.pop — otherwise IMPERSONATION_END "
        "is written with tenant_id=None"
    )


def test_exit_route_guards_against_subjectless_entry():
    """Exiting while not impersonating must not write an audit row."""
    src = _function_source("crm_super_impersonate_exit")
    assert re.search(r"if\s+_prev_tenant_id\s*:", src), (
        "exit route must only log when an impersonation was actually active"
    )


def test_session_still_cleared_on_exit():
    """Audit must not have displaced the actual logout-of-tenant behaviour."""
    src = _function_source("crm_super_impersonate_exit")
    assert "session.pop('impersonate_tenant_id', None)" in src
    assert "session.pop('impersonate_tenant_name', None)" in src


def test_campaign_model_has_impersonated_by():
    """Phase 8.2E.6B: impersonated_by column must now exist on Campaign."""
    with open(os.path.join(_ROOT, "app", "models.py"), encoding="utf-8") as fh:
        models_src = fh.read()
    assert "impersonated_by" in models_src, (
        "Phase 8.2E.6B requires impersonated_by on the Campaign model"
    )
