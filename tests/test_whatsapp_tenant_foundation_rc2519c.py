"""Phase RC2.5.19-C: WhatsApp tenant foundation & security hardening.

WHAT THIS SUITE PROVES
----------------------
  C2  A tenant admin can no longer CLAIM a Phone Number ID. Binding or changing
      one is SUPER_ADMIN-only; a tenant admin may replace the token for the
      number already bound to their own tenant. Inbound webhooks route by this
      column, so this closes the RC2.5.19-B CRITICAL ownership gap.
  C3  The Graph API version lives in one place and every URL uses it.
  C4  check_tenant_whatsapp() validates the credential a tenant would SEND with
      -- not the env token /health reports -- with bounded timeouts, no
      cross-tenant fallback, and without touching /health.
  C5  MultiFernet key rotation: old ciphertext still decrypts, new ciphertext
      uses the new key, rotation is all-or-nothing.
  C6  Save, token replacement and clear write REAL audit rows, never a token.
  C7  The inbound status gate is one function, unchanged (ACTIVE/TRIAL).
  C8  No raw Meta body or exception text reaches the user.
  C9  The primary tenant's credential precedence is unchanged.

NO NETWORK: an unroutable proxy is set before the app is imported, and every
Meta call goes through a fake that replaces whatsapp_service's own `requests`
reference (the shared module is never patched).
"""
import ast
import json
import logging
import os
import re
import sys
import tempfile

import pytest

os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
os.environ["NO_PROXY"] = ""

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc2519c_wa_foundation.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc2519c-admin-key")
os.environ.setdefault("SECRET_KEY", "rc2519c-secret-key")
os.environ["BROADCAST_API_KEY"] = "rc2519c-broadcast-key"
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ["PRIMARY_TENANT_ID"] = "t-ox"
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
from app.services import whatsapp_service as wa                         # noqa: E402
from app.services import encryption_service as enc                      # noqa: E402

OX, TB, TC = "t-ox", "t-beta", "t-gamma"
PHONE_OX, PHONE_B, FREE = "111111111111111", "222222222222222", "333333333333333"
TOK_OX, TOK_B = "ox-DB-token-SECRET", "beta-token-SECRET"
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


def _code_only(rel):
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            d = ast.get_docstring(n, clean=False)
            if d:
                src = src.replace(d, "")
    return "\n".join(l.split("#", 1)[0] for l in src.splitlines())


# ── fake Meta ────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = text if text is not None else json.dumps(self._body)

    def json(self):
        return self._body


class FakeRequests:
    exceptions = RX

    def __init__(self):
        self.calls = []
        self.respond = lambda method, url, kw: _Resp(200, {"id": PHONE_OX})

    def _do(self, method, url, kw):
        self.calls.append((method, url, kw))
        out = self.respond(method, url, kw)
        if isinstance(out, BaseException):
            raise out
        return out

    def get(self, url, **kw):
        return self._do("get", url, kw)

    def post(self, url, **kw):
        return self._do("post", url, kw)


@pytest.fixture()
def fake(monkeypatch):
    f = FakeRequests()
    monkeypatch.setattr(wa, "requests", f)
    return f


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
        db.session.add(Tenant(id=OX, name="Oxford", slug=OX, status="ACTIVE",
                              billing_exempt=True, waba_phone_number_id=PHONE_OX,
                              waba_access_token_encrypted=enc.encrypt_token(TOK_OX)))
        db.session.add(Tenant(id=TB, name="Beta", slug=TB, status="ACTIVE",
                              billing_exempt=True, waba_phone_number_id=PHONE_B,
                              waba_access_token_encrypted=enc.encrypt_token(TOK_B)))
        db.session.add(Tenant(id=TC, name="Gamma", slug=TC, status="ACTIVE",
                              billing_exempt=True))
        db.session.commit()

        def mk(tid, name, role="ADMIN"):
            u = User(username=name, email=f"{name}@x.test",
                     password_hash=generate_password_hash("pw"), role=role,
                     tenant_id=tid, is_active=True, require_password_change=False)
            db.session.add(u)
            db.session.commit()
            return u.id
        ids = {"ox_admin": mk(OX, "ox_admin"), "tb_admin": mk(TB, "tb_admin"),
               "tc_admin": mk(TC, "tc_admin"), "ox_staff": mk(OX, "ox_staff", "STAFF"),
               "super": mk(None, "platform_super", "SUPER_ADMIN")}
    yield ids
    with _APP.app_context():
        db.session.remove()
        db.drop_all()


