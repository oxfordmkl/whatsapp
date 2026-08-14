"""Phase RC2.3E-9 — STAFF see only their own leads in the priority queue.

THE DEFECT
----------
/crm/operations renders intel.priority_queue from calculate_intelligence(),
which had no actor parameter and therefore could not filter by owner even in
principle. RC2.3E-3C isolated the three calculate_operations() panels on the
same page and had to leave this one open, because this helper is also called
by crm_staff_dashboard.

Each row carries customer NAME and PHONE, the owning staff member's name, and
an <a href="/crm/lead/{{ item.phone }}"> link. The lead-detail route and its
update/stage/send POSTs sit behind check_auth() alone, so the exposure was not
only disclosure: it was a working click-through to a colleague's customer.
Those route guards are a separate RBAC question and are NOT touched here — but
it is why test_every_phone_is_owned below asserts on the PHONE SET rather than
on rendered text. Redacting the visible name while leaving the phone in the
href would look fixed and would not be.

WHY THE FILTER IS SCOPED TO MODULE 4
------------------------------------
Filtering the shared `leads` collection would change leaderboard, sla,
activity_feed and workload_snapshot for BOTH callers. crm_staff_dashboard
derives the viewer's RANK from that leaderboard, so a filtered set would make
every staff member rank #1. It never renders priority_queue, so confining the
filter to Module 4 leaves that screen provably untouched — pinned by
TestSharedHelperUnaffected below, which is the regression guard for the wrong
implementation.

Import isolation follows test_operations_isolation_rc23e3c.py.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23e9_pq.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc23e9-admin-key")
os.environ.setdefault("SECRET_KEY", "rc23e9-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc23e9-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from flask import template_rendered                                      # noqa: E402
from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState                  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_PY = os.path.join(ROOT, "app", "routes", "admin.py")

OX = "t-ox"
OTHER = "t-other"
URL = "/crm/operations"
HOT = 80                       # INTELLIGENCE_CONSTANTS["THRESHOLD_HOT"]

# Phones are distinctive so a substring search cannot pass by accident.
A_HOT = "919000000101"         # Anju's, qualifies
K_HOT = "919000000201"         # Kiran's, qualifies
K_HOT2 = "919000000202"        # Kiran's, qualifies
ADM_HOT = "919000000301"       # the ADMIN's own, qualifies
UNOWNED = "919000000401"       # nobody's, qualifies
COLD = "919000000501"          # Kiran's, score below threshold
ADMITTED = "919000000601"      # Kiran's, high score but ADMITTED
RIVAL = "919000000701"         # another tenant's

# Two rows that a NAIVE ownership rule gets wrong. Both mirror production:
#   - assigned_staff='kiran' beside a username of 'Kiran' (production holds
#     both spellings; the RC2.3E-1 Batch 3 undercount was exactly this)
#   - a member whose display_label differs from username entirely — production
#     has NIBU01 labelled 'nibu s'
# owner_filter() resolves both (lower(trim(col)) == display_label, or the FK
# under the flag). `assigned_staff == user.username` resolves neither, which is
# what mutation M6 substitutes.
K_LOWER = "919000000203"       # Kiran's, but stored lowercase
N_HOT = "919000000801"         # owned by NIBU01, whose label is 'nibu s'

_APP = create_app()
_APP.config["WTF_CSRF_ENABLED"] = False


def _mk_user(tenant, username, role="STAFF", display=None):
    u = User(username=username, display_name=display,
             email=f"{username}.{tenant}@x.test",
             password_hash=generate_password_hash("pw"), role=role,
             tenant_id=tenant, is_active=True, require_password_change=False)
    db.session.add(u)
    db.session.commit()
    return u


def _mk_lead(tenant, phone, name, staff, uid, score, admitted=False):
    db.session.add(ConversationState(
        phone=phone, tenant_id=tenant, name=name, lead_status="Lead",
        assigned_staff=staff, assigned_user_id=uid, lead_score=score,
        is_admitted=admitted))
    db.session.commit()


@pytest.fixture()
def seeded():
    """Seeds, then RELEASES the app context before yielding — flask_login
    caches the resolved user on flask.g, which is bound to the APPLICATION
    context, so a context held across test_client requests leaks identity
    between them (the 14B.1 fixture bug)."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((OX, "Oxford"), (OTHER, "Rival")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        anju = _mk_user(OX, "Anju", display="Anju")
        nibu01 = _mk_user(OX, "NIBU01", display="nibu s")
        kiran = _mk_user(OX, "Kiran", display="Kiran")
        admin = _mk_user(OX, "admin_ox", role="ADMIN")
        rival = _mk_user(OTHER, "RivalStaff")
        plat = _mk_user(OX, "platform_op", role="SUPER_ADMIN")
        plat.tenant_id = None
        db.session.commit()

        _mk_lead(OX, A_HOT, "AnjuCustomerHot", "Anju", anju.id, 95)
        _mk_lead(OX, K_HOT, "KiranCustomerHot", "Kiran", kiran.id, 90)
        _mk_lead(OX, K_HOT2, "KiranCustomerTwo", "Kiran", kiran.id, 85)
        _mk_lead(OX, ADM_HOT, "AdminCustomerHot", "admin_ox", admin.id, 88)
        _mk_lead(OX, UNOWNED, "UnownedCustomer", None, None, 92)
        _mk_lead(OX, COLD, "KiranCold", "Kiran", kiran.id, 10)
        _mk_lead(OX, ADMITTED, "KiranAdmitted", "Kiran", kiran.id, 99,
                 admitted=True)
        # Case-variant spelling, and a member labelled by display_name.
        _mk_lead(OX, K_LOWER, "KiranCustomerLower", "kiran", kiran.id, 82)
        _mk_lead(OX, N_HOT, "NibuCustomerHot", "nibu s", nibu01.id, 84)
        _mk_lead(OTHER, RIVAL, "RivalCustomer", "RivalStaff", rival.id, 97)

        ids = {"anju": anju.id, "kiran": kiran.id, "admin": admin.id,
               "rival": rival.id, "plat": plat.id, "nibu01": nibu01.id}
    yield ids
    with _APP.app_context():
        db.session.remove()


