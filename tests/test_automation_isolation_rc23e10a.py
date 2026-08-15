"""Phase RC2.3E-10A — STAFF see only their own rows in the automation panels.

THE DEFECT
----------
/crm/operations renders four panels from calculate_automation_intelligence():
unassigned_hot, stalled_admissions, recovery_queue and recommendations. The
helper took only (leads, events) — no actor — so it could not filter by owner
even in principle. Each row carries customer NAME and PHONE, and every panel
renders an <a href="/crm/lead/{{ item.phone }}"> link, where lead
detail/update/stage/send sit behind check_auth() alone. The exposure was
therefore not only disclosure but a working click-through to a colleague's
customer. Those route guards are a separate RBAC defect and are NOT fixed here
— which is why test_every_phone_is_owned asserts on the PHONE SET rather than
on rendered text.

Production at discovery: 20 rendered rows (all from `recommendations`), 17-20
of them not the viewer's, for 3 of 3 STAFF members.

The other three panels were empty for TWO different reasons, and the
distinction matters:

  * unassigned_hot and recovery_queue are empty by DATA. No lead currently
    meets their predicates; both populate the moment one does. The fixture
    seeds rows for them.

  * stalled_admissions is empty by CONSTRUCTION — a pre-existing dead panel
    discovered while writing this file. See
    test_stalled_admissions_is_structurally_dead. My RC2.3E-10 discovery
    report said all three were "empty by data"; that was wrong for this one.

WHY THE FILTER IS SCOPED TO THE CUSTOMER LOOP
---------------------------------------------
`aging` is counted in its own earlier loop over `leads` and must stay
tenant-wide; `productivity` is derived from `events` and cannot be affected by
lead filtering at all. crm_staff_dashboard consumes ONLY productivity — it
never passes `automation` to its template — so it is provably untouched.
Filtering `leads` globally would silently turn `aging` into a per-staff count.
TestSharedOutputsUnchanged is the regression guard for that wrong
implementation.

UNASSIGNED LEADS ARE ABSENT FOR STAFF BY DESIGN
------------------------------------------------
They have no owner, so no ownership rule can match them. Approved in the
RC2.3E-10A scope and consistent with RC2.3E-3C, where the same consequence was
accepted for the "Unassigned lead" issue class.

Import isolation follows test_priority_queue_isolation_rc23e9.py.
"""
import ast
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23e10a_auto.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc23e10a-admin-key")
os.environ.setdefault("SECRET_KEY", "rc23e10a-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc23e10a-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from flask import template_rendered                                      # noqa: E402
from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState, LeadEvent       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_PY = os.path.join(ROOT, "app", "routes", "admin.py")

OX = "t-ox"
OTHER = "t-other"
URL = "/crm/operations"
PANELS = ("unassigned_hot", "stalled_admissions", "recovery_queue",
          "recommendations")

# Kiran's — one per panel, so every panel is populated for the owner.
K_STALL = "919000001001"       # PAYMENT_PENDING  -> stalled_admissions
K_RECOV = "919000001002"       # score 60, 30d silent -> recovery_queue + recs
K_REC = "919000001003"         # 5d silent -> recommendations
# Anju's — the colleague whose rows must not leak.
A_STALL = "919000002001"
A_RECOV = "919000002002"
A_REC = "919000002003"
# Unowned — feeds unassigned_hot; must vanish for STAFF, remain for ADMIN.
U_HOT = "919000003001"         # score 95, no owner
# Ownership spellings a naive rule gets wrong (both mirror production).
K_LOWER = "919000001004"       # assigned_staff 'kiran' vs username 'Kiran'
N_REC = "919000004001"         # owned by NIBU01, whose label is 'nibu s'
# Excluded by the predicates.
K_ADMITTED = "919000001005"
K_TERMINAL = "919000001006"
K_FRESH = "919000001007"       # 0 days silent -> no panel
RIVAL = "919000009001"

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


def _mk_lead(tenant, phone, name, staff, uid, score, days_silent,
             admitted=False, status="Lead"):
    db.session.add(ConversationState(
        phone=phone, tenant_id=tenant, name=name, lead_status=status,
        assigned_staff=staff, assigned_user_id=uid, lead_score=score,
        is_admitted=admitted,
        updated_at=datetime.utcnow() - timedelta(days=days_silent)))
    db.session.commit()


def _mk_event(tenant, phone, etype, payload=None):
    db.session.add(LeadEvent(tenant_id=tenant, phone=phone, event_type=etype,
                             event_data=json.dumps(payload or {})))
    db.session.commit()


@pytest.fixture()
def seeded():
    """Seeds, then RELEASES the app context before yielding — flask_login
    caches the resolved user on flask.g, bound to the APPLICATION context, so
    a held context leaks identity between test_client requests (14B.1)."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((OX, "Oxford"), (OTHER, "Rival")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()
        kiran = _mk_user(OX, "Kiran", display="Kiran")
        anju = _mk_user(OX, "Anju", display="Anju")
        nibu01 = _mk_user(OX, "NIBU01", display="nibu s")
        admin = _mk_user(OX, "admin_ox", role="ADMIN")
        rival = _mk_user(OTHER, "RivalStaff")
        plat = _mk_user(OX, "platform_op", role="SUPER_ADMIN")
        plat.tenant_id = None
        db.session.commit()

        _mk_lead(OX, K_STALL, "KiranStalled", "Kiran", kiran.id, 40, 5)
        _mk_lead(OX, K_RECOV, "KiranRecovery", "Kiran", kiran.id, 60, 30)
        _mk_lead(OX, K_REC, "KiranRecommend", "Kiran", kiran.id, 20, 5)
        _mk_lead(OX, A_STALL, "AnjuStalled", "Anju", anju.id, 45, 5)
        _mk_lead(OX, A_RECOV, "AnjuRecovery", "Anju", anju.id, 65, 30)
        _mk_lead(OX, A_REC, "AnjuRecommend", "Anju", anju.id, 25, 5)
        _mk_lead(OX, U_HOT, "UnownedHot", None, None, 95, 5)
        _mk_lead(OX, K_LOWER, "KiranLowercase", "kiran", kiran.id, 55, 30)
        _mk_lead(OX, N_REC, "NibuRecommend", "nibu s", nibu01.id, 30, 5)
        _mk_lead(OX, K_ADMITTED, "KiranAdmitted", "Kiran", kiran.id, 70, 30,
                 admitted=True)
        _mk_lead(OX, K_TERMINAL, "KiranLost", "Kiran", kiran.id, 70, 30,
                 status="Lost")
        _mk_lead(OX, K_FRESH, "KiranFresh", "Kiran", kiran.id, 70, 0)
        _mk_lead(OTHER, RIVAL, "RivalCustomer", "RivalStaff", rival.id, 95, 30)

        _mk_event(OX, K_STALL, "PAYMENT_PENDING")
        _mk_event(OX, A_STALL, "PAYMENT_PENDING")
        _mk_event(OTHER, RIVAL, "PAYMENT_PENDING")
        # Productivity is built from events only — pinned by
        # TestSharedOutputsUnchanged.
        _mk_event(OX, K_REC, "FOLLOW_UP_TASK",
                  {"task_id": "t1", "staff": "Kiran", "due_date": "2020-01-01"})
        _mk_event(OX, A_REC, "FOLLOW_UP_TASK",
                  {"task_id": "t2", "staff": "Anju", "due_date": "2020-01-01"})
        _mk_event(OX, A_REC, "FOLLOW_UP_COMPLETED",
                  {"task_id": "t2", "completed_by": "Anju"})

        ids = {"kiran": kiran.id, "anju": anju.id, "nibu01": nibu01.id,
               "admin": admin.id, "rival": rival.id, "plat": plat.id}
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
    seen = []

    def rec(sender, template, context, **extra):
        seen.append(context)

    template_rendered.connect(rec, _APP)
    try:
        r = client(uid, impersonate).get(URL, follow_redirects=False)
        return r, (seen[-1] if seen else {}), (
            r.get_data(as_text=True) if r.status_code == 200 else "")
    finally:
        template_rendered.disconnect(rec, _APP)


def auto(uid, impersonate=None):
    _, ctx, _ = render(uid, impersonate)
    return ctx.get("automation") or {}


def panel_phones(a, name):
    return {row["phone"] for row in a.get(name, [])}


def all_phones(a):
    out = set()
    for p in PANELS:
        out |= panel_phones(a, p)
    return out


def owned_phones(uid):
    with _APP.app_context():
        return {r[0] for r in db.session.query(ConversationState.phone)
                .filter(ConversationState.tenant_id == OX,
                        ConversationState.assigned_user_id == uid).all()}


def _fn_src(name):
    src = open(ADMIN_PY, encoding="utf-8").read()
    node = [n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef) and n.name == name][0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node = ast.Module(body=node.body[1:], type_ignores=[])
    return ast.unparse(node)


# ═══ STAFF isolation, all four panels ════════════════════════════════════════

class TestStaffIsolation:
    def test_stalled_admissions_is_structurally_dead(self, seeded):
        """PRE-EXISTING DEFECT, found while writing this file — not caused by
        and not fixed by this phase.

        The helper computes phones_payment_pending from the `events` it is
        GIVEN (admin.py:4951), but both callers pass only
        ["FOLLOW_UP_TASK", "FOLLOW_UP_COMPLETED"] (admin.py:4549, 6465).
        PAYMENT_PENDING is never in that list, so the set is always empty and
        stalled_admissions can never render a row — for ANY role. The fixture
        seeds real PAYMENT_PENDING events and the panel is still empty.

        This is why production measured 0 rows: the panel is dead code, not
        merely unpopulated. Widening the caller's event filter would ADD a
        panel's worth of customer PII, so it is deliberately NOT done here.
        Asserting the panel is empty pins the current behaviour; it fails if
        someone revives it without revisiting ownership filtering.
        """
        for who in ("kiran", "anju", "admin"):
            assert panel_phones(auto(seeded[who]), "stalled_admissions") == set()

    def test_recovery_queue_only_own(self, seeded):
        a = auto(seeded["kiran"])
        assert panel_phones(a, "recovery_queue") == {K_RECOV, K_LOWER}

    def test_recommendations_only_own(self, seeded):
        a = auto(seeded["kiran"])
        assert panel_phones(a, "recommendations") <= owned_phones(seeded["kiran"])
        assert K_RECOV in panel_phones(a, "recommendations")

    def test_unassigned_hot_absent_for_staff(self, seeded):
        """Approved consequence: unassigned leads have no owner, so no
        ownership rule can match them. Consistent with RC2.3E-3C."""
        a = auto(seeded["kiran"])
        assert panel_phones(a, "unassigned_hot") == set()
        assert U_HOT not in all_phones(a)

    def test_colleague_rows_absent_from_every_panel(self, seeded):
        a = auto(seeded["kiran"])
        for ph in (A_STALL, A_RECOV, A_REC, N_REC):
            assert ph not in all_phones(a), ph

    def test_colleague_names_absent(self, seeded):
        blob = repr({p: auto(seeded["kiran"]).get(p) for p in PANELS})
        for nm in ("AnjuStalled", "AnjuRecovery", "AnjuRecommend",
                   "UnownedHot", "NibuRecommend"):
            assert nm not in blob, nm

    def test_colleague_owner_labels_absent(self, seeded):
        a = auto(seeded["kiran"])
        owners = set()
        for p in PANELS:
            owners |= {r.get("staff") for r in a.get(p, []) if "staff" in r}
        assert "Anju" not in owners
        assert all((o or "").strip().lower() == "kiran" for o in owners), owners

    def test_every_phone_is_owned(self, seeded):
        """THE security assertion — the phone is what the /crm/lead link
        carries, so a redaction that left it would look fixed and not be."""
        assert all_phones(auto(seeded["kiran"])) <= owned_phones(seeded["kiran"])

    def test_no_colleague_lead_link_in_the_panels(self, seeded):
        _, _, body = render(seeded["kiran"])
        for ph in (A_STALL, A_RECOV, A_REC, U_HOT, N_REC):
            assert f"/crm/lead/{ph}" not in body, ph

    def test_own_lead_links_still_render(self, seeded):
        _, _, body = render(seeded["kiran"])
        assert f"/crm/lead/{K_RECOV}" in body

    def test_other_staff_sees_only_their_own(self, seeded):
        assert all_phones(auto(seeded["anju"])) <= owned_phones(seeded["anju"])
        assert K_RECOV not in all_phones(auto(seeded["anju"]))


# ═══ ownership spellings a naive rule gets wrong ═════════════════════════════

class TestOwnershipMechanism:
    def test_case_variant_ownership_resolved(self, seeded):
        """'kiran' stored beside username 'Kiran' — owner_filter compares
        lower(trim(col)) to the display label."""
        assert K_LOWER in panel_phones(auto(seeded["kiran"]), "recovery_queue")

    def test_display_name_labelled_member(self, seeded):
        """NIBU01's label is 'nibu s', nothing like its username."""
        a = auto(seeded["nibu01"])
        assert all_phones(a) == {N_REC}

    def test_display_name_labelled_row_not_leaked(self, seeded):
        assert N_REC not in all_phones(auto(seeded["kiran"]))

    def test_uses_the_existing_owner_filter(self):
        src = _fn_src("calculate_automation_intelligence")
        assert "staff_identity_service.owner_filter" in src


# ═══ ADMIN stays tenant-wide ═════════════════════════════════════════════════

class TestAdminUnfiltered:
    def test_admin_sees_every_panel_tenant_wide(self, seeded):
        a = auto(seeded["admin"])
        assert panel_phones(a, "unassigned_hot") == {U_HOT}
        assert panel_phones(a, "recovery_queue") == {K_RECOV, A_RECOV, K_LOWER}

    def test_admin_sees_colleague_and_unowned_rows(self, seeded):
        p = all_phones(auto(seeded["admin"]))
        assert {A_RECOV, U_HOT, N_REC} <= p

    def test_admin_page_links_every_lead(self, seeded):
        _, _, body = render(seeded["admin"])
        for ph in (A_RECOV, U_HOT, K_RECOV):
            assert f"/crm/lead/{ph}" in body


# ═══ SUPER_ADMIN ═════════════════════════════════════════════════════════════

class TestSuperAdmin:
    def test_impersonating_sees_that_tenant(self, seeded):
        a = auto(seeded["plat"], impersonate=OX)
        assert panel_phones(a, "unassigned_hot") == {U_HOT}
        assert RIVAL not in all_phones(a)

    def test_impersonating_the_other_tenant(self, seeded):
        a = auto(seeded["plat"], impersonate=OTHER)
        assert panel_phones(a, "recovery_queue") == {RIVAL}
        assert K_RECOV not in all_phones(a)

    def test_non_impersonating_is_fail_closed(self, seeded):
        r = client(seeded["plat"]).get(URL, follow_redirects=False)
        assert r.status_code in (302, 403), r.status_code

    def test_super_admin_is_not_treated_as_staff(self, seeded):
        """Role is SUPER_ADMIN, so the STAFF branch must not engage."""
        assert U_HOT in all_phones(auto(seeded["plat"], impersonate=OX))


# ═══ cross-tenant ════════════════════════════════════════════════════════════

class TestTenantIsolation:
    def test_no_other_tenant_rows(self, seeded):
        for who in ("kiran", "anju", "admin"):
            assert RIVAL not in all_phones(auto(seeded[who]))
        assert RIVAL in all_phones(auto(seeded["rival"]))

    def test_rival_sees_only_their_tenant(self, seeded):
        assert all_phones(auto(seeded["rival"])) <= {RIVAL}


# ═══ aging / productivity / dashboard must not move ══════════════════════════

class TestSharedOutputsUnchanged:
    """The regression guard for the WRONG implementation — filtering the
    global `leads` collection instead of the customer loop."""

    def test_aging_identical_for_staff_and_admin(self, seeded):
        assert auto(seeded["kiran"])["aging"] == auto(seeded["admin"])["aging"]

    def test_aging_counts_the_whole_tenant(self, seeded):
        a = auto(seeded["kiran"])["aging"]
        assert sum(a.values()) == 10, a   # 13 leads - 1 admitted - 1 terminal - 1 other tenant

    def test_productivity_identical_for_staff_and_admin(self, seeded):
        assert (auto(seeded["kiran"])["productivity"]
                == auto(seeded["admin"])["productivity"])

    def test_productivity_still_covers_every_member(self, seeded):
        prod = auto(seeded["kiran"])["productivity"]
        assert "Kiran" in prod and "Anju" in prod, prod

    def test_only_the_four_panels_differ_between_roles(self, seeded):
        k, a = auto(seeded["kiran"]), auto(seeded["admin"])
        differing = {key for key in k if k[key] != a[key]}
        assert differing <= set(PANELS), differing

    def test_dashboard_caller_passes_no_actor(self):
        src = _fn_src("crm_staff_dashboard")
        assert "calculate_automation_intelligence(leads, auto_events)" in src
        assert "calculate_automation_intelligence(leads, auto_events, actor" \
            not in src

    def test_staff_dashboard_still_renders(self, seeded):
        r = client(seeded["kiran"]).get("/crm/staff-dashboard",
                                        follow_redirects=True)
        assert r.status_code == 200, r.status_code

    def test_aging_loop_still_iterates_unfiltered_leads(self):
        src = _fn_src("calculate_automation_intelligence")
        assert "for lead in leads:" in src, \
            "the aging loop no longer iterates the full tenant lead set"

    def test_productivity_still_derives_from_events(self):
        src = _fn_src("calculate_automation_intelligence")
        assert "for ev in events:" in src


# ═══ predicates, ordering and caps unchanged ═════════════════════════════════

class TestPredicatesUnchanged:
    def test_admitted_excluded(self, seeded):
        for who in ("kiran", "admin"):
            assert K_ADMITTED not in all_phones(auto(seeded[who]))

    def test_terminal_status_excluded(self, seeded):
        for who in ("kiran", "admin"):
            assert K_TERMINAL not in all_phones(auto(seeded[who]))

    def test_fresh_lead_not_recommended(self, seeded):
        assert K_FRESH not in panel_phones(auto(seeded["kiran"]),
                                           "recommendations")

    def test_recovery_requires_warm_and_silent(self, seeded):
        """K_REC is silent 5 days with score 20 — below WARM and under 14d."""
        assert K_REC not in panel_phones(auto(seeded["admin"]),
                                         "recovery_queue")

    def test_unassigned_hot_requires_hot_score(self, seeded):
        assert panel_phones(auto(seeded["admin"]), "unassigned_hot") == {U_HOT}

    def test_stalled_panel_stays_empty_even_with_payment_events(self, seeded):
        """See test_stalled_admissions_is_structurally_dead."""
        assert panel_phones(auto(seeded["admin"]), "stalled_admissions") == set()

    def test_sort_orders_unchanged(self, seeded):
        a = auto(seeded["admin"])
        for p in ("recovery_queue", "unassigned_hot", "stalled_admissions"):
            s = [r["score"] for r in a[p]]
            assert s == sorted(s, reverse=True), (p, s)
        d = [r["days"] for r in a["recommendations"]]
        assert d == sorted(d, reverse=True), d

    def test_caps_unchanged(self):
        src = _fn_src("calculate_automation_intelligence")
        for key in ("recovery_queue[:20]", "recommendations[:20]",
                    "unassigned_hot[:20]", "stalled_admissions[:20]"):
            assert key in src, key


# ═══ wiring ══════════════════════════════════════════════════════════════════

class TestWiring:
    def test_signature_takes_an_actor(self):
        src = open(ADMIN_PY, encoding="utf-8").read()
        node = [n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.FunctionDef)
                and n.name == "calculate_automation_intelligence"][0]
        assert [a.arg for a in node.args.args] == ["leads", "events", "actor"]

    def test_route_threads_the_actor(self):
        src = _fn_src("crm_operations")
        assert "actor=get_current_actor()" in src

    def test_filter_is_role_conditional(self):
        src = _fn_src("calculate_automation_intelligence")
        assert '"STAFF"' in src or "'STAFF'" in src

    def test_rc23e9_priority_queue_still_filtered(self):
        """The previous phase must survive this one."""
        src = _fn_src("calculate_intelligence")
        assert "staff_identity_service.owner_filter" in src

    def test_rc23e3c_operations_panels_still_filtered(self):
        src = _fn_src("calculate_operations")
        assert "staff_identity_service.owner_filter" in src
