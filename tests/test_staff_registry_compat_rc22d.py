"""Phase RC2.2D Stage 0 — staff registry compatibility layer.

staff_service.as_registry() must be a drop-in replacement for
load_staff_registry(). Stage 0 only makes it so; NO consumer switches over
here, and nothing imports it.

The three defects fixed, all found by testing against a production-realistic
tenant rather than a clean fixture:

  I1  as_registry() defaulted include_admins=True, so the tenant's ADMIN
      account would have entered the registry the moment a consumer switched —
      'admin' in every assignment dropdown, Staff Active 3 -> 4.
  I2  active_display_names() hardcoded include_admins=True, same leak.
  I3  the key was username.upper(). Usernames are unique per tenant
      CASE-SENSITIVELY, so 'anju2' and 'ANJU2' both uppercase to 'ANJU2' and
      the second silently overwrote the first — a real person disappearing
      from every CRM screen with no error.

The parity tests compare against the REAL staff_master.json, not a
hand-written expectation, so the two cannot drift apart unnoticed.

Import isolation follows test_pipeline_foundation_10_6.py.
"""
import ast
import json
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc22d_compat.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc22d-admin-key")
os.environ.setdefault("SECRET_KEY", "rc22d-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc22d-broadcast")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User                                     # noqa: E402
from app.services import staff_service                                  # noqa: E402
from app.routes.admin import load_staff_registry                        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX = "t-ox"
OTHER = "t-other"
_APP = create_app()


@pytest.fixture()
def ctx():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add_all([
            Tenant(id=OX, name="Oxford", slug="ox", status="ACTIVE",
                   billing_exempt=True),
            Tenant(id=OTHER, name="Other", slug="other", status="ACTIVE",
                   billing_exempt=True),
        ])
        db.session.commit()
        yield
        db.session.remove()


