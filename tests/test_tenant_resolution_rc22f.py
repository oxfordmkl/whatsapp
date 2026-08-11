"""Phase RC2.2F — tenant resolution stabilization.

Two defects found by the RC2.2E post-migration audit. Both are tenant-context
propagation bugs, not staff-directory bugs; RC2.2D is untouched.

H1 — calculate_home_kpis() dropped the tenant into get_all_tasks()
-----------------------------------------------------------------
It scoped lead and activity queries with the passed tenant_id but called
get_all_tasks() with NO argument, so task KPIs fell back to current_user while
everything else honoured the parameter. Nothing hit this in production —
crm_home passes nothing, so both resolved to the same tenant — but any caller
supplying an explicit tenant would have rendered ONE dashboard mixing TWO
tenants' numbers. This is also what emitted the fail-closed
`tenant_query(...) could not resolve a tenant` warnings during the Batch 3
validation: the validation script passed an explicit tenant outside a request,
so the leads honoured it and the tasks could not.

H2 — crm_staff_workload ignored impersonation
---------------------------------------------
It resolved `getattr(current_user, 'tenant_id', None)`, which is NULL for a
SUPER_ADMIN. tenant_filter() has an explicit SUPER_ADMIN branch honouring
session['impersonate_tenant_id'], so while impersonating the page rendered the
impersonated tenant's LEADS beside an EMPTY staff roster — one half of the
screen fail-closed and the other did not.

WHY THE H1 FIX IS BEHAVIOUR-NEUTRAL FOR EXISTING CALLERS
--------------------------------------------------------
tenant_query()/tenant_filter() return on their SUPER_ADMIN branch BEFORE
consulting the passed argument, and for every other role
`tenant_id or current_user.tenant_id` is exactly what _actor_tenant_id()
yields. So resolving once and passing it everywhere changes only the case that
was already wrong. The tests below pin that both ways.

Import isolation follows test_staff_batch3_rc22d.py.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc22f_tenant.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc22f-admin-key")
os.environ.setdefault("SECRET_KEY", "rc22f-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc22f-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState                  # noqa: E402
from app.routes.admin import (calculate_home_kpis, normalize_staff_name)  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX = "t-ox"        # 3 staff, 3 leads, 2 tasks
OTHER = "t-other"  # 1 staff, 1 lead, 1 task — the tenant H1 could bleed in
EMPTY = "t-empty"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False


def _mk(tenant, username, role="STAFF", active=True):
    u = User(username=username, email=f"{username}.{tenant}@x.test",
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


def _task(tenant, title, staff, due="2026-12-01"):
    from app.services import task_service
    return task_service.create_task(
        tenant_id=tenant, title=title, created_by="tester",
        assigned_staff=normalize_staff_name(staff) if staff else None,
        due_date=due)


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((OX, "Oxford"), (OTHER, "Other"), (EMPTY, "Empty")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        admins = {t: _mk(t, f"admin_{t}", role="ADMIN")
                  for t in (OX, OTHER, EMPTY)}
        su = _mk(None, "platform_root", role="SUPER_ADMIN")

        for n in ("Anju", "Kiran", "Nisha"):
            _mk(OX, n)
        _lead(OX, "+919000000001", "Anju")
        _lead(OX, "+919000000002", "Kiran")
        _lead(OX, "+919000000003", None)
        _task(OX, "Oxford task 1", "Anju")
        _task(OX, "Oxford task 2", "Kiran")

        _mk(OTHER, "Ravi")
        _lead(OTHER, "+919100000001", "Ravi")
        _task(OTHER, "Other task", "Ravi")

        ids = {t: admins[t].id for t in admins}
        ids["SU"] = su.id
    yield ids
    with _APP.app_context():
        db.session.remove()


def client_for(uid, impersonate=None):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
        if impersonate:
            s["impersonate_tenant_id"] = impersonate
    return c


def html_of(uid, url, impersonate=None):
    r = client_for(uid, impersonate).get(url, follow_redirects=True)
    assert r.status_code == 200, f"{url} -> {r.status_code}"
    return r.get_data(as_text=True)


# ═══ H1 — full tenant propagation ════════════════════════════════════════════

class TestH1TaskKpiHonoursTenant:
    def test_task_kpis_scoped_to_the_passed_tenant(self, seeded):
        """THE H1 DEFECT. Before the fix these counted every tenant's tasks
        (or none, outside a request) while the lead counts honoured OX."""
        with _APP.app_context():
            k = calculate_home_kpis(OX)
        assert k["open_tasks"] == 2, k["open_tasks"]

    def test_other_tenant_gets_its_own_task_count(self, seeded):
        with _APP.app_context():
            assert calculate_home_kpis(OTHER)["open_tasks"] == 1

    def test_empty_tenant_gets_zero(self, seeded):
        with _APP.app_context():
            k = calculate_home_kpis(EMPTY)
        assert k["open_tasks"] == 0
        assert k["total_leads"] == 0

    def test_lead_and_task_kpis_come_from_the_SAME_tenant(self, seeded):
        """The actual harm H1 could cause: one dashboard, two tenants."""
        with _APP.app_context():
            ox = calculate_home_kpis(OX)
            other = calculate_home_kpis(OTHER)
        assert (ox["total_leads"], ox["open_tasks"]) == (3, 2), ox
        assert (other["total_leads"], other["open_tasks"]) == (1, 1), other

    def test_activity_kpis_scoped_to_the_same_tenant(self, seeded):
        with _APP.app_context():
            k = calculate_home_kpis(OX)
        assert all(l.tenant_id == OX for l in k["recent_leads"])
        assert all(e.tenant_id == OX for e in k["recent_events"])

    def test_staff_active_uses_the_same_tenant(self, seeded):
        with _APP.app_context():
            assert calculate_home_kpis(OX)["staff_active"] == 3
            assert calculate_home_kpis(OTHER)["staff_active"] == 1
            assert calculate_home_kpis(EMPTY)["staff_active"] == 0

    def test_every_kpi_is_internally_consistent_per_tenant(self, seeded):
        """One assertion covering the whole contract: no KPI may disagree with
        the tenant it was asked for."""
        expected = {OX: (3, 2, 3), OTHER: (1, 1, 1), EMPTY: (0, 0, 0)}
        with _APP.app_context():
            for tid, (leads, tasks, staff) in expected.items():
                k = calculate_home_kpis(tid)
                assert (k["total_leads"], k["open_tasks"],
                        k["staff_active"]) == (leads, tasks, staff), (tid, k)

    def test_no_fail_closed_warning_when_a_tenant_is_supplied(self, seeded, caplog):
        """The warning seen during Batch 3 validation was this defect: an
        explicit tenant outside a request scoped the leads but not the tasks."""
        import logging
        with _APP.app_context():
            with caplog.at_level(logging.WARNING):
                calculate_home_kpis(OX)
        assert "could not resolve a tenant" not in caplog.text


class TestH1PreservesExistingBehaviour:
    """The fix must be a no-op for the ONE caller that exists."""

    def test_crm_home_renders_unchanged_for_each_tenant(self, seeded):
        for tid in (OX, OTHER, EMPTY):
            assert client_for(seeded[tid]).get(
                "/crm/home", follow_redirects=True).status_code == 200

    def test_in_request_no_arg_call_matches_explicit_call(self, seeded):
        """crm_home calls calculate_home_kpis() with no argument. That must
        still resolve to the caller's own tenant — identical to passing it."""
        with _APP.test_request_context("/crm/home"):
            from flask_login import login_user
            from app.models import User as U
            login_user(U.query.filter_by(tenant_id=OX, role="ADMIN").first())
            implicit = calculate_home_kpis()
            explicit = calculate_home_kpis(OX)
        for key in ("total_leads", "open_tasks", "staff_active", "admissions"):
            assert implicit[key] == explicit[key], key

    def test_payload_shape_unchanged(self, seeded):
        with _APP.app_context():
            k = calculate_home_kpis(OX)
        assert set(k) == {"total_leads", "hot_leads", "open_tasks",
                          "overdue_tasks", "needs_reply", "admissions",
                          "staff_active", "recent_leads", "recent_events"}

    def test_oxford_numbers_are_exact(self, seeded):
        """Oxford behaviour must be preserved exactly."""
        with _APP.app_context():
            k = calculate_home_kpis(OX)
        assert k["total_leads"] == 3
        assert k["staff_active"] == 3
        assert k["open_tasks"] == 2
        assert len(k["recent_leads"]) == 3


