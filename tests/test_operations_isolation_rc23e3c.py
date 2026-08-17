"""Phase RC2.3E-3C — /crm/operations shows STAFF only their own leads.

THE EXPOSURE
------------
calculate_operations() had NO actor parameter, so it could not filter by owner
even in principle. /crm/operations is guarded by check_auth(), which
authenticates without inspecting role, so under SESSION_ONLY every STAFF
member read the whole tenant. Three panels carry customer name AND phone:

    data_issues      name, phone, issue text
    admission_ready  name, phone, owning staff, score
    high_value_ops   name, phone, score

Measured in production before the fix: 38 of 90 customers reachable by any
staff actor, 25 of them owned by a colleague (Anju 10, Kiran 9, Nisha 4,
'kiran' 2). Live, not hypothetical.

WHY IT WAS NEVER A DECISION
---------------------------
The route's docstring still reads "Protected by ?key=ADMIN_KEY" — true under
ADMIN_KEY_ONLY, stale since production moved to SESSION_ONLY. The isolation
phases (10H-B1/10J) enumerate four protected routes and this is not one.

THE FIX
-------
Thread `actor` in and reuse owner_filter() — the SAME mechanism
_build_leads_query already uses. One ownership rule, not a second
implementation. Only the LEAD SET narrows; every issue classification,
threshold and downstream calculation is untouched, so an ADMIN's numbers are
unchanged.

`events` is deliberately NOT filtered: it is consulted only through phone_data
for leads already in the loop, so filtering leads is sufficient.

CONSEQUENCES ACCEPTED BY OPTION A
---------------------------------
  * "Unassigned lead" issues vanish for STAFF — they own none by definition.
    Correct: assigning leads is @admin_required, so a STAFF member could not
    act on them anyway. ADMIN still sees them.
  * kpis and staff_workload narrow for STAFF too, since both derive from the
    same lead set. Expected under Option A and asserted below rather than
    left as a surprise.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23e3c_ops.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc23e3c-admin-key")
os.environ.setdefault("SECRET_KEY", "rc23e3c-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc23e3c-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import json                                                            # noqa: E402
from werkzeug.security import generate_password_hash                    # noqa: E402
from app import create_app                                             # noqa: E402
from app.extensions import db                                          # noqa: E402
from app.models import Tenant, User, ConversationState, LeadEvent       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX = "t-ox"
OTHER = "t-other"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False

# Anju's customers
A1 = "919100000001"          # >=2 enquiries, no admission -> data_issues
A2 = "919100000002"          # score 60 + enquiry         -> admission_ready
# Kiran's customers — must never reach Anju
K1 = "919100000011"          # >=2 enquiries, no admission -> data_issues
K2 = "919100000012"          # score 85 + 2 enquiries      -> high_value_ops
K3 = "919100000013"          # score 60 + enquiry          -> admission_ready
# Tenant-level
U1 = "919100000021"          # unassigned                  -> data_issues
# Another tenant entirely
X1 = "919100000031"


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

        def mk(tenant, username, role="STAFF"):
            u = User(username=username, email=f"{username}.{tenant}@x.test",
                     password_hash=generate_password_hash("pw"), role=role,
                     tenant_id=tenant, is_active=True,
                     require_password_change=False)
            db.session.add(u); db.session.commit(); return u

        anju = mk(OX, "Anju"); kiran = mk(OX, "Kiran")
        admin = mk(OX, "admin_ox", role="ADMIN")
        other_admin = mk(OTHER, "admin_other", role="ADMIN")
        # A SUPER_ADMIN has no tenant of its own.
        su = mk(None, "platform_admin", role="SUPER_ADMIN")

        def lead(phone, tenant, staff, uid, name, score=10, admitted=False):
            db.session.add(ConversationState(
                phone=phone, tenant_id=tenant, name=name, lead_status="Lead",
                assigned_staff=staff, assigned_user_id=uid,
                lead_score=score, is_admitted=admitted))

        lead(A1, OX, "Anju", anju.id, "AnjuCustomerOne")
        lead(A2, OX, "Anju", anju.id, "AnjuCustomerTwo", score=65)
        lead(K1, OX, "Kiran", kiran.id, "KiranCustomerOne")
        lead(K2, OX, "Kiran", kiran.id, "KiranCustomerTwo", score=85)
        lead(K3, OX, "Kiran", kiran.id, "KiranCustomerThree", score=65)
        lead(U1, OX, None, None, "UnassignedCustomer")
        lead(X1, OTHER, "admin_other", other_admin.id, "OtherTenantCustomer")
        db.session.commit()

        def enq(phone, tenant, course):
            db.session.add(LeadEvent(
                tenant_id=tenant, phone=phone, event_type="COURSE_ENQUIRY",
                event_data=json.dumps({"course": course})))

        for ph, tn in ((A1, OX), (K1, OX), (K2, OX), (X1, OTHER)):
            enq(ph, tn, "Python"); enq(ph, tn, "Java")     # two enquiries
        enq(A2, OX, "Python"); enq(K3, OX, "Python")       # one enquiry
        db.session.commit()

        yield {"anju": anju.id, "kiran": kiran.id, "admin": admin.id,
               "super": su.id, "other_admin": other_admin.id}
        db.session.remove()


def client(uid, impersonate=None):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid); s["_fresh"] = True
        if impersonate is not None:
            s["impersonate_tenant_id"] = impersonate
    return c


def ops(uid, impersonate=None):
    """Render /crm/operations and return its template context."""
    from flask import template_rendered
    captured = {}

    def record(sender, template, context, **extra):
        captured.update(context)

    template_rendered.connect(record, _APP)
    try:
        r = client(uid, impersonate).get("/crm/operations", follow_redirects=True)
    finally:
        template_rendered.disconnect(record, _APP)
    assert r.status_code == 200
    return captured, r.get_data(as_text=True)


def phones(panel):
    return {row.get("phone") for row in panel}


def names(panel):
    return {row.get("name") for row in panel}


# ═══ STAFF sees only their own, per panel ════════════════════════════════════

class TestStaffIsolationPerPanel:

    def test_data_issues_only_own_leads(self, seeded):
        ctx, _ = ops(seeded["anju"])
        assert phones(ctx["data"]["data_issues"]) <= {A1, A2, "-"}
        assert K1 not in phones(ctx["data"]["data_issues"])

    def test_admission_ready_only_own_leads(self, seeded):
        ctx, _ = ops(seeded["anju"])
        got = phones(ctx["data"]["admission_ready"])
        assert A2 in got
        assert K3 not in got, "saw a colleague's admission-ready customer"

    def test_high_value_ops_only_own_leads(self, seeded):
        """Anju has no high-value lead; Kiran's K2 must not appear."""
        ctx, _ = ops(seeded["anju"])
        assert K2 not in phones(ctx["data"]["high_value_ops"])

    def test_kiran_sees_only_kirans(self, seeded):
        ctx, _ = ops(seeded["kiran"])
        allp = (phones(ctx["data"]["data_issues"]) |
                phones(ctx["data"]["admission_ready"]) |
                phones(ctx["data"]["high_value_ops"])) - {"-"}
        assert allp <= {K1, K2, K3}, allp
        assert A1 not in allp and A2 not in allp