def client(uid, impersonate=None):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
        if impersonate:
            s["impersonate_tenant_id"] = impersonate
    return c


def render(uid, impersonate=None):
    """The rendered page AND the template context, so assertions can read the
    real priority_queue rather than pattern-matching HTML."""
    seen = []

    def rec(sender, template, context, **extra):
        seen.append(context)

    template_rendered.connect(rec, _APP)
    try:
        r = client(uid, impersonate).get(URL, follow_redirects=False)
        ctx = seen[-1] if seen else {}
        body = r.get_data(as_text=True) if r.status_code == 200 else ""
        return r, ctx, body
    finally:
        template_rendered.disconnect(rec, _APP)


def pq(uid, impersonate=None):
    _, ctx, _ = render(uid, impersonate)
    return (ctx.get("intel") or {}).get("priority_queue", [])


def phones(queue):
    return {row["phone"] for row in queue}


def pq_panel(body):
    """Just the Priority Opportunity Queue table.

    Asserting page-wide would be wrong: /crm/operations renders FOUR other
    panels that also emit /crm/lead/<phone> links, fed by
    calculate_automation_intelligence() — unassigned_hot, stalled_admissions,
    recovery_queue and recommendations. Those are OUT OF SCOPE for this phase
    and still unfiltered; see TestKnownRemainingExposure below, which pins them
    so they cannot be forgotten. This slice keeps the assertions honest about
    what this phase actually fixed.
    """
    start = body.find("Priority Opportunity Queue")
    assert start != -1, "priority queue panel not found in the page"
    end = body.find("</table>", start)
    return body[start:end if end != -1 else len(body)]


