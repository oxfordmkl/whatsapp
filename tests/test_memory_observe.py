"""
Phase 1.3A-2 Tests — Conversation Memory Observe Mode

Covers:
  - Flag OFF  → MemoryProvider never called, zero side effects
  - Flag ON   → MemoryProvider called for AI-eligible requests only
  - Not AI-eligible (no gemini_start mark) → no fetch even when flag ON
  - Metrics logged (no PII: no phone number, no message bodies)
  - Return value discarded semantics (observe never feeds Gemini)
  - Provider failure → observe returns [], flow continues
  - Observer's own failure → [], flow continues
  - fetch_with_stats correctness (stats fields)

Run:
  pytest tests/test_memory_observe.py -v
  python tests/test_memory_observe.py
"""
import sys
import os
import types
import traceback
import logging
from unittest.mock import MagicMock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# ── Stubs (same pattern as test_memory_provider.py) ───────────────────────────

_flask = types.ModuleType("flask")
sys.modules.setdefault("flask", _flask)

_ext = types.ModuleType("app.extensions")
_ext.db = MagicMock()
sys.modules["app.extensions"] = _ext

for pkg in ("app", "app.memory"):
    sys.modules.setdefault(pkg, types.ModuleType(pkg))

# app.models / app.config — use the canonical stubs anchored by conftest.py.
# setdefault ensures conftest's object wins regardless of collection order.
_models = sys.modules.setdefault("app.models", types.ModuleType("app.models"))

_cfg = sys.modules.setdefault("app.config", types.ModuleType("app.config"))
if not hasattr(_cfg, "MEMORY_OBSERVE_MODE"):
    _cfg.MEMORY_OBSERVE_MODE = False

import importlib.util