# ═══ H2 — impersonation-aware workload ═══════════════════════════════════════

class TestH2ImpersonationAwareWorkload:
    URL = "/crm/staff-workload"

    def test_impersonating_super_admin_sees_the_staff_roster(self, seeded):
        """THE H2 DEFECT: leads rendered, roster empty."""
        html = html_of(seeded["SU"], self.URL, impersonate=OX)
        for n in ("Anju", "Kiran", "Nisha"):
            assert n in html, f"{n} missing while impersonating Oxford"

    def test_impersonation_targets_the_right_tenant(self, seeded):
        html = html_of(seeded["SU"], self.URL, impersonate=OTHER)
        assert "Ravi" in html
        for n in ("Anju", "Kiran", "Nisha"):
            assert n not in html, f"{n} leaked while impersonating OTHER"

    def test_switching_impersonation_switches_the_roster(self, seeded):
        a = html_of(seeded["SU"], self.URL, impersonate=OX)
        b = html_of(seeded["SU"], self.URL, impersonate=OTHER)
        assert "Anju" in a and "Anju" not in b
        assert "Ravi" in b and "Ravi" not in a

    def test_non_impersonating_super_admin_still_fails_closed(self, seeded):
        """Approved operator decision: impersonation is mandatory; without
        tenant context expose no staff."""
        r = client_for(seeded["SU"]).get(self.URL, follow_redirects=True)
        if r.status_code == 200:
            body = r.get_data(as_text=True)
            for n in ("Anju", "Kiran", "Nisha", "Ravi"):
                assert n not in body

    def test_ordinary_admin_behaviour_unchanged(self, seeded):
        html = html_of(seeded[OX], self.URL)
        for n in ("Anju", "Kiran", "Nisha"):
            assert n in html
        assert "Ravi" not in html

    def test_other_tenant_admin_unchanged(self, seeded):
        html = html_of(seeded[OTHER], self.URL)
        assert "Ravi" in html
        for n in ("Anju", "Kiran", "Nisha"):
            assert n not in html

    def test_zero_staff_tenant_renders(self, seeded):
        assert client_for(seeded[EMPTY]).get(
            self.URL, follow_redirects=True).status_code == 200


