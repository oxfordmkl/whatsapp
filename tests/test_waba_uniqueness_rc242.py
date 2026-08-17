"""Phase RC2.4.2 — one WhatsApp identity belongs to at most one tenant.

THE DEFECT
----------
tenants.waba_phone_number_id had no constraint and no index, and the settings
save path validated only .isdigit() — no duplicate check of any kind. Any
tenant's own ADMIN could enter ANOTHER tenant's Meta Phone Number ID through
/tenant/whatsapp/save, a normal @tenant_admin_required form.

The inbound webhook resolves the tenant with

    Tenant.query.filter_by(waba_phone_number_id=phone_number_id).first()

on that non-unique column with no ORDER BY. Two matching rows would be
resolved arbitrarily, so one tenant could begin receiving another tenant's
customer conversations — the same class of defect as the Tenant.query.first()
mis-filing traced in RC2.4.0.

THE THREE LAYERS
----------------
    application check  -> friendly rejection, names no other tenant
    unique index       -> the integrity boundary, wins any race
    webhook .first()   -> now provably at most one row, so it is deterministic

The application check is deliberately NOT the boundary:
test_db_constraint_is_the_real_boundary bypasses it entirely and asserts the
database still refuses.

CLEARING RELEASES THE TOKEN TOO
-------------------------------
Not an invention: this system already treats id + token as a pair at three
independent sites (_get_waba_credentials, tenant_whatsapp_test, and the
settings template), and production holds 0 half-configured rows in either
direction. Clearing only the id would create a state no consumer expects.

NOTE ON SQLITE
--------------
The suite runs on SQLite, where a partial unique index is expressed as
CREATE UNIQUE INDEX ... WHERE. The fixture creates the same index by hand so
the constraint is exercised here; migration b8f4c2e97d15 is what creates it in
PostgreSQL, and test_migration_defines_the_approved_index pins its definition.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc242_waba.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc242-admin-key")
os.environ.setdefault("SECRET_KEY", "rc242-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc242-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from sqlalchemy.exc import IntegrityError                                # noqa: E402
from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User                                     # noqa: E402
from app.services.encryption_service import encrypt_token               # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TENANT_PY = os.path.join(ROOT, "app", "routes", "tenant.py")
WEBHOOK_PY = os.path.join(ROOT, "app", "routes", "webhook.py")
MIGRATION = os.path.join(ROOT, "migrations", "versions",
                         "b8f4c2e97d15_rc2_4_2_waba_identity_uniqueness.py")
INDEX_NAME = "uq_tenants_waba_phone_number_id"

OX = "t-ox"
TB = "t-beta"
TC = "t-gamma"

PHONE_A = "111111111111111"
PHONE_B = "222222222222222"
FREE = "999999999999999"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False


@pytest.fixture()
def seeded():
    """Seeds, then RELEASES the app context before yielding — flask_login
    caches the resolved user on flask.g, bound to the APPLICATION context, so
    a held context leaks identity between test_client requests (14B.1)."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        # The same partial unique index migration b8f4c2e97d15 creates.
        db.session.execute(db.text(
            f"CREATE UNIQUE INDEX {INDEX_NAME} ON tenants "
            f"(waba_phone_number_id) WHERE waba_phone_number_id IS NOT NULL"))
        db.session.commit()

        db.session.add(Tenant(id=OX, name="Oxford", slug=OX, status="ACTIVE",
                              billing_exempt=True,
                              waba_phone_number_id=PHONE_A,
                              waba_access_token_encrypted=encrypt_token("ox-tok")))
        db.session.add(Tenant(id=TB, name="Beta", slug=TB, status="ACTIVE",
                              billing_exempt=True,
                              waba_phone_number_id=PHONE_B,
                              waba_access_token_encrypted=encrypt_token("tb-tok")))
        # Unconfigured — proves multiple NULLs coexist under the index.
        db.session.add(Tenant(id=TC, name="Gamma", slug=TC, status="ACTIVE",
                              billing_exempt=True))
        db.session.commit()

        def mk(tid, username, role="ADMIN"):
            u = User(username=username, email=f"{username}@x.test",
                     password_hash=generate_password_hash("pw"), role=role,
                     tenant_id=tid, is_active=True, require_password_change=False)
            db.session.add(u)
            db.session.commit()
            return u.id

        ids = {"ox_admin": mk(OX, "ox_admin"), "tb_admin": mk(TB, "tb_admin"),
               "tc_admin": mk(TC, "tc_admin"), "ox_staff": mk(OX, "ox_staff", "STAFF")}
    yield ids
    with _APP.app_context():
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def save(uid, phone_id, token=""):
    return client(uid).post("/tenant/whatsapp/save",
                            data={"phone_number_id": phone_id,
                                  "access_token": token},
                            follow_redirects=True)


