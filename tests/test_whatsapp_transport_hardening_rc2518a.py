"""Phase RC2.5.18-A: WhatsApp transport and logging hardening.

WHY
---
Two defects had to close before ANY OTP caller could exist:

  1. None of the seven Graph API calls passed a timeout. A stalled Meta
     connection held its thread forever; on a request path it held the ONLY
     sync gunicorn worker until gunicorn's 30s WORKER TIMEOUT killed it, taking
     the scheduler, campaign and retention threads with it.
  2. The send path logged full phone numbers, and on a template failure logged
     the entire payload. For an AUTHENTICATION template the payload IS the
     one-time code, so a single Meta rejection would have written a live
     credential to production logs.

THE CONTRACT THAT MUST NOT MOVE
-------------------------------
The four send functions have always RETURNED a response. broadcast.py's two
per-number loops have no try/except around them, so a newly RAISED timeout
would abort a broadcast part-way and skip its BROADCAST_SEND audit row. A
transport failure therefore returns a non-200 TransportFailure instead. The
TestBroadcastAudit class drives the real routes to prove it.

NO NETWORK
----------
whatsapp_service starts a token-validation thread at IMPORT time, which would
otherwise issue a real GET to graph.facebook.com. An unroutable local proxy is
set below BEFORE the app is imported, so nothing in this file can reach Meta
even by accident. Every send is additionally routed through a fake transport.

The shared `requests` module is never patched. Only whatsapp_service's own
reference to it is replaced -- the same lesson as RC2.5.16's _FakeSecrets:
patching an attribute on a shared module rewires it for every other consumer.
"""
import ast
import importlib.util
import logging
import os
import re
import sys
import tempfile
import types

import pytest

# ── Network guard: MUST precede every app import ──
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
os.environ["NO_PROXY"] = ""

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc2518a_wa_hardening.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc2518a-admin-key")
os.environ.setdefault("SECRET_KEY", "rc2518a-secret-key")
# Assignment, not setdefault -- see test_outbound_tenant_binding_rc241.py:46.
os.environ["BROADCAST_API_KEY"] = "rc2518a-broadcast-key"
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ["PRIMARY_TENANT_ID"] = "t-ox"
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import requests as _real_requests                                       # noqa: E402

import app.services.followup_service as _fs                             # noqa: E402
import app.marketing.campaign_worker as _cw                             # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import AuditLog, Tenant                                 # noqa: E402
from app.services import whatsapp_service as wa                         # noqa: E402

OX = "t-ox"
KEY = "rc2518a-broadcast-key"
DEST = "919847312534"
DEST2 = "919999000111"
CODE = "482913"
TOKEN = "tok-DO-NOT-LOG-7f3a9c"
WA_PY = os.path.join(ROOT, "app", "services", "whatsapp_service.py")

_APP = create_app()
_APP.config["TESTING"] = True
_APP.config["PRIMARY_TENANT_ID"] = OX

_OWN = {k: v for k, v in sys.modules.items()
        if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN)
    yield


def _copy_code_components(code=CODE):
    """The exact shape oxford_verification_code requires (RC2.5.18 audit)."""
    return [
        {"type": "body", "parameters": [{"type": "text", "text": code}]},
        {"type": "button", "sub_type": "url", "index": "0",
         "parameters": [{"type": "text", "text": code}]},
    ]


# ── Fake transport ──────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status_code=200, body=None, text=None, json_raises=False):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text if text is not None else "{}"
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("not json")
        return self._body


class FakeRequests:
    """Replaces whatsapp_service's `requests` reference only.

    `exceptions` is the REAL requests.exceptions, so real Timeout /
    ConnectionError classes are raised and caught exactly as in production.
    """
    exceptions = _real_requests.exceptions

    def __init__(self):
        self.calls = []
        self.respond = lambda method, url, kw: _Resp(200)

    def post(self, url, **kw):
        self.calls.append(("post", url, kw))
        return self.respond("post", url, kw)

    def get(self, url, **kw):
        self.calls.append(("get", url, kw))
        return self.respond("get", url, kw)


