"""
Phase 8.2E.3A — AUTH_MODE enforcement tests.

Covers:
  - All three valid modes are accepted at import time.
  - An unrecognised value raises RuntimeError unconditionally (no DEBUG bypass).
  - Default (no env var) resolves to SESSION_ONLY.
  - ADMIN_KEY_ONLY is rejected in production (DEBUG=False).
  - DUAL is rejected in production (ADR-023 D5 / R1).
  - SESSION_ONLY boots cleanly in production.
  - DEBUG=True disables the production-only mode refusal.
  - app/__init__.py logs AUTH_MODE at startup (source-level assertion).
"""
import importlib.util
import os
import sys
import types

import pytest

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "app", "config.py")


# ── helpers ──────────────────────────────────────────────────────────────────

def _load_config(env: dict):
    """Load app/config.py in a fresh module context under a controlled environment.

    Uses spec_from_file_location to execute the file directly — this avoids
    touching sys.modules["app"] or the package import chain entirely, so the
    test module never corrupts the shared package state.

    Caller provides only the env vars they care about; everything else is
    supplied with safe non-default values so the existing production checks
    (SECRET_KEY, ADMIN_KEY, BROADCAST_API_KEY) do not fire unexpectedly.
    """
    safe_env = {
        "DATABASE_URL": "postgresql://test:test@localhost/testdb",
        "SECRET_KEY": "some-real-secret-key-not-default",
        "ADMIN_KEY": "real-admin-key-not-default",
        "BROADCAST_API_KEY": "real-broadcast-key-not-default",
    }
    safe_env.update(env)

    old_env = os.environ.copy()
    for k in ("AUTH_MODE", "FLASK_ENV", "DEBUG",
               "DATABASE_URL", "SECRET_KEY", "ADMIN_KEY", "BROADCAST_API_KEY"):
        os.environ.pop(k, None)
    os.environ.update(safe_env)

    try:
        spec = importlib.util.spec_from_file_location("_test_app_config", _CONFIG_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k in list(os.environ.keys()):
            if k not in old_env:
                del os.environ[k]
        os.environ.update(old_env)


# ── valid-mode acceptance ─────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["SESSION_ONLY", "DUAL", "ADMIN_KEY_ONLY"])
def test_valid_modes_accepted(mode):
    """Each valid AUTH_MODE value must load without error (DEBUG=True bypasses prod checks)."""
    cfg = _load_config({"AUTH_MODE": mode, "FLASK_ENV": "development"})
    assert cfg.AUTH_MODE == mode


# ── default value ─────────────────────────────────────────────────────────────

def test_default_is_session_only():
    """Absent AUTH_MODE env var must resolve to SESSION_ONLY (ADR-023 D5.3)."""
    cfg = _load_config({"FLASK_ENV": "development"})   # no AUTH_MODE key
    assert cfg.AUTH_MODE == "SESSION_ONLY"


# ── unrecognised value ────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_value", ["NONE", "session_only", "admin", "", "UNKNOWN"])
def test_invalid_mode_raises_regardless_of_debug(bad_value):
    """An unrecognised AUTH_MODE must raise RuntimeError in all environments."""
    with pytest.raises(RuntimeError, match="not recognised"):
        _load_config({"AUTH_MODE": bad_value, "FLASK_ENV": "development"})


def test_invalid_mode_raises_in_production():
    with pytest.raises(RuntimeError, match="not recognised"):
        _load_config({"AUTH_MODE": "GARBAGE"})   # no FLASK_ENV → DEBUG=False


# ── production refusal ────────────────────────────────────────────────────────

def test_admin_key_only_refused_in_production():
    """ADMIN_KEY_ONLY must cause RuntimeError when DEBUG=False (production)."""
    with pytest.raises(RuntimeError, match="not permitted in production"):
        _load_config({"AUTH_MODE": "ADMIN_KEY_ONLY"})   # DEBUG=False


def test_dual_refused_in_production():
    """DUAL must cause RuntimeError when DEBUG=False (ADR-023 R1 escalation path)."""
    with pytest.raises(RuntimeError, match="not permitted in production"):
        _load_config({"AUTH_MODE": "DUAL"})


def test_session_only_accepted_in_production():
    """SESSION_ONLY must boot cleanly in production (DEBUG=False)."""
    cfg = _load_config({"AUTH_MODE": "SESSION_ONLY"})
    assert cfg.AUTH_MODE == "SESSION_ONLY"
    assert cfg.DEBUG is False


# ── DEBUG=True disables production refusal ────────────────────────────────────

def test_admin_key_only_allowed_in_development():
    """DEBUG=True must bypass the production-only mode refusal for local dev."""
    cfg = _load_config({"AUTH_MODE": "ADMIN_KEY_ONLY", "FLASK_ENV": "development"})
    assert cfg.AUTH_MODE == "ADMIN_KEY_ONLY"
    assert cfg.DEBUG is True


def test_dual_allowed_in_development():
    cfg = _load_config({"AUTH_MODE": "DUAL", "FLASK_ENV": "development"})
    assert cfg.AUTH_MODE == "DUAL"
    assert cfg.DEBUG is True


# ── startup log (source-level assertion) ─────────────────────────────────────

def test_startup_log_line_present():
    """app/__init__.py must log AUTH_MODE after populating app.config.

    This is a source-level check — the same pattern used in
    test_campaign_worker_startup.py — so it does not require bootstrapping
    the full application.
    """
    init_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "__init__.py"
    )
    with open(init_path, encoding="utf-8") as fh:
        src = fh.read()

    assert 'logging.getLogger(__name__).info("AUTH_MODE resolved: %s", AUTH_MODE)' in src, (
        "app/__init__.py must emit an INFO log of the resolved AUTH_MODE at startup"
    )
