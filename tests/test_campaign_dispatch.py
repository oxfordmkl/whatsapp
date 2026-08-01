"""
Phase 8.2E.8 — ADR-024 Campaign V2 dispatch contract tests.

Covers the pieces send_automation() used to conflate:
  - _window_open(): the 24h check, replicated (not delegated) per ADR-024 R1
  - _template_components(): MessageTemplate.variables -> Meta components
  - send_campaign_message(): D1/D2/D4 — window-open text, window-closed
    template, window-closed-no-template explicit failure, never
    PendingMessage, never send_automation
  - resolve_campaign_template(): D3 — tenant/status/provider_id/channel gate

send_campaign_message and _window_open are loaded from campaign_worker.py via
file path (matching test_campaign_worker.py's stub pattern) so this module
never needs the full app package. resolve_campaign_template is exercised
against a real in-memory SQLite session, matching test_campaign_service.py's
harness, so D3's filter conditions are genuinely evaluated by SQLAlchemy.
"""
import importlib.util
import json
import os
import sys
import types
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(unique_name, relpath):
    path = os.path.join(_ROOT, relpath)
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub(name):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    return sys.modules[name]


for _n in [
    "app", "app.persistence", "app.persistence.campaign_repository",
    "app.extensions", "app.models",
    "app.services", "app.services.whatsapp_service", "app.services.log_service",
    "app.flags", "app.marketing", "app.marketing.campaign_service",
]:
    _stub(_n)

# from-imports inside send_campaign_message require both names to exist on the
# stub at call time; individual tests override whichever one they exercise.
sys.modules["app.services.whatsapp_service"].send_text = MagicMock()
sys.modules["app.services.whatsapp_service"].send_template = MagicMock()

sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: True
_svc_mod = _load("_p82e8_svc", "app/marketing/campaign_service.py")
sys.modules["app.marketing.campaign_service"].CampaignService = _svc_mod.CampaignService
sys.modules["app.marketing.campaign_service"].resolve_campaign_template = (
    _svc_mod.resolve_campaign_template
)

wkr = _load("_p82e8_worker", "app/marketing/campaign_worker.py")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _response(status=200, wa_id="wamid.CAMP1", text="OK"):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.json.return_value = {"messages": [{"id": wa_id}]}
    return r


def _patch_send_text(response):
    mock = MagicMock(return_value=response)
    sys.modules["app.services.whatsapp_service"].send_text = mock
    return mock


def _patch_send_template(response):
    mock = MagicMock(return_value=response)
    sys.modules["app.services.whatsapp_service"].send_template = mock
    return mock


def _patch_conversation_state(last_msg=None, exists=True):
    """last_msg is an ISO string, or None for 'no recent message'."""
    state = MagicMock()
    state.last_msg = last_msg
    mock_cs = MagicMock()
    mock_cs.query.filter_by.return_value.first.return_value = state if exists else None
    sys.modules["app.models"].ConversationState = mock_cs
    return mock_cs


def _make_template(variables='["name"]', provider_template_id="oxford_promo_v1",
                   language="en", status="approved", channel="whatsapp"):
    t = MagicMock()
    t.variables = variables
    t.provider_template_id = provider_template_id
    t.language = language
    t.status = status
    t.channel = channel
    return t


# ── _window_open ─────────────────────────────────────────────────────────────

class TestWindowOpen:
    def test_open_when_recent_message(self):
        from datetime import datetime
        _patch_conversation_state(last_msg=datetime.utcnow().isoformat())
        assert wkr._window_open("919000000001", "T1") is True

    def test_closed_when_no_state_row(self):
        _patch_conversation_state(exists=False)
        assert wkr._window_open("919000000001", "T1") is False

    def test_closed_when_last_msg_missing(self):
        _patch_conversation_state(last_msg=None)
        assert wkr._window_open("919000000001", "T1") is False

    def test_closed_when_older_than_24h(self):
        from datetime import datetime, timedelta
        old = (datetime.utcnow() - timedelta(hours=25)).isoformat()
        _patch_conversation_state(last_msg=old)
        assert wkr._window_open("919000000001", "T1") is False

    def test_closed_on_unparseable_last_msg(self):
        _patch_conversation_state(last_msg="not-a-date")
        assert wkr._window_open("919000000001", "T1") is False


# ── _template_components ────────────────────────────────────────────────────

class TestTemplateComponents:
    def test_fills_name_variable(self):
        t = _make_template(variables='["name"]')
        components = wkr._template_components(t, "Alice")
        assert components == [{"type": "body", "parameters": [{"type": "text", "text": "Alice"}]}]

    def test_unknown_variable_becomes_empty_string(self):
        t = _make_template(variables='["course"]')
        components = wkr._template_components(t, "Alice")
        assert components == [{"type": "body", "parameters": [{"type": "text", "text": ""}]}]

    def test_multiple_variables_ordered(self):
        t = _make_template(variables='["course", "name"]')
        components = wkr._template_components(t, "Bob")
        params = components[0]["parameters"]
        assert params == [{"type": "text", "text": ""}, {"type": "text", "text": "Bob"}]

    def test_no_variables_returns_none(self):
        t = _make_template(variables="[]")
        assert wkr._template_components(t, "Alice") is None

    def test_null_variables_returns_none(self):
        t = _make_template(variables=None)
        assert wkr._template_components(t, "Alice") is None

    def test_malformed_json_returns_none(self):
        t = _make_template(variables="not-json")
        assert wkr._template_components(t, "Alice") is None


# ── send_campaign_message — ADR-024 D1/D2/D4 ──────────────────────────────────

