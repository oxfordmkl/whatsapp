"""Phase RC2.5.19-E: WhatsApp Embedded Signup (Tech Provider).

WHAT THIS SUITE PROVES
----------------------
  1. Gating: flag OFF hides the UI and 404s every endpoint; only the tenant's
     own ADMIN (not SUPER_ADMIN, not impersonating, not STAFF) may run it;
     only ACTIVE/TRIAL tenants with NO existing binding (Oxford is refused).
  2. The tenant comes from the session: a client tenant_id is ignored; the
     nonce is one-time, expiring and bound to user and tenant.
  3. Nothing is bound unless the server verifies, with the freshly exchanged
     token, that Meta granted this WABA (debug_token, System User token) and
     that the number is listed under it. Browser ids are hints only.
  4. Uniqueness and races: another tenant's number or WABA is refused; the
     binding is row-locked and the unique indexes are the final boundary.
  5. Activation: exactly-6-digit PIN, register + subscribed_apps, retryable
     VERIFIED_PENDING_ACTIVATION on failure, CONNECTED on success.
  6. Secrets: the code, business token, PIN and System User token never reach
     a response, the session, a log, an audit row or the HTML; the business
     token is stored encrypted.
  7. Graph stays v21.0 through _graph_base() and _timeout().
  8. The C/D paths and Oxford's manual binding are unchanged.

NO NETWORK: an unroutable proxy is set before the app is imported, and every
Meta call goes through a fake that replaces embedded_signup_service's own
`requests` reference.
"""
import json
import logging
import os
import re
import sys
import tempfile
import time

import pytest

os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
os.environ["NO_PROXY"] = ""

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc2519e_embedded_signup.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc2519e-admin-key")
os.environ.setdefault("SECRET_KEY", "rc2519e-secret-key")
os.environ["BROADCAST_API_KEY"] = "rc2519e-broadcast-key"
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ["PRIMARY_TENANT_ID"] = "t-ox"
os.environ.pop("WA_EMBEDDED_SIGNUP_ENABLED", None)
from cryptography.fernet import Fernet                                   # noqa: E402
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import requests as _real_requests                                       # noqa: E402

import app.services.followup_service as _fs                             # noqa: E402
import app.marketing.campaign_worker as _cw                             # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from werkzeug.security import generate_password_hash                    # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import AuditLog, Tenant, User                           # noqa: E402
from app.services import embedded_signup_service as es                  # noqa: E402
from app.services import encryption_service as enc                      # noqa: E402

OX, TA, TT, TP, TS, TB = "t-ox", "t-active", "t-trial", "t-pending", "t-susp", "t-bound"
PHONE_OX, PHONE_B = "111111111111111", "222222222222222"
WABA, PHONE = "300000000000001", "400000000000001"
OTHER_WABA, OTHER_PHONE = "300000000000009", "400000000000009"
CODE = "ES-CODE-SECRET-abc123"
BIZ_TOKEN = "EAAG-BUSINESS-TOKEN-SECRET"
SYS_TOKEN = "SYSTEM-USER-TOKEN-SECRET"
APP_SECRET = "APP-SECRET-VALUE"
PIN = "482913"
SECRETS = (CODE, BIZ_TOKEN, SYS_TOKEN, APP_SECRET, PIN)
RX = _real_requests.exceptions

_APP = create_app()
_APP.config["TESTING"] = True
_APP.config["WTF_CSRF_ENABLED"] = False
_APP.config["PRIMARY_TENANT_ID"] = OX

_OWN = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN)
    yield


# ── configuration + flag ─────────────────────────────────────────────────────

@pytest.fixture()
def configured(monkeypatch):
    import app.config as cfg
    monkeypatch.setattr(cfg, "META_APP_ID", "123456789", raising=False)
    monkeypatch.setattr(cfg, "META_APP_SECRET", APP_SECRET, raising=False)
    monkeypatch.setattr(cfg, "META_ES_CONFIG_ID", "cfg-987", raising=False)
    monkeypatch.setattr(cfg, "META_SYSTEM_USER_TOKEN", SYS_TOKEN, raising=False)


@pytest.fixture()
def flag_on(monkeypatch):
    monkeypatch.setenv("WA_EMBEDDED_SIGNUP_ENABLED", "true")


