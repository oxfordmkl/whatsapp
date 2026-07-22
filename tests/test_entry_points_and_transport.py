"""
Phase 1.6.9 — entry-point integration + transport dispatch regression tests.

Two halves:

  * TRANSPORT — the real whatsapp_service is path-loaded with fakes and we assert
    that send_reply() dispatches text / reply-buttons / List Messages through ONE
    pipeline, and that WA_LIST_MESSAGES is evaluated only inside send_list().

  * ENTRY POINTS — the real router is path-loaded and we assert that hi / hello /
    start / courses / menu now enter the navigation architecture, returning a
    ListMessage that carries BOTH the list and its legacy fallback.

The flag matrix is then verified end-to-end:
    OFF → legacy welcome text + "GOAL" reply buttons  (exactly today's behaviour)
    ON  → a real WhatsApp List Message
"""
import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(unique_name, relpath, register_as=None, monkeypatch=None):
    path = os.path.join(_ROOT, relpath)
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    if register_as and monkeypatch is not None:
        monkeypatch.setitem(sys.modules, register_as, mod)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.text = "ok"

    def json(self):
        return {}


# ════════════════════════════════════════════════════════════════════════════
# TRANSPORT
# ════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def wa(monkeypatch):
    calls = {"posts": [], "texts": []}
    flag = {"on": False}

    req = types.ModuleType("requests")
    req.post = lambda url, headers=None, json=None, **k: (
        calls["posts"].append(json) or _Resp(200))
    req.get = lambda url, headers=None, **k: _Resp(200)
    req.Response = _Resp
    monkeypatch.setitem(sys.modules, "requests", req)

    cfg = types.ModuleType("app.config")
    cfg.ACCESS_TOKEN, cfg.PHONE_NUMBER_ID, cfg.WHATSAPP_API_URL = "t", "p", "u"
    monkeypatch.setitem(sys.modules, "app.config", cfg)

    consts = types.ModuleType("app.bot.constants")
    consts.BUTTON_PRESETS = {
        "COURSE": [{"id": "DEMO", "title": "Demo"}],
        "GOAL": [{"id": "1", "title": "Job"}, {"id": "2", "title": "Business"}],
    }
    monkeypatch.setitem(sys.modules, "app.bot.constants", consts)

    perf = types.ModuleType("app.perf")
    perf.mark = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "app.perf", perf)

    flags = types.ModuleType("app.flags")
    flags.wa_list_messages_enabled = lambda: flag["on"]
    monkeypatch.setitem(sys.modules, "app.flags", flags)

    mod = _load("_p69_wa", "app/services/whatsapp_service.py")
    monkeypatch.setattr(mod, "_get_waba_credentials", lambda tid=None: ("p", "t"))
    monkeypatch.setattr(mod, "send_text",
                        lambda to, text, tenant_id=None: (calls["texts"].append(text) or _Resp(200)))
    return types.SimpleNamespace(mod=mod, calls=calls, flag=flag)


SECTIONS = [{"title": "S", "rows": [{"id": "CAT:job", "title": "Job"}]}]


