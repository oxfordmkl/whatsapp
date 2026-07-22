"""
Phase 1.1 Regression Tests — Intelligent Course Conversation Routing

Covers:
  - _is_question() detection logic
  - detect_objection() hardened fee triggers
  - Bare keyword → static course card (no Gemini)
  - Course + question → Gemini called with course context
  - Gemini failure → fallback to static course card
  - Existing keyword flows (fees, demo, visit, call, greeting, exit)

Run:
  pytest tests/test_routing_phase1.py -v
  python tests/test_routing_phase1.py
"""
import sys
import types
import os
import importlib.util
import traceback
from unittest.mock import MagicMock

# ── Project root (one directory above tests/) ────────────────────────────────
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ════════════════════════════════════════════════════════════════════════════
# Bootstrap: stub all external / Flask dependencies before any app.* import.
# Must happen before any "from app.* import ..." line.
# ════════════════════════════════════════════════════════════════════════════

# Flask stub
_flask = types.ModuleType("flask")
_flask.current_app = MagicMock()
_flask.current_app._get_current_object = MagicMock(return_value=MagicMock())
_flask.Blueprint = MagicMock(return_value=MagicMock())
_flask.jsonify = lambda d: d
sys.modules["flask"] = _flask

# google.genai stub
_google = types.ModuleType("google")
_genai = types.ModuleType("google.genai")
_genai.Client = MagicMock
_google.genai = _genai
sys.modules["google"] = _google
sys.modules["google.genai"] = _genai

# App package skeleton stubs (prevent __init__.py from running)
for _pkg in ("app", "app.bot", "app.services"):
    sys.modules.setdefault(_pkg, types.ModuleType(_pkg))

# app.config
_cfg = types.ModuleType("app.config")
_cfg.GEMINI_API_KEY = "test-key"
_cfg.GEMINI_MODEL = "gemini-2.5-flash"
_cfg.SHEETS_ID = ""
_cfg.GOOGLE_CREDENTIALS_JSON = "{}"
sys.modules["app.config"] = _cfg

# app.services.ai_service — controllable gemini_reply
_ai = types.ModuleType("app.services.ai_service")
_gemini_reply = MagicMock(return_value=None)
_smart_fallback = MagicMock(return_value=("Fallback text", "COURSE"))
_ai.gemini_reply = _gemini_reply
_ai.smart_fallback = _smart_fallback
_ai.gemini_client = MagicMock()
sys.modules["app.services.ai_service"] = _ai

# app.services.crm_service
_crm = types.ModuleType("app.services.crm_service")
_crm.update_lead_status = MagicMock()
sys.modules["app.services.crm_service"] = _crm

# app.services.log_service
_log = types.ModuleType("app.services.log_service")
_log.log_lead_event_in_thread = MagicMock()
_log.resolve_tenant_id = MagicMock(return_value="test-tenant")
sys.modules["app.services.log_service"] = _log

# app.bot.prompts
_prompts = types.ModuleType("app.bot.prompts")
_prompts.AALIZA_PROMPT = "You are Oxford Nova, a helpful counselor."
sys.modules["app.bot.prompts"] = _prompts


