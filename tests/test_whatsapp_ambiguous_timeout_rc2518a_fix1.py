"""Phase RC2.5.18-A-FIX1: ambiguous transport outcomes must not cause a resend.

WHY
---
The RC2.5.18-A final audit found two blockers.

  A. A ReadTimeout means the request may have REACHED Meta -- only the
     response was lost. RC2.5.18-A turned it into an ordinary non-200, so the
     format fallback in send_interactive()/send_list() fired and the customer
     could receive the same reply twice. send_automation() deleted the queued
     message the customer may just have been invited to reply for.
  B. The timeout was justified as "three calls x 8s, under gunicorn's 30s".
     A requests timeout is per phase, not a total, so that was false.

THE RULE UNDER TEST
-------------------
A transport failure is either NOT_SENT (provably never left) or AMBIGUOUS
(may have been delivered). An AMBIGUOUS outcome never triggers another
WhatsApp request. Format fallback happens only when Meta answered with a
rejection -- a definite HTTP response.

REAL SOCKETS, NOT JUST MOCKS
----------------------------
"The request reached the server" is the whole point, so TestRealSockets runs
the real requests library against a local TCP server that records every
request it receives. Nothing leaves the machine: an unroutable proxy is set
before the app is imported, and the local server is reached through a Session
with trust_env disabled.
"""
import os
import re
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime

import pytest

# ── Network guard: MUST precede every app import ──
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
os.environ["NO_PROXY"] = ""

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc2518a_fix1_ambiguous.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc2518a-fix1-admin-key")
os.environ.setdefault("SECRET_KEY", "rc2518a-fix1-secret-key")
os.environ["BROADCAST_API_KEY"] = "rc2518a-fix1-broadcast-key"
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ["PRIMARY_TENANT_ID"] = "t-ox"
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import requests as _real_requests                                       # noqa: E402
from urllib3.exceptions import (                                        # noqa: E402
    MaxRetryError, NameResolutionError, NewConnectionError, ProtocolError)

import app.services.followup_service as _fs                             # noqa: E402
import app.marketing.campaign_worker as _cw                             # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import ConversationState, PendingMessage, Tenant        # noqa: E402
from app.services import whatsapp_service as wa                         # noqa: E402

OX = "t-ox"
DEST = "919847312534"
TOKEN = "tok-FIX1-DO-NOT-LOG"
RX = _real_requests.exceptions

_APP = create_app()
_APP.config["TESTING"] = True
_APP.config["PRIMARY_TENANT_ID"] = OX

_OWN = {k: v for k, v in sys.modules.items()
        if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN)
    yield


BUTTONS = [{"id": "a", "title": "A"}]
SECTIONS = [{"title": "S", "rows": [{"id": "r", "title": "R"}]}]


# ── Fake transport (unit tests) ─────────────────────────────────────────────

class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.text = "{}"
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class FakeRequests:
    """Replaces whatsapp_service's `requests` reference only; `exceptions` is
    the REAL module, so real exception classes are raised and classified."""
    exceptions = RX

    def __init__(self):
        self.posts = []
        self.script = []

    def post(self, url, **kw):
        self.posts.append(kw["json"])
        step = self.script.pop(0) if self.script else _Resp(200)
        if isinstance(step, BaseException):
            raise step
        return step

    def get(self, url, **kw):
        return _Resp(200)


def _kinds(fake):
    """The message type of every POST, e.g. ['interactive/button', 'text']."""
    out = []
    for p in fake.posts:
        t = p["type"]
        if t == "interactive":
            t += "/" + p["interactive"]["type"]
        out.append(t)
    return out


@pytest.fixture()
def fake(monkeypatch):
    f = FakeRequests()
    monkeypatch.setattr(wa, "requests", f)
    monkeypatch.setattr(wa, "_get_waba_credentials",
                        lambda tenant_id=None: ("pn-1", TOKEN))
    import app.flags as flags
    monkeypatch.setattr(flags, "wa_list_messages_enabled", lambda: True)
    return f


def _refused_linux_shape():
    """What requests raises on Linux for a refused connection."""
    inner = NewConnectionError(None, "Failed to establish a new connection: "
                                     "[Errno 111] Connection refused")
    return RX.ConnectionError(MaxRetryError(None, "/v21.0/pn-1/messages",
                                            reason=inner))