class TestSendCampaignMessage:
    def test_window_open_sends_text_with_campaign_body(self):
        from datetime import datetime
        _patch_conversation_state(last_msg=datetime.utcnow().isoformat())
        text_mock = _patch_send_text(_response(200, "wamid.T1"))

        result = wkr.send_campaign_message(
            "919000000001", "Alice", "T1", "Hello from campaign", None
        )

        text_mock.assert_called_once_with("919000000001", "Hello from campaign", tenant_id="T1")
        assert result == {"outcome": "sent", "wa_message_id": "wamid.T1", "send_type": "text"}

    def test_window_open_api_failure_is_reported_failed(self):
        from datetime import datetime
        _patch_conversation_state(last_msg=datetime.utcnow().isoformat())
        _patch_send_text(_response(500, text="Internal Server Error"))

        result = wkr.send_campaign_message("919000000001", "Alice", "T1", "Hi", None)

        assert result["outcome"] == "failed"
        assert "500" in result["reason"]

    def test_window_closed_with_template_sends_template(self):
        _patch_conversation_state(exists=False)
        template = _make_template(provider_template_id="oxford_promo_v1", language="ml")
        template_mock = _patch_send_template(_response(200, "wamid.TPL1"))

        result = wkr.send_campaign_message(
            "919000000001", "Alice", "T1", "campaign body", template
        )

        template_mock.assert_called_once()
        args, kwargs = template_mock.call_args
        assert args[0] == "919000000001"
        assert args[1] == "oxford_promo_v1"
        assert kwargs["lang"] == "ml"
        assert kwargs["tenant_id"] == "T1"
        assert result == {"outcome": "sent", "wa_message_id": "wamid.TPL1", "send_type": "template"}

    def test_window_closed_no_template_fails_without_any_provider_call(self):
        """ADR-024 D2: the core contract — no silent substitution, ever."""
        _patch_conversation_state(exists=False)
        text_mock = _patch_send_text(_response(200))
        template_mock = _patch_send_template(_response(200))

        result = wkr.send_campaign_message("919000000001", "Alice", "T1", "body", None)

        text_mock.assert_not_called()
        template_mock.assert_not_called()
        assert result["outcome"] == "failed"
        assert "window closed" in result["reason"]
        assert "template" in result["reason"]

    def test_window_closed_template_failure_is_reported_failed(self):
        _patch_conversation_state(exists=False)
        template = _make_template()
        _patch_send_template(_response(400, text="template rejected"))

        result = wkr.send_campaign_message("919000000001", "Alice", "T1", "body", template)

        assert result["outcome"] == "failed"
        assert "400" in result["reason"]

    def test_never_calls_send_automation(self):
        """Structural guard: send_campaign_message must not reference send_automation."""
        import inspect
        src = inspect.getsource(wkr.send_campaign_message)
        assert "send_automation" not in src

    def test_never_touches_pending_message(self):
        import inspect
        src = inspect.getsource(wkr.send_campaign_message)
        assert "PendingMessage" not in src


# ── resolve_campaign_template — ADR-024 D3 ────────────────────────────────────

_Base = declarative_base()


class _MessageTemplate(_Base):
    __tablename__ = "message_templates"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(String(36), nullable=False)
    status = Column(String(20), nullable=False, default="draft")
    provider_template_id = Column(String(200))
    channel = Column(String(20), nullable=False, default="whatsapp")
    language = Column(String(10), nullable=False, default="en")
    variables = Column(String(500), default="[]")


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    _Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _insert(session, **kw):
    kw.setdefault("tenant_id", "T1")
    kw.setdefault("status", "approved")
    kw.setdefault("provider_template_id", "meta_tpl_1")
    kw.setdefault("channel", "whatsapp")
    kw.setdefault("language", "en")
    kw.setdefault("variables", "[]")
    row = _MessageTemplate(**kw)
    session.add(row)
    session.commit()
    return row


class TestResolveCampaignTemplate:
    def test_resolves_approved_tenant_owned_template(self, session):
        row = _insert(session)
        result = _svc_mod.resolve_campaign_template("T1", row.id, session=session, model=_MessageTemplate)
        assert result is not None
        assert result.id == row.id

    def test_none_when_missing_tenant_id(self, session):
        row = _insert(session)
        assert _svc_mod.resolve_campaign_template(None, row.id, session=session, model=_MessageTemplate) is None

    def test_none_when_missing_template_id(self, session):
        assert _svc_mod.resolve_campaign_template("T1", None, session=session, model=_MessageTemplate) is None

    def test_none_for_wrong_tenant(self, session):
        row = _insert(session, tenant_id="T1")
        assert _svc_mod.resolve_campaign_template("T2", row.id, session=session, model=_MessageTemplate) is None

    def test_none_when_not_approved(self, session):
        for status in ("draft", "approval_pending", "rejected", "archived"):
            row = _insert(session, status=status)
            assert _svc_mod.resolve_campaign_template(
                "T1", row.id, session=session, model=_MessageTemplate
            ) is None, f"status={status} must not resolve"

    def test_none_when_provider_template_id_missing(self, session):
        row = _insert(session, provider_template_id=None)
        assert _svc_mod.resolve_campaign_template("T1", row.id, session=session, model=_MessageTemplate) is None

    def test_none_when_channel_not_whatsapp(self, session):
        row = _insert(session, channel="sms")
        assert _svc_mod.resolve_campaign_template("T1", row.id, session=session, model=_MessageTemplate) is None

    def test_none_when_template_id_does_not_exist(self, session):
        assert _svc_mod.resolve_campaign_template("T1", 99999, session=session, model=_MessageTemplate) is None
