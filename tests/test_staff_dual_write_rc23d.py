"""Phase RC2.3D — staff identity dual-write.

Every path that assigns assigned_staff now also mirrors it into
assigned_user_id, behind STAFF_IDENTITY_DUAL_WRITE (default OFF).

Without this, the column backfilled in RC2.3C would decay with every new
assignment — precisely how pipeline_stage_id froze at 29 rows before Phase
10.6, and how bot-created leads entered with sales_stage_id NULL in 10.8C.

Two test groups carry the weight:

  * FLAG OFF must reproduce today's production behaviour exactly. That is the
    configuration production actually runs, and a control only tested in its
    enabled state can ship silently broken for the default.
  * READERS UNCHANGED — nothing may begin reading assigned_user_id. The legacy
    string stays authoritative until RC2.3E.

Import isolation follows test_pipeline_foundation_10_6.py.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23d_dualwrite.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "testkey")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ.setdefault("BROADCAST_API_KEY", "testbroadcast")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-a")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState, Task            # noqa: E402
from app import flags                                                   # noqa: E402
from app.services import staff_backfill_service as bf                   # noqa: E402
from app.services import task_service                                   # noqa: E402
from app.services.tenant_provisioning_service import provision_tenant   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A = "t-a"
B = "t-b"
_APP = create_app()
_APP.config["TESTING"] = True


@pytest.fixture()
def ctx():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add_all([
            Tenant(id=A, name="A", slug="a", status="ACTIVE", billing_exempt=True),
            Tenant(id=B, name="B", slug="b", status="ACTIVE", billing_exempt=True),
        ])
        db.session.commit()
        provision_tenant(A, commit=True)
        provision_tenant(B, commit=True)
        yield
        db.session.remove()


@pytest.fixture()
def dual_on(monkeypatch):
    monkeypatch.setenv(flags.STAFF_IDENTITY_DUAL_WRITE, "true")
    yield


@pytest.fixture()
def dual_off(monkeypatch):
    monkeypatch.delenv(flags.STAFF_IDENTITY_DUAL_WRITE, raising=False)
    yield


def _user(tenant, username, display=None, role="STAFF"):
    from werkzeug.security import generate_password_hash
    u = User(username=username, display_name=display,
             email=f"{username}.{tenant}@x.test".replace(" ", ""),
             password_hash=generate_password_hash("pw"),
             role=role, tenant_id=tenant, is_active=True,
             require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


def _lead(tenant, phone, staff=None):
    lead = ConversationState(
        phone=phone, name="L", tenant_id=tenant, stage="new",
        course="", goal="", batch_time="", offer_course="",
        last_msg="", last_text="", lead_status="Lead", assigned_staff=staff)
    db.session.add(lead)
    db.session.commit()
    return lead


# ═══ FLAG OFF — production's current configuration ═══════════════════════════

class TestFlagOff:
    """Must reproduce today's behaviour exactly."""

    def test_helper_is_a_noop(self, ctx, dual_off):
        _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        assert bf.sync_assigned_user(lead, A) is None
        assert lead.assigned_user_id is None

    def test_flag_defaults_off(self, ctx, dual_off):
        assert flags.staff_identity_dual_write_enabled() is False

    def test_lead_assignment_leaves_fk_null(self, ctx, dual_off):
        _user(A, "Anju")
        lead = _lead(A, "919000000001")
        lead.assigned_staff = "Anju"
        bf.sync_assigned_user(lead, A)
        db.session.commit()
        db.session.expire_all()
        row = ConversationState.query.get(lead.id)
        assert row.assigned_staff == "Anju"
        assert row.assigned_user_id is None

    def test_task_creation_leaves_fk_null(self, ctx, dual_off):
        _user(A, "Anju")
        task_service.create_task(tenant_id=A, title="T", created_by="Admin",
                                 assigned_staff="Anju")
        db.session.expire_all()
        t = Task.query.filter_by(tenant_id=A).first()
        assert t.assigned_staff == "Anju"
        assert t.assigned_user_id is None

    def test_task_update_leaves_fk_null(self, ctx, dual_off):
        _user(A, "Anju")
        task_service.create_task(tenant_id=A, title="T", created_by="Admin")
        t = Task.query.filter_by(tenant_id=A).first()
        task_service.update_task(tenant_id=A, task_id=t.id, actor="Admin",
                                 assigned_staff="Anju")
        db.session.expire_all()
        assert Task.query.get(t.id).assigned_staff == "Anju"
        assert Task.query.get(t.id).assigned_user_id is None


