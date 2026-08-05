"""Phase RC2.2D Batch 3 — the final JSON consumers.

Three consumers move off app/data/staff_master.json:

    calculate_home_kpis, crm_my_tasks, crm_admin_tasks

THE CONTRADICTION THIS BATCH CLOSES
-----------------------------------
Batches 1 and 2 made the assignment, workload, allocation and performance
screens tenant-scoped. calculate_home_kpis was left on the global file, so a
tenant with no staff saw:

    Home:      "Staff Active: 3"      <- Oxford's Anju/Kiran/Nisha
    Dashboard: (empty)
    Workload:  (empty)
    Allocation:(empty)

Verified live in production after Batch 2: 10 of 12 tenants had zero staff and
every one of them reported 3. After this batch every staff-related screen
derives its directory from the same tenant-scoped User source.

WHAT STAYS
----------
staff_master.json, load_staff_registry(), save_staff_registry() and
get_staff_json_path() all remain. Retirement is Stage 4, a separate approval.
This suite asserts they are still present AND that nothing calls them at
runtime any more — the precondition Stage 4 needs.

crm_admin_tasks keeps its two-step build: seed ACTIVE staff, then re-add
anyone who still holds tasks but is no longer active. That is what keeps a
deactivated person's outstanding tasks visible, and it is pinned below.

Import isolation follows test_staff_batch2_rc22d.py.
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

_DB = os.path.join(tempfile.gettempdir(), "phase_rc22d_batch3.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "batch3-admin-key")
os.environ.setdefault("SECRET_KEY", "batch3-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "batch3-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState, Task            # noqa: E402
from app.routes.admin import (normalize_staff_name,                      # noqa: E402
                              calculate_home_kpis)
from legacy_staff_registry import LEGACY_OXFORD_REGISTRY                  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX = "t-ox"        # 3 active staff — production shape. MUST NOT CHANGE.
MULTI = "t-multi"  # active + an inactive member who still holds a task
EMPTY = "t-empty"  # zero staff — the tenant the contradiction was visible on

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


def _task(tenant, title, staff, due="2026-12-01", status="PENDING"):
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
        for tid, nm in ((OX, "Oxford"), (MULTI, "Multi"), (EMPTY, "Empty")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        admins = {t: _mk(t, f"admin_{t}", role="ADMIN")
                  for t in (OX, MULTI, EMPTY)}

        for n in ("Anju", "Kiran", "Nisha"):
            _mk(OX, n)
        db.session.add(ConversationState(phone="+919000000001", tenant_id=OX,
                                         lead_status="Lead",
                                         assigned_staff="Anju"))
        db.session.commit()
        _task(OX, "Call Anju's lead", "Anju")

        # 'Old Staff' is INACTIVE but still holds a task — the two-step build
        # in crm_admin_tasks must still surface them.
        for n, act in (("Ravi", True), ("Old Staff", False)):
            _mk(MULTI, n, active=act)
        _task(MULTI, "Ravi task", "Ravi")
        _task(MULTI, "Legacy task", "Old Staff")

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
    """Options of ONE select — a page-wide search would match task tables and
    the sidebar's 'Logged in as' block as readily as the dropdown."""
    m = re.search(rf'<select[^>]*(?:name|id)="{ident}"[^>]*>(.*?)</select>',
                  html, re.S)
    assert m, f"select {ident!r} not found"
    return [v for v in re.findall(r'<option value="([^"]*)"', m.group(1)) if v]


# ═══ The contradiction — Home KPI ════════════════════════════════════════════