def _client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def _save(uid, phone, token="", tenant_id=None):
    url = "/tenant/whatsapp/save" + (f"?tenant_id={tenant_id}" if tenant_id else "")
    return _client(uid).post(url, data={"phone_number_id": phone, "access_token": token},
                             follow_redirects=True)


def _row(tid):
    with _APP.app_context():
        t = Tenant.query.get(tid)
        return t.waba_phone_number_id, t.waba_access_token_encrypted


def _audits(tid):
    with _APP.app_context():
        return [json.loads(r.detail) for r in AuditLog.query.filter_by(
            action="TENANT_SETTINGS_CHANGE", tenant_id=tid).order_by(AuditLog.id).all()]


# ═══ C2 — ownership ═════════════════════════════════════════════════════════

class TestOwnership:

    def test_super_admin_can_bind_an_unbound_tenant(self, seeded):
        r = _save(seeded["super"], FREE, "new-tok", tenant_id=TC)
        assert b"saved successfully" in r.data
        pid, tok = _row(TC)
        assert pid == FREE and enc.decrypt_token(tok) == "new-tok"

    def test_tenant_admin_cannot_claim_an_unowned_id(self, seeded):
        r = _save(seeded["tc_admin"], FREE, "tok")
        assert _row(TC) == (None, None), "a tenant admin claimed an unowned id"
        assert b"platform administrator" in r.data

    def test_tenant_admin_cannot_change_their_bound_id(self, seeded):
        r = _save(seeded["tb_admin"], FREE, "tok")
        assert _row(TB)[0] == PHONE_B
        assert b"platform administrator" in r.data

    def test_tenant_admin_cannot_claim_another_tenants_id(self, seeded):
        _save(seeded["tb_admin"], PHONE_OX, "tok")
        assert _row(TB)[0] == PHONE_B and _row(OX)[0] == PHONE_OX

    def test_tenant_admin_may_replace_the_token_for_their_own_number(self, seeded):
        r = _save(seeded["tb_admin"], PHONE_B, "rotated-tok")
        assert b"saved successfully" in r.data
        pid, tok = _row(TB)
        assert pid == PHONE_B and enc.decrypt_token(tok) == "rotated-tok"

    def test_uniqueness_still_applies_to_super_admin(self, seeded):
        r = _save(seeded["super"], PHONE_OX, "tok", tenant_id=TC)
        assert b"already configured" in r.data
        assert _row(TC)[0] is None and _row(OX)[0] == PHONE_OX

    def test_super_admin_save_touches_only_the_named_tenant(self, seeded):
        before_ox, before_tb = _row(OX), _row(TB)
        _save(seeded["super"], FREE, "t", tenant_id=TC)
        assert _row(OX) == before_ox and _row(TB) == before_tb

    def test_staff_still_forbidden(self, seeded):
        r = _client(seeded["ox_staff"]).post("/tenant/whatsapp/save",
                                             data={"phone_number_id": FREE})
        assert r.status_code == 403

    def test_rule_is_enforced_server_side_not_by_the_template(self):
        src = _code_only("app/routes/tenant.py")
        fn = src.split("def tenant_whatsapp_save")[1].split("\ndef ")[0]
        assert "_is_super" in fn and "SUPER_ADMIN" in fn

    def test_tenant_admin_sees_no_bind_form_when_unbound(self, seeded):
        r = _client(seeded["tc_admin"]).get("/tenant/whatsapp")
        assert b"Please contact support to connect your number" in r.data
        assert b'name="phone_number_id"' not in r.data

    def test_tenant_admin_sees_a_readonly_number_when_bound(self, seeded):
        r = _client(seeded["tb_admin"]).get("/tenant/whatsapp")
        html = r.data.decode()
        field = re.search(r'<input[^>]*name="phone_number_id"[^>]*>', html, re.S).group(0)
        assert "readonly" in field

    def test_super_admin_forms_carry_the_target_tenant(self, seeded):
        r = _client(seeded["super"]).get(f"/tenant/whatsapp?tenant_id={TC}")
        assert f"/tenant/whatsapp/save?tenant_id={TC}".encode() in r.data

    def test_outbound_binding_is_unchanged(self, seeded):
        with _APP.app_context():
            assert wa._get_waba_credentials(TB) == (PHONE_B, TOK_B)
            with pytest.raises(ValueError):
                wa._get_waba_credentials(TC)
            with pytest.raises(ValueError):
                wa._get_waba_credentials(None)