@pytest.fixture()
def fake(monkeypatch):
    f = FakeRequests()
    monkeypatch.setattr(wa, "requests", f)
    monkeypatch.setattr(wa, "_get_waba_credentials",
                        lambda tenant_id=None: ("pn-1", TOKEN))
    return f


def _raise(exc):
    def _r(method, url, kw):
        raise exc
    return _r


def _code_only(path):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef,
                          ast.AsyncFunctionDef)):
            d = ast.get_docstring(n, clean=False)
            if d:
                src = src.replace(d, "")
    return "\n".join(l.split("#", 1)[0] for l in src.splitlines())


# ── 1. Every Graph API call is bounded ──────────────────────────────────────

class TestTimeoutCoverage:

    def test_every_requests_call_in_source_passes_timeout(self):
        """Structural: a FUTURE unbounded call fails here, not in production."""
        tree = ast.parse(open(WA_PY, encoding="utf-8").read())
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and getattr(n.func.value, "id", "") == "requests"]
        assert calls, "expected requests calls in whatsapp_service"
        for c in calls:
            kws = {k.arg for k in c.keywords}
            assert "timeout" in kws, f"line {c.lineno}: requests.{c.func.attr} has no timeout"

    @pytest.mark.parametrize("entry", [
        "send_text", "send_interactive", "send_list", "send_template",
        "fetch_templates", "upload_media", "validate_token",
    ])
    def test_each_of_the_seven_entry_points_passes_timeout(self, entry, fake,
                                                           monkeypatch):
        """Behavioural: the seven original call paths, each driven for real."""
        import app.config as cfg
        monkeypatch.setattr(cfg, "WABA_ID", "waba-1", raising=False)
        monkeypatch.setattr(cfg, "ACCESS_TOKEN", TOKEN, raising=False)
        monkeypatch.setattr(wa, "fetch_templates", wa.fetch_templates)
        import app.flags as flags
        monkeypatch.setattr(flags, "wa_list_messages_enabled", lambda: True)

        if entry == "fetch_templates":
            fake.respond = lambda m, u, k: _Resp(200, body={"data": []})
        if entry == "upload_media":
            fake.respond = lambda m, u, k: _Resp(200, body={"id": "media-1"})

        with _APP.app_context():
            {
                "send_text": lambda: wa.send_text(DEST, "hi", tenant_id=OX),
                "send_interactive": lambda: wa.send_interactive(
                    DEST, "hi", [{"id": "a", "title": "A"}], tenant_id=OX),
                "send_list": lambda: wa.send_list(
                    DEST, "hi", "Pick", [{"title": "S", "rows": [
                        {"id": "r", "title": "R"}]}], tenant_id=OX),
                "send_template": lambda: wa.send_template(
                    DEST, "t", tenant_id=OX),
                "fetch_templates": lambda: wa.fetch_templates(),
                "upload_media": lambda: wa.upload_media(
                    b"x", "f.png", "image/png", tenant_id=OX),
                "validate_token": lambda: wa.validate_token(),
            }[entry]()

        assert fake.calls, f"{entry} made no request"
        expected = (cfg.WHATSAPP_CONNECT_TIMEOUT_SECONDS,
                    cfg.WHATSAPP_READ_TIMEOUT_SECONDS)
        for method, url, kw in fake.calls:
            assert kw.get("timeout") == expected, (entry, kw)

    def test_timeout_is_read_from_config_at_call_time(self, fake, monkeypatch):
        import app.config as cfg
        monkeypatch.setattr(cfg, "WHATSAPP_CONNECT_TIMEOUT_SECONDS", 1.5)
        monkeypatch.setattr(cfg, "WHATSAPP_READ_TIMEOUT_SECONDS", 4.0)
        with _APP.app_context():
            wa.send_text(DEST, "hi", tenant_id=OX)
        assert fake.calls[-1][2]["timeout"] == (1.5, 4.0)

    def test_config_follows_the_email_timeout_pattern(self):
        # RC2.5.18-A-FIX1: a (connect, read) pair replaces the single scalar.
        src = open(os.path.join(ROOT, "app", "config.py"), encoding="utf-8").read()
        assert ('WHATSAPP_CONNECT_TIMEOUT_SECONDS = float(os.environ.get('
                '"WHATSAPP_CONNECT_TIMEOUT_SECONDS", "3.05"))') in src
        assert ('WHATSAPP_READ_TIMEOUT_SECONDS = float(os.environ.get('
                '"WHATSAPP_READ_TIMEOUT_SECONDS", "8"))') in src
        assert "WHATSAPP_TIMEOUT_SECONDS" not in src

    # REMOVED BY RC2.5.18-A-FIX1: test_worst_degradation_chain_stays_under_
    # gunicorn_timeout asserted `3 * timeout < 30`. That encoded a FALSE claim:
    # a requests timeout is per phase (connect, then each gap between reads),
    # not a total, so no multiple of it bounds wall-clock time. The final audit
    # proved it with a trickling server. The replacement is behavioural, in
    # test_whatsapp_ambiguous_timeout_rc2518a_fix1.py: a transport failure
    # never triggers a second call, and the timeout's real per-phase semantics
    # are measured against a local server rather than assumed.

    def test_fallbacks_match_the_config_defaults(self):
        src = open(os.path.join(ROOT, "app", "config.py"), encoding="utf-8").read()
        c = re.search(r'"WHATSAPP_CONNECT_TIMEOUT_SECONDS", "([\d.]+)"', src)
        r = re.search(r'"WHATSAPP_READ_TIMEOUT_SECONDS", "([\d.]+)"', src)
        assert float(c.group(1)) == wa._FALLBACK_CONNECT_TIMEOUT_SECONDS
        assert float(r.group(1)) == wa._FALLBACK_READ_TIMEOUT_SECONDS