class TestSingleTransportPipeline:
    def test_none_preset_sends_text(self, wa):
        wa.mod.send_reply("+1", "hello", None)
        assert wa.calls["texts"] == ["hello"] and wa.calls["posts"] == []

    def test_str_preset_sends_reply_buttons(self, wa):
        wa.mod.send_reply("+1", "body", "GOAL")
        assert wa.calls["posts"][0]["interactive"]["type"] == "button"

    def test_button_list_sends_reply_buttons(self, wa):
        wa.mod.send_reply("+1", "body", [{"id": "SLOT:morning", "title": "Morning"}])
        payload = wa.calls["posts"][0]["interactive"]
        assert payload["type"] == "button"
        assert payload["action"]["buttons"][0]["reply"]["id"] == "SLOT:morning"

    def test_listmessage_sends_a_list_when_flag_on(self, wa):
        wa.flag["on"] = True
        wa.mod.send_reply("+1", "body", wa.mod.ListMessage("📋 Select", SECTIONS))
        payload = wa.calls["posts"][0]["interactive"]
        assert payload["type"] == "list"
        assert payload["action"]["sections"][0]["rows"][0]["id"] == "CAT:job"

    def test_listmessage_uses_legacy_fallback_when_flag_off(self, wa):
        wa.flag["on"] = False
        lm = wa.mod.ListMessage("📋 Select", SECTIONS,
                                fallback_body="LEGACY TEXT", fallback_preset="GOAL")
        wa.mod.send_reply("+1", "body", lm)
        assert wa.calls["posts"][0]["interactive"]["type"] == "button"   # legacy buttons
        assert wa.calls["posts"][0]["interactive"]["body"]["text"] == "LEGACY TEXT"

    def test_listmessage_without_fallback_degrades_to_text(self, wa):
        wa.flag["on"] = False
        wa.mod.send_reply("+1", "body", wa.mod.ListMessage("📋 Select", SECTIONS))
        assert wa.calls["texts"] and "Job" in wa.calls["texts"][0]

    def test_api_failure_degrades_to_legacy(self, wa, monkeypatch):
        wa.flag["on"] = True
        monkeypatch.setitem(sys.modules, "requests", sys.modules["requests"])
        monkeypatch.setattr(sys.modules["requests"], "post",
                            lambda *a, **k: _Resp(400))
        lm = wa.mod.ListMessage("📋 Select", SECTIONS, fallback_body="LEGACY", fallback_preset=None)
        wa.mod.send_reply("+1", "body", lm)
        assert wa.calls["texts"] == ["LEGACY"]

    def test_flag_is_only_read_inside_send_list(self, wa):
        """WA_LIST_MESSAGES must not influence text or button sends."""
        for state in (True, False):
            wa.flag["on"] = state
            wa.calls["posts"].clear(); wa.calls["texts"].clear()
            wa.mod.send_reply("+1", "t", None)
            wa.mod.send_reply("+1", "b", "GOAL")
            assert wa.calls["texts"] == ["t"]
            assert wa.calls["posts"][0]["interactive"]["type"] == "button"