def _load(dotted: str, relpath: str):
    """Load a module from a file path, bypassing the package import machinery."""
    path = os.path.join(BASE, relpath)
    spec = importlib.util.spec_from_file_location(dotted, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


# Load real pure modules (no Flask deps)
# Phase 1.6.4: constants.py now sources institute facts from the canonical
# Business Profile, so that module must be registered before constants loads.
_load("app.bot.business_profile", "app/bot/business_profile.py")
_load("app.bot.constants", "app/bot/constants.py")
_load("app.bot.objections", "app/bot/objections.py")
# Phase 1.6.6: router imports the CTA handler layer.
_load("app.bot.cta_handlers", "app/bot/cta_handlers.py")
# Phase 1.6.7: router imports the booking handler layer.
_load("app.bot.navigation", "app/bot/navigation.py")
_load("app.bot.screens", "app/bot/screens.py")
_load("app.bot.booking_handlers", "app/bot/booking_handlers.py")
# Phase 1.6.8: router imports the offer handler layer.
_load("app.bot.offer_handlers", "app/bot/offer_handlers.py")

# app.state — stateful stub with a mutable state dict
_state_mod = types.ModuleType("app.state")
_state_mod.count_states = lambda: 0
_state_mod.count_pending_followups = lambda: 0
_state_mod.get_or_create_state = MagicMock(return_value={})
sys.modules["app.state"] = _state_mod

# Load router last (depends on all stubs above)
_load("app.bot.router", "app/bot/router.py")

# ── Imports now safe ─────────────────────────────────────────────────────────
from app.bot.objections import detect_objection
from app.bot.router import _is_question, smart_reply
from app.bot.constants import ALL_COURSES


# ── Test helpers ─────────────────────────────────────────────────────────────

def _make_state(stage="course_viewed", course="PGDCA"):
    return {
        "stage": stage, "course": course,
        "last_msg": "", "last_text": "", "batch_time": "", "goal": "job",
    }


def _call(msg, stage="course_viewed", course="PGDCA", is_new=False):
    """Call smart_reply with a fresh state dict and reset mocks."""
    _state_mod.get_or_create_state.return_value = _make_state(stage, course)
    _gemini_reply.reset_mock()
    return smart_reply(msg, "Tester", "9900000000", is_new, tenant_id="test-tenant")


# ════════════════════════════════════════════════════════════════════════════
# _is_question — question detection logic
# ════════════════════════════════════════════════════════════════════════════

def test_question_mark_detected():
    assert _is_question("is pgdca fee high?") is True

def test_question_mark_on_bare_word():
    assert _is_question("pgdca?") is True

def test_interrogative_starter_is():
    # The exact message from the audit
    assert _is_question("is it very high fee for pgdca course") is True

def test_interrogative_starter_does():
    assert _is_question("does pgdca have placement") is True

def test_interrogative_starter_how():
    assert _is_question("how much is pgdca fee") is True

def test_interrogative_starter_which():
    assert _is_question("which course is better") is True

def test_interrogative_starter_will():
    assert _is_question("will python help me get a job") is True

def test_long_message_with_fee_signal():
    # 5 tokens, "fee" and "high" both in signal set
    assert _is_question("pgdca course fee high aano") is True

def test_long_message_with_worth_signal():
    assert _is_question("is pgdca worth the money here") is True

def test_bare_single_keyword_not_question():
    assert _is_question("pgdca") is False

def test_bare_two_word_not_question():
    # 2 tokens, no signal, no starter, no ?
    assert _is_question("dca course") is False

def test_short_course_name_not_question():
    # 2 tokens
    assert _is_question("python programming") is False

def test_three_tokens_no_signal_not_question():
    # 3 tokens — boundary: len > 3 requires at least 4
    assert _is_question("pgdca course details") is False

# ── Review refinement: removed "do" and "tell" from _QUESTION_STARTERS ──────

def test_do_dca_is_not_question():
    # "do dca" = intent statement ("I want to do DCA"), not a question
    assert _is_question("do dca") is False

def test_tell_pgdca_is_not_question():
    # "tell pgdca" = imperative command, not an interrogative
    assert _is_question("tell pgdca") is False

def test_i_want_pgdca_is_not_question():
    # 3 tokens, no starter, no signal — should be static
    assert _is_question("i want pgdca") is False

def test_pgdca_admission_is_not_question():
    # 2 tokens, no starter, no signal
    assert _is_question("pgdca admission") is False

def test_is_pgdca_fee_high_no_mark():
    # "is" remains in _QUESTION_STARTERS — Gemini path preserved
    assert _is_question("is pgdca fee high") is True

def test_pgdca_vs_dca_with_mark():
    # "?" present → path (a) catches it regardless of starters
    assert _is_question("pgdca vs dca?") is True


# ════════════════════════════════════════════════════════════════════════════
# detect_objection — hardened fee triggers
# ════════════════════════════════════════════════════════════════════════════

def test_high_fee_reversed_order():
    """The documented bug: 'high fee' (reversed) was not matched before this fix."""
    assert detect_objection("is it very high fee for pgdca course") == "fees_high"

def test_fee_high_original_order():
    assert detect_objection("fee high aano") == "fees_high"

def test_fees_high_plural():
    assert detect_objection("fees high aanu") == "fees_high"

def test_expensive_substring():
    assert detect_objection("too expensive for me") == "fees_high"

def test_cannot_afford():
    assert detect_objection("i cannot afford this course") == "fees_high"

def test_budget_issue():
    assert detect_objection("budget issue und") == "fees_high"

def test_costly():
    assert detect_objection("it is costly") == "fees_high"

def test_other_objections_unchanged():
    assert detect_objection("not interested") == "not_interested"
    assert detect_objection("i am busy") == "time_issue"
    assert detect_objection("already job und") == "already_working"
    assert detect_objection("think") == "think_later"
    assert detect_objection("confused") == "confused"

def test_no_objection_on_neutral():
    assert detect_objection("pgdca course details") is None
    assert detect_objection("hello") is None
    assert detect_objection("demo") is None


# ════════════════════════════════════════════════════════════════════════════
# Routing: bare keyword → static course card (Gemini NOT called)
# ════════════════════════════════════════════════════════════════════════════

def test_bare_pgdca_no_gemini():
    text, preset = _call("pgdca")
    assert "PGDCA" in text
    assert preset == "COURSE"
    _gemini_reply.assert_not_called()

def test_bare_dca_no_gemini():
    text, preset = _call("dca")
    assert "DCA" in text
    assert preset == "COURSE"
    _gemini_reply.assert_not_called()

def test_bare_python_no_gemini():
    text, preset = _call("python")
    assert "Python" in text
    assert preset == "COURSE"
    _gemini_reply.assert_not_called()

def test_bare_sap_no_gemini():
    text, preset = _call("sap")
    assert "SAP" in text
    assert preset == "COURSE"
    _gemini_reply.assert_not_called()

def test_bare_gst_no_gemini():
    text, preset = _call("gst")
    assert "GST" in text
    assert preset == "COURSE"
    _gemini_reply.assert_not_called()


# ════════════════════════════════════════════════════════════════════════════
# Routing: course + question → Gemini called with injected course context
# ════════════════════════════════════════════════════════════════════════════

def test_pgdca_fee_question_calls_gemini():
    # "how much does pgdca cost?" — has "?", reaches KEYWORD_TO_COURSE, triggers Gemini.
    # ("is it very high fee for pgdca course?" is correctly intercepted by detect_objection
    # as fees_high before reaching this branch — see test_high_fee_to_objection_not_gemini.)
    _gemini_reply.return_value = "PGDCA fee is ₹15,999 — excellent value!"
    text, preset = _call("how much does pgdca course cost?")
    _gemini_reply.assert_called_once()
    _, kwargs = _gemini_reply.call_args
    assert "Course details" in kwargs.get("context", ""), "Expected course card in context"
    assert text == "PGDCA fee is ₹15,999 — excellent value!"
    assert preset == "COURSE"

def test_high_fee_to_objection_not_gemini():
    """'is it very high fee for pgdca course?' → detect_objection wins (fees_high),
    not KEYWORD_TO_COURSE. Objection routing is the correct outcome for this message."""
    _gemini_reply.return_value = "AI response"
    text, preset = _call("is it very high fee for pgdca course?")
    # Objection handler fires — Gemini is NOT called (deterministic empathy response)
    _gemini_reply.assert_not_called()
    # Objection response contains value/demo framing, not a raw course card
    assert preset == "FEES"

def test_pgdca_placement_question_calls_gemini():
    _gemini_reply.return_value = "Yes, PGDCA has excellent placement support!"
    text, preset = _call("does pgdca have placement support?")
    _gemini_reply.assert_called_once()
    assert text == "Yes, PGDCA has excellent placement support!"

def test_course_comparison_calls_gemini():
    _gemini_reply.return_value = "PGDCA is for graduates; DCA is for beginners."
    text, preset = _call("pgdca vs dca which is better?")
    _gemini_reply.assert_called_once()
    assert text == "PGDCA is for graduates; DCA is for beginners."

def test_python_worth_question_calls_gemini():
    _gemini_reply.return_value = "Python is great for getting IT jobs fast!"
    text, preset = _call("is python worth learning for jobs?")
    _gemini_reply.assert_called_once()

def test_gemini_receives_correct_course_card():
    """Context injected to Gemini must contain the matched course card, not another course."""
    _gemini_reply.return_value = "DCA is ideal for beginners."
    _call("dca course fees how much?")
    _, kwargs = _gemini_reply.call_args
    ctx = kwargs.get("context", "")
    assert "DCA" in ctx
    # Should NOT contain PGDCA card details
    assert "Post Graduate Diploma" not in ctx


# ════════════════════════════════════════════════════════════════════════════
# Routing: Gemini failure → static course card fallback
# ════════════════════════════════════════════════════════════════════════════

def test_gemini_failure_falls_back_to_card():
    _gemini_reply.return_value = None
    text, preset = _call("is pgdca worth the fees?")
    _gemini_reply.assert_called_once()
    assert "PGDCA" in text
    assert preset == "COURSE"

def test_gemini_failure_fallback_dca():
    _gemini_reply.return_value = None
    text, preset = _call("dca course duration how long?")
    _gemini_reply.assert_called_once()
    assert "DCA" in text
    assert preset == "COURSE"


# ════════════════════════════════════════════════════════════════════════════
# Existing flows: regression (must not change behaviour)
# ════════════════════════════════════════════════════════════════════════════

def test_fees_keyword_exact():
    text, preset = _call("fees", course="PGDCA")
    assert "₹15,999" in text
    assert preset == "FEES"
    _gemini_reply.assert_not_called()

def test_demo_keyword_enters_flow():
    state = _make_state()
    _state_mod.get_or_create_state.return_value = state
    _gemini_reply.reset_mock()
    text, _ = smart_reply("demo", "Tester", "9900000000", False, tenant_id="test-tenant")
    assert any(w in text for w in ("Demo", "demo", "Batch", "batch", "time"))

def test_visit_word_triggers_visit():
    text, _ = _call("visit office please")
    assert "Malayinkeezhu" in text or "Oxford" in text

def test_call_word_triggers_call():
    text, _ = _call("call me please")
    assert "9447329972" in text

def test_greeting_new_lead():
    _state_mod.get_or_create_state.return_value = _make_state("new", "")
    _gemini_reply.reset_mock()
    text, _ = smart_reply("hi", "NewLead", "9811111111", True, tenant_id="test-tenant")
    assert "Oxford" in text or "നമസ്കാരം" in text

def test_exit_command():
    text, _ = _call("exit")
    assert "Nandi" in text or "9447329972" in text


# ════════════════════════════════════════════════════════════════════════════
# Script runner (python tests/test_routing_phase1.py)
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