# ── 2. Transport failures return, they do not raise ─────────────────────────

class TestTransportFailure:

    @pytest.mark.parametrize("exc", [
        _real_requests.exceptions.Timeout("t"),
        _real_requests.exceptions.ConnectTimeout("t"),
        _real_requests.exceptions.ReadTimeout("t"),
        _real_requests.exceptions.ConnectionError("c"),
        _real_requests.exceptions.RequestException("r"),
    ], ids=["Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError",
            "RequestException"])
    def test_send_template_returns_a_failure_result(self, exc, fake):
        fake.respond = _raise(exc)
        with _APP.app_context():
            r = wa.send_template(DEST, "t", tenant_id=OX)
        assert isinstance(r, wa.TransportFailure)
        assert r.status_code == wa.TRANSPORT_FAILURE_STATUS == 599
        assert r.status_code != 200

    @pytest.mark.parametrize("fn", ["send_text", "send_interactive", "send_list"])
    def test_other_sends_also_return_rather_than_raise(self, fn, fake,
                                                       monkeypatch):
        import app.flags as flags
        monkeypatch.setattr(flags, "wa_list_messages_enabled", lambda: True)
        fake.respond = _raise(_real_requests.exceptions.Timeout("t"))
        with _APP.app_context():
            r = {
                "send_text": lambda: wa.send_text(DEST, "hi", tenant_id=OX),
                "send_interactive": lambda: wa.send_interactive(
                    DEST, "hi", [{"id": "a", "title": "A"}], tenant_id=OX),
                "send_list": lambda: wa.send_list(
                    DEST, "hi", "Pick", [{"title": "S", "rows": [
                        {"id": "r", "title": "R"}]}], tenant_id=OX),
            }[fn]()
        assert r.status_code == 599

    def test_failure_result_honours_the_caller_contract(self, fake):
        fake.respond = _raise(_real_requests.exceptions.Timeout("t"))
        with _APP.app_context():
            r = wa.send_template(DEST, "t", tenant_id=OX)
        # Callers read exactly these three things.
        assert isinstance(r.status_code, int)
        assert isinstance(r.text, str)
        assert r.json()["error"]["type"] == "transport"
        assert r.ok is False

    def test_failure_text_carries_the_class_name_only(self, fake):
        # A requests exception message can embed the URL; campaign_worker
        # copies .text into failure reasons and logs.
        fake.respond = _raise(_real_requests.exceptions.ConnectionError(
            f"https://graph.facebook.com/v21.0/pn-1/messages to={DEST} Bearer {TOKEN}"))
        with _APP.app_context():
            r = wa.send_template(DEST, "t", tenant_id=OX)
        # RC2.5.18-A-FIX1: the text now also carries the delivery state. A bare
        # ConnectionError is AMBIGUOUS -- it may follow a transmitted request.
        assert r.text == "transport failure: ConnectionError (ambiguous)"
        assert DEST not in r.text and TOKEN not in r.text

    def test_a_non_transport_error_still_propagates(self, fake):
        # The catch is deliberately narrow: a programming error must not be
        # disguised as a network blip.
        fake.respond = _raise(KeyError("bug"))
        with _APP.app_context(), pytest.raises(KeyError):
            wa.send_template(DEST, "t", tenant_id=OX)

    def test_campaign_worker_classifies_it_transient(self):
        from app.marketing.campaign_worker import (
            _classify_provider_failure, FAILURE_TRANSIENT)
        # Same classification its `except Exception` branch gave a raised
        # transport error before this phase: retry behaviour is unchanged.
        assert _classify_provider_failure(599) == FAILURE_TRANSIENT

    def test_fetch_templates_keeps_its_valueerror_contract(self, fake,
                                                          monkeypatch):
        import app.config as cfg
        monkeypatch.setattr(cfg, "WABA_ID", "waba-1", raising=False)
        monkeypatch.setattr(cfg, "ACCESS_TOKEN", TOKEN, raising=False)
        fake.respond = _raise(_real_requests.exceptions.Timeout("t"))
        with pytest.raises(ValueError, match="transport Timeout"):
            wa.fetch_templates()

    def test_upload_media_keeps_its_valueerror_contract(self, fake):
        fake.respond = _raise(_real_requests.exceptions.ConnectionError("c"))
        with _APP.app_context(), pytest.raises(ValueError,
                                               match="transport ConnectionError"):
            wa.upload_media(b"x", "f.png", "image/png", tenant_id=OX)

    def test_validate_token_transport_failure_leaves_status_unknown(self, fake,
                                                                  monkeypatch):
        monkeypatch.setattr(wa, "token_status", "unknown")
        fake.respond = _raise(_real_requests.exceptions.Timeout("t"))
        wa.validate_token()          # must not raise
        # NOT "invalid": a timeout says nothing about the token, and /health
        # reports this value.
        assert wa.token_status == "unknown"