def _dns_failure_shape():
    inner = NameResolutionError("graph.facebook.com", None,
                                "Temporary failure in name resolution")
    return RX.ConnectionError(MaxRetryError(None, "/v21.0/pn-1/messages",
                                            reason=inner))


def _reset_after_send_shape():
    """What requests raises when the server received the request and then
    reset the connection -- verified against a real socket below."""
    return RX.ConnectionError(ProtocolError(
        "Connection aborted.", ConnectionResetError(104, "reset by peer")))


# ═══ G. Classification ═════════════════════════════════════════════════════

class TestDeliveryStateClassification:

    @pytest.mark.parametrize("exc", [
        RX.ConnectTimeout("connect timed out"),
        _refused_linux_shape(),
        _dns_failure_shape(),
    ], ids=["ConnectTimeout", "refused(NewConnectionError)",
            "dns(NameResolutionError)"])
    def test_provably_unsent_failures_are_not_sent(self, exc):
        assert wa._delivery_state(exc) == wa.NOT_SENT

    @pytest.mark.parametrize("exc", [
        RX.ReadTimeout("read timed out"),
        RX.Timeout("timed out"),
        RX.ConnectionError("connection broken"),
        _reset_after_send_shape(),
        RX.ChunkedEncodingError("incomplete read"),
        RX.RequestException("unknown"),
    ], ids=["ReadTimeout", "Timeout", "bare-ConnectionError",
            "reset-after-send", "ChunkedEncodingError", "RequestException"])
    def test_everything_else_is_ambiguous(self, exc):
        # Conservative by design: misclassifying a transmitted request as
        # NOT_SENT is what causes a duplicate.
        assert wa._delivery_state(exc) == wa.AMBIGUOUS

    def test_a_real_http_answer_is_never_ambiguous(self):
        for status in (200, 400, 500):
            r = _Resp(status)
            assert wa.is_ambiguous(r) is False
            assert wa.is_transport_failure(r) is False

    def test_the_failure_result_carries_its_delivery_state(self):
        amb = wa.TransportFailure("ReadTimeout", wa.AMBIGUOUS)
        uns = wa.TransportFailure("ConnectTimeout", wa.NOT_SENT)
        assert amb.ambiguous is True and wa.is_ambiguous(amb)
        assert uns.ambiguous is False and not wa.is_ambiguous(uns)
        assert amb.json()["error"]["delivery_state"] == "ambiguous"
        assert uns.json()["error"]["delivery_state"] == "not_sent"
        # The caller contract is unchanged: still a non-200, still 599.
        assert amb.status_code == uns.status_code == 599

    def test_default_delivery_state_is_ambiguous(self):
        assert wa.TransportFailure("X").delivery_state == wa.AMBIGUOUS

    def test_send_path_attaches_the_classification(self, fake):
        fake.script = [RX.ReadTimeout("r")]
        with _APP.app_context():
            r = wa.send_text(DEST, "hi", tenant_id=OX)
        assert wa.is_ambiguous(r)
        fake.script = [RX.ConnectTimeout("c")]
        with _APP.app_context():
            r = wa.send_text(DEST, "hi", tenant_id=OX)
        assert wa.is_transport_failure(r) and not wa.is_ambiguous(r)


# ═══ A / B / D / E / F. Fallback only after a definite rejection ═══════════

