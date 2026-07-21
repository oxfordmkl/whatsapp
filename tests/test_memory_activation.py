"""
Phase 1.3B Tests — Context Assembler + Memory Activation

Updated from Phase 1.3A-3 to reflect the 1.3B refactor:
  _build_memory_context() in router.py → _assembler().assemble() in app/context/assembler.py

Covers:
  - _assembler().assemble() — unit tests (replaces _build_memory_context tests)
  - MEMORY_ACTIVATE=false → '' returned, no DB access
  - MEMORY_ACTIVATE=true  → memory fetched and formatted as "Chat history:" block
  - course_context combined correctly (memory + double-newline + course card)
  - Memory injected into Gemini context for general fallback path
  - Memory injected alongside course card for course-question path
  - The five reference conversations resolve to Gemini (with history context)
  - Deterministic paths (certificate, static placement exact-match) unchanged
  - Fail-open: MemoryProvider error → '' → Gemini called with empty/course-only context
  - wa_message_id correctly threaded from smart_reply through assembler to MemoryProvider
  - Existing Phase 1.1 behaviour preserved (bare keywords still return static card)
  - Identical output before/after refactor (context string format unchanged)

Run:
  pytest tests/test_memory_activation.py -v
  python tests/test_memory_activation.py
"""
import sys
import os
import types
import traceback
import threading
from unittest.mock import MagicMock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# ── Stubs ─────────────────────────────────────────────────────────────────────

_flask = types.ModuleType("flask")
_flask.current_app = MagicMock()
_flask.current_app._get_current_object = MagicMock(return_value=MagicMock())
_flask.Blueprint = MagicMock(return_value=MagicMock())
_flask.jsonify = lambda d: d
sys.modules["flask"] = _flask

_google = types.ModuleType("google")
_genai = types.ModuleType("google.genai")
_genai.Client = MagicMock
_google.genai = _genai
sys.modules["google"] = _google
sys.modules["google.genai"] = _genai

for _pkg in ("app", "app.bot", "app.services", "app.memory", "app.context"):
    sys.modules.setdefault(_pkg, types.ModuleType(_pkg))

# Merge into existing app.config rather than replacing it — so test_memory_observe.py's
# _cfg variable (captured at its import time) stays the live object in sys.modules.
_cfg = sys.modules.get("app.config")
if _cfg is None:
    _cfg = types.ModuleType("app.config")
    sys.modules["app.config"] = _cfg
_cfg.GEMINI_API_KEY = "test-key"
_cfg.GEMINI_MODEL = "gemini-2.5-flash"
_cfg.SHEETS_ID = ""
_cfg.GOOGLE_CREDENTIALS_JSON = "{}"
if not hasattr(_cfg, "MEMORY_ACTIVATE"):
    _cfg.MEMORY_ACTIVATE = False
if not hasattr(_cfg, "MEMORY_OBSERVE_MODE"):
    _cfg.MEMORY_OBSERVE_MODE = False

_ai = types.ModuleType("app.services.ai_service")
_gemini_reply = MagicMock(return_value=None)
_smart_fallback = MagicMock(return_value=("Fallback text", "COURSE"))
_ai.gemini_reply = _gemini_reply
_ai.smart_fallback = _smart_fallback
_ai.gemini_client = MagicMock()
sys.modules["app.services.ai_service"] = _ai

_crm = types.ModuleType("app.services.crm_service")
_crm.update_lead_status = MagicMock()
sys.modules["app.services.crm_service"] = _crm

_log = types.ModuleType("app.services.log_service")
_log.log_lead_event_in_thread = MagicMock()
_log.resolve_tenant_id = MagicMock(return_value="test-tenant")
sys.modules["app.services.log_service"] = _log

_prompts = types.ModuleType("app.bot.prompts")
_prompts.AALIZA_PROMPT = "You are Oxford Nova."
sys.modules["app.bot.prompts"] = _prompts

_ext = types.ModuleType("app.extensions")
_ext.db = MagicMock()
sys.modules["app.extensions"] = _ext

sys.modules.setdefault("app.models", types.ModuleType("app.models"))

import importlib.util