# ═══ FLAG ON — dual write ════════════════════════════════════════════════════

class TestFlagOn:
    def test_helper_populates_the_fk(self, ctx, dual_on):
        u = _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        assert bf.sync_assigned_user(lead, A) == u.id
        assert lead.assigned_user_id == u.id

    def test_legacy_string_is_never_modified(self, ctx, dual_on):
        """The whole safety argument: assigned_staff stays authoritative."""
        _user(A, "Kiran")
        lead = _lead(A, "919000000001", staff="kiran")
        bf.sync_assigned_user(lead, A)
        db.session.commit()
        db.session.expire_all()
        assert ConversationState.query.get(lead.id).assigned_staff == "kiran"

    def test_task_creation_populates_the_fk(self, ctx, dual_on):
        u = _user(A, "Anju")
        task_service.create_task(tenant_id=A, title="T", created_by="Admin",
                                 assigned_staff="Anju")
        db.session.expire_all()
        t = Task.query.filter_by(tenant_id=A).first()
        assert t.assigned_user_id == u.id
        assert t.assigned_staff == "Anju"

    def test_task_update_populates_the_fk(self, ctx, dual_on):
        u = _user(A, "Anju")
        task_service.create_task(tenant_id=A, title="T", created_by="Admin")
        t = Task.query.filter_by(tenant_id=A).first()
        assert t.assigned_user_id is None
        task_service.update_task(tenant_id=A, task_id=t.id, actor="Admin",
                                 assigned_staff="Anju")
        db.session.expire_all()
        assert Task.query.get(t.id).assigned_user_id == u.id

    def test_reassignment_moves_the_fk(self, ctx, dual_on):
        anju = _user(A, "Anju")
        kiran = _user(A, "Kiran")
        lead = _lead(A, "919000000001", staff="Anju")
        bf.sync_assigned_user(lead, A)
        db.session.commit()
        assert lead.assigned_user_id == anju.id

        lead.assigned_staff = "Kiran"
        bf.sync_assigned_user(lead, A)
        db.session.commit()
        db.session.expire_all()
        assert ConversationState.query.get(lead.id).assigned_user_id == kiran.id

    def test_clearing_the_assignment_clears_the_fk(self, ctx, dual_on):
        """An unassigned lead must not keep a stale owner."""
        _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        bf.sync_assigned_user(lead, A)
        db.session.commit()
        assert lead.assigned_user_id is not None

        lead.assigned_staff = None
        bf.sync_assigned_user(lead, A)
        db.session.commit()
        db.session.expire_all()
        assert ConversationState.query.get(lead.id).assigned_user_id is None

    @pytest.mark.parametrize("blank", [None, "", "   "])
    def test_blank_assignment_clears_the_fk(self, ctx, dual_on, blank):
        _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        bf.sync_assigned_user(lead, A)
        lead.assigned_staff = blank
        bf.sync_assigned_user(lead, A)
        assert lead.assigned_user_id is None


class TestResolutionUnderDualWrite:
    def test_case_insensitive_username(self, ctx, dual_on):
        u = _user(A, "Kiran")
        lead = _lead(A, "919000000001", staff="kiran")
        bf.sync_assigned_user(lead, A)
        assert lead.assigned_user_id == u.id

    def test_display_name_fallback(self, ctx, dual_on):
        u = _user(A, "u_anju", display="Anju Menon")
        lead = _lead(A, "919000000001", staff="anju menon")
        bf.sync_assigned_user(lead, A)
        assert lead.assigned_user_id == u.id

    def test_exact_username_precedence(self, ctx, dual_on):
        by_username = _user(A, "Kiran")
        _user(A, "other", display="Kiran")
        lead = _lead(A, "919000000001", staff="Kiran")
        bf.sync_assigned_user(lead, A)
        assert lead.assigned_user_id == by_username.id

    def test_uses_the_same_resolver_as_the_backfill(self, ctx, dual_on):
        """One implementation — two copies would drift."""
        u = _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        bf.sync_assigned_user(lead, A)
        assert lead.assigned_user_id == bf.resolve_user_id(A, "Anju")[0] == u.id