# ═══ no colleague PII anywhere in the rendered page ══════════════════════════

class TestNoColleaguePII:

    @pytest.mark.parametrize("secret", [K1, K2, K3,
                                        "KiranCustomerOne", "KiranCustomerTwo",
                                        "KiranCustomerThree"])
    def test_absent_from_the_three_approved_panels(self, seeded, secret):
        """Scoped to the three panels this phase was approved to fix.

        NOT asserted against the whole page: a FOURTH PII source exists on it
        (intel.priority_queue) which this phase is not permitted to touch —
        see test_priority_queue_still_leaks below. Asserting page-wide here
        would fail for a reason outside the approved scope and hide which
        panels are actually fixed.
        """
        ctx, _ = ops(seeded["anju"])
        d = ctx["data"]
        blob = repr(d["data_issues"]) + repr(d["admission_ready"]) +             repr(d["high_value_ops"])
        assert secret not in blob, f"{secret} leaked through an approved panel"

    def test_priority_queue_is_now_filtered_too(self, seeded):
        """INVERTED by Phase RC2.3E-9 — deliberately not deleted.

        This asserted the OPPOSITE: that intel.priority_queue still leaked a
        colleague's customer. That was the honest record of a gap RC2.3E-3C
        could not close, because calculate_intelligence() had no actor
        parameter and is also called by crm_staff_dashboard.

        RC2.3E-9 closed it by threading an actor and filtering MODULE 4 ONLY —
        not the shared `leads` collection — so crm_staff_dashboard's
        leaderboard and rank are untouched. The assertion is therefore
        reversed, not removed: this file's job is still to pin what a STAFF
        actor may see on /crm/operations, and the answer for this panel has
        changed from "everything" to "only their own".

        The gap that REMAINS is the automation panels
        (unassigned_hot / stalled_admissions / recovery_queue /
        recommendations, from calculate_automation_intelligence). Those are
        pinned by tests/test_priority_queue_isolation_rc23e9.py::
        TestKnownRemainingExposure, not here.
        """
        ctx, _ = ops(seeded["anju"])
        pq = repr(ctx.get("intel", {}).get("priority_queue", []))
        assert K2 not in pq and "KiranCustomerTwo" not in pq, \
            "priority_queue leaked a colleague's lead — RC2.3E-9 regressed"

    def test_other_tenant_never_appears(self, seeded):
        _, html = ops(seeded["anju"])
        assert X1 not in html and "OtherTenantCustomer" not in html