class TestFallbackPolicy:

    # ── A. send_interactive ──
    def test_A_read_timeout_sends_no_text_fallback(self, fake):
        fake.script = [RX.ReadTimeout("r")]
        with _APP.app_context():
            r = wa.send_interactive(DEST, "hi", BUTTONS, tenant_id=OX)
        assert _kinds(fake) == ["interactive/button"], "a second message was sent"
        assert wa.is_ambiguous(r)

    @pytest.mark.parametrize("exc", [RX.ConnectionError("broken"),
                                     _reset_after_send_shape()],
                             ids=["bare", "reset-after-send"])
    def test_A_ambiguous_connection_error_sends_no_fallback(self, exc, fake):
        fake.script = [exc]
        with _APP.app_context():
            wa.send_interactive(DEST, "hi", BUTTONS, tenant_id=OX)
        assert _kinds(fake) == ["interactive/button"]

    # ── B. send_list ──
    def test_B_read_timeout_sends_no_button_fallback(self, fake):
        fake.script = [RX.ReadTimeout("r")]
        with _APP.app_context():
            r = wa.send_list(DEST, "hi", "Pick", SECTIONS, tenant_id=OX,
                             fallback_preset=BUTTONS)
        assert _kinds(fake) == ["interactive/list"], "a second message was sent"
        assert wa.is_ambiguous(r)

    def test_B_read_timeout_sends_no_text_fallback(self, fake):
        fake.script = [RX.ReadTimeout("r")]
        with _APP.app_context():
            wa.send_list(DEST, "hi", "Pick", SECTIONS, tenant_id=OX)
        assert _kinds(fake) == ["interactive/list"]

    def test_B_ambiguity_midway_through_the_chain_stops_the_chain(self, fake):
        # list rejected (definite) -> degrades to buttons -> buttons time out
        # (ambiguous) -> must NOT continue to text.
        fake.script = [_Resp(400), RX.ReadTimeout("r")]
        with _APP.app_context():
            r = wa.send_list(DEST, "hi", "Pick", SECTIONS, tenant_id=OX,
                             fallback_preset=BUTTONS)
        assert _kinds(fake) == ["interactive/list", "interactive/button"]
        assert wa.is_ambiguous(r)

    def test_send_reply_list_path_also_stops(self, fake):
        lm = wa.ListMessage("Pick", SECTIONS, fallback_body="LEGACY",
                            fallback_preset=BUTTONS)
        fake.script = [RX.ReadTimeout("r")]
        with _APP.app_context():
            wa.send_reply(DEST, "hi", lm, tenant_id=OX)
        assert _kinds(fake) == ["interactive/list"]

    # ── G. NOT_SENT: also no fallback, by design ──
    @pytest.mark.parametrize("exc", [RX.ConnectTimeout("c"),
                                     _refused_linux_shape()],
                             ids=["ConnectTimeout", "refused"])
    def test_G_unsent_failure_sends_no_fallback_either(self, exc, fake):
        # Not because it would duplicate -- it would not -- but because the
        # network is down: a second format only waits out another timeout on
        # the request path. The fallback exists for FORMAT rejections.
        fake.script = [exc]
        with _APP.app_context():
            r = wa.send_interactive(DEST, "hi", BUTTONS, tenant_id=OX)
        assert _kinds(fake) == ["interactive/button"]
        assert wa.is_transport_failure(r) and not wa.is_ambiguous(r)

    # ── D. Definite rejection keeps the existing fallback ──
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 503])
    def test_D_interactive_rejection_still_falls_back_to_text(self, status,
                                                              fake):
        fake.script = [_Resp(status), _Resp(200)]
        with _APP.app_context():
            r = wa.send_interactive(DEST, "hi", BUTTONS, tenant_id=OX)
        assert _kinds(fake) == ["interactive/button", "text"]
        assert r.status_code == 200

    # ── E. ──
    def test_E_list_rejection_still_falls_back_to_buttons(self, fake):
        fake.script = [_Resp(400), _Resp(200)]
        with _APP.app_context():
            r = wa.send_list(DEST, "hi", "Pick", SECTIONS, tenant_id=OX,
                             fallback_preset=BUTTONS)
        assert _kinds(fake) == ["interactive/list", "interactive/button"]
        assert r.status_code == 200

    def test_E_list_rejection_still_falls_back_to_text(self, fake):
        fake.script = [_Resp(400), _Resp(200)]
        with _APP.app_context():
            wa.send_list(DEST, "hi", "Pick", SECTIONS, tenant_id=OX)
        assert _kinds(fake) == ["interactive/list", "text"]

    def test_E_full_definite_chain_is_unchanged(self, fake):
        fake.script = [_Resp(400), _Resp(400), _Resp(200)]
        with _APP.app_context():
            wa.send_list(DEST, "hi", "Pick", SECTIONS, tenant_id=OX,
                         fallback_preset=BUTTONS)
        assert _kinds(fake) == ["interactive/list", "interactive/button", "text"]

    # ── F. Success is unchanged ──
    @pytest.mark.parametrize("fn", ["text", "interactive", "list"])
    def test_F_success_sends_exactly_once(self, fn, fake):
        ok = _Resp(200)
        fake.script = [ok]
        with _APP.app_context():
            r = {"text": lambda: wa.send_text(DEST, "hi", tenant_id=OX),
                 "interactive": lambda: wa.send_interactive(
                     DEST, "hi", BUTTONS, tenant_id=OX),
                 "list": lambda: wa.send_list(DEST, "hi", "Pick", SECTIONS,
                                              tenant_id=OX)}[fn]()
        assert r is ok
        assert len(fake.posts) == 1