# ── fake Graph ───────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeGraph:
    """Routes by path. Defaults describe a successful signup for WABA/PHONE."""
    exceptions = RX

    def __init__(self):
        self.calls = []
        self.overrides = {}

    def route(self, method, path, kw):
        if path in self.overrides:
            out = self.overrides[path]
            return out(kw) if callable(out) else out
        if path == "oauth/access_token":
            return _Resp(200, {"access_token": BIZ_TOKEN, "token_type": "bearer"})
        if path == "debug_token":
            return _Resp(200, {"data": {"is_valid": True, "granular_scopes": [
                {"scope": "whatsapp_business_management", "target_ids": [WABA]},
                {"scope": "whatsapp_business_messaging", "target_ids": [WABA]}]}})
        if path == f"{WABA}/phone_numbers":
            return _Resp(200, {"data": [{"id": PHONE}, {"id": "400000000000002"}]})
        if path.endswith("/register") or path.endswith("/subscribed_apps"):
            return _Resp(200, {"success": True})
        return _Resp(404, {"error": {"code": 100, "message": "unknown path " + path}})

    def _do(self, method, url, kw):
        base = es._wa()._graph_base() + "/"
        assert url.startswith(base), url
        path = url[len(base):]
        self.calls.append((method, path, kw))
        out = self.route(method, path, kw)
        if isinstance(out, BaseException):
            raise out
        return out

    def get(self, url, **kw):
        return self._do("get", url, kw)

    def post(self, url, **kw):
        return self._do("post", url, kw)

    def paths(self):
        return [p for _m, p, _k in self.calls]


@pytest.fixture()
def graph(monkeypatch):
    g = FakeGraph()
    monkeypatch.setattr(es, "requests", g)
    return g


# ── data ─────────────────────────────────────────────────────────────────────

@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.execute(db.text(
            "CREATE UNIQUE INDEX uq_tenants_waba_phone_number_id ON tenants "
            "(waba_phone_number_id) WHERE waba_phone_number_id IS NOT NULL"))
        def t(tid, status, **kw):
            db.session.add(Tenant(id=tid, name=tid, slug=tid, status=status,
                                  billing_exempt=True, **kw))
        t(OX, "ACTIVE", waba_phone_number_id=PHONE_OX,
          waba_access_token_encrypted=enc.encrypt_token("ox-manual-token"))
        t(TA, "ACTIVE")
        t(TT, "TRIAL")
        t(TP, "PENDING")
        t(TS, "SUSPENDED")
        t(TB, "ACTIVE", waba_phone_number_id=PHONE_B,
          waba_access_token_encrypted=enc.encrypt_token("b-token"))
        db.session.commit()

        def mk(tid, name, role="ADMIN"):
            u = User(username=name, email=f"{name}@x.test",
                     password_hash=generate_password_hash("pw"), role=role,
                     tenant_id=tid, is_active=True, require_password_change=False)
            db.session.add(u)
            db.session.commit()
            return u.id
        ids = {"ox": mk(OX, "ox_admin"), "a": mk(TA, "a_admin"), "a2": mk(TA, "a_admin2"),
               "t": mk(TT, "t_admin"), "p": mk(TP, "p_admin"), "s": mk(TS, "s_admin"),
               "b": mk(TB, "b_admin"), "a_staff": mk(TA, "a_staff", "STAFF"),
               "super": mk(None, "platform_super", "SUPER_ADMIN")}
    yield ids
    with _APP.app_context():
        db.session.remove()
        db.drop_all()


def _client(uid, **session_extra):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
        s.update(session_extra)
    return c


def _start(c):
    return c.post("/tenant/whatsapp/es/start", json={})


def _complete(c, nonce, **over):
    body = {"nonce": nonce, "code": CODE, "waba_id": WABA, "phone_number_id": PHONE}
    body.update(over)
    return c.post("/tenant/whatsapp/es/complete", json=body)


def _signup(c, **over):
    r = _start(c)
    assert r.status_code == 200, r.get_json()
    return _complete(c, r.get_json()["nonce"], **over)


def _row(tid):
    with _APP.app_context():
        t = Tenant.query.get(tid)
        return {"phone": t.waba_phone_number_id, "waba": t.waba_id,
                "token": t.waba_access_token_encrypted, "status": t.whatsapp_connection_status,
                "source": t.waba_connection_source, "obtained": t.waba_token_obtained_at}


def _audits(tid):
    with _APP.app_context():
        return [json.loads(r.detail) for r in AuditLog.query.filter_by(
            action="TENANT_SETTINGS_CHANGE", tenant_id=tid).order_by(AuditLog.id).all()]


def _session_dump(c):
    with c.session_transaction() as s:
        return json.dumps(dict(s), default=str)


# ═══ 1. gating ═══════════════════════════════════════════════════════════════