# ═══ the "Unassigned lead" consequence, stated explicitly ════════════════════

class TestUnassignedConsequence:

    def test_staff_no_longer_sees_unassigned_leads(self, seeded):
        """Accepted under Option A: STAFF own none, and assigning is
        @admin_required, so they could not act on them anyway."""
        ctx, html = ops(seeded["anju"])
        assert U1 not in phones(ctx["data"]["data_issues"])
        assert "UnassignedCustomer" not in html

    def test_admin_still_sees_unassigned_leads(self, seeded):
        ctx, _ = ops(seeded["admin"])
        assert U1 in phones(ctx["data"]["data_issues"])

    def test_no_new_staff_assignment_capability(self, seeded):
        """This phase must not hand STAFF an assign button."""
        r = client(seeded["anju"]).post(
            "/crm/leads/unassigned/assign",
            data={"phone": U1, "target_staff": "Anju"}, follow_redirects=False)
        assert r.status_code == 403
        with _APP.app_context():
            assert ConversationState.query.filter_by(
                phone=U1).first().assigned_staff is None


# ═══ ADMIN keeps the tenant-wide view, unchanged ═════════════════════════════

class TestAdminUnchanged:

    def test_admin_sees_every_staff_member(self, seeded):
        ctx, html = ops(seeded["admin"])
        allp = (phones(ctx["data"]["data_issues"]) |
                phones(ctx["data"]["admission_ready"]) |
                phones(ctx["data"]["high_value_ops"]))
        for ph in (A1, A2, K1, K2, K3, U1):
            assert ph in allp, ph

    def test_admin_calculations_are_unchanged(self, seeded):
        """The exact figures an ADMIN saw before the change."""
        ctx, _ = ops(seeded["admin"])
        d = ctx["data"]
        # K2 (score 85, 2 enquiries, not admitted) satisfies BOTH
        # admission_ready (score>=60, >=1 enquiry) and high_value_ops
        # (score>=80, >=2 enquiries). My first expectation of 2 was wrong;
        # the code is right.
        assert len(d["data_issues"]) == 4      # A1, K1, K2, U1
        assert len(d["admission_ready"]) == 3  # A2, K3, K2
        assert len(d["high_value_ops"]) == 1   # K2
        assert d["kpis"]["total_data_issues"] == 4

    def test_admin_never_leaks_another_tenant(self, seeded):
        _, html = ops(seeded["admin"])
        assert X1 not in html


