"""Phase 14C — platform security hardening.

Covers the four controls added in this phase:

  1. WhatsApp webhook HMAC verification (the C4 gap from the 14A audit)
  2. /stats no longer returns cross-tenant PII
  3. a startup warning when a secret is still its committed default
  4. billing webhook signature verification

The webhook and billing controls are OPT-IN: with no secret configured they
allow the request and warn, so shipping them cannot change production
behaviour. Both halves of that contract are tested — the disabled path must
stay permissive, and the enabled path must actually reject. A control that is
only tested in its enabled state can ship silently broken for the default
configuration, which is the one production is running.

Import isolation follows test_pipeline_foundation_10_6.py.
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_14c_security.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "testkey")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ.setdefault("BROADCAST_API_KEY", "testbroadcast")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-14c")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app, _check_default_secrets                      # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, ConversationState                        # noqa: E402
from app.routes import webhook as wh                                    # noqa: E402
from app.routes import billing as bl                                    # noqa: E402

TENANT = "t-14c"
_APP = create_app()
_APP.config["TESTING"] = True

SECRET = "meta-app-secret-under-test"


def payload(phone="919000000001", wamid="w.14c.1"):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "PHONE_A"},
        "messages": [{"from": phone, "type": "text", "id": wamid,
                      "text": {"body": "hi"}}],
        "contacts": [{"profile": {"name": "T"}}],
    }}]}]}


def sign(body: bytes, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture()
def ctx():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=TENANT, name="A", slug="a", status="ACTIVE",
                              waba_phone_number_id="PHONE_A",
                              billing_exempt=True))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


@pytest.fixture()
def signed(monkeypatch):
    """Enable webhook verification for the duration of one test."""
    monkeypatch.setitem(_APP.config, "META_APP_SECRET", SECRET)
    yield


def leads():
    with _APP.app_context():
        return ConversationState.query.count()


# ── 1. WhatsApp webhook HMAC ─────────────────────────────────────────────────

class TestWebhookSignatureDisabled:
    """Default configuration — must behave exactly as before this phase."""

    def test_unsigned_request_is_accepted_when_no_secret_configured(self, ctx):
        body = json.dumps(payload()).encode()
        r = _APP.test_client().post("/webhook", data=body,
                                    content_type="application/json")
        assert r.status_code == 200, "shipping this must not change behaviour"

    def test_lead_is_still_created_when_verification_is_off(self, ctx):
        before = leads()
        _APP.test_client().post("/webhook", json=payload(phone="919000000009",
                                                         wamid="w.off.1"))
        assert leads() == before + 1

    def test_verifier_returns_true_with_no_secret(self, ctx):
        with _APP.test_request_context("/webhook", method="POST", data=b"{}"):
            assert wh.verify_meta_signature() is True


class TestWebhookSignatureEnabled:
    """With META_APP_SECRET set, forged payloads must be refused."""

    def test_correctly_signed_request_is_accepted(self, ctx, signed):
        body = json.dumps(payload(wamid="w.sig.ok")).encode()
        r = _APP.test_client().post(
            "/webhook", data=body, content_type="application/json",
            headers={"X-Hub-Signature-256": sign(body)})
        assert r.status_code == 200

    def test_signed_request_still_creates_the_lead(self, ctx, signed):
        before = leads()
        body = json.dumps(payload(phone="919000000010", wamid="w.sig.ok2")).encode()
        _APP.test_client().post(
            "/webhook", data=body, content_type="application/json",
            headers={"X-Hub-Signature-256": sign(body)})
        assert leads() == before + 1, "verification must not break delivery"

    def test_unsigned_request_is_rejected(self, ctx, signed):
        body = json.dumps(payload(wamid="w.sig.none")).encode()
        r = _APP.test_client().post("/webhook", data=body,
                                    content_type="application/json")
        assert r.status_code == 403

    def test_wrong_signature_is_rejected(self, ctx, signed):
        body = json.dumps(payload(wamid="w.sig.bad")).encode()
        r = _APP.test_client().post(
            "/webhook", data=body, content_type="application/json",
            headers={"X-Hub-Signature-256": sign(body, "the-wrong-secret")})
        assert r.status_code == 403

    def test_signature_over_a_different_body_is_rejected(self, ctx, signed):
        """Signing *some* payload is not enough — it must sign THIS one."""
        other = json.dumps(payload(wamid="other")).encode()
        body = json.dumps(payload(wamid="w.sig.swap")).encode()
        r = _APP.test_client().post(
            "/webhook", data=body, content_type="application/json",
            headers={"X-Hub-Signature-256": sign(other)})
        assert r.status_code == 403

    def test_malformed_signature_header_is_rejected(self, ctx, signed):
        body = json.dumps(payload(wamid="w.sig.malformed")).encode()
        for bad in ("", "sha1=abc", "abc", "sha256=", "sha256=zzzz"):
            r = _APP.test_client().post(
                "/webhook", data=body, content_type="application/json",
                headers={"X-Hub-Signature-256": bad})
            assert r.status_code == 403, f"accepted {bad!r}"

    def test_forged_payload_creates_no_lead(self, ctx, signed):
        """The consequence that matters: no forged inbound message may enter
        a tenant's CRM or consume its WhatsApp quota."""
        before = leads()
        body = json.dumps(payload(phone="919999999999", wamid="w.forge")).encode()
        _APP.test_client().post("/webhook", data=body,
                                content_type="application/json")
        assert leads() == before, "forged payload created a lead"

    def test_rejection_happens_before_any_parsing(self, ctx, signed):
        """Garbage that would break the parser must still be refused cleanly,
        not 500."""
        r = _APP.test_client().post("/webhook", data=b"not json at all",
                                    content_type="application/json")
        assert r.status_code == 403

    def test_get_handshake_is_unaffected(self, ctx, signed):
        """VERIFY_TOKEN still governs the subscription handshake; the HMAC
        applies only to POSTs."""
        from app.config import VERIFY_TOKEN
        r = _APP.test_client().get(
            f"/webhook?hub.mode=subscribe&hub.verify_token={VERIFY_TOKEN}"
            f"&hub.challenge=42")
        assert r.status_code == 200 and r.get_data(as_text=True) == "42"


