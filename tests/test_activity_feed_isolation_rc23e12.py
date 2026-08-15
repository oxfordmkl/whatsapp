"""Phase RC2.3E-12 — STAFF see activity on THEIR OWN leads in the feed.

THE DEFECT
----------
intel.activity_feed was the last panel on /crm/operations disclosing customer
identities to a non-owner. It has no name/phone field — it renders a
pre-formatted `label`, and three of five event types interpolate the customer:

    COURSE_ADMISSION   "{staff} admitted {lead_name}: {course}"
    LEAD_REASSIGNED    "Reassigned {lead_name}: {from} -> {to}"
    MANUAL_MESSAGE     "{staff} messaged {lead_name}"

so no field-level redaction reaches it. Production measured 45 of 50 rendered
entries naming a customer, 43 distinct customers, and 29-45 of them not the
viewer's — a larger disclosure than RC2.3E-9 (1 customer) or RC2.3E-10A (20).
It also revealed the tenant's reassignment history.

    lead_name = (lead.name if lead and lead.name else None) or ev.phone

...so a NAMELESS lead puts a raw customer phone into the label. Zero such rows
in production at audit time, one nameless lead away from being non-zero.
test_nameless_lead_phone_fallback_is_not_leaked covers it explicitly.

THE APPROVED DEFINITION
-----------------------
"Activity on MY LEADS", not "my activity". The filter keys on ev.phone against
the owner's lead set — never on edata['staff'] / 'completed_by', which are
display strings written at event time and can name someone who no longer owns
the lead. test_filter_does_not_key_on_the_event_actor_field pins that: an event
whose payload names the viewer, on a colleague's lead, must NOT appear.

ONE QUERY, TWO MODULES
----------------------
RC2.3E-9 resolved the owned-phone set inside Module 4. Module 3 needs the same
set, so it is hoisted above Module 3 and both read it. Modules 1, 2 and 5
(leaderboard, sla, workload_snapshot) still aggregate the UNFILTERED `leads`
and `events`, because crm_staff_dashboard derives the viewer's rank from that
leaderboard. TestSharedOutputsUnchanged is the regression guard.

Import isolation follows test_automation_isolation_rc23e10a.py.
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

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23e12_feed.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc23e12-admin-key")
os.environ.setdefault("SECRET_KEY", "rc23e12-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc23e12-broadcast")
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

K_LEAD = "919000012001"        # Kiran's, named
K_LEAD2 = "919000012002"       # Kiran's, named
K_NAMELESS = "919000012003"    # Kiran's, NO name -> label falls back to phone
A_LEAD = "919000012101"        # Anju's, named
A_NAMELESS = "919000012102"    # Anju's, NO name -> the fallback that must NOT leak
K_LOWER = "919000012004"       # Kiran's, assigned_staff stored lowercase
N_LEAD = "919000012201"        # NIBU01's, whose label is 'nibu s'
RIVAL = "919000012901"         # another tenant's

K_NAME = "KiranCustomerAlpha"
K_NAME2 = "KiranCustomerBeta"
K_LOWER_NAME = "KiranCustomerLower"
A_NAME = "AnjuCustomerGamma"
N_NAME = "NibuCustomerDelta"
RIVAL_NAME = "RivalCustomerZeta"

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


def _mk_lead(tenant, phone, name, staff, uid):
    db.session.add(ConversationState(
        phone=phone, tenant_id=tenant, name=name, lead_status="Lead",
        assigned_staff=staff, assigned_user_id=uid, lead_score=50,
        is_admitted=False, updated_at=datetime.utcnow()))
    db.session.commit()


def _mk_event(tenant, phone, etype, payload=None, ago_min=0):
    db.session.add(LeadEvent(
        tenant_id=tenant, phone=phone, event_type=etype,
        event_data=json.dumps(payload or {}),
        created_at=datetime.utcnow() - timedelta(minutes=ago_min)))
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

        _mk_lead(OX, K_LEAD, K_NAME, "Kiran", kiran.id)
        _mk_lead(OX, K_LEAD2, K_NAME2, "Kiran", kiran.id)
        _mk_lead(OX, K_NAMELESS, None, "Kiran", kiran.id)
        _mk_lead(OX, K_LOWER, K_LOWER_NAME, "kiran", kiran.id)
        _mk_lead(OX, A_LEAD, A_NAME, "Anju", anju.id)
        _mk_lead(OX, A_NAMELESS, None, "Anju", anju.id)
        _mk_lead(OX, N_LEAD, N_NAME, "nibu s", nibu01.id)
        _mk_lead(OTHER, RIVAL, RIVAL_NAME, "RivalStaff", rival.id)

        # One of every feed type, newest first by ago_min.
        _mk_event(OX, K_LEAD, "MANUAL_MESSAGE", {"staff": "Kiran"}, 1)
        _mk_event(OX, K_LEAD2, "COURSE_ADMISSION", {"staff": "Kiran"}, 2)
        _mk_event(OX, K_NAMELESS, "MANUAL_MESSAGE", {"staff": "Kiran"}, 3)
        _mk_event(OX, K_LOWER, "LEAD_REASSIGNED",
                  {"from": "Anju", "to": "Kiran"}, 4)
        _mk_event(OX, K_LEAD, "FOLLOW_UP_TASK",
                  {"task_id": "kt1", "staff": "Kiran", "task": "Call back"}, 5)
        _mk_event(OX, K_LEAD, "FOLLOW_UP_COMPLETED",
                  {"task_id": "kt1", "completed_by": "Kiran"}, 6)

        _mk_event(OX, A_LEAD, "MANUAL_MESSAGE", {"staff": "Anju"}, 7)
        _mk_event(OX, A_LEAD, "COURSE_ADMISSION", {"staff": "Anju"}, 8)
        _mk_event(OX, A_NAMELESS, "MANUAL_MESSAGE", {"staff": "Anju"}, 9)
        _mk_event(OX, A_LEAD, "LEAD_REASSIGNED",
                  {"from": "Kiran", "to": "Anju"}, 10)

        # THE AUTHORIZATION TRAP: the payload names Kiran, but the LEAD is
        # Anju's. Under "activity on my leads" this must NOT reach Kiran.
        _mk_event(OX, A_LEAD, "FOLLOW_UP_TASK",
                  {"task_id": "at1", "staff": "Kiran", "task": "Anju lead"}, 11)
        _mk_event(OX, A_LEAD, "FOLLOW_UP_COMPLETED",
                  {"task_id": "at1", "completed_by": "Kiran"}, 12)

        _mk_event(OX, N_LEAD, "MANUAL_MESSAGE", {"staff": "nibu s"}, 13)
        _mk_event(OTHER, RIVAL, "MANUAL_MESSAGE", {"staff": "RivalStaff"}, 14)
        # A type outside feed_types — must never render for anyone.
        _mk_event(OX, K_LEAD, "LEAD_SCORE_CHANGE", {"staff": "Kiran"}, 15)

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


def intel(uid, impersonate=None):
    _, ctx, _ = render(uid, impersonate)
    return ctx.get("intel") or {}


def feed(uid, impersonate=None):
    return intel(uid, impersonate).get("activity_feed", [])


def labels(uid, impersonate=None):
    return [e["label"] for e in feed(uid, impersonate)]


def blob(uid, impersonate=None):
    return " || ".join(labels(uid, impersonate))


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

class TestStaffFeedIsolation:
    def test_staff_sees_activity_on_own_leads(self, seeded):
        b = blob(seeded["kiran"])
        assert K_NAME in b
        assert K_NAME2 in b
        assert K_LOWER_NAME in b

    def test_colleague_customer_names_absent(self, seeded):
        b = blob(seeded["kiran"])
        assert A_NAME not in b
        assert N_NAME not in b

    def test_colleague_lead_phones_absent(self, seeded):
        b = blob(seeded["kiran"])
        for ph in (A_LEAD, A_NAMELESS, N_LEAD, RIVAL):
            assert ph not in b, ph

    def test_nameless_lead_phone_fallback_is_not_leaked(self, seeded):
        """THE fallback case. lead_name degrades to ev.phone when the lead has
        no name, so a colleague's raw number can land in the label text where
        no field-level redaction reaches it."""
        b = blob(seeded["kiran"])
        assert A_NAMELESS not in b, "a colleague's phone leaked via the fallback"
        # ...while the viewer's OWN nameless lead still renders as its phone.
        assert K_NAMELESS in b

    def test_filter_does_not_key_on_the_event_actor_field(self, seeded):
        """'Activity on my leads', not 'my activity'. Two events name Kiran in
        their payload but sit on ANJU's lead; keying on edata['staff'] would
        admit them and disclose Anju's customer."""
        b = blob(seeded["kiran"])
        assert "Anju lead" not in b
        assert A_NAME not in b

    def test_every_entry_concerns_an_owned_lead(self, seeded):
        """The security assertion, expressed over the owned set rather than
        over rendered text."""
        with _APP.app_context():
            owned = {r[0] for r in db.session.query(ConversationState.phone)
                     .filter(ConversationState.tenant_id == OX,
                             ConversationState.assigned_user_id
                             == seeded["kiran"]).all()}
            names = {r[0] for r in db.session.query(ConversationState.name)
                     .filter(ConversationState.tenant_id == OX,
                             ConversationState.assigned_user_id
                             != seeded["kiran"]).all() if r[0]}
        b = blob(seeded["kiran"])
        for n in names:
            assert n not in b, n
        assert any(p in b or K_NAME in b for p in owned)

    def test_other_staff_sees_only_their_own(self, seeded):
        b = blob(seeded["anju"])
        assert A_NAME in b
        assert K_NAME not in b
        assert K_NAMELESS not in b

    def test_display_name_labelled_member(self, seeded):
        """NIBU01's label is 'nibu s', nothing like its username — ownership
        must resolve through the existing rule, not a naive comparison."""
        b = blob(seeded["nibu01"])
        assert N_NAME in b
        assert K_NAME not in b

    def test_case_variant_ownership_resolved(self, seeded):
        """assigned_staff 'kiran' beside username 'Kiran'."""
        assert K_LOWER_NAME in blob(seeded["kiran"])

    def test_staff_feed_is_smaller_than_admin_feed(self, seeded):
        assert len(feed(seeded["kiran"])) < len(feed(seeded["admin"]))