class TestGating:

    @pytest.mark.parametrize("path", ["start", "complete", "activate"])
    def test_flag_off_404s_every_endpoint(self, path, seeded, configured, graph):
        r = _client(seeded["a"]).post(f"/tenant/whatsapp/es/{path}", json={})
        assert r.status_code == 404
        assert graph.calls == []

    def test_flag_off_hides_the_ui(self, seeded, configured):
        html = _client(seeded["a"]).get("/tenant/whatsapp").get_data(as_text=True)
        assert "es-card" not in html and "connect.facebook.net" not in html

    def test_flag_is_off_by_default(self, monkeypatch):
        monkeypatch.delenv("WA_EMBEDDED_SIGNUP_ENABLED", raising=False)
        from app.flags import wa_embedded_signup_enabled
        assert wa_embedded_signup_enabled() is False

    @pytest.mark.parametrize("who,code", [("super", 403), ("a_staff", 403)])
    def test_super_admin_and_staff_are_refused(self, who, code, seeded, configured, flag_on, graph):
        c = _client(seeded[who])
        assert _start(c).status_code == code
        assert c.post("/tenant/whatsapp/es/complete", json={}).status_code == code
        assert c.post("/tenant/whatsapp/es/activate", json={"pin": PIN}).status_code == code

    def test_impersonating_session_is_refused(self, seeded, configured, flag_on):
        c = _client(seeded["a"], impersonate_tenant_id=TB)
        assert _start(c).status_code == 403

    def test_super_admin_sees_no_signup_ui(self, seeded, configured, flag_on):
        html = _client(seeded["super"]).get(f"/tenant/whatsapp?tenant_id={TA}").get_data(as_text=True)
        assert "es-card" not in html

    @pytest.mark.parametrize("who", ["p", "s"])
    def test_pending_and_suspended_tenants_are_refused(self, who, seeded, configured, flag_on, graph):
        r = _start(_client(seeded[who]))
        assert r.status_code in (302, 409)          # billing guard or pre-flight
        if r.status_code == 409:
            assert r.get_json()["reason"] == "tenant_inactive"
        assert graph.calls == []

    def test_trial_tenant_is_allowed(self, seeded, configured, flag_on, graph):
        assert _signup(_client(seeded["t"])).status_code == 200

    @pytest.mark.parametrize("who", ["ox", "b"])
    def test_already_bound_tenants_including_oxford_are_refused(self, who, seeded, configured, flag_on, graph):
        before = _row(OX if who == "ox" else TB)
        r = _start(_client(seeded[who]))
        assert r.status_code == 409 and r.get_json()["reason"] == "already_bound"
        assert _row(OX if who == "ox" else TB) == before
        assert graph.calls == []

    def test_not_configured_is_refused_and_ui_says_unavailable(self, seeded, flag_on, graph, monkeypatch):
        import app.config as cfg
        monkeypatch.setattr(cfg, "META_SYSTEM_USER_TOKEN", "", raising=False)
        r = _start(_client(seeded["a"]))
        assert r.status_code == 409 and r.get_json()["reason"] == "not_configured"
        html = _client(seeded["a"]).get("/tenant/whatsapp").get_data(as_text=True)
        assert "not available right now" in html

    def test_csrf_is_enforced(self, seeded, configured, flag_on, monkeypatch):
        monkeypatch.setitem(_APP.config, "WTF_CSRF_ENABLED", True)
        c = _client(seeded["a"])
        for p in ("start", "complete", "activate"):
            assert c.post(f"/tenant/whatsapp/es/{p}", json={}).status_code == 400


# ═══ 2. nonce + tenant association ══════════════════════════════════════════