# ── 3. Non-200 behaviour is unchanged ───────────────────────────────────────

class TestNon200Compatibility:

    @pytest.mark.parametrize("status", [200, 400, 401, 429, 500, 503])
    def test_send_template_returns_the_very_same_response(self, status, fake):
        resp = _Resp(status, body={"error": {"code": 1}})
        fake.respond = lambda m, u, k: resp
        with _APP.app_context():
            assert wa.send_template(DEST, "t", tenant_id=OX) is resp

    def test_interactive_non200_still_falls_back_to_text(self, fake):
        seq = iter([_Resp(400), _Resp(200)])
        fake.respond = lambda m, u, k: next(seq)
        with _APP.app_context():
            r = wa.send_interactive(DEST, "hi", [{"id": "a", "title": "A"}],
                                    tenant_id=OX)
        assert r.status_code == 200
        assert [c[2]["json"]["type"] for c in fake.calls] == ["interactive",
                                                              "text"]

    def test_payload_shape_is_unchanged(self, fake):
        with _APP.app_context():
            wa.send_template(DEST, "oxford_verification_code", lang="en",
                             components=_copy_code_components(), tenant_id=OX)
        payload = fake.calls[-1][2]["json"]
        assert payload == {
            "messaging_product": "whatsapp", "to": DEST, "type": "template",
            "template": {"name": "oxford_verification_code",
                         "language": {"code": "en"},
                         "components": _copy_code_components()},
        }
        assert fake.calls[-1][2]["headers"]["Authorization"] == f"Bearer {TOKEN}"

    def test_send_template_signature_is_unchanged(self):
        import inspect
        assert list(inspect.signature(wa.send_template).parameters) == [
            "to", "template", "lang", "components", "tenant_id"]


# ── 4. Malformed Meta responses stay safe ───────────────────────────────────