# ═══ C3 — Graph version ═════════════════════════════════════════════════════

class TestGraphVersion:

    def test_version_is_still_v21(self):
        from app import config
        assert config.GRAPH_API_VERSION == "v21.0"
        assert config.GRAPH_API_BASE == "https://graph.facebook.com/v21.0"

    def test_no_graph_literal_outside_config(self):
        hits = []
        for root, _d, files in os.walk(os.path.join(ROOT, "app")):
            for f in files:
                if f.endswith(".py"):
                    p = os.path.join(root, f)
                    rel = os.path.relpath(p, ROOT).replace("\\", "/")
                    if rel == "app/config.py":
                        continue
                    if re.search(r"graph\.facebook\.com/v\d", open(p, encoding="utf-8").read()):
                        hits.append(rel)
        assert hits == [], hits

    @pytest.mark.parametrize("entry", ["text", "interactive", "list", "template",
                                       "media", "templates", "validate", "health"])
    def test_every_call_path_uses_the_central_base(self, entry, seeded, fake, monkeypatch):
        import app.config as cfg
        import app.flags as flags
        monkeypatch.setattr(cfg, "GRAPH_API_BASE", "https://graph.example.invalid/vTEST")
        monkeypatch.setattr(cfg, "WABA_ID", "waba-1", raising=False)
        monkeypatch.setattr(cfg, "ACCESS_TOKEN", "env-tok", raising=False)
        monkeypatch.setattr(flags, "wa_list_messages_enabled", lambda: True)
        fake.respond = lambda m, u, k: _Resp(200, {"id": PHONE_OX, "data": []})
        with _APP.app_context():
            {"text": lambda: wa.send_text("9190", "x", tenant_id=OX),
             "interactive": lambda: wa.send_interactive("9190", "x", [{"id": "a", "title": "A"}], tenant_id=OX),
             "list": lambda: wa.send_list("9190", "x", "P", [{"title": "S", "rows": [{"id": "r", "title": "R"}]}], tenant_id=OX),
             "template": lambda: wa.send_template("9190", "t", tenant_id=OX),
             "media": lambda: wa.upload_media(b"x", "f.png", "image/png", tenant_id=OX),
             "templates": lambda: wa.fetch_templates(),
             "validate": lambda: wa.validate_token(),
             "health": lambda: wa.check_tenant_whatsapp(OX)}[entry]()
        assert fake.calls
        for _m, url, _k in fake.calls:
            assert url.startswith("https://graph.example.invalid/vTEST/"), url


# ═══ C4 — tenant-scoped health ══════════════════════════════════════════════

