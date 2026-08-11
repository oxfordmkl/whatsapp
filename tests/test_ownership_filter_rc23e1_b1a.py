"""Phase RC2.3E-1 Batch 1a — ownership filters read through the dual-read helper.

Three filters migrate to staff_identity_service.owner_filter():

    _build_leads_query            (the leads list AND its export)
    crm_my_leads
    sales_pipeline._staff_ownership_clause   (+ required tenant_id)

THE DEFECT THIS FIXES
---------------------
All three compared assigned_staff to the actor's USERNAME. But assigned_staff
holds DISPLAY LABELS: the assignment dropdown is active_display_names(), which
is display_label() per user — display_name when set, username otherwise.

Staff Management writes display_name on create (admin.py:1927) and on edit, so
a staff member added with code RAVI and display name "Ravi Kumar" owns leads
reading "Ravi Kumar" while every one of these filters looked for "ravi". That
staff member saw an EMPTY leads list, an empty My Leads, and an empty sales
pipeline — their whole book of work, invisible.

Production has no display_name set on any user today, so nothing changes hands
on this deploy. The `ravi` fixture below is the case production cannot yet
exhibit and is one operator action away from.

FAIL CLOSED
-----------
owner_filter(model, None) is false(). An unresolvable ?staff= shows nothing
rather than falling back to a name match that would ignore the FK regime.
Approved policy for this batch.

BOTH REGIMES
------------
Every behavioural test runs under STAFF_IDENTITY_READ_FK off and on. A
dual-read helper exercised in one regime is untested in the other.

H4
--
crm_leads and crm_my_leads (and crm_staff_dashboard's tenant line) move from
getattr(current_user,'tenant_id') to _actor_tenant_id(), which honours
session['impersonate_tenant_id']. crm_leads_export ALREADY used
_actor_tenant_id(), so the list and its export were resolving the tenant by
different rules.

Import isolation follows test_deactivation_guard_rc23e1_b3.py.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23e1b1a_owner.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc23e1b1a-admin-key")
os.environ.setdefault("SECRET_KEY", "rc23e1b1a-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc23e1b1a-broadcast")
os.environ["AUTH_MODE"] = "SESSION_ONLY"
os.environ.setdefault("PRIMARY_TENANT_ID", "t-ox")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from werkzeug.security import generate_password_hash                     # noqa: E402
from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import Tenant, User, ConversationState                  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX = "t-ox"
OTHER = "t-other"

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
        kiran = _mk(OX, "Kiran")
        # THE CASE PRODUCTION CANNOT YET EXHIBIT: username != display_label.
        ravi = _mk(OX, "RAVI", display_name="Ravi Kumar")
        admin = _mk(OX, "admin_ox", role="ADMIN")
        other_staff = _mk(OTHER, "Anju")
        ids = {"anju": anju.id, "kiran": kiran.id, "ravi": ravi.id,
               "admin": admin.id, "other_staff": other_staff.id}

        def lead(phone, tenant, staff, uid, status="Lead"):
            db.session.add(ConversationState(
                phone=phone, tenant_id=tenant, name=f"L{phone[-3:]}",
                lead_status=status, assigned_staff=staff, assigned_user_id=uid))

        lead("919300000001", OX, "Anju", ids["anju"])
        lead("919300000002", OX, "anju", ids["anju"])          # case variant
        lead("919300000003", OX, "Kiran", ids["kiran"])
        # Ravi's leads carry his DISPLAY LABEL, as the dropdown produces.
        lead("919300000004", OX, "Ravi Kumar", ids["ravi"])
        lead("919300000005", OX, "Ravi Kumar", ids["ravi"])
        lead("919300000006", OX, None, None)                    # unassigned
        # A lead owned by the ADMIN. Without this, a fail-open bug that falls
        # back to current_user is invisible: the admin owns nothing, so the
        # screen looks empty either way. Mutation M4 survived until this
        # existed.
        lead("919300000008", OX, "admin_ox", ids["admin"])
        lead("919300000077", OTHER, "Anju", ids["other_staff"])  # other tenant
        db.session.commit()
        yield ids
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def phones(html):
    import re
    return set(re.findall(r"9193000000\d\d", html))


# ═══ the defect: display_name != username ════════════════════════════════════

class TestDisplayLabelOwnership:

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_staff_with_a_display_name_sees_their_leads(self, seeded, regime):
        """THE defect. Ravi's leads read 'Ravi Kumar'; the old filter looked
        for 'ravi' and returned nothing at all."""
        r = client(seeded["ravi"]).get("/crm/leads")
        assert r.status_code == 200
        got = phones(r.get_data(as_text=True))
        assert "919300000004" in got
        assert "919300000005" in got

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_that_staff_sees_only_their_own(self, seeded, regime):
        got = phones(client(seeded["ravi"]).get("/crm/leads").get_data(as_text=True))
        assert "919300000001" not in got, "saw Anju's lead"
        assert "919300000003" not in got, "saw Kiran's lead"
        assert "919300000006" not in got, "saw an unassigned lead"

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_my_leads_honours_the_display_label(self, seeded, regime):
        got = phones(client(seeded["ravi"]).get("/crm/my-leads").get_data(as_text=True))
        assert {"919300000004", "919300000005"} <= got


# ═══ ownership isolation, staff to staff ═════════════════════════════════════

class TestStaffIsolation:

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_staff_sees_only_own_leads_on_the_list(self, seeded, regime):
        got = phones(client(seeded["kiran"]).get("/crm/leads").get_data(as_text=True))
        assert got == {"919300000003"}

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_case_variants_still_belong_to_their_owner(self, seeded, regime):
        """Anju owns 'Anju' and 'anju'. Both regimes must return both."""
        got = phones(client(seeded["anju"]).get("/crm/leads").get_data(as_text=True))
        assert {"919300000001", "919300000002"} <= got
        assert "919300000003" not in got

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_admin_sees_every_lead_in_the_tenant(self, seeded, regime):
        got = phones(client(seeded["admin"]).get("/crm/leads").get_data(as_text=True))
        assert {"919300000001", "919300000003", "919300000004",
                "919300000006"} <= got

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_no_cross_tenant_leakage(self, seeded, regime):
        """The other tenant has its own 'Anju' with a lead."""
        for uid in ("anju", "admin"):
            got = phones(client(seeded[uid]).get("/crm/leads").get_data(as_text=True))
            assert "919300000077" not in got, uid

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_staff_cannot_export_at_all(self, seeded, regime):
        """/crm/leads/export is @admin_required — STAFF gets 403. The export's
        "same STAFF ownership rule" docstring describes _build_leads_query's
        contract, not a path a STAFF user can actually reach."""
        assert client(seeded["kiran"]).get("/crm/leads/export").status_code == 403

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_admin_export_is_scoped_identically_to_the_admin_list(self, seeded, regime):
        """An export broader than the list is a data leak, not a cosmetic bug.
        Both now resolve the tenant through _actor_tenant_id() (H4)."""
        listed = phones(client(seeded["admin"]).get("/crm/leads").get_data(as_text=True))
        exported = phones(client(seeded["admin"]).get(
            "/crm/leads/export").get_data(as_text=True))
        assert exported == listed
        assert "919300000077" not in exported, "exported another tenant's lead"


# ═══ fail closed ═════════════════════════════════════════════════════════════

class TestFailClosed:

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_unresolvable_staff_param_shows_nothing(self, seeded, regime):
        r = client(seeded["admin"]).get("/crm/my-leads?staff=Ghost")
        assert r.status_code == 200
        assert not phones(r.get_data(as_text=True))

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_unresolvable_staff_does_not_fall_back_to_the_viewer(self, seeded, regime):
        """`resolve(...) or current_user` is the tempting shape and it FAILS
        OPEN: an unknown ?staff= would quietly show the admin their own leads
        as if they were someone else's. 919300000008 is the admin's own lead,
        and it must not appear under a name that resolves to nobody."""
        got = phones(client(seeded["admin"]).get(
            "/crm/my-leads?staff=Ghost").get_data(as_text=True))
        assert "919300000008" not in got
        assert got == set()

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_foreign_tenant_staff_name_shows_nothing(self, seeded, regime):
        """'Anju' exists in the other tenant too; resolution is tenant-scoped
        so an Oxford admin asking for a foreign user gets Oxford's Anju, never
        the other tenant's rows."""
        got = phones(client(seeded["admin"]).get(
            "/crm/my-leads?staff=Anju").get_data(as_text=True))
        assert "919300000077" not in got

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_admin_can_view_a_named_staff_member(self, seeded, regime):
        got = phones(client(seeded["admin"]).get(
            "/crm/my-leads?staff=Ravi Kumar").get_data(as_text=True))
        assert {"919300000004", "919300000005"} <= got