def _fn_src(name):
    src = open(ADMIN_PY, encoding="utf-8").read()
    node = [n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef) and n.name == name][0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node = ast.Module(body=node.body[1:], type_ignores=[])
    return ast.unparse(node)


# ═══ STAFF isolation ═════════════════════════════════════════════════════════

class TestStaffIsolation:
    def test_staff_sees_only_own_leads(self, seeded):
        assert phones(pq(seeded["kiran"])) == {K_HOT, K_HOT2, K_LOWER}

    def test_case_variant_ownership_is_resolved(self, seeded):
        """'kiran' vs username 'Kiran'. owner_filter() compares
        lower(trim(col)) to the display label; a naive `== username` misses
        this row, and production holds exactly this pair."""
        assert K_LOWER in phones(pq(seeded["kiran"]))

    def test_display_name_labelled_member_sees_own_lead(self, seeded):
        """NIBU01's label is 'nibu s', nothing like its username. Ownership
        must resolve through display_label(), not username."""
        assert phones(pq(seeded["nibu01"])) == {N_HOT}

    def test_display_name_labelled_lead_is_not_leaked_to_others(self, seeded):
        assert N_HOT not in phones(pq(seeded["kiran"]))

    def test_colleague_customer_name_absent(self, seeded):
        _, ctx, _ = render(seeded["kiran"])
        blob = repr((ctx.get("intel") or {}).get("priority_queue", []))
        assert "AnjuCustomerHot" not in blob
        assert "AdminCustomerHot" not in blob
        assert "UnownedCustomer" not in blob

    def test_colleague_phone_absent(self, seeded):
        p = phones(pq(seeded["kiran"]))
        assert A_HOT not in p and ADM_HOT not in p and UNOWNED not in p

    def test_colleague_owner_name_absent(self, seeded):
        owners = {row["staff"] for row in pq(seeded["kiran"])}
        assert "Anju" not in owners
        assert all(o.strip().lower() == "kiran" for o in owners), owners

    def test_colleague_lead_link_absent_from_the_queue(self, seeded):
        """The href is the escalation path — /crm/lead/<phone> reaches
        update/stage/send, which are behind check_auth() alone. Scoped to the
        queue's own panel: the page's automation panels are out of scope and
        still leak (TestKnownRemainingExposure)."""
        _, _, body = render(seeded["kiran"])
        panel = pq_panel(body)
        assert f"/crm/lead/{A_HOT}" not in panel
        assert f"/crm/lead/{ADM_HOT}" not in panel
        assert f"/crm/lead/{UNOWNED}" not in panel

    def test_own_qualifying_lead_still_visible(self, seeded):
        _, _, body = render(seeded["kiran"])
        p = phones(pq(seeded["kiran"]))
        assert K_HOT in p and K_HOT2 in p
        assert f"/crm/lead/{K_HOT}" in pq_panel(body)

    def test_other_staff_sees_only_their_own(self, seeded):
        assert phones(pq(seeded["anju"])) == {A_HOT}

    def test_unowned_lead_is_not_given_to_staff(self, seeded):
        """No owner means no ownership match; it must not fall through."""
        assert UNOWNED not in phones(pq(seeded["kiran"]))
        assert UNOWNED not in phones(pq(seeded["anju"]))

    def test_every_phone_is_owned(self, seeded):
        """THE security assertion. Not 'is the name hidden' but 'is the phone
        set a subset of what this user owns' — because the phone is what the
        link carries."""
        with _APP.app_context():
            owned = {r[0] for r in db.session.query(ConversationState.phone)
                     .filter(ConversationState.tenant_id == OX,
                             ConversationState.assigned_user_id
                             == seeded["kiran"]).all()}
        assert phones(pq(seeded["kiran"])) <= owned


# ═══ ADMIN ═══════════════════════════════════════════════════════════════════

class TestAdminUnfiltered:
    def test_admin_gets_tenant_wide_queue(self, seeded):
        assert phones(pq(seeded["admin"])) == {A_HOT, K_HOT, K_HOT2,
                                               ADM_HOT, UNOWNED,
                                               K_LOWER, N_HOT}

    def test_admin_own_lead_visible(self, seeded):
        assert ADM_HOT in phones(pq(seeded["admin"]))

    def test_staff_owned_leads_visible_to_admin(self, seeded):
        p = phones(pq(seeded["admin"]))
        assert K_HOT in p and K_HOT2 in p and A_HOT in p

    def test_admin_page_still_links_every_lead(self, seeded):
        _, _, body = render(seeded["admin"])
        for ph in (A_HOT, K_HOT, ADM_HOT):
            assert f"/crm/lead/{ph}" in body


# ═══ SUPER_ADMIN ═════════════════════════════════════════════════════════════

class TestSuperAdmin:
    def test_impersonating_gets_the_impersonated_tenants_queue(self, seeded):
        p = phones(pq(seeded["plat"], impersonate=OX))
        assert p == {A_HOT, K_HOT, K_HOT2, ADM_HOT, UNOWNED, K_LOWER, N_HOT}

    def test_tenant_boundary_holds_while_impersonating(self, seeded):
        assert RIVAL not in phones(pq(seeded["plat"], impersonate=OX))

    def test_impersonating_the_other_tenant_sees_only_its_own(self, seeded):
        p = phones(pq(seeded["plat"], impersonate=OTHER))
        assert p == {RIVAL}
        assert K_HOT not in p

    def test_non_impersonating_super_admin_is_fail_closed(self, seeded):
        """admin_security_guard redirects a non-impersonating SUPER_ADMIN off
        /crm/ routes entirely — it never reaches the queue."""
        r = client(seeded["plat"]).get(URL, follow_redirects=False)
        assert r.status_code in (302, 403), r.status_code

    def test_super_admin_is_not_treated_as_staff(self, seeded):
        """Its role is SUPER_ADMIN, so the STAFF branch must not engage."""
        assert len(pq(seeded["plat"], impersonate=OX)) == 7


# ═══ cross-tenant ════════════════════════════════════════════════════════════

class TestTenantIsolation:
    def test_staff_never_sees_another_tenant(self, seeded):
        for uid in ("kiran", "anju", "admin"):
            assert RIVAL not in phones(pq(seeded[uid]))

    def test_rival_staff_sees_only_their_tenant(self, seeded):
        assert phones(pq(seeded["rival"])) == {RIVAL}


# ═══ shared helper must stay untouched ═══════════════════════════════════════

class TestSharedHelperUnaffected:
    """The regression guard for the WRONG implementation: filtering the shared
    `leads` collection instead of Module 4."""

    def test_dashboard_caller_passes_no_actor(self):
        src = _fn_src("crm_staff_dashboard")
        assert "calculate_intelligence()" in src, \
            "crm_staff_dashboard must keep the default actor=None"
        assert "calculate_intelligence(actor" not in src

    def test_leaderboard_is_identical_for_staff_and_admin(self, seeded):
        """Rank is derived from the leaderboard. If the filter had been applied
        to `leads`, a STAFF viewer's leaderboard would collapse to themselves
        and everyone would rank #1."""
        _, ctx_k, _ = render(seeded["kiran"])
        _, ctx_a, _ = render(seeded["admin"])
        lb_k = (ctx_k.get("intel") or {})["leaderboard"]
        lb_a = (ctx_a.get("intel") or {})["leaderboard"]
        assert lb_k == lb_a
        assert len(lb_k) > 1, "leaderboard collapsed to a single member"

    def test_sla_activity_workload_identical_for_staff_and_admin(self, seeded):
        _, ctx_k, _ = render(seeded["kiran"])
        _, ctx_a, _ = render(seeded["admin"])
        ik = ctx_k.get("intel") or {}
        ia = ctx_a.get("intel") or {}
        for key in ("sla", "activity_feed", "workload_snapshot"):
            assert ik[key] == ia[key], f"{key} diverged between roles"

    def test_only_priority_queue_differs_between_roles(self, seeded):
        _, ctx_k, _ = render(seeded["kiran"])
        _, ctx_a, _ = render(seeded["admin"])
        ik = ctx_k.get("intel") or {}
        ia = ctx_a.get("intel") or {}
        differing = {k for k in ik if ik[k] != ia[k]}
        assert differing == {"priority_queue"}, differing

    def test_staff_dashboard_still_renders(self, seeded):
        r = client(seeded["kiran"]).get("/crm/staff-dashboard",
                                        follow_redirects=True)
        assert r.status_code == 200, r.status_code

    def test_filter_is_not_applied_to_the_shared_leads_query(self):
        """`leads = tenant_query(...).all()` must remain unfiltered."""
        src = _fn_src("calculate_intelligence")
        line = [l for l in src.split("\n")
                if l.strip().startswith("leads = tenant_query(")]
        assert line, "the shared leads query moved or was renamed"
        assert "owner_filter" not in line[0], \
            "ownership was applied to the SHARED leads collection"


# ═══ threshold behaviour unchanged ═══════════════════════════════════════════

class TestThresholdBehaviour:
    def test_below_threshold_excluded(self, seeded):
        assert COLD not in phones(pq(seeded["kiran"]))
        assert COLD not in phones(pq(seeded["admin"]))

    def test_admitted_excluded(self, seeded):
        assert ADMITTED not in phones(pq(seeded["kiran"]))
        assert ADMITTED not in phones(pq(seeded["admin"]))

    def test_sorted_descending_by_score(self, seeded):
        for uid in ("admin", "kiran"):
            scores = [r["score"] for r in pq(seeded[uid])]
            assert scores == sorted(scores, reverse=True), scores

    def test_threshold_constant_unchanged(self):
        src = open(ADMIN_PY, encoding="utf-8").read()
        node = [n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "") == "INTELLIGENCE_CONSTANTS"][0]
        d = ast.literal_eval(node.value)
        assert d["THRESHOLD_HOT"] == 80

    def test_top_25_limit_unchanged(self):
        src = _fn_src("calculate_intelligence")
        assert "priority_queue[:25]" in src

    def test_every_returned_row_meets_the_predicate(self, seeded):
        with _APP.app_context():
            rows = {c.phone: c for c in ConversationState.query.filter_by(
                tenant_id=OX).all()}
        for uid in ("admin", "kiran"):
            for r in pq(seeded[uid]):
                lead = rows[r["phone"]]
                assert (lead.lead_score or 0) >= HOT
                assert not lead.is_admitted


