"""Phase RC2.5.5a — WhatsApp webhook safety hardening.

Three changes, all narrow:

  1. verify_meta_signature() is FAIL-CLOSED. A missing META_APP_SECRET now
     refuses the request instead of accepting it and warning.
  2. The PRIMARY_TENANT_ID grace fallback is removed from receive_message().
     An unregistered phone_number_id can no longer resolve to a tenant.
  3. The dead v19 WHATSAPP_API_URL constant is gone. Every live Graph call
     already targeted v21.

Framing, deliberately precise: change 1 is STRUCTURAL hardening of a control
that was already enforced in production, where META_APP_SECRET is configured.
It does not close an actively exploitable hole. It converts a guarantee that
rested on a configuration value into one that rests on the code, so a cleared
variable or a fresh environment cannot silently re-open the endpoint. Change 2
removes a branch that was already unreachable. Change 3 has no security impact
at all and is not claimed to have any.

Source-level assertions here are AST-based, not substring matches. The
docstrings in this phase necessarily contain the strings "PRIMARY_TENANT_ID",
"META_APP_SECRET" and "WHATSAPP_API_URL" as prose; a naive `in src` check
would match its own explanatory comment and pass while the code is broken.
That failure mode has recurred across several phases in this programme.

Import isolation follows test_platform_security_14c.py.
"""
import ast
import hashlib
import hmac
import json
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_DB = os.path.join(tempfile.gettempdir(), "rc255a_webhook_hardening.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "testkey")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ.setdefault("BROADCAST_API_KEY", "testbroadcast")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-rc255a-primary")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, ConversationState                        # noqa: E402
from app.routes import webhook as wh                                    # noqa: E402

PRIMARY = "t-rc255a-primary"
OTHER = "t-rc255a-other"
SECRET = "rc255a-meta-app-secret"
REGISTERED_ID = "PHONE_REGISTERED"
ENV_ONLY_ID = "PHONE_ENV_ONLY"

_APP = create_app()
_APP.config["TESTING"] = True


# ── source helpers (AST, never substring) ────────────────────────────────────

def _src(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _func(rel, name):
    """Return the AST node for one function, so assertions cannot be satisfied
    by prose in a neighbouring docstring or comment."""
    tree = ast.parse(_src(rel))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {rel}")


def _names_loaded(node):
    """Every identifier READ inside a function body, docstrings excluded by
    construction (a docstring is a Constant, never a Name)."""
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _string_constants(node):
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def payload(phone_number_id, phone, wamid):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone_number_id},
        "messages": [{"from": phone, "type": "text", "id": wamid,
                      "text": {"body": "hi"}}],
        "contacts": [{"profile": {"name": "T"}}],
    }}]}]}


def sign(body, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(payload_dict, secret=SECRET, signed=True):
    body = json.dumps(payload_dict).encode()
    headers = {"X-Hub-Signature-256": sign(body, secret)} if signed else {}
    return _APP.test_client().post("/webhook", data=body,
                                   content_type="application/json",
                                   headers=headers)


@pytest.fixture()
def ctx():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=PRIMARY, name="Primary", slug="primary",
                              status="ACTIVE",
                              waba_phone_number_id=REGISTERED_ID,
                              billing_exempt=True))
        db.session.add(Tenant(id=OTHER, name="Other", slug="other",
                              status="ACTIVE", billing_exempt=True))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setitem(_APP.config, "META_APP_SECRET", SECRET)
    yield


def states():
    with _APP.app_context():
        return ConversationState.query.count()


# ── 1. fail-closed signature verification ────────────────────────────────────

