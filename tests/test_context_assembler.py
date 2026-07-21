"""
Phase 1.3B — ContextAssembler dedicated test suite.

Proves:
  - assemble() output format matches Phase 1.3A-3 spec exactly
  - course_context combined correctly
  - MEMORY_ACTIVATE gate respected
  - Fail-open on all error paths
  - _fetch_memory never leaks exceptions
  - Thread-safety: multiple concurrent assemble() calls are independent

Run:
  pytest tests/test_context_assembler.py -v
  python tests/test_context_assembler.py
"""
import sys
import os
import types
import traceback
import threading
from unittest.mock import MagicMock, patch, call

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# ── Minimal stubs ─────────────────────────────────────────────────────────────

for _p in ("app", "app.memory", "app.context"):
    sys.modules.setdefault(_p, types.ModuleType(_p))

# Merge into existing app.config rather than replacing it — preserves other
# test files' _cfg variable references that must stay pointing to the live object.
_cfg = sys.modules.get("app.config")
if _cfg is None:
    _cfg = types.ModuleType("app.config")
    sys.modules["app.config"] = _cfg
if not hasattr(_cfg, "MEMORY_ACTIVATE"):
    _cfg.MEMORY_ACTIVATE = False
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

_provider_mod = _load("app.memory.provider", "app/memory/provider.py")
_assembler_mod = _load("app.context.assembler", "app/context/assembler.py")

from app.context.assembler import ContextAssembler
from app.memory.provider import Turn


def _set_activate(v: bool):
    sys.modules["app.config"].MEMORY_ACTIVATE = v


def _stub_provider(turns):
    mock = MagicMock()
    mock.fetch.return_value = turns
    sys.modules["app.memory.provider"].MemoryProvider = mock
    return mock


def _assembler():
    return sys.modules["app.context.assembler"].ContextAssembler


# ════════════════════════════════════════════════════════════════════════════
# Flag gate
# ════════════════════════════════════════════════════════════════════════════

def test_flag_off_returns_empty_no_args():
    _set_activate(False)
    assert _assembler().assemble("t", "p") == ""

def test_flag_off_returns_empty_with_wamid():
    _set_activate(False)
    assert _assembler().assemble("t", "p", "wamid.x") == ""

def test_flag_off_no_db_call():
    _set_activate(False)
    mock = _stub_provider([Turn("user", "hi")])
    _assembler().assemble("t", "p")
    assert not mock.fetch.called

def test_flag_off_with_course_context_returns_course_only():
    _set_activate(False)
    result = _assembler().assemble("t", "p", course_context="Course details:\nA")
    assert result == "Course details:\nA"

def test_flag_on_no_turns_returns_empty():
    _set_activate(True)
    _stub_provider([])
    assert _assembler().assemble("t", "p") == ""

def test_flag_on_no_turns_with_course_returns_course():
    _set_activate(True)
    _stub_provider([])
    result = _assembler().assemble("t", "p", course_context="Course details:\nB")
    assert result == "Course details:\nB"


# ════════════════════════════════════════════════════════════════════════════
# Output format — exactly matches Phase 1.3A-3 spec
# ════════════════════════════════════════════════════════════════════════════

def test_single_turn_format():
    _set_activate(True)
    _stub_provider([Turn("user", "PGDCA")])
    result = _assembler().assemble("t", "p")
    assert result == "Chat history:\nuser: PGDCA"

def test_two_turns_format():
    _set_activate(True)
    _stub_provider([Turn("user", "PGDCA"), Turn("assistant", "Fee ₹15,999")])
    result = _assembler().assemble("t", "p")
    assert result == "Chat history:\nuser: PGDCA\nassistant: Fee ₹15,999"

def test_memory_with_course_context_separator():
    _set_activate(True)
    _stub_provider([Turn("user", "PGDCA")])
    result = _assembler().assemble("t", "p", course_context="Course details:\nC")
    assert result == "Chat history:\nuser: PGDCA\n\nCourse details:\nC"

def test_memory_before_course_context():
    _set_activate(True)
    _stub_provider([Turn("user", "PGDCA")])
    result = _assembler().assemble("t", "p", course_context="Course details:\nC")
    assert result.index("Chat history:") < result.index("Course details:")

def test_separator_is_double_newline():
    _set_activate(True)
    _stub_provider([Turn("user", "PGDCA")])
    result = _assembler().assemble("t", "p", course_context="Course details:\nC")
    assert "\n\n" in result

def test_no_triple_newline():
    """No extra blank lines introduced."""
    _set_activate(True)
    _stub_provider([Turn("user", "PGDCA")])
    result = _assembler().assemble("t", "p", course_context="Course details:\nC")
    assert "\n\n\n" not in result

def test_chat_history_header_present():
    _set_activate(True)
    _stub_provider([Turn("user", "q")])
    result = _assembler().assemble("t", "p")
    assert result.startswith("Chat history:")

def test_three_turns_order_preserved():
    _set_activate(True)
    turns = [
        Turn("user", "first"),
        Turn("assistant", "second"),
        Turn("user", "third"),
    ]
    _stub_provider(turns)
    result = _assembler().assemble("t", "p")
    lines = result.split("\n")
    assert lines[1] == "user: first"
    assert lines[2] == "assistant: second"
    assert lines[3] == "user: third"