def clear(uid):
    return client(uid).post("/tenant/whatsapp/clear", follow_redirects=True)


def waba_of(tid):
    with _APP.app_context():
        t = db.session.get(Tenant, tid)
        return t.waba_phone_number_id, t.waba_access_token_encrypted


def _fn_src(path, name):
    src = open(path, encoding="utf-8").read()
    node = [n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef) and n.name == name][0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node = ast.Module(body=node.body[1:], type_ignores=[])
    return ast.unparse(node)


# ═══ 1-6 database uniqueness ═════════════════════════════════════════════════

class TestDatabaseUniqueness:
    def test_tenant_a_holds_phone_a(self, seeded):
        assert waba_of(OX)[0] == PHONE_A

    def test_two_tenants_may_both_be_null(self, seeded):
        with _APP.app_context():
            db.session.add(Tenant(id="t-null2", name="N2", slug="t-null2",
                                  status="ACTIVE", billing_exempt=True))
            db.session.commit()
            n = Tenant.query.filter(
                Tenant.waba_phone_number_id.is_(None)).count()
        assert n >= 2, "the partial index must permit many NULLs"

    def test_db_constraint_is_the_real_boundary(self, seeded):
        """Bypasses the application check entirely — the index must refuse."""
        with _APP.app_context():
            t = db.session.get(Tenant, TB)
            t.waba_phone_number_id = PHONE_A
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_direct_insert_of_a_duplicate_is_refused(self, seeded):
        with _APP.app_context():
            db.session.add(Tenant(id="t-dup", name="Dup", slug="t-dup",
                                  status="ACTIVE", billing_exempt=True,
                                  waba_phone_number_id=PHONE_A))
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_own_unchanged_value_saves_at_db_level(self, seeded):
        with _APP.app_context():
            t = db.session.get(Tenant, OX)
            t.waba_phone_number_id = PHONE_A          # no-op update
            db.session.commit()
        assert waba_of(OX)[0] == PHONE_A

    def test_migration_defines_the_approved_index(self):
        src = open(MIGRATION, encoding="utf-8").read()
        assert "revision = 'b8f4c2e97d15'" in src
        assert "down_revision = 'a3d7f21c94e8'" in src
        assert INDEX_NAME in src
        assert "unique=True" in src
        assert "waba_phone_number_id IS NOT NULL" in src
        assert "op.create_index" in src
        assert "op.drop_index" in src

    def test_migration_downgrade_drops_only_this_index(self):
        src = open(MIGRATION, encoding="utf-8").read()
        node = [n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.FunctionDef) and n.name == "downgrade"][0]
        body = ast.unparse(node)
        assert body.count("op.") == 1, "downgrade must touch exactly one object"
        assert "drop_index" in body

    def test_migration_adds_no_other_schema_object(self):
        src = open(MIGRATION, encoding="utf-8").read()
        for forbidden in ("add_column", "drop_column", "alter_column",
                          "create_table", "drop_table", "drop_constraint"):
            assert forbidden not in src, forbidden


# ═══ 7-9 application collision protection ════════════════════════════════════