class TestHomeKpiStaffActive:
    def test_oxford_still_reports_three(self, seeded):
        with _APP.app_context():
            assert calculate_home_kpis(OX)["staff_active"] == 3

    def test_oxford_matches_the_legacy_file(self, seeded):
        """Parity against the frozen production registry, not a
        hand-written number."""
        legacy = sum(1 for v in LEGACY_OXFORD_REGISTRY.values() if v.get("active"))
        with _APP.app_context():
            assert calculate_home_kpis(OX)["staff_active"] == legacy

    def test_zero_staff_tenant_reports_zero(self, seeded):
        """THE CONTRADICTION THIS BATCH CLOSES. Before Batch 3 this returned 3
        — Oxford's staff — for a tenant that has none."""
        with _APP.app_context():
            assert calculate_home_kpis(EMPTY)["staff_active"] == 0

    def test_count_is_tenant_scoped(self, seeded):
        with _APP.app_context():
            assert calculate_home_kpis(MULTI)["staff_active"] == 1   # Ravi only

    def test_inactive_staff_not_counted(self, seeded):
        """'Old Staff' is inactive; the card counts ACTIVE staff only."""
        with _APP.app_context():
            assert calculate_home_kpis(MULTI)["staff_active"] == 1

    def test_fails_closed_without_tenant(self, seeded):
        with _APP.app_context():
            assert calculate_home_kpis(None)["staff_active"] == 0

    def test_other_kpis_still_computed(self, seeded):
        """Only the staff count changed; the rest of the payload must survive."""
        with _APP.app_context():
            kpis = calculate_home_kpis(OX)
        assert "staff_active" in kpis
        assert len(kpis) > 1


class TestHomeAgreesWithTheOtherScreens:
    def test_home_and_workload_agree_for_every_tenant(self, seeded):
        """The whole point of Batch 3: one directory source everywhere."""
        from app.services import staff_service
        with _APP.app_context():
            for tid in (OX, MULTI, EMPTY):
                card = calculate_home_kpis(tid)["staff_active"]
                names = len(staff_service.active_display_names(tid))
                assert card == names, f"{tid}: card={card} directory={names}"


# ═══ My Tasks ════════════════════════════════════════════════════════════════

class TestMyTasks:
    URL = "/crm/tasks/my"

    def test_oxford_picker_unchanged(self, seeded):
        html = html_of(seeded[OX], self.URL)
        assert options_of(html, "staff") == ["Anju", "Kiran", "Nisha"]

    def test_matches_the_legacy_file(self, seeded):
        legacy = sorted(d["display_name"] for d in LEGACY_OXFORD_REGISTRY.values()
                        if d.get("active"))
        assert options_of(html_of(seeded[OX], self.URL), "staff") == legacy

    def test_other_tenant_sees_no_oxford_staff(self, seeded):
        opts = options_of(html_of(seeded[MULTI], self.URL), "staff")
        assert opts == ["Ravi"]
        assert not {"Anju", "Kiran", "Nisha"} & set(opts)

    def test_inactive_staff_not_offered(self, seeded):
        assert "Old Staff" not in options_of(html_of(seeded[MULTI], self.URL),
                                             "staff")

    def test_zero_staff_tenant_renders(self, seeded):
        r = client_for(seeded[EMPTY]).get(self.URL, follow_redirects=True)
        assert r.status_code == 200


# ═══ Admin Tasks ═════════════════════════════════════════════════════════════

class TestAdminTasks:
    URL = "/crm/tasks/admin"

    def test_oxford_summary_covers_its_staff(self, seeded):
        html = html_of(seeded[OX], self.URL)
        for n in ("Anju", "Kiran", "Nisha"):
            assert n in html

    def test_other_tenant_sees_no_oxford_staff(self, seeded):
        html = html_of(seeded[MULTI], self.URL)
        for n in ("Anju", "Kiran", "Nisha"):
            assert n not in html

    def test_inactive_staff_with_tasks_still_appears(self, seeded):
        """The two-step build: seeded from ACTIVE staff, then anyone holding a
        task is re-added. Losing that would hide outstanding work."""
        assert "Old Staff" in html_of(seeded[MULTI], self.URL)

    def test_active_staff_seeded_even_with_no_tasks(self, seeded):
        """Kiran and Nisha hold no tasks but must still get a zero row."""
        html = html_of(seeded[OX], self.URL)
        assert "Kiran" in html and "Nisha" in html

    def test_zero_staff_tenant_renders(self, seeded):
        assert client_for(seeded[EMPTY]).get(
            self.URL, follow_redirects=True).status_code == 200