class TestNonce:

    def test_happy_path_binds_the_callers_own_tenant(self, seeded, configured, flag_on, graph):
        r = _signup(_client(seeded["a"]))
        assert r.status_code == 200 and r.get_json()["status"] == es.STATUS_PENDING_ACTIVATION
        row = _row(TA)
        assert row["phone"] == PHONE and row["waba"] == WABA
        assert row["status"] == es.STATUS_PENDING_ACTIVATION and row["source"] == "embedded_signup"
        assert row["obtained"] is not None
        assert enc.decrypt_token(row["token"]) == BIZ_TOKEN

    def test_client_tenant_id_is_ignored(self, seeded, configured, flag_on, graph):
        c = _client(seeded["a"])
        nonce = c.post("/tenant/whatsapp/es/start?tenant_id=" + TT, json={"tenant_id": TT}).get_json()["nonce"]
        r = c.post("/tenant/whatsapp/es/complete?tenant_id=" + TT, json={
            "nonce": nonce, "code": CODE, "waba_id": WABA, "phone_number_id": PHONE,
            "tenant_id": TT})
        assert r.status_code == 200
        assert _row(TA)["phone"] == PHONE and _row(TT)["phone"] is None

    def test_nonce_is_one_time(self, seeded, configured, flag_on, graph):
        c = _client(seeded["a"])
        nonce = _start(c).get_json()["nonce"]
        graph.overrides["oauth/access_token"] = _Resp(400, {"error": {"code": 100}})
        assert _complete(c, nonce).status_code == 502
        graph.overrides.clear()
        r = _complete(c, nonce)
        assert r.status_code == 400 and r.get_json()["reason"] == "invalid_session"

    def test_completion_without_start_is_refused(self, seeded, configured, flag_on, graph):
        r = _complete(_client(seeded["a"]), "made-up")
        assert r.status_code == 400 and r.get_json()["reason"] == "invalid_session"
        assert graph.calls == []

    def test_wrong_nonce_is_refused(self, seeded, configured, flag_on, graph):
        c = _client(seeded["a"])
        _start(c)
        assert _complete(c, "not-the-nonce").get_json()["reason"] == "invalid_session"
        assert graph.calls == []

    def test_expired_nonce_is_refused(self, seeded, configured, flag_on, graph):
        c = _client(seeded["a"])
        nonce = _start(c).get_json()["nonce"]
        with c.session_transaction() as s:
            s["wa_es"]["issued_at"] -= 601
            s.modified = True
        assert _complete(c, nonce).get_json()["reason"] == "invalid_session"
        assert graph.calls == []

    def test_nonce_is_bound_to_the_user(self, seeded, configured, flag_on, graph):
        c1 = _client(seeded["a"])
        nonce = _start(c1).get_json()["nonce"]
        with c1.session_transaction() as s:
            pending = dict(s["wa_es"])
        c2 = _client(seeded["a2"], wa_es=pending)          # same tenant, other admin
        assert _complete(c2, nonce).get_json()["reason"] == "invalid_session"

    def test_nonce_is_bound_to_the_tenant(self, seeded, configured, flag_on, graph):
        c1 = _client(seeded["a"])
        nonce = _start(c1).get_json()["nonce"]
        with c1.session_transaction() as s:
            pending = dict(s["wa_es"])
        pending["user_id"] = str(seeded["t"])
        c2 = _client(seeded["t"], wa_es=pending)           # other tenant's admin
        assert _complete(c2, nonce).get_json()["reason"] == "invalid_session"
        assert _row(TT)["phone"] is None and _row(TA)["phone"] is None

    def test_a_replayed_successful_completion_cannot_bind_again(self, seeded, configured, flag_on, graph):
        c = _client(seeded["a"])
        nonce = _start(c).get_json()["nonce"]
        with c.session_transaction() as s:
            saved = dict(s["wa_es"])
        assert _complete(c, nonce).status_code == 200
        with c.session_transaction() as s:                 # replay the old cookie state
            s["wa_es"] = saved
        r = _complete(c, nonce, waba_id=OTHER_WABA, phone_number_id=OTHER_PHONE)
        assert r.status_code == 409 and r.get_json()["reason"] == "already_bound"
        assert _row(TA)["waba"] == WABA

    @pytest.mark.parametrize("over", [{"code": ""}, {"code": None}, {"waba_id": "abc"},
                                      {"phone_number_id": ""}, {"waba_id": None}])
    def test_malformed_completion_is_refused_before_meta(self, over, seeded, configured, flag_on, graph):
        c = _client(seeded["a"])
        r = _complete(c, _start(c).get_json()["nonce"], **over)
        assert r.status_code == 400 and r.get_json()["reason"] == "invalid_request"
        assert graph.calls == []


# ═══ 3. server-side verification ════════════════════════════════════════════

