"""Phase RC2.3E-1 Batch 1b — the staff dashboard reads one identity, five ways.

crm_staff_dashboard resolves ownership in FIVE places:

    1. the open-leads query        -> owner_filter()          (FK-able)
    2. the admissions query        -> owner_filter()          (FK-able)
    3. get_all_tasks()["staff"]    -> raw assigned_staff string
    4. intel["leaderboard"]["name"]-> normalize_staff_name(assigned_staff)
    5. automation["productivity"]  -> LeadEvent JSON payload names

WHY THIS IS A BRIDGE, NOT A MIGRATION
-------------------------------------
Only 1 and 2 can be FK-keyed. LeadEvent has NO assigned_user_id column, so 5
can never key on a FK without migrating the event log; 3 and 4 are produced by
Batch 4 helpers that key on normalize_staff_name(). Rather than pretend one
key-space exists, the route derives BOTH from a single resolved User:

    _owner_user  -> owner_filter() for the queries
    _display_key -> normalize_staff_name(_owner_user.display_label())
                    for the three name-keyed lookups

Both sides therefore agree whether STAFF_IDENTITY_READ_FK is on or off, which
is what makes this screen safe to flip. Every behavioural test below runs in
BOTH regimes and asserts the KPI card and the leaderboard describe the same
person.

THE DEFECT THIS FIXES
---------------------
The route looked itself up by USERNAME (normalize_staff_name(actor username))
while every producer keys on the DISPLAY LABEL. For a staff member whose
display_name differs from their username — which Staff Management creates —
the KPIs, leaderboard rank and productivity block were all somebody else's, or
empty. The `ravi` fixture is that case; production cannot exhibit it yet
because no user has display_name set.

Import isolation follows test_ownership_filter_rc23e1_b1a.py.
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

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23e1b1b_dash.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc23e1b1b-admin-key")
os.environ.setdefault("SECRET_KEY", "rc23e1b1b-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc23e1b1b-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState, LeadEvent       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX = "t-ox"
OTHER = "t-other"
URL = "/crm/staff-dashboard"

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


def _mk(tenant, username, role="STAFF", active=True, display_name=None):
    u = User(username=username, email=f"{username}.{tenant}@x.test".replace(" ", "_"),
             password_hash=generate_password_hash("pw"), role=role,
             tenant_id=tenant, is_active=active, display_name=display_name,
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

        anju = _mk(OX, "Anju")
        # username RAVI, display label "Ravi Kumar" — the renamed staff member.
        ravi = _mk(OX, "RAVI", display_name="Ravi Kumar")
        # ALL-CAPS display label. calculate_intelligence stores the raw string
        # in leaderboard["name"] but keys its tallies on normalize_staff_name(),
        # so matching the row needs normalization on BOTH sides. Without this
        # fixture that requirement is only pinned by a structural test.
        nisha = _mk(OX, "nisha", display_name="NISHA KUMARI")
        admin = _mk(OX, "admin_ox", role="ADMIN")
        ids = {"anju": anju.id, "ravi": ravi.id, "nisha": nisha.id,
               "admin": admin.id}

        def lead(phone, staff, uid, score=10, admitted=False, status="Lead"):
            db.session.add(ConversationState(
                phone=phone, tenant_id=OX, name=f"L{phone[-3:]}",
                lead_status=status, assigned_staff=staff, assigned_user_id=uid,
                lead_score=score, is_admitted=admitted))

        # Ravi: 3 open (one hot), 1 admitted.
        lead("919400000001", "Ravi Kumar", ids["ravi"], score=90)
        lead("919400000002", "Ravi Kumar", ids["ravi"])
        lead("919400000003", "ravi kumar", ids["ravi"])      # case variant
        lead("919400000004", "Ravi Kumar", ids["ravi"], admitted=True)
        # Anju: 1 open.
        lead("919400000005", "Anju", ids["anju"])
        # Nisha: 2 open, stored under the all-caps label the dropdown offers.
        lead("919400000006", "NISHA KUMARI", ids["nisha"])
        lead("919400000007", "NISHA KUMARI", ids["nisha"])
        db.session.commit()

        # Productivity comes from LeadEvent payloads, which carry NAMES only.
        for phone, who in (("919400000001", "Ravi Kumar"),
                           ("919400000002", "Ravi Kumar")):
            db.session.add(LeadEvent(
                tenant_id=OX, phone=phone, event_type="FOLLOW_UP_COMPLETED",
                event_data=json.dumps({"task_id": f"t-{phone[-2:]}",
                                       "completed_by": who, "staff": who})))
        db.session.commit()
        yield ids
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def get(uid, qs=""):
    return client(uid).get(f"{URL}{qs}", follow_redirects=True)


def context(uid, qs=""):
    """Render the dashboard and return its TEMPLATE CONTEXT.

    Substring-matching the HTML is too weak to catch a wrong lookup — a page
    can mention the right name while every number on it belongs to someone
    else. The context carries kpis / staff_rank / staff_lb / my_productivity
    verbatim, so these tests assert the values the route actually computed.
    """
    from flask import template_rendered
    captured = {}

    def record(sender, template, context, **extra):
        captured.update(context)

    template_rendered.connect(record, _APP)
    try:
        resp = client(uid).get(f"{URL}{qs}", follow_redirects=True)
    finally:
        template_rendered.disconnect(record, _APP)
    assert resp.status_code == 200
    return captured


# ═══ the renamed staff member ════════════════════════════════════════════════

class TestDisplayLabelIdentity:

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_renamed_staff_is_identified_by_display_label(self, seeded, regime):
        """Ravi's leads read 'Ravi Kumar'; the route used to look up 'Ravi'."""
        ctx = context(seeded["ravi"])
        assert ctx["staff_name"] == "Ravi Kumar"

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_kpis_are_exactly_theirs(self, seeded, regime):
        """4 non-terminal (one hot at score 90), 1 of them admitted.

        my_leads filters on lead_status NOT IN terminal — is_admitted does NOT
        remove a lead from the open count, so the admitted one is in both. The
        old username lookup produced 0 for every one of these.
        """
        k = context(seeded["ravi"])["kpis"]
        assert k["my_leads"] == 4
        assert k["hot_leads"] == 1
        assert k["admissions"] == 1

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_case_variant_lead_is_counted(self, seeded, regime):
        """'ravi kumar' is one of the four; a case-sensitive key drops it."""
        assert context(seeded["ravi"])["kpis"]["my_leads"] == 4

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_another_staff_members_leads_are_excluded(self, seeded, regime):
        assert context(seeded["anju"])["kpis"]["my_leads"] == 1


