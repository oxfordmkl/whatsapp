"""
Phase 1.6.3 — regression tests for router navigation integration.

The router is path-loaded with every dependency registered through monkeypatch
(auto-reverted), so this file makes no permanent sys.modules changes and stays
independent of collection order.

Proves the two things that matter:
  1. NAV:MENU / NAV:BACK resolve through navigation → screens → transport.
  2. EVERYTHING else — numeric replies, legacy interactive ids, free text,
     unknown/unwired namespaces — falls through to the legacy router unchanged.
"""
import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Institute facts that must never be restated outside business_profile.py
_LITERALS = ["The Oxford Computers", "maps.app.goo.gl", "9447329972",
             "theoxfordedu.com", "Malayinkeezhu"]


def _load(unique_name, relpath, register_as=None, monkeypatch=None):
    path = os.path.join(_ROOT, relpath)
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    if register_as and monkeypatch is not None:
        monkeypatch.setitem(sys.modules, register_as, mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(monkeypatch):
    """Load the real router with real navigation/screens and stubbed services."""
    # ── external / flask stubs ────────────────────────────────────────────
    flask = types.ModuleType("flask")
    flask.current_app = MagicMock()
    flask.current_app._get_current_object = MagicMock(return_value=MagicMock())
    flask.Blueprint = MagicMock(return_value=MagicMock())
    flask.jsonify = lambda d: d
    monkeypatch.setitem(sys.modules, "flask", flask)

    google = types.ModuleType("google")
    genai = types.ModuleType("google.genai")
    genai.Client = MagicMock
    google.genai = genai
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)

    for pkg in ("app", "app.bot", "app.services"):
        monkeypatch.setitem(sys.modules, pkg, types.ModuleType(pkg))

    cfg = types.ModuleType("app.config")
    cfg.GEMINI_API_KEY = "k"
    cfg.GEMINI_MODEL = "gemini-2.5-flash"
    monkeypatch.setitem(sys.modules, "app.config", cfg)

    gemini_reply = MagicMock(return_value=None)
    ai = types.ModuleType("app.services.ai_service")
    ai.gemini_reply = gemini_reply
    ai.smart_fallback = MagicMock(return_value="AI_FALLBACK")
    monkeypatch.setitem(sys.modules, "app.services.ai_service", ai)

    crm = types.ModuleType("app.services.crm_service")
    crm.update_lead_status = MagicMock()
    monkeypatch.setitem(sys.modules, "app.services.crm_service", crm)

    log = types.ModuleType("app.services.log_service")
    log.log_lead_event_in_thread = MagicMock()
    log.resolve_tenant_id = MagicMock(return_value="t1")
    monkeypatch.setitem(sys.modules, "app.services.log_service", log)

    prompts = types.ModuleType("app.bot.prompts")
    prompts.AALIZA_PROMPT = "persona"
    monkeypatch.setitem(sys.modules, "app.bot.prompts", prompts)

    # ContextAssembler (memory) — inert stub; memory is untouched this phase.
    ctx_pkg = types.ModuleType("app.context")
    assembler = types.ModuleType("app.context.assembler")

    class _CA:
        @staticmethod
        def assemble(**kwargs):
            return kwargs.get("course_context", "")

    assembler.ContextAssembler = _CA
    monkeypatch.setitem(sys.modules, "app.context", ctx_pkg)
    monkeypatch.setitem(sys.modules, "app.context.assembler", assembler)

    # ── transport stub: records how the router rendered a screen ──────────
    rendered = {"calls": []}

    def render_list_text(body, sections):
        rendered["calls"].append({"body": body, "sections": sections})
        lines = [body]
        for s in sections:
            for r in s["rows"]:
                lines.append(r["title"])
        return "\n".join(lines)

    wa = types.ModuleType("app.services.whatsapp_service")
    wa.render_list_text = render_list_text
    wa.send_list = MagicMock()
    monkeypatch.setitem(sys.modules, "app.services.whatsapp_service", wa)

    # ── real pure modules ─────────────────────────────────────────────────
    profile = _load("_rn_profile", "app/bot/business_profile.py",
                    register_as="app.bot.business_profile", monkeypatch=monkeypatch)
    constants = _load("_rn_constants", "app/bot/constants.py",
                      register_as="app.bot.constants", monkeypatch=monkeypatch)
    _load("_rn_objections", "app/bot/objections.py",
          register_as="app.bot.objections", monkeypatch=monkeypatch)
    nav = _load("_rn_navigation", "app/bot/navigation.py",
                register_as="app.bot.navigation", monkeypatch=monkeypatch)
    screens = _load("_rn_screens", "app/bot/screens.py",
                    register_as="app.bot.screens", monkeypatch=monkeypatch)
    cta = _load("_rn_cta", "app/bot/cta_handlers.py",
                register_as="app.bot.cta_handlers", monkeypatch=monkeypatch)
    booking = _load("_rn_booking", "app/bot/booking_handlers.py",
                    register_as="app.bot.booking_handlers", monkeypatch=monkeypatch)
    offers = _load("_rn_offers", "app/bot/offer_handlers.py",
                   register_as="app.bot.offer_handlers", monkeypatch=monkeypatch)

    state_holder = {}

    state_mod = types.ModuleType("app.state")
    state_mod.get_or_create_state = lambda *a, **k: state_holder["st"]
    monkeypatch.setitem(sys.modules, "app.state", state_mod)

    router = _load("_rn_router", "app/bot/router.py")

    # Run CRM/analytics background threads synchronously so their side effects
    # are deterministic to assert (the router's real behaviour is unchanged).
    class _ImmediateThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target, self._args = target, args
            self._kwargs = kwargs or {}

        def start(self):
            if self._target:
                self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(router, "threading",
                        types.SimpleNamespace(Thread=_ImmediateThread))

    def make_state(stage="course_viewed", course="PGDCA", goal="job"):
        state_holder["st"] = {
            "stage": stage, "course": course, "goal": goal,
            "last_msg": "", "last_text": "", "batch_time": "", "offer_course": "",
            "name": "Alice",
        }
        return state_holder["st"]

    def reply(text, stage="course_viewed", course="PGDCA", is_new_lead=False):
        make_state(stage, course)
        return router.smart_reply(text, "Alice", "+911", is_new_lead, tenant_id="t1")

    return types.SimpleNamespace(
        router=router, nav=nav, screens=screens, constants=constants,
        profile=profile, rendered=rendered, reply=reply, make_state=make_state,
        state=lambda: state_holder["st"], gemini=gemini_reply,
        crm=crm.update_lead_status, events=log.log_lead_event_in_thread,
        cta=cta, booking=booking, offers=offers,
    )