# ═══ SUPER_ADMIN boundary ════════════════════════════════════════════════════

class TestSuperAdmin:

    def test_impersonating_gets_that_tenants_dataset(self, seeded):
        ctx, _ = ops(seeded["super"], impersonate=OX)
        allp = (phones(ctx["data"]["data_issues"]) |
                phones(ctx["data"]["admission_ready"]))
        assert A1 in allp and K1 in allp, "impersonation lost the tenant view"

    def test_impersonation_switches_tenant(self, seeded):
        ctx, html = ops(seeded["super"], impersonate=OTHER)
        assert X1 in phones(ctx["data"]["data_issues"])
        assert A1 not in html and K1 not in html

    def test_non_impersonating_super_admin_is_fail_closed(self, seeded):
        """No tenant context => no tenant data. Never everyone's."""
        r = client(seeded["super"]).get("/crm/operations", follow_redirects=True)
        html = r.get_data(as_text=True)
        for ph in (A1, K1, X1, U1):
            assert ph not in html, ph


# ═══ existing isolation invariants intact ════════════════════════════════════

class TestExistingInvariantsIntact:

    def test_lead_list_isolation_unchanged(self, seeded):
        """_build_leads_query must still filter /crm/leads for STAFF."""
        html = client(seeded["anju"]).get("/crm/leads").get_data(as_text=True)
        assert A1 in html
        assert K1 not in html

    def test_lead_detail_isolation_unchanged(self, seeded):
        """crm_lead_detail was explicitly out of scope — still protected."""
        r = client(seeded["anju"]).get(f"/crm/lead/{K1}", follow_redirects=False)
        assert r.status_code in (403, 302)

    def test_operations_is_still_read_only(self, seeded):
        """No mutation surface was added."""
        tpl = os.path.join(ROOT, "templates", "crm_operations.html")
        src = open(tpl, encoding="utf-8").read()
        assert "<form" not in src.lower()
        assert "fetch(" not in src


# ═══ structural ══════════════════════════════════════════════════════════════

class TestStructure:

    def _fn(self, name):
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        m = ast.parse(ast.unparse(fn)).body[0]
        if (m.body and isinstance(m.body[0], ast.Expr)
                and isinstance(m.body[0].value, ast.Constant)):
            m.body.pop(0)
        return ast.unparse(m)

    def test_uses_the_existing_owner_filter(self):
        """No second ownership implementation."""
        src = self._fn("calculate_operations")
        assert "staff_identity_service.owner_filter" in src
        assert "assigned_staff ==" not in src

    def test_actor_is_threaded_from_the_route(self):
        assert "calculate_operations(actor=get_current_actor())" in \
            self._fn("crm_operations")

    def test_staff_test_matches_build_leads_query(self):
        """Same predicate shape as the approved mechanism."""
        ops_src = self._fn("calculate_operations")
        assert "actor.get('source') == 'SESSION'" in ops_src
        assert "actor.get('role') == 'STAFF'" in ops_src

    def test_events_are_not_filtered(self):
        """Deliberate: events feed phone_data for leads already in the loop."""
        src = self._fn("calculate_operations")
        assert "events = tenant_query(LeadEvent, tenant_id).all()" in src

    def test_build_leads_query_untouched(self):
        src = self._fn("_build_leads_query")
        assert "owner_filter" in src

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
        """RC2.3E-3C shipped no migration or schema change.

        Phase RC2.4.2: converted from `git status --porcelain -- migrations/`
        to a COMMIT-scoped check. The worktree form asserted that NOBODY has a
        migration in progress, which is not this phase's business and which
        failed the moment RC2.4.2 added an authorised one. The invariant is
        unchanged and still enforced: RC2.3E-3C's OWN committed changeset must
        contain no migrations/ path.
        """
        files = self._phase_commit_files("tests/test_operations_isolation_rc23e3c.py")
        if files is None:
            pytest.skip("RC2.3E-3C is not committed yet")
        migrations = [f for f in files if f.startswith("migrations/")]
        assert migrations == [], (
            f"RC2.3E-3C committed a migration: {migrations}")