# ═══ the five lookups agree ══════════════════════════════════════════════════

class TestFiveLookupsAgree:

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_productivity_is_found_by_the_display_key(self, seeded, regime):
        """Keyed off LeadEvent payload names — the bridge's whole point.
        Two FOLLOW_UP_COMPLETED events name 'Ravi Kumar'; a username lookup
        finds the empty default instead."""
        p = context(seeded["ravi"])["my_productivity"]
        assert p["completed"] == 2

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_leaderboard_row_belongs_to_the_same_person(self, seeded, regime):
        """The leaderboard is keyed by normalize_staff_name(assigned_staff).
        If the KPI card and this row disagree, the screen is describing two
        different people — the exact failure this batch exists to prevent."""
        ctx = context(seeded["ravi"])
        assert ctx["staff_lb"] is not None, "no leaderboard row matched"
        assert ctx["staff_lb"]["name"] == ctx["staff_name"] == "Ravi Kumar"
        assert ctx["staff_rank"] is not None

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_leaderboard_and_kpi_count_the_same_leads(self, seeded, regime):
        """Both sides of the bridge must land on one person's numbers."""
        ctx = context(seeded["ravi"])
        assert ctx["staff_lb"]["assigned_leads"] == 4
        assert ctx["staff_lb"]["admissions"] == ctx["kpis"]["admissions"] == 1
        assert ctx["kpis"]["my_leads"] == 4

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_all_caps_display_label_still_matches_its_leaderboard_row(
            self, seeded, regime):
        """leaderboard["name"] holds the RAW label ('NISHA KUMARI') while the
        key is normalized ('Nisha Kumari'). Both sides must be normalized or
        this person has KPIs and no rank."""
        ctx = context(seeded["nisha"])
        assert ctx["kpis"]["my_leads"] == 2
        assert ctx["staff_lb"] is not None, "all-caps label found no rank"
        assert ctx["staff_rank"] is not None
        assert ctx["staff_lb"]["assigned_leads"] == 2

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_admin_and_staff_views_agree_exactly(self, seeded, regime):
        """The same person's numbers must not depend on who is looking."""
        a = context(seeded["ravi"])
        b = context(seeded["admin"], "?staff=Ravi Kumar")
        assert a["kpis"] == b["kpis"]
        assert a["staff_name"] == b["staff_name"]
        assert a["my_productivity"] == b["my_productivity"]
        assert a["staff_rank"] == b["staff_rank"]

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_admin_may_look_up_by_username_too(self, seeded, regime):
        """resolve() matches username OR display_name, so an operator typing
        the code lands on the same person as one typing the label."""
        by_label = context(seeded["admin"], "?staff=Ravi Kumar")
        by_user = context(seeded["admin"], "?staff=RAVI")
        assert by_user["staff_name"] == "Ravi Kumar"
        assert by_user["kpis"] == by_label["kpis"]


