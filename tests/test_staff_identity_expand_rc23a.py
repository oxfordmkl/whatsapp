"""Phase RC2.3A — staff identity EXPAND.

The Expand phase adds columns and an abstraction and wires NOTHING. So the
load-bearing tests here are the DORMANCY tests: they assert that
staff_master.json is still the source of truth, that the new columns stay NULL,
and that no consumer calls the new service. Getting "it works" right is easy;
proving "nothing changed" is the part that protects production.

If a later phase wires something up, the dormancy tests fail — deliberately.
They are the tripwire that says the Expand boundary has been crossed, and they
should be updated in that phase, never deleted.

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

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23a_expand.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "testkey")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ.setdefault("BROADCAST_API_KEY", "testbroadcast")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-rc23a")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import (                                                # noqa: E402
    Tenant, User, ConversationState, Task,
)
from app.services import staff_service                                  # noqa: E402
from app import flags                                                   # noqa: E402
from app.services.tenant_provisioning_service import provision_tenant   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A = "t-rc23a"
B = "t-rc23a-other"
_APP = create_app()
_APP.config["TESTING"] = True


def _user(tenant, username, display=None, role="STAFF", active=True):
    from werkzeug.security import generate_password_hash
    u = User(username=username, display_name=display,
             email=f"{username}.{tenant}@x.test",
             password_hash=generate_password_hash("pw"),
             role=role, tenant_id=tenant, is_active=active,
             require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


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


# ═══ DORMANCY — the Expand boundary ══════════════════════════════════════════

class TestNothingIsWired:
    """These are the tests that make Expand safe."""

    def test_staff_master_json_still_exists(self):
        assert os.path.exists(os.path.join(ROOT, "app", "data", "staff_master.json"))

    def test_registry_functions_are_untouched(self):
        from app.routes import admin
        assert hasattr(admin, "load_staff_registry")
        assert hasattr(admin, "save_staff_registry")

    def test_no_module_imports_staff_service_yet(self):
        """The abstraction exists; nothing consumes it."""
        offenders = []
        for dirpath, _d, files in os.walk(os.path.join(ROOT, "app")):
            if "__pycache__" in dirpath:
                continue
            for name in files:
                if not name.endswith(".py"):
                    continue
                full = os.path.join(dirpath, name)
                if os.path.basename(full) == "staff_service.py":
                    continue
                with open(full, encoding="utf-8") as fh:
                    if "staff_service" in fh.read():
                        offenders.append(os.path.relpath(full, ROOT))
        assert offenders == [], f"staff_service wired early: {offenders}"

    def test_admin_still_reads_the_global_registry(self):
        """AST, not string matching — docstrings mention the registry."""
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "load_staff_registry"]
        assert len(calls) >= 15, f"consumers changed: {len(calls)}"

    def test_no_code_reads_the_new_flags(self):
        offenders = []
        for dirpath, _d, files in os.walk(os.path.join(ROOT, "app")):
            if "__pycache__" in dirpath:
                continue
            for name in files:
                if not name.endswith(".py") or name == "flags.py":
                    continue
                full = os.path.join(dirpath, name)
                with open(full, encoding="utf-8") as fh:
                    body = fh.read()
                if ("staff_identity_dual_write_enabled" in body
                        or "staff_identity_read_fk_enabled" in body):
                    offenders.append(os.path.relpath(full, ROOT))
        assert offenders == [], f"flags read early: {offenders}"

    def test_no_code_writes_assigned_user_id(self):
        """AST, not string matching. flags.py mentions the column in a comment
        explaining what the flag gates — a textual scan flags that as a write,
        which is exactly the false positive that has bitten this project
        before. Only a real assignment or kwarg counts."""
        offenders = []
        for dirpath, _d, files in os.walk(os.path.join(ROOT, "app")):
            if "__pycache__" in dirpath:
                continue
            for name in files:
                if not name.endswith(".py") or name == "models.py":
                    continue
                full = os.path.join(dirpath, name)
                with open(full, encoding="utf-8") as fh:
                    try:
                        tree = ast.parse(fh.read())
                    except SyntaxError:
                        continue
                for node in ast.walk(tree):
                    # lead.assigned_user_id = ...
                    if isinstance(node, ast.Assign):
                        for t in node.targets:
                            if (isinstance(t, ast.Attribute)
                                    and t.attr == "assigned_user_id"):
                                offenders.append(os.path.relpath(full, ROOT))
                    # Model(assigned_user_id=...)
                    elif isinstance(node, ast.Call):
                        if any(k.arg == "assigned_user_id"
                               for k in node.keywords if k.arg):
                            offenders.append(os.path.relpath(full, ROOT))
        assert sorted(set(offenders)) == [], f"FK written early: {set(offenders)}"


class TestFlagsDefaultOff:
    def test_dual_write_defaults_off(self, monkeypatch):
        monkeypatch.delenv(flags.STAFF_IDENTITY_DUAL_WRITE, raising=False)
        assert flags.staff_identity_dual_write_enabled() is False

    def test_read_fk_defaults_off(self, monkeypatch):
        monkeypatch.delenv(flags.STAFF_IDENTITY_READ_FK, raising=False)
        assert flags.staff_identity_read_fk_enabled() is False

    def test_flags_are_read_live_not_cached(self, monkeypatch):
        """The app/flags.py contract — rollback is an env toggle, no redeploy."""
        monkeypatch.setenv(flags.STAFF_IDENTITY_DUAL_WRITE, "true")
        assert flags.staff_identity_dual_write_enabled() is True
        monkeypatch.setenv(flags.STAFF_IDENTITY_DUAL_WRITE, "false")
        assert flags.staff_identity_dual_write_enabled() is False


# ═══ Schema — additive and inert ═════════════════════════════════════════════

class TestSchemaIsAdditive:
    def test_new_columns_exist(self, ctx):
        assert hasattr(User, "display_name")
        assert hasattr(ConversationState, "assigned_user_id")
        assert hasattr(Task, "assigned_user_id")

    def test_legacy_columns_are_untouched(self, ctx):
        assert hasattr(ConversationState, "assigned_staff")
        assert hasattr(Task, "assigned_staff")
        assert hasattr(Task, "created_by") and hasattr(Task, "completed_by")

    def test_a_lead_created_the_old_way_leaves_the_fk_null(self, ctx):
        """No column default — the Phase 10.8C regression guard. A default is
        applied at flush and never passes through a setter."""
        lead = ConversationState(
            phone="919000000001", name="X", tenant_id=A, stage="new",
            course="", goal="", batch_time="", offer_course="",
            last_msg="", last_text="", lead_status="Lead",
            assigned_staff="Anju")
        db.session.add(lead)
        db.session.commit()
        assert lead.assigned_user_id is None
        assert lead.assigned_staff == "Anju"

    def test_a_user_created_the_old_way_leaves_display_name_null(self, ctx):
        u = _user(A, "kiran")
        assert u.display_name is None

    def test_display_label_falls_back_to_username(self, ctx):
        assert _user(A, "kiran").display_label() == "kiran"

    def test_display_label_prefers_display_name(self, ctx):
        assert _user(A, "kiran2", display="Kiran Nair").display_label() == "Kiran Nair"

    def test_display_label_ignores_blank_display_name(self, ctx):
        assert _user(A, "kiran3", display="   ").display_label() == "kiran3"

    def test_fk_accepts_a_user_id(self, ctx):
        """Column is usable when a later phase populates it."""
        u = _user(A, "anju")
        lead = ConversationState(
            phone="919000000002", name="X", tenant_id=A, stage="new",
            course="", goal="", batch_time="", offer_course="",
            last_msg="", last_text="", lead_status="Lead",
            assigned_user_id=u.id)
        db.session.add(lead)
        db.session.commit()
        assert lead.assigned_user_id == u.id


# ═══ staff_service ═══════════════════════════════════════════════════════════

class TestStaffServiceIsTenantScoped:
    def test_lists_only_this_tenants_staff(self, ctx):
        _user(A, "anju")
        _user(B, "bob")
        names = [u.username for u in staff_service.list_staff(A)]
        assert names == ["anju"]

    def test_same_username_in_two_tenants_stays_separate(self, ctx):
        """Production has 'NIBU S S' in FOUR tenants — uniqueness is
        (tenant_id, username), not global."""
        _user(A, "NIBU S S")
        _user(B, "NIBU S S")
        assert len(staff_service.list_staff(A)) == 1
        assert len(staff_service.list_staff(B)) == 1
        assert staff_service.list_staff(A)[0].id != staff_service.list_staff(B)[0].id

    def test_missing_tenant_id_fails_closed(self, ctx):
        _user(A, "anju")
        assert staff_service.list_staff(None) == []
        assert staff_service.as_registry(None) == {}
        assert staff_service.active_display_names(None) == []

    def test_active_only_filter(self, ctx):
        _user(A, "anju", active=True)
        _user(A, "kiran", active=False)
        assert [u.username for u in staff_service.list_staff(A, active_only=True)] == ["anju"]

    def test_admins_excluded_by_default(self, ctx):
        _user(A, "anju", role="STAFF")
        _user(A, "bossman", role="ADMIN")
        assert [u.username for u in staff_service.list_staff(A)] == ["anju"]
        assert len(staff_service.list_staff(A, include_admins=True)) == 2


class TestRegistryShapeCompatibility:
    def test_shape_matches_load_staff_registry(self, ctx):
        """Same shape = consumers migrate by changing one line."""
        _user(A, "anju", display="Anju")
        reg = staff_service.as_registry(A)
        assert list(reg) == ["ANJU"]
        assert set(reg["ANJU"]) == {"display_name", "role", "active"}
        assert reg["ANJU"]["display_name"] == "Anju"
        assert reg["ANJU"]["active"] is True

    def test_shape_is_identical_to_the_real_file(self, ctx):
        """Compare against the actual staff_master.json structure."""
        import json
        with open(os.path.join(ROOT, "app", "data", "staff_master.json"),
                  encoding="utf-8") as fh:
            live = json.load(fh)
        _user(A, "anju")
        mine = staff_service.as_registry(A)
        assert set(next(iter(live.values()))) == set(next(iter(mine.values())))

    def test_code_is_derived_not_stored(self, ctx):
        _user(A, "kiran")
        assert "KIRAN" in staff_service.as_registry(A)
        assert not hasattr(User, "staff_code"), "staff_code was not ratified"

    def test_active_display_names_matches_dropdown_semantics(self, ctx):
        _user(A, "anju", display="Anju", active=True)
        _user(A, "kiran", display="Kiran", active=False)
        assert staff_service.active_display_names(A) == ["Anju"]


class TestResolution:
    def test_resolves_by_username(self, ctx):
        u = _user(A, "anju")
        assert staff_service.resolve(A, "anju").id == u.id

    def test_resolves_by_display_name(self, ctx):
        u = _user(A, "anju", display="Anju Menon")
        assert staff_service.resolve(A, "Anju Menon").id == u.id

    def test_resolution_is_case_and_whitespace_insensitive(self, ctx):
        u = _user(A, "anju")
        for probe in ("ANJU", "  Anju  ", "anju"):
            assert staff_service.resolve(A, probe).id == u.id

    def test_unknown_name_returns_none_never_guesses(self, ctx):
        """Production holds 'Anju_display', which resolves to nobody. A
        migration that guessed here would reassign a real customer's lead."""
        _user(A, "anju")
        assert staff_service.resolve(A, "Anju_display") is None
        assert staff_service.resolve_id(A, "Anju_display") is None

    def test_cannot_resolve_across_tenants(self, ctx):
        _user(B, "bob")
        assert staff_service.resolve(A, "bob") is None

    def test_blank_and_missing_inputs_return_none(self, ctx):
        for probe in (None, "", "   "):
            assert staff_service.resolve(A, probe) is None
        assert staff_service.resolve(None, "anju") is None

    def test_ambiguous_name_is_refused_not_guessed(self, ctx):
        """display_name has no uniqueness constraint."""
        _user(A, "u1", display="Same Name")
        _user(A, "u2", display="Same Name")
        assert staff_service.resolve(A, "Same Name") is None

    def test_display_for_id_is_tenant_scoped(self, ctx):
        u = _user(B, "bob", display="Bob B")
        assert staff_service.display_for_id(B, u.id) == "Bob B"
        assert staff_service.display_for_id(A, u.id) is None, \
            "cross-tenant id must not render a name"


