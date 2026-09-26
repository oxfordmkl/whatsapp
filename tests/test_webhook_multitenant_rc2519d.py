"""Phase RC2.5.19-D: webhook multi-tenant hardening.

WHAT THIS SUITE PROVES
----------------------
  1. Every entry, every change and every message of a delivery is processed;
     tenant resolution and the ACTIVE/TRIAL gate apply PER CHANGE.
  2. A status event, or an unknown / PENDING / SUSPENDED tenant's change,
     never suppresses a valid sibling -- including another tenant's.
  3. One malformed message never drops its siblings; the acknowledgement is
     always 200 once the signature is valid.
  4. An inbound wamid is claimed synchronously, BEFORE any side effect, and a
     partial unique index makes the claim atomic: a duplicate delivery -- in
     the same POST or a later one -- is processed once.
  5. Webhook logs carry masked numbers and exception classes, never the full
     number or the exception text.
  6. The GET handshake is fail-closed with no committed default token, and
     compares in constant time.

NO NETWORK: an unroutable proxy is set before the app is imported, and the
reply path (smart_reply / send_reply), Sheets and follow-up scheduling are
replaced on the webhook module itself.
"""
import ast
import hashlib
import hmac
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

_DB = os.path.join(tempfile.gettempdir(), "rc2519d_webhook.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc2519d-admin-key")
os.environ.setdefault("SECRET_KEY", "rc2519d-secret-key")
os.environ["BROADCAST_API_KEY"] = "rc2519d-broadcast-key"
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ["PRIMARY_TENANT_ID"] = "t-ox"
from cryptography.fernet import Fernet                                   # noqa: E402
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import app.services.followup_service as _fs                             # noqa: E402
import app.marketing.campaign_worker as _cw                             # noqa: E402
_fs.init_followup_service = lambda a: None
_cw.init_campaign_worker = lambda a: None

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import ConversationMessage, Tenant                      # noqa: E402
from app.routes import webhook as wh                                    # noqa: E402

SECRET = "rc2519d-app-secret"
VTOKEN = "rc2519d-verify-token"
OX, TB, TP, TS = "t-ox", "t-beta", "t-pending", "t-suspended"
PID = {OX: "100000000000001", TB: "100000000000002",
       TP: "100000000000003", TS: "100000000000004"}
UNKNOWN_PID = "199999999999999"
CUST_A, CUST_B = "919876543210", "919812345678"

_APP = create_app()
_APP.config["TESTING"] = True
_APP.config["WTF_CSRF_ENABLED"] = False
_APP.config["PRIMARY_TENANT_ID"] = OX
_APP.config["META_APP_SECRET"] = SECRET

_OWN = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}


@pytest.fixture(autouse=True)
def _pin_own_modules():
    sys.modules.update(_OWN)
    yield


def _func(name):
    tree = ast.parse(open(os.path.join(ROOT, "app", "routes", "webhook.py"),
                          encoding="utf-8").read())
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)


# ── harness ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, status in ((OX, "ACTIVE"), (TB, "TRIAL"),
                            (TP, "PENDING"), (TS, "SUSPENDED")):
            db.session.add(Tenant(id=tid, name=tid, slug=tid, status=status,
                                  billing_exempt=True, waba_phone_number_id=PID[tid]))
        db.session.commit()
    yield
    with _APP.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def calls(monkeypatch):
    """Replace every side effect that would leave the process, and record."""
    rec = {"replies": [], "sheets": [], "followups": [], "claimed_before_reply": []}

    def fake_smart_reply(msg_text, name, phone, is_new_lead, tenant_id=None, wa_message_id=None):
        # The inbound row must already exist when the reply path starts.
        rec["claimed_before_reply"].append(ConversationMessage.query.filter_by(
            phone=phone, direction="incoming", tenant_id=tenant_id,
            message=msg_text).count() >= 1)
        from app.state import get_or_create_state
        get_or_create_state(phone, name, tenant_id=tenant_id)
        return f"reply:{msg_text}", None

    monkeypatch.setattr(wh, "smart_reply", fake_smart_reply)
    monkeypatch.setattr(wh, "send_reply",
                        lambda to, text, preset, tenant_id=None:
                        rec["replies"].append((tenant_id, to, text)))
    monkeypatch.setattr(wh, "save_lead_to_sheets", lambda *a, **k: rec["sheets"].append(a))
    monkeypatch.setattr(wh, "schedule_followups",
                        lambda phone, name, tenant_id=None:
                        rec["followups"].append((tenant_id, phone)))
    for name in ("log_message_in_thread", "log_lead_event_in_thread",
                 "save_conversation_message_in_thread"):
        monkeypatch.setattr(wh, name, lambda **k: None)
    return rec


