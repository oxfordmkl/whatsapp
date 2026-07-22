"""
Phase 1.6.2 — unit tests for the WhatsApp List Message transport.

whatsapp_service.py imports app.config / app.bot.constants at module top and
starts a token-validation thread on import, so every one of those collaborators
(including `requests`) is injected as a fake via monkeypatch before the module is
path-loaded. Nothing touches the network or the app bootstrap.

Covers the pure payload builder (shape + Meta limit enforcement + row cap) and
send_list()'s flag gating and fallbacks.
"""
import importlib.util
import os
import sys
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Resp:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {}


@pytest.fixture
def wa(monkeypatch):
    """Path-load whatsapp_service with all collaborators faked."""
    calls = {"posts": [], "texts": []}
    flag = {"on": True}
    post_result = {"resp": _Resp(200)}

    fake_requests = types.ModuleType("requests")

    def _post(url, headers=None, json=None, **kw):
        calls["posts"].append({"url": url, "json": json})
        return post_result["resp"]

    def _get(url, headers=None, **kw):
        return _Resp(200)

    fake_requests.post = _post
    fake_requests.get = _get
    fake_requests.Response = _Resp
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    cfg = types.ModuleType("app.config")
    cfg.ACCESS_TOKEN = "tok"
    cfg.PHONE_NUMBER_ID = "phone123"
    cfg.WHATSAPP_API_URL = "https://graph.facebook.com/v19.0/phone123/messages"
    monkeypatch.setitem(sys.modules, "app.config", cfg)

    consts = types.ModuleType("app.bot.constants")
    consts.BUTTON_PRESETS = {"COURSE": [{"id": "DEMO", "title": "Demo"}]}
    monkeypatch.setitem(sys.modules, "app.bot.constants", consts)

    perf = types.ModuleType("app.perf")
    perf.mark = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "app.perf", perf)

    flags = types.ModuleType("app.flags")
    flags.wa_list_messages_enabled = lambda: flag["on"]
    monkeypatch.setitem(sys.modules, "app.flags", flags)

    spec = importlib.util.spec_from_file_location(
        "_wa_service", os.path.join(_ROOT, "app/services/whatsapp_service.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Bypass tenant/DB credential lookup — transport under test, not auth.
    monkeypatch.setattr(mod, "_get_waba_credentials", lambda tid=None: ("phone123", "tok"))

    def _send_text(to, text, tenant_id=None):
        calls["texts"].append(text)
        return _Resp(200)

    monkeypatch.setattr(mod, "send_text", _send_text)

    return types.SimpleNamespace(mod=mod, calls=calls, flag=flag, post_result=post_result)


SECTIONS = [
    {"title": "Courses", "rows": [
        {"id": "CRS:1", "title": "PGDCA", "description": "12 Months • ₹15,999"},
        {"id": "CRS:4", "title": "Python", "description": "3 Months • ₹4,499"},
    ]},
    {"title": "Navigation", "rows": [
        {"id": "NAV:MENU", "title": "🏠 Main Menu"},
    ]},
]


class TestBuildListPayload:
    def test_payload_shape(self, wa):
        p = wa.mod.build_list_payload("+1", "body", "📋 Select", SECTIONS)
        assert p["type"] == "interactive"
        assert p["to"] == "+1"
        assert p["interactive"]["type"] == "list"
        assert p["interactive"]["body"]["text"] == "body"
        assert p["interactive"]["action"]["button"] == "📋 Select"
        assert len(p["interactive"]["action"]["sections"]) == 2

    def test_rows_preserve_ids(self, wa):
        p = wa.mod.build_list_payload("+1", "b", "Select", SECTIONS)
        ids = [r["id"] for s in p["interactive"]["action"]["sections"] for r in s["rows"]]
        assert ids == ["CRS:1", "CRS:4", "NAV:MENU"]

    def test_description_omitted_when_absent(self, wa):
        p = wa.mod.build_list_payload("+1", "b", "Select", SECTIONS)
        nav_row = p["interactive"]["action"]["sections"][1]["rows"][0]
        assert "description" not in nav_row

    def test_limits_enforced(self, wa):
        long_sections = [{"title": "T" * 50, "rows": [
            {"id": "CRS:1", "title": "X" * 50, "description": "D" * 200},
        ]}]
        p = wa.mod.build_list_payload("+1", "B" * 2000, "L" * 50, long_sections,
                                      header="H" * 200, footer="F" * 200)
        act = p["interactive"]["action"]
        assert len(act["button"]) <= wa.mod.LIST_BUTTON_LABEL_MAX
        assert len(act["sections"][0]["title"]) <= wa.mod.LIST_SECTION_TITLE_MAX
        row = act["sections"][0]["rows"][0]
        assert len(row["title"]) <= wa.mod.LIST_ROW_TITLE_MAX
        assert len(row["description"]) <= wa.mod.LIST_ROW_DESC_MAX
        assert len(p["interactive"]["body"]["text"]) <= wa.mod.LIST_BODY_MAX
        assert len(p["interactive"]["header"]["text"]) <= wa.mod.LIST_HEADER_MAX
        assert len(p["interactive"]["footer"]["text"]) <= wa.mod.LIST_FOOTER_MAX

    def test_total_rows_capped_at_ten(self, wa):
        big = [{"title": "S", "rows": [{"id": f"CRS:{i}", "title": f"c{i}"} for i in range(25)]}]
        p = wa.mod.build_list_payload("+1", "b", "Select", big)
        total = sum(len(s["rows"]) for s in p["interactive"]["action"]["sections"])
        assert total == wa.mod.LIST_MAX_ROWS

    def test_empty_sections_dropped(self, wa):
        p = wa.mod.build_list_payload("+1", "b", "Select", [{"title": "Empty", "rows": []}])
        assert p["interactive"]["action"]["sections"] == []

    def test_header_footer_optional(self, wa):
        p = wa.mod.build_list_payload("+1", "b", "Select", SECTIONS)
        assert "header" not in p["interactive"]
        assert "footer" not in p["interactive"]


class TestSendListFlagGating:
    def test_flag_off_falls_back_to_text(self, wa):
        wa.flag["on"] = False
        wa.mod.send_list("+1", "Pick one", "📋 Select", SECTIONS)
        assert wa.calls["posts"] == []          # no interactive API call
        assert len(wa.calls["texts"]) == 1
        body = wa.calls["texts"][0]
        assert "Pick one" in body and "PGDCA" in body and "🏠 Main Menu" in body

    def test_text_fallback_has_no_numeric_affordance(self, wa):
        """Numbers would collide with the legacy positional handlers."""
        wa.flag["on"] = False
        wa.mod.send_list("+1", "Pick", "Select", SECTIONS)
        for line in wa.calls["texts"][0].splitlines():
            assert not line.strip().startswith(("1.", "2.", "3."))

    def test_flag_on_sends_interactive_list(self, wa):
        wa.flag["on"] = True
        wa.mod.send_list("+1", "Pick one", "📋 Select", SECTIONS)
        assert len(wa.calls["posts"]) == 1
        assert wa.calls["texts"] == []
        assert wa.calls["posts"][0]["json"]["interactive"]["type"] == "list"

    def test_api_failure_falls_back_to_text(self, wa):
        wa.flag["on"] = True
        wa.post_result["resp"] = _Resp(400, "bad request")
        wa.mod.send_list("+1", "Pick one", "📋 Select", SECTIONS)
        assert len(wa.calls["posts"]) == 1
        assert len(wa.calls["texts"]) == 1      # degraded, never lost


class TestExistingTransportUntouched:
    def test_send_reply_without_preset_still_text(self, wa):
        wa.mod.send_reply("+1", "hello", None)
        assert wa.calls["texts"] == ["hello"]
        assert wa.calls["posts"] == []

    def test_send_reply_with_preset_still_uses_buttons(self, wa):
        wa.mod.send_reply("+1", "hello", "COURSE")
        assert len(wa.calls["posts"]) == 1
        assert wa.calls["posts"][0]["json"]["interactive"]["type"] == "button"