class TestApplicationCollisionCheck:
    def test_new_unused_id_is_accepted(self, seeded):
        save(seeded["tc_admin"], FREE)
        assert waba_of(TC)[0] == FREE

    def test_own_existing_id_is_accepted(self, seeded):
        """The form posts the current value back on every save, so a no-op
        save must not report a collision with itself."""
        r = save(seeded["ox_admin"], PHONE_A)
        assert r.status_code == 200
        assert waba_of(OX)[0] == PHONE_A
        assert b"already configured" not in r.data

    def test_another_tenants_id_is_rejected(self, seeded):
        r = save(seeded["tb_admin"], PHONE_A)
        assert b"already configured" in r.data
        assert waba_of(TB)[0] == PHONE_B, "Tenant B's own id must be untouched"
        assert waba_of(OX)[0] == PHONE_A, "Tenant A must be untouched"

    def test_rejection_does_not_name_the_other_tenant(self, seeded):
        r = save(seeded["tb_admin"], PHONE_A)
        body = r.data.decode(errors="replace")
        assert "Oxford" not in body.split("already configured")[0][-400:]
        assert OX not in body

    def test_non_numeric_still_rejected(self, seeded):
        r = save(seeded["tc_admin"], "not-a-number")
        assert b"numeric" in r.data.lower()
        assert waba_of(TC)[0] is None

    def test_check_precedes_assignment(self):
        src = _fn_src(TENANT_PY, "tenant_whatsapp_save")
        assert src.index("_clash") < src.index(
            "tenant.waba_phone_number_id = phone_number_id")

    def test_integrity_error_is_handled_readably(self):
        src = _fn_src(TENANT_PY, "tenant_whatsapp_save")
        assert "except IntegrityError:" in src
        assert "already configured" in src


# ═══ 10-13 clear / release path ══════════════════════════════════════════════

class TestClearPath:
    def test_clear_sets_id_to_null(self, seeded):
        clear(seeded["ox_admin"])
        assert waba_of(OX)[0] is None

    def test_clear_also_releases_the_paired_token(self, seeded):
        """id + token are a pair everywhere this system reads them; clearing
        one without the other would create a half-configured state."""
        clear(seeded["ox_admin"])
        assert waba_of(OX) == (None, None)

    def test_cleared_id_can_be_taken_by_another_tenant(self, seeded):
        """The whole point: release makes the number reusable."""
        clear(seeded["ox_admin"])
        save(seeded["tc_admin"], PHONE_A)
        assert waba_of(TC)[0] == PHONE_A
        assert waba_of(OX)[0] is None

    def test_clear_before_release_would_have_collided(self, seeded):
        """Same assignment WITHOUT clearing must still be refused."""
        r = save(seeded["tc_admin"], PHONE_A)
        assert b"already configured" in r.data
        assert waba_of(TC)[0] is None

    def test_clear_touches_only_the_callers_tenant(self, seeded):
        clear(seeded["ox_admin"])
        assert waba_of(TB)[0] == PHONE_B, "another tenant was modified"

    def test_clear_deletes_no_tenant_or_user(self, seeded):
        with _APP.app_context():
            before = (Tenant.query.count(), User.query.count())
        clear(seeded["ox_admin"])
        with _APP.app_context():
            assert (Tenant.query.count(), User.query.count()) == before

    def test_clear_with_nothing_configured_is_a_no_op(self, seeded):
        r = clear(seeded["tc_admin"])
        assert r.status_code == 200
        assert waba_of(TC) == (None, None)

    def test_staff_cannot_clear(self, seeded):
        r = client(seeded["ox_staff"]).post("/tenant/whatsapp/clear",
                                            follow_redirects=False)
        assert r.status_code == 403, r.status_code
        assert waba_of(OX)[0] == PHONE_A

    def test_unauthenticated_cannot_clear(self, seeded):
        r = _APP.test_client().post("/tenant/whatsapp/clear",
                                    follow_redirects=False)
        assert r.status_code in (302, 401, 403), r.status_code
        assert waba_of(OX)[0] == PHONE_A

    def test_clear_route_is_authorization_protected(self):
        src = open(TENANT_PY, encoding="utf-8").read()
        i = src.index("def tenant_whatsapp_clear")
        head = src[max(0, i - 300):i]
        assert "@tenant_admin_required" in head
        assert "@login_required" in head
        assert "methods=['POST']" in head or 'methods=["POST"]' in head

    def test_clear_is_audited(self):
        src = _fn_src(TENANT_PY, "tenant_whatsapp_clear")
        assert "log_audit" in src