# ═══ sales pipeline ══════════════════════════════════════════════════════════

class TestPipelineOwnership:

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_clause_is_none_for_non_staff(self, seeded, regime):
        from app.services import sales_pipeline_service as sps
        with _APP.app_context():
            actor = {"source": "SESSION", "role": "ADMIN", "username": "admin_ox"}
            assert sps._staff_ownership_clause(actor, OX) is None

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_clause_is_a_predicate_for_staff(self, seeded, regime):
        from app.services import sales_pipeline_service as sps
        with _APP.app_context():
            actor = {"source": "SESSION", "role": "STAFF", "username": "Kiran"}
            assert sps._staff_ownership_clause(actor, OX) is not None

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_clause_fails_closed_for_an_unknown_staff(self, seeded, regime):
        """An identity we cannot establish sees nothing, not everything."""
        from app.services import sales_pipeline_service as sps
        from app.models import ConversationState
        with _APP.app_context():
            actor = {"source": "SESSION", "role": "STAFF", "username": "Nobody"}
            clause = sps._staff_ownership_clause(actor, OX)
            assert clause is not None
            n = ConversationState.query.filter(clause).count()
            assert n == 0

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_clause_requires_a_tenant(self, seeded, regime):
        """Without a tenant the name cannot be resolved, so it fails closed."""
        from app.services import sales_pipeline_service as sps
        from app.models import ConversationState
        with _APP.app_context():
            actor = {"source": "SESSION", "role": "STAFF", "username": "Kiran"}
            clause = sps._staff_ownership_clause(actor, None)
            assert ConversationState.query.filter(clause).count() == 0

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_clause_matches_the_display_label_owner(self, seeded, regime):
        from app.services import sales_pipeline_service as sps
        from app.models import ConversationState
        with _APP.app_context():
            actor = {"source": "SESSION", "role": "STAFF", "username": "RAVI"}
            clause = sps._staff_ownership_clause(actor, OX)
            n = ConversationState.query.filter(
                ConversationState.tenant_id == OX).filter(clause).count()
            assert n == 2