class TestVerification:

    def test_calls_are_exchange_then_debug_token_then_phone_numbers(self, seeded, configured, flag_on, graph):
        _signup(_client(seeded["a"]))
        assert graph.paths() == ["oauth/access_token", "debug_token", f"{WABA}/phone_numbers"]
        _m, _p, kw = graph.calls[0]
        assert kw["params"] == {"client_id": "123456789", "client_secret": APP_SECRET, "code": CODE}
        assert "redirect_uri" not in kw["params"]            # UNRESOLVED: live test

    def test_debug_token_is_authorised_with_the_system_user_token(self, seeded, configured, flag_on, graph):
        _signup(_client(seeded["a"]))
        _m, _p, kw = graph.calls[1]
        assert kw["headers"]["Authorization"] == f"Bearer {SYS_TOKEN}"
        assert kw["params"] == {"input_token": BIZ_TOKEN}

    def test_phone_numbers_use_the_business_token(self, seeded, configured, flag_on, graph):
        _signup(_client(seeded["a"]))
        assert graph.calls[2][2]["headers"]["Authorization"] == f"Bearer {BIZ_TOKEN}"

    def test_every_call_is_v21_with_the_standard_timeout(self, seeded, configured, flag_on, graph):
        import app.config as cfg
        assert cfg.GRAPH_API_VERSION == "v21.0"
        _signup(_client(seeded["a"]))
        for _m, _p, kw in graph.calls:
            assert kw["timeout"] == es._wa()._timeout()

    def test_waba_not_granted_binds_nothing(self, seeded, configured, flag_on, graph):
        graph.overrides["debug_token"] = _Resp(200, {"data": {"is_valid": True, "granular_scopes": [
            {"scope": "whatsapp_business_management", "target_ids": [OTHER_WABA]}]}})
        r = _signup(_client(seeded["a"]))
        assert r.get_json()["reason"] == "waba_not_granted"
        assert _row(TA)["phone"] is None and _row(TA)["token"] is None

    def test_waba_only_in_the_messaging_scope_is_not_enough(self, seeded, configured, flag_on, graph):
        graph.overrides["debug_token"] = _Resp(200, {"data": {"granular_scopes": [
            {"scope": "whatsapp_business_messaging", "target_ids": [WABA]}]}})
        assert _signup(_client(seeded["a"])).get_json()["reason"] == "waba_not_granted"

    def test_invalid_token_per_debug_token_binds_nothing(self, seeded, configured, flag_on, graph):
        graph.overrides["debug_token"] = _Resp(200, {"data": {"is_valid": False, "granular_scopes": [
            {"scope": "whatsapp_business_management", "target_ids": [WABA]}]}})
        assert _signup(_client(seeded["a"])).get_json()["reason"] == "waba_not_granted"

    def test_browser_waba_hint_must_match(self, seeded, configured, flag_on, graph):
        r = _signup(_client(seeded["a"]), waba_id=OTHER_WABA)
        assert r.get_json()["reason"] == "waba_not_granted"
        assert _row(TA)["phone"] is None

    def test_phone_not_under_the_waba_binds_nothing(self, seeded, configured, flag_on, graph):
        r = _signup(_client(seeded["a"]), phone_number_id=OTHER_PHONE)
        assert r.get_json()["reason"] == "phone_not_in_waba"
        assert _row(TA)["phone"] is None

    @pytest.mark.parametrize("path", ["oauth/access_token", "debug_token", f"{WABA}/phone_numbers"])
    def test_meta_error_or_transport_failure_binds_nothing(self, path, seeded, configured, flag_on, graph):
        graph.overrides[path] = _Resp(500, {"error": {"code": 2, "message": "BODY " + BIZ_TOKEN}})
        r = _signup(_client(seeded["a"]))
        assert r.status_code == 502 and _row(TA)["phone"] is None
        assert "BODY" not in r.get_data(as_text=True)
        graph.overrides[path] = RX.ConnectTimeout("x")
        c = _client(seeded["a"])
        assert _signup(c).get_json()["reason"] == "transport"

    def test_exchange_without_a_token_fails(self, seeded, configured, flag_on, graph):
        graph.overrides["oauth/access_token"] = _Resp(200, {})
        assert _signup(_client(seeded["a"])).get_json()["reason"] == "exchange_failed"


# ═══ 4. uniqueness + races ══════════════════════════════════════════════════