# ═══ wiring ══════════════════════════════════════════════════════════════════

class TestWiring:
    def test_signature_takes_an_actor(self):
        src = open(ADMIN_PY, encoding="utf-8").read()
        node = [n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.FunctionDef)
                and n.name == "calculate_intelligence"][0]
        assert [a.arg for a in node.args.args] == ["tenant_id", "actor"]

    def test_operations_route_threads_the_actor(self):
        src = _fn_src("crm_operations")
        assert "calculate_intelligence(actor=get_current_actor())" in src

    def test_filter_is_role_conditional(self):
        src = _fn_src("calculate_intelligence")
        assert '"STAFF"' in src or "'STAFF'" in src

    def test_uses_the_existing_owner_filter(self):
        """One ownership rule, not a second implementation."""
        src = _fn_src("calculate_intelligence")
        assert "staff_identity_service.owner_filter" in src

    def test_operations_panels_from_3c_still_filtered(self):
        """RC2.3E-3C must survive this phase."""
        src = _fn_src("calculate_operations")
        assert "staff_identity_service.owner_filter" in src


# ═══ what this phase does NOT fix ════════════════════════════════════════════

class TestKnownRemainingExposure:
    """A FIFTH PII surface on /crm/operations, found while writing this file.

    calculate_automation_intelligence(leads, events) feeds four more panels
    that render customer name, phone and an /crm/lead/<phone> link:
    unassigned_hot, stalled_admissions, recovery_queue and recommendations. It
    takes a pre-fetched `leads` list, has no actor parameter and no ownership
    filter, and is shared with crm_staff_dashboard — the same shape as the
    defect this phase fixes.

    It is EXPLICITLY out of scope (the approved scope names it), so these tests
    assert the leak is STILL PRESENT. They will FAIL when it is closed, which
    is the point: the gap cannot be quietly forgotten, and whoever closes it is
    forced to come here and invert them.
    """

    def test_automation_helper_still_has_no_actor(self):
        src = open(ADMIN_PY, encoding="utf-8").read()
        node = [n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.FunctionDef)
                and n.name == "calculate_automation_intelligence"][0]
        assert [a.arg for a in node.args.args] == ["leads", "events"], \
            "calculate_automation_intelligence now takes an actor — close the " \
            "gap and invert this test"

    def test_automation_panels_still_leak_a_colleagues_lead(self, seeded):
        """Kiran still sees the unowned/colleague leads through the automation
        panels, outside the priority queue."""
        _, _, body = render(seeded["kiran"])
        panel = pq_panel(body)
        leaked = [ph for ph in (A_HOT, ADM_HOT, UNOWNED)
                  if f"/crm/lead/{ph}" in body]
        assert leaked, \
            "the automation panels no longer leak — update this test and " \
            "remove the caveat from the module docstring"
        for ph in leaked:
            assert f"/crm/lead/{ph}" not in panel, \
                "a leak reappeared INSIDE the priority queue — this phase " \
                "regressed"