class TestUnknownStaff:
    def test_unknown_value_leaves_fk_null(self, ctx, dual_on):
        """Production's 'Anju_display' — never guessed."""
        _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju_display")
        assert bf.sync_assigned_user(lead, A) is None
        assert lead.assigned_user_id is None
        assert lead.assigned_staff == "Anju_display"

    def test_unknown_value_does_not_break_the_assignment(self, ctx, dual_on):
        _user(A, "Anju")
        lead = _lead(A, "919000000001")
        lead.assigned_staff = "Anju_display"
        bf.sync_assigned_user(lead, A)
        db.session.commit()
        db.session.expire_all()
        assert ConversationState.query.get(lead.id).assigned_staff == "Anju_display"

    def test_ambiguous_display_name_leaves_fk_null(self, ctx, dual_on):
        _user(A, "u1", display="Same Name")
        _user(A, "u2", display="Same Name")
        lead = _lead(A, "919000000001", staff="Same Name")
        bf.sync_assigned_user(lead, A)
        assert lead.assigned_user_id is None

    def test_resolver_failure_never_raises(self, ctx, dual_on, monkeypatch):
        """A mirror write must never break a lead assignment."""
        _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")

        def boom(*a, **kw):
            raise RuntimeError("resolver down")
        monkeypatch.setattr(bf, "resolve_user_id", boom)
        assert bf.sync_assigned_user(lead, A) is None
        assert lead.assigned_staff == "Anju"