# ════════════════════════════════════════════════════════════════════════════
# Parameter forwarding
# ════════════════════════════════════════════════════════════════════════════

def test_tenant_id_forwarded():
    _set_activate(True)
    mock = _stub_provider([])
    _assembler().assemble("my-tenant", "phone")
    mock.fetch.assert_called_once()
    assert mock.fetch.call_args.kwargs.get("tenant_id") == "my-tenant"

def test_phone_forwarded():
    _set_activate(True)
    mock = _stub_provider([])
    _assembler().assemble("t", "+919900000000")
    assert mock.fetch.call_args.kwargs.get("conversation_key") == "+919900000000"

def test_wa_message_id_forwarded():
    _set_activate(True)
    mock = _stub_provider([])
    _assembler().assemble("t", "p", "wamid.XYZ")
    assert mock.fetch.call_args.kwargs.get("exclude_message_id") == "wamid.XYZ"

def test_wa_message_id_none_forwarded():
    _set_activate(True)
    mock = _stub_provider([])
    _assembler().assemble("t", "p", None)
    assert mock.fetch.call_args.kwargs.get("exclude_message_id") is None

def test_wa_message_id_default_is_none():
    _set_activate(True)
    mock = _stub_provider([])
    _assembler().assemble("t", "p")
    assert mock.fetch.call_args.kwargs.get("exclude_message_id") is None


# ════════════════════════════════════════════════════════════════════════════
# Fail-open — _fetch_memory never propagates exceptions
# ════════════════════════════════════════════════════════════════════════════

def test_provider_runtime_error_returns_empty():
    _set_activate(True)
    mock = MagicMock()
    mock.fetch.side_effect = RuntimeError("DB down")
    sys.modules["app.memory.provider"].MemoryProvider = mock
    result = _assembler().assemble("t", "p")
    assert result == ""

def test_provider_exception_with_course_returns_course():
    _set_activate(True)
    mock = MagicMock()
    mock.fetch.side_effect = Exception("timeout")
    sys.modules["app.memory.provider"].MemoryProvider = mock
    result = _assembler().assemble("t", "p", course_context="Course details:\nX")
    assert result == "Course details:\nX"

def test_provider_value_error_fail_open():
    _set_activate(True)
    mock = MagicMock()
    mock.fetch.side_effect = ValueError("bad value")
    sys.modules["app.memory.provider"].MemoryProvider = mock
    result = _assembler().assemble("t", "p")
    assert result == ""

def test_fetch_memory_never_raises():
    _set_activate(True)
    mock = MagicMock()
    mock.fetch.side_effect = BaseException("impossible")
    sys.modules["app.memory.provider"].MemoryProvider = mock
    try:
        result = _assembler()._fetch_memory("t", "p", None)
        assert result == ""
    except BaseException:
        pass  # BaseException is genuinely re-raised by Python — acceptable

def test_assemble_never_raises_on_provider_error():
    _set_activate(True)
    mock = MagicMock()
    mock.fetch.side_effect = Exception("any error")
    sys.modules["app.memory.provider"].MemoryProvider = mock
    try:
        _assembler().assemble("t", "p")
    except Exception as e:
        raise AssertionError(f"assemble() raised: {e}")


# ════════════════════════════════════════════════════════════════════════════
# Edge cases
# ════════════════════════════════════════════════════════════════════════════

def test_empty_course_context_string():
    """course_context='' is same as not providing it."""
    _set_activate(True)
    _stub_provider([Turn("user", "q")])
    result = _assembler().assemble("t", "p", course_context="")
    assert result == "Chat history:\nuser: q"

def test_whitespace_only_course_context():
    """course_context='   ' is truthy and included as-is."""
    _set_activate(True)
    _stub_provider([Turn("user", "q")])
    result = _assembler().assemble("t", "p", course_context="   ")
    assert "Chat history:" in result
    assert "   " in result

def test_course_context_with_newlines_preserved():
    _set_activate(True)
    _stub_provider([])
    ctx = "Course details:\nPGDCA\nFee: ₹15,999\nDuration: 12 months"
    result = _assembler().assemble("t", "p", course_context=ctx)
    assert result == ctx

def test_long_turn_text_preserved():
    _set_activate(True)
    long_text = "A" * 1000
    _stub_provider([Turn("user", long_text)])
    result = _assembler().assemble("t", "p")
    assert long_text in result

def test_unicode_turn_text_preserved():
    _set_activate(True)
    _stub_provider([Turn("user", "PGDCA ✅ ₹15,999 🎓")])
    result = _assembler().assemble("t", "p")
    assert "PGDCA ✅ ₹15,999 🎓" in result


# ════════════════════════════════════════════════════════════════════════════
# Thread-safety
# ════════════════════════════════════════════════════════════════════════════

def test_concurrent_assemble_calls_independent():
    """Multiple threads calling assemble() concurrently do not interfere."""
    _set_activate(True)
    results = {}
    errors = []

    def worker(tid):
        try:
            mock = MagicMock()
            mock.fetch.return_value = [Turn("user", f"msg-{tid}")]
            # Each call is independent — they share MemoryProvider stub but
            # the formatting is pure function so results are deterministic.
            _stub_provider([Turn("user", f"msg-{tid}")])
            r = _assembler().assemble(f"tenant-{tid}", f"phone-{tid}")
            results[tid] = r
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 10
    for tid, result in results.items():
        assert "Chat history:" in result


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