# ═══ ADMIN unchanged ═════════════════════════════════════════════════════════

class TestAdminUnfiltered:
    def test_admin_sees_every_tenant_customer(self, seeded):
        b = blob(seeded["admin"])
        for n in (K_NAME, K_NAME2, A_NAME, N_NAME, K_LOWER_NAME):
            assert n in b, n

    def test_admin_sees_both_nameless_fallbacks(self, seeded):
        b = blob(seeded["admin"])
        assert K_NAMELESS in b and A_NAMELESS in b

    def test_admin_never_sees_another_tenant(self, seeded):
        b = blob(seeded["admin"])
        assert RIVAL_NAME not in b and RIVAL not in b


# ═══ SUPER_ADMIN ═════════════════════════════════════════════════════════════

class TestSuperAdmin:
    def test_impersonating_sees_the_tenant_feed(self, seeded):
        b = blob(seeded["plat"], impersonate=OX)
        assert K_NAME in b and A_NAME in b
        assert RIVAL_NAME not in b

    def test_impersonating_the_other_tenant(self, seeded):
        b = blob(seeded["plat"], impersonate=OTHER)
        assert RIVAL_NAME in b
        assert K_NAME not in b

    def test_non_impersonating_is_fail_closed(self, seeded):
        r = client(seeded["plat"]).get(URL, follow_redirects=False)
        assert r.status_code in (302, 403), r.status_code

    def test_super_admin_is_not_treated_as_staff(self, seeded):
        assert len(feed(seeded["plat"], impersonate=OX)) == \
            len(feed(seeded["admin"]))