# ═══ structural ══════════════════════════════════════════════════════════════

class TestStructure:

    def _admin(self):
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            return ast.parse(fh.read())

    def _fn(self, name, tree=None):
        tree = tree or self._admin()
        return ast.unparse(next(n for n in ast.walk(tree)
                                if isinstance(n, ast.FunctionDef) and n.name == name))

    def test_all_three_filters_use_owner_filter(self):
        assert "owner_filter" in self._fn("_build_leads_query")
        assert "owner_filter" in self._fn("crm_my_leads")
        with open(os.path.join(ROOT, "app/services/sales_pipeline_service.py"),
                  encoding="utf-8") as fh:
            sps = ast.parse(fh.read())
        assert "owner_filter" in self._fn("_staff_ownership_clause", sps)

    def test_hand_rolled_name_comparison_is_gone(self):
        for name in ("_build_leads_query", "crm_my_leads"):
            src = self._fn(name)
            assert "func.lower(func.trim(ConversationState.assigned_staff))" not in src, name

    def test_consumers_never_read_the_flag(self):
        for name in ("_build_leads_query", "crm_my_leads", "crm_leads"):
            src = self._fn(name)
            assert "STAFF_IDENTITY_READ_FK" not in src
            assert "read_fk_enabled" not in src

    def test_h4_idiom_gone_from_the_migrated_routes(self):
        for name in ("crm_leads", "crm_my_leads", "crm_staff_dashboard"):
            src = self._fn(name)
            assert "getattr(current_user, 'tenant_id'" not in src, name
            assert "_actor_tenant_id()" in src, name

    def test_ownership_clause_requires_tenant_at_every_call_site(self):
        with open(os.path.join(ROOT, "app/services/sales_pipeline_service.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        assert "_staff_ownership_clause(actor)" not in src
        # 3 = the def line plus the two call sites. Counting the raw string
        # without allowing for the definition is what made the first version
        # of this test fail against correct code.
        assert src.count("_staff_ownership_clause(actor, tenant_id)") == 3
        assert src.count("def _staff_ownership_clause(actor, tenant_id)") == 1

    def test_h4_is_now_fully_closed(self):
        """INVERTED by H4-b, which is what the previous version asked for.

        This asserted H4 was still open elsewhere — 14 route-level sites, not
        the "two idioms" my Batch 1 discovery claimed. Batch 1a closed three,
        H4-a four, H4-b the last seven. Only _actor_tenant_id (which defines
        the idiom) and check_billing_status (its own three-way resolution)
        may still contain it.
        """
        tree = self._admin()
        remaining = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef):
                s = ast.unparse(n)
                if "getattr(current_user, 'tenant_id'" in s:
                    remaining.add(n.name)
        assert remaining == {"_actor_tenant_id", "check_billing_status"},             f"H4 sites remain: {sorted(remaining)}"

    def test_no_schema_or_migration_change(self):
        versions = os.path.join(ROOT, "migrations", "versions")
        assert not [f for f in os.listdir(versions)
                    if "rc23e" in f.lower() or "h4" in f.lower()]

    def test_flag_default_is_still_off(self):
        from app import flags
        before = os.environ.pop("STAFF_IDENTITY_READ_FK", None)
        try:
            assert flags.staff_identity_read_fk_enabled() is False
        finally:
            if before is not None:
                os.environ["STAFF_IDENTITY_READ_FK"] = before