class TestTenantHealth:

    def test_requires_an_explicit_tenant(self):
        with pytest.raises(ValueError):
            wa.check_tenant_whatsapp(None)

    def test_ok_uses_the_tenants_own_send_credential(self, seeded, fake):
        fake.respond = lambda m, u, k: _Resp(200, {"id": PHONE_B, "display_phone_number": "+91 98470 12345",
                                                   "verified_name": "Beta Institute"})
        with _APP.app_context():
            r = wa.check_tenant_whatsapp(TB)
        assert r.status == wa.HEALTH_OK and r.ok
        method, url, kw = fake.calls[0]
        assert url.endswith(f"/{PHONE_B}")
        assert kw["headers"]["Authorization"] == f"Bearer {TOK_B}"
        assert kw["timeout"] == wa._timeout()
        assert r.display_phone_masked and "98470" not in r.display_phone_masked

    def test_primary_uses_the_db_token_it_sends_with_not_the_env_token(self, seeded, fake, monkeypatch):
        monkeypatch.setattr(wa, "ACCESS_TOKEN", "ENV-token-used-by-/health")
        with _APP.app_context():
            wa.check_tenant_whatsapp(OX)
        assert fake.calls[0][2]["headers"]["Authorization"] == f"Bearer {TOK_OX}"

    @pytest.mark.parametrize("status,expected", [
        (401, wa.HEALTH_UNAUTHORIZED), (400, wa.HEALTH_REJECTED), (403, wa.HEALTH_REJECTED),
        (404, wa.HEALTH_REJECTED), (500, wa.HEALTH_META_ERROR), (503, wa.HEALTH_META_ERROR)])
    def test_http_outcomes(self, status, expected, seeded, fake):
        fake.respond = lambda m, u, k: _Resp(status, {"error": {"code": 190, "error_subcode": 463,
                                                               "message": f"secret {TOK_B}"}})
        with _APP.app_context():
            r = wa.check_tenant_whatsapp(TB)
        assert r.status == expected and r.http_status == status
        assert r.meta_code == 190 and r.meta_subcode == 463

    def test_200_for_a_different_number_is_not_ok(self, seeded, fake):
        fake.respond = lambda m, u, k: _Resp(200, {"id": "999"})
        with _APP.app_context():
            assert wa.check_tenant_whatsapp(TB).status == wa.HEALTH_UNEXPECTED

    @pytest.mark.parametrize("exc,expected", [
        (RX.ConnectTimeout("c"), wa.HEALTH_NOT_SENT), (RX.ReadTimeout("r"), wa.HEALTH_AMBIGUOUS),
        (RX.ConnectionError("x"), wa.HEALTH_AMBIGUOUS)])
    def test_network_failures_never_raise(self, exc, expected, seeded, fake):
        fake.respond = lambda m, u, k: exc
        with _APP.app_context():
            assert wa.check_tenant_whatsapp(TB).status == expected

    def test_unconfigured_tenant(self, seeded, fake):
        with _APP.app_context():
            assert wa.check_tenant_whatsapp(TC).status == wa.HEALTH_NOT_CONFIGURED
        assert fake.calls == [], "an unconfigured tenant must not reach Meta"

    def test_undecryptable_token(self, seeded, fake):
        with _APP.app_context():
            t = Tenant.query.get(TB)
            t.waba_access_token_encrypted = Fernet(Fernet.generate_key()).encrypt(b"x").decode()
            db.session.commit()
            assert wa.check_tenant_whatsapp(TB).status == wa.HEALTH_DECRYPT_FAILED
        assert fake.calls == []

    def test_non_primary_never_falls_back_to_primary(self, seeded, fake):
        with _APP.app_context():
            wa.check_tenant_whatsapp(TC)
        assert all(TOK_OX not in json.dumps(k.get("headers", {})) for _m, _u, k in fake.calls)

    def test_one_tenants_failure_does_not_affect_another(self, seeded, fake):
        fake.respond = lambda m, u, k: (_Resp(401, {}) if u.endswith(PHONE_B)
                                        else _Resp(200, {"id": PHONE_OX}))
        with _APP.app_context():
            assert wa.check_tenant_whatsapp(TB).status == wa.HEALTH_UNAUTHORIZED
            assert wa.check_tenant_whatsapp(OX).status == wa.HEALTH_OK

    def test_logs_carry_no_token_or_body(self, seeded, fake, caplog):
        fake.respond = lambda m, u, k: _Resp(400, {"error": {"code": 100, "message": f"echo {TOK_B}"}})
        with _APP.app_context(), caplog.at_level(logging.DEBUG):
            wa.check_tenant_whatsapp(TB)
        assert TOK_B not in caplog.text and "echo" not in caplog.text

    def test_health_endpoint_is_untouched(self):
        src = _code_only("app/routes/health.py")
        assert "check_tenant_whatsapp" not in src
        assert "token_status" in src