# ── NAV:MENU / NAV:BACK are wired ────────────────────────────────────────────
class TestNavigationWired:
    def test_nav_menu_returns_main_menu(self, env):
        text, preset = env.reply("NAV:MENU")
        assert "Main Menu" in text
        assert "💼 Job / IT Career" in text          # category rows present
        assert "🎓 Book Free Demo" in text           # quick actions present
        assert preset is None

    def test_nav_menu_uses_screens_builder_and_transport(self, env):
        env.reply("NAV:MENU")
        assert len(env.rendered["calls"]) == 1       # transport renderer used
        assert env.rendered["calls"][0]["sections"]  # builder supplied sections

    def test_nav_back_defaults_to_main_menu(self, env):
        text, _ = env.reply("NAV:BACK")
        assert "Main Menu" in text

    def test_nav_back_menu(self, env):
        text, _ = env.reply("NAV:BACK:MENU")
        assert "Main Menu" in text

    def test_nav_back_category_returns_category_menu(self, env):
        text, _ = env.reply("NAV:BACK:CATEGORY")
        assert "Career Categories" in text
        assert "📊 Accounting / Tax" in text

    def test_unsupported_back_target_degrades_to_main_menu(self, env):
        """LIST/COURSE targets are later phases — must not break the flow."""
        text, _ = env.reply("NAV:BACK:LIST:accounting")
        assert "Main Menu" in text

    def test_navigation_sets_canonical_stage(self, env):
        env.reply("NAV:MENU", stage="course_viewed")
        assert env.state()["stage"] == "goal_selection"   # existing vocabulary

    def test_navigation_works_from_any_stage(self, env):
        for stage in ("new", "goal_selection", "course_recommendation",
                      "course_viewed", "demo_time_ask", "offer_menu",
                      "payment_pending", "enrolled", "done"):
            text, _ = env.reply("NAV:MENU", stage=stage)
            assert "Main Menu" in text, stage


# ── Legacy behaviour must be untouched ───────────────────────────────────────
class TestLegacyNumericUnchanged:
    def test_goal_selection_numbers_still_select_goals(self, env):
        text, preset = env.reply("1", stage="goal_selection")
        assert "Main Menu" not in text            # navigation did NOT intercept
        assert env.state()["stage"] == "course_recommendation"

    def test_course_recommendation_numbers_still_pick_courses(self, env):
        env.make_state(stage="course_recommendation", course="")
        text, _ = env.router.smart_reply("1", "Alice", "+911", False, tenant_id="t1")
        assert "Main Menu" not in text
        assert env.state()["stage"] == "course_viewed"

    def test_demo_slot_numbers_unchanged(self, env):
        text, _ = env.reply("1", stage="demo_time_ask")
        assert "Main Menu" not in text
        assert env.state()["stage"] == "demo_date_ask"

    def test_offer_menu_numbers_unchanged(self, env):
        text, _ = env.reply("2", stage="offer_menu")
        assert "Main Menu" not in text
        assert env.state()["stage"] == "payment_pending"

    @pytest.mark.parametrize("digit", ["1", "2", "3", "4", "5", "10"])
    def test_no_digit_is_claimed_by_navigation(self, env, digit):
        assert env.router._try_navigation(digit, "Alice", env.make_state()) is None


class TestLegacyInteractiveIdsUnchanged:
    @pytest.mark.parametrize("legacy_id", [
        "DEMO", "FEES", "VISIT", "CALL", "ENROLL_NOW", "COURSES", "OFFER",
    ])
    def test_legacy_ids_not_claimed_by_navigation(self, env, legacy_id):
        assert env.router._try_navigation(legacy_id, "Alice", env.make_state()) is None

    def test_demo_still_starts_booking(self, env):
        text, _ = env.reply("DEMO")
        assert "Main Menu" not in text
        assert env.state()["stage"] == "demo_time_ask"

    def test_fees_still_returns_fee_reply(self, env):
        text, preset = env.reply("fees", course="PGDCA")
        assert preset == "FEES"
        assert "Main Menu" not in text

    def test_visit_still_returns_office_reply(self, env):
        text, _ = env.reply("visit")
        assert "Office Visit" in text


class TestFallThrough:
    @pytest.mark.parametrize("raw", [
        "XYZ:something", "FOO:BAR", "CAT:notacategory", "ACT:DANCE",
        "SLOT:midnight", "NAV:SIDEWAYS", "NAV:BACK:GALAXY",
    ])
    def test_unknown_or_invalid_ids_fall_through(self, env, raw):
        assert env.router._try_navigation(raw, "Alice", env.make_state()) is None


