"""Phase RC2.3E-1 Batch 2 — task authorization moves onto the FK.

authorize_assignee() decided task mutation rights by comparing two normalized
strings that were never the same field:

    assignee = task.assigned_staff   -> a DISPLAY LABEL (what the dropdown writes)
    actor    = _actor_name()         -> normalize_staff_name(USERNAME)

They agree only while display_name is unset. `username` is unique per tenant;
`display_name` has NO uniqueness constraint. Two failure modes followed, and
BOTH exist in production today:

  LOCKOUT      username NIBU01 with display label 'nibu' -> actor key 'Nibu01'
               vs assignee key 'Nibu' -> refused access to their OWN task.

  ESCALATION   username 'NIBU' normalizes to 'Nibu', which equals NIBU01's
               display label -> 'NIBU' could mutate NIBU01's tasks. Survivable
               in production only because that account happens to be an ADMIN
               and short-circuits above the comparison. A STAFF account in the
               same shape is the staff-to-staff escalation 16.5A7-B closed.

OPTION 2 (approved)
-------------------
The FK is authoritative whenever BOTH sides have one. Names are compared only
where they are the only identity available — rows predating the dual-write,
and the legacy no-Task-row completion path, whose assignee lives in a
FOLLOW_UP_TASK JSON payload (15 such events in production, none with a FK).

LEGACY FAILS CLOSED (approved)
------------------------------
On that path the name must identify EXACTLY ONE current staff member.
staff_service.resolve() returns None both for unknown and for ambiguous, and
both are refused: a name that does not pick out one person cannot authorize
anything. 'nibu' currently matches two users, so it resolves to None — and
must therefore be denied, not allowed.

Import isolation follows test_aggregation_keys_rc23e1_b4.py.
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

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23e1b2_authz.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc23e1b2-admin-key")
os.environ.setdefault("SECRET_KEY", "rc23e1b2-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc23e1b2-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, Task, ConversationState, LeadEvent  # noqa: E402
from app.services import task_service                                   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX = "t-ox"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False

REGIMES = [pytest.param(False, id="flag_off_names"),
           pytest.param(True, id="flag_on_fk")]


@pytest.fixture()
def regime(request):
    before = os.environ.get("STAFF_IDENTITY_READ_FK")
    os.environ["STAFF_IDENTITY_DUAL_WRITE"] = "true"
    if request.param:
        os.environ["STAFF_IDENTITY_READ_FK"] = "true"
    else:
        os.environ.pop("STAFF_IDENTITY_READ_FK", None)
    yield request.param
    if before is None:
        os.environ.pop("STAFF_IDENTITY_READ_FK", None)
    else:
        os.environ["STAFF_IDENTITY_READ_FK"] = before


def _mk(username, role="STAFF", display_name=None, tenant=OX):
    u = User(username=username, email=f"{username}.{tenant}@x.test".replace(" ", "_"),
             password_hash=generate_password_hash("pw"), role=role,
             tenant_id=tenant, is_active=True, display_name=display_name,
             require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture()
def seeded():
    """Mirrors production: a renamed staff member and a colliding username."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=OX, name="Oxford", slug=OX, status="ACTIVE",
                              billing_exempt=True))
        db.session.commit()

        anju = _mk("Anju")
        # THE LOCKOUT SHAPE: username != display label.
        nibu01 = _mk("NIBU01", display_name="nibu")
        # THE ESCALATION SHAPE: this username normalizes onto nibu01's label.
        # STAFF here, deliberately — production's is an ADMIN and so never
        # reaches the comparison, which hides the defect.
        nibu = _mk("NIBU", display_name=None)
        admin = _mk("admin_ox", role="ADMIN")
        ids = {"anju": anju.id, "nibu01": nibu01.id,
               "nibu": nibu.id, "admin": admin.id}

        db.session.add(ConversationState(
            phone="919600000001", tenant_id=OX, name="Lead1",
            lead_status="Lead", assigned_staff="nibu",
            assigned_user_id=nibu01.id))
        db.session.commit()

        # A Task owned by nibu01, carrying BOTH identities.
        t = Task(tenant_id=OX, task_uid="task-nibu-1", lead_phone="919600000001",
                 title="Call the lead", priority="NORMAL", status="OPEN",
                 assigned_staff="nibu", assigned_user_id=nibu01.id,
                 created_by="admin_ox")
        db.session.add(t)
        # A Task owned by Anju, unambiguous.
        t2 = Task(tenant_id=OX, task_uid="task-anju-1", lead_phone="919600000001",
                  title="Anju task", priority="NORMAL", status="OPEN",
                  assigned_staff="Anju", assigned_user_id=anju.id,
                  created_by="admin_ox")
        db.session.add(t2)
        # A PRE-dual-write task: name only, no FK. The name path must survive.
        t3 = Task(tenant_id=OX, task_uid="task-legacy-1", lead_phone="919600000001",
                  title="Old task", priority="NORMAL", status="OPEN",
                  assigned_staff="Anju", assigned_user_id=None,
                  created_by="admin_ox")
        db.session.add(t3)
        db.session.commit()
        ids["task_nibu"] = t.id
        ids["task_anju"] = t2.id
        ids["task_legacy"] = t3.id

        # Legacy FOLLOW_UP_TASK events: no Task row, no FK — name is the only
        # identity the legacy completion path can authorize against.
        for uid, who in (("legacy-evt-anju", "Anju"),
                         ("legacy-evt-nibu", "nibu")):
            db.session.add(LeadEvent(
                tenant_id=OX, phone="919600000001", event_type="FOLLOW_UP_TASK",
                event_data=json.dumps({"task_id": uid, "task": "legacy",
                                       "staff": who})))
        db.session.commit()
        yield ids
        db.session.remove()