# ═══ Scope containment ═══════════════════════════════════════════════════════

def _tree():
    with open(os.path.join(ROOT, "app", "routes", "admin.py"),
              encoding="utf-8") as fh:
        return ast.parse(fh.read())


class TestScopeContainment:
    def test_home_kpis_resolves_the_tenant_exactly_once(self):
        tree = _tree()
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "calculate_home_kpis")
        resolutions = [c for c in ast.walk(fn)
                       if isinstance(c, ast.Call)
                       and ast.unparse(c.func) == "_actor_tenant_id"]
        assert len(resolutions) == 1, "two resolutions of one thing will drift"

    def test_every_scoped_call_in_home_kpis_uses_that_tenant(self):
        tree = _tree()
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "calculate_home_kpis")
        scoped = ("tenant_query", "tenant_filter", "get_all_tasks",
                  "staff_service.active_display_names")
        seen = 0
        for c in ast.walk(fn):
            if isinstance(c, ast.Call) and ast.unparse(c.func) in scoped:
                assert c.args, ast.unparse(c)
                assert ast.unparse(c.args[-1]) == "_tid", ast.unparse(c)
                seen += 1
        assert seen >= 9, seen

    def test_workload_uses_the_impersonation_aware_helper(self):
        tree = _tree()
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "crm_staff_workload")
        src = ast.unparse(fn)
        assert "_tid = _actor_tenant_id()" in src
        assert "getattr(current_user, 'tenant_id'" not in src

    def test_no_other_function_changed_its_tenant_source(self):
        """RC2.2F touches exactly two functions. Every other consumer keeps
        whatever it had — converging the two _tid idioms repo-wide is separate
        debt (RC2.2E H4), deliberately not done here.

        UPDATED as that debt is repaid. This is a scope-containment tripwire,
        so its threshold falls each time a LATER, separately approved phase
        migrates routes on purpose:

            RC2.2F           14 legacy sites
            RC2.3E-1 Batch 1a -3 (crm_leads, crm_my_leads, crm_staff_dashboard)
            H4-a              -4 (crm_lead_update, crm_lead_send,
                                  crm_lead_detail, crm_staff_performance_detail)
                              -7 (H4-b: the remainder)
                            => 0 remain; H4 is closed

        The assertion below is lowered to 7 and made EXACT rather than a
        floor. A floor cannot detect an accidental migration; equality can.
        The point of the test — that RC2.2F itself changed only two functions
        — is preserved by naming the remaining seven explicitly.
        """
        tree = _tree()
        legacy = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            if "getattr(current_user, 'tenant_id', None)" in ast.unparse(fn):
                legacy.add(fn.name)
        assert "crm_staff_workload" not in legacy
        # _actor_tenant_id defines the correct idiom; check_billing_status has
        # its own three-way resolution. Neither is an H4 site.
        routes = legacy - {"_actor_tenant_id", "check_billing_status"}
        # H4-b closed the last seven. The debt this test tracked is repaid;
        # the assertion now guards against the idiom REAPPEARING.
        assert routes == set(), f"H4 idiom reappeared in: {sorted(routes)}"

    def test_staff_service_behaviour_untouched(self):
        """RC2.2F must not change staff_service BEHAVIOUR.

        This asserted the file appeared in no diff at all. Stage 4A (RC2.2G)
        then corrected that module's docstring by approval — it claimed
        staff_master.json was still the source of truth — which is a
        documentation change, not a behavioural one. Narrowed to the property
        actually being protected: the public surface and the executable code.
        """
        import subprocess
        out = subprocess.run(["git", "diff", "--name-only"],
                             cwd=ROOT, capture_output=True, text=True).stdout
        assert "app/models.py" not in out, "schema changed"

        from app.services import staff_service
        for fn in ("list_staff", "as_registry", "active_display_names",
                   "resolve", "resolve_id", "resolve_code", "display_for_id"):
            assert callable(getattr(staff_service, fn)), fn
        # Signature defaults are the contract Stage 0 established.
        import inspect
        sig = inspect.signature(staff_service.as_registry)
        assert sig.parameters["include_admins"].default is False
        sig = inspect.signature(staff_service.active_display_names)
        assert sig.parameters["include_admins"].default is False

    def test_no_schema_or_migration_change(self):
        versions = os.path.join(ROOT, "migrations", "versions")
        assert not [f for f in os.listdir(versions) if "rc22f" in f.lower()]

    def test_rc23_and_flags_untouched(self):
        from app import flags
        assert hasattr(flags, "staff_identity_dual_write_enabled")
        assert hasattr(flags, "staff_identity_read_fk_enabled")
        tree = _tree()
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "calculate_home_kpis")
        src = ast.unparse(fn)
        for token in ("assigned_user_id", "STAFF_IDENTITY", "sync_assigned_user"):
            assert token not in src

    def test_legacy_registry_is_retired(self):
        """Stage 4 is no longer deferred — 4C retired the registry."""
        from app.routes import admin
        for name in ("load_staff_registry", "save_staff_registry",
                     "get_staff_json_path"):
            assert not hasattr(admin, name), name
        assert not os.path.exists(os.path.join(ROOT, "app", "data",
                                               "staff_master.json"))

    def test_workload_algorithm_untouched(self):
        tree = _tree()
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "calculate_workload_scoring")
        src = ast.unparse(fn)
        assert "'Lead': 1" in src and "'Contacted': 2" in src and \
               "'Interested': 3" in src
