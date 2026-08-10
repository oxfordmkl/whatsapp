"""Phase RC2.3E-1 Batch 4 — case-sensitive aggregation joins.

Batch 4's discovery found that MOST of the ~12 calculate_* aggregations
already key on normalize_staff_name() and are therefore consistent in both
regimes. Two did not, and both rendered wrong numbers in production:

  crm_staff_performance_detail
      staff_metrics was keyed by the raw display label and leads were matched
      with `lead.assigned_staff not in staff_metrics` — an EXACT string
      compare. Any lead stored in another spelling was silently skipped.
      Production: Kiran read 24 against a true 27, Anju 26 against 27 —
      4 lead-rows invisible.

  calculate_revenue_analytics
      staff_agg was keyed by (staff or "").strip() — case-sensitive, so one
      person became several rows. Production rendered FIVE staff rows for
      THREE people ('Kiran' 24 + 'kiran' 3, 'Anju' 26 + 'anju' 1), each with
      its own admissions, and the Top Performing Staff KPI was chosen from
      the split figures.

WHY THE OTHER AGGREGATIONS ARE NOT TOUCHED
------------------------------------------
They already normalize both sides of their join, so they are correct today
and identical under either regime. Rewriting a dozen large functions for no
behavioural gain would add risk, not remove it. This suite pins that claim so
a future phase cannot quietly reintroduce a raw-key join.

BOTH REGIMES
------------
These aggregations key on NAMES by construction — they build buckets for
display, not ownership predicates — so they read the same under either flag
state. The regime parametrization asserts exactly that: the numbers must not
move when STAFF_IDENTITY_READ_FK flips.

Import isolation follows test_staff_dashboard_rc23e1_b1b.py.
"""
import ast
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_rc23e1b4_agg.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "rc23e1b4-admin-key")
os.environ.setdefault("SECRET_KEY", "rc23e1b4-secret-key")
os.environ.setdefault("BROADCAST_API_KEY", "rc23e1b4-broadcast")
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
    """Mirrors the production shape: one owner, several spellings."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, nm in ((OX, "Oxford"), (OTHER, "Other")):
            db.session.add(Tenant(id=tid, name=nm, slug=tid, status="ACTIVE",
                                  billing_exempt=True))
        db.session.commit()

        kiran = _mk(OX, "Kiran")
        anju = _mk(OX, "Anju")
        # A LOWERCASE display label — production has exactly this shape
        # (username NIBU01, display label 'nibu'). normalize_staff_name()
        # title-cases, so if the metrics dict were keyed by the NORMALIZED
        # name it would read 'Nibu' while the picker offers 'nibu', and the
        # template would render a row the operator cannot select. Without this
        # fixture every label is already title-case and that bug is invisible.
        nibu = _mk(OX, "NIBU01", display_name="nibu")
        admin = _mk(OX, "admin_ox", role="ADMIN")
        ids = {"kiran": kiran.id, "anju": anju.id, "nibu": nibu.id,
               "admin": admin.id}

        def lead(phone, staff, uid, admitted=False):
            db.session.add(ConversationState(
                phone=phone, tenant_id=OX, name=f"L{phone[-3:]}",
                lead_status="Lead", assigned_staff=staff, assigned_user_id=uid,
                lead_score=50, is_admitted=admitted))

        # Kiran: 3 spellings, 4 leads, 2 admitted.
        lead("919500000001", "Kiran", ids["kiran"], admitted=True)
        lead("919500000002", "kiran", ids["kiran"], admitted=True)
        lead("919500000003", "  KIRAN ", ids["kiran"])
        lead("919500000004", "Kiran", ids["kiran"])
        # Anju: 2 spellings, 2 leads.
        lead("919500000005", "Anju", ids["anju"])
        lead("919500000006", "anju", ids["anju"])
        # nibu: 2 leads stored under the lowercase label the dropdown offers.
        lead("919500000008", "nibu", ids["nibu"])
        lead("919500000009", "NIBU", ids["nibu"], admitted=True)
        # Unassigned.
        lead("919500000007", None, None)
        db.session.commit()
        yield ids
        db.session.remove()


def client(uid):
    c = _APP.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def context(uid, url):
    from flask import template_rendered
    captured = {}

    def record(sender, template, context, **extra):
        captured.update(context)

    template_rendered.connect(record, _APP)
    try:
        resp = client(uid).get(url, follow_redirects=True)
    finally:
        template_rendered.disconnect(record, _APP)
    assert resp.status_code == 200, url
    return captured


# ═══ crm_staff_performance_detail ════════════════════════════════════════════

class TestPerformanceDetail:
    URL = "/crm/staff-performance-detail"

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_case_variants_are_counted(self, seeded, regime):
        """THE defect: 'kiran' and '  KIRAN ' were skipped by the exact match,
        so Kiran's 4 leads rendered as 2."""
        m = context(seeded["admin"], self.URL)["metrics"]
        assert m["Kiran"]["assigned_leads"] == 4
        assert m["Anju"]["assigned_leads"] == 2

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_admissions_follow_the_same_key(self, seeded, regime):
        """One of Kiran's admissions is on the 'kiran' spelling."""
        m = context(seeded["admin"], self.URL)["metrics"]
        assert m["Kiran"]["admissions"] == 2

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_conversion_uses_the_corrected_denominator(self, seeded, regime):
        """2/4 = 50.0. The old undercount gave 2/2 = 100.0 — a number an
        operator would have acted on."""
        m = context(seeded["admin"], self.URL)["metrics"]
        assert m["Kiran"]["conversion"] == 50.0

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_unassigned_leads_are_not_attributed(self, seeded, regime):
        m = context(seeded["admin"], self.URL)["metrics"]
        assert sum(v["assigned_leads"] for v in m.values()) == 8

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_lowercase_display_label_keeps_its_own_key(self, seeded, regime):
        """The production shape: display label 'nibu'. The metrics key must
        stay 'nibu' — what the picker offers — while still matching leads
        stored as 'nibu' AND 'NIBU'."""
        ctx = context(seeded["admin"], self.URL)
        assert "nibu" in ctx["metrics"], "key was normalized away from the picker"
        assert "Nibu" not in ctx["metrics"]
        assert ctx["metrics"]["nibu"]["assigned_leads"] == 2
        assert ctx["metrics"]["nibu"]["admissions"] == 1

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_metrics_keys_stay_display_labels(self, seeded, regime):
        """The template renders these keys, so they must remain what the
        picker shows — only the LOOKUP was normalized."""
        ctx = context(seeded["admin"], self.URL)
        assert set(ctx["metrics"]) == set(ctx["active_staff"])

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_numbers_are_identical_in_both_regimes(self, seeded, regime):
        m = context(seeded["admin"], self.URL)["metrics"]
        assert m["Kiran"]["assigned_leads"] == 4
        assert m["Kiran"]["admissions"] == 2
        assert m["Anju"]["assigned_leads"] == 2