# ═══ the lockout ═════════════════════════════════════════════════════════════

class TestLockout:

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_renamed_staff_may_complete_their_own_task(self, seeded, regime):
        """THE defect: actor key 'Nibu01' never equalled assignee key 'Nibu',
        so this raised TaskForbidden on the owner's own task."""
        with _APP.app_context():
            t = task_service.complete_task(
                OX, seeded["task_nibu"], "Nibu01", is_admin=False,
                actor_user_id=seeded["nibu01"])
            assert t.status == "COMPLETED"

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_renamed_staff_may_staff_update_their_own_task(self, seeded, regime):
        with _APP.app_context():
            t = task_service.staff_update(
                OX, seeded["task_nibu"], "Nibu01", status="IN_PROGRESS",
                is_admin=False, actor_user_id=seeded["nibu01"])
            assert t.status == "IN_PROGRESS"

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_renamed_staff_may_complete_VIA_staff_update(self, seeded, regime):
        """staff_update(status=COMPLETED) DELEGATES to complete_task, which
        authorizes a second time. If the id is not forwarded that second check
        falls back to the name compare and locks the owner out again — a path
        the IN_PROGRESS case above never exercises.
        """
        with _APP.app_context():
            t = task_service.staff_update(
                OX, seeded["task_nibu"], "Nibu01", status="COMPLETED",
                is_admin=False, actor_user_id=seeded["nibu01"])
            assert t.status == "COMPLETED"

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_stranger_still_refused_through_the_delegation(self, seeded, regime):
        with _APP.app_context():
            with pytest.raises(task_service.TaskForbidden):
                task_service.staff_update(
                    OX, seeded["task_anju"], "Nibu01", status="COMPLETED",
                    is_admin=False, actor_user_id=seeded["nibu01"])


# ═══ the escalation ══════════════════════════════════════════════════════════

class TestEscalation:

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_colliding_username_cannot_touch_another_users_task(
            self, seeded, regime):
        """'NIBU' normalizes to 'Nibu', which equals nibu01's display label.
        Under the old name compare this was ALLOWED."""
        with _APP.app_context():
            with pytest.raises(task_service.TaskForbidden):
                task_service.complete_task(
                    OX, seeded["task_nibu"], "Nibu", is_admin=False,
                    actor_user_id=seeded["nibu"])

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_unrelated_staff_still_cannot_touch_a_task(self, seeded, regime):
        with _APP.app_context():
            with pytest.raises(task_service.TaskForbidden):
                task_service.complete_task(
                    OX, seeded["task_anju"], "Nibu01", is_admin=False,
                    actor_user_id=seeded["nibu01"])

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_owner_still_may(self, seeded, regime):
        with _APP.app_context():
            t = task_service.complete_task(
                OX, seeded["task_anju"], "Anju", is_admin=False,
                actor_user_id=seeded["anju"])
            assert t.status == "COMPLETED"

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_admin_may_still_touch_anything(self, seeded, regime):
        with _APP.app_context():
            t = task_service.complete_task(
                OX, seeded["task_nibu"], "Admin_Ox", is_admin=True,
                actor_user_id=seeded["admin"])
            assert t.status == "COMPLETED"


