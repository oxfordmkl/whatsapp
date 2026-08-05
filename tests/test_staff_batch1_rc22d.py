"""Phase RC2.2D Batch 1 — assignment & triage consumers.

Five consumers move off the global staff_master.json onto the tenant-scoped
User table:

    crm_lead_new, crm_lead_detail, crm_unassigned_leads,
    crm_reassignment_center, calculate_workload_scoring

WHY THESE FIVE, TOGETHER
------------------------
This is the only batch where the defect creates BAD DATA rather than a bad
display: assigned_staff is stored as a free string with NO server-side
validation, so picking a foreign name from the dropdown wrote it onto a real
lead.

calculate_workload_scoring travels with them despite being a roster read,
because /crm/leads/unassigned and /crm/reassignment-center each render a picker
AND a recommendation panel fed by that helper. Migrating them apart would give
an operator a page whose dropdown lists their own staff while the panel beside
it recommends Oxford's — authoritative and wrong, which is worse than
uniformly stale.

THE ACCEPTANCE TEST THAT MATTERS
--------------------------------
Oxford's workload scores must be NUMERICALLY IDENTICAL before and after. The
algorithm is untouched — same weights, same grouping, same normalize_staff_name
join key — so only the candidate set changed. test_oxford_scores_are_identical
pins that by computing the legacy result from the real JSON file and comparing.

Import isolation follows test_staff_management_stage2_rc22d.py.
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

_DB = os.path.join(tempfile.gettempdir(), "phase_rc22d_batch1.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "batch1-admin-key")
os.environ.setdefault("SECRET_KEY", "batch1-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "batch1-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                    # noqa: E402
from app import create_app                                             # noqa: E402
from app.extensions import db                                          # noqa: E402
from app.models import Tenant, User, ConversationState                 # noqa: E402
from app.routes.admin import (load_staff_registry, normalize_staff_name,  # noqa: E402
                              calculate_workload_scoring,
                              get_staff_recommendations)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX = "t-ox"          # Oxford: the 3 production staff. MUST NOT CHANGE.
MULTI = "t-multi"    # several staff, one inactive
EMPTY = "t-empty"    # zero staff
CASE = "t-case"      # a staff name that is NOT already normalized
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

        # Oxford mirrors production exactly: 3 active staff whose usernames
        # title-case to the JSON display names.
        for n in ("Anju", "Kiran", "Nisha"):
            _mk(OX, n)
        _lead(OX, "+919000000001", "Anju", "Lead")        # weight 1
        _lead(OX, "+919000000002", "Anju", "Contacted")   # weight 2
        _lead(OX, "+919000000003", "Kiran", "Interested") # weight 3
        _lead(OX, "+919000000004", "Nisha", "Enrolled")   # weight 0
        _lead(OX, "+919000000005", None)                  # unassigned
        _lead(OX, "+919000000006", "Anju_display")        # the known phantom

        for n, act in (("Ravi", True), ("Meera", True), ("Old Staff", False)):
            _mk(MULTI, n, active=act)
        _lead(MULTI, "+919100000001", "Ravi", "Contacted")
        _lead(MULTI, "+919100000002", None)

        _lead(EMPTY, "+919200000001", None)

        # A staff member whose stored name is NOT already normalized. Oxford's
        # three are title-case already, so normalize_staff_name() is a no-op
        # for them and they cannot detect the join key breaking — a mutation
        # dropping normalize() passed the whole suite until this was added.
        # assigned_staff is written normalized ('Ravi Kumar'), so the scores
        # dict must be keyed the same way or the lookup misses.
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
    r = client_for(uid).get(url)
    assert r.status_code == 200, f"{url} -> {r.status_code}"
    return r.get_data(as_text=True)


def options_of(html, ident):
    """Values of one <select>'s options — the dropdown's actual contents.

    A page-wide search is the wrong instrument: the sidebar prints the viewer's
    username and lead tables print owner names, so either would match a staff
    name that is NOT in the dropdown.

    Matches on name= OR id=: the reassignment centre's select carries only
    id="targetStaff" (it is read by JS, never posted as a form field).
    """
    m = re.search(rf'<select[^>]*(?:name|id)="{ident}"[^>]*>(.*?)</select>',
                  html, re.S)
    assert m, f"select {ident!r} not found"
    return [v for v in re.findall(r'<option value="([^"]*)"', m.group(1))]


def staff_options(html, select_name):
    """Dropdown entries excluding the blank Unassigned placeholder."""
    return [v for v in options_of(html, select_name) if v]


# ═══ Criterion 5 — the decisive test ═════════════════════════════════════════

class TestWorkloadScoringUnchanged:
    def test_oxford_scores_are_identical(self, seeded):
        """THE acceptance test. Recompute the legacy result from the REAL JSON
        file and require the migrated helper to match it exactly."""
        with _APP.app_context():
            legacy_staff = {normalize_staff_name(d["display_name"]): d["display_name"]
                            for d in load_staff_registry().values() if d.get("active")}
            scores, active = calculate_workload_scoring(OX)
        assert active == legacy_staff, "candidate set drifted for Oxford"
        # Anju: Lead(1) + Contacted(2) = 3; Kiran: Interested(3); Nisha: Enrolled(0)
        assert scores == {"Anju": 3, "Kiran": 3, "Nisha": 0}

    def test_weights_are_unchanged(self, seeded):
        with _APP.app_context():
            scores, _ = calculate_workload_scoring(OX)
        assert scores["Anju"] == 3      # 1*Lead + 1*Contacted
        assert scores["Nisha"] == 0     # Enrolled contributes nothing

    def test_join_key_still_resolves(self, seeded):
        """R1, the highest risk in the Stage 3 audit: if normalize_staff_name
        parity broke, every score would collapse to 0 and read as data loss."""
        with _APP.app_context():
            scores, _ = calculate_workload_scoring(OX)
        assert any(v > 0 for v in scores.values()), "join key broke — all zero"

    def test_join_key_is_normalized_not_raw(self, seeded):
        """The scores dict MUST be keyed by normalize_staff_name(display), not
        the raw display name.

        Oxford cannot detect this: its names are already title-case, so
        normalize() is an identity there and a mutation dropping it survives.
        This staff member is stored as 'ravi kumar' while the lead's
        assigned_staff was written as 'Ravi Kumar', so the two only meet if the
        candidate key is normalized.
        """
        with _APP.app_context():
            scores, active = calculate_workload_scoring(CASE)
        assert "Ravi Kumar" in scores, f"join key not normalized: {list(scores)}"
        assert scores["Ravi Kumar"] == 3, scores
        assert active["Ravi Kumar"] == "ravi kumar"

    def test_phantom_owner_is_ignored_not_crashed(self, seeded):
        """'Anju_display' matches no user; its lead must not be counted under
        Anju, and must not raise."""
        with _APP.app_context():
            scores, _ = calculate_workload_scoring(OX)
        assert "Anju_Display" not in scores
        assert scores["Anju"] == 3

    def test_scoring_is_tenant_scoped(self, seeded):
        with _APP.app_context():
            ox, _ = calculate_workload_scoring(OX)
            multi, _ = calculate_workload_scoring(MULTI)
        assert set(ox) == {"Anju", "Kiran", "Nisha"}
        assert set(multi) == {"Ravi", "Meera"}

    def test_inactive_staff_excluded(self, seeded):
        with _APP.app_context():
            _s, active = calculate_workload_scoring(MULTI)
        assert "Old Staff" not in active.values()

    def test_empty_tenant_scores_nothing(self, seeded):
        with _APP.app_context():
            scores, active = calculate_workload_scoring(EMPTY)
        assert scores == {} and active == {}

    def test_recommendations_follow_the_scoring(self, seeded):
        with _APP.app_context():
            recs = get_staff_recommendations(limit=5)   # no tenant ctx
        assert recs == [], "must fail closed without a tenant"


# ═══ Criterion 1 — Lead Create ═══════════════════════════════════════════════

class TestLeadCreate:
    def test_oxford_dropdown_unchanged(self, seeded):
        opts = staff_options(html_of(seeded[OX], "/crm/lead/new"), "assigned_staff")
        assert opts == ["Anju", "Kiran", "Nisha"]

    def test_matches_the_legacy_file_for_oxford(self, seeded):
        legacy = sorted(d["display_name"] for d in load_staff_registry().values()
                        if d.get("active"))
        assert staff_options(html_of(seeded[OX], "/crm/lead/new"),
                             "assigned_staff") == legacy

    def test_other_tenant_sees_no_oxford_staff(self, seeded):
        opts = staff_options(html_of(seeded[MULTI], "/crm/lead/new"), "assigned_staff")
        assert opts == ["Meera", "Ravi"]
        assert not {"Anju", "Kiran", "Nisha"} & set(opts)

    def test_unassigned_option_survives(self, seeded):
        assert "" in options_of(html_of(seeded[OX], "/crm/lead/new"), "assigned_staff")

    def test_zero_staff_tenant_renders(self, seeded):
        html = html_of(seeded[EMPTY], "/crm/lead/new")
        assert staff_options(html, "assigned_staff") == []
        assert "" in options_of(html, "assigned_staff")


# ═══ Criterion 2 — Lead Detail ═══════════════════════════════════════════════

class TestLeadDetail:
    def test_oxford_owner_dropdown_unchanged(self, seeded):
        html = html_of(seeded[OX], "/crm/lead/+919000000001")
        assert staff_options(html, "assigned_staff") == ["Anju", "Kiran", "Nisha"]

    def test_current_owner_stays_selected(self, seeded):
        html = html_of(seeded[OX], "/crm/lead/+919000000001")
        assert re.search(r'<option value="Anju"[^>]*selected', html)

    def test_non_listed_owner_is_preserved_as_inactive(self, seeded):
        """F4: the lead owned by the phantom must still render and round-trip,
        or migrating the list would silently blank ownership on save."""
        html = html_of(seeded[OX], "/crm/lead/+919000000006")
        assert "Anju_Display" in html
        assert "(Inactive)" in html

    def test_other_tenant_sees_only_its_own(self, seeded):
        html = html_of(seeded[MULTI], "/crm/lead/+919100000001")
        assert staff_options(html, "assigned_staff") == ["Meera", "Ravi"]

    def test_task_assignee_lists_tenant_staff(self, seeded):
        html = html_of(seeded[OX], "/crm/lead/+919000000001")
        assert staff_options(html, "staff") == ["Anju", "Kiran", "Nisha"]


class TestZeroStaffTaskCreation:
    """Approved operator decision 1: task creation MUST remain possible."""

    def test_assignee_field_is_not_required_without_staff(self, seeded):
        html = html_of(seeded[EMPTY], "/crm/lead/+919200000001")
        m = re.search(r'<select[^>]*id="task_staff"[^>]*>', html)
        assert m and "required" not in m.group(0), m.group(0) if m else "missing"

    def test_unassigned_option_is_offered(self, seeded):
        html = html_of(seeded[EMPTY], "/crm/lead/+919200000001")
        assert staff_options(html, "staff") == []
        assert "-- Unassigned --" in html

    def test_task_creation_succeeds_with_no_staff(self, seeded):
        from app.models import Task
        r = client_for(seeded[EMPTY]).post("/crm/tasks/create", data={
            "phone": "+919200000001", "task": "Call back",
            "due_date": "2026-12-01", "staff": "", "priority": "NORMAL"})
        assert r.status_code in (302, 303)
        with _APP.app_context():
            t = Task.query.filter_by(tenant_id=EMPTY).first()
            assert t is not None, "operator decision 1 violated: creation blocked"
            assert t.assigned_staff is None

    def test_required_is_retained_when_staff_exist(self, seeded):
        """Oxford behaviour must be preserved exactly — the field is still
        required where it always was."""
        html = html_of(seeded[OX], "/crm/lead/+919000000001")
        m = re.search(r'<select[^>]*id="task_staff"[^>]*>', html)
        assert m and "required" in m.group(0)


# ═══ Criteria 3 & 4 — triage screens ═════════════════════════════════════════

class TestUnassignedLeads:
    def test_oxford_picker_unchanged(self, seeded):
        html = html_of(seeded[OX], "/crm/leads/unassigned")
        assert staff_options(html, "target_staff") == ["Anju", "Kiran", "Nisha"]

    def test_recommendations_render_for_oxford(self, seeded):
        html = html_of(seeded[OX], "/crm/leads/unassigned")
        assert "No active staff available" not in html

    def test_other_tenant_has_no_oxford_names(self, seeded):
        html = html_of(seeded[MULTI], "/crm/leads/unassigned")
        assert not {"Anju", "Kiran", "Nisha"} & set(staff_options(html, "target_staff"))

    def test_zero_staff_shows_the_empty_state(self, seeded):
        html = html_of(seeded[EMPTY], "/crm/leads/unassigned")
        assert "No active staff available for recommendations." in html


class TestReassignmentCentre:
    def test_oxford_picker_unchanged(self, seeded):
        html = html_of(seeded[OX], "/crm/reassignment-center")
        assert staff_options(html, "targetStaff") == ["Anju", "Kiran", "Nisha"]

    def test_other_tenant_scoped(self, seeded):
        html = html_of(seeded[MULTI], "/crm/reassignment-center")
        assert staff_options(html, "targetStaff") == ["Meera", "Ravi"]

    def test_zero_staff_empty_state(self, seeded):
        html = html_of(seeded[EMPTY], "/crm/reassignment-center")
        assert "No active staff available for recommendations." in html

    def test_agrees_with_unassigned_leads(self, seeded):
        """The batch's designated rollback trigger: these two screens share
        get_staff_recommendations() and must never disagree."""
        for tid in (OX, MULTI, EMPTY):
            a = html_of(seeded[tid], "/crm/leads/unassigned")
            b = html_of(seeded[tid], "/crm/reassignment-center")
            # /crm/leads/unassigned renders its per-row select only when the
            # tenant actually HAS unassigned leads. When it does, the two
            # screens must offer exactly the same people.
            if 'name="target_staff"' not in a:
                continue
            assert set(staff_options(a, "target_staff")) == \
                set(staff_options(b, "targetStaff")), f"screens disagree for {tid}"


# ═══ Scenario matrix ═════════════════════════════════════════════════════════

class TestTenantScenarios:
    def test_oxford_is_a_no_op(self, seeded):
        """Any visible change for Oxford is a rollback trigger."""
        legacy = sorted(d["display_name"] for d in load_staff_registry().values()
                        if d.get("active"))
        for url, sel in (("/crm/lead/new", "assigned_staff"),
                         ("/crm/reassignment-center", "targetStaff")):
            assert staff_options(html_of(seeded[OX], url), sel) == legacy

    def test_inactive_staff_never_offered(self, seeded):
        for url, sel in (("/crm/lead/new", "assigned_staff"),
                         ("/crm/reassignment-center", "targetStaff")):
            assert "Old Staff" not in staff_options(html_of(seeded[MULTI], url), sel)

    def test_every_batch1_screen_returns_200_for_every_tenant(self, seeded):
        for tid in (OX, MULTI, EMPTY):
            for url in ("/crm/lead/new", "/crm/leads/unassigned",
                        "/crm/reassignment-center"):
                assert client_for(seeded[tid]).get(url).status_code == 200

    def test_super_admin_fails_closed(self, seeded):
        """Approved operator decision 2: without tenant context, expose no
        staff at all rather than staff across tenants."""
        with _APP.app_context():
            su = _mk(None, "platform_root", role="SUPER_ADMIN")
            sid = su.id
            scores, active = calculate_workload_scoring(None)
        assert active == {} and scores == {}
        r = client_for(sid).get("/crm/lead/new")
        if r.status_code == 200:
            body = r.get_data(as_text=True)
            assert not {"Anju", "Kiran", "Nisha"} & set(
                staff_options(body, "assigned_staff"))


# ═══ Scope containment ═══════════════════════════════════════════════════════

def _tree():
    with open(os.path.join(ROOT, "app", "routes", "admin.py"),
              encoding="utf-8") as fh:
        return ast.parse(fh.read())


class TestScopeContainment:
    BATCH1 = {"crm_lead_new", "crm_lead_detail", "crm_unassigned_leads",
              "crm_reassignment_center", "calculate_workload_scoring"}

    def test_batch1_consumers_no_longer_read_the_file(self):
        tree = _tree()
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name in self.BATCH1:
                called = {ast.unparse(c.func) for c in ast.walk(fn)
                          if isinstance(c, ast.Call)}
                assert "load_staff_registry" not in called, fn.name

    def test_unmigrated_consumers_remain(self):
        """Was ==10 after Batch 1. Batch 2 migrated seven more by approved
        plan, so the exact count is now owned by
        test_staff_batch2_rc22d.py::test_batch3_consumers_are_untouched.
        Here it only needs to stay non-zero — the file is still load-bearing
        for Batch 3."""
        tree = _tree()
        n = sum(1 for c in ast.walk(tree)
                if isinstance(c, ast.Call)
                and ast.unparse(c.func) == "load_staff_registry")
        # Batch 3 migrated the final three, so this is now legitimately 0.
        # The authoritative assertion moved to
        # test_staff_batch3_rc22d.py::test_zero_runtime_calls_to_load_staff_registry.
        assert n == 0, f"expected the migration to be complete, found {n}"

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
        # Batch 2 added seven more by approved plan; the authoritative set
        # lives in test_staff_batch2_rc22d.py. This suite guards only that
        # its own five are still migrated.
        assert self.BATCH1 <= users, f"a Batch 1 consumer regressed: {users}"

    def test_every_call_is_tenant_scoped(self):
        tree = _tree()
        for c in ast.walk(tree):
            if isinstance(c, ast.Call) and \
               ast.unparse(c.func).startswith("staff_service."):
                # _tid / _tid_staff added by Batch 2: both are locals bound
                # from _actor_tenant_id(). A literal tenant is what this
                # guards against.
                assert ast.unparse(c.args[0]) in \
                    ("_tenant", "_actor_tenant_id()", "tid", "_tid",
                     "_tid_staff"), ast.unparse(c)

    def test_legacy_registry_functions_still_exist(self):
        from app.routes import admin
        for name in ("load_staff_registry", "save_staff_registry",
                     "get_staff_json_path"):
            assert callable(getattr(admin, name))
        assert os.path.exists(os.path.join(ROOT, "app", "data",
                                           "staff_master.json"))

    def test_no_schema_or_migration_change(self):
        versions = os.path.join(ROOT, "migrations", "versions")
        assert not [f for f in os.listdir(versions) if "rc22d" in f.lower()]