class TestUniqueness:

    def test_another_tenants_number_is_refused(self, seeded, configured, flag_on, graph):
        graph.overrides[f"{WABA}/phone_numbers"] = _Resp(200, {"data": [{"id": PHONE_B}]})
        r = _signup(_client(seeded["a"]), phone_number_id=PHONE_B)
        assert r.status_code == 409 and r.get_json()["reason"] == "already_bound"
        assert _row(TA)["phone"] is None and _row(TB)["phone"] == PHONE_B

    def test_a_waba_already_connected_elsewhere_is_refused(self, seeded, configured, flag_on, graph):
        assert _signup(_client(seeded["t"])).status_code == 200        # TRIAL tenant takes WABA
        graph.overrides[f"{WABA}/phone_numbers"] = _Resp(200, {"data": [{"id": OTHER_PHONE}]})
        r = _signup(_client(seeded["a"]), phone_number_id=OTHER_PHONE)
        assert r.status_code == 409 and r.get_json()["reason"] == "already_bound"
        assert _row(TA)["waba"] is None

    def test_unique_waba_index_is_the_final_boundary(self, seeded):
        from sqlalchemy.exc import IntegrityError
        with _APP.app_context():
            Tenant.query.get(TA).waba_id = WABA
            db.session.commit()
            Tenant.query.get(TT).waba_id = WABA
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_a_race_lost_at_the_index_is_already_bound(self, seeded, configured, monkeypatch):
        """Pre-check passes (simulated), the INSERT hits the unique index."""
        with _APP.app_context():
            Tenant.query.get(TT).waba_phone_number_id = PHONE
            db.session.commit()
            real = Tenant.query
            class _Q:
                def __getattr__(self, n):
                    return getattr(db.session.query(Tenant), n)
                def filter(self, *a):
                    return db.session.query(Tenant).filter(Tenant.id == "__none__")
            Tenant.query = _Q()
            try:
                with pytest.raises(es.SignupError) as e:
                    es.bind_connection(TA, WABA, PHONE, BIZ_TOKEN)
            finally:
                del Tenant.query
            assert e.value.category == es.ALREADY_BOUND
            assert Tenant.query.get(TA).waba_phone_number_id is None

    def test_bind_takes_a_row_lock(self):
        import inspect
        assert "with_for_update()" in inspect.getsource(es.bind_connection)


# ═══ 5. activation ══════════════════════════════════════════════════════════

class TestActivation:

    def _pending(self, seeded, graph):
        c = _client(seeded["a"])
        assert _signup(c).status_code == 200
        graph.calls.clear()
        return c

    @pytest.mark.parametrize("pin", ["", "12345", "1234567", "12a456", " 123456", None, 123456])
    def test_pin_must_be_exactly_six_digits(self, pin, seeded, configured, flag_on, graph):
        c = self._pending(seeded, graph)
        r = c.post("/tenant/whatsapp/es/activate", json={"pin": pin})
        assert r.status_code == 400 and r.get_json()["reason"] == "invalid_pin"
        assert graph.calls == []

    def test_success_registers_subscribes_and_connects(self, seeded, configured, flag_on, graph):
        c = self._pending(seeded, graph)
        r = c.post("/tenant/whatsapp/es/activate", json={"pin": PIN})
        assert r.status_code == 200 and r.get_json()["status"] == es.STATUS_CONNECTED
        assert graph.paths() == [f"{PHONE}/register", f"{WABA}/subscribed_apps"]
        reg = graph.calls[0][2]
        assert reg["data"] == {"messaging_product": "whatsapp", "pin": PIN}
        assert reg["headers"]["Authorization"] == f"Bearer {BIZ_TOKEN}"
        assert _row(TA)["status"] == es.STATUS_CONNECTED

    @pytest.mark.parametrize("fail", ["register", "subscribed_apps"])
    def test_failure_stays_pending_and_is_retryable(self, fail, seeded, configured, flag_on, graph):
        c = self._pending(seeded, graph)
        path = f"{PHONE}/register" if fail == "register" else f"{WABA}/subscribed_apps"
        graph.overrides[path] = _Resp(400, {"error": {"code": 136025, "message": "x"}})
        r = c.post("/tenant/whatsapp/es/activate", json={"pin": PIN})
        assert r.status_code == 502
        assert r.get_json()["reason"] == ("register_failed" if fail == "register" else "subscribe_failed")
        assert _row(TA)["status"] == es.STATUS_PENDING_ACTIVATION
        graph.overrides.clear()
        assert c.post("/tenant/whatsapp/es/activate", json={"pin": PIN}).status_code == 200
        assert _row(TA)["status"] == es.STATUS_CONNECTED

    def test_activation_without_a_pending_connection_is_refused(self, seeded, configured, flag_on, graph):
        r = _client(seeded["a"]).post("/tenant/whatsapp/es/activate", json={"pin": PIN})
        assert r.status_code == 409 and r.get_json()["reason"] == "not_pending"
        assert graph.calls == []

    def test_a_manual_tenant_cannot_be_activated(self, seeded, configured, flag_on, graph):
        r = _client(seeded["ox"]).post("/tenant/whatsapp/es/activate", json={"pin": PIN})
        assert r.status_code == 409 and graph.calls == []


# ═══ 6. secrets ═════════════════════════════════════════════════════════════