# ═══ calculate_revenue_analytics ═════════════════════════════════════════════

class TestRevenueAnalytics:
    URL = "/crm/revenue-analytics"

    def _rows(self, uid):
        data = context(uid, self.URL)["data"]
        return {r["name"] if isinstance(r, dict) and "name" in r else r[0]: r
                for r in data["staff_rows"]}

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_one_person_is_one_row(self, seeded, regime):
        """Production rendered 5 rows for 3 people."""
        rows = self._rows(seeded["admin"])
        assert "Kiran" in rows
        assert "kiran" not in rows
        assert "anju" not in rows

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_merged_row_carries_every_lead(self, seeded, regime):
        rows = self._rows(seeded["admin"])
        k = rows["Kiran"]
        assert k["assigned"] == 4
        assert k["admissions"] == 2

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_unassigned_still_buckets_as_unassigned(self, seeded, regime):
        """normalize_staff_name('') is 'Unassigned' — the same label the old
        `or "Unassigned"` produced."""
        assert "Unassigned" in self._rows(seeded["admin"])

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_row_count_is_the_number_of_real_people(self, seeded, regime):
        rows = self._rows(seeded["admin"])
        assert len([k for k in rows if k != "Unassigned"]) == 3
        # 7 raw spellings across the fixture ('Kiran','kiran','  KIRAN ',
        # 'Anju','anju','nibu','NIBU') collapse to 3 people.
        assert set(rows) - {"Unassigned"} == {"Kiran", "Anju", "Nibu"}

    @pytest.mark.parametrize("regime", REGIMES, indirect=True)
    def test_top_staff_is_picked_from_merged_figures(self, seeded, regime):
        """The KPI was previously chosen from split rows."""
        data = context(seeded["admin"], self.URL)["data"]
        assert data["top_staff"].strip().lower().startswith("kiran")