# ── Phase 6.8: offer flow wired ──────────────────────────────────────────────
class TestOfferFlow:
    def _offer(self, env, code, stage="offer_menu"):
        env.make_state(stage=stage, course="")
        return env.router.smart_reply(f"OFR:{code}", "Alice", "+911", False, tenant_id="t1")

    @pytest.mark.parametrize("code", ["CWPDE", "DCA", "AIDM", "PGDCA"])
    def test_offer_selection_issues_payment_link(self, env, code):
        text, preset = self._offer(env, code)
        assert "Secure Payment Link" in text
        assert env.state()["offer_course"] == code
        assert env.state()["stage"] == "payment_pending"
        assert preset is None

    def test_offer_code_is_case_insensitive(self, env):
        self._offer(env, "dca")
        assert env.state()["offer_course"] == "DCA"

    def test_offer_uses_the_single_catalogue(self, env):
        """Price/link must come from constants.OFFER_MENU, not a copy."""
        text, _ = self._offer(env, "PGDCA")
        _code, _name, price, _dur, link = env.constants.OFFER_MENU["4"]
        assert price in text and link in text

    def test_offer_selection_writes_no_crm_and_no_analytics(self, env):
        """Identical to legacy: selecting an offer records nothing."""
        self._offer(env, "DCA")
        env.crm.assert_not_called()
        env.events.assert_not_called()

    def test_offer_works_from_any_stage(self, env):
        for stage in ("new", "goal_selection", "course_viewed", "demo_booked"):
            self._offer(env, "AIDM", stage=stage)
            assert env.state()["stage"] == "payment_pending"

    @pytest.mark.parametrize("bad", ["OFR:NOPE", "OFR:123", "OFR:"])
    def test_unknown_offers_fall_through(self, env, bad):
        assert env.router._try_navigation(bad, "Alice", env.make_state(),
                                          "+911", "t1") is None

    def test_unknown_offer_does_not_mutate_state(self, env):
        st = env.make_state(stage="course_viewed")
        env.router._try_navigation("OFR:NOPE", "Alice", st, "+911", "t1")
        assert st["stage"] == "course_viewed"
        assert st["offer_course"] == ""


class TestOfferParityWithLegacy:
    def test_numeric_and_code_paths_are_identical(self, env):
        """OFR:DCA must equal replying '2' at the offer_menu stage."""
        env.make_state(stage="offer_menu", course="")
        legacy_text, legacy_preset = env.router.smart_reply(
            "2", "Alice", "+911", False, tenant_id="t1")
        legacy_state = dict(env.state())

        env.make_state(stage="offer_menu", course="")
        new_text, new_preset = env.router.smart_reply(
            "OFR:DCA", "Alice", "+911", False, tenant_id="t1")

        assert (new_text, new_preset) == (legacy_text, legacy_preset)
        assert env.state()["offer_course"] == legacy_state["offer_course"]
        assert env.state()["stage"] == legacy_state["stage"]

    @pytest.mark.parametrize("digit,code", [
        ("1", "CWPDE"), ("2", "DCA"), ("3", "AIDM"), ("4", "PGDCA"),
    ])
    def test_all_legacy_offer_numbers_map_identically(self, env, digit, code):
        env.make_state(stage="offer_menu", course="")
        env.router.smart_reply(digit, "Alice", "+911", False, tenant_id="t1")
        assert env.state()["offer_course"] == code

    def test_legacy_offer_keyword_still_opens_menu(self, env):
        env.make_state(stage="course_viewed", course="")
        text, preset = env.router.smart_reply("offer", "Alice", "+911",
                                              False, tenant_id="t1")
        assert "Special Offer" in text and preset == "OFFER"
        assert env.state()["stage"] == "offer_menu"

    def test_legacy_discount_keyword_still_works(self, env):
        env.make_state(stage="course_viewed", course="")
        text, _ = env.router.smart_reply("discount", "Alice", "+911",
                                         False, tenant_id="t1")
        assert "Special Offer" in text

    def test_pay_keyword_with_course_issues_link(self, env):
        env.make_state(stage="course_viewed", course="PGDCA")
        text, _ = env.router.smart_reply("pay", "Alice", "+911", False, tenant_id="t1")
        assert "Secure Payment Link" in text
        assert env.state()["stage"] == "payment_pending"

    def test_pay_keyword_without_course_opens_offer_menu(self, env):
        env.make_state(stage="course_viewed", course="")
        text, _ = env.router.smart_reply("seat", "Alice", "+911", False, tenant_id="t1")
        assert "Special Offer" in text
        assert env.state()["stage"] == "offer_menu"


class TestPaymentConfirmationParity:
    def test_payment_confirmation_crm_identical(self, env):
        env.make_state(stage="payment_pending", course="DCA Fast Track")
        env.state()["offer_course"] = "DCA"
        env.router.smart_reply("T2504281234", "Alice", "+911", False, tenant_id="t1")
        args = env.crm.call_args.args
        assert args[0] == "+911"
        assert args[1] == "Payment Received: T2504281234"
        assert "Payment: T2504281234 Course: DCA" in args[2]   # timestamped note
        assert args[3] == "t1"

    def test_payment_confirmation_sets_enrolled(self, env):
        env.make_state(stage="payment_pending", course="")
        env.state()["offer_course"] = "PGDCA"
        text, preset = env.router.smart_reply("TXN99", "Alice", "+911",
                                              False, tenant_id="t1")
        assert "Payment Received — Seat Confirmed" in text
        assert preset == "AFTER_BOOKING"
        assert env.state()["stage"] == "enrolled"

    def test_payment_confirmation_emits_no_analytics(self, env):
        env.make_state(stage="payment_pending", course="")
        env.state()["offer_course"] = "DCA"
        env.events.reset_mock()
        env.router.smart_reply("TXN1", "Alice", "+911", False, tenant_id="t1")
        env.events.assert_not_called()

    def test_confirmation_sources_institute_facts_from_profile(self, env):
        env.make_state(stage="payment_pending", course="")
        env.state()["offer_course"] = "DCA"
        text, _ = env.router.smart_reply("TXN1", "Alice", "+911", False, tenant_id="t1")
        assert env.profile.INSTITUTE_NAME in text
        assert env.profile.PHONE in text
        assert env.profile.LOCALITY in text and env.profile.CITY in text

    def test_full_offer_to_admission_flow(self, env):
        env.make_state(stage="course_viewed", course="")
        env.router.smart_reply("offer", "Alice", "+911", False, tenant_id="t1")
        env.router.smart_reply("OFR:AIDM", "Alice", "+911", False, tenant_id="t1")
        env.crm.reset_mock()
        text, _ = env.router.smart_reply("T123", "Alice", "+911", False, tenant_id="t1")
        assert "Seat Confirmed" in text
        assert env.state()["stage"] == "enrolled"
        env.crm.assert_called_once()