# ═══ C5 — key rotation ══════════════════════════════════════════════════════

@pytest.fixture()
def keys(monkeypatch):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv("WABA_ENCRYPTION_KEY", old)
    monkeypatch.delenv("WABA_ENCRYPTION_KEYS_PREVIOUS", raising=False)
    return old, new


class TestKeyRotation:

    def test_single_key_round_trip_is_unchanged(self, keys):
        assert enc.decrypt_token(enc.encrypt_token("tok")) == "tok"

    def test_old_ciphertext_decrypts_after_rotation(self, keys, monkeypatch):
        old, new = keys
        ct = enc.encrypt_token("tok")
        monkeypatch.setenv("WABA_ENCRYPTION_KEY", new)
        assert enc.decrypt_token(ct) is None, "without the previous key it must NOT decrypt"
        monkeypatch.setenv("WABA_ENCRYPTION_KEYS_PREVIOUS", old)
        assert enc.decrypt_token(ct) == "tok"

    def test_new_ciphertext_uses_the_new_key(self, keys, monkeypatch):
        old, new = keys
        monkeypatch.setenv("WABA_ENCRYPTION_KEY", new)
        monkeypatch.setenv("WABA_ENCRYPTION_KEYS_PREVIOUS", old)
        ct = enc.encrypt_token("tok")
        assert Fernet(new.encode()).decrypt(ct.encode()) == b"tok"

    def test_rotate_token_moves_to_the_current_key(self, keys, monkeypatch):
        old, new = keys
        ct = enc.encrypt_token("tok")
        monkeypatch.setenv("WABA_ENCRYPTION_KEY", new)
        monkeypatch.setenv("WABA_ENCRYPTION_KEYS_PREVIOUS", old)
        rotated = enc.rotate_token(ct)
        assert Fernet(new.encode()).decrypt(rotated.encode()) == b"tok"
        assert enc.rotate_token(Fernet(Fernet.generate_key()).encrypt(b"x").decode()) is None

    def test_invalid_previous_key_raises_without_echoing_it(self, keys, monkeypatch):
        monkeypatch.setenv("WABA_ENCRYPTION_KEYS_PREVIOUS", "not-a-real-key-VALUE")
        with pytest.raises(RuntimeError) as e:
            enc.encrypt_token("tok")
        assert "WABA_ENCRYPTION_KEYS_PREVIOUS" in str(e.value)
        assert "not-a-real-key-VALUE" not in str(e.value)

    def test_missing_current_key_still_raises(self, keys, monkeypatch):
        monkeypatch.delenv("WABA_ENCRYPTION_KEY")
        with pytest.raises(RuntimeError):
            enc.decrypt_token("x")

    def test_reencrypt_all_dry_run_writes_nothing(self, keys, monkeypatch):
        old, new = keys
        with _APP.app_context():
            db.drop_all(); db.create_all()
            db.session.add(Tenant(id="k1", name="k1", slug="k1", waba_access_token_encrypted=enc.encrypt_token("a")))
            db.session.commit()
            before = Tenant.query.get("k1").waba_access_token_encrypted
            monkeypatch.setenv("WABA_ENCRYPTION_KEY", new)
            monkeypatch.setenv("WABA_ENCRYPTION_KEYS_PREVIOUS", old)
            res = enc.reencrypt_all_tenant_tokens(dry_run=True)
            db.session.expire_all()
            assert res == {"total": 1, "rotated": 1, "failed": 0, "written": False}
            assert Tenant.query.get("k1").waba_access_token_encrypted == before
            db.drop_all()

    def test_reencrypt_all_rotates_every_token(self, keys, monkeypatch):
        old, new = keys
        with _APP.app_context():
            db.drop_all(); db.create_all()
            for i in ("k1", "k2"):
                db.session.add(Tenant(id=i, name=i, slug=i, waba_access_token_encrypted=enc.encrypt_token(i)))
            db.session.commit()
            monkeypatch.setenv("WABA_ENCRYPTION_KEY", new)
            monkeypatch.setenv("WABA_ENCRYPTION_KEYS_PREVIOUS", old)
            res = enc.reencrypt_all_tenant_tokens(dry_run=False)
            assert res["written"] and res["rotated"] == 2
            db.session.expire_all()
            monkeypatch.delenv("WABA_ENCRYPTION_KEYS_PREVIOUS")     # old key gone
            for i in ("k1", "k2"):
                assert enc.decrypt_token(Tenant.query.get(i).waba_access_token_encrypted) == i
            db.drop_all()

    def test_one_undecryptable_token_aborts_the_whole_batch(self, keys, monkeypatch):
        old, new = keys
        with _APP.app_context():
            db.drop_all(); db.create_all()
            db.session.add(Tenant(id="k1", name="k1", slug="k1", waba_access_token_encrypted=enc.encrypt_token("a")))
            db.session.add(Tenant(id="k2", name="k2", slug="k2",
                                  waba_access_token_encrypted=Fernet(Fernet.generate_key()).encrypt(b"z").decode()))
            db.session.commit()
            before = Tenant.query.get("k1").waba_access_token_encrypted
            monkeypatch.setenv("WABA_ENCRYPTION_KEY", new)
            monkeypatch.setenv("WABA_ENCRYPTION_KEYS_PREVIOUS", old)
            res = enc.reencrypt_all_tenant_tokens(dry_run=False)
            db.session.expire_all()
            assert res["failed"] == 1 and not res["written"]
            assert Tenant.query.get("k1").waba_access_token_encrypted == before
            db.drop_all()

    def test_production_behaviour_needs_no_new_variable(self):
        src = _code_only("app/services/encryption_service.py")
        assert 'os.environ.get(_PREVIOUS_KEYS_VAR, "")' in src     # optional