class TestMalformedMeta:

    @pytest.mark.parametrize("resp", [
        _Resp(400, json_raises=True, text="<html>bad gateway</html>"),
        _Resp(400, body=["not", "a", "dict"]),
        _Resp(400, body={"error": "just a string"}),
        _Resp(400, body={"error": None}),
        _Resp(400, body={}),
        _Resp(500, json_raises=True, text=""),
    ], ids=["non-json", "list", "error-str", "error-none", "empty", "empty-text"])
    def test_send_template_never_raises(self, resp, fake, caplog):
        fake.respond = lambda m, u, k: resp
        with _APP.app_context(), caplog.at_level(logging.INFO):
            r = wa.send_template(DEST, "t", components=_copy_code_components(),
                                 tenant_id=OX)
        assert r is resp
        assert f"HTTP {resp.status_code}" in caplog.text
        assert CODE not in caplog.text and DEST not in caplog.text


# ── 5. Logs: no destination, no code, no token ──────────────────────────────

def _masked(dest=DEST):
    from app.services.phone_service import mask_destination
    return mask_destination(dest)


class TestLogging:

    @pytest.mark.parametrize("status", [200, 400])
    @pytest.mark.parametrize("fn", ["send_text", "send_interactive", "send_list"])
    def test_sends_never_log_the_full_destination(self, fn, status, fake,
                                                  caplog, monkeypatch):
        import app.flags as flags
        monkeypatch.setattr(flags, "wa_list_messages_enabled", lambda: True)
        fake.respond = lambda m, u, k: _Resp(status)
        with _APP.app_context(), caplog.at_level(logging.INFO):
            {
                "send_text": lambda: wa.send_text(DEST, "hi", tenant_id=OX),
                "send_interactive": lambda: wa.send_interactive(
                    DEST, "hi", [{"id": "a", "title": "A"}], tenant_id=OX),
                "send_list": lambda: wa.send_list(
                    DEST, "hi", "Pick", [{"title": "S", "rows": [
                        {"id": "r", "title": "R"}]}], tenant_id=OX),
            }[fn]()
        assert DEST not in caplog.text
        assert _masked() in caplog.text

    def test_authentication_template_failure_never_logs_the_code(self, fake,
                                                                 caplog):
        """The defect this phase exists for.

        Meta's error fields are made to ECHO the code and the destination, the
        worst realistic case: an error describing a bad parameter may quote it.
        """
        fake.respond = lambda m, u, k: _Resp(400, body={"error": {
            "message": f"(#132000) Number of parameters does not match: {CODE}",
            "type": "OAuthException", "code": 132000,
            "error_subcode": 2494010,
            "error_data": {"details": f"body param {CODE} for {DEST}; "
                                      f"url otp{CODE}"},
        }}, text=f'{{"error":"{CODE} {DEST}"}}')
        with _APP.app_context(), caplog.at_level(logging.INFO):
            wa.send_template(DEST, "oxford_verification_code", lang="en",
                             components=_copy_code_components(), tenant_id=OX)
        log = caplog.text
        assert CODE not in log, "the one-time code reached a log"
        assert DEST not in log
        assert "<redacted>" in log
        # Diagnostics that must survive.
        assert "oxford_verification_code" in log
        assert "HTTP 400" in log
        assert "meta.code=132000" in log and "subcode=2494010" in log
        assert "type=OAuthException" in log
        assert "Number of parameters does not match" in log
        assert _masked() in log
        assert "sent.components=body:1, button[url]:1" in log

    def test_code_inside_a_longer_token_is_still_removed(self, fake, caplog):
        # The COPY_CODE button URL carries "otp<code>"; the code is scrubbed
        # even when it is a substring.
        fake.respond = lambda m, u, k: _Resp(400, body={"error": {
            "message": f"bad url https://x/?code=otp{CODE}"}})
        with _APP.app_context(), caplog.at_level(logging.INFO):
            wa.send_template(DEST, "oxford_verification_code",
                             components=_copy_code_components(), tenant_id=OX)
        assert CODE not in caplog.text

    def test_non_json_error_body_is_scrubbed(self, fake, caplog):
        fake.respond = lambda m, u, k: _Resp(
            400, json_raises=True, text=f"gateway error code={CODE} to={DEST}")
        with _APP.app_context(), caplog.at_level(logging.INFO):
            wa.send_template(DEST, "oxford_verification_code",
                             components=_copy_code_components(), tenant_id=OX)
        assert CODE not in caplog.text and DEST not in caplog.text
        assert "meta.body=" in caplog.text

    def test_raw_body_is_not_logged_when_meta_json_parsed(self, fake, caplog):
        fake.respond = lambda m, u, k: _Resp(
            400, body={"error": {"code": 1}}, text="RAW-BODY-MARKER")
        with _APP.app_context(), caplog.at_level(logging.INFO):
            wa.send_template(DEST, "t", tenant_id=OX)
        assert "RAW-BODY-MARKER" not in caplog.text

    def test_transport_failure_never_logs_the_code(self, fake, caplog):
        fake.respond = _raise(_real_requests.exceptions.ReadTimeout(
            f"read timed out; payload code={CODE} to={DEST}"))
        with _APP.app_context(), caplog.at_level(logging.INFO):
            wa.send_template(DEST, "oxford_verification_code",
                             components=_copy_code_components(), tenant_id=OX)
        assert CODE not in caplog.text and DEST not in caplog.text
        assert "transport failure (ReadTimeout, ambiguous)" in caplog.text

    def test_successful_authentication_send_logs_nothing_sensitive(self, fake,
                                                                   caplog):
        with _APP.app_context(), caplog.at_level(logging.DEBUG):
            wa.send_template(DEST, "oxford_verification_code",
                             components=_copy_code_components(), tenant_id=OX)
        assert CODE not in caplog.text and DEST not in caplog.text

    def test_marketing_parameter_values_are_scrubbed_too(self, fake, caplog):
        # A customer's name is PII as well; scrubbing is structural, not
        # special-cased to authentication templates.
        comps = [{"type": "body", "parameters": [{"type": "text",
                                                  "text": "Anjali Menon"}]}]
        fake.respond = lambda m, u, k: _Resp(400, body={"error": {
            "message": "bad param Anjali Menon"}})
        with _APP.app_context(), caplog.at_level(logging.INFO):
            wa.send_template(DEST, "oxford_re_engagement_v1",
                             components=comps, tenant_id=OX)
        assert "Anjali Menon" not in caplog.text

    def test_access_token_never_reaches_a_log(self, fake, caplog, monkeypatch):
        import app.config as cfg
        import app.flags as flags
        monkeypatch.setattr(cfg, "WABA_ID", "waba-1", raising=False)
        monkeypatch.setattr(cfg, "ACCESS_TOKEN", TOKEN, raising=False)
        monkeypatch.setattr(wa, "ACCESS_TOKEN", TOKEN)
        monkeypatch.setattr(flags, "wa_list_messages_enabled", lambda: True)
        with _APP.app_context(), caplog.at_level(logging.DEBUG):
            for respond in (
                lambda m, u, k: _Resp(400, body={"error": {"code": 190}}),
                _raise(_real_requests.exceptions.ConnectionError(
                    f"Authorization: Bearer {TOKEN}")),
            ):
                fake.respond = respond
                wa.send_text(DEST, "hi", tenant_id=OX)
                wa.send_interactive(DEST, "hi", [{"id": "a", "title": "A"}],
                                    tenant_id=OX)
                wa.send_list(DEST, "hi", "P", [{"title": "S", "rows": [
                    {"id": "r", "title": "R"}]}], tenant_id=OX)
                wa.send_template(DEST, "t", components=_copy_code_components(),
                                 tenant_id=OX)
                wa.validate_token()
                for fn in (lambda: wa.fetch_templates(),
                           lambda: wa.upload_media(b"x", "f", "image/png",
                                                   tenant_id=OX)):
                    try:
                        fn()
                    except ValueError:
                        pass
        assert TOKEN not in caplog.text

    def test_no_log_line_in_source_interpolates_a_raw_destination(self):
        src = _code_only(WA_PY)
        offenders = [l.strip() for l in src.splitlines()
                     if re.search(r"logger\.\w+\(", l) and "{to}" in l]
        assert offenders == [], offenders

    def test_components_are_never_logged_whole(self):
        src = _code_only(WA_PY)
        assert "sent.components={_components" not in src
        assert "meta.body={r.text}" not in src

    def test_masking_uses_phone_service(self):
        src = _code_only(WA_PY)
        assert "from app.services.phone_service import mask_destination" in src