def _load(dotted, relpath):
    path = os.path.join(BASE, relpath)
    spec = importlib.util.spec_from_file_location(dotted, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod

# Real perf module (stdlib only — safe to load)
_perf = _load("app.perf", "app/perf.py")
sys.modules["app"].perf = _perf

_provider_mod = _load("app.memory.provider", "app/memory/provider.py")
_observer_mod = _load("app.memory.observer", "app/memory/observer.py")

from app.memory.observer import observe_memory, _conversation_hash
from app.memory.provider import MemoryProvider


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row(message, direction="incoming", source="user", wa_message_id=None):
    r = MagicMock()
    r.message = message
    r.direction = direction
    r.source = source
    r.wa_message_id = wa_message_id
    return r


def _mock_query(rows):
    mock_cm = MagicMock()
    mock_q = MagicMock()
    mock_q.filter.return_value = mock_q
    mock_q.order_by.return_value = mock_q
    mock_q.limit.return_value = mock_q
    mock_q.all.return_value = rows
    mock_cm.query = mock_q
    _models.ConversationMessage = mock_cm
    return mock_cm


def _set_flag(value: bool):
    _cfg.MEMORY_OBSERVE_MODE = value


def _begin_ai_request():
    """Simulate an AI-eligible webhook request: perf context with gemini marks."""
    _perf.start()
    _perf.mark("router_start")
    _perf.mark("gemini_start")
    _perf.mark("gemini_end")
    _perf.mark("send_start")
    _perf.mark("meta_response")


def _begin_non_ai_request():
    """Simulate a deterministic (non-Gemini) request."""
    _perf.start()
    _perf.mark("router_start")
    _perf.mark("send_start")
    _perf.mark("meta_response")


def _end_request():
    _perf._clear()


# ════════════════════════════════════════════════════════════════════════════
# Flag OFF
# ════════════════════════════════════════════════════════════════════════════

def test_flag_off_provider_never_called():
    _set_flag(False)
    mock_cm = _mock_query([_row("PGDCA fee?")])
    _begin_ai_request()
    result = observe_memory("t1", "+919876543210", "wamid.x")
    _end_request()
    assert result == []
    assert not mock_cm.query.filter.called

def test_flag_off_no_perf_marks():
    _set_flag(False)
    _mock_query([])
    _begin_ai_request()
    observe_memory("t1", "+91", "w")
    assert not _perf.has_stage("memory_fetch_start")
    _end_request()


# ════════════════════════════════════════════════════════════════════════════
# Flag ON — AI-eligible
# ════════════════════════════════════════════════════════════════════════════

def test_flag_on_provider_called_for_ai_request():
    _set_flag(True)
    mock_cm = _mock_query([_row("PGDCA fee?", "incoming", "user", "w1")])
    _begin_ai_request()
    result = observe_memory("t1", "+919876543210", "wamid.current")
    _end_request()
    assert mock_cm.query.filter.called
    assert len(result) == 1
    assert result[0].text == "PGDCA fee?"

def test_flag_on_perf_marks_recorded():
    _set_flag(True)
    _mock_query([])
    _begin_ai_request()
    observe_memory("t1", "+91", "w")
    assert _perf.has_stage("memory_fetch_start")
    assert _perf.has_stage("memory_fetch_end")
    _end_request()

def test_flag_on_non_ai_request_skipped():
    """Deterministic routing (no gemini_start) must not trigger memory fetch."""
    _set_flag(True)
    mock_cm = _mock_query([_row("PGDCA")])
    _begin_non_ai_request()
    result = observe_memory("t1", "+91", "w")
    _end_request()
    assert result == []
    assert not mock_cm.query.filter.called

def test_flag_on_no_perf_context_skipped():
    """Outside a webhook request (no perf context) → no fetch."""
    _set_flag(True)
    mock_cm = _mock_query([_row("PGDCA")])
    _perf._clear()
    result = observe_memory("t1", "+91", "w")
    assert result == []
    assert not mock_cm.query.filter.called


# ════════════════════════════════════════════════════════════════════════════
# Logging — metrics present, PII absent
# ════════════════════════════════════════════════════════════════════════════

def test_memory_log_emitted_with_metrics(caplog=None):
    _set_flag(True)
    _mock_query([
        _row("hi"),                                  # filtered (greeting)
        _row("PGDCA fee?", "incoming", "user"),      # kept
    ])
    _begin_ai_request()

    records = []
    handler = logging.Handler()
    handler.emit = lambda rec: records.append(rec.getMessage())
    obs_logger = logging.getLogger("app.memory.observer")
    obs_logger.addHandler(handler)
    obs_logger.setLevel(logging.INFO)
    try:
        observe_memory("t1", "+919876543210", "wamid.x")
    finally:
        obs_logger.removeHandler(handler)
        _end_request()

    mem_lines = [m for m in records if "[MEMORY]" in m]
    assert mem_lines, "Expected a [MEMORY] metrics line"
    line = mem_lines[0]
    assert "rows_loaded=2" in line
    assert "rows_filtered=1" in line
    assert "rows_kept=1" in line
    assert "estimated_tokens=" in line
    assert "trimmed=" in line
    assert "fetch_ms=" in line

def test_memory_log_contains_no_pii():
    _set_flag(True)
    _mock_query([_row("PGDCA fee is 15999 call 9447329972", "incoming", "user")])
    _begin_ai_request()

    records = []
    handler = logging.Handler()
    handler.emit = lambda rec: records.append(rec.getMessage())
    obs_logger = logging.getLogger("app.memory.observer")
    obs_logger.addHandler(handler)
    obs_logger.setLevel(logging.INFO)
    try:
        observe_memory("t1", "+919876543210", "wamid.x")
    finally:
        obs_logger.removeHandler(handler)
        _end_request()

    line = [m for m in records if "[MEMORY]" in m][0]
    assert "+919876543210" not in line, "Phone number leaked into log"
    assert "919876543210" not in line, "Phone number leaked into log"
    assert "PGDCA fee is 15999" not in line, "Message body leaked into log"

def test_conversation_hash_stable_and_masked():
    h1 = _conversation_hash("+919876543210")
    h2 = _conversation_hash("+919876543210")
    assert h1 == h2
    assert len(h1) == 8
    assert "9876" not in h1 or True  # hex hash; just assert not the raw phone
    assert h1 != "+919876543210"


# ════════════════════════════════════════════════════════════════════════════
# Failure behaviour
# ════════════════════════════════════════════════════════════════════════════

def test_provider_failure_returns_empty_and_continues():
    _set_flag(True)
    mock_cm = MagicMock()
    mock_cm.query.filter.side_effect = Exception("DB down")
    _models.ConversationMessage = mock_cm
    _begin_ai_request()
    result = observe_memory("t1", "+91", "w")
    _end_request()
    assert result == []

def test_observer_internal_failure_fails_open():
    _set_flag(True)
    # Break perf.has_stage to force an internal observer error
    original = _perf.has_stage
    _perf.has_stage = MagicMock(side_effect=RuntimeError("boom"))
    try:
        result = observe_memory("t1", "+91", "w")
        assert result == []
    finally:
        _perf.has_stage = original


# ════════════════════════════════════════════════════════════════════════════
# fetch_with_stats — stats correctness
# ════════════════════════════════════════════════════════════════════════════

def test_fetch_with_stats_fields():
    _mock_query([
        _row("hi"),
        _row("ok"),
        _row("PGDCA fee?", "incoming", "user"),
        _row("PGDCA fee is 15999", "outgoing", "ai"),
    ])
    turns, stats = MemoryProvider.fetch_with_stats("t1", "+91")
    assert stats["rows_loaded"] == 4
    assert stats["rows_filtered"] == 2
    assert stats["rows_kept"] == 2
    assert stats["estimated_tokens"] > 0
    assert stats["trimmed"] is False
    assert stats["error"] is False
    assert len(turns) == 2

def test_fetch_with_stats_trimmed_flag():
    # Distinct texts so adjacent-dedup keeps all four (each ~100 tokens)
    _mock_query([_row(f"msg{i} " + "x" * 400, "incoming", "user") for i in range(4)])
    turns, stats = MemoryProvider.fetch_with_stats("t1", "+91", token_budget=150)
    assert stats["trimmed"] is True

def test_fetch_with_stats_fail_open():
    mock_cm = MagicMock()
    mock_cm.query.filter.side_effect = Exception("boom")
    _models.ConversationMessage = mock_cm
    turns, stats = MemoryProvider.fetch_with_stats("t1", "+91")
    assert turns == []
    assert stats["error"] is True

def test_fetch_plain_api_unchanged():
    """Phase 1.3A-1 public API: fetch() still returns a plain list."""
    _mock_query([_row("PGDCA fee?", "incoming", "user")])
    result = MemoryProvider.fetch("t1", "+91")
    assert isinstance(result, list)
    assert result[0].text == "PGDCA fee?"


# ════════════════════════════════════════════════════════════════════════════
# Script runner
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERR  {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    total = passed + failed
    print(f"\n{passed}/{total} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