class TestOfferLayerHasNoBusinessLiterals:
    @pytest.mark.parametrize("literal", _LITERALS)
    def test_offer_handlers_module_is_literal_free(self, literal):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app/bot/offer_handlers.py"), encoding="utf-8") as fh:
            assert literal not in fh.read()


# ── Phase 6.7: slot booking wired ────────────────────────────────────────────
class TestSlotBooking:
    def _slot(self, env, key, stage="demo_time_ask", course="PGDCA"):
        env.make_state(stage=stage, course=course)
        return env.router.smart_reply(f"SLOT:{key}", "Alice", "+911", False, tenant_id="t1")

    @pytest.mark.parametrize("key,label", [
        ("morning", "Morning (9–11 AM)"),
        ("afternoon", "Afternoon (12–2 PM)"),
        ("evening", "Evening (5–7 PM)"),
    ])
    def test_slot_selection_sets_batch_time_and_stage(self, env, key, label):
        text, preset = self._slot(env, key)
        assert env.state()["batch_time"] == label
        assert env.state()["stage"] == "demo_date_ask"
        assert "batch confirmed" in text
        assert preset is None

    def test_slot_label_matches_builder_definition(self, env):
        for key, _title, label in env.screens.DEMO_SLOTS:
            self._slot(env, key)
            assert env.state()["batch_time"] == label

    def test_slot_works_from_any_stage(self, env):
        """Absolute ids: tapping an older slot message re-enters booking."""
        for stage in ("new", "goal_selection", "course_viewed", "demo_booked"):
            self._slot(env, "evening", stage=stage)
            assert env.state()["stage"] == "demo_date_ask"

    @pytest.mark.parametrize("bad", ["SLOT:midnight", "SLOT:noon", "SLOT:"])
    def test_unknown_slots_fall_through(self, env, bad):
        assert env.router._try_navigation(bad, "Alice", env.make_state(),
                                          "+911", "t1") is None

    def test_demo_cta_offers_slot_buttons(self, env):
        env.make_state(stage="course_viewed", course="PGDCA")
        _text, preset = env.router.smart_reply("ACT:DEMO", "Alice", "+911",
                                               False, tenant_id="t1")
        assert [b["id"] for b in preset] == ["SLOT:morning", "SLOT:afternoon", "SLOT:evening"]

    def test_slot_buttons_are_all_handled(self, env):
        """Every slot button offered must resolve — no dead ends."""
        for b in env.screens.demo_slots_screen().as_buttons():
            env.make_state(stage="demo_time_ask")
            assert env.router._try_navigation(b["id"], "Alice", env.state(),
                                              "+911", "t1") is not None

    def test_full_booking_flow_via_buttons(self, env):
        env.make_state(stage="course_viewed", course="PGDCA")
        env.router.smart_reply("ACT:DEMO", "Alice", "+911", False, tenant_id="t1")
        env.router.smart_reply("SLOT:morning", "Alice", "+911", False, tenant_id="t1")
        text, preset = env.router.smart_reply("Tomorrow", "Alice", "+911",
                                              False, tenant_id="t1")
        assert "Demo Class Booked Successfully" in text
        assert preset == "AFTER_BOOKING"
        assert env.state()["stage"] == "demo_booked"

    def test_booking_writes_crm_record(self, env):
        env.make_state(stage="demo_time_ask", course="PGDCA")
        env.router.smart_reply("SLOT:morning", "Alice", "+911", False, tenant_id="t1")
        env.crm.reset_mock()
        env.router.smart_reply("Monday", "Alice", "+911", False, tenant_id="t1")
        env.crm.assert_called_once_with(
            "+911", "Demo Booked: PGDCA | Morning (9–11 AM) | Monday", "", "t1")

    def test_confirmation_sources_institute_facts_from_profile(self, env):
        env.make_state(stage="demo_date_ask", course="PGDCA")
        env.state()["batch_time"] = "Morning (9–11 AM)"
        text, _ = env.router.smart_reply("Tomorrow", "Alice", "+911", False, tenant_id="t1")
        assert env.profile.INSTITUTE_NAME in text
        assert env.profile.PHONE in text
        assert env.profile.WEBSITE in text