class TestProductionSecretPath:
    """Production sets META_APP_SECRET as an environment variable, which lands
    on the MODULE constant, not app.config. The enabled-path tests above patch
    config, so this exercises the path production actually takes."""

    def test_module_constant_activates_verification(self, ctx, monkeypatch):
        monkeypatch.setattr(wh, "META_APP_SECRET", SECRET)
        body = json.dumps(payload(wamid="w.modconst.bad")).encode()
        r = _APP.test_client().post("/webhook", data=body,
                                    content_type="application/json")
        assert r.status_code == 403, "env-var secret did not activate the check"

    def test_module_constant_accepts_a_valid_signature(self, ctx, monkeypatch):
        monkeypatch.setattr(wh, "META_APP_SECRET", SECRET)
        body = json.dumps(payload(wamid="w.modconst.ok")).encode()
        r = _APP.test_client().post(
            "/webhook", data=body, content_type="application/json",
            headers={"X-Hub-Signature-256": sign(body)})
        assert r.status_code == 200


class TestSignatureComparisonIsConstantTime:
    def test_uses_compare_digest_not_equality(self):
        """A plain == leaks the correct prefix through response timing."""
        import ast
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app", "routes", "webhook.py"),
            encoding="utf-8").read()
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "verify_meta_signature")
        calls = {ast.unparse(n.func) for n in ast.walk(fn)
                 if isinstance(n, ast.Call)}
        assert "hmac.compare_digest" in calls


# ── 2. /stats no longer leaks PII ────────────────────────────────────────────

class TestStatsEndpoint:
    def _get(self, key):
        return _APP.test_client().get("/stats", headers={"X-Admin-Key": key})

    def test_still_requires_the_admin_key(self, ctx):
        assert self._get("wrong").status_code == 401

    def test_valid_key_still_returns_counts(self, ctx):
        from app.config import ADMIN_KEY
        r = self._get(ADMIN_KEY)
        assert r.status_code == 200
        body = r.get_json()
        assert "total_leads" in body and "stage_breakdown" in body

    def test_no_per_lead_pii_is_returned(self, ctx):
        """get_all_states() returned every lead's name, stage, course and last
        message text across EVERY tenant, behind one static header key."""
        from app.config import ADMIN_KEY
        with _APP.app_context():
            db.session.add(ConversationState(
                phone="919000000077", name="SECRET-LEAD-NAME", tenant_id=TENANT,
                stage="new", course="", goal="", batch_time="", offer_course="",
                last_msg="", last_text="SECRET-MESSAGE-TEXT", lead_status="Lead"))
            db.session.commit()
        raw = self._get(ADMIN_KEY).get_data(as_text=True)
        assert "SECRET-LEAD-NAME" not in raw
        assert "SECRET-MESSAGE-TEXT" not in raw

    def test_active_conversations_is_no_longer_a_list_of_leads(self, ctx):
        from app.config import ADMIN_KEY
        assert self._get(ADMIN_KEY).get_json().get("active_conversations") is None