class TestSendAutomationLogging:
    """send_automation logged the full number and Meta's raw body twice."""

    @pytest.fixture()
    def ctx(self):
        with _APP.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()
            db.session.add(Tenant(id=OX, name="Oxford", slug="rc2518a-ox",
                                  status="ACTIVE", billing_exempt=True))
            db.session.commit()
            yield
            db.session.remove()
            db.drop_all()

    @pytest.mark.parametrize("status", [200, 400])
    def test_template_fallback_logs_are_masked(self, status, ctx, fake,
                                               caplog):
        fake.respond = lambda m, u, k: _Resp(
            status, body={"error": {"message": "x"}},
            text=f"RAW {DEST} Anjali")
        with caplog.at_level(logging.INFO):
            wa.send_automation(DEST, "follow up", name="Anjali", tenant_id=OX)
        assert DEST not in caplog.text
        assert _masked() in caplog.text
        assert "RAW" not in caplog.text


# ── 6. broadcast.py keeps its audit row when a send times out ───────────────

class TestBroadcastAudit:

    @pytest.fixture()
    def seeded(self):
        with _APP.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()
            db.session.add(Tenant(id=OX, name="Oxford", slug="rc2518a-bx",
                                  status="ACTIVE", billing_exempt=True))
            db.session.commit()
        yield
        with _APP.app_context():
            db.session.remove()
            db.drop_all()

    @staticmethod
    def _audit_rows():
        with _APP.app_context():
            return [(r.action, r.detail) for r in
                    AuditLog.query.filter_by(action="BROADCAST_SEND").all()]

    def _post(self, path, body):
        return _APP.test_client().post(path, json=body,
                                       headers={"X-API-Key": KEY})

    @pytest.mark.parametrize("path,body", [
        ("/broadcast", {"numbers": [DEST, DEST2], "message": "hi",
                        "delay_seconds": 0}),
        ("/broadcast-template", {"numbers": [DEST, DEST2],
                                 "template_name": "t", "language": "en",
                                 "delay_seconds": 0}),
    ], ids=["broadcast", "broadcast-template"])
    def test_timeout_does_not_abort_the_loop_or_lose_the_audit(self, path, body,
                                                               seeded, fake):
        fake.respond = _raise(_real_requests.exceptions.Timeout("t"))
        r = self._post(path, body)
        assert r.status_code == 200, r.get_data(as_text=True)
        j = r.get_json()
        # BOTH numbers were attempted: the loop was not aborted.
        assert j["total"] == 2 and j["success"] == 0 and j["failed"] == 2
        assert [x["status"] for x in j["results"]] == [599, 599]
        rows = self._audit_rows()
        assert len(rows) == 1, "BROADCAST_SEND audit row was lost"
        assert '"failed": 2' in rows[0][1]

    def test_a_timeout_mid_loop_does_not_stop_later_numbers(self, seeded, fake):
        seq = iter([_Resp(200), _real_requests.exceptions.Timeout("t"),
                    _Resp(200)])

        def respond(m, u, k):
            nxt = next(seq)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        fake.respond = respond
        r = self._post("/broadcast", {"numbers": ["9000000001", "9000000002",
                                                  "9000000003"],
                                      "message": "hi", "delay_seconds": 0})
        j = r.get_json()
        assert [x["ok"] for x in j["results"]] == [True, False, True]
        assert j["success"] == 2 and j["failed"] == 1
        assert len(self._audit_rows()) == 1

    def test_successful_broadcast_is_unchanged(self, seeded, fake):
        r = self._post("/broadcast", {"numbers": [DEST, DEST2],
                                      "message": "hi", "delay_seconds": 0})
        j = r.get_json()
        assert j == {"total": 2, "success": 2, "failed": 0,
                     "results": j["results"]}
        assert all(x["status"] == 200 for x in j["results"])
        assert len(self._audit_rows()) == 1