class TestLegacyDemoFlowUnchanged:
    def test_legacy_numeric_slot_still_works(self, env):
        env.make_state(stage="demo_time_ask", course="PGDCA")
        text, _ = env.router.smart_reply("1", "Alice", "+911", False, tenant_id="t1")
        assert env.state()["batch_time"] == "Morning (9–11 AM)"
        assert env.state()["stage"] == "demo_date_ask"
        assert "batch confirmed" in text

    @pytest.mark.parametrize("digit,label", [
        ("1", "Morning (9–11 AM)"),
        ("2", "Afternoon (12–2 PM)"),
        ("3", "Evening (5–7 PM)"),
    ])
    def test_all_legacy_numbers_map_identically(self, env, digit, label):
        env.make_state(stage="demo_time_ask", course="PGDCA")
        env.router.smart_reply(digit, "Alice", "+911", False, tenant_id="t1")
        assert env.state()["batch_time"] == label

    def test_numeric_and_button_paths_are_identical(self, env):
        """Tapping SLOT:morning must equal replying '1'."""
        env.make_state(stage="demo_time_ask", course="PGDCA")
        legacy_text, legacy_preset = env.router.smart_reply(
            "1", "Alice", "+911", False, tenant_id="t1")
        legacy_state = dict(env.state())

        env.make_state(stage="demo_time_ask", course="PGDCA")
        new_text, new_preset = env.router.smart_reply(
            "SLOT:morning", "Alice", "+911", False, tenant_id="t1")

        assert (new_text, new_preset) == (legacy_text, legacy_preset)
        assert env.state()["batch_time"] == legacy_state["batch_time"]
        assert env.state()["stage"] == legacy_state["stage"]

    def test_legacy_demo_keyword_still_starts_booking(self, env):
        env.make_state(stage="course_viewed", course="PGDCA")
        text, _ = env.router.smart_reply("demo", "Alice", "+911", False, tenant_id="t1")
        assert "Free Demo Class Booking" in text
        assert env.state()["stage"] == "demo_time_ask"

    def test_legacy_date_path_crm_identical(self, env):
        env.make_state(stage="demo_date_ask", course="DCA Fast Track")
        env.state()["batch_time"] = "Evening (5–7 PM)"
        env.router.smart_reply("May 5", "Alice", "+911", False, tenant_id="t1")
        env.crm.assert_called_once_with(
            "+911", "Demo Booked: DCA Fast Track | Evening (5–7 PM) | May 5", "", "t1")

    def test_no_new_analytics_event_on_booking(self, env):
        """Analytics must remain identical — booking emits no new event."""
        env.make_state(stage="demo_date_ask", course="PGDCA")
        env.state()["batch_time"] = "Morning (9–11 AM)"
        env.events.reset_mock()
        env.router.smart_reply("Tomorrow", "Alice", "+911", False, tenant_id="t1")
        env.events.assert_not_called()


class TestBookingLayerHasNoBusinessLiterals:
    @pytest.mark.parametrize("literal", _LITERALS)
    def test_booking_handlers_module_is_literal_free(self, literal):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app/bot/booking_handlers.py"), encoding="utf-8") as fh:
            assert literal not in fh.read()


# ── Phase 6.6: CTA handlers wired ────────────────────────────────────────────
class TestCtaHandlers:
    def _act(self, env, cta, stage="course_viewed", course="PGDCA"):
        env.make_state(stage=stage, course=course)
        return env.router.smart_reply(f"ACT:{cta}", "Alice", "+911", False, tenant_id="t1")

    def test_demo_starts_booking(self, env):
        text, _ = self._act(env, "DEMO")
        assert "Free Demo Class Booking" in text
        assert env.state()["stage"] == "demo_time_ask"

    def test_demo_emits_analytics(self, env):
        self._act(env, "DEMO")
        assert env.events.call_args.kwargs["event_type"] == "DEMO_REQUESTED"

    def test_fees_returns_course_fee(self, env):
        text, preset = self._act(env, "FEES", course="PGDCA")
        assert "Fee Details" in text and preset == "FEES"

    def test_fees_emits_analytics_with_course(self, env):
        self._act(env, "FEES", course="PGDCA")
        kwargs = env.events.call_args.kwargs
        assert kwargs["event_type"] == "FEES_REQUESTED"
        assert kwargs["event_data"] == "PGDCA"

    def test_fees_without_course_returns_full_table(self, env):
        text, preset = self._act(env, "FEES", course="")
        assert env.constants.FULL_FEE_TABLE in text and preset == "FEES"

    def test_call_returns_counsellor_reply_and_crm(self, env):
        text, _ = self._act(env, "CALL")
        assert env.profile.PHONE in text
        env.crm.assert_called_once_with("+911", "Call Requested", "", "t1")

    def test_enroll_returns_payment_link_when_course_has_one(self, env):
        text, _ = self._act(env, "ENROLL", course="PGDCA")
        assert "Secure Payment Link" in text
        assert env.state()["stage"] == "payment_pending"

    def test_enroll_without_course_asks_for_selection(self, env):
        text, preset = self._act(env, "ENROLL", course="")
        assert "enroll cheyyan ready aano" in text and preset == "GOAL"


class TestVisitCta:
    def _visit(self, env):
        env.make_state(stage="course_viewed")
        return env.router.smart_reply("ACT:VISIT", "Alice", "+911", False, tenant_id="t1")

    def test_visit_includes_all_four_required_facts(self, env):
        text, _ = self._visit(env)
        p = env.profile
        assert p.INSTITUTE_NAME in text      # institute name
        assert p.ADDRESS in text             # canonical address
        assert p.MAPS_URL in text            # google maps link
        assert p.PHONE in text               # phone number

    def test_visit_updates_crm(self, env):
        self._visit(env)
        env.crm.assert_called_once_with("+911", "Office Visit Interested", "", "t1")

    def test_maps_link_emitted_only_from_profile(self, env):
        """The link in the reply must be the profile value, and the profile must
        be the only place in app/ where that link is written."""
        import os
        import re
        text, _ = self._visit(env)
        assert env.profile.MAPS_URL in text
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        hits = []
        for folder, _d, files in os.walk(os.path.join(root, "app")):
            for f in files:
                if f.endswith(".py"):
                    with open(os.path.join(folder, f), encoding="utf-8") as fh:
                        if re.search(r"maps\.app\.goo\.gl", fh.read()):
                            hits.append(f)
        assert hits == ["business_profile.py"], hits

    def test_legacy_visit_keyword_gets_same_enhanced_reply(self, env):
        env.make_state(stage="course_viewed")
        text, _ = env.router.smart_reply("visit", "Alice", "+911", False, tenant_id="t1")
        assert env.profile.MAPS_URL in text