# ═══ the name path still works where it is the only identity ═════════════════

class TestNamePathFallback:

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_pre_dualwrite_task_still_authorizes_by_name(self, seeded, regime):
        """assigned_user_id is NULL on this row, so the FK branch cannot
        apply. The owner must still be able to act."""
        with _APP.app_context():
            t = task_service.complete_task(
                OX, seeded["task_legacy"], "Anju", is_admin=False,
                actor_user_id=seeded["anju"])
            assert t.status == "COMPLETED"

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_pre_dualwrite_task_still_refuses_a_stranger(self, seeded, regime):
        with _APP.app_context():
            with pytest.raises(task_service.TaskForbidden):
                task_service.complete_task(
                    OX, seeded["task_legacy"], "Nibu01", is_admin=False,
                    actor_user_id=seeded["nibu01"])

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_missing_actor_id_falls_back_to_the_name(self, seeded, regime):
        """Back-compat: a caller that passes no id keeps the old behaviour
        rather than failing open OR locking everyone out."""
        with _APP.app_context():
            t = task_service.complete_task(
                OX, seeded["task_anju"], "Anju", is_admin=False)
            assert t.status == "COMPLETED"

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_unassigned_task_is_admin_only(self, seeded, regime):
        with _APP.app_context():
            t = Task(tenant_id=OX, task_uid="task-unassigned", title="x",
                     priority="NORMAL", status="OPEN", assigned_staff=None,
                     assigned_user_id=None, created_by="admin_ox")
            db.session.add(t)
            db.session.commit()
            with pytest.raises(task_service.TaskForbidden):
                task_service.complete_task(OX, t.id, "Anju", is_admin=False,
                                           actor_user_id=seeded["anju"])


# ═══ legacy path fails closed ════════════════════════════════════════════════

class TestLegacyFailsClosed:

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_ambiguous_legacy_name_is_refused(self, seeded, regime):
        """'nibu' matches TWO users, so resolve() returns None. A name that
        does not identify one person cannot authorize anything."""
        with _APP.app_context():
            with pytest.raises(task_service.TaskForbidden):
                task_service.authorize_assignee(
                    "nibu", "Nibu01", False, tenant_id=OX,
                    actor_user_id=seeded["nibu01"])

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_unknown_legacy_name_is_refused(self, seeded, regime):
        with _APP.app_context():
            with pytest.raises(task_service.TaskForbidden):
                task_service.authorize_assignee(
                    "Ghost", "Ghost", False, tenant_id=OX)

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_unambiguous_legacy_owner_is_allowed(self, seeded, regime):
        with _APP.app_context():
            task_service.authorize_assignee(
                "Anju", "Anju", False, tenant_id=OX,
                actor_user_id=seeded["anju"])

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_legacy_non_owner_is_refused_by_id(self, seeded, regime):
        with _APP.app_context():
            with pytest.raises(task_service.TaskForbidden):
                task_service.authorize_assignee(
                    "Anju", "Anju", False, tenant_id=OX,
                    actor_user_id=seeded["nibu01"])

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_admin_bypasses_the_legacy_check(self, seeded, regime):
        with _APP.app_context():
            task_service.authorize_assignee("nibu", "Admin_Ox", True,
                                            tenant_id=OX)

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_without_tenant_the_old_name_compare_is_kept(self, seeded, regime):
        """No tenant means no resolver, so the check cannot fail closed —
        it must not silently allow either. Old behaviour preserved."""
        with _APP.app_context():
            task_service.authorize_assignee("Anju", "Anju", False)
            with pytest.raises(task_service.TaskForbidden):
                task_service.authorize_assignee("Anju", "Kiran", False)


# ═══ structural ══════════════════════════════════════════════════════════════

