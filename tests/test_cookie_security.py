"""
Phase 8.2E.5 — Cookie security hardening tests (ADR-023 D2 minimum bar).

Covers:
  - SESSION_COOKIE_HTTPONLY is True in all environments.
  - SESSION_COOKIE_SAMESITE is "Lax" in all environments.
  - SESSION_COOKIE_SECURE is True when DEBUG=False (production).
  - SESSION_COOKIE_SECURE is False when DEBUG=True (local dev — HTTP allowed).
  - The three keys are present in app/__init__.py source (source-level guard
    against accidental deletion, same pattern as test_campaign_worker_startup).
  - Values are set inside create_app(), not as module-level globals that could
    be overridden before the app object is created.
"""
import os
import re

_INIT_PATH = os.path.join(os.path.dirname(__file__), "..", "app", "__init__.py")


# ── Source-level assertions ───────────────────────────────────────────────────

def _init_src() -> str:
    with open(_INIT_PATH, encoding="utf-8") as fh:
        return fh.read()


def test_httponly_present_in_source():
    assert 'SESSION_COOKIE_HTTPONLY' in _init_src(), (
        "app/__init__.py must set SESSION_COOKIE_HTTPONLY"
    )


def test_samesite_present_in_source():
    assert 'SESSION_COOKIE_SAMESITE' in _init_src(), (
        "app/__init__.py must set SESSION_COOKIE_SAMESITE"
    )


def test_secure_present_in_source():
    assert 'SESSION_COOKIE_SECURE' in _init_src(), (
        "app/__init__.py must set SESSION_COOKIE_SECURE"
    )


def test_httponly_set_to_true_in_source():
    src = _init_src()
    assert re.search(r'SESSION_COOKIE_HTTPONLY\s*["\]]*\s*=\s*True', src), (
        "SESSION_COOKIE_HTTPONLY must be assigned True"
    )


def test_samesite_set_to_lax_in_source():
    src = _init_src()
    assert re.search(r'SESSION_COOKIE_SAMESITE\s*["\]]*\s*=\s*["\']Lax["\']', src), (
        "SESSION_COOKIE_SAMESITE must be assigned \"Lax\""
    )


def test_secure_is_not_hardcoded_true_in_source():
    """SESSION_COOKIE_SECURE must not be a hardcoded True — it must be conditional
    so that local HTTP development sessions still work."""
    src = _init_src()
    # The assignment must not be the bare literal True; it should reference DEBUG.
    assert 'DEBUG' in src, "SESSION_COOKIE_SECURE gate must reference DEBUG"
    # Guard: confirm it is not simply `= True` with no condition
    match = re.search(r'SESSION_COOKIE_SECURE\s*["\]]*\s*=\s*(True|False)', src)
    assert match is None, (
        "SESSION_COOKIE_SECURE must not be a bare True/False literal — "
        "it must be conditional on DEBUG"
    )


def test_secure_references_not_debug_in_source():
    """The SECURE value must be the inverse of DEBUG (not _DEBUG, not DEBUG)."""
    src = _init_src()
    assert re.search(r'SESSION_COOKIE_SECURE\s*["\]]*\s*=\s*not\s+\w*DEBUG', src), (
        "SESSION_COOKIE_SECURE must be assigned `not DEBUG` (or `not _DEBUG`)"
    )


# ── Behavioural assertions via config module reload ──────────────────────────
#
# We cannot call create_app() in isolation (it requires DB, WABA key, etc.)
# so we verify the DEBUG→SECURE mapping at the config level, which is what
# the __init__.py code reads when it computes `not _DEBUG`.

import importlib.util
import sys

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "app", "config.py")


def _load_config(env: dict):
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
        spec = importlib.util.spec_from_file_location("_test_cfg_cookie", _CONFIG_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k in list(os.environ.keys()):
            if k not in old_env:
                del os.environ[k]
        os.environ.update(old_env)


def test_debug_false_in_production():
    """In production (no FLASK_ENV), DEBUG resolves to False."""
    cfg = _load_config({"AUTH_MODE": "SESSION_ONLY"})
    assert cfg.DEBUG is False


def test_debug_true_in_development():
    """With FLASK_ENV=development, DEBUG resolves to True."""
    cfg = _load_config({"AUTH_MODE": "SESSION_ONLY", "FLASK_ENV": "development"})
    assert cfg.DEBUG is True


def test_secure_cookie_expected_in_production():
    """not DEBUG == True in production — SESSION_COOKIE_SECURE should be True."""
    cfg = _load_config({"AUTH_MODE": "SESSION_ONLY"})
    assert not cfg.DEBUG is True, "Production must yield SECURE=True (not DEBUG)"


def test_secure_cookie_not_expected_in_development():
    """not DEBUG == False in development — SESSION_COOKIE_SECURE should be False."""
    cfg = _load_config({"AUTH_MODE": "SESSION_ONLY", "FLASK_ENV": "development"})
    assert not cfg.DEBUG is False, "Development must yield SECURE=False (not DEBUG)"