class TestCourseDetailsCtaDefinitions:
    def test_buttons_come_from_the_builder(self, env):
        env.make_state(stage="course_recommendation", course="")
        env.state()["goal"] = "accounting"
        _text, preset = env.router.smart_reply("CRS:3", "Alice", "+911", False, tenant_id="t1")
        assert preset == env.screens.course_details("3").as_buttons()

    def test_buttons_are_act_ids_that_resolve(self, env):
        buttons = env.screens.course_details("1").as_buttons()
        assert [b["id"] for b in buttons] == ["ACT:DEMO", "ACT:FEES", "ACT:VISIT"]
        for b in buttons:
            assert env.nav.parse_action(b["id"]).kind == env.nav.KIND_CTA

    def test_course_details_buttons_are_tappable_end_to_end(self, env):
        """Every CTA offered on Course Details must be handled, not dead-end."""
        for b in env.screens.course_details("1").as_buttons():
            env.make_state(stage="course_viewed", course="PGDCA")
            result = env.router._try_navigation(b["id"], "Alice", env.state(), "+911", "t1")
            assert result is not None, b["id"]


class TestLegacyCtaKeywordsUnchanged:
    @pytest.mark.parametrize("keyword,expect", [
        ("demo", "Free Demo Class Booking"),
        ("free demo", "Free Demo Class Booking"),
        ("call", "counselorne connect"),
        ("office", "Office Visit"),
        ("fees", "Fee Details"),
        ("offer", "Special Offer"),
        ("enroll_now", "Secure Payment Link"),
    ])
    def test_legacy_keyword_still_works(self, env, keyword, expect):
        env.make_state(stage="course_viewed", course="PGDCA")
        text, _ = env.router.smart_reply(keyword, "Alice", "+911", False, tenant_id="t1")
        assert expect in text

    def test_legacy_button_id_and_act_id_agree(self, env):
        """Legacy 'DEMO' and new 'ACT:DEMO' must produce the same reply."""
        env.make_state(stage="course_viewed", course="PGDCA")
        legacy, _ = env.router.smart_reply("DEMO", "Alice", "+911", False, tenant_id="t1")
        env.make_state(stage="course_viewed", course="PGDCA")
        new, _ = env.router.smart_reply("ACT:DEMO", "Alice", "+911", False, tenant_id="t1")
        assert legacy == new

    def test_offer_menu_stage_still_issues_payment_link(self, env):
        env.make_state(stage="offer_menu", course="")
        text, _ = env.router.smart_reply("2", "Alice", "+911", False, tenant_id="t1")
        assert "Secure Payment Link" in text
        assert env.state()["stage"] == "payment_pending"


class TestCtaLayerHasNoBusinessLiterals:
    @pytest.mark.parametrize("literal", _LITERALS)
    def test_cta_handlers_module_is_literal_free(self, literal):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app/bot/cta_handlers.py"), encoding="utf-8") as fh:
            assert literal not in fh.read()


# ── Phase 6.5: course selection wired ────────────────────────────────────────
class TestCourseSelection:
    def _select(self, env, course_id, stage="course_recommendation"):
        env.make_state(stage=stage, course="")
        env.state()["goal"] = "accounting"
        return env.router.smart_reply(course_id, "Alice", "+911", False, tenant_id="t1")

    def test_course_selection_returns_details(self, env):
        text, preset = self._select(env, "CRS:3")
        _name, card = env.constants.ALL_COURSES["3"]
        assert card in text
        # Phase 6.6: CTA definitions now come from the builder (3 reply buttons).
        assert preset == env.screens.course_details("3").as_buttons()
        assert len(preset) == 3

    def test_course_selection_sets_state(self, env):
        self._select(env, "CRS:3")
        assert env.state()["course"] == env.constants.ALL_COURSES["3"][0]
        assert env.state()["stage"] == "course_viewed"

    def test_course_selection_updates_crm_identically(self, env):
        self._select(env, "CRS:3")
        name = env.constants.ALL_COURSES["3"][0]
        env.crm.assert_called_once_with("+911", f"Viewed: {name}", "", "t1")

    def test_course_selection_emits_course_viewed_analytics(self, env):
        self._select(env, "CRS:3")
        name = env.constants.ALL_COURSES["3"][0]
        kwargs = env.events.call_args.kwargs
        assert kwargs["event_type"] == "COURSE_VIEWED"
        assert kwargs["event_data"] == name
        assert kwargs["phone"] == "+911"
        assert kwargs["tenant_id"] == "t1"

    def test_crm_and_analytics_match_legacy_numeric_path(self, env):
        """Tapping a course row must be indistinguishable from replying '1'."""
        # New path
        self._select(env, "CRS:3")
        new_crm, new_evt = env.crm.call_args, env.events.call_args.kwargs
        env.crm.reset_mock(); env.events.reset_mock()

        # Legacy numeric path: accounting recommendations → index 1 is SAP
        env.make_state(stage="course_recommendation", course="")
        env.state()["goal"] = "accounting"
        env.router.smart_reply("1", "Alice", "+911", False, tenant_id="t1")

        assert env.crm.call_args == new_crm
        assert env.events.call_args.kwargs == new_evt

    @pytest.mark.parametrize("index", ["1", "2", "5", "10"])
    def test_all_valid_course_ids_work(self, env, index):
        text, preset = self._select(env, f"CRS:{index}")
        assert len(preset) == 3
        assert env.constants.ALL_COURSES[index][1] in text

    def test_course_selection_works_from_any_stage(self, env):
        for stage in ("new", "goal_selection", "course_viewed", "demo_time_ask"):
            _text, preset = self._select(env, "CRS:4", stage=stage)
            assert len(preset) == 3

    @pytest.mark.parametrize("bad", ["CRS:999", "CRS:0", "CRS:abc"])
    def test_unknown_course_ids_fall_through(self, env, bad):
        assert env.router._try_navigation(bad, "Alice", env.make_state(),
                                          "+911", "t1") is None

    def test_unknown_course_id_does_not_touch_crm_or_analytics(self, env):
        env.router._try_navigation("CRS:999", "Alice", env.make_state(), "+911", "t1")
        env.crm.assert_not_called()
        env.events.assert_not_called()

    def test_full_flow_menu_category_course(self, env):
        env.make_state(stage="new")
        env.router.smart_reply("NAV:MENU", "Alice", "+911", False, tenant_id="t1")
        env.router.smart_reply("CAT:accounting", "Alice", "+911", False, tenant_id="t1")
        _text, preset = env.router.smart_reply("CRS:3", "Alice", "+911", False, tenant_id="t1")
        assert len(preset) == 3
        assert env.state()["stage"] == "course_viewed"