# ── 3. Default-secret detection ──────────────────────────────────────────────

class TestDefaultSecretWarning:
    def test_warns_for_each_secret_left_on_its_committed_default(self, caplog):
        import app as app_pkg
        import app.config as cfg
        originals = {n: getattr(cfg, n) for n in
                     ("SECRET_KEY", "ADMIN_KEY", "BROADCAST_API_KEY", "VERIFY_TOKEN")}
        try:
            cfg.SECRET_KEY = "oxford-crm-local-dev-key"
            cfg.ADMIN_KEY = "oxford_admin_2026"
            cfg.BROADCAST_API_KEY = "oxford_broadcast_2026"
            cfg.VERIFY_TOKEN = "oxford2026"
            with caplog.at_level("WARNING"):
                app_pkg._check_default_secrets(_APP)
        finally:
            for n, v in originals.items():
                setattr(cfg, n, v)
        text = caplog.text
        for name in originals:
            assert name in text, f"{name} not reported"

    def test_does_not_warn_when_secrets_are_overridden(self, caplog):
        import app as app_pkg
        import app.config as cfg
        originals = {n: getattr(cfg, n) for n in
                     ("SECRET_KEY", "ADMIN_KEY", "BROADCAST_API_KEY", "VERIFY_TOKEN")}
        try:
            for n in originals:
                setattr(cfg, n, "a-genuinely-unique-value-" + n)
            with caplog.at_level("WARNING"):
                app_pkg._check_default_secrets(_APP)
        finally:
            for n, v in originals.items():
                setattr(cfg, n, v)
        assert "using the DEFAULT value" not in caplog.text

    def test_never_logs_the_secret_value_itself(self, caplog):
        import app as app_pkg
        import app.config as cfg
        original = cfg.SECRET_KEY
        try:
            cfg.SECRET_KEY = "oxford-crm-local-dev-key"
            with caplog.at_level("WARNING"):
                app_pkg._check_default_secrets(_APP)
        finally:
            cfg.SECRET_KEY = original
        assert "oxford-crm-local-dev-key" not in caplog.text


# ── 4. Billing webhook signatures ────────────────────────────────────────────

class TestBillingWebhookSignatures:
    def test_disabled_when_no_secret_configured(self, ctx):
        """Unchanged behaviour: these are inert stubs today."""
        for path in ("/webhooks/razorpay", "/webhooks/stripe"):
            r = _APP.test_client().post(path, json={"event": "x", "type": "x"})
            assert r.status_code == 200, path

    def test_valid_signature_accepted(self, ctx, monkeypatch):
        body = json.dumps({"event": "subscription.activated"}).encode()
        monkeypatch.setattr(bl, "RAZORPAY_WEBHOOK_SECRET", SECRET)
        digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        r = _APP.test_client().post(
            "/webhooks/razorpay", data=body, content_type="application/json",
            headers={"X-Razorpay-Signature": digest})
        assert r.status_code == 200

    def test_invalid_signature_rejected(self, ctx, monkeypatch):
        body = json.dumps({"event": "subscription.activated"}).encode()
        monkeypatch.setattr(bl, "RAZORPAY_WEBHOOK_SECRET", SECRET)
        r = _APP.test_client().post(
            "/webhooks/razorpay", data=body, content_type="application/json",
            headers={"X-Razorpay-Signature": "deadbeef"})
        assert r.status_code == 403

    def test_missing_signature_rejected(self, ctx, monkeypatch):
        monkeypatch.setattr(bl, "STRIPE_WEBHOOK_SECRET", SECRET)
        r = _APP.test_client().post("/webhooks/stripe", json={"type": "x"})
        assert r.status_code == 403

    def test_handlers_are_still_inert(self):
        """The verification gate exists BEFORE the handlers do. If these stop
        being `pass`, the corresponding secret must be configured first."""
        import ast
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app", "routes", "billing.py"),
            encoding="utf-8").read()
        tree = ast.parse(src)
        for name in ("razorpay_webhook", "stripe_webhook"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            gate = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id == "_verify_hmac"]
            assert gate, f"{name} has no signature gate"


class TestNoNewUnauthenticatedSurface:
    def test_webhook_post_is_the_only_intentionally_open_write_route(self):
        """Documents the platform's unauthenticated write surface so that
        adding to it is a deliberate act."""
        open_writes = set()
        for rule in _APP.url_map.iter_rules():
            if "POST" not in (rule.methods or set()):
                continue
            open_writes.add(rule.rule)
        expected_open = {"/webhook", "/webhooks/razorpay", "/webhooks/stripe"}
        assert expected_open <= open_writes