class TestStructure:

    def _svc(self, name):
        with open(os.path.join(ROOT, "app/services/task_service.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        return ast.unparse(next(n for n in ast.walk(tree)
                                if isinstance(n, ast.FunctionDef) and n.name == name))

    def _adm(self, name):
        with open(os.path.join(ROOT, "app/routes/admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        return ast.unparse(next(n for n in ast.walk(tree)
                                if isinstance(n, ast.FunctionDef) and n.name == name))

    def test_fk_is_preferred_when_both_sides_have_one(self):
        src = self._svc("_authorize_mutation")
        assert "task.assigned_user_id is not None and actor_user_id is not None" in src
        assert "task.assigned_user_id != actor_user_id" in src

    def test_name_path_is_still_reachable(self):
        assert "authorize_assignee(task.assigned_staff" in self._svc("_authorize_mutation")

    def test_legacy_resolves_and_fails_closed(self):
        src = self._svc("authorize_assignee")
        assert "staff_service.resolve(tenant_id, assignee)" in src
        assert "if owner is None:" in src
        assert "raise TaskForbidden" in src

    def test_delegation_carries_the_actor_id(self):
        """staff_update -> complete_task authorizes again; without the id that
        second check silently falls back to the name compare."""
        assert "actor_user_id=actor_user_id" in self._svc("staff_update")

    def test_routes_supply_the_actor_id(self):
        """Three call sites: staff_update, the Task completion path, and the
        legacy completion path. `or True` in an earlier draft of this test
        made the first assertion vacuous — asserted properly here."""
        with open(os.path.join(ROOT, "app/routes/admin.py"), encoding="utf-8") as fh:
            raw = fh.read()
        tree = ast.parse(raw)
        callers = {fn.name for fn in ast.walk(tree)
                   if isinstance(fn, ast.FunctionDef)
                   and fn.name != "_actor_user_id"
                   and "_actor_user_id()" in ast.unparse(fn)}
        # THREE call sites living in TWO routes — the legacy completion
        # authorization sits inside crm_tasks_complete, not a route of its own.
        assert raw.count("actor_user_id=_actor_user_id()") == 3
        assert callers == {"crm_tasks_complete", "crm_tasks_staff_update"}, callers

    def test_legacy_call_site_passes_the_tenant(self):
        with open(os.path.join(ROOT, "app/routes/admin.py"), encoding="utf-8") as fh:
            src = fh.read()
        assert "tenant_id=_tid," in src

    def test_admin_only_routes_did_not_gain_a_dead_param(self):
        """crm_tasks_edit / crm_tasks_delete are @admin_required, so their
        service functions never authorize per-assignee and must not carry an
        unused actor_user_id."""
        import inspect
        assert "actor_user_id" not in self._svc("update_task").split("\n")[0]
        assert "actor_user_id" not in self._svc("delete_task").split("\n")[0]

    def _phase_commit_files(self, marker):
        """Files touched by the commit that introduced `marker`.

        Asserted against the COMMIT, not `git status`. A worktree assertion
        breaks the moment a LATER, separately approved phase ships a change --
        a phase's scope is a fact about what it shipped, not about what anyone
        is editing now.
        """
        import subprocess
        sha = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%H", "-1", "--", marker],
            cwd=ROOT, capture_output=True, text=True).stdout.strip()
        if not sha:
            return None
        return sorted(subprocess.run(
            ["git", "show", "--name-only", "--format=", sha],
            cwd=ROOT, capture_output=True, text=True).stdout.split())

    def test_no_schema_or_migration_change(self):
        """Batch 2 shipped no migration or schema change.

        Phase RC2.4.2: converted from `git status --porcelain -- migrations/`
        to a COMMIT-scoped check. The worktree form asserted that NOBODY has a
        migration in progress, which is not this phase's business and which
        failed the moment RC2.4.2 added an authorised one. The invariant is
        unchanged and still enforced: Batch 2's OWN committed changeset must
        contain no migrations/ path.
        """
        files = self._phase_commit_files("tests/test_task_authz_rc23e1_b2.py")
        if files is None:
            pytest.skip("Batch 2 is not committed yet")
        migrations = [f for f in files if f.startswith("migrations/")]
        assert migrations == [], (
            f"Batch 2 committed a migration: {migrations}")

    def test_flag_default_is_still_off(self):
        from app import flags
        before = os.environ.pop("STAFF_IDENTITY_READ_FK", None)
        try:
            assert flags.staff_identity_read_fk_enabled() is False
        finally:
            if before is not None:
                os.environ["STAFF_IDENTITY_READ_FK"] = before