# ════════════════════════════════════════════════════════════════════════════
# ENTRY POINTS
# ════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def env(monkeypatch):
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
    cfg.GEMINI_API_KEY, cfg.GEMINI_MODEL = "k", "m"
    monkeypatch.setitem(sys.modules, "app.config", cfg)

    ai = types.ModuleType("app.services.ai_service")
    ai.gemini_reply = MagicMock(return_value=None)
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
    prompts.AALIZA_PROMPT = "p"
    monkeypatch.setitem(sys.modules, "app.bot.prompts", prompts)

    ctx = types.ModuleType("app.context")
    asm = types.ModuleType("app.context.assembler")

    class _CA:
        @staticmethod
        def assemble(**kw):
            return kw.get("course_context", "")

    asm.ContextAssembler = _CA
    monkeypatch.setitem(sys.modules, "app.context", ctx)
    monkeypatch.setitem(sys.modules, "app.context.assembler", asm)

    # Real transport module (for the ListMessage type the router returns).
    req = types.ModuleType("requests")
    req.post = lambda *a, **k: _Resp(200)
    req.get = lambda *a, **k: _Resp(200)
    req.Response = _Resp
    monkeypatch.setitem(sys.modules, "requests", req)
    wacfg = types.ModuleType("app.config")
    wacfg.ACCESS_TOKEN = wacfg.PHONE_NUMBER_ID = wacfg.WHATSAPP_API_URL = "x"
    wacfg.GEMINI_API_KEY, wacfg.GEMINI_MODEL = "k", "m"
    monkeypatch.setitem(sys.modules, "app.config", wacfg)
    perf = types.ModuleType("app.perf")
    perf.mark = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "app.perf", perf)

    _load("_p69_profile", "app/bot/business_profile.py",
          register_as="app.bot.business_profile", monkeypatch=monkeypatch)
    constants = _load("_p69_constants", "app/bot/constants.py",
                      register_as="app.bot.constants", monkeypatch=monkeypatch)
    _load("_p69_objections", "app/bot/objections.py",
          register_as="app.bot.objections", monkeypatch=monkeypatch)
    nav = _load("_p69_nav", "app/bot/navigation.py",
                register_as="app.bot.navigation", monkeypatch=monkeypatch)
    screens = _load("_p69_screens", "app/bot/screens.py",
                    register_as="app.bot.screens", monkeypatch=monkeypatch)
    wa_mod = _load("_p69_wa2", "app/services/whatsapp_service.py",
                   register_as="app.services.whatsapp_service", monkeypatch=monkeypatch)
    _load("_p69_cta", "app/bot/cta_handlers.py",
          register_as="app.bot.cta_handlers", monkeypatch=monkeypatch)
    _load("_p69_booking", "app/bot/booking_handlers.py",
          register_as="app.bot.booking_handlers", monkeypatch=monkeypatch)
    _load("_p69_offers", "app/bot/offer_handlers.py",
          register_as="app.bot.offer_handlers", monkeypatch=monkeypatch)

    holder = {}
    state_mod = types.ModuleType("app.state")
    state_mod.get_or_create_state = lambda *a, **k: holder["st"]
    monkeypatch.setitem(sys.modules, "app.state", state_mod)

    router = _load("_p69_router", "app/bot/router.py")

    def make_state(stage="new", course=""):
        holder["st"] = {"stage": stage, "course": course, "goal": "",
                        "last_msg": "", "last_text": "", "batch_time": "",
                        "offer_course": "", "name": "Alice"}
        return holder["st"]

    def reply(text, stage="new", is_new_lead=False):
        make_state(stage)
        return router.smart_reply(text, "Alice", "+911", is_new_lead, tenant_id="t1")

    return types.SimpleNamespace(router=router, screens=screens, wa=wa_mod,
                                 constants=constants, nav=nav, reply=reply,
                                 state=lambda: holder["st"], make_state=make_state)


ENTRY_WORDS = ["hi", "hello", "start", "courses", "menu"]


class TestEntryPointsEnterNavigation:
    @pytest.mark.parametrize("word", ENTRY_WORDS)
    def test_entry_word_returns_a_list_message(self, env, word):
        _text, preset = env.reply(word)
        assert isinstance(preset, env.wa.ListMessage), word

    @pytest.mark.parametrize("word", ENTRY_WORDS)
    def test_entry_word_body_is_the_new_main_menu(self, env, word):
        text, _ = env.reply(word)
        assert "Main Menu" in text, word

    @pytest.mark.parametrize("word", ENTRY_WORDS)
    def test_entry_word_emits_navigation_ids(self, env, word):
        """The audit's core symptom: CAT:/ACT: ids must now appear."""
        _text, preset = env.reply(word)
        ids = [r["id"] for s in preset.sections for r in s["rows"]]
        assert "CAT:job" in ids and "ACT:DEMO" in ids, word

    @pytest.mark.parametrize("word", ENTRY_WORDS)
    def test_entry_word_carries_legacy_fallback(self, env, word):
        _text, preset = env.reply(word)
        assert "സ്വാഗതം" in preset.fallback_body
        assert preset.fallback_preset == "GOAL"

    def test_new_lead_enters_navigation(self, env):
        _text, preset = env.reply("anything", is_new_lead=True)
        assert isinstance(preset, env.wa.ListMessage)

    @pytest.mark.parametrize("word", ENTRY_WORDS)
    def test_entry_word_sets_canonical_stage(self, env, word):
        env.reply(word)
        assert env.state()["stage"] == "goal_selection"

    def test_greeting_still_stage_gated(self, env):
        """'hi' mid-conversation must not hijack an active flow."""
        _text, preset = env.reply("hi", stage="demo_time_ask")
        assert not isinstance(preset, env.wa.ListMessage)

    def test_menu_word_works_from_any_stage(self, env):
        for stage in ("new", "course_viewed", "demo_time_ask", "payment_pending"):
            _text, preset = env.reply("menu", stage=stage)
            assert isinstance(preset, env.wa.ListMessage), stage