# ═══ Isolation / scenarios ═══════════════════════════════════════════════════

class TestTenantIsolation:
    URLS = ("/crm/tasks/my", "/crm/tasks/admin", "/crm/home")

    def test_no_oxford_staff_anywhere_in_another_tenant(self, seeded):
        for url in self.URLS:
            html = html_of(seeded[MULTI], url)
            for n in ("Anju", "Kiran", "Nisha"):
                assert n not in html, f"{n} leaked into {url}"

    def test_every_screen_200_for_every_tenant(self, seeded):
        for tid in (OX, MULTI, EMPTY):
            for url in self.URLS:
                r = client_for(seeded[tid]).get(url, follow_redirects=True)
                assert r.status_code == 200, f"{tid} {url} -> {r.status_code}"

    def test_super_admin_fails_closed(self, seeded):
        with _APP.app_context():
            su = _mk(None, "platform_root", role="SUPER_ADMIN")
            sid = su.id
            assert calculate_home_kpis(None)["staff_active"] == 0
        for url in self.URLS:
            r = client_for(sid).get(url, follow_redirects=True)
            if r.status_code == 200:
                body = r.get_data(as_text=True)
                for n in ("Anju", "Kiran", "Nisha"):
                    assert n not in body, f"{n} exposed on {url}"


# ═══ Scope containment / Stage 4 precondition ════════════════════════════════

def _tree():
    with open(os.path.join(ROOT, "app", "routes", "admin.py"),
              encoding="utf-8") as fh:
        return ast.parse(fh.read())


class TestNoJsonConsumersRemain:
    def test_zero_runtime_calls_to_load_staff_registry(self):
        """The Stage 4 precondition. After Batch 3 nothing reads the file at
        runtime; only the retirement code itself remains."""
        tree = _tree()
        calls = [c for c in ast.walk(tree)
                 if isinstance(c, ast.Call)
                 and ast.unparse(c.func) == "load_staff_registry"]
        assert calls == [], f"{len(calls)} runtime consumers remain"

    def test_zero_runtime_calls_to_save_staff_registry(self):
        tree = _tree()
        calls = [c for c in ast.walk(tree)
                 if isinstance(c, ast.Call)
                 and ast.unparse(c.func) == "save_staff_registry"]
        assert calls == [], f"{len(calls)} writers remain"

    def test_no_module_reads_the_json_path(self):
        """Nothing outside admin.py may read the legacy file.

        AST, not string matching. Five modules MENTION staff_master.json in
        docstrings and comments explaining the migration — flags.py,
        tenant.py, staff_backfill_service.py, staff_service.py and admin.py
        itself. Counting those as consumers is the same false positive this
        project has hit repeatedly. Only a real import of the registry API, or
        a real string literal used in an expression, counts.
        """
        offenders = []
        for dp, _d, fs in os.walk(os.path.join(ROOT, "app")):
            if "__pycache__" in dp:
                continue
            for f in fs:
                if not f.endswith(".py") or f == "admin.py":
                    continue
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, ROOT)
                try:
                    tree = ast.parse(open(full, encoding="utf-8").read())
                except SyntaxError:
                    continue
                docstrings = set()
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Module, ast.FunctionDef,
                                         ast.AsyncFunctionDef, ast.ClassDef)):
                        d = ast.get_docstring(node, clean=False)
                        if d:
                            docstrings.add(d)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and \
                            isinstance(node.value, str) and \
                            "staff_master" in node.value and \
                            node.value not in docstrings:
                        offenders.append(f"{rel}:{node.lineno} literal")
                    elif isinstance(node, ast.ImportFrom):
                        if any(a.name in ("load_staff_registry",
                                          "save_staff_registry",
                                          "get_staff_json_path")
                               for a in node.names):
                            offenders.append(f"{rel}:{node.lineno} import")
        assert offenders == [], offenders