# ── 7. Bare test stubs still import ─────────────────────────────────────────

class TestBareStubsStillImport:
    """Several suites load this file against a bare types.ModuleType
    "requests" (no .exceptions) and an app.config holding a handful of names.
    Module-level references to either would make it unimportable there."""

    def test_loads_and_sends_under_bare_stubs(self, monkeypatch):
        calls = []
        bare = types.ModuleType("requests")
        bare.post = lambda url, headers=None, json=None, **k: (
            calls.append(k) or _Resp(200))
        bare.get = lambda url, headers=None, **k: _Resp(200)
        bare.Response = _Resp
        monkeypatch.setitem(sys.modules, "requests", bare)

        cfg = types.ModuleType("app.config")
        cfg.ACCESS_TOKEN, cfg.PHONE_NUMBER_ID = "t", "p"   # no TIMEOUT
        # RC2.5.19-C: the Graph base has NO fallback by design (a fallback
        # would be a second copy of the version), so even a bare stub must
        # declare it. The timeout settings are still deliberately absent.
        cfg.GRAPH_API_BASE = "https://graph.facebook.com/" + re.search(
            r'^GRAPH_API_VERSION\s*=\s*"([^"]+)"',
            open(os.path.join(ROOT, "app", "config.py"), encoding="utf-8").read(),
            re.M).group(1)
        monkeypatch.setitem(sys.modules, "app.config", cfg)
        consts = types.ModuleType("app.bot.constants")
        consts.BUTTON_PRESETS = {"COURSE": []}
        monkeypatch.setitem(sys.modules, "app.bot.constants", consts)

        spec = importlib.util.spec_from_file_location("_rc2518a_bare", WA_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)           # must not raise

        monkeypatch.setattr(mod, "_get_waba_credentials",
                            lambda tid=None: ("p", "t"))
        mod.send_text("+1", "hi", tenant_id="x")
        assert calls[-1]["timeout"] == (mod._FALLBACK_CONNECT_TIMEOUT_SECONDS,
                                        mod._FALLBACK_READ_TIMEOUT_SECONDS)
        # No exceptions module -> nothing is caught -> exactly the behaviour
        # these suites had before this phase.
        assert mod._transport_errors() == ()