# ═══ C. send_automation keeps what may have been delivered ═════════════════

class TestSendAutomation:

    @pytest.fixture()
    def ctx(self):
        with _APP.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()
            db.session.add(Tenant(id=OX, name="Oxford", slug="rc2518a-fix1",
                                  status="ACTIVE", billing_exempt=True))
            db.session.commit()
            yield
            db.session.remove()
            db.drop_all()

    @staticmethod
    def _pending():
        return PendingMessage.query.filter_by(phone=DEST, tenant_id=OX).all()

    def test_C_ambiguous_template_keeps_the_queued_message(self, ctx, fake):
        fake.script = [RX.ReadTimeout("r")]
        r = wa.send_automation(DEST, "Your follow-up", name="Anjali",
                               tenant_id=OX)
        rows = self._pending()
        assert len(rows) == 1, "queued message deleted after an ambiguous send"
        assert rows[0].text == "Your follow-up"
        assert wa.is_ambiguous(r)
        # And no resend happened inside the call.
        assert _kinds(fake) == ["template"]

    def test_C_retry_after_ambiguity_does_not_queue_the_text_twice(self, ctx,
                                                                   fake):
        # The follow-up worker retries a non-200 job later. Without reuse, the
        # retry would queue the same text a second time and the customer
        # would receive it twice when the webhook flushes the queue.
        fake.script = [RX.ReadTimeout("r"), _Resp(200)]
        wa.send_automation(DEST, "Your follow-up", name="A", tenant_id=OX)
        wa.send_automation(DEST, "Your follow-up", name="A", tenant_id=OX)
        assert len(self._pending()) == 1
        # Honest about what remains: the TEMPLATE was sent twice across the
        # two attempts. That is the caller's later retry, not an immediate
        # resend, and it is documented as residual at-least-once behaviour.
        assert _kinds(fake) == ["template", "template"]

    def test_C_different_texts_are_still_queued_separately(self, ctx, fake):
        fake.script = [_Resp(200), _Resp(200)]
        wa.send_automation(DEST, "first", tenant_id=OX)
        wa.send_automation(DEST, "second", tenant_id=OX)
        assert sorted(r.text for r in self._pending()) == ["first", "second"]

    @pytest.mark.parametrize("failure", [RX.ConnectTimeout("c"),
                                         _refused_linux_shape(), _Resp(400)],
                             ids=["ConnectTimeout", "refused", "HTTP400"])
    def test_C_definite_failure_still_removes_its_own_row(self, failure, ctx,
                                                          fake):
        # Nothing reached the customer, so the existing behaviour -- remove the
        # row this call created -- is kept.
        fake.script = [failure]
        wa.send_automation(DEST, "Your follow-up", tenant_id=OX)
        assert self._pending() == []

    def test_C_definite_failure_never_deletes_an_earlier_row(self, ctx, fake):
        # A reused row belongs to an earlier interception that is still
        # waiting for the customer's reply.
        db.session.add(PendingMessage(phone=DEST, text="Your follow-up",
                                      tenant_id=OX))
        db.session.commit()
        fake.script = [_Resp(400)]
        wa.send_automation(DEST, "Your follow-up", tenant_id=OX)
        assert len(self._pending()) == 1

    def test_C_success_is_unchanged(self, ctx, fake):
        fake.script = [_Resp(200)]
        r = wa.send_automation(DEST, "Your follow-up", tenant_id=OX)
        assert r.status_code == 200 and len(self._pending()) == 1

    def test_C_open_window_ambiguous_text_queues_nothing_and_resends_nothing(
            self, ctx, fake):
        db.session.add(ConversationState(
            phone=DEST, tenant_id=OX, name="A",
            last_msg=datetime.utcnow().isoformat()))
        db.session.commit()
        fake.script = [RX.ReadTimeout("r")]
        r = wa.send_automation(DEST, "hello", tenant_id=OX)
        assert _kinds(fake) == ["text"]
        assert self._pending() == []
        assert wa.is_ambiguous(r)

    def test_C_logs_stay_masked(self, ctx, fake, caplog):
        fake.script = [RX.ReadTimeout("r")]
        with caplog.at_level("INFO"):
            wa.send_automation(DEST, "Your follow-up", name="Anjali",
                               tenant_id=OX)
        assert DEST not in caplog.text
        assert "queued message kept" in caplog.text