class TestFlagMatrixEndToEnd:
    """OFF → legacy buttons · ON → List Message, through the real transport."""

    def _send(self, env, monkeypatch, word, flag_on):
        posts, texts = [], []
        monkeypatch.setattr(sys.modules["requests"], "post",
                            lambda url, headers=None, json=None, **k: (posts.append(json) or _Resp(200)))
        monkeypatch.setattr(env.wa, "_get_waba_credentials", lambda tid=None: ("p", "t"))
        monkeypatch.setattr(env.wa, "send_text",
                            lambda to, text, tenant_id=None: (texts.append(text) or _Resp(200)))
        flags = types.ModuleType("app.flags")
        flags.wa_list_messages_enabled = lambda: flag_on
        monkeypatch.setitem(sys.modules, "app.flags", flags)

        text, preset = env.reply(word)
        env.wa.send_reply("+911", text, preset)          # the webhook's exact call
        return posts, texts

    @pytest.mark.parametrize("word", ENTRY_WORDS)
    def test_flag_off_renders_legacy_buttons(self, env, monkeypatch, word):
        posts, _texts = self._send(env, monkeypatch, word, flag_on=False)
        assert posts, word
        assert posts[0]["interactive"]["type"] == "button"        # legacy buttons
        assert "സ്വാഗതം" in posts[0]["interactive"]["body"]["text"]

    @pytest.mark.parametrize("word", ENTRY_WORDS)
    def test_flag_on_sends_list_message(self, env, monkeypatch, word):
        posts, _texts = self._send(env, monkeypatch, word, flag_on=True)
        assert posts, word
        assert posts[0]["interactive"]["type"] == "list"          # real List Message
        ids = [r["id"] for s in posts[0]["interactive"]["action"]["sections"]
               for r in s["rows"]]
        assert "CAT:job" in ids

    def test_off_matches_pre_phase6_behaviour(self, env, monkeypatch):
        """The OFF rendering must equal the legacy welcome reply exactly."""
        legacy_text, legacy_preset = env.screens.legacy_main_menu_reply("Alice")
        posts, _ = self._send(env, monkeypatch, "hi", flag_on=False)
        assert posts[0]["interactive"]["body"]["text"] == legacy_text
        assert legacy_preset == "GOAL"


class TestBackwardCompatibilityPreserved:
    def test_legacy_numeric_goal_still_works(self, env):
        env.make_state(stage="goal_selection")
        _text, preset = env.router.smart_reply("1", "Alice", "+911", False, tenant_id="t1")
        assert not isinstance(preset, env.wa.ListMessage)
        assert env.state()["stage"] == "course_recommendation"

    def test_legacy_demo_keyword_still_works(self, env):
        env.make_state(stage="course_viewed", course="PGDCA")
        _text, preset = env.router.smart_reply("demo", "Alice", "+911", False, tenant_id="t1")
        assert [b["id"] for b in preset] == ["SLOT:morning", "SLOT:afternoon", "SLOT:evening"]

    def test_legacy_fees_keyword_still_works(self, env):
        env.make_state(stage="course_viewed", course="PGDCA")
        _text, preset = env.router.smart_reply("fees", "Alice", "+911", False, tenant_id="t1")
        assert preset == "FEES"

    def test_course_keyword_still_returns_card(self, env):
        env.make_state(stage="course_viewed")
        text, _ = env.router.smart_reply("sap", "Alice", "+911", False, tenant_id="t1")
        assert env.state()["course"] == "SAP Financial Accounting"

    def test_unknown_text_still_reaches_ai(self, env):
        sys.modules["app.services.ai_service"].gemini_reply.return_value = "AI reply"
        env.make_state(stage="course_viewed")
        text, _ = env.router.smart_reply("random question here", "Alice", "+911",
                                         False, tenant_id="t1")
        assert text == "AI reply"