# ═══ C6 — audit ═════════════════════════════════════════════════════════════

class TestAudit:

    def test_action_is_registered(self):
        from app.services.audit_service import VALID_ACTIONS
        assert "TENANT_SETTINGS_CHANGE" in VALID_ACTIONS

    def test_super_admin_bind_is_audited(self, seeded):
        _save(seeded["super"], FREE, "secret-new-tok", tenant_id=TC)
        rows = _audits(TC)
        assert len(rows) == 1
        assert rows[0]["event"] == "waba_credentials_saved"
        assert rows[0]["phone_number_id"] == FREE and rows[0]["token_supplied"] is True
        assert rows[0]["by_role"] == "SUPER_ADMIN"
        assert "secret-new-tok" not in json.dumps(rows[0])

    def test_token_replacement_is_audited(self, seeded):
        _save(seeded["tb_admin"], PHONE_B, "rotated-SECRET")
        rows = _audits(TB)
        assert [r["event"] for r in rows] == ["waba_token_replaced"]
        assert "rotated-SECRET" not in json.dumps(rows)

    def test_rebind_records_the_previous_number(self, seeded):
        _save(seeded["super"], FREE, tenant_id=TB)
        rows = _audits(TB)
        assert rows[0]["previous_phone_number_id"] == PHONE_B

    def test_noop_save_writes_nothing(self, seeded):
        _save(seeded["tb_admin"], PHONE_B, "")
        assert _audits(TB) == []

    def test_clear_is_audited(self, seeded):
        _client(seeded["tb_admin"]).post("/tenant/whatsapp/clear", follow_redirects=True)
        rows = _audits(TB)
        assert rows and rows[-1]["event"] == "waba_identity_released"
        assert TOK_B not in json.dumps(rows)


# ═══ C7 — inbound status gate ═══════════════════════════════════════════════