class TestFailClosedBehaviour:

    def test_missing_secret_refuses(self, ctx):
        assert post(payload(REGISTERED_ID, "919000000001", "r.1"),
                    signed=False).status_code == 403

    def test_missing_secret_creates_nothing(self, ctx):
        before = states()
        post(payload(REGISTERED_ID, "919000000002", "r.2"), signed=False)
        assert states() == before

    def test_valid_signature_still_accepted(self, ctx, enabled):
        assert post(payload(REGISTERED_ID, "919000000003", "r.3")).status_code == 200

    def test_valid_signature_still_creates_the_lead(self, ctx, enabled):
        before = states()
        post(payload(REGISTERED_ID, "919000000004", "r.4"))
        assert states() == before + 1, "hardening must not break real delivery"

    def test_wrong_secret_refused(self, ctx, enabled):
        assert post(payload(REGISTERED_ID, "919000000005", "r.5"),
                    secret="not-the-secret").status_code == 403

    def test_unsigned_refused_when_enabled(self, ctx, enabled):
        assert post(payload(REGISTERED_ID, "919000000006", "r.6"),
                    signed=False).status_code == 403

    @pytest.mark.parametrize("bad", ["", "sha256=", "sha1=abc", "abcdef",
                                     "sha256=zzzz", "SHA256=" + "a" * 64])
    def test_malformed_signature_header_refused(self, ctx, enabled, bad):
        body = json.dumps(payload(REGISTERED_ID, "919000000007", "r.7")).encode()
        r = _APP.test_client().post("/webhook", data=body,
                                    content_type="application/json",
                                    headers={"X-Hub-Signature-256": bad})
        assert r.status_code == 403, f"accepted {bad!r}"

    def test_get_handshake_is_unaffected(self, ctx):
        """The subscription handshake is authenticated by VERIFY_TOKEN and must
        keep working with no app secret involved."""
        from app.config import VERIFY_TOKEN
        r = _APP.test_client().get(
            f"/webhook?hub.mode=subscribe&hub.verify_token={VERIFY_TOKEN}"
            "&hub.challenge=42")
        assert r.status_code == 200 and r.get_data(as_text=True) == "42"