def msg(wamid, text, sender=CUST_A):
    return {"from": sender, "id": wamid, "type": "text", "text": {"body": text}}


def change(tenant_or_pid, messages=None, statuses=None, contacts=None):
    pid = PID.get(tenant_or_pid, tenant_or_pid)
    value = {"messaging_product": "whatsapp",
             "metadata": {"display_phone_number": "x", "phone_number_id": pid}}
    if messages is not None:
        value["messages"] = messages
        value["contacts"] = contacts if contacts is not None else [
            {"wa_id": m.get("from"), "profile": {"name": "Asha"}} for m in messages
            if isinstance(m, dict)]
    if statuses is not None:
        value["statuses"] = statuses
    return {"field": "messages", "value": value}


def delivery(*entries):
    """entries: each a list of changes."""
    return {"object": "whatsapp_business_account",
            "entry": [{"id": f"waba-{i}", "changes": chs} for i, chs in enumerate(entries)]}


def post(payload, signed=True, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {}
    if signed:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(
            SECRET.encode(), body, hashlib.sha256).hexdigest()
    return _APP.test_client().post("/webhook", data=body, headers=headers,
                                   content_type="application/json")


def incoming(tenant_id=None):
    with _APP.app_context():
        q = ConversationMessage.query.filter_by(direction="incoming")
        if tenant_id:
            q = q.filter_by(tenant_id=tenant_id)
        return [(r.tenant_id, r.wa_message_id, r.message) for r in q.order_by(ConversationMessage.id)]


# ═══ 1. every entry / change / message ══════════════════════════════════════

class TestEverythingIsProcessed:

    def test_multiple_entries(self, seeded, calls):
        r = post(delivery([change(OX, [msg("w1", "one")])],
                          [change(OX, [msg("w2", "two", CUST_B)])]))
        assert r.status_code == 200
        assert [w for _t, w, _m in incoming()] == ["w1", "w2"]
        assert len(calls["replies"]) == 2

    def test_multiple_changes(self, seeded, calls):
        post(delivery([change(OX, [msg("w1", "one")]),
                       change(OX, [msg("w2", "two", CUST_B)])]))
        assert [w for _t, w, _m in incoming()] == ["w1", "w2"]

    def test_multiple_messages(self, seeded, calls):
        post(delivery([change(OX, [msg("w1", "one"), msg("w2", "two"), msg("w3", "three")])]))
        assert [w for _t, w, _m in incoming()] == ["w1", "w2", "w3"]
        assert [t for _tid, _to, t in calls["replies"]] == ["reply:one", "reply:two", "reply:three"]

    def test_two_tenants_in_one_post_stay_isolated(self, seeded, calls):
        post(delivery([change(OX, [msg("wa", "for ox")])],
                      [change(TB, [msg("wb", "for beta")])]))
        assert incoming(OX) == [(OX, "wa", "for ox")]
        assert incoming(TB) == [(TB, "wb", "for beta")]
        assert sorted((t, x) for t, _to, x in calls["replies"]) == sorted([
            (OX, "reply:for ox"), (TB, "reply:for beta")])

    def test_contact_name_matches_the_sender(self, seeded, calls, monkeypatch):
        names = []
        monkeypatch.setattr(wh, "save_lead_to_sheets", lambda p, n, *a: names.append((p, n)))
        post(delivery([change(OX, [msg("w1", "a", CUST_A), msg("w2", "b", CUST_B)],
                              contacts=[{"wa_id": CUST_B, "profile": {"name": "Bina"}},
                                        {"wa_id": CUST_A, "profile": {"name": "Asha"}}])]))
        assert names == [(CUST_A, "Asha"), (CUST_B, "Bina")]

    def test_second_message_from_a_new_number_is_not_a_second_new_lead(self, seeded, calls):
        post(delivery([change(OX, [msg("w1", "hi"), msg("w2", "again")])]))
        assert calls["followups"] == [(OX, CUST_A)]


# ═══ 2. siblings are never suppressed ═══════════════════════════════════════

class TestSiblingsSurvive:

    @pytest.mark.parametrize("bad", [UNKNOWN_PID, TP, TS], ids=["unknown", "pending", "suspended"])
    def test_dropped_change_does_not_suppress_a_valid_sibling(self, bad, seeded, calls):
        r = post(delivery([change(bad, [msg("wx", "dropped")]),
                           change(OX, [msg("wv", "valid")])]))
        assert r.status_code == 200
        assert incoming() == [(OX, "wv", "valid")]

    @pytest.mark.parametrize("bad", [UNKNOWN_PID, TP, TS], ids=["unknown", "pending", "suspended"])
    def test_dropped_entry_does_not_suppress_another_tenants_entry(self, bad, seeded, calls):
        post(delivery([change(bad, [msg("wx", "dropped")])],
                      [change(TB, [msg("wv", "valid")])]))
        assert incoming() == [(TB, "wv", "valid")]

    @pytest.mark.parametrize("tid", [TP, TS])
    def test_pending_and_suspended_write_nothing(self, tid, seeded, calls):
        post(delivery([change(tid, [msg("w1", "x")])]))
        assert incoming() == [] and calls["replies"] == []

    def test_status_change_does_not_suppress_sibling_messages(self, seeded, calls):
        post(delivery([change(OX, statuses=[{"id": "s1", "status": "read"}]),
                       change(OX, [msg("w1", "after a receipt")])]))
        assert incoming() == [(OX, "w1", "after a receipt")]

    def test_statuses_and_messages_in_the_same_value(self, seeded, calls):
        post(delivery([change(OX, [msg("w1", "both")], statuses=[{"id": "s1"}])]))
        assert incoming() == [(OX, "w1", "both")]

    def test_status_only_delivery_writes_nothing(self, seeded, calls):
        r = post(delivery([change(OX, statuses=[{"id": "s1", "status": "delivered"}])]))
        assert r.status_code == 200 and incoming() == [] and calls["replies"] == []


# ═══ 3. malformed input is isolated; always 200 ═════════════════════════════

class TestMalformedIsolation:

    def test_malformed_message_does_not_drop_its_sibling(self, seeded, calls):
        bad = {"from": CUST_A, "id": "wbad", "type": "text", "text": {}}   # no body
        r = post(delivery([change(OX, [bad, msg("wok", "fine")])]))
        assert r.status_code == 200
        assert incoming() == [(OX, "wok", "fine")]

    def test_failure_mid_processing_does_not_drop_the_next_message(self, seeded, calls, monkeypatch):
        real = wh.smart_reply

        def flaky(msg_text, *a, **k):
            if msg_text == "boom":
                raise RuntimeError("boom")
            return real(msg_text, *a, **k)
        monkeypatch.setattr(wh, "smart_reply", flaky)
        r = post(delivery([change(OX, [msg("w1", "boom"), msg("w2", "fine", CUST_B)])]))
        assert r.status_code == 200
        assert [t for _tid, _to, t in calls["replies"]] == ["reply:fine"]

    @pytest.mark.parametrize("metadata", [None, "not-a-dict", ["x"], {}, {"phone_number_id": ""},
                                          {"phone_number_id": 12345}])
    def test_malformed_metadata_drops_that_change_only(self, metadata, seeded, calls):
        c = change(OX, [msg("wx", "x")])
        if metadata is None:
            del c["value"]["metadata"]
        else:
            c["value"]["metadata"] = metadata
        r = post(delivery([c, change(OX, [msg("wv", "valid")])]))
        assert r.status_code == 200
        assert incoming() == [(OX, "wv", "valid")]

    @pytest.mark.parametrize("payload", [
        {}, {"entry": "x"}, {"entry": [None, 3, "s"]}, {"entry": [{"changes": "x"}]},
        {"entry": [{"changes": [None, {"value": "x"}]}]}, [1, 2], "str"])
    def test_malformed_shapes_are_acknowledged(self, payload, seeded, calls):
        r = post(payload)
        assert r.status_code == 200 and incoming() == []

    def test_non_json_body_is_acknowledged_when_signed(self, seeded, calls):
        r = post(None, raw=b"not json at all")
        assert r.status_code == 200 and incoming() == []

    def test_unsupported_type_is_not_claimed(self, seeded, calls):
        post(delivery([change(OX, [{"from": CUST_A, "id": "wimg", "type": "image",
                                    "image": {"id": "m"}}])]))
        assert incoming() == [] and calls["replies"] == []


# ═══ 4. idempotency ═════════════════════════════════════════════════════════

class TestIdempotency:

    def test_duplicate_wamid_in_one_post_is_processed_once(self, seeded, calls):
        post(delivery([change(OX, [msg("wdup", "hello"), msg("wdup", "hello")])]))
        assert len(incoming()) == 1 and len(calls["replies"]) == 1

    def test_repeated_delivery_is_processed_once(self, seeded, calls):
        payload = delivery([change(OX, [msg("wrep", "hello")])])
        assert post(payload).status_code == 200
        assert post(payload).status_code == 200
        assert len(incoming()) == 1 and len(calls["replies"]) == 1
        assert len(calls["sheets"]) == 1

    def test_the_claim_precedes_every_side_effect(self, seeded, calls):
        post(delivery([change(OX, [msg("w1", "hello")])]))
        assert calls["claimed_before_reply"] == [True]

    def test_the_database_refuses_a_second_incoming_claim(self, seeded):
        from sqlalchemy.exc import IntegrityError
        with _APP.app_context():
            def row(direction, wamid):
                return ConversationMessage(phone=CUST_A, direction=direction, message="m",
                                           wa_message_id=wamid, tenant_id=OX)
            db.session.add(row("incoming", "wu"))
            db.session.commit()
            db.session.add(row("incoming", "wu"))
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()
            # outgoing rows and id-less rows stay unconstrained
            db.session.add_all([row("outgoing", "wu"), row("outgoing", "wu"),
                                row("incoming", None), row("incoming", None)])
            db.session.commit()

    def test_a_lost_race_is_a_duplicate_not_a_failure(self, seeded, calls, caplog):
        """Simulate the concurrent loser: the pre-check misses the winner's row,
        the INSERT then hits the unique index. The message must be treated as a
        duplicate -- no reply -- not as an error."""
        with _APP.app_context():
            db.session.add(ConversationMessage(phone=CUST_A, direction="incoming",
                                               message="hello", wa_message_id="wrace",
                                               tenant_id=OX))
            db.session.commit()
        state = {"first": True}

        class _Q:
            def __getattr__(self, n):
                return getattr(db.session.query(ConversationMessage), n)

            def filter_by(self, **kw):
                real = db.session.query(ConversationMessage)
                if kw.get("wa_message_id") == "wrace" and state["first"]:
                    state["first"] = False
                    return real.filter_by(wa_message_id="__none__")
                return real.filter_by(**kw)
        # Set on the class and deleted afterwards, which re-exposes the
        # inherited query descriptor (monkeypatch would read the descriptor
        # outside an app context to save it).
        assert "query" not in ConversationMessage.__dict__
        ConversationMessage.query = _Q()
        try:
            with caplog.at_level(logging.INFO, logger="app.routes.webhook"):
                r = post(delivery([change(OX, [msg("wrace", "hello")])]))
        finally:
            del ConversationMessage.query
        assert r.status_code == 200
        assert calls["replies"] == []
        assert state["first"] is False, "the race was not simulated"
        text = "\n".join(x.getMessage() for x in caplog.records)
        # A duplicate, recognised as such -- not an error swallowed by the
        # per-message isolation (which would also produce no reply).
        assert "deduplicated" in text and "message failed" not in text

    def test_empty_wamid_is_stored_as_null_and_still_processed(self, seeded, calls):
        post(delivery([change(OX, [msg("", "a"), msg("", "b", CUST_B)])]))
        assert [(w, m) for _t, w, m in incoming()] == [(None, "a"), (None, "b")]
        assert len(calls["replies"]) == 2

    def test_model_and_migration_declare_the_same_partial_unique_index(self):
        idx = {i.name: i for i in ConversationMessage.__table__.indexes}
        u = idx["uq_conv_msg_incoming_wa_message_id"]
        assert u.unique and [c.name for c in u.columns] == ["wa_message_id"]
        where = str(u.dialect_options["postgresql"]["where"])
        assert "direction = 'incoming'" in where and "IS NOT NULL" in where
        mig = open(os.path.join(ROOT, "migrations", "versions",
                                "c7e19d4a2b58_rc2_5_19d_inbound_wamid_uniqueness.py"),
                   encoding="utf-8").read()
        assert "down_revision = 'a4f2c70b19de'" in mig
        assert "uq_conv_msg_incoming_wa_message_id" in mig
        assert "wa_message_id IS NOT NULL AND direction = 'incoming'" in mig
        assert "delete" not in mig.lower().split('"""', 2)[-1], \
            "the migration must not modify data"

    def test_migration_is_the_single_head(self):
        heads, revs = [], {}
        vdir = os.path.join(ROOT, "migrations", "versions")
        for f in os.listdir(vdir):
            if f.endswith(".py"):
                s = open(os.path.join(vdir, f), encoding="utf-8").read()
                r = re.search(r"^revision\s*=\s*['\"](\w+)['\"]", s, re.M)
                d = re.search(r"^down_revision\s*=\s*(.+)$", s, re.M)
                if r:
                    revs[r.group(1)] = re.findall(r"['\"](\w+)['\"]", d.group(1)) if d else []
        children = {p for ps in revs.values() for p in ps}
        heads = [r for r in revs if r not in children]
        # UPDATED BY RC2.5.19-E: e2b7c41d9f63 descends from c7e19d4a2b58.
        assert heads == ["e2b7c41d9f63"], heads
        assert revs["e2b7c41d9f63"] == ["c7e19d4a2b58"]


# ═══ 5. logs ════════════════════════════════════════════════════════════════

class TestLogs:

    def test_no_full_customer_number_in_webhook_logs(self, seeded, calls, caplog):
        with caplog.at_level(logging.DEBUG, logger="app.routes.webhook"):
            post(delivery([change(OX, [msg("w1", "stop"), msg("w2", "hello", CUST_B)])]))
        text = "\n".join(r.getMessage() for r in caplog.records if r.name == "app.routes.webhook")
        assert text, "no webhook log captured"
        assert CUST_A not in text and CUST_B not in text
        assert CUST_B[-3:] in text

    def test_no_exception_text_in_webhook_logs(self, seeded, calls, monkeypatch, caplog):
        def boom(*a, **k):
            raise RuntimeError("SECRET-DETAIL-xyz " + CUST_A)
        monkeypatch.setattr(wh, "smart_reply", boom)
        with caplog.at_level(logging.DEBUG, logger="app.routes.webhook"):
            r = post(delivery([change(OX, [msg("w1", "hi")])]))
        assert r.status_code == 200
        text = "\n".join(r.getMessage() for r in caplog.records if r.name == "app.routes.webhook")
        assert "RuntimeError" in text
        assert "SECRET-DETAIL" not in text and CUST_A not in text

    def test_no_raw_exception_formatting_in_source(self):
        src = open(os.path.join(ROOT, "app", "routes", "webhook.py"), encoding="utf-8").read()
        assert "{e}" not in src
        assert "{from_number}" not in src


# ═══ 6. signature still first; GET handshake hardened ═══════════════════════

class TestAuthentication:

    def test_unsigned_delivery_is_refused_and_writes_nothing(self, seeded, calls):
        r = post(delivery([change(OX, [msg("w1", "x")])]), signed=False)
        assert r.status_code == 403 and incoming() == []

    def test_signature_is_still_the_first_statement(self):
        fn = _func("receive_message")
        first = fn.body[1] if isinstance(fn.body[0], ast.Expr) else fn.body[0]
        called = {n.func.id for n in ast.walk(first)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "verify_meta_signature" in called

    def test_valid_handshake_succeeds(self, monkeypatch):
        monkeypatch.setitem(_APP.config, "VERIFY_TOKEN", VTOKEN)
        r = _APP.test_client().get(
            f"/webhook?hub.mode=subscribe&hub.verify_token={VTOKEN}&hub.challenge=777")
        assert r.status_code == 200 and r.get_data(as_text=True) == "777"

    @pytest.mark.parametrize("query", [
        "hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=1",
        "hub.mode=subscribe&hub.challenge=1",
        "hub.mode=unsubscribe&hub.verify_token=" + VTOKEN + "&hub.challenge=1"])
    def test_bad_handshakes_are_refused(self, query, monkeypatch):
        monkeypatch.setitem(_APP.config, "VERIFY_TOKEN", VTOKEN)
        assert _APP.test_client().get("/webhook?" + query).status_code == 403

    @pytest.mark.parametrize("presented", ["", "anything"])
    def test_missing_verify_token_fails_closed(self, presented, monkeypatch):
        monkeypatch.setitem(_APP.config, "VERIFY_TOKEN", "")
        monkeypatch.setattr(wh, "VERIFY_TOKEN", "")
        r = _APP.test_client().get(
            f"/webhook?hub.mode=subscribe&hub.verify_token={presented}&hub.challenge=1")
        assert r.status_code == 403

    def test_handshake_compares_in_constant_time(self):
        fn = _func("verify_webhook")
        digest = [n for n in ast.walk(fn) if isinstance(n, ast.Attribute)
                  and n.attr == "compare_digest"]
        assert digest, "the handshake no longer uses hmac.compare_digest"
        eq_on_token = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)
                       and any(isinstance(o, (ast.Eq, ast.NotEq)) for o in n.ops)
                       and "token" in ast.unparse(n)]
        assert not eq_on_token, "a token is compared with == again"

    def test_config_has_no_committed_verify_token(self):
        src = open(os.path.join(ROOT, "app", "config.py"), encoding="utf-8").read()
        assert 'VERIFY_TOKEN         = os.environ.get("VERIFY_TOKEN", "")' in src

    def test_the_formerly_published_token_is_not_in_app_source(self):
        """Recognised by hash, so this test does not re-publish it either."""
        import app as app_pkg
        legacy = app_pkg._LEGACY_VERIFY_TOKEN_SHA256
        for root, _d, files in os.walk(os.path.join(ROOT, "app")):
            for f in files:
                if not f.endswith(".py"):
                    continue
                tree = ast.parse(open(os.path.join(root, f), encoding="utf-8").read())
                for n in ast.walk(tree):
                    if isinstance(n, ast.Constant) and isinstance(n.value, str):
                        assert hashlib.sha256(n.value.encode()).hexdigest() != legacy, \
                            f"the published VERIFY_TOKEN default is back in {f}"

    def test_lifecycle_policy_is_unchanged(self):
        from app.services import whatsapp_service as wa
        assert wa.INBOUND_ACCEPTED_TENANT_STATUSES == ("ACTIVE", "TRIAL")