# ═══ structural ══════════════════════════════════════════════════════════════

class TestStructure:

    def _tree(self):
        with open(os.path.join(ROOT, "app", "routes", "admin.py"),
                  encoding="utf-8") as fh:
            return ast.parse(fh.read())

    def _fn(self, name):
        return ast.unparse(next(n for n in ast.walk(self._tree())
                                if isinstance(n, ast.FunctionDef) and n.name == name))

    def test_performance_detail_matches_normalized(self):
        src = self._fn("crm_staff_performance_detail")
        assert "_by_norm" in src
        assert "s = lead.assigned_staff" not in src

    def test_performance_detail_scopes_tasks_to_the_tenant(self):
        assert "get_all_tasks(_tid)" in self._fn("crm_staff_performance_detail")

    def test_revenue_buckets_are_normalized(self):
        src = self._fn("calculate_revenue_analytics")
        assert "normalize_staff_name(staff)" in src
        assert '(staff or \'\').strip() or \'Unassigned\'' not in src

    def test_no_aggregation_keys_on_a_raw_name(self):
        """The claim this batch rests on: every remaining aggregation already
        normalizes. If a future phase adds a raw-key join, this fails."""
        tree = self._tree()
        offenders = []
        for name in ("calculate_staff_performance", "calculate_staff_performance_fixed",
                     "calculate_admission_analytics", "calculate_crm_health",
                     "calculate_action_center", "calculate_operations",
                     "calculate_intelligence", "calculate_automation_intelligence",
                     "calculate_workload_scoring", "calculate_revenue_analytics",
                     "crm_staff_performance_detail"):
            src = ast.unparse(next(n for n in ast.walk(tree)
                                   if isinstance(n, ast.FunctionDef) and n.name == name))
            if "normalize_staff_name" not in src:
                offenders.append(name)
        assert not offenders, f"aggregations keying on a raw name: {offenders}"

    def test_consumers_never_read_the_flag(self):
        for name in ("crm_staff_performance_detail", "calculate_revenue_analytics"):
            src = self._fn(name)
            assert "STAFF_IDENTITY_READ_FK" not in src
            assert "read_fk_enabled" not in src

    def test_h4_remains_open_here(self):
        """HONEST RECORD: crm_staff_performance_detail still uses the unsafe
        tenant idiom. H4 was approved for Batch 1a only and is tracked as its
        own item across 11 routes. Update this when H4 lands — do not delete
        it to make the file look clean."""
        assert "getattr(current_user, 'tenant_id'" in self._fn("crm_staff_performance_detail")

    def test_no_schema_or_migration_change(self):
        """Compared against git rather than by filename substring.

        The first version of this test looked for "b4" in the filename and
        tripped on 5d03593d42b4_add_users_table.py — an alembic revision hash.
        Substring matching over generated names is exactly the false-positive
        trap this codebase keeps hitting; ask git what actually changed.
        """
        import subprocess
        out = subprocess.run(
            ["git", "status", "--porcelain", "--", "migrations/"],
            cwd=ROOT, capture_output=True, text=True).stdout.strip()
        assert out == "", f"migrations/ is not clean:\n{out}"

    def test_flag_default_is_still_off(self):
        from app import flags
        before = os.environ.pop("STAFF_IDENTITY_READ_FK", None)
        try:
            assert flags.staff_identity_read_fk_enabled() is False
        finally:
            if before is not None:
                os.environ["STAFF_IDENTITY_READ_FK"] = before