# ── 8. Scope: nothing outside Part A moved ──────────────────────────────────

class TestScope:

    def test_whatsapp_service_does_not_touch_otp(self):
        src = _code_only(WA_PY)
        for banned in ("otp_service", "rate_limit_service", "OTP_HMAC_KEY",
                       "create_challenge", "verify_challenge"):
            assert banned not in src, banned

    def test_no_otp_caller_exists(self):
        hits = []
        for root, _d, files in os.walk(os.path.join(ROOT, "app")):
            for f in files:
                if not f.endswith(".py") or f == "otp_service.py":
                    continue
                p = os.path.join(root, f)
                try:
                    if "otp_service" in _code_only(p):
                        hits.append(p)
                except SyntaxError:
                    pytest.fail(f"{p} does not parse")
        assert hits == []

    def test_no_new_migration(self):
        d = os.path.join(ROOT, "migrations", "versions")
        revs, downs = set(), set()
        for f in os.listdir(d):
            if not f.endswith(".py"):
                continue
            s = open(os.path.join(d, f), encoding="utf-8").read()
            r = re.search(r"^revision = '([^']+)'", s, re.M)
            dn = re.search(r"^down_revision = '([^']+)'", s, re.M)
            if r:
                revs.add(r.group(1))
            if dn:
                downs.add(dn.group(1))
        # UPDATED BY RC2.5.19-D: this phase added no migration; RC2.5.19-D
        # adds exactly one, c7e19d4a2b58, on top of a4f2c70b19de.
        # UPDATED AGAIN BY RC2.5.19-E: e2b7c41d9f63 on top of c7e19d4a2b58.
        assert revs - downs == {"e2b7c41d9f63"}
        assert len(revs) == 33

    def test_register_route_is_untouched_by_this_phase(self):
        src = open(os.path.join(ROOT, "app", "routes", "public.py"),
                   encoding="utf-8").read()
        assert "whatsapp_service" not in src
        assert "_REGISTER_MAX_PER_IP = 5" in src