class TestLegacyCourseSelectionUnchanged:
    def test_numeric_course_selection_still_works(self, env):
        env.make_state(stage="course_recommendation", course="")
        env.state()["goal"] = "job"
        text, preset = env.router.smart_reply("1", "Alice", "+911", False, tenant_id="t1")
        assert preset == "COURSE"
        assert env.state()["stage"] == "course_viewed"
        env.crm.assert_called_once()

    def test_keyword_course_selection_still_works(self, env):
        env.make_state(stage="goal_selection", course="")
        text, preset = env.router.smart_reply("sap", "Alice", "+911", False, tenant_id="t1")
        assert env.state()["course"] == "SAP Financial Accounting"
        assert env.state()["stage"] == "course_viewed"

    def test_direct_index_reply_still_works(self, env):
        env.make_state(stage="course_viewed", course="")
        text, preset = env.router.smart_reply("5", "Alice", "+911", False, tenant_id="t1")
        assert env.state()["course"] == env.constants.ALL_COURSES["5"][0]



class TestNoBusinessLiterals:
    """Requirement: no business literals in the navigation code or screens.

    NOTE: router.py still contains institute literals inside the LEGACY reply
    builders (msg_visit / msg_call_us / msg_demo_booked ...). Those are out of
    scope here: they belong to Visit/Demo/Admission, which this phase must not
    wire, and swapping in the Business Profile would change their visible text
    (the profile address is now far longer), breaking backward compatibility.
    Reconciling them is deliberate work for the phase that wires those replies.
    """

    def _source(self, relpath):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, relpath), encoding="utf-8") as fh:
            return fh.read()

    @pytest.mark.parametrize("literal", _LITERALS)
    def test_screens_module_is_literal_free(self, literal):
        assert literal not in self._source("app/bot/screens.py")

    @pytest.mark.parametrize("literal", _LITERALS)
    def test_navigation_code_in_router_is_literal_free(self, env, literal):
        import inspect
        src = "".join(
            inspect.getsource(fn) for fn in (
                env.router._try_navigation,
                env.router._course_destination,
                env.router._category_destination,
                env.router._nearest_menu,
            )
        )
        assert literal not in src

    def test_course_details_sources_institute_info_from_profile(self, env):
        screen = env.screens.course_details("1")
        assert env.profile.INSTITUTE_NAME in screen.body
        assert env.profile.PHONE in screen.body

    def test_main_menu_sources_institute_name_from_profile(self, env):
        assert env.profile.INSTITUTE_NAME in env.screens.main_menu().body


# ── Phase 6.4: categories wired ──────────────────────────────────────────────
class TestCategoryRouting:
    @pytest.mark.parametrize("category", ["job", "business", "basic", "accounting"])
    def test_category_returns_course_list(self, env, category):
        env.make_state(stage="goal_selection")
        text, preset = env.router.smart_reply(
            f"CAT:{category}", "Alice", "+911", False, tenant_id="t1")
        assert "Recommended Courses" in text
        assert preset is None

    def test_category_sets_goal_and_canonical_stage(self, env):
        env.make_state(stage="goal_selection")
        env.router.smart_reply("CAT:accounting", "Alice", "+911", False, tenant_id="t1")
        assert env.state()["goal"] == "accounting"
        assert env.state()["stage"] == "course_recommendation"

    def test_course_list_contains_that_categorys_courses(self, env):
        env.make_state(stage="goal_selection")
        text, _ = env.router.smart_reply("CAT:accounting", "Alice", "+911", False, tenant_id="t1")
        assert "SAP Financial Accounting" in text
        assert "GST & Payroll Diploma" in text

    def test_course_list_offers_back_and_menu(self, env):
        env.make_state(stage="goal_selection")
        text, _ = env.router.smart_reply("CAT:job", "Alice", "+911", False, tenant_id="t1")
        assert "⬅ All Categories" in text
        assert "🏠 Main Menu" in text

    def test_unsure_category_does_not_dead_end(self, env):
        env.make_state(stage="goal_selection")
        text, _ = env.router.smart_reply("CAT:unsure", "Alice", "+911", False, tenant_id="t1")
        assert "suggest cheyyam" in text
        assert env.state()["stage"] == "not_sure"

    def test_full_menu_to_category_to_list_flow(self, env):
        """NAV:MENU → Category → Course List, the wired Phase 6.4 path."""
        env.make_state(stage="new")
        menu, _ = env.router.smart_reply("NAV:MENU", "Alice", "+911", False, tenant_id="t1")
        assert "Main Menu" in menu
        lst, _ = env.router.smart_reply("CAT:basic", "Alice", "+911", False, tenant_id="t1")
        assert "Recommended Courses" in lst
        assert env.state()["stage"] == "course_recommendation"