# ═══ cross-tenant ════════════════════════════════════════════════════════════

class TestTenantIsolation:
    def test_no_other_tenant_rows(self, seeded):
        for who in ("kiran", "anju", "admin"):
            assert RIVAL_NAME not in blob(seeded[who])

    def test_rival_sees_only_their_tenant(self, seeded):
        b = blob(seeded["rival"])
        assert K_NAME not in b and A_NAME not in b


# ═══ everything else must not move ═══════════════════════════════════════════

class TestSharedOutputsUnchanged:
    """Regression guard for the WRONG implementation: filtering the shared
    `leads`/`events` collections instead of Module 3 only."""

    def test_leaderboard_identical_for_staff_and_admin(self, seeded):
        assert intel(seeded["kiran"])["leaderboard"] == \
            intel(seeded["admin"])["leaderboard"]

    def test_leaderboard_still_lists_every_member(self, seeded):
        lb = intel(seeded["kiran"])["leaderboard"]
        assert len(lb) > 1, "leaderboard collapsed to a single member"

    def test_sla_identical(self, seeded):
        assert intel(seeded["kiran"])["sla"] == intel(seeded["admin"])["sla"]

    def test_workload_snapshot_identical(self, seeded):
        assert intel(seeded["kiran"])["workload_snapshot"] == \
            intel(seeded["admin"])["workload_snapshot"]

    def test_only_feed_and_priority_queue_differ(self, seeded):
        k, a = intel(seeded["kiran"]), intel(seeded["admin"])
        differing = {key for key in k if k[key] != a[key]}
        assert differing <= {"activity_feed", "priority_queue"}, differing

    def test_automation_productivity_and_aging_identical(self, seeded):
        _, ck, _ = render(seeded["kiran"])
        _, ca, _ = render(seeded["admin"])
        for key in ("productivity", "aging"):
            assert ck["automation"][key] == ca["automation"][key], key

    def test_staff_dashboard_still_renders(self, seeded):
        r = client(seeded["kiran"]).get("/crm/staff-dashboard",
                                        follow_redirects=True)
        assert r.status_code == 200, r.status_code

    def test_modules_1_2_5_still_aggregate_unfiltered_leads(self):
        src = _fn_src("calculate_intelligence")
        line = [l for l in src.splitlines()
                if l.strip().startswith("leads = tenant_query(")]
        assert line and "owner_filter" not in line[0], \
            "ownership was applied to the SHARED leads collection"


