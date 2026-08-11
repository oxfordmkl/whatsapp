"""Phase H4-a — tenant resolution for the four routes that consume _tid.

THE DEFECT
----------
getattr(current_user, 'tenant_id', None) is NULL for a SUPER_ADMIN, whose
tenant lives in session['impersonate_tenant_id'].

For MOST routes that made no difference: tenant_query()'s SUPER_ADMIN branch
returns BEFORE it reads its tenant_id argument, and for everyone else
`tenant_id or current_user.tenant_id` yields the same value. Those seven
routes are H4-b, and are deliberately NOT touched here.

These four are different — each passes _tid into a function that consumes
tenant context directly:

    crm_lead_update              resolve_assignment, _sync_assigned_user,
                                 transition_verdict, log_audit, notify
    crm_lead_send                log_message, save_conversation_message
    crm_lead_detail              _sps.get_stage_history
    crm_staff_performance_detail get_all_tasks

crm_lead_update is the sharp one. With _tid None, H3's validator resolves no
staff and REJECTS every edit an impersonating SUPER_ADMIN makes, reporting
"not a current staff member" — blaming the staff member for a tenant-context
bug. That fail-closed behaviour is also what prevented data damage, and it
must survive this fix unchanged.

THE MECHANISM
-------------
_actor_tenant_id(), which already exists and already implements the required
invariant. No new abstraction, no global mutable context, no inference from
names:

    normal user                     -> their own tenant
    SUPER_ADMIN, no impersonation   -> None (fail closed)
    SUPER_ADMIN, impersonating      -> the impersonated tenant
    unauthenticated                 -> None
"""
import json
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_h4a_tenant.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "h4a-admin-key")
os.environ.setdefault("SECRET_KEY", "h4a-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "h4a-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import (Tenant, User, ConversationState, LeadEvent,      # noqa: E402
                        Task, MessageLog)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX = "t-ox"
OTHER = "t-other"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False


@pytest.fixture(autouse=True)
def dual_write_on():
    before = os.environ.get("STAFF_IDENTITY_DUAL_WRITE")
    os.environ["STAFF_IDENTITY_DUAL_WRITE"] = "true"
    os.environ.pop("STAFF_IDENTITY_READ_FK", None)
    yield
    if before is None:
        os.environ.pop("STAFF_IDENTITY_DUAL_WRITE", None)
    else:
        os.environ["STAFF_IDENTITY_DUAL_WRITE"] = before


def _mk(tenant, username, role="STAFF", display_name=None):
    u = User(username=username,
             email=f"{username}.{tenant or 'plat'}@x.test".replace(" ", "_"),
             password_hash=generate_password_hash("pw"), role=role,
             tenant_id=tenant, is_active=True, display_name=display_name,
             require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((OX, "Oxford"), (OTHER, "Other")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()

        ids = {
            "anju": _mk(OX, "Anju").id,
            "kiran": _mk(OX, "Kiran").id,
            "admin": _mk(OX, "admin_ox", role="ADMIN").id,
            "other_staff": _mk(OTHER, "Ravi").id,
            "other_admin": _mk(OTHER, "admin_other", role="ADMIN").id,
            # A SUPER_ADMIN has NO tenant of its own — the whole point.
            "super": _mk(None, "platform_admin", role="SUPER_ADMIN").id,
        }

        db.session.add(ConversationState(
            phone="919700000001", tenant_id=OX, name="Oxford Lead",
            lead_status="Lead", assigned_staff="Anju",
            assigned_user_id=ids["anju"]))
        db.session.add(ConversationState(
            phone="919700000077", tenant_id=OTHER, name="Other Lead",
            lead_status="Lead", assigned_staff="Ravi",
            assigned_user_id=ids["other_staff"]))
        db.session.add(Task(
            tenant_id=OX, task_uid="h4a-task-1", lead_phone="919700000001",
            title="Oxford task", priority="NORMAL", status="OPEN",
            assigned_staff="Anju", assigned_user_id=ids["anju"],
            created_by="admin_ox"))
        db.session.add(Task(
            tenant_id=OTHER, task_uid="h4a-task-2", lead_phone="919700000077",
            title="Other task", priority="NORMAL", status="OPEN",
            assigned_staff="Ravi", assigned_user_id=ids["other_staff"],
            created_by="admin_other"))
        db.session.commit()
        yield ids
        db.session.remove()


def client(uid, impersonate=None):
    """A logged-in client, optionally impersonating a tenant.

    session['impersonate_tenant_id'] is the ONLY mechanism by which a
    SUPER_ADMIN acquires tenant context. No existing suite builds this shape,
    which is why H4 went unnoticed.
    """
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
        if impersonate is not None:
            s["impersonate_tenant_id"] = impersonate
    return c


@pytest.fixture()
def stage_history(seeded):
    """One LeadStageHistory row per tenant, on each tenant's own lead."""
    from app.models import LeadStageHistory
    with _APP.app_context():
        for tid, phone in ((OX, "919700000001"), (OTHER, "919700000077")):
            row = ConversationState.query.filter_by(phone=phone,
                                                    tenant_id=tid).first()
            db.session.add(LeadStageHistory(
                tenant_id=tid, conversation_state_id=row.id,
                from_status="Lead", to_status="Contacted", actor="seed"))
        db.session.commit()
    yield


def lead(phone, tenant):
    with _APP.app_context():
        return ConversationState.query.filter_by(phone=phone,
                                                 tenant_id=tenant).first()


def update(c, phone, **form):
    data = {"lead_status": "Lead", "assigned_staff": "", "notes": ""}
    data.update(form)
    return c.post(f"/crm/lead/{phone}/update", data=data,
                  follow_redirects=False)


# ═══ 1-6: the resolution invariant, on every H4-a route ══════════════════════

ROUTES = [
    ("/crm/lead/919700000001", "crm_lead_detail"),
    ("/crm/staff-performance-detail", "crm_staff_performance_detail"),
]


class TestResolutionInvariant:

    def test_1_normal_admin_still_works(self, seeded):
        for url, name in ROUTES:
            r = client(seeded["admin"]).get(url, follow_redirects=True)
            assert r.status_code == 200, name

    def test_1b_normal_staff_still_works(self, seeded):
        r = client(seeded["anju"]).get("/crm/lead/919700000001",
                                       follow_redirects=True)
        assert r.status_code in (200, 302, 403)

    def test_2_super_admin_without_impersonation_fails_closed(self, seeded):
        """No tenant context => no tenant-scoped data. Never everyone's."""
        r = client(seeded["super"]).get("/crm/lead/919700000001",
                                        follow_redirects=True)
        assert "Oxford Lead" not in r.get_data(as_text=True)

    def test_3_super_admin_with_impersonation_gets_that_tenant(self, seeded):
        r = client(seeded["super"], impersonate=OX).get(
            "/crm/lead/919700000001", follow_redirects=True)
        assert r.status_code == 200
        assert "919700000001" in r.get_data(as_text=True)

    def test_4_changing_the_impersonated_tenant_changes_context(self, seeded):
        """OX sees the Oxford lead; OTHER does not — same actor, same URL."""
        a = client(seeded["super"], impersonate=OX).get(
            "/crm/lead/919700000001", follow_redirects=True).get_data(as_text=True)
        b = client(seeded["super"], impersonate=OTHER).get(
            "/crm/lead/919700000001", follow_redirects=True).get_data(as_text=True)
        assert "919700000001" in a
        assert "Oxford Lead" not in b

    def test_5_removing_impersonation_fails_closed_again(self, seeded):
        with_imp = client(seeded["super"], impersonate=OX).get(
            "/crm/lead/919700000001", follow_redirects=True)
        without = client(seeded["super"]).get(
            "/crm/lead/919700000001", follow_redirects=True)
        assert "919700000001" in with_imp.get_data(as_text=True)
        assert "Oxford Lead" not in without.get_data(as_text=True)

    def test_6_invalid_impersonation_tenant_fails_safely(self, seeded):
        """A tenant id that does not exist must not error or leak."""
        r = client(seeded["super"], impersonate="t-does-not-exist").get(
            "/crm/lead/919700000001", follow_redirects=True)
        body = r.get_data(as_text=True)
        assert "Traceback" not in body
        assert "Oxford Lead" not in body
        assert "Other Lead" not in body


# ═══ 7-13: crm_lead_update, the write path ═══════════════════════════════════

class TestLeadUpdate:

    def test_7_impersonating_super_admin_can_update(self, seeded):
        """THE fix. Before it, H3's validator resolved no staff for a NULL
        tenant and rejected this with 'not a current staff member'."""
        c = client(seeded["super"], impersonate=OX)
        r = update(c, "919700000001", assigned_staff="Kiran")
        assert "err=" not in r.headers.get("Location", ""), \
            r.headers.get("Location")
        row = lead("919700000001", OX)
        assert row.assigned_staff == "Kiran"
        assert row.assigned_user_id == seeded["kiran"]

    def test_8_same_operation_fails_without_impersonation(self, seeded):
        """Fail-closed is preserved: no tenant context, no write."""
        c = client(seeded["super"])
        update(c, "919700000001", assigned_staff="Kiran")
        row = lead("919700000001", OX)
        assert row.assigned_staff == "Anju", "wrote without tenant context"
        assert row.assigned_user_id == seeded["anju"]

    def test_9_assignment_validates_against_the_impersonated_tenant(self, seeded):
        c = client(seeded["super"], impersonate=OX)
        r = update(c, "919700000001", assigned_staff="Ravi")   # OTHER's staff
        assert "err=" in r.headers.get("Location", "")
        assert lead("919700000001", OX).assigned_staff == "Anju"

    def test_10_cannot_update_across_the_impersonated_boundary(self, seeded):
        """Impersonating OX must not reach OTHER's lead."""
        c = client(seeded["super"], impersonate=OX)
        update(c, "919700000077", assigned_staff="Kiran")
        row = lead("919700000077", OTHER)
        assert row.assigned_staff == "Ravi"
        assert row.assigned_user_id == seeded["other_staff"]

    def test_11_h3_fail_closed_intact_for_normal_admins(self, seeded):
        c = client(seeded["admin"])
        r = update(c, "919700000001", assigned_staff="Ghost")
        assert "err=" in r.headers.get("Location", "")
        assert lead("919700000001", OX).assigned_staff == "Anju"

    def test_12_no_null_tenant_rows_are_created(self, seeded):
        """The damage shape H4 could have produced."""
        c = client(seeded["super"], impersonate=OX)
        update(c, "919700000001", assigned_staff="Kiran")
        with _APP.app_context():
            for model in (ConversationState, LeadEvent, Task):
                n = model.query.filter(model.tenant_id.is_(None)).count()
                assert n == 0, f"{model.__name__} gained a NULL-tenant row"

    def test_13_events_are_attributed_to_the_impersonated_tenant(self, seeded):
        c = client(seeded["super"], impersonate=OX)
        update(c, "919700000001", assigned_staff="Kiran")
        with _APP.app_context():
            evs = LeadEvent.query.filter_by(phone="919700000001").all()
            assert evs, "no lead event written"
            assert all(e.tenant_id == OX for e in evs), \
                [e.tenant_id for e in evs]

    def test_13b_normal_admin_write_behaviour_unchanged(self, seeded):
        c = client(seeded["admin"])
        r = update(c, "919700000001", assigned_staff="Kiran")
        assert "err=" not in r.headers.get("Location", "")
        assert lead("919700000001", OX).assigned_user_id == seeded["kiran"]


# ═══ crm_lead_detail: stage history ══════════════════════════════════════════

class TestLeadDetailStageHistory:
    """get_stage_history(tenant_id, ...) returns [] for a falsy tenant — an
    explicit guard in the service. So under the old idiom an impersonating
    SUPER_ADMIN saw the lead (tenant_query handled that) but NO stage history
    beside it. This is the discriminating check for this route."""

    def _ctx(self, c, phone="919700000001"):
        from flask import template_rendered
        captured = {}

        def record(sender, template, context, **extra):
            captured.update(context)

        template_rendered.connect(record, _APP)
        try:
            r = c.get(f"/crm/lead/{phone}", follow_redirects=True)
        finally:
            template_rendered.disconnect(record, _APP)
        return r, captured

    def test_history_resolves_under_impersonation(self, seeded, stage_history):
        _, ctx = self._ctx(client(seeded["super"], impersonate=OX))
        assert ctx.get("stage_history"), "stage history empty while impersonating"
        assert all(h.tenant_id == OX for h in ctx["stage_history"])

    def test_history_matches_a_normal_admin(self, seeded, stage_history):
        _, admin_ctx = self._ctx(client(seeded["admin"]))
        _, imp_ctx = self._ctx(client(seeded["super"], impersonate=OX))
        assert [h.id for h in imp_ctx["stage_history"]] == \
               [h.id for h in admin_ctx["stage_history"]]

    def test_history_does_not_cross_tenants(self, seeded, stage_history):
        _, ctx = self._ctx(client(seeded["super"], impersonate=OTHER))
        assert all(h.tenant_id != OX for h in ctx.get("stage_history", []))


# ═══ crm_lead_send ═══════════════════════════════════════════════════════════

class TestLeadSend:

    def test_messages_are_attributed_to_the_impersonated_tenant(self, seeded):
        c = client(seeded["super"], impersonate=OX)
        c.post("/crm/lead/919700000001/send", data={"message": "hello"},
               follow_redirects=False)
        with _APP.app_context():
            rows = MessageLog.query.filter(MessageLog.tenant_id.is_(None)).count()
            assert rows == 0, "message logged with NO tenant"

    def test_cannot_send_across_the_impersonated_boundary(self, seeded):
        c = client(seeded["super"], impersonate=OX)
        c.post("/crm/lead/919700000077/send", data={"message": "x"},
               follow_redirects=False)
        with _APP.app_context():
            leaked = MessageLog.query.filter_by(tenant_id=OX).filter(
                MessageLog.phone == "919700000077").count()
            assert leaked == 0, "sent into another tenant's lead"


# ═══ crm_staff_performance_detail ════════════════════════════════════════════

class TestPerformanceDetail:
    URL = "/crm/staff-performance-detail"

    def _ctx(self, c):
        from flask import template_rendered
        captured = {}

        def record(sender, template, context, **extra):
            captured.update(context)

        template_rendered.connect(record, _APP)
        try:
            r = c.get(self.URL, follow_redirects=True)
        finally:
            template_rendered.disconnect(record, _APP)
        assert r.status_code == 200
        return captured

    def test_task_data_uses_the_impersonated_tenant(self, seeded):
        ctx = self._ctx(client(seeded["super"], impersonate=OX))
        assert "Anju" in ctx["metrics"]
        assert "Ravi" not in ctx["metrics"], "saw the other tenant's staff"

    def test_task_counts_are_the_impersonated_tenants(self, seeded):
        """Anju owns one OPEN task in OX; Ravi's belongs to OTHER."""
        m = self._ctx(client(seeded["super"], impersonate=OX))["metrics"]
        assert m["Anju"]["open_tasks"] == 1

    def test_this_route_is_a_CONSISTENCY_change_not_a_behavioural_fix(self):
        """HONEST RECORD, and the reason this test exists rather than a
        behavioural one.

        My H4 discovery classified crm_staff_performance_detail as H4-a
        because it passes _tid to get_all_tasks(). But get_all_tasks() hands
        that tenant straight to tenant_query(), whose SUPER_ADMIN branch
        returns BEFORE reading the argument — so an impersonating SUPER_ADMIN
        already got the right tasks under the old idiom.

        The change here is therefore consistency with the other three routes,
        NOT a defect repair. Reverting this one line alone changes no
        behaviour, which is why only the structural test catches it. Stating
        that is more useful than implying a fix that was not needed.
        """
        import ast
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        src = ast.unparse(next(n for n in ast.walk(tree)
                               if isinstance(n, ast.FunctionDef)
                               and n.name == "get_all_tasks"))
        assert "tenant_query(ConversationState, tenant_id)" in src
        assert "tenant_query(Task, tenant_id)" in src

    def test_switching_tenant_switches_the_staff_set(self, seeded):
        ox = self._ctx(client(seeded["super"], impersonate=OX))["metrics"]
        other = self._ctx(client(seeded["super"], impersonate=OTHER))["metrics"]
        assert "Anju" in ox and "Ravi" not in ox
        assert "Ravi" in other and "Anju" not in other

    def test_without_impersonation_the_route_is_not_reached(self, seeded):
        """Fail-closed happens EARLIER than _tid here: an existing guard
        redirects a non-impersonating SUPER_ADMIN to /crm/super/dashboard, so
        the screen never renders and there is no `metrics` context at all.

        Asserted as the redirect rather than as empty metrics — the first
        version of this test assumed the page rendered with an empty dict and
        failed with KeyError against correct behaviour.
        """
        r = client(seeded["super"]).get(self.URL, follow_redirects=False)
        assert r.status_code == 302
        assert "/crm/super/dashboard" in r.headers.get("Location", "")

    def test_without_impersonation_no_tenant_data_is_rendered(self, seeded):
        body = client(seeded["super"]).get(
            self.URL, follow_redirects=True).get_data(as_text=True)
        assert "Ravi" not in body


# ═══ structural ══════════════════════════════════════════════════════════════

class TestStructure:

    def _fn(self, name):
        import ast
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        return ast.unparse(next(n for n in ast.walk(tree)
                                if isinstance(n, ast.FunctionDef) and n.name == name))

    @pytest.mark.parametrize("name", ["crm_lead_update", "crm_lead_send",
                                      "crm_lead_detail",
                                      "crm_staff_performance_detail"])
    def test_route_uses_the_existing_helper(self, name):
        src = self._fn(name)
        assert "_actor_tenant_id()" in src
        assert "getattr(current_user, 'tenant_id'" not in src

    def test_no_new_abstraction_was_introduced(self):
        """The approved constraint: use the smallest EXISTING mechanism."""
        import ast
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        assert "_actor_tenant_id" in names
        for invented in ("_resolve_tenant", "_tenant_context", "get_tenant_ctx",
                         "_current_tenant"):
            assert invented not in names, f"new abstraction {invented} added"

    def test_h4b_scope_is_now_closed_too(self):
        """Was: "seven routes remain on the old idiom BY DESIGN — H4-b was not
        approved in this phase. Update the count when it lands." H4-b landed
        and migrated all seven."""
        import ast
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        GET = "getattr(current_user, 'tenant_id'"
        remaining = {n.name for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)
                     and GET in ast.unparse(n)
                     and n.name not in ("_actor_tenant_id", "check_billing_status")}
        # H4-b subsequently migrated all seven, so the set is now empty.
        # Kept (not deleted) because it still guards the H4-a four against
        # regression via the loop above.
        assert remaining == set(), remaining

    def test_h4c_services_were_not_modified(self):
        """log_audit / log_message / save_conversation_message keep their
        current (unguarded) signatures — H4-c was explicitly out of scope."""
        import subprocess
        out = subprocess.run(
            ["git", "status", "--porcelain", "--",
             "app/services/audit_service.py", "app/services/log_service.py"],
            cwd=ROOT, capture_output=True, text=True).stdout.strip()
        assert out == "", f"H4-c files modified: {out}"

    def test_only_admin_py_changed(self):
        import subprocess
        out = subprocess.run(["git", "status", "--porcelain", "--", "app/"],
                             cwd=ROOT, capture_output=True, text=True).stdout
        dirty = sorted(l.split()[-1] for l in out.splitlines()
                       if l.strip() and not l.endswith("screens.py"))
        assert dirty == ["app/routes/admin.py"], dirty

    def test_read_fk_flag_untouched(self):
        from app import flags
        before = os.environ.pop("STAFF_IDENTITY_READ_FK", None)
        try:
            assert flags.staff_identity_read_fk_enabled() is False
        finally:
            if before is not None:
                os.environ["STAFF_IDENTITY_READ_FK"] = before

    def test_no_schema_or_migration_change(self):
        import subprocess
        out = subprocess.run(["git", "status", "--porcelain", "--", "migrations/"],
                             cwd=ROOT, capture_output=True, text=True).stdout.strip()
        assert out == "", out