# ── Phase 6.4: NAV:BACK returns to the NEAREST valid menu ────────────────────
class TestNearestValidMenu:
    def test_back_from_course_details_returns_to_its_course_list(self, env):
        env.make_state(stage="course_viewed", course="SAP Financial Accounting")
        env.state()["goal"] = "accounting"
        text, _ = env.router.smart_reply("NAV:BACK", "Alice", "+911", False, tenant_id="t1")
        assert "Recommended Courses" in text      # NOT the main menu
        assert env.state()["stage"] == "course_recommendation"

    def test_back_from_course_list_returns_to_categories(self, env):
        env.make_state(stage="course_recommendation")
        env.state()["goal"] = "job"
        text, _ = env.router.smart_reply("NAV:BACK", "Alice", "+911", False, tenant_id="t1")
        assert "Career Categories" in text        # one level up, not main menu

    def test_back_to_list_uses_category_in_the_id(self, env):
        env.make_state(stage="course_viewed")
        text, _ = env.router.smart_reply("NAV:BACK:LIST:accounting", "Alice", "+911",
                                         False, tenant_id="t1")
        assert "SAP Financial Accounting" in text
        assert env.state()["goal"] == "accounting"

    def test_back_to_list_falls_back_to_state_goal(self, env):
        env.make_state(stage="course_viewed")
        env.state()["goal"] = "basic"
        text, _ = env.router.smart_reply("NAV:BACK:LIST", "Alice", "+911", False, tenant_id="t1")
        assert "Recommended Courses" in text

    def test_unresolvable_list_degrades_to_categories_not_main_menu(self, env):
        """Nearest valid level up — the improvement over Phase 6.3."""
        env.make_state(stage="course_viewed")
        env.state()["goal"] = ""
        text, _ = env.router.smart_reply("NAV:BACK:LIST:bogus", "Alice", "+911",
                                         False, tenant_id="t1")
        assert "Career Categories" in text
        assert "Main Menu*" not in text

    def test_back_from_top_level_still_reaches_main_menu(self, env):
        env.make_state(stage="goal_selection")
        text, _ = env.router.smart_reply("NAV:BACK", "Alice", "+911", False, tenant_id="t1")
        assert "Main Menu" in text

    def test_free_text_reaches_ai_fallback(self, env):
        env.gemini.return_value = "AI reply"
        text, _ = env.reply("enthokke und sap course-nu?")
        assert text == "AI reply"
        assert env.gemini.called

    def test_greeting_still_returns_welcome(self, env):
        text, _ = env.reply("hi", stage="new")
        assert "സ്വാഗതം" in text or "Main Menu" not in text


class TestTransportUnchanged:
    def test_navigation_returns_text_not_a_list_send(self, env):
        """WA_LIST_MESSAGES stays OFF: the router returns (text, None) and the
        webhook sends it exactly as it does today — no list transport call."""
        _text, preset = env.reply("NAV:MENU")
        assert preset is None
        assert not env.router.__dict__.get("send_list")

    def test_router_contains_no_screen_text(self, env):
        """Screen copy must live in screens.py, not the router."""
        import inspect
        src = inspect.getsource(env.router._try_navigation)
        assert "Main Menu" not in src
        assert "Career Categories" not in src


# ── Institute configuration is prepared but NOT wired ────────────────────────
class TestBusinessProfile:
    def test_profile_has_all_required_fields(self, env):
        p = env.profile.BUSINESS_PROFILE
        for key in ("name", "address", "maps_url", "website",
                    "phone", "whatsapp", "email", "office_hours"):
            assert p[key], key

    def test_constants_reference_the_profile_not_copies(self, env):
        """constants must not restate business facts."""
        c, p = env.constants, env.profile
        assert c.INST_NAME == p.INSTITUTE_NAME
        assert c.INST_LOCATION == p.ADDRESS
        assert c.INST_PHONE == p.PHONE
        assert c.INST_WEBSITE == p.WEBSITE
        assert c.INST_MAPS_URL == p.MAPS_URL
        assert c.INSTITUTE is p.BUSINESS_PROFILE   # same object, no duplication

    def test_maps_url_defined_exactly_once_in_codebase(self, env):
        import os
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        hits = []
        for folder, _dirs, files in os.walk(os.path.join(root, "app")):
            for f in files:
                if f.endswith(".py"):
                    path = os.path.join(folder, f)
                    with open(path, encoding="utf-8") as fh:
                        if re.search(r"maps\.app\.goo\.gl", fh.read()):
                            hits.append(f)
        assert hits == ["business_profile.py"], hits

    def test_location_keywords_declared(self, env):
        for kw in ("location", "map", "google map", "address", "route",
                   "evide", "institute evide", "location ayakku"):
            assert kw in env.constants.LOCATION_KEYWORDS

    def test_visit_reply_now_emits_the_map_link(self, env):
        """Phase 6.6 Maps enhancement: the Visit reply now carries the link."""
        text, _ = env.reply("visit")
        assert env.constants.INST_MAPS_URL in text