# ═══ H / I. Real sockets: what actually reaches the server ════════════════

class _Server:
    """A local TCP server that counts every HTTP request it receives."""

    def __init__(self, mode, trickle_gap=0.0, trickle_bytes=0):
        self.mode = mode
        self.gap = trickle_gap
        self.nbytes = trickle_bytes
        self.received = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                c, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _handle(self, c):
        # Read the WHOLE request -- headers AND the Content-Length body --
        # before acting. requests sends the JSON body as a separate TCP
        # segment; replying and closing while it is still unread makes the OS
        # send a TCP reset, which the client (correctly) reports as a
        # transport failure. An earlier version stopped at the header
        # terminator and was flaky for exactly that reason.
        data = b""
        c.settimeout(2)
        try:
            while b"\r\n\r\n" not in data:
                chunk = c.recv(65536)
                if not chunk:
                    break
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            m = re.search(rb"(?i)content-length:\s*(\d+)", head)
            need = int(m.group(1)) if m else 0
            while len(body) < need:
                chunk = c.recv(65536)
                if not chunk:
                    break
                body += chunk
            data = head + b"\r\n\r\n" + body
        except OSError:
            pass
        if data:
            self.received.append(data)
        try:
            if self.mode == "hang":
                self._stop.wait(3)
            elif self.mode == "reset":
                c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                             b"\x01\x00\x00\x00\x00\x00\x00\x00")
            elif self.mode == "trickle":
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n"
                          % self.nbytes)
                for _ in range(self.nbytes):
                    time.sleep(self.gap)
                    c.sendall(b"x")
            elif self.mode == "ok":
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        except OSError:
            pass
        finally:
            c.close()

    def close(self):
        self._stop.set()
        self.sock.close()


class _Redirect:
    """whatsapp_service's `requests`, pointed at the local server.

    The REAL requests library does the work -- real timeouts, real exceptions,
    real classification -- only the URL is rewritten. trust_env is off so the
    network-guard proxy does not intercept a loopback connection.
    """
    exceptions = RX

    def __init__(self, port):
        self.port = port
        self.timeouts = []
        self._s = _real_requests.Session()
        self._s.trust_env = False

    def post(self, url, **kw):
        self.timeouts.append(kw.get("timeout"))
        return self._s.post(f"http://127.0.0.1:{self.port}/v21.0/messages", **kw)

    def get(self, url, **kw):
        return self._s.get(f"http://127.0.0.1:{self.port}/", **kw)