class TestSecrets:

    def test_no_secret_in_responses_session_html_logs_or_audit(self, seeded, configured, flag_on, graph, caplog):
        c = _client(seeded["a"])
        seen = []
        with caplog.at_level(logging.DEBUG):
            r1 = _start(c); seen.append(r1.get_data(as_text=True))
            r2 = _complete(c, r1.get_json()["nonce"]); seen.append(r2.get_data(as_text=True))
            seen.append(c.get("/tenant/whatsapp").get_data(as_text=True))
            r3 = c.post("/tenant/whatsapp/es/activate", json={"pin": PIN}); seen.append(r3.get_data(as_text=True))
            seen.append(c.get("/tenant/whatsapp").get_data(as_text=True))
            seen.append(_session_dump(c))
        seen.append(caplog.text)
        seen.append(json.dumps(_audits(TA)))
        blob = "\n".join(seen)
        for secret in SECRETS:
            assert secret not in blob, "a secret leaked"

    def test_failure_logs_and_audit_carry_no_body_or_secret(self, seeded, configured, flag_on, graph, caplog):
        graph.overrides["debug_token"] = _Resp(400, {"error": {"code": 190, "message": "LEAK " + SYS_TOKEN}})
        with caplog.at_level(logging.DEBUG):
            r = _signup(_client(seeded["a"]))
        assert "LEAK" not in caplog.text and "LEAK" not in r.get_data(as_text=True)
        rows = _audits(TA)
        assert rows[-1]["event"] == "es_failed" and rows[-1]["category"] == "waba_not_granted"
        assert all(s not in json.dumps(rows) for s in SECRETS)

    def test_token_is_stored_encrypted(self, seeded, configured, flag_on, graph):
        _signup(_client(seeded["a"]))
        stored = _row(TA)["token"]
        assert BIZ_TOKEN not in stored and enc.decrypt_token(stored) == BIZ_TOKEN

    def test_audit_trail_for_a_full_signup(self, seeded, configured, flag_on, graph):
        c = _client(seeded["a"])
        _signup(c)
        c.post("/tenant/whatsapp/es/activate", json={"pin": PIN})
        events = [r["event"] for r in _audits(TA)]
        assert events == ["es_bound", "es_activated"]
        assert _audits(TA)[0]["waba_id"] == WABA and _audits(TA)[0]["phone_number_id"] == PHONE

    def test_browser_receives_only_public_identifiers(self, seeded, configured, flag_on):
        html = _client(seeded["a"]).get("/tenant/whatsapp").get_data(as_text=True)
        assert '"123456789"' in html and '"cfg-987"' in html and '"v21.0"' in html
        assert SYS_TOKEN not in html and APP_SECRET not in html


# ═══ 7. UI states ═══════════════════════════════════════════════════════════

class TestUi:

    def test_connect_button_for_an_eligible_tenant(self, seeded, configured, flag_on):
        html = _client(seeded["a"]).get("/tenant/whatsapp").get_data(as_text=True)
        assert 'id="es-connect"' in html and "connect.facebook.net/en_US/sdk.js" in html

    def test_bound_tenant_sees_already_set_up(self, seeded, configured, flag_on):
        html = _client(seeded["ox"]).get("/tenant/whatsapp").get_data(as_text=True)
        assert "already set up" in html and 'id="es-connect"' not in html

    def test_pending_then_connected_states(self, seeded, configured, flag_on, graph):
        c = _client(seeded["a"])
        _signup(c)
        html = c.get("/tenant/whatsapp").get_data(as_text=True)
        assert 'id="es-activate"' in html and "activation needed" in html
        c.post("/tenant/whatsapp/es/activate", json={"pin": PIN})
        html = c.get("/tenant/whatsapp").get_data(as_text=True)
        assert "Connected" in html and "payment method" in html

    def test_listener_checks_origin_and_handles_every_event(self):
        src = open(os.path.join(ROOT, "templates", "tenant", "whatsapp.html"), encoding="utf-8").read()
        assert "facebook\\.com$" in src and "WA_EMBEDDED_SIGNUP" in src
        for ev in ("FINISH_ONLY_WABA", "CANCEL", "ERROR", "'FINISH'"):
            assert ev in src
        assert "localStorage" not in src and "sessionStorage" not in src

    def test_fb_login_uses_the_v4_code_flow(self):
        src = open(os.path.join(ROOT, "templates", "tenant", "whatsapp.html"), encoding="utf-8").read()
        assert "response_type: 'code'" in src and "override_default_response_type: true" in src
        assert "config_id: ES.configId" in src and "extras: {setup: {}}" in src


# ═══ 8. C / D / Oxford unchanged ════════════════════════════════════════════