class TestRetirementCodeIsRetained:
    """Stage 4 is a SEPARATE approval. Nothing may be deleted here."""

    def test_the_three_functions_still_exist(self):
        from app.routes import admin
        for name in ("load_staff_registry", "save_staff_registry",
                     "get_staff_json_path"):
            assert callable(getattr(admin, name)), name

    def test_the_json_file_still_exists(self):
        path = os.path.join(ROOT, "app", "data", "staff_master.json")
        assert os.path.exists(path)

    def test_the_file_still_holds_the_legacy_rows(self):
        """It is the rollback target; its contents must be intact.

        Stage 4B: reads the file DIRECTLY rather than through
        load_staff_registry(), so this tripwire depends only on the file — and
        compares against the frozen snapshot rather than a literal, so the two
        records of the retired registry cannot disagree.
        """
        import json
        path = os.path.join(ROOT, "app", "data", "staff_master.json")
        with open(path, encoding="utf-8") as fh:
            live = json.load(fh)
        assert sorted(live) == sorted(LEGACY_OXFORD_REGISTRY)


class TestScopeContainment:
    BATCH3 = {"calculate_home_kpis", "crm_my_tasks", "crm_admin_tasks"}
    EARLIER = {"crm_staff_management", "crm_lead_new", "crm_lead_detail",
               "crm_unassigned_leads", "crm_reassignment_center",
               "calculate_workload_scoring", "crm_staff_dashboard",
               "calculate_intelligence", "crm_staff_performance_detail",
               "crm_my_leads", "crm_staff_allocation",
               "crm_staff_allocation_detail", "crm_staff_workload"}

    def test_service_consumers_are_exactly_the_full_set(self):
        tree = _tree()
        users = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for c in ast.walk(fn):
                if isinstance(c, ast.Call) and \
                   ast.unparse(c.func).startswith("staff_service."):
                    users.add(fn.name)
        assert users == self.BATCH3 | self.EARLIER, users

    def test_every_call_is_tenant_scoped(self):
        tree = _tree()
        for c in ast.walk(tree):
            if isinstance(c, ast.Call) and \
               ast.unparse(c.func).startswith("staff_service."):
                assert ast.unparse(c.args[0]) in (
                    "_tenant", "_actor_tenant_id()", "tid", "_tid",
                    "_tid_staff"), ast.unparse(c)

    def test_admin_tasks_uses_active_only(self):
        """It seeds ACTIVE staff and re-adds task-holders separately. Using
        as_registry() here would seed inactive staff with zero rows they never
        had before."""
        tree = _tree()
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "crm_admin_tasks")
        calls = {ast.unparse(c.func) for c in ast.walk(fn)
                 if isinstance(c, ast.Call)}
        assert "staff_service.active_display_names" in calls
        assert "staff_service.as_registry" not in calls

    def test_business_rules_untouched(self):
        tree = _tree()
        n = sum(1 for c in ast.walk(tree)
                if isinstance(c, ast.Call)
                and ast.unparse(c.func) == "normalize_staff_name")
        assert n >= 40, f"normalize_staff_name call sites dropped to {n}"

    def test_no_schema_or_migration_change(self):
        versions = os.path.join(ROOT, "migrations", "versions")
        assert not [f for f in os.listdir(versions) if "rc22d" in f.lower()]

    def test_rc23_untouched(self):
        """No dual-write, FK reader or flag change belongs in this batch."""
        from app import flags
        assert hasattr(flags, "staff_identity_dual_write_enabled")
        assert hasattr(flags, "staff_identity_read_fk_enabled")
        tree = _tree()
        fns = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert "crm_lead_update" in fns          # assignment path still present