class TestServiceIsFrameworkFree:
    def test_no_flask_import(self):
        with open(os.path.join(ROOT, "app", "services", "staff_service.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                bad += [a.name for a in node.names
                        if a.name.split(".")[0] in {"flask", "flask_login"}]
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in {"flask", "flask_login"}:
                    bad.append(node.module)
        assert bad == [], f"framework imports: {bad}"

    def test_service_never_writes(self):
        with open(os.path.join(ROOT, "app", "services", "staff_service.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        calls = {n.func.attr for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not calls & {"commit", "add", "delete", "flush", "merge"}


class TestBehaviourUnchanged:
    def test_existing_assignment_flow_is_unaffected(self, ctx):
        """assigned_staff remains authoritative; the FK stays NULL."""
        _user(A, "anju")
        lead = ConversationState(
            phone="919000000010", name="X", tenant_id=A, stage="new",
            course="", goal="", batch_time="", offer_course="",
            last_msg="", last_text="", lead_status="Lead")
        db.session.add(lead)
        db.session.commit()

        lead.assigned_staff = "Anju"
        db.session.commit()
        assert lead.assigned_staff == "Anju"
        assert lead.assigned_user_id is None

    def test_pipeline_fields_untouched(self, ctx):
        lead = ConversationState(
            phone="919000000011", name="X", tenant_id=A, stage="new",
            course="", goal="", batch_time="", offer_course="",
            last_msg="", last_text="", lead_status="Lead")
        db.session.add(lead)
        db.session.commit()
        assert lead.sales_stage_id is not None
        assert lead._stage == "new"
        assert lead.pipeline_stage_id is None

    def test_task_assignment_flow_is_unaffected(self, ctx):
        from app.services.task_service import create_task
        create_task(tenant_id=A, title="T", created_by="Admin",
                    assigned_staff="Anju")
        t = Task.query.filter_by(tenant_id=A).first()
        assert t.assigned_staff == "Anju"
        assert t.assigned_user_id is None
        assert t.created_by == "Admin", "audit field must stay free text"