@pytest.fixture()
def fast_timeouts(monkeypatch):
    import app.config as cfg
    monkeypatch.setattr(cfg, "WHATSAPP_CONNECT_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(cfg, "WHATSAPP_READ_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(wa, "_get_waba_credentials",
                        lambda tenant_id=None: ("pn-1", TOKEN))
    import app.flags as flags
    monkeypatch.setattr(flags, "wa_list_messages_enabled", lambda: True)


class TestRealSockets:

    def _run(self, monkeypatch, server, fn):
        monkeypatch.setattr(wa, "requests", _Redirect(server.port))
        try:
            with _APP.app_context():
                r = fn()
            time.sleep(0.4)          # give any stray second request time to land
            return r
        finally:
            server.close()

    # ── H. The request reached the server; exactly one arrives ──
    def test_H_read_timeout_after_receipt_generates_no_second_request(
            self, monkeypatch, fast_timeouts):
        srv = _Server("hang")
        r = self._run(monkeypatch, srv, lambda: wa.send_interactive(
            DEST, "hi", BUTTONS, tenant_id=OX))
        assert len(srv.received) == 1, (
            f"server received {len(srv.received)} requests; the fallback fired")
        assert b"interactive" in srv.received[0]
        assert r.error_name == "ReadTimeout" and wa.is_ambiguous(r)

    def test_H_list_read_timeout_after_receipt_generates_no_second_request(
            self, monkeypatch, fast_timeouts):
        srv = _Server("hang")
        r = self._run(monkeypatch, srv, lambda: wa.send_list(
            DEST, "hi", "Pick", SECTIONS, tenant_id=OX, fallback_preset=BUTTONS))
        assert len(srv.received) == 1
        assert wa.is_ambiguous(r)

    def test_H_reset_after_receipt_is_ambiguous_and_not_resent(
            self, monkeypatch, fast_timeouts):
        srv = _Server("reset")
        r = self._run(monkeypatch, srv, lambda: wa.send_interactive(
            DEST, "hi", BUTTONS, tenant_id=OX))
        assert len(srv.received) == 1
        assert wa.is_ambiguous(r), (
            "a request the server RECEIVED was classified as not sent")

    # ── G on a real socket ──
    def test_G_nothing_listening_is_not_sent(self, monkeypatch, fast_timeouts):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()                         # nothing listens here now
        monkeypatch.setattr(wa, "requests", _Redirect(port))
        with _APP.app_context():
            r = wa.send_text(DEST, "hi", tenant_id=OX)
        # Windows surfaces this as ConnectTimeout, Linux as a ConnectionError
        # wrapping NewConnectionError; both mean nothing was transmitted.
        assert wa.is_transport_failure(r) and not wa.is_ambiguous(r), r.text

    def test_success_through_real_transport_is_unchanged(self, monkeypatch,
                                                        fast_timeouts):
        srv = _Server("ok")
        r = self._run(monkeypatch, srv, lambda: wa.send_interactive(
            DEST, "hi", BUTTONS, tenant_id=OX))
        assert r.status_code == 200 and len(srv.received) == 1

    # ── I. The chosen timeout semantics, measured rather than assumed ──
    def test_I_the_timeout_is_a_connect_read_pair(self, monkeypatch,
                                                  fast_timeouts):
        srv = _Server("ok")
        red = _Redirect(srv.port)
        monkeypatch.setattr(wa, "requests", red)
        try:
            with _APP.app_context():
                wa.send_text(DEST, "hi", tenant_id=OX)
        finally:
            srv.close()
        assert red.timeouts == [(0.5, 0.5)]

    def test_I_a_silent_server_fails_after_about_the_read_timeout(
            self, monkeypatch, fast_timeouts):
        srv = _Server("hang")
        t0 = time.monotonic()
        r = self._run(monkeypatch, srv, lambda: wa.send_text(
            DEST, "hi", tenant_id=OX))
        elapsed = time.monotonic() - t0 - 0.4        # minus the settle wait
        assert r.error_name == "ReadTimeout"
        assert 0.4 <= elapsed < 3.0, elapsed

    def test_I_a_trickling_server_exceeds_the_read_timeout(self, monkeypatch,
                                                          fast_timeouts):
        """The fact the old reasoning got wrong, pinned as a test.

        Each byte arrives inside the 0.5s read timeout, so the call succeeds --
        after several times the read timeout. A requests timeout bounds each
        GAP, not the call. Nothing in this module may claim otherwise.
        """
        srv = _Server("trickle", trickle_gap=0.3, trickle_bytes=6)
        t0 = time.monotonic()
        r = self._run(monkeypatch, srv, lambda: wa.send_text(
            DEST, "hi", tenant_id=OX))
        elapsed = time.monotonic() - t0 - 0.4
        assert r.status_code == 200
        assert elapsed > 2 * 0.5, (
            f"took {elapsed:.2f}s; expected well past the 0.5s read timeout")


# ═══ J. No false wall-clock claim survives ═════════════════════════════════

class TestNoFalseBudgetClaim:

    def test_J_config_states_the_real_semantics(self):
        src = open(os.path.join(ROOT, "app", "config.py"), encoding="utf-8").read()
        assert "NOT a total wall-clock limit" in src
        assert "WHATSAPP_TIMEOUT_SECONDS" not in src

    def test_J_no_code_or_test_multiplies_a_timeout_into_a_budget(self):
        pat = re.compile(r"\d\s*[x×*]\s*\d+\s*=\s*\d+\s*s?\b|<\s*30\b")
        offenders = []
        for rel in ("app/config.py", "app/services/whatsapp_service.py",
                    "tests/test_whatsapp_transport_hardening_rc2518a.py"):
            for n, line in enumerate(open(os.path.join(ROOT, rel),
                                          encoding="utf-8"), 1):
                code = line.split("#", 1)[0]
                if "TIMEOUT" in code.upper() and pat.search(code):
                    offenders.append(f"{rel}:{n}: {line.strip()}")
        assert offenders == [], offenders

    def test_J_the_removed_false_test_is_gone(self):
        src = open(os.path.join(ROOT, "tests",
                                "test_whatsapp_transport_hardening_rc2518a.py"),
                   encoding="utf-8").read()
        assert "def test_worst_degradation_chain_stays_under_gunicorn_timeout" \
            not in src


# ═══ Webhook pending-message flush (FIX1 scope expansion) ══════════════════
#
# The inbound webhook delivers a contact's queued messages when they next
# write in. RC2.5.18-A made send_text() RETURN a failure instead of raising,
# and the flush deleted each row unconditionally -- so a send that provably
# never left LOST the queued message. The rule now:
#
#   success / Meta rejection -> delete         (unchanged)
#   AMBIGUOUS                -> delete         (keeping it would re-send it)
#   NOT_SENT                 -> keep, and stop (retried, in order, next time)
#
# Driven through the REAL /webhook route and the real send_text() and
# classifier; only the network is faked. The contact replies "yes", which
# makes the route return straight after the flush -- before any Gemini call
# or bot reply -- so each test observes the flush and nothing else.

import hashlib as _hashlib                                              # noqa: E402
import hmac as _hmac                                                    # noqa: E402
import itertools as _itertools                                          # noqa: E402
import json as _json                                                    # noqa: E402

_WH_SECRET = "rc2518a-fix1-meta-app-secret"
_WH_PHONE_ID = "PN_FIX1_PRIMARY"
_OTHER_TENANT = "t-fix1-other"
_OTHER_PHONE = "919999000111"
_wamids = _itertools.count(1)


class _TextKeyedFake:
    """Fake transport that answers by the text being sent, so a test decides
    the fate of each queued message independently of send order."""
    exceptions = RX

    def __init__(self, outcomes):
        self.outcomes = outcomes            # text -> _Resp | exception
        self.sent = []

    def post(self, url, **kw):
        body = kw["json"]
        text = (body.get("text") or {}).get("body")
        self.sent.append(text)
        out = self.outcomes.get(text, _Resp(200))
        if isinstance(out, BaseException):
            raise out
        return out

    def get(self, url, **kw):
        return _Resp(200)


class TestWebhookPendingFlush:

    @pytest.fixture()
    def env(self, monkeypatch):
        monkeypatch.setitem(_APP.config, "META_APP_SECRET", _WH_SECRET)
        monkeypatch.setattr(wa, "_get_waba_credentials",
                            lambda tenant_id=None: ("pn-1", TOKEN))
        with _APP.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()
            db.session.add(Tenant(id=OX, name="Oxford", slug="rc2518a-fix1-wh",
                                  status="ACTIVE", billing_exempt=True,
                                  waba_phone_number_id=_WH_PHONE_ID))
            db.session.add(Tenant(id=_OTHER_TENANT, name="Other",
                                  slug="rc2518a-fix1-other", status="ACTIVE",
                                  billing_exempt=True))
            db.session.commit()
        yield
        with _APP.app_context():
            db.session.remove()
            db.drop_all()

    @staticmethod
    def _queue(*texts, phone=DEST, tenant=OX):
        with _APP.app_context():
            base = datetime(2026, 9, 24, 12, 0, 0)
            for i, t in enumerate(texts):
                db.session.add(PendingMessage(
                    phone=phone, text=t, tenant_id=tenant,
                    created_at=base.replace(second=i)))
            db.session.commit()

    @staticmethod
    def _left(phone=DEST, tenant=OX):
        with _APP.app_context():
            return [p.text for p in PendingMessage.query.filter_by(
                phone=phone, tenant_id=tenant).order_by(
                    PendingMessage.created_at.asc()).all()]

    def _inbound(self, monkeypatch, outcomes, text="yes"):
        fake = _TextKeyedFake(outcomes)
        monkeypatch.setattr(wa, "requests", fake)
        payload = {"entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": _WH_PHONE_ID},
            "messages": [{"from": DEST, "type": "text",
                          "id": f"wamid.FIX1.{next(_wamids)}",
                          "text": {"body": text}}],
            "contacts": [{"profile": {"name": "T"}}],
        }}]}]}
        body = _json.dumps(payload).encode()
        sig = "sha256=" + _hmac.new(_WH_SECRET.encode(), body,
                                    _hashlib.sha256).hexdigest()
        r = _APP.test_client().post("/webhook", data=body,
                                    content_type="application/json",
                                    headers={"X-Hub-Signature-256": sig})
        assert r.status_code == 200
        return fake

    # 3. SUCCESS deletes as before
    def test_success_deletes_the_queued_row(self, env, monkeypatch):
        self._queue("m1")
        fake = self._inbound(monkeypatch, {})
        assert fake.sent == ["m1"]
        assert self._left() == []

    # 1. NOT_SENT keeps the row
    @pytest.mark.parametrize("exc", [RX.ConnectTimeout("c"),
                                     _refused_linux_shape(),
                                     _dns_failure_shape()],
                             ids=["ConnectTimeout", "refused", "dns"])
    def test_not_sent_failure_keeps_the_queued_row(self, exc, env, monkeypatch):
        self._queue("m1")
        self._inbound(monkeypatch, {"m1": exc})
        assert self._left() == ["m1"], "a message that never left was lost"

    # 2. AMBIGUOUS removes the row -- no re-send (decision recorded in FIX1)
    @pytest.mark.parametrize("exc", [RX.ReadTimeout("r"),
                                     _reset_after_send_shape(),
                                     RX.ConnectionError("broken")],
                             ids=["ReadTimeout", "reset-after-send", "bare"])
    def test_ambiguous_failure_removes_the_row_so_it_is_not_resent(
            self, exc, env, monkeypatch):
        self._queue("m1")
        self._inbound(monkeypatch, {"m1": exc})
        assert self._left() == []
        # And the contact's next message does not send m1 again. (With the
        # queue now empty the route goes on to its normal bot reply, which is
        # not a text message -- so assert on m1 specifically, not on silence.)
        fake = self._inbound(monkeypatch, {})
        assert "m1" not in fake.sent, "an ambiguously-delivered message was re-sent"

    # 4. Definitive Meta rejection: existing behaviour preserved
    @pytest.mark.parametrize("status", [400, 401, 403, 500])
    def test_meta_rejection_keeps_existing_behaviour(self, status, env,
                                                     monkeypatch):
        self._queue("m1")
        self._inbound(monkeypatch, {"m1": _Resp(status)})
        assert self._left() == []

    # 5. Multiple rows: ordering and isolation
    def test_not_sent_midway_keeps_that_row_and_everything_after_it(
            self, env, monkeypatch):
        self._queue("m1", "m2", "m3")
        fake = self._inbound(monkeypatch, {"m2": RX.ConnectTimeout("c")})
        # m1 went out and is gone; m2 never left; m3 was not attempted, so
        # delivery order is preserved for the next flush.
        assert fake.sent == ["m1", "m2"]
        assert self._left() == ["m2", "m3"]

    def test_kept_rows_are_delivered_in_order_on_the_next_inbound(
            self, env, monkeypatch):
        self._queue("m1", "m2", "m3")
        self._inbound(monkeypatch, {"m2": RX.ConnectTimeout("c")})
        fake = self._inbound(monkeypatch, {})
        assert fake.sent == ["m2", "m3"]
        assert self._left() == []

    def test_ambiguous_midway_does_not_stop_later_rows(self, env,
                                                       monkeypatch):
        self._queue("m1", "m2", "m3")
        fake = self._inbound(monkeypatch, {"m2": RX.ReadTimeout("r")})
        assert fake.sent == ["m1", "m2", "m3"]
        assert self._left() == []

    def test_other_contacts_and_tenants_are_untouched(self, env, monkeypatch):
        self._queue("mine")
        self._queue("other-phone", phone=_OTHER_PHONE)
        self._queue("other-tenant", tenant=_OTHER_TENANT)
        fake = self._inbound(monkeypatch, {})
        assert fake.sent == ["mine"]
        assert self._left() == []
        assert self._left(phone=_OTHER_PHONE) == ["other-phone"]
        assert self._left(tenant=_OTHER_TENANT) == ["other-tenant"]

    def test_the_new_log_line_is_masked(self, env, monkeypatch, caplog):
        self._queue("m1")
        with caplog.at_level("WARNING"):
            self._inbound(monkeypatch, {"m1": RX.ConnectTimeout("c")})
        new = [r.getMessage() for r in caplog.records
               if "Pending delivery not sent" in r.getMessage()]
        assert new and all(DEST not in m for m in new)