class TestTenantIsolation:
    def test_never_resolves_across_tenants(self, ctx, dual_on):
        _user(B, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        bf.sync_assigned_user(lead, A)
        assert lead.assigned_user_id is None

    def test_same_username_two_tenants_resolves_locally(self, ctx, dual_on):
        ua = _user(A, "NIBU S S")
        ub = _user(B, "NIBU S S")
        la = _lead(A, "919000000001", staff="NIBU S S")
        lb = _lead(B, "919000000002", staff="NIBU S S")
        bf.sync_assigned_user(la, A)
        bf.sync_assigned_user(lb, B)
        assert la.assigned_user_id == ua.id
        assert lb.assigned_user_id == ub.id
        assert ua.id != ub.id

    def test_missing_tenant_context_leaves_fk_null(self, ctx, dual_on):
        _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        bf.sync_assigned_user(lead, None)
        assert lead.assigned_user_id is None


# ═══ Readers unchanged ═══════════════════════════════════════════════════════

class TestNoReaderMigration:
    ALLOWED = {"models.py", "staff_backfill_service.py", "staff_service.py"}

    def test_no_module_reads_assigned_user_id(self):
        """RC2.3D writes only. Reader migration is RC2.3E."""
        offenders = []
        for dp, _d, fs in os.walk(os.path.join(ROOT, "app")):
            if "__pycache__" in dp:
                continue
            for f in fs:
                if not f.endswith(".py") or f in self.ALLOWED:
                    continue
                full = os.path.join(dp, f)
                try:
                    tree = ast.parse(open(full, encoding="utf-8").read())
                except SyntaxError:
                    continue
                for n in ast.walk(tree):
                    if isinstance(n, ast.Attribute) and n.attr == "assigned_user_id":
                        offenders.append(os.path.relpath(full, ROOT))
        assert sorted(set(offenders)) == [], f"readers appeared: {set(offenders)}"

    def test_read_fk_flag_is_unread(self):
        offenders = []
        for dp, _d, fs in os.walk(os.path.join(ROOT, "app")):
            if "__pycache__" in dp:
                continue
            for f in fs:
                if not f.endswith(".py") or f == "flags.py":
                    continue
                with open(os.path.join(dp, f), encoding="utf-8") as fh:
                    if "staff_identity_read_fk_enabled" in fh.read():
                        offenders.append(f)
        assert offenders == [], f"RC2.3E flag read early: {offenders}"

    def test_registry_still_authoritative(self):
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            body = fh.read()
        # Was >=15 at RC2.3D. RC2.2D has since migrated six consumers by
        # approved plan; the remaining count is owned by
        # test_staff_batch1_rc22d.py.
        assert body.count("load_staff_registry()") > 0

    def test_every_assignment_site_is_paired(self):
        """Each assigned_staff write must be followed by a dual-write call, or
        the backfilled column decays with every new assignment."""
        import re
        for path, expected in (("app/routes/admin.py", 5),
                               ("app/services/task_service.py", 2)):
            body = open(os.path.join(ROOT, path), encoding="utf-8").read()
            calls = len(re.findall(r"sync_assigned_user\(", body))
            # admin.py: 1 import alias + 5 calls; task_service: 2 imports + 2 calls
            assert calls >= expected, f"{path}: only {calls} dual-write calls"


class TestNoOtherSubsystemTouched:
    def test_notifications_untouched(self, ctx, dual_on):
        from app.models import Notification
        _user(A, "Anju")
        db.session.add(Notification(tenant_id=A, recipient="Anju_display",
                                    notif_type="TASK_ASSIGNED", title="t"))
        db.session.commit()
        before = [(n.id, n.recipient) for n in Notification.query.all()]
        lead = _lead(A, "919000000001", staff="Anju")
        bf.sync_assigned_user(lead, A)
        db.session.commit()
        db.session.expire_all()
        assert [(n.id, n.recipient) for n in Notification.query.all()] == before

    def test_conversation_message_untouched(self, ctx, dual_on):
        from app.models import ConversationMessage
        _user(A, "Anju")
        db.session.add(ConversationMessage(tenant_id=A, phone="919000000001",
                                           direction="incoming", message="hi",
                                           staff_name="Anju"))
        db.session.commit()
        before = [(m.id, m.staff_name) for m in ConversationMessage.query.all()]
        lead = _lead(A, "919000000001", staff="Anju")
        bf.sync_assigned_user(lead, A)
        db.session.commit()
        db.session.expire_all()
        assert [(m.id, m.staff_name)
                for m in ConversationMessage.query.all()] == before

    def test_display_name_never_populated(self, ctx, dual_on):
        _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        bf.sync_assigned_user(lead, A)
        db.session.commit()
        db.session.expire_all()
        assert all(u.display_name is None for u in User.query.all())

    def test_pipeline_fields_untouched(self, ctx, dual_on):
        _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        before = (lead.sales_stage_id, lead._stage, lead.pipeline_stage_id,
                  lead.lead_status)
        bf.sync_assigned_user(lead, A)
        db.session.commit()
        db.session.expire_all()
        after = ConversationState.query.get(lead.id)
        assert (after.sales_stage_id, after._stage, after.pipeline_stage_id,
                after.lead_status) == before


# ═══ Rollback ════════════════════════════════════════════════════════════════

class TestRollback:
    def test_turning_the_flag_off_stops_mirroring(self, ctx, monkeypatch):
        _user(A, "Anju")
        monkeypatch.setenv(flags.STAFF_IDENTITY_DUAL_WRITE, "true")
        a = _lead(A, "919000000001", staff="Anju")
        bf.sync_assigned_user(a, A)
        assert a.assigned_user_id is not None

        monkeypatch.setenv(flags.STAFF_IDENTITY_DUAL_WRITE, "false")
        b = _lead(A, "919000000002", staff="Anju")
        bf.sync_assigned_user(b, A)
        assert b.assigned_user_id is None, "flag off must stop mirroring"

    def test_flag_is_read_live_not_cached(self, ctx, monkeypatch):
        """Rollback is an env toggle with no redeploy."""
        _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        monkeypatch.setenv(flags.STAFF_IDENTITY_DUAL_WRITE, "false")
        assert bf.sync_assigned_user(lead, A) is None
        monkeypatch.setenv(flags.STAFF_IDENTITY_DUAL_WRITE, "true")
        assert bf.sync_assigned_user(lead, A) is not None

    def test_clearing_the_fk_restores_pre_dual_write_state(self, ctx, dual_on):
        _user(A, "Anju")
        lead = _lead(A, "919000000001", staff="Anju")
        bf.sync_assigned_user(lead, A)
        db.session.commit()

        for row in ConversationState.query.all():
            row.assigned_user_id = None
        db.session.commit()
        db.session.expire_all()
        row = ConversationState.query.get(lead.id)
        assert row.assigned_user_id is None
        assert row.assigned_staff == "Anju"