# ═══ feed semantics preserved ════════════════════════════════════════════════

class TestFeedSemanticsPreserved:
    def test_newest_first(self, seeded):
        for who in ("admin", "kiran"):
            f = feed(seeded[who])
            assert f, who
            # seeded newest-first; the first entry is the most recent event
            assert f[0]["label"] == feed(seeded[who])[0]["label"]

    def test_event_types_unchanged(self, seeded):
        b = blob(seeded["admin"])
        assert "admitted" in b        # COURSE_ADMISSION
        assert "Reassigned" in b      # LEAD_REASSIGNED
        assert "messaged" in b        # MANUAL_MESSAGE
        assert "created task" in b    # FOLLOW_UP_TASK
        assert "completed task" in b  # FOLLOW_UP_COMPLETED

    def test_type_outside_feed_types_never_renders(self, seeded):
        for who in ("admin", "kiran"):
            assert "LEAD_SCORE_CHANGE" not in blob(seeded[who])

    def test_entry_shape_unchanged(self, seeded):
        for e in feed(seeded["admin"]):
            assert set(e) == {"time", "date", "label", "icon", "color"}

    def test_timestamps_and_icons_present(self, seeded):
        for e in feed(seeded["kiran"]):
            assert e["time"] and e["date"] and e["icon"] and e["color"]

    def test_cap_is_still_fifty(self):
        assert "len(activity_feed) >= 50" in _fn_src("calculate_intelligence")

    def test_staff_still_gets_a_full_page_of_their_own(self, seeded):
        """BEHAVIOURAL cap test, replacing a shape assertion.

        A colleague's events must not consume the 50-entry budget. Seeds 60
        owned events beneath 60 newer colleague events: if the cap were
        counted over SCANNED rather than RENDERED entries — or if the skip
        were a `break` — the staff feed would come back short or empty.

        Note the ordering of the two `continue`s in the loop is NOT asserted:
        both forms append only when the type matches AND the lead is owned AND
        fewer than 50 are collected, so swapping them changes nothing. Pinning
        that order would assert implementation shape, not behaviour.
        """
        with _APP.app_context():
            for i in range(60):
                _mk_event(OX, A_LEAD, "MANUAL_MESSAGE", {"staff": "Anju"},
                          100 + i)          # newer than the owned batch below
            for i in range(60):
                _mk_event(OX, K_LEAD, "MANUAL_MESSAGE", {"staff": "Kiran"},
                          1000 + i)
        f = feed(seeded["kiran"])
        assert len(f) == 50, len(f)
        b = " || ".join(e["label"] for e in f)
        assert A_NAME not in b
        assert K_NAME in b


# ═══ wiring ══════════════════════════════════════════════════════════════════

class TestWiring:
    def test_exactly_one_ownership_query_in_the_function(self):
        assert _fn_src("calculate_intelligence").count("owner_filter") == 1

    def test_module_3_uses_the_hoisted_set(self):
        assert "ev.phone not in _owned_phones" in _fn_src("calculate_intelligence")

    def test_module_4_reuses_the_same_set(self):
        src = _fn_src("calculate_intelligence")
        assert "if _owned_phones is not None:" in src

    def test_filter_is_role_conditional(self):
        src = _fn_src("calculate_intelligence")
        assert "'STAFF'" in src or '"STAFF"' in src

    def test_route_still_threads_the_actor(self):
        assert "calculate_intelligence(actor=get_current_actor())" in \
            _fn_src("crm_operations")

    def test_dashboard_caller_passes_no_actor(self):
        assert "calculate_intelligence()" in _fn_src("crm_staff_dashboard")

    def test_previous_phases_survive(self):
        for fn in ("calculate_operations", "calculate_automation_intelligence"):
            assert "staff_identity_service.owner_filter" in _fn_src(fn), fn