def _load(dotted, relpath):
    path = os.path.join(BASE, relpath)
    spec = importlib.util.spec_from_file_location(dotted, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod

_load("app.bot.constants", "app/bot/constants.py")
_load("app.bot.objections", "app/bot/objections.py")

_state_mod = types.ModuleType("app.state")
_state_mod.count_states = lambda: 0
_state_mod.count_pending_followups = lambda: 0
_state_mod.get_or_create_state = MagicMock(return_value={})
sys.modules["app.state"] = _state_mod

_provider_mod = _load("app.memory.provider", "app/memory/provider.py")
_assembler_mod = _load("app.context.assembler", "app/context/assembler.py")
_load("app.bot.router", "app/bot/router.py")

from app.bot.router import smart_reply
from app.bot.constants import ALL_COURSES
from app.memory.provider import Turn
from app.context.assembler import ContextAssembler


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_state(stage="course_viewed", course="PGDCA"):
    return {
        "stage": stage, "course": course,
        "last_msg": "", "last_text": "", "batch_time": "", "goal": "job",
    }


def _call(msg, stage="course_viewed", course="PGDCA", is_new=False, wamid=None):
    _state_mod.get_or_create_state.return_value = _make_state(stage, course)
    _gemini_reply.reset_mock()
    return smart_reply(msg, "Tester", "9900000000", is_new,
                       tenant_id="test-tenant", wa_message_id=wamid)


def _set_activate(value: bool):
    sys.modules["app.config"].MEMORY_ACTIVATE = value


def _mock_memory(turns):
    """Stub MemoryProvider.fetch to return given Turn list."""
    mock_provider = MagicMock()
    mock_provider.fetch.return_value = turns
    sys.modules["app.memory.provider"].MemoryProvider = mock_provider
    return mock_provider


def _assembler():
    return sys.modules["app.context.assembler"].ContextAssembler


# ════════════════════════════════════════════════════════════════════════════
# ContextAssembler.assemble — unit tests
# (formerly _build_memory_context tests — identical semantics, new home)
# ════════════════════════════════════════════════════════════════════════════

def test_assemble_flag_off_returns_empty():
    _set_activate(False)
    result = _assembler().assemble("test-tenant", "9900000000", "wamid.x")
    assert result == ""

def test_assemble_flag_off_no_db_access():
    _set_activate(False)
    mock = _mock_memory([Turn("user", "PGDCA fee?")])
    _assembler().assemble("test-tenant", "9900000000", "wamid.x")
    assert not mock.fetch.called

def test_assemble_flag_on_formats_turns():
    _set_activate(True)
    _mock_memory([
        Turn("user", "PGDCA"),
        Turn("assistant", "PGDCA details: fee 15999"),
    ])
    result = _assembler().assemble("test-tenant", "9900000000", "wamid.x")
    assert "Chat history:" in result
    assert "user: PGDCA" in result
    assert "assistant: PGDCA details: fee 15999" in result

def test_assemble_empty_turns_returns_empty():
    _set_activate(True)
    _mock_memory([])
    result = _assembler().assemble("test-tenant", "9900000000", None)
    assert result == ""

def test_assemble_fail_open_on_provider_error():
    _set_activate(True)
    mock = MagicMock()
    mock.fetch.side_effect = Exception("DB down")
    sys.modules["app.memory.provider"].MemoryProvider = mock
    result = _assembler().assemble("test-tenant", "9900000000", None)
    assert result == ""

def test_assemble_passes_wa_message_id():
    _set_activate(True)
    mock = _mock_memory([])
    _assembler().assemble("test-tenant", "9900000000", "wamid.abc")
    mock.fetch.assert_called_once()
    assert mock.fetch.call_args.kwargs.get("exclude_message_id") == "wamid.abc"

def test_assemble_passes_tenant_id():
    _set_activate(True)
    mock = _mock_memory([])
    _assembler().assemble("tenant-XYZ", "9900000000", None)
    mock.fetch.assert_called_once()
    assert mock.fetch.call_args.kwargs.get("tenant_id") == "tenant-XYZ"

def test_assemble_with_course_context_no_memory():
    """When memory is off/empty, course_context is returned unchanged."""
    _set_activate(False)
    result = _assembler().assemble(
        "test-tenant", "9900000000", None,
        course_context="Course details:\nPGDCA fee 15999"
    )
    assert result == "Course details:\nPGDCA fee 15999"

def test_assemble_with_course_context_and_memory():
    """Memory + course_context → memory block, double newline, course block."""
    _set_activate(True)
    _mock_memory([Turn("user", "PGDCA"), Turn("assistant", "Great choice!")])
    result = _assembler().assemble(
        "test-tenant", "9900000000", None,
        course_context="Course details:\nPGDCA fee 15999"
    )
    assert "Chat history:" in result
    assert "Course details:" in result
    assert result.index("Chat history:") < result.index("Course details:")
    assert "\n\n" in result  # double-newline separator

def test_assemble_memory_only_no_course_context():
    """Memory with no course_context → memory block only."""
    _set_activate(True)
    _mock_memory([Turn("user", "duration?"), Turn("assistant", "12 months")])
    result = _assembler().assemble("test-tenant", "9900000000", None)
    assert "Chat history:" in result
    assert "Course details:" not in result

def test_assemble_both_empty():
    """Flag off and no course_context → empty string."""
    _set_activate(False)
    result = _assembler().assemble("test-tenant", "9900000000", None)
    assert result == ""

def test_assemble_course_context_only_when_memory_empty():
    """Flag on but no turns → falls back to course_context alone."""
    _set_activate(True)
    _mock_memory([])
    result = _assembler().assemble(
        "test-tenant", "9900000000", None,
        course_context="Course details:\nPGDCA"
    )
    assert result == "Course details:\nPGDCA"


# ════════════════════════════════════════════════════════════════════════════
# Output format parity — identical to Phase 1.3A-3 _build_memory_context
# ════════════════════════════════════════════════════════════════════════════

def test_format_parity_two_turns():
    """Chat history format matches the Phase 1.3A-3 specification exactly."""
    _set_activate(True)
    _mock_memory([
        Turn("user", "PGDCA"),
        Turn("assistant", "✅ PGDCA — fee ₹15,999. Duration 12 months."),
    ])
    result = _assembler().assemble("t", "+91", None)
    expected = (
        "Chat history:\n"
        "user: PGDCA\n"
        "assistant: ✅ PGDCA — fee ₹15,999. Duration 12 months."
    )
    assert result == expected

def test_format_parity_with_course_context():
    """Combined format matches the Phase 1.3A-3 inline assembly exactly."""
    _set_activate(True)
    _mock_memory([Turn("user", "PGDCA")])
    course_ctx = "Course details:\nPGDCA: 12 months"
    result = _assembler().assemble("t", "+91", None, course_context=course_ctx)
    expected = "Chat history:\nuser: PGDCA\n\nCourse details:\nPGDCA: 12 months"
    assert result == expected


# ════════════════════════════════════════════════════════════════════════════
# Five reference conversations — router integration
# ════════════════════════════════════════════════════════════════════════════

def _setup_reference(prior_turns):
    _set_activate(True)
    _mock_memory(prior_turns)
    _gemini_reply.return_value = "Contextual answer about PGDCA"


def _pgdca_history():
    return [
        Turn("user", "PGDCA"),
        Turn("assistant", "✅ PGDCA — Post Graduate Diploma. Fee: ₹15,999. Duration: 12 months."),
    ]


def test_ref1_answer_detail_ayi_parayu():
    _setup_reference(_pgdca_history())
    text, preset = _call("answer detail ayi parayu")
    _gemini_reply.assert_called_once()
    _, kwargs = _gemini_reply.call_args
    ctx = kwargs.get("context", "")
    assert "Chat history:" in ctx
    assert "PGDCA" in ctx
    assert text == "Contextual answer about PGDCA"

def test_ref2_athinte_fee():
    _setup_reference(_pgdca_history())
    _call("athinte fee?")
    _gemini_reply.assert_called_once()
    _, kwargs = _gemini_reply.call_args
    assert "Chat history:" in kwargs.get("context", "")

def test_ref3_placement_undo():
    _setup_reference(_pgdca_history())
    _call("placement undo?")
    _gemini_reply.assert_called_once()
    _, kwargs = _gemini_reply.call_args
    assert "Chat history:" in kwargs.get("context", "")

def test_ref4_duration():
    _setup_reference(_pgdca_history())
    _call("duration?")
    _gemini_reply.assert_called_once()
    _, kwargs = _gemini_reply.call_args
    assert "Chat history:" in kwargs.get("context", "")

def test_ref5_certificate_undo_deterministic():
    """'certificate undo?' → deterministic static handler. No assembler call."""
    _set_activate(True)
    mock = _mock_memory(_pgdca_history())
    _gemini_reply.reset_mock()
    text, _ = _call("certificate undo?")
    _gemini_reply.assert_not_called()
    assert "certificate" in text.lower() or "certified" in text.lower()
    assert not mock.fetch.called


# ════════════════════════════════════════════════════════════════════════════
# Router integration — context passes correctly to Gemini
# ════════════════════════════════════════════════════════════════════════════

def test_general_fallback_passes_assembled_context():
    _set_activate(True)
    _mock_memory([Turn("user", "PGDCA"), Turn("assistant", "Here are PGDCA details")])
    _gemini_reply.return_value = "AI answer"
    _call("when are classes starting?")
    _, kwargs = _gemini_reply.call_args
    assert "context" in kwargs
    assert "Chat history:" in kwargs["context"]

def test_course_question_memory_prepended_to_course_card():
    _set_activate(True)
    _mock_memory([Turn("user", "pgdca"), Turn("assistant", "Great choice!")])
    _gemini_reply.return_value = "Fee is 15999"
    _call("how much is pgdca fee?")
    _, kwargs = _gemini_reply.call_args
    ctx = kwargs.get("context", "")
    assert "Chat history:" in ctx
    assert "Course details:" in ctx
    assert ctx.index("Chat history:") < ctx.index("Course details:")
    assert "\n\n" in ctx

def test_flag_off_gemini_receives_course_card_only():
    """Flag off + course question: Gemini gets course card with no history block."""
    _set_activate(False)
    _gemini_reply.return_value = "Fee is 15999"
    _call("how much is pgdca fee?")
    _, kwargs = _gemini_reply.call_args
    ctx = kwargs.get("context", "")
    assert "Course details:" in ctx
    assert "Chat history:" not in ctx

def test_flag_off_general_fallback_empty_context():
    """Flag off + general fallback: Gemini receives empty context (Phase 1.2B baseline)."""
    _set_activate(False)
    _gemini_reply.return_value = "AI answer"
    _call("when are classes starting?")
    _, kwargs = _gemini_reply.call_args
    assert kwargs.get("context", "") == ""


# ════════════════════════════════════════════════════════════════════════════
# Deterministic paths — assembler never called
# ════════════════════════════════════════════════════════════════════════════

def test_bare_keyword_no_assembler_call():
    _set_activate(True)
    mock = _mock_memory(_pgdca_history())
    text, preset = _call("pgdca")
    _gemini_reply.assert_not_called()
    assert "PGDCA" in text
    assert not mock.fetch.called

def test_fees_exact_no_assembler_call():
    _set_activate(True)
    mock = _mock_memory(_pgdca_history())
    _call("fees", course="PGDCA")
    _gemini_reply.assert_not_called()
    assert not mock.fetch.called

def test_objection_no_assembler_call():
    _set_activate(True)
    mock = _mock_memory(_pgdca_history())
    _call("too expensive for me")
    _gemini_reply.assert_not_called()
    assert not mock.fetch.called

def test_greeting_no_assembler_call():
    _set_activate(True)
    mock = _mock_memory([])
    _state_mod.get_or_create_state.return_value = _make_state("new", "")
    _gemini_reply.reset_mock()
    smart_reply("hi", "NewLead", "9811111111", True,
                tenant_id="test-tenant", wa_message_id="wamid.x")
    _gemini_reply.assert_not_called()
    assert not mock.fetch.called


# ════════════════════════════════════════════════════════════════════════════
# Fail-open scenarios
# ════════════════════════════════════════════════════════════════════════════

def test_provider_error_gemini_still_called_empty_context():
    _set_activate(True)
    mock = MagicMock()
    mock.fetch.side_effect = RuntimeError("timeout")
    sys.modules["app.memory.provider"].MemoryProvider = mock
    _gemini_reply.return_value = "AI answer without memory"
    text, preset = _call("duration?")
    _gemini_reply.assert_called_once()
    _, kwargs = _gemini_reply.call_args
    assert kwargs.get("context", "") == ""
    assert text == "AI answer without memory"

def test_provider_error_course_question_still_gets_course_card():
    """Even if memory fails, course-question Gemini call still gets the course card."""
    _set_activate(True)
    mock = MagicMock()
    mock.fetch.side_effect = RuntimeError("timeout")
    sys.modules["app.memory.provider"].MemoryProvider = mock
    _gemini_reply.return_value = "Fee is 15999"
    _call("how much is pgdca fee?")
    _, kwargs = _gemini_reply.call_args
    ctx = kwargs.get("context", "")
    assert "Course details:" in ctx
    assert "Chat history:" not in ctx

def test_gemini_failure_with_memory_falls_back():
    _set_activate(True)
    _mock_memory(_pgdca_history())
    _gemini_reply.return_value = None
    _smart_fallback.return_value = "Fallback reply"
    text, preset = _call("duration?")
    assert text == "Fallback reply"
    assert preset == "COURSE"


# ════════════════════════════════════════════════════════════════════════════
# wa_message_id threading
# ════════════════════════════════════════════════════════════════════════════

def test_wa_message_id_passed_to_provider():
    _set_activate(True)
    mock = _mock_memory([])
    _gemini_reply.return_value = "AI answer"
    smart_reply("duration?", "Tester", "9900000000", False,
                tenant_id="test-tenant", wa_message_id="wamid.TARGET")
    mock.fetch.assert_called_once()
    assert mock.fetch.call_args.kwargs.get("exclude_message_id") == "wamid.TARGET"

def test_smart_reply_accepts_no_wa_message_id():
    _set_activate(False)
    _state_mod.get_or_create_state.return_value = _make_state()
    _gemini_reply.return_value = "AI answer"
    result = smart_reply("duration?", "Tester", "9900000000", False,
                          tenant_id="test-tenant")
    assert result is not None

def test_assembler_no_wa_message_id_defaults_none():
    """wa_message_id=None default: exclude_message_id=None passed to provider."""
    _set_activate(True)
    mock = _mock_memory([])
    _assembler().assemble("t", "+91")
    mock.fetch.assert_called_once()
    assert mock.fetch.call_args.kwargs.get("exclude_message_id") is None


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