# ═══ 14-17 webhook ═══════════════════════════════════════════════════════════

class TestWebhookUnchanged:
    def test_unique_id_resolves_to_exactly_one_tenant(self, seeded):
        with _APP.app_context():
            rows = Tenant.query.filter_by(waba_phone_number_id=PHONE_A).all()
        assert len(rows) == 1 and rows[0].id == OX

    def test_resolution_call_site_is_unchanged(self):
        src = open(WEBHOOK_PY, encoding="utf-8").read()
        assert ("Tenant.query.filter_by(waba_phone_number_id=phone_number_id)"
                ".first()" in src), "webhook resolution was redesigned"

    def test_unknown_id_still_dropped(self):
        src = _fn_src(WEBHOOK_PY, "receive_message")
        assert "Unknown WABA Phone ID" in src

    def test_non_active_tenant_still_rejected(self):
        src = _fn_src(WEBHOOK_PY, "receive_message")
        assert "ACTIVE" in src and "TRIAL" in src

    def test_env_fallback_guard_preserved(self):
        src = _fn_src(WEBHOOK_PY, "receive_message")
        assert "PHONE_NUMBER_ID" in src and "PRIMARY_TENANT_ID" in src

    def test_hmac_verification_preserved(self):
        src = open(WEBHOOK_PY, encoding="utf-8").read()
        assert "verify_meta_signature()" in src


# ═══ 18-20 RC2.4.1 outbound regression ═══════════════════════════════════════

class TestOutboundRegressionRC241:
    def test_tenant_binding_still_fails_closed(self, seeded):
        from app.services import whatsapp_service as wa
        with _APP.app_context():
            with pytest.raises(ValueError):
                wa._get_waba_credentials(None)

    def test_tenant_b_resolves_tenant_b_credentials(self, seeded):
        from app.services import whatsapp_service as wa
        with _APP.app_context():
            assert wa._get_waba_credentials(TB)[0] == PHONE_B

    def test_tenant_without_credentials_still_fails_closed(self, seeded):
        from app.services import whatsapp_service as wa
        with _APP.app_context():
            with pytest.raises(ValueError):
                wa._get_waba_credentials(TC)

    def test_crm_lead_send_still_binds_the_lead_tenant(self):
        admin_py = os.path.join(ROOT, "app", "routes", "admin.py")
        assert "send_text(phone, message, tenant_id=lead.tenant_id)" in \
            _fn_src(admin_py, "crm_lead_send")


# ═══ 21-23 multi-tenant security ═════════════════════════════════════════════

class TestMultiTenantSecurity:
    def test_tenant_a_cannot_claim_tenant_b_id(self, seeded):
        save(seeded["ox_admin"], PHONE_B)
        assert waba_of(OX)[0] == PHONE_A
        assert waba_of(TB)[0] == PHONE_B

    def test_tenant_b_cannot_claim_tenant_a_id(self, seeded):
        save(seeded["tb_admin"], PHONE_A)
        assert waba_of(TB)[0] == PHONE_B
        assert waba_of(OX)[0] == PHONE_A

    def test_settings_flow_cannot_mutate_another_tenant(self, seeded):
        before_ox, before_tb = waba_of(OX), waba_of(TB)
        save(seeded["tc_admin"], FREE)
        assert waba_of(OX) == before_ox
        assert waba_of(TB) == before_tb

    def test_save_route_still_authorization_protected(self, seeded):
        r = client(seeded["ox_staff"]).post(
            "/tenant/whatsapp/save",
            data={"phone_number_id": FREE, "access_token": ""},
            follow_redirects=False)
        assert r.status_code == 403, r.status_code
        assert waba_of(OX)[0] == PHONE_A