class TestInboundGate:

    @pytest.mark.parametrize("status,ok", [("ACTIVE", True), ("TRIAL", True), ("PENDING", False),
                                           ("SUSPENDED", False), ("CANCELLED", False)])
    def test_semantics_unchanged(self, status, ok):
        t = Tenant(id="x", name="x", slug="x", status=status)
        assert wa.tenant_accepts_whatsapp_inbound(t) is ok

    def test_none_is_not_accepted(self):
        assert wa.tenant_accepts_whatsapp_inbound(None) is False

    def test_webhook_uses_the_single_gate(self):
        src = _code_only("app/routes/webhook.py")
        assert "tenant_accepts_whatsapp_inbound(tenant)" in src
        assert '["ACTIVE", "TRIAL"]' not in src


# ═══ C8 — redaction ═════════════════════════════════════════════════════════

class TestRedaction:

    def test_test_route_never_shows_the_meta_body(self, seeded, fake):
        fake.respond = lambda m, u, k: _Resp(400, {"error": {"code": 100, "message": f"BODY-{TOK_B}"}},
                                             text=f"RAW-BODY-{TOK_B}")
        r = _client(seeded["tb_admin"]).post("/tenant/whatsapp/test", follow_redirects=True)
        assert b"RAW-BODY" not in r.data and b"BODY-" not in r.data and TOK_B.encode() not in r.data
        assert b"Meta refused" in r.data and b"HTTP 400" in r.data

    def test_test_route_uses_a_timeout(self, seeded, fake):
        _client(seeded["tb_admin"]).post("/tenant/whatsapp/test", follow_redirects=True)
        assert fake.calls and fake.calls[0][2]["timeout"] == wa._timeout()

    def test_test_route_network_failure_is_a_fixed_message(self, seeded, fake):
        fake.respond = lambda m, u, k: RX.ConnectionError(f"https://x/{PHONE_B}?SECRET")
        r = _client(seeded["tb_admin"]).post("/tenant/whatsapp/test", follow_redirects=True)
        assert b"SECRET" not in r.data and b"try again" in r.data

    def test_save_encryption_failure_hides_the_exception(self, seeded, monkeypatch):
        import app.services.encryption_service as e
        def boom(_t):
            raise RuntimeError("INTERNAL-DETAIL-xyz")
        monkeypatch.setattr(e, "encrypt_token", boom)
        r = _save(seeded["tb_admin"], PHONE_B, "tok")
        assert b"INTERNAL-DETAIL" not in r.data
        assert b"could not be stored securely" in r.data
        assert _row(TB)[0] == PHONE_B

    def test_no_raw_exception_is_flashed_anywhere_in_the_whatsapp_routes(self):
        src = _code_only("app/routes/tenant.py")
        for fn in ("tenant_whatsapp_save", "tenant_whatsapp_clear", "tenant_whatsapp_test"):
            body = src.split(f"def {fn}")[1].split("\ndef ")[0]
            assert "{e}" not in body and "r.text" not in body, fn


# ═══ C9 — Oxford ════════════════════════════════════════════════════════════

class TestOxfordCompatibility:

    def test_primary_outbound_still_prefers_db_credentials(self, seeded):
        with _APP.app_context():
            assert wa._get_waba_credentials(OX) == (PHONE_OX, TOK_OX)

    def test_primary_env_fallback_is_kept(self, seeded, monkeypatch):
        monkeypatch.setattr(wa, "PHONE_NUMBER_ID", "env-phone")
        monkeypatch.setattr(wa, "ACCESS_TOKEN", "env-token")
        with _APP.app_context():
            t = Tenant.query.get(OX)
            t.waba_phone_number_id = None
            t.waba_access_token_encrypted = None
            db.session.commit()
            assert wa._get_waba_credentials(OX) == ("env-phone", "env-token")

    def test_env_credentials_are_still_read_from_config(self):
        src = open(os.path.join(ROOT, "app", "config.py"), encoding="utf-8").read()
        for name in ("ACCESS_TOKEN", "PHONE_NUMBER_ID", "WABA_ID", "PRIMARY_TENANT_ID"):
            assert f'os.environ.get("{name}"' in src

    def test_oxford_admin_can_still_rotate_its_own_token(self, seeded):
        _save(seeded["ox_admin"], PHONE_OX, "ox-rotated")
        pid, tok = _row(OX)
        assert pid == PHONE_OX and enc.decrypt_token(tok) == "ox-rotated"
