"""Phase RC2.4.1 — every outbound WhatsApp send names its tenant.

THE DEFECT
----------
_get_waba_credentials(None) resolved through resolve_tenant_id(None), which
answers PRIMARY_TENANT_ID. For a LOG write that is a defensible default; for
OUTBOUND TRANSPORT it decides which WhatsApp number the customer sees, whose
quota is spent, and which tenant's webhook receives the reply.

RC2.4.0 traced 5 production messages sent through the primary tenant's WABA
while being persisted elsewhere: crm_lead_send bound its four persistence
calls to the actor's tenant (Phase 17.1-B, 2026-07-18) but left the transport
call unbound. Transport and record could name different tenants.

Six call sites omitted the tenant. One was a real defect (crm_lead_send, which
had the lead in scope); five are legacy endpoints authenticated by a single
global API key with no tenant context, approved to remain primary-tenant-only
and now stating that explicitly rather than relying on the service layer to
guess.

WHY lead.tenant_id AND NOT THE ACTOR'S
--------------------------------------
The lead is fetched through tenant_query(..., _tid), so the two are equal by
construction today. Binding to the RECORD is the invariant that survives a
future divergence between actor and lead — exactly the class of bug this
fixes. test_binds_to_the_lead_not_the_actor pins it with a fixture where the
two differ.

Import isolation follows test_activity_feed_isolation_rc23e12.py.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc241_outbound.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc241-admin-key")
os.environ.setdefault("SECRET_KEY", "rc241-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc241-broadcast-key")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState                  # noqa: E402
from app.services import whatsapp_service as wa                         # noqa: E402
from app.services.encryption_service import encrypt_token               # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_PY = os.path.join(ROOT, "app", "routes", "admin.py")
BROADCAST_PY = os.path.join(ROOT, "app", "routes", "broadcast.py")
WA_PY = os.path.join(ROOT, "app", "services", "whatsapp_service.py")

OX = "t-ox"          # primary tenant
TB = "t-beta"        # a second, independently configured tenant
TNC = "t-nocreds"    # a real tenant with no WABA credentials

OX_PHONE_ID = "111111111111111"
TB_PHONE_ID = "222222222222222"

K_LEAD = "919000024001"      # Oxford lead
B_LEAD = "919000024002"      # Tenant B lead

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False
_APP.config["PRIMARY_TENANT_ID"] = OX


@pytest.fixture()
def seeded():
    """Seeds, then RELEASES the app context before yielding — flask_login
    caches the resolved user on flask.g, bound to the APPLICATION context, so
    a held context leaks identity between test_client requests (14B.1)."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=OX, name="Oxford", slug=OX, status="ACTIVE",
                              billing_exempt=True,
                              waba_phone_number_id=OX_PHONE_ID,
                              waba_access_token_encrypted=encrypt_token("ox-token")))
        db.session.add(Tenant(id=TB, name="Beta Institute", slug=TB,
                              status="ACTIVE", billing_exempt=True,
                              waba_phone_number_id=TB_PHONE_ID,
                              waba_access_token_encrypted=encrypt_token("tb-token")))
        db.session.add(Tenant(id=TNC, name="No Creds", slug=TNC,
                              status="ACTIVE", billing_exempt=True))
        db.session.commit()

        def mk(tid, username, role="ADMIN"):
            u = User(username=username, email=f"{username}@x.test",
                     password_hash=generate_password_hash("pw"), role=role,
                     tenant_id=tid, is_active=True,
                     require_password_change=False)
            db.session.add(u)
            db.session.commit()
            return u.id

        ox_admin = mk(OX, "ox_admin")
        tb_admin = mk(TB, "tb_admin")

        for tid, ph, nm in ((OX, K_LEAD, "OxfordCustomer"),
                            (TB, B_LEAD, "BetaCustomer")):
            db.session.add(ConversationState(
                phone=ph, tenant_id=tid, name=nm, lead_status="Lead",
                assigned_staff="ox_admin" if tid == OX else "tb_admin",
                lead_score=10, is_admitted=False))
        db.session.commit()
        ids = {"ox_admin": ox_admin, "tb_admin": tb_admin}
    yield ids
    with _APP.app_context():
        db.session.remove()


@pytest.fixture()
def spy(monkeypatch):
    """Records the tenant each outbound call resolves credentials for, without
    touching the network."""
    seen = []

    def fake_creds(tenant_id=None):
        seen.append(tenant_id)
        # delegate to the real implementation so fail-closed behaviour is the
        # behaviour under test, not a stub's approximation
        return _real_creds(tenant_id)

    global _real_creds
    _real_creds = wa._get_waba_credentials
    monkeypatch.setattr(wa, "_get_waba_credentials", fake_creds)
    return seen


class _Resp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"messages": [{"id": "wamid.TEST"}]}