class TestFailClosedIsStructuralNotIncidental:

    def test_the_falsy_secret_branch_returns_false(self, ctx):
        """AST: locate the `if not secret:` guard and prove its body returns a
        literal False. A substring search for "return False" would also match
        the two later rejection paths."""
        fn = _func("app/routes/webhook.py", "verify_meta_signature")
        # Identify the guard by the name it tests -- `secret`. Matching any
        # `if not ...` is not enough: the compare_digest rejection is also a
        # UnaryOp/Not, so a mutation that deletes the secret guard would let
        # the HMAC check stand in for it and this test would pass regardless.
        guards = [n for n in fn.body
                  if isinstance(n, ast.If) and isinstance(n.test, ast.UnaryOp)
                  and isinstance(n.test.op, ast.Not)
                  and isinstance(n.test.operand, ast.Name)
                  and n.test.operand.id == "secret"]
        assert len(guards) == 1, "the `if not secret:` guard is gone"
        returns = [s for s in guards[0].body if isinstance(s, ast.Return)]
        assert len(returns) == 1
        assert returns[0].value.value is False, "fail-open has been restored"

    def test_no_branch_of_the_verifier_returns_true_before_hmac(self, ctx):
        """The only `return True` may be the final one, after compare_digest."""
        fn = _func("app/routes/webhook.py", "verify_meta_signature")
        true_returns = [n for n in ast.walk(fn)
                        if isinstance(n, ast.Return)
                        and isinstance(n.value, ast.Constant)
                        and n.value.value is True]
        assert len(true_returns) == 1, "more than one success path"
        digest = [n for n in ast.walk(fn) if isinstance(n, ast.Attribute)
                  and n.attr == "compare_digest"]
        assert digest, "constant-time comparison removed"
        assert true_returns[0].lineno > digest[0].lineno, \
            "a success path precedes the HMAC comparison"

    def test_the_call_site_is_still_the_first_statement(self, ctx):
        """Authentication must precede parsing and any DB access."""
        fn = _func("app/routes/webhook.py", "receive_message")
        first = fn.body[1] if isinstance(fn.body[0], ast.Expr) else fn.body[0]
        called = {n.func.id for n in ast.walk(first)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "verify_meta_signature" in called, \
            "the signature gate is no longer the first thing receive_message does"


# ── 2. the PRIMARY_TENANT_ID webhook fallback is gone ────────────────────────

class TestWebhookFallbackRemoved:

    @pytest.fixture()
    def env_ids(self, monkeypatch):
        monkeypatch.setitem(_APP.config, "PHONE_NUMBER_ID", ENV_ONLY_ID)
        monkeypatch.setitem(_APP.config, "PRIMARY_TENANT_ID", PRIMARY)
        yield

    def test_precondition_env_id_is_unregistered(self, ctx, env_ids):
        with _APP.app_context():
            assert Tenant.query.filter_by(
                waba_phone_number_id=ENV_ONLY_ID).count() == 0

    def test_env_matching_unknown_id_is_acknowledged(self, ctx, enabled, env_ids):
        assert post(payload(ENV_ONLY_ID, "919111111101", "f.1")).status_code == 200

    def test_env_matching_unknown_id_creates_nothing(self, ctx, enabled, env_ids):
        before = states()
        post(payload(ENV_ONLY_ID, "919111111102", "f.2"))
        assert states() == before

    def test_it_does_not_land_under_the_primary_tenant(self, ctx, enabled, env_ids):
        post(payload(ENV_ONLY_ID, "919111111103", "f.3"))
        with _APP.app_context():
            assert ConversationState.query.filter_by(
                phone="919111111103", tenant_id=PRIMARY).count() == 0, \
                "the PRIMARY_TENANT_ID webhook fallback is back"

    def test_registered_id_still_routes_correctly(self, ctx, enabled, env_ids):
        assert post(payload(REGISTERED_ID, "919111111104", "f.4")).status_code == 200
        with _APP.app_context():
            assert ConversationState.query.filter_by(
                phone="919111111104", tenant_id=PRIMARY).count() == 1

    @pytest.mark.parametrize("pid", ["", "UNKNOWN", "PHONE_REGISTERED_X"])
    def test_no_unknown_id_ever_resolves(self, ctx, enabled, env_ids, pid):
        before = states()
        r = post(payload(pid, "9192222222" + str(abs(hash(pid)) % 100).zfill(2), "f." + pid))
        assert r.status_code == 200
        assert states() == before

    def test_missing_metadata_is_dropped(self, ctx, enabled, env_ids):
        p = payload("", "919333333301", "f.m")
        del p["entry"][0]["changes"][0]["value"]["metadata"]
        before = states()
        assert post(p).status_code == 200
        assert states() == before

    def test_blank_env_phone_id_does_not_reopen_the_fall_through(self, ctx, enabled, monkeypatch):
        """The specific latent defect: with PHONE_NUMBER_ID == "" the old outer
        guard matched a payload with no phone id, the inner guard failed, and
        execution fell through with tenant_id = None."""
        monkeypatch.setitem(_APP.config, "PHONE_NUMBER_ID", "")
        monkeypatch.setitem(_APP.config, "PRIMARY_TENANT_ID", PRIMARY)
        before = states()
        assert post(payload("", "919333333302", "f.b")).status_code == 200
        assert states() == before
        with _APP.app_context():
            assert ConversationState.query.filter_by(tenant_id=None).count() == 0


class TestFallbackRemovalIsStructural:

    def test_receive_message_no_longer_reads_primary_tenant_id(self):
        """AST over the function body only. The module and this test both
        mention PRIMARY_TENANT_ID in prose; a substring check on the file
        would pass regardless of the code."""
        fn = _func("app/routes/webhook.py", "receive_message")
        assert "PRIMARY_TENANT_ID" not in _string_constants(fn), \
            "receive_message still looks up PRIMARY_TENANT_ID"

    def test_receive_message_no_longer_reads_phone_number_id(self):
        fn = _func("app/routes/webhook.py", "receive_message")
        assert "PHONE_NUMBER_ID" not in _string_constants(fn), \
            "the env-phone-id comparison that gated the fallback is back"

    def test_the_unknown_tenant_branch_returns_unconditionally(self):
        """The `else:` of `if tenant:` must return unconditionally.

        Anchored on the node whose test is the bare name `tenant`, NOT on "any
        If with an orelse containing a Return". receive_message is full of
        such nodes -- the msg_type if/elif/else chain is one -- and the loose
        form silently matched the first of them, passing no matter what the
        tenant branch looked like. A mutation that replaced this branch's
        `return` with `pass` proved it: the test stayed green.
        """
        fn = _func("app/routes/webhook.py", "receive_message")
        target = [n for n in ast.walk(fn)
                  if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
                  and n.test.id == "tenant" and n.orelse]
        assert len(target) == 1, "the `if tenant: ... else:` lookup is gone"
        orelse = target[0].orelse
        assert not any(isinstance(s, ast.If) for s in orelse), \
            "the unknown-phone-id branch is conditional again — a fallback is back"
        assert isinstance(orelse[-1], ast.Return), \
            "the unknown-phone-id branch no longer returns; execution falls " \
            "through into message processing with no tenant"


# ── 3. the dead v19 constant ─────────────────────────────────────────────────

class TestDeadApiUrlRemoved:

    def test_config_no_longer_defines_it(self):
        import app.config as cfg
        assert not hasattr(cfg, "WHATSAPP_API_URL")

    def test_config_module_has_no_such_assignment(self):
        """AST: the removal comment names the constant in prose."""
        tree = ast.parse(_src("app/config.py"))
        assigned = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)}
        assert "WHATSAPP_API_URL" not in assigned

    def test_whatsapp_service_no_longer_imports_it(self):
        tree = ast.parse(_src("app/services/whatsapp_service.py"))
        imported = {a.name for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "WHATSAPP_API_URL" not in imported

    def test_whatsapp_service_still_imports_what_it_uses(self):
        tree = ast.parse(_src("app/services/whatsapp_service.py"))
        imported = {a.name for n in ast.walk(tree)
                    if isinstance(n, ast.ImportFrom) for a in n.names}
        assert {"ACCESS_TOKEN", "PHONE_NUMBER_ID"} <= imported


class TestLiveApiVersionUnchanged:

    def _graph_literals(self, rel):
        tree = ast.parse(_src(rel))
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and "graph.facebook.com" in node.value:
                out.append(node.value)
        return out

    def test_every_live_graph_url_is_v21(self):
        lits = self._graph_literals("app/services/whatsapp_service.py")
        assert lits, "no Graph URLs found — the file changed shape"
        for lit in lits:
            assert "/v21.0/" in lit, f"non-v21 Graph URL: {lit}"

    def test_no_v19_remains_anywhere_in_the_app_package(self):
        offenders = []
        for root, _dirs, files in os.walk(os.path.join(_ROOT, "app")):
            if "__pycache__" in root:
                continue
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(root, fname)
                for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
                    if isinstance(node, ast.Constant) \
                            and isinstance(node.value, str) \
                            and "graph.facebook.com/v19" in node.value:
                        offenders.append(os.path.relpath(path, _ROOT))
        assert offenders == [], f"v19 Graph URLs still live: {offenders}"

    def test_send_text_builds_its_url_per_call(self):
        """Proves the constant was not simply relocated."""
        fn = _func("app/services/whatsapp_service.py", "send_text")
        assert "WHATSAPP_API_URL" not in _names_loaded(fn)


# ── 4. out-of-scope mechanisms must be untouched ─────────────────────────────

class TestOutOfScopeFallbacksSurvive:
    """Fallbacks B and C are separate mechanisms. RC2.5.5a removed only the
    webhook tenant-resolution fallback (A). If a later edit takes these out
    while claiming this phase's scope, these fail."""

    def test_outbound_credential_fallback_b_is_intact(self):
        """whatsapp_service still falls back to global env credentials for the
        primary tenant. This selects CREDENTIALS for a tenant already known —
        it cannot assign a tenant to an inbound message."""
        src = _src("app/services/whatsapp_service.py")
        tree = ast.parse(src)
        found = any(
            isinstance(n, ast.Constant) and n.value == "PRIMARY_TENANT_ID"
            for n in ast.walk(tree))
        assert found, "outbound credential fallback (B) was removed — out of scope"

    def test_resolve_tenant_id_leg_2_is_intact(self):
        """log_service's resolve_tenant_id still names PRIMARY_TENANT_ID.
        Retiring it is a separate phase with its own tripwires."""
        tree = ast.parse(_src("app/services/log_service.py"))
        found = any(
            isinstance(n, ast.Constant) and n.value == "PRIMARY_TENANT_ID"
            for n in ast.walk(tree))
        assert found, "resolve_tenant_id leg 2 (C) was removed — out of scope"

    def test_webhook_still_gates_on_tenant_status(self):
        fn = _func("app/routes/webhook.py", "receive_message")
        consts = _string_constants(fn)
        assert "ACTIVE" in consts and "TRIAL" in consts, \
            "the ACTIVE/TRIAL status gate was dropped"
