"""
tests/conftest.py — Test isolation anchor.

PROBLEM
-------
pytest collects (imports) ALL test modules during the collection phase before
any test function runs.  Two separate contamination vectors exist:

  Vector 1 — Module-object replacement (collection-time)
  -------------------------------------------------------
  test_memory_provider.py:   sys.modules["app.models"] = <object A>
  test_memory_observe.py:    sys.modules["app.models"] = <object B>  # replaces A

  After collection sys.modules["app.models"] is <object B>.
  test_memory_provider.py's module-level `_models` still points to <object A>.
  _mock_query() writes _models.ConversationMessage = mock to <object A>, but
  MemoryProvider does `from app.models import ConversationMessage` which reads
  sys.modules ("app.models") = <object B>.  ConversationMessage not found
  -> fail-open returns [] -> 5 tests fail.

  Vector 2 — Class-replacement at test runtime
  ---------------------------------------------
  test_memory_activation.py and test_context_assembler.py replace
  sys.modules["app.memory.provider"].MemoryProvider with a MagicMock per
  test (in _mock_memory / _stub_provider).  This mutates the live module's
  __dict__, which is also the globals dict for every method defined in that
  module.  After the test the mock is never restored.  When test_memory_observe
  or test_memory_provider then run, the observer imports MemoryProvider from
  sys.modules and gets the mock.  Calling mock.fetch_with_stats() returns a
  MagicMock(); the caller unpacks `turns, stats = MagicMock()` -> "not enough
  values to unpack (expected 2, got 0)".

FIX — two-part
--------------
  Part A (collection-time): install canonical stubs for app.models and
  app.config HERE, before any test file is imported.  Every test file must
  use sys.modules.setdefault() / sys.modules.get() for these two keys —
  never a fresh assignment that would replace this file's objects.

  Part B (runtime): load the provider module once, save the real MemoryProvider
  class, and restore it to sys.modules["app.memory.provider"] after every test
  via an autouse fixture.  Also reset feature flags to OFF after every test so
  no flag bleeds into the next test.

SCOPE
-----
Only app.models, app.config, and the MemoryProvider class identity are
stabilised here.  Everything else (flask, google, extensions, etc.) is safe
to overwrite across files because those stubs all produce functionally
identical MagicMock values.

DO NOT add router / webhook / heavy production module loads here.  Each test
file loads only the modules it exercises.  This conftest provides the stable
environment those loads run in.
"""

import sys
import types
import importlib.util
import os
from unittest.mock import MagicMock
import pytest

# ── Locate project root ────────────────────────────────────────────────────
# conftest.py lives in tests/; BASE is the project root one level up.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BASE_DIR   = os.path.dirname(_TESTS_DIR)


def _load_module(dotted: str, relpath: str):
    """Load a module from a file path, register in sys.modules, return it."""
    path = os.path.join(_BASE_DIR, relpath)
    spec = importlib.util.spec_from_file_location(dotted, path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


# ══════════════════════════════════════════════════════════════════════════
# Part A — Canonical shared stubs (collection-time, module-level)
# ══════════════════════════════════════════════════════════════════════════

# app package stub must exist first so sub-packages can be installed on it.
sys.modules.setdefault("app", types.ModuleType("app"))

# ── Canonical app.models ────────────────────────────────────────────────
# One object, shared by all test files.  Tests inject ConversationMessage /
# Tenant mocks by writing to sys.modules["app.models"] directly (or to any
# _models variable obtained via sys.modules.setdefault / sys.modules.get).
_shared_models = types.ModuleType("app.models")
sys.modules["app.models"] = _shared_models

# ── Canonical app.config ────────────────────────────────────────────────
# Feature flags default OFF.  _set_activate / _set_flag helpers write to
# sys.modules["app.config"] so changes are visible to the module-under-test.
_shared_config = types.ModuleType("app.config")
_shared_config.MEMORY_ACTIVATE     = False
_shared_config.MEMORY_OBSERVE_MODE = False
_shared_config.GEMINI_API_KEY      = "test-key"
_shared_config.GEMINI_MODEL        = "gemini-2.5-flash"
_shared_config.SHEETS_ID           = ""
_shared_config.GOOGLE_CREDENTIALS_JSON = "{}"
_shared_config.VERIFY_TOKEN        = "test-verify-token"
sys.modules["app.config"] = _shared_config


# ══════════════════════════════════════════════════════════════════════════
# Part B — Provider module canonical load + per-test restore fixture
# ══════════════════════════════════════════════════════════════════════════

# Minimal stubs required to import app/memory/provider.py cleanly.
sys.modules.setdefault("app.memory",     types.ModuleType("app.memory"))
sys.modules.setdefault("app.extensions", _ext := types.ModuleType("app.extensions"))
_ext.db = MagicMock()  # provider.py imports from app.extensions

# Load the real provider module.  Test files that also call _load() create
# further versions in sys.modules; the fixture below re-anchors the real class
# on whichever version is current at test-teardown time.
_provider_canonical = _load_module("app.memory.provider", "app/memory/provider.py")
_REAL_MEMORY_PROVIDER = _provider_canonical.MemoryProvider


@pytest.fixture(autouse=True)
def _reset_shared_stubs():
    """
    Per-test isolation guard — runs after every test, regardless of file.

    Restores:
      * sys.modules["app.memory.provider"].MemoryProvider  <- real class
        (test_memory_activation + test_context_assembler replace it with
        MagicMock per test; without restoration the mock leaks into later
        tests in other files that call MemoryProvider.fetch_with_stats() via
        the observer)

      * sys.modules["app.config"].MEMORY_ACTIVATE    <- False
      * sys.modules["app.config"].MEMORY_OBSERVE_MODE <- False
        (prevents flag state set by one test from bleeding into the next)
    """
    yield  # test runs here

    # Restore real MemoryProvider on whichever module is current in sys.modules.
    provider_mod = sys.modules.get("app.memory.provider")
    if provider_mod is not None:
        provider_mod.MemoryProvider = _REAL_MEMORY_PROVIDER

    # Reset feature flags.
    config_mod = sys.modules.get("app.config")
    if config_mod is not None:
        config_mod.MEMORY_ACTIVATE     = False
        config_mod.MEMORY_OBSERVE_MODE = False