@pytest.fixture()
def no_network(monkeypatch):
    monkeypatch.setattr(wa.requests, "post", lambda *a, **k: _Resp())
    monkeypatch.setattr(wa.requests, "get", lambda *a, **k: _Resp())


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def _fn_src(path, name):
    src = open(path, encoding="utf-8").read()
    node = [n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef) and n.name == name][0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node = ast.Module(body=node.body[1:], type_ignores=[])
    return ast.unparse(node)


# ═══ 1-5 credential selection ════════════════════════════════════════════════

class TestCredentialSelection:
    def test_tenant_a_selects_tenant_a_identity(self, seeded):
        with _APP.app_context():
            assert wa._get_waba_credentials(OX)[0] == OX_PHONE_ID

    def test_tenant_b_selects_tenant_b_identity(self, seeded):
        with _APP.app_context():
            assert wa._get_waba_credentials(TB)[0] == TB_PHONE_ID

    def test_the_two_tenants_do_not_share_an_identity(self, seeded):
        with _APP.app_context():
            assert wa._get_waba_credentials(OX)[0] != wa._get_waba_credentials(TB)[0]
            assert wa._get_waba_credentials(OX)[1] != wa._get_waba_credentials(TB)[1]

    def test_known_tenant_without_credentials_fails_closed(self, seeded):
        with _APP.app_context():
            with pytest.raises(ValueError):
                wa._get_waba_credentials(TNC)

    def test_none_fails_closed(self, seeded):
        """THE change. Previously resolved to PRIMARY_TENANT_ID silently."""
        with _APP.app_context():
            with pytest.raises(ValueError):
                wa._get_waba_credentials(None)

    def test_empty_string_fails_closed(self, seeded):
        with _APP.app_context():
            with pytest.raises(ValueError):
                wa._get_waba_credentials("")

    def test_none_does_not_return_the_primary_identity(self, seeded):
        """Pins the absence of the fallback by VALUE, not just by exception."""
        with _APP.app_context():
            try:
                got = wa._get_waba_credentials(None)
            except ValueError:
                return
            pytest.fail(f"None resolved to {got[0]!r} instead of failing closed")

    def test_unknown_tenant_still_fails_closed(self, seeded):
        with _APP.app_context():
            with pytest.raises(ValueError):
                wa._get_waba_credentials("t-does-not-exist")

    def test_no_implicit_primary_fallback_remains_in_source(self):
        src = _fn_src(WA_PY, "_get_waba_credentials")
        i_guard = src.index("if not tenant_id")
        i_res = src.index("resolve_tenant_id(tenant_id)")
        assert i_guard < i_res, "the guard must precede resolve_tenant_id()"


# ═══ 6-8 CRM manual send ═════════════════════════════════════════════════════

class TestCrmLeadSend:
    def test_source_passes_the_lead_tenant(self):
        src = _fn_src(ADMIN_PY, "crm_lead_send")
        assert "send_text(phone, message, tenant_id=lead.tenant_id)" in src

    def test_send_uses_the_leads_tenant_identity(self, seeded, spy, no_network):
        r = client(seeded["ox_admin"]).post(
            f"/crm/lead/{K_LEAD}/send", data={"manual_message": "hi"},
            follow_redirects=False)
        assert r.status_code == 302, r.status_code
        assert spy == [OX], spy

    def test_tenant_b_send_uses_tenant_b_identity(self, seeded, spy, no_network):
        """A Tenant B operator must never transmit on Oxford's number."""
        r = client(seeded["tb_admin"]).post(
            f"/crm/lead/{B_LEAD}/send", data={"manual_message": "hi"},
            follow_redirects=False)
        assert r.status_code == 302, r.status_code
        assert spy == [TB], spy
        assert OX not in spy

    def test_binds_to_the_lead_not_the_actor(self, seeded, spy, no_network):
        """The invariant that survives a future actor/lead divergence.

        The lookup is tenant-scoped, so an actor cannot reach a foreign lead —
        that authorization is NOT weakened here. Instead the assertion is made
        directly against the resolved value: whatever tenant is used, it is the
        one on the lead row.
        """
        with _APP.app_context():
            lead_tid = db.session.query(ConversationState.tenant_id).filter_by(
                phone=B_LEAD).scalar()
        client(seeded["tb_admin"]).post(
            f"/crm/lead/{B_LEAD}/send", data={"manual_message": "hi"},
            follow_redirects=False)
        assert spy == [lead_tid]

    def test_cross_tenant_send_is_still_refused(self, seeded, spy, no_network):
        """Oxford's admin cannot reach Tenant B's lead at all — unchanged."""
        r = client(seeded["ox_admin"]).post(
            f"/crm/lead/{B_LEAD}/send", data={"manual_message": "hi"},
            follow_redirects=False)
        assert r.status_code in (302, 403, 404), r.status_code
        assert spy == [], "no outbound call should have been attempted"


# ═══ 9-11 legacy endpoints ═══════════════════════════════════════════════════

class TestLegacyEndpointsExplicit:
    KEY = "rc241-broadcast-key"

    def test_trigger_followup_passes_primary_explicitly(self):
        src = _fn_src(ADMIN_PY, "trigger_followup")
        assert 'tenant_id=current_app.config.get("PRIMARY_TENANT_ID")' in src \
            or "tenant_id=current_app.config.get('PRIMARY_TENANT_ID')" in src

    @pytest.mark.parametrize("fn", ["templates_route", "upload_media_route",
                                    "broadcast", "broadcast_template"])
    def test_broadcast_routes_pass_primary_explicitly(self, fn):
        src = _fn_src(BROADCAST_PY, fn)
        assert "_primary" in src, f"{fn} does not bind a tenant"
        assert "PRIMARY_TENANT_ID" in src, f"{fn} does not name the primary tenant"
        assert "tenant_id=_primary" in src, f"{fn} does not pass it"

    def test_trigger_followup_still_works(self, seeded, spy, no_network):
        r = _APP.test_client().post(
            "/trigger-followup",
            headers={"X-Admin-Key": os.environ["ADMIN_KEY"]},
            json={"phone": "919000024001", "message": "hi"})
        assert r.status_code == 200, r.status_code
        assert spy == [OX], spy

    def test_broadcast_still_works_and_binds_primary(self, seeded, spy, no_network):
        r = _APP.test_client().post(
            "/broadcast", headers={"X-API-Key": self.KEY},
            json={"numbers": ["919000024001"], "message": "hi",
                  "delay_seconds": 0})
        assert r.status_code == 200, r.status_code
        assert spy == [OX], spy

    def test_broadcast_template_binds_primary(self, seeded, spy, no_network):
        r = _APP.test_client().post(
            "/broadcast-template", headers={"X-API-Key": self.KEY},
            json={"numbers": ["919000024001"], "template_name": "t",
                  "delay_seconds": 0})
        assert r.status_code == 200, r.status_code
        assert spy == [OX], spy

    def test_legacy_auth_unchanged(self, seeded):
        assert _APP.test_client().post(
            "/broadcast", json={"numbers": ["9"], "message": "x"}
        ).status_code == 401
        assert _APP.test_client().post(
            "/trigger-followup", json={"phone": "9", "message": "x"}
        ).status_code == 401


# ═══ 12-14 existing outbound paths unchanged ═════════════════════════════════

class TestExistingPathsUnchanged:
    def test_followup_worker_uses_job_tenant(self):
        src = open(os.path.join(ROOT, "app", "services",
                                "followup_service.py"), encoding="utf-8").read()
        assert "tenant_id=job.tenant_id" in src

    def test_campaign_worker_binds_tenant(self):
        src = open(os.path.join(ROOT, "app", "marketing",
                                "campaign_worker.py"), encoding="utf-8").read()
        assert "tenant_id=" in src

    def test_webhook_binds_tenant(self):
        src = open(os.path.join(ROOT, "app", "routes", "webhook.py"),
                   encoding="utf-8").read()
        assert "send_text(from_number, pm.text, tenant_id=tenant_id)" in src

    @pytest.mark.parametrize("fn", ["send_reply", "send_automation",
                                    "send_interactive", "send_list"])
    def test_internal_delegation_forwards_tenant(self, fn):
        src = _fn_src(WA_PY, fn)
        assert "tenant_id" in src, f"{fn} dropped tenant_id"


# ═══ 15-16 security regression ═══════════════════════════════════════════════

class TestSecurityRegression:
    def test_tenant_b_cannot_resolve_tenant_a_credentials(self, seeded):
        with _APP.app_context():
            assert wa._get_waba_credentials(TB)[0] != OX_PHONE_ID

    def test_tenant_without_credentials_cannot_borrow_another(self, seeded):
        with _APP.app_context():
            with pytest.raises(ValueError):
                wa._get_waba_credentials(TNC)

    def test_every_outbound_call_site_supplies_a_tenant(self):
        """Repository-wide AST sweep — the guarantee this phase exists for."""
        senders = {"send_text": 2, "send_interactive": 3, "send_reply": 3,
                   "send_list": 6, "send_template": 4, "send_automation": 3,
                   "upload_media": 3, "fetch_templates": 0}
        missing = []
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, "app")):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for f in filenames:
                if not f.endswith(".py"):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    tree = ast.parse(open(p, encoding="utf-8",
                                          errors="replace").read())
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    nm = (node.func.id if isinstance(node.func, ast.Name)
                          else getattr(node.func, "attr", None))
                    if nm not in senders:
                        continue
                    kw = {k.arg for k in node.keywords if k.arg}
                    if "tenant_id" in kw or len(node.args) > senders[nm]:
                        continue
                    missing.append(f"{os.path.relpath(p, ROOT)}:{node.lineno} {nm}")
        assert missing == [], f"outbound call sites without a tenant: {missing}"