class TestExistingPathsUnchanged:

    def test_clear_releases_the_whole_connection(self, seeded, configured, flag_on, graph):
        c = _client(seeded["a"])
        _signup(c)
        c.post("/tenant/whatsapp/clear")
        row = _row(TA)
        assert all(row[k] is None for k in ("phone", "waba", "token", "status", "source", "obtained"))

    def test_manual_rebind_by_super_admin_resets_the_es_connection(self, seeded, configured, flag_on, graph):
        _signup(_client(seeded["a"]))
        _client(seeded["super"]).post(f"/tenant/whatsapp/save?tenant_id={TA}",
                                      data={"phone_number_id": "555555555555555", "access_token": "m"})
        row = _row(TA)
        assert row["phone"] == "555555555555555" and row["waba"] is None and row["source"] is None

    def test_manual_token_replacement_keeps_the_es_connection(self, seeded, configured, flag_on, graph):
        c = _client(seeded["a"])
        _signup(c)
        c.post("/tenant/whatsapp/save", data={"phone_number_id": PHONE, "access_token": "rotated"})
        assert _row(TA)["waba"] == WABA and _row(TA)["source"] == "embedded_signup"

    def test_revoked_token_marks_reconnect_required(self, seeded, configured, flag_on, graph, monkeypatch):
        c = _client(seeded["a"])
        _signup(c)
        c.post("/tenant/whatsapp/es/activate", json={"pin": PIN})
        from app.services import whatsapp_service as wa
        monkeypatch.setattr(wa, "check_tenant_whatsapp",
                            lambda tid: wa.TenantWhatsAppHealth(tid, wa.HEALTH_UNAUTHORIZED, http_status=401))
        c.post("/tenant/whatsapp/test")
        assert _row(TA)["status"] == es.STATUS_RECONNECT_REQUIRED
        assert "Reconnect required" in c.get("/tenant/whatsapp").get_data(as_text=True)

    def test_oxford_manual_binding_and_credentials_are_untouched(self, seeded, configured, flag_on, graph):
        before = _row(OX)
        for who in ("a", "t"):
            _signup(_client(seeded[who])) if who == "a" else None
        assert _row(OX) == before
        assert before["waba"] is None and before["status"] is None and before["source"] is None
        from app.services import whatsapp_service as wa
        with _APP.app_context():
            assert wa._get_waba_credentials(OX) == (PHONE_OX, "ox-manual-token")

    def test_an_es_tenant_resolves_for_outbound_and_inbound(self, seeded, configured, flag_on, graph):
        _signup(_client(seeded["a"]))
        from app.services import whatsapp_service as wa
        with _APP.app_context():
            assert wa._get_waba_credentials(TA) == (PHONE, BIZ_TOKEN)
            assert Tenant.query.filter_by(waba_phone_number_id=PHONE).first().id == TA

    def test_d_inbound_policy_is_unchanged(self):
        from app.services import whatsapp_service as wa
        assert wa.INBOUND_ACCEPTED_TENANT_STATUSES == ("ACTIVE", "TRIAL")


# ═══ 9. schema ══════════════════════════════════════════════════════════════

class TestSchema:

    def test_model_declares_the_four_columns_and_the_partial_unique_index(self):
        cols = Tenant.__table__.c
        for name in ("waba_id", "whatsapp_connection_status", "waba_connection_source",
                     "waba_token_obtained_at"):
            assert cols[name].nullable
        idx = {i.name: i for i in Tenant.__table__.indexes}["uq_tenants_waba_id"]
        assert idx.unique and "waba_id IS NOT NULL" in str(idx.dialect_options["postgresql"]["where"])

    def test_migration_is_additive_and_on_the_current_head(self):
        path = os.path.join(ROOT, "migrations", "versions",
                            "e2b7c41d9f63_rc2_5_19e_tenant_whatsapp_connection.py")
        src = open(path, encoding="utf-8").read()
        assert "down_revision = 'c7e19d4a2b58'" in src
        up = src.split("def upgrade")[1].split("def downgrade")[0]
        for forbidden in ("drop_", "alter_column", "execute(", "UPDATE", "DELETE"):
            assert forbidden not in up
        assert up.count("nullable=True") == 4

    def test_migration_is_the_single_head(self):
        revs = {}
        vdir = os.path.join(ROOT, "migrations", "versions")
        for f in os.listdir(vdir):
            if f.endswith(".py"):
                s = open(os.path.join(vdir, f), encoding="utf-8").read()
                r = re.search(r"^revision\s*=\s*'(\w+)'", s, re.M)
                d = re.search(r"^down_revision\s*=\s*'(\w+)'", s, re.M)
                if r:
                    revs[r.group(1)] = d.group(1) if d else None
        heads = [k for k in revs if k not in set(revs.values())]
        assert heads == ["e2b7c41d9f63"]