# ═══ isolation and fail-closed ═══════════════════════════════════════════════

class TestIsolation:

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_unknown_staff_param_fails_closed(self, seeded, regime):
        """owner_filter(model, None) is false() — zero rows, not everyone's."""
        k = context(seeded["admin"], "?staff=Ghost")["kpis"]
        assert k["my_leads"] == 0
        assert k["admissions"] == 0
        assert k["hot_leads"] == 0

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_staff_cannot_view_another_staff_member(self, seeded, regime):
        """A STAFF actor is always current_user; ?staff= must be ignored."""
        ctx = context(seeded["ravi"], "?staff=Anju")
        assert ctx["staff_name"] == "Ravi Kumar"
        assert ctx["kpis"]["my_leads"] == 4

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_anju_sees_only_her_own(self, seeded, regime):
        ctx = context(seeded["anju"])
        assert ctx["staff_name"] == "Anju"
        assert ctx["kpis"]["my_leads"] == 1
        assert ctx["kpis"]["admissions"] == 0


# ═══ structural ══════════════════════════════════════════════════════════════

class TestStructure:

    def _fn(self, name="crm_staff_dashboard"):
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        return ast.unparse(next(n for n in ast.walk(tree)
                                if isinstance(n, ast.FunctionDef) and n.name == name))

    def test_queries_use_owner_filter(self):
        assert "owner_filter" in self._fn()

    def test_hand_rolled_name_comparison_is_gone(self):
        assert "func.lower(func.trim(ConversationState.assigned_staff))" not in self._fn()

    def test_all_name_keyed_lookups_use_the_display_key(self):
        """One identity, five uses — none may fall back to the raw ?staff=.

        The leaderboard side is normalized on BOTH sides: calculate_intelligence
        stores the raw active_staff string in "name" while keying its own
        tallies on normalize_staff_name(), so a bare == would miss an all-caps
        display label.
        """
        src = self._fn()
        assert "normalize_staff_name(entry['name']) == _display_key" in src
        assert "automation['productivity'].get(_display_key" in src
        assert "staff_name_normalized" in src

    def test_display_key_derives_from_the_resolved_user(self):
        src = self._fn()
        assert "_owner_user.display_label()" in src
        assert "normalize_staff_name(_owner_user.display_label())" in src

    def test_consumer_never_reads_the_flag(self):
        src = self._fn()
        assert "STAFF_IDENTITY_READ_FK" not in src
        assert "read_fk_enabled" not in src

    def test_h4_stays_closed_here(self):
        src = self._fn()
        assert "getattr(current_user, 'tenant_id'" not in src
        assert "_actor_tenant_id()" in src

    def test_no_schema_or_migration_change(self):
        versions = os.path.join(ROOT, "migrations", "versions")
        assert not [f for f in os.listdir(versions)
                    if "rc23e" in f.lower() or "b1b" in f.lower()]

    def test_flag_default_is_still_off(self):
        from app import flags
        before = os.environ.pop("STAFF_IDENTITY_READ_FK", None)
        try:
            assert flags.staff_identity_read_fk_enabled() is False
        finally:
            if before is not None:
                os.environ["STAFF_IDENTITY_READ_FK"] = before
