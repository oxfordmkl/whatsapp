"""Phase H4-b — the remaining seven routes resolve their tenant by one rule.

H4-a migrated the four routes whose _tid feeds a consumer other than
tenant_query(). These seven were classified as CONSISTENCY-ONLY on the
grounds that their _tid feeds only tenant_query(), whose SUPER_ADMIN branch
returns before reading the argument.

ONE OF THE SEVEN WAS MIS-CLASSIFIED
-----------------------------------
crm_course_admissions is a POST that calls

    log_lead_event(tenant_id=_tid, ...)

That is NOT tenant_query. log_lead_event does not guard a falsy tenant — it
calls resolve_tenant_id(), whose fallback is PRIMARY_TENANT_ID. So under the
legacy idiom an impersonating SUPER_ADMIN recording an admission for tenant X
would have written the COURSE_ADMISSION event into the PRIMARY tenant: a
CROSS-TENANT WRITE, the same class as the TD-P0-1 mis-filing incident that
the resolve_tenant_id docstring exists to describe.

My H4 discovery missed it because the keyword-argument form
`log_lead_event(tenant_id=_tid, ...)` did not match the call-scanning pattern
I used. Production evidence: zero mis-filed rows — every lead_event already
belongs to the primary tenant, so the fallback happened to land in the right
place. Latent, not realised. It is tested behaviourally below rather than
being folded into the consistency claim.

THE OTHER SIX
-------------
Genuinely consistency-only. Their tests assert the invariant holds and that
behaviour is IDENTICAL before and after — which is the honest claim, and is
why reverting one of them changes nothing observable.

Import isolation follows test_tenant_resolution_h4a.py.
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

_DB = os.path.join(tempfile.gettempdir(), "phase_h4b_tenant.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "h4b-admin-key")
os.environ.setdefault("SECRET_KEY", "h4b-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "h4b-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
# The PRIMARY tenant is deliberately NOT the tenant under test: that is what
# makes a mis-filed event visible instead of accidentally landing correctly,
# which is exactly why production has not yet exhibited the defect.
os.environ["PRIMARY_TENANT_ID"] = "t-primary"
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState, LeadEvent       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN = os.path.join(ROOT, "app", "routes", "admin.py")
OX = "t-ox"
OTHER = "t-other"
PRIMARY = "t-primary"

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False
_APP.config["PRIMARY_TENANT_ID"] = PRIMARY

H4B = ["campaigns", "crm_course_admissions", "crm_operations",
       "crm_staff_allocation", "crm_staff_allocation_check",
       "crm_staff_allocation_detail", "crm_unassigned_leads"]

CONSISTENCY_ONLY = [r for r in H4B if r != "crm_course_admissions"]


def _mk(tenant, username, role="STAFF"):
    u = User(username=username,
             email=f"{username}.{tenant or 'plat'}@x.test".replace(" ", "_"),
             password_hash=generate_password_hash("pw"), role=role,
             tenant_id=tenant, is_active=True, require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture()
def seeded():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((OX, "Oxford"), (OTHER, "Other"), (PRIMARY, "Primary")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        ids = {
            "anju": _mk(OX, "Anju").id,
            "admin": _mk(OX, "admin_ox", role="ADMIN").id,
            "other_admin": _mk(OTHER, "admin_other", role="ADMIN").id,
            "super": _mk(None, "platform_admin", role="SUPER_ADMIN").id,
        }
        db.session.add(ConversationState(
            phone="919800000001", tenant_id=OX, name="Oxford Lead",
            lead_status="Lead", assigned_staff="Anju",
            assigned_user_id=ids["anju"]))
        db.session.add(ConversationState(
            phone="919800000077", tenant_id=OTHER, name="Other Lead",
            lead_status="Lead", assigned_staff=None, assigned_user_id=None))
        db.session.commit()
        yield ids
        db.session.remove()


@pytest.fixture()
def enquiry(seeded):
    """A COURSE_ENQUIRY per tenant — the route only accepts an admission for
    a course the lead has already enquired about."""
    with _APP.app_context():
        for tid, phone in ((OX, "919800000001"), (OTHER, "919800000077")):
            db.session.add(LeadEvent(
                tenant_id=tid, phone=phone, event_type="COURSE_ENQUIRY",
                event_data=json.dumps({"course": "Python Basics"})))
        db.session.commit()
    yield


def client(uid, impersonate=None):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
        if impersonate is not None:
            s["impersonate_tenant_id"] = impersonate
    return c


# ═══ the mis-classified route: a real cross-tenant WRITE ═════════════════════

class TestCourseAdmissionsWrite:
    """The one route in this batch with observable behaviour.

    Reaching the write took two corrections. The form field is
    `admitted_courses` (a getlist), not `course`; and a submitted course is
    only accepted if it already appears in the lead's COURSE_ENQUIRY history —
    a deliberate anti-injection gate. My first version posted the wrong field
    with no enquiry seeded, so the route wrote NOTHING and every assertion
    below passed vacuously against the reverted code.
    """

    URL = "/crm/course-admissions/919800000001"
    COURSE = "Python Basics"

    def _post(self, c, phone="919800000001"):
        return c.post(f"/crm/course-admissions/{phone}",
                      data={"admitted_courses": self.COURSE},
                      follow_redirects=False)

    def test_the_write_actually_happens(self, seeded, enquiry):
        """Guards the guard: if this stops writing, every other test in this
        class becomes vacuous again."""
        self._post(client(seeded["admin"]))
        with _APP.app_context():
            n = LeadEvent.query.filter_by(phone="919800000001",
                                          event_type="COURSE_ADMISSION").count()
            assert n == 1, "the route wrote no COURSE_ADMISSION event"

    def test_event_is_filed_in_the_impersonated_tenant(self, seeded, enquiry):
        """THE defect. With _tid None, log_lead_event fell back to
        PRIMARY_TENANT_ID and filed the event in the wrong tenant."""
        self._post(client(seeded["super"], impersonate=OX))
        with _APP.app_context():
            evs = LeadEvent.query.filter_by(
                phone="919800000001", event_type="COURSE_ADMISSION").all()
            assert evs, "no COURSE_ADMISSION written while impersonating"
            assert all(e.tenant_id == OX for e in evs), \
                [(e.event_type, e.tenant_id) for e in evs]

    def test_nothing_is_filed_into_the_primary_tenant(self, seeded, enquiry):
        """The mis-filing signature: an event in PRIMARY for a lead that
        lives in OX."""
        self._post(client(seeded["super"], impersonate=OX))
        with _APP.app_context():
            stray = LeadEvent.query.filter_by(tenant_id=PRIMARY).count()
            assert stray == 0, "event mis-filed into PRIMARY_TENANT_ID"

    def test_normal_admin_unchanged(self, seeded, enquiry):
        self._post(client(seeded["admin"]))
        with _APP.app_context():
            assert LeadEvent.query.filter_by(tenant_id=PRIMARY).count() == 0
            for e in LeadEvent.query.filter_by(phone="919800000001").all():
                assert e.tenant_id == OX

    def test_super_admin_without_impersonation_writes_nothing_stray(self, seeded, enquiry):
        """No tenant context: the route must not fall back to PRIMARY."""
        self._post(client(seeded["super"]))
        with _APP.app_context():
            assert LeadEvent.query.filter_by(tenant_id=PRIMARY).count() == 0

    def test_cannot_write_across_the_impersonated_boundary(self, seeded, enquiry):
        c = client(seeded["super"], impersonate=OX)
        self._post(c, phone="919800000077")
        with _APP.app_context():
            for e in LeadEvent.query.filter_by(phone="919800000077").all():
                assert e.tenant_id != OX, "wrote another tenant's lead as OX"


# ═══ the six consistency-only routes ═════════════════════════════════════════

class TestConsistencyOnlyRoutes:
    """These have no observable behavioural change — that IS the claim, and
    these tests assert it rather than implying a fix."""

    URLS = {
        "campaigns": "/crm/campaigns",
        "crm_operations": "/crm/operations",
        "crm_staff_allocation": "/crm/staff-allocation",
        # Read from the route decorator, not guessed: my first attempt used
        # /crm/staff-allocation-check and 404'd against correct code.
        "crm_staff_allocation_check":
            "/crm/staff-allocation/check-deactivation/Anju",
        "crm_staff_allocation_detail": "/crm/staff-allocation/Anju",
        "crm_unassigned_leads": "/crm/leads/unassigned",
    }

    @pytest.mark.parametrize("route", CONSISTENCY_ONLY)
    def test_normal_admin_still_renders(self, seeded, route):
        r = client(seeded["admin"]).get(self.URLS[route], follow_redirects=True)
        assert r.status_code == 200, route
        assert "Traceback" not in r.get_data(as_text=True)

    @pytest.mark.parametrize("route", CONSISTENCY_ONLY)
    def test_impersonating_super_admin_renders(self, seeded, route):
        r = client(seeded["super"], impersonate=OX).get(
            self.URLS[route], follow_redirects=True)
        assert r.status_code == 200, route
        assert "Traceback" not in r.get_data(as_text=True)

    @pytest.mark.parametrize("route", CONSISTENCY_ONLY)
    def test_no_cross_tenant_leak(self, seeded, route):
        body = client(seeded["super"], impersonate=OX).get(
            self.URLS[route], follow_redirects=True).get_data(as_text=True)
        assert "Other Lead" not in body, route
        assert "919800000077" not in body, route


# ═══ the resolution invariant ════════════════════════════════════════════════

class TestInvariant:

    def test_impersonation_switches_the_unassigned_queue(self, seeded):
        """OTHER has one unassigned lead; OX has none."""
        ox = client(seeded["super"], impersonate=OX).get(
            "/crm/leads/unassigned", follow_redirects=True).get_data(as_text=True)
        other = client(seeded["super"], impersonate=OTHER).get(
            "/crm/leads/unassigned", follow_redirects=True).get_data(as_text=True)
        assert "919800000077" not in ox
        assert "919800000077" in other

    def test_removing_impersonation_stops_showing_tenant_data(self, seeded):
        body = client(seeded["super"]).get(
            "/crm/leads/unassigned", follow_redirects=True).get_data(as_text=True)
        assert "919800000077" not in body

    def test_other_tenant_admin_is_unaffected(self, seeded):
        body = client(seeded["other_admin"]).get(
            "/crm/leads/unassigned", follow_redirects=True).get_data(as_text=True)
        assert "919800000001" not in body


# ═══ structural / tripwires ══════════════════════════════════════════════════

class TestStructure:

    def _tree(self):
        with open(ADMIN, encoding="utf-8") as fh:
            return ast.parse(fh.read())

    def _fn(self, name):
        return ast.unparse(next(n for n in ast.walk(self._tree())
                                if isinstance(n, ast.FunctionDef) and n.name == name))

    @pytest.mark.parametrize("route", H4B)
    def test_route_uses_the_existing_helper(self, route):
        src = self._fn(route)
        assert "_actor_tenant_id()" in src
        assert "getattr(current_user, 'tenant_id'" not in src

    def test_h4_is_now_fully_closed(self):
        """THE POINT OF THIS PHASE. Only _actor_tenant_id itself (which
        defines the idiom) and check_billing_status (which has its own
        three-way resolution, never an H4 site) may still contain it."""
        GET = "getattr(current_user, 'tenant_id'"
        remaining = {n.name for n in ast.walk(self._tree())
                     if isinstance(n, ast.FunctionDef) and GET in ast.unparse(n)}
        assert remaining == {"_actor_tenant_id", "check_billing_status"}, \
            f"H4 sites remain: {sorted(remaining - {'_actor_tenant_id', 'check_billing_status'})}"

    def test_helper_itself_is_unchanged(self):
        """H4-b must not touch the mechanism it relies on."""
        src = self._fn("_actor_tenant_id")
        assert "session.get('impersonate_tenant_id')" in src
        assert "if getattr(current_user, 'role', None) == 'SUPER_ADMIN'" in src

    def test_tenant_query_is_unchanged(self):
        """Out of scope, and the reason six of these seven are cosmetic."""
        src = self._fn("tenant_query")
        assert "impersonate_tenant_id" in src
        assert "return model.query.filter(false())" in src

    def test_h4c_is_still_open(self):
        """HONEST RECORD: log_lead_event still does NOT guard a falsy tenant —
        it falls back to PRIMARY_TENANT_ID via resolve_tenant_id(). H4-b fixes
        the CALLER; the callee remains a latent hole for any future caller
        that forgets. That is H4-c, deliberately not done here. This test
        fails when the guard is added, which is when H4-c lands."""
        with open(os.path.join(ROOT, "app", "services", "log_service.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "log_lead_event")
        src = ast.unparse(fn)
        assert "resolve_tenant_id(tenant_id)" in src
        assert "if not tenant_id" not in src, \
            "log_lead_event now guards — H4-c has landed; update this test"

    def test_out_of_scope_files_untouched_by_THIS_phase(self):
        """H4-b shipped touching only admin.py under app/.

        Asserted against H4-b's own commit rather than `git status`: H4-c
        subsequently edited log_service.py under separate approval, and a
        worktree check would blame H4-b for it.
        """
        import subprocess
        sha = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%H", "-1", "--",
             "tests/test_tenant_resolution_h4b.py"],
            cwd=ROOT, capture_output=True, text=True).stdout.strip()
        if not sha:
            pytest.skip("H4-b is not committed yet")
        files = subprocess.run(
            ["git", "show", "--name-only", "--format=", sha],
            cwd=ROOT, capture_output=True, text=True).stdout.split()
        prod = sorted(f for f in files if f.startswith("app/"))
        assert prod == ["app/routes/admin.py"], prod

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
        """H4-b shipped no migration or schema change.

        Phase RC2.4.2: converted from `git status --porcelain -- migrations/`
        to a COMMIT-scoped check. The worktree form asserted that NOBODY has a
        migration in progress, which is not this phase's business and which
        failed the moment RC2.4.2 added an authorised one. The invariant is
        unchanged and still enforced: H4-b's OWN committed changeset must
        contain no migrations/ path.
        """
        files = self._phase_commit_files("tests/test_tenant_resolution_h4b.py")
        if files is None:
            pytest.skip("H4-b is not committed yet")
        migrations = [f for f in files if f.startswith("migrations/")]
        assert migrations == [], (
            f"H4-b committed a migration: {migrations}")

    def test_read_fk_flag_untouched(self):
        from app import flags
        before = os.environ.pop("STAFF_IDENTITY_READ_FK", None)
        try:
            assert flags.staff_identity_read_fk_enabled() is False
        finally:
            if before is not None:
                os.environ["STAFF_IDENTITY_READ_FK"] = before
