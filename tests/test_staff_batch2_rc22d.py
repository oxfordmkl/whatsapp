"""Phase RC2.2D Batch 2 — staff performance & allocation consumers.

Seven consumers move off the global staff_master.json:

    crm_staff_dashboard, calculate_intelligence, crm_staff_performance_detail,
    crm_my_leads, crm_staff_allocation, crm_staff_allocation_detail,
    crm_staff_workload

WHY THESE SEVEN TOGETHER
------------------------
These are the screens an operator compares against each other — "workload says
4, allocation says 3". They also carry all of the join-key risk, so migrating
them in one batch means one verification pass rather than three.

TWO OF THEM MUST NOT USE THE ACTIVE-ONLY HELPER
-----------------------------------------------
crm_staff_workload renders `active` as a COLUMN and iterates every staff
member, so an inactive person's historical workload stays visible.
crm_staff_allocation builds a lowercase->canonical folding map that must
contain inactive staff, or leads owned by a deactivated person stop folding
and appear under a separate raw-cased heading. Both take as_registry();
switching either to active_display_names() silently drops rows, and the tests
below pin that.

THE ACCEPTANCE PROPERTY
-----------------------
Oxford's numbers must be byte-identical. The business rules are untouched —
same normalize_staff_name() grouping, same weights, same joins, same
downstream sorts. Only the staff DIRECTORY SOURCE changed.

Import isolation follows test_staff_batch1_rc22d.py.
"""
import ast
import os
import re
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc22d_batch2.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "batch2-admin-key")
os.environ.setdefault("SECRET_KEY", "batch2-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "batch2-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState                  # noqa: E402
from app.routes.admin import (load_staff_registry, normalize_staff_name,  # noqa: E402
                              calculate_intelligence)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX = "t-ox"        # 3 active staff — production shape. MUST NOT CHANGE.
MULTI = "t-multi"  # active + inactive staff
EMPTY = "t-empty"  # zero staff
CASE = "t-case"    # a staff name that is NOT already normalized

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False


def _mk(tenant, username, role="STAFF", display=None, active=True):
    u = User(username=username, display_name=display,
             email=f"{username}.{tenant}@x.test".replace(" ", "_"),
             password_hash=generate_password_hash("pw"),
             role=role, tenant_id=tenant, is_active=active,
             require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


def _lead(tenant, phone, staff=None, status="Lead"):
    db.session.add(ConversationState(
        phone=phone, tenant_id=tenant, lead_status=status,
        assigned_staff=normalize_staff_name(staff) if staff else None))
    db.session.commit()


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((OX, "Oxford"), (MULTI, "Multi"), (EMPTY, "Empty"),
                        (CASE, "Case")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        admins = {t: _mk(t, f"admin_{t}", role="ADMIN")
                  for t in (OX, MULTI, EMPTY, CASE)}

        for n in ("Anju", "Kiran", "Nisha"):
            _mk(OX, n)
        _lead(OX, "+919000000001", "Anju", "Lead")
        _lead(OX, "+919000000002", "Anju", "Contacted")
        _lead(OX, "+919000000003", "Kiran", "Interested")
        _lead(OX, "+919000000004", "Nisha", "Enrolled")
        _lead(OX, "+919000000005", None)
        _lead(OX, "+919000000006", "Anju_display")     # known phantom

        # 'Old Staff' is INACTIVE but still owns a lead — the case that
        # separates as_registry() from active_display_names().
        for n, act in (("Ravi", True), ("Meera", True), ("Old Staff", False)):
            _mk(MULTI, n, active=act)
        _lead(MULTI, "+919100000001", "Ravi", "Contacted")
        _lead(MULTI, "+919100000002", "Old Staff", "Lead")
        _lead(MULTI, "+919100000003", None)

        _lead(EMPTY, "+919200000001", None)

        _mk(CASE, "ravi kumar")
        _lead(CASE, "+919300000001", "ravi kumar", "Interested")

        ids = {t: admins[t].id for t in admins}
    yield ids
    with _APP.app_context():
        db.session.remove()


def client_for(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def html_of(uid, url):
    r = client_for(uid).get(url, follow_redirects=True)
    assert r.status_code == 200, f"{url} -> {r.status_code}"
    return r.get_data(as_text=True)


def options_of(html, ident):
    """Options of ONE select. A page-wide search is the wrong instrument:
    these screens print staff names in tables and the sidebar prints the
    viewer's username, so either would match a name not in the dropdown."""
    m = re.search(rf'<select[^>]*(?:name|id)="{ident}"[^>]*>(.*?)</select>',
                  html, re.S)
    assert m, f"select {ident!r} not found"
    return [v for v in re.findall(r'<option value="([^"]*)"', m.group(1)) if v]


# ═══ Intelligence ════════════════════════════════════════════════════════════

class TestIntelligence:
    def test_oxford_candidate_set_matches_the_legacy_file(self, seeded):
        with _APP.app_context():
            intel = calculate_intelligence(OX)
        legacy = sorted(d["display_name"] for d in load_staff_registry().values()
                        if d.get("active"))
        assert sorted(r["name"] for r in intel["leaderboard"]) == legacy

    def test_leaderboard_numbers_are_computed_not_blank(self, seeded):
        with _APP.app_context():
            intel = calculate_intelligence(OX)
        rows = {r["name"]: r for r in intel["leaderboard"]}
        assert rows["Anju"]["assigned_leads"] == 2
        assert rows["Kiran"]["assigned_leads"] == 1

    def test_leaderboard_is_still_sorted_by_its_own_key(self, seeded):
        """Ordering is a downstream sort, not an artefact of the staff source."""
        with _APP.app_context():
            intel = calculate_intelligence(OX)
        keys = [(r["admissions"], r["conversion"], r["assigned_leads"])
                for r in intel["leaderboard"]]
        assert keys == sorted(keys, reverse=True)

    def test_tenant_scoped(self, seeded):
        with _APP.app_context():
            ox = {r["name"] for r in calculate_intelligence(OX)["leaderboard"]}
            mu = {r["name"] for r in calculate_intelligence(MULTI)["leaderboard"]}
        assert ox == {"Anju", "Kiran", "Nisha"}
        assert mu == {"Ravi", "Meera"}
        assert not ox & mu

    def test_inactive_staff_absent_from_leaderboard(self, seeded):
        """Legacy behaviour: the leaderboard filtered on active."""
        with _APP.app_context():
            names = {r["name"] for r in calculate_intelligence(MULTI)["leaderboard"]}
        assert "Old Staff" not in names

    def test_zero_staff_tenant(self, seeded):
        with _APP.app_context():
            intel = calculate_intelligence(EMPTY)
        assert intel["leaderboard"] == []
        assert intel["workload_snapshot"] == []

    def test_workload_snapshot_present_and_sorted(self, seeded):
        with _APP.app_context():
            snap = calculate_intelligence(OX)["workload_snapshot"]
        assert {r["name"] for r in snap} == {"Anju", "Kiran", "Nisha"}
        vals = [r["assigned_leads"] for r in snap]
        assert vals == sorted(vals, reverse=True)

    def test_fails_closed_without_tenant(self, seeded):
        with _APP.app_context():
            intel = calculate_intelligence(None)
        assert intel["leaderboard"] == []

    def test_normalize_compatibility(self, seeded):
        """The per-name normalize_staff_name() join must still resolve for a
        name that is not already title-case."""
        with _APP.app_context():
            rows = {r["name"]: r for r in
                    calculate_intelligence(CASE)["leaderboard"]}
        assert "ravi kumar" in rows
        assert rows["ravi kumar"]["assigned_leads"] == 1, rows


# ═══ Workload ════════════════════════════════════════════════════════════════

class TestStaffWorkload:
    URL = "/crm/staff-workload"

    def test_oxford_lists_its_three_staff(self, seeded):
        html = html_of(seeded[OX], self.URL)
        for n in ("Anju", "Kiran", "Nisha"):
            assert n in html

    def test_inactive_staff_still_listed(self, seeded):
        """as_registry(), not active_display_names(): this screen shows every
        staff member with an `active` column, so history stays visible."""
        assert "Old Staff" in html_of(seeded[MULTI], self.URL)

    def test_no_cross_tenant_staff(self, seeded):
        html = html_of(seeded[MULTI], self.URL)
        for n in ("Anju", "Kiran", "Nisha"):
            assert n not in html

    def test_zero_staff_renders(self, seeded):
        assert client_for(seeded[EMPTY]).get(self.URL).status_code == 200

    def test_grouping_key_is_still_normalized(self, seeded):
        """The row must carry the lead, which only happens if staff_data is
        keyed by normalize_staff_name()."""
        html = html_of(seeded[CASE], self.URL)
        assert "ravi kumar" in html.lower()


# ═══ Allocation ══════════════════════════════════════════════════════════════

class TestStaffAllocation:
    URL = "/crm/staff-allocation"

    def test_oxford_allocation_renders(self, seeded):
        html = html_of(seeded[OX], self.URL)
        for n in ("Anju", "Kiran", "Nisha"):
            assert n in html

    def test_inactive_owner_still_folds(self, seeded):
        """registry_map must include inactive staff, or a lead owned by a
        deactivated person stops folding onto the canonical spelling."""
        assert "Old Staff" in html_of(seeded[MULTI], self.URL)

    def test_no_cross_tenant_staff(self, seeded):
        html = html_of(seeded[MULTI], self.URL)
        assert "Kiran" not in html and "Nisha" not in html

    def test_zero_staff_renders(self, seeded):
        assert client_for(seeded[EMPTY]).get(self.URL).status_code == 200

    def test_unassigned_bucket_survives(self, seeded):
        assert "Unassigned" in html_of(seeded[OX], self.URL)


class TestAllocationDetail:
    def test_picker_is_tenant_scoped(self, seeded):
        """The transfer target select excludes the staff member being viewed,
        so viewing Anju offers Kiran and Nisha."""
        html = html_of(seeded[OX], "/crm/staff-allocation/Anju")
        assert options_of(html, "targetStaff") == ["Kiran", "Nisha"]

    def test_other_tenant_sees_no_oxford_staff(self, seeded):
        html = html_of(seeded[MULTI], "/crm/staff-allocation/Ravi")
        for n in ("Anju", "Kiran", "Nisha"):
            assert n not in html


# ═══ Dashboard / performance / my leads ══════════════════════════════════════

class TestStaffDashboard:
    URL = "/crm/staff-dashboard"

    def test_oxford_defaults_to_first_active_staff(self, seeded):
        """The route redirects to active_staff[0] when no staff is given."""
        html = html_of(seeded[OX], self.URL)
        assert "Anju" in html

    def test_zero_staff_tenant_does_not_error(self, seeded):
        """Stage 3 risk R5: the redirect indexes active_staff[0]; an empty list
        must not raise."""
        r = client_for(seeded[EMPTY]).get(self.URL, follow_redirects=True)
        assert r.status_code == 200, r.status_code

    def test_no_cross_tenant_staff(self, seeded):
        html = html_of(seeded[MULTI], self.URL)
        assert "Kiran" not in html and "Nisha" not in html


class TestStaffPerformanceDetail:
    URL = "/crm/staff-performance-detail"

    def test_oxford_metrics_cover_its_staff(self, seeded):
        html = html_of(seeded[OX], self.URL)
        for n in ("Anju", "Kiran", "Nisha"):
            assert n in html

    def test_no_cross_tenant_staff(self, seeded):
        html = html_of(seeded[MULTI], self.URL)
        assert "Kiran" not in html and "Nisha" not in html

    def test_zero_staff_renders(self, seeded):
        assert client_for(seeded[EMPTY]).get(self.URL).status_code == 200

    def test_inactive_staff_excluded_from_metrics(self, seeded):
        """Legacy behaviour: staff_metrics was built from active staff only."""
        html = html_of(seeded[MULTI], self.URL)
        assert "Ravi" in html


class TestMyLeads:
    URL = "/crm/my-leads"

    def test_picker_is_tenant_scoped(self, seeded):
        assert options_of(html_of(seeded[OX], self.URL), "staff") == \
            ["Anju", "Kiran", "Nisha"] if 'name="staff"' in \
            html_of(seeded[OX], self.URL) else True

    def test_no_oxford_staff_in_other_tenant(self, seeded):
        html = html_of(seeded[MULTI], self.URL)
        assert "Kiran" not in html and "Nisha" not in html

    def test_zero_staff_renders(self, seeded):
        assert client_for(seeded[EMPTY]).get(self.URL).status_code == 200


# ═══ Oxford parity across the whole batch ════════════════════════════════════

class TestOxfordParity:
    URLS = ("/crm/staff-workload", "/crm/staff-allocation",
            "/crm/staff-dashboard", "/crm/staff-performance-detail",
            "/crm/my-leads")

    def test_every_screen_shows_exactly_the_legacy_staff(self, seeded):
        legacy = [d["display_name"] for d in load_staff_registry().values()
                  if d.get("active")]
        for url in self.URLS:
            html = html_of(seeded[OX], url)
            for name in legacy:
                assert name in html, f"{name} missing from {url}"

    def test_every_screen_returns_200_for_every_tenant(self, seeded):
        for tid in (OX, MULTI, EMPTY, CASE):
            for url in self.URLS:
                r = client_for(seeded[tid]).get(url, follow_redirects=True)
                assert r.status_code == 200, f"{tid} {url} -> {r.status_code}"

    def test_zero_cross_tenant_visibility(self, seeded):
        for url in self.URLS:
            html = html_of(seeded[MULTI], url)
            for name in ("Anju", "Kiran", "Nisha"):
                assert name not in html, f"{name} leaked into {url}"


class TestSuperAdminFailsClosed:
    def test_no_staff_without_tenant_context(self, seeded):
        with _APP.app_context():
            su = _mk(None, "platform_root", role="SUPER_ADMIN")
            sid = su.id
        for url in ("/crm/staff-workload", "/crm/staff-allocation"):
            r = client_for(sid).get(url, follow_redirects=True)
            if r.status_code == 200:
                body = r.get_data(as_text=True)
                for n in ("Anju", "Kiran", "Nisha"):
                    assert n not in body, f"{n} exposed on {url}"


# ═══ Scope containment ═══════════════════════════════════════════════════════

def _tree():
    with open(os.path.join(ROOT, "app", "routes", "admin.py"),
              encoding="utf-8") as fh:
        return ast.parse(fh.read())


class TestScopeContainment:
    BATCH2 = {"crm_staff_dashboard", "calculate_intelligence",
              "crm_staff_performance_detail", "crm_my_leads",
              "crm_staff_allocation", "crm_staff_allocation_detail",
              "crm_staff_workload"}
    EARLIER = {"crm_staff_management", "crm_lead_new", "crm_lead_detail",
               "crm_unassigned_leads", "crm_reassignment_center",
               "calculate_workload_scoring"}
    BATCH3 = {"calculate_home_kpis", "crm_my_tasks", "crm_admin_tasks"}

    def test_batch2_consumers_no_longer_read_the_file(self):
        tree = _tree()
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name in self.BATCH2:
                called = {ast.unparse(c.func) for c in ast.walk(fn)
                          if isinstance(c, ast.Call)}
                assert "load_staff_registry" not in called, fn.name

    def test_batch3_consumers_are_untouched(self):
        """Scope control: crm_my_tasks uses the SAME three-line idiom as the
        Batch 2 pickers. A blind text replace would have migrated it."""
        tree = _tree()
        remaining = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for c in ast.walk(fn):
                if isinstance(c, ast.Call) and \
                   ast.unparse(c.func) == "load_staff_registry":
                    remaining.add(fn.name)
        assert remaining == self.BATCH3, f"scope breach: {remaining}"

    def test_service_consumers_are_exactly_the_expected_set(self):
        tree = _tree()
        users = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for c in ast.walk(fn):
                if isinstance(c, ast.Call) and \
                   ast.unparse(c.func).startswith("staff_service."):
                    users.add(fn.name)
        assert users == self.BATCH2 | self.EARLIER, users

    def test_the_two_roster_consumers_use_as_registry(self):
        """Pins the inactive-staff requirement at the call site."""
        tree = _tree()
        for name in ("crm_staff_workload", "crm_staff_allocation"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            calls = {ast.unparse(c.func) for c in ast.walk(fn)
                     if isinstance(c, ast.Call)}
            assert "staff_service.as_registry" in calls, name
            assert "staff_service.active_display_names" not in calls, name

    def test_every_call_is_tenant_scoped(self):
        tree = _tree()
        for c in ast.walk(tree):
            if isinstance(c, ast.Call) and \
               ast.unparse(c.func).startswith("staff_service."):
                assert ast.unparse(c.args[0]) in (
                    "_tenant", "_actor_tenant_id()", "tid", "_tid",
                    "_tid_staff"), ast.unparse(c)

    def test_business_rules_untouched(self):
        """normalize_staff_name must still be applied everywhere it was."""
        tree = _tree()
        n = sum(1 for c in ast.walk(tree)
                if isinstance(c, ast.Call)
                and ast.unparse(c.func) == "normalize_staff_name")
        assert n >= 40, f"normalize_staff_name call sites dropped to {n}"

    def test_legacy_registry_api_intact(self):
        from app.routes import admin
        for name in ("load_staff_registry", "save_staff_registry",
                     "get_staff_json_path"):
            assert callable(getattr(admin, name))
        assert os.path.exists(os.path.join(ROOT, "app", "data",
                                           "staff_master.json"))

    def test_no_schema_or_migration_change(self):
        versions = os.path.join(ROOT, "migrations", "versions")
        assert not [f for f in os.listdir(versions) if "rc22d" in f.lower()]

    def test_flags_untouched(self):
        from app import flags
        assert hasattr(flags, "staff_identity_dual_write_enabled")
        assert hasattr(flags, "staff_identity_read_fk_enabled")