def mk(tenant, username, role="STAFF", display=None, active=True):
    from werkzeug.security import generate_password_hash
    u = User(username=username, display_name=display,
             email=f"{username}.{tenant}@x.test".replace(" ", "_"),
             password_hash=generate_password_hash("pw"),
             role=role, tenant_id=tenant, is_active=active,
             require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


def oxford_staff():
    """The three users mirroring production's staff_master.json."""
    return [mk(OX, "Anju"), mk(OX, "Kiran"), mk(OX, "Nisha")]


# ═══ I1 — admins excluded by default ═════════════════════════════════════════

class TestAdminExclusion:
    def test_as_registry_excludes_admins_by_default(self, ctx):
        oxford_staff()
        mk(OX, "admin", role="ADMIN")
        reg = staff_service.as_registry(OX)
        assert "ADMIN" not in reg
        assert sorted(reg) == ["ANJU", "KIRAN", "NISHA"]

    def test_default_matches_the_real_json_file(self, ctx):
        """Parity against the actual file, not a hand-written expectation."""
        oxford_staff()
        mk(OX, "admin", role="ADMIN")
        assert sorted(staff_service.as_registry(OX)) == sorted(load_staff_registry())

    def test_admin_would_have_leaked_before_the_fix(self, ctx):
        """Pins I1: asking for admins explicitly still includes them, which is
        what the old default did implicitly."""
        oxford_staff()
        mk(OX, "admin", role="ADMIN")
        assert "ADMIN" in staff_service.as_registry(OX, include_admins=True)

    def test_staff_active_count_is_unaffected_by_an_admin(self, ctx):
        """The CRM 'Staff Active' card counts registry entries with active=True.
        An ADMIN must not inflate it from 3 to 4."""
        oxford_staff()
        mk(OX, "admin", role="ADMIN")
        reg = staff_service.as_registry(OX)
        assert sum(1 for v in reg.values() if v["active"]) == 3

    def test_super_admin_is_never_included(self, ctx):
        oxford_staff()
        mk(OX, "platform", role="SUPER_ADMIN")
        assert sorted(staff_service.as_registry(OX)) == ["ANJU", "KIRAN", "NISHA"]
        assert "PLATFORM" not in staff_service.as_registry(OX, include_admins=True)


class TestIncludeAdminsTrue:
    def test_admins_appear_when_asked_for(self, ctx):
        oxford_staff()
        mk(OX, "admin", role="ADMIN")
        reg = staff_service.as_registry(OX, include_admins=True)
        assert sorted(reg) == ["ADMIN", "ANJU", "KIRAN", "NISHA"]
        assert reg["ADMIN"]["role"] == "ADMIN"

    def test_shape_is_identical_with_and_without_admins(self, ctx):
        oxford_staff()
        mk(OX, "admin", role="ADMIN")
        a = staff_service.as_registry(OX)
        b = staff_service.as_registry(OX, include_admins=True)
        assert {k for v in a.values() for k in v} == {k for v in b.values() for k in v}


# ═══ I2 — dropdown source ════════════════════════════════════════════════════

class TestActiveDisplayNames:
    def test_excludes_admins_by_default(self, ctx):
        oxford_staff()
        mk(OX, "admin", role="ADMIN")
        assert staff_service.active_display_names(OX) == ["Anju", "Kiran", "Nisha"]

    def test_matches_the_legacy_dropdown_exactly(self, ctx):
        oxford_staff()
        mk(OX, "admin", role="ADMIN")
        legacy = sorted(d["display_name"]
                        for d in load_staff_registry().values() if d["active"])
        assert staff_service.active_display_names(OX) == legacy

    def test_admins_can_still_be_requested(self, ctx):
        oxford_staff()
        mk(OX, "admin", role="ADMIN")
        assert "admin" in staff_service.active_display_names(OX, include_admins=True)

    def test_inactive_staff_are_excluded(self, ctx):
        oxford_staff()
        mk(OX, "onleave", active=False)
        names = staff_service.active_display_names(OX)
        assert "onleave" not in names
        assert names == ["Anju", "Kiran", "Nisha"]

    def test_display_name_is_used_when_set(self, ctx):
        mk(OX, "u_priya", display="Priya Menon")
        assert staff_service.active_display_names(OX) == ["Priya Menon"]


class TestInactiveStaff:
    def test_inactive_staff_still_appear_in_the_registry(self, ctx):
        """The registry lists everyone with an `active` flag — it does not
        filter. Only the dropdown filters."""
        mk(OX, "onleave", active=False)
        reg = staff_service.as_registry(OX)
        assert reg["ONLEAVE"]["active"] is False

    def test_active_flag_is_a_real_bool(self, ctx):
        """json.dumps must produce true/false, not 1/0."""
        mk(OX, "Anju")
        mk(OX, "onleave", active=False)
        reg = staff_service.as_registry(OX)
        assert reg["ANJU"]["active"] is True
        assert reg["ONLEAVE"]["active"] is False
        assert '"active": true' in json.dumps(reg)


# ═══ I3 — collision prevention ═══════════════════════════════════════════════

class TestCollisionPrevention:
    def test_case_variant_usernames_do_not_collide(self, ctx):
        """The I3 defect: both uppercase to ANJU2 and one used to vanish."""
        a = mk(OX, "anju2")
        b = mk(OX, "ANJU2")
        reg = staff_service.as_registry(OX)
        assert len(reg) == 2, f"a staff member was silently dropped: {reg}"
        assert {v["display_name"] for v in reg.values()} == {"anju2", "ANJU2"}
        assert a.id != b.id

    def test_lowest_id_keeps_the_unsuffixed_code(self, ctx):
        first = mk(OX, "anju2")
        second = mk(OX, "ANJU2")
        reg = staff_service.as_registry(OX)
        assert "ANJU2" in reg
        assert f"ANJU2#{second.id}" in reg
        assert first.id < second.id

    def test_codes_are_unique(self, ctx):
        for name in ("anju", "Anju", "ANJU", "AnJu"):
            mk(OX, name)
        reg = staff_service.as_registry(OX)
        assert len(reg) == 4, "four users must yield four codes"
        assert len(set(reg)) == 4

    def test_every_user_receives_a_code(self, ctx):
        """Totality: no user may be dropped for any reason."""
        users = [mk(OX, n) for n in ("anju", "ANJU", "kiran", "KIRAN", "Nisha")]
        assert len(staff_service.as_registry(OX)) == len(users)

    def test_assignment_is_deterministic_across_calls(self, ctx):
        mk(OX, "anju2")
        mk(OX, "ANJU2")
        first = staff_service.as_registry(OX)
        for _ in range(5):
            assert staff_service.as_registry(OX) == first

    def test_existing_codes_are_stable_when_a_colliding_user_is_added(self, ctx):
        """Adding a colliding user must not rename anyone already there."""
        mk(OX, "Anju")
        before = staff_service.as_registry(OX)
        mk(OX, "ANJU")
        after = staff_service.as_registry(OX)
        assert "ANJU" in after
        assert after["ANJU"]["display_name"] == before["ANJU"]["display_name"]
        assert len(after) == 2

    def test_blank_username_degrades_to_a_usable_code(self, ctx):
        u = mk(OX, "   ")
        reg = staff_service.as_registry(OX)
        assert f"USER{u.id}" in reg
        assert "" not in reg

    def test_no_collision_means_no_suffix(self, ctx):
        """The common case must look exactly like the legacy file."""
        oxford_staff()
        assert sorted(staff_service.as_registry(OX)) == ["ANJU", "KIRAN", "NISHA"]
        assert not any("#" in k for k in staff_service.as_registry(OX))


# ═══ Tenant isolation ════════════════════════════════════════════════════════

class TestTenantIsolation:
    def test_registry_contains_only_this_tenants_staff(self, ctx):
        oxford_staff()
        mk(OTHER, "Bob")
        assert sorted(staff_service.as_registry(OX)) == ["ANJU", "KIRAN", "NISHA"]
        assert sorted(staff_service.as_registry(OTHER)) == ["BOB"]

    def test_same_username_in_two_tenants_is_independent(self, ctx):
        """Production has 'NIBU S S' in four tenants."""
        mk(OX, "NIBU S S")
        mk(OTHER, "NIBU S S")
        assert list(staff_service.as_registry(OX)) == ["NIBU S S"]
        assert list(staff_service.as_registry(OTHER)) == ["NIBU S S"]

    def test_collision_suffixing_is_per_tenant(self, ctx):
        """A collision in one tenant must not suffix another tenant's code."""
        mk(OX, "anju")
        mk(OX, "ANJU")
        mk(OTHER, "Anju")
        assert list(staff_service.as_registry(OTHER)) == ["ANJU"]
        assert len(staff_service.as_registry(OX)) == 2

    def test_missing_tenant_fails_closed(self, ctx):
        oxford_staff()
        assert staff_service.as_registry(None) == {}
        assert staff_service.active_display_names(None) == []

    def test_unknown_tenant_returns_empty(self, ctx):
        oxford_staff()
        assert staff_service.as_registry("no-such-tenant") == {}


# ═══ Registry parity ═════════════════════════════════════════════════════════

class TestRegistryParity:
    def test_byte_compatible_with_the_real_json(self, ctx):
        """The decisive test: identical serialisation, so a consumer cannot
        tell the two apart."""
        oxford_staff()
        legacy = json.dumps(load_staff_registry(), sort_keys=True)
        new = json.dumps(staff_service.as_registry(OX), sort_keys=True)
        assert new == legacy

    def test_field_names_match_exactly(self, ctx):
        oxford_staff()
        lf = {k for v in load_staff_registry().values() for k in v}
        nf = {k for v in staff_service.as_registry(OX).values() for k in v}
        assert nf == lf == {"display_name", "role", "active"}

    def test_field_types_match(self, ctx):
        oxford_staff()
        legacy = next(iter(load_staff_registry().values()))
        new = next(iter(staff_service.as_registry(OX).values()))
        for f in legacy:
            assert type(new[f]) is type(legacy[f]), f

    def test_legacy_consumer_access_patterns_work(self, ctx):
        """The exact idioms the 16 consumers use."""
        oxford_staff()
        reg = staff_service.as_registry(OX)
        assert [d["display_name"] for c, d in reg.items() if d.get("active")]
        assert all(d.get("role", "STAFF") for d in reg.values())
        assert all(isinstance(c, str) for c in reg.keys())


# ═══ Ordering ════════════════════════════════════════════════════════════════

class TestDeterministicOrdering:
    def test_active_display_names_is_sorted(self, ctx):
        for n in ("Zara", "Anju", "Mira"):
            mk(OX, n)
        names = staff_service.active_display_names(OX)
        assert names == sorted(names)

    def test_list_staff_order_is_stable(self, ctx):
        for n in ("Zara", "Anju", "Mira"):
            mk(OX, n)
        first = [u.id for u in staff_service.list_staff(OX)]
        for _ in range(3):
            assert [u.id for u in staff_service.list_staff(OX)] == first

    def test_registry_content_is_order_independent(self, ctx):
        oxford_staff()
        assert staff_service.as_registry(OX) == staff_service.as_registry(OX)


# ═══ Stage 0 scope containment ═══════════════════════════════════════════════

class TestStage0IsNotWired:
    def test_only_the_approved_consumer_imports_staff_service(self):
        """Stage 0 prepared the layer with NO consumer.

        RC2.2D Stage 1 migrated exactly one: the read path of
        crm_staff_management() in admin.py. The guard is now an allowlist, so
        it still catches a consumer migrating outside an approved stage.
        """
        offenders = []
        for dp, _d, fs in os.walk(os.path.join(ROOT, "app")):
            if "__pycache__" in dp:
                continue
            for f in fs:
                if not f.endswith(".py") or f == "staff_service.py":
                    continue
                full = os.path.join(dp, f)
                try:
                    tree = ast.parse(open(full, encoding="utf-8").read())
                except SyntaxError:
                    continue
                for n in ast.walk(tree):
                    if isinstance(n, ast.Import):
                        if any("staff_service" in a.name and "backfill" not in a.name
                               for a in n.names):
                            offenders.append(os.path.relpath(full, ROOT))
                    elif isinstance(n, ast.ImportFrom):
                        mod = n.module or ""
                        if ("staff_service" in mod and "backfill" not in mod) or \
                           any(a.name == "staff_service" for a in n.names):
                            offenders.append(os.path.relpath(full, ROOT))
        allowed = [os.path.join("app", "routes", "admin.py")]
        assert sorted(set(offenders)) == allowed, \
            f"unapproved consumer: {set(offenders)}"

    def test_registry_still_authoritative(self):
        """Stage 1 migrated ONE read. The file is still the write authority and
        still backs every other consumer."""
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            body = fh.read()
        # Stage 1 migrated one read, Stage 2 the writes, Batch 1 five more.
        # The exact remaining count is owned by test_staff_batch1_rc22d.py;
        # here it only needs to still be load-bearing.
        assert body.count("load_staff_registry()") > 0
        assert os.path.exists(os.path.join(ROOT, "app", "data", "staff_master.json"))

    def test_service_never_writes(self):
        with open(os.path.join(ROOT, "app", "services", "staff_service.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        # Only session/query writes count — `taken.add(code)` is a set.
        writes = {"commit", "add", "add_all", "delete", "flush", "merge",
                  "update", "bulk_save_objects"}
        offenders = set()
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                continue
            if n.func.attr not in writes:
                continue
            recv = n.func.value
            name = getattr(recv, "attr", None) or getattr(recv, "id", None)
            if name in ("session", "db", "query"):
                offenders.add(ast.unparse(n.func))
        assert offenders == set(), f"staff_service must be read-only: {offenders}"
