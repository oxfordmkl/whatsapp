"""
Phase 10.7 — Sales Pipeline service tests.

Exercises the real service against a real database. The thing under test is
query behaviour — LEFT JOIN semantics, tenant scoping, STAFF ownership inside
an aggregate — none of which a source-level check could prove.

Import isolation matches test_pipeline_foundation_10_6.py: tests/conftest.py
installs stub modules for `app`, `app.models` and `app.config` at collection
time, so the real package must be re-imported here. Safe because this suite is
run file-by-file (the documented practice); see that file for the full note.
"""
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_10_7_test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("ADMIN_KEY", "testkey")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ.setdefault("BROADCAST_API_KEY", "testbroadcast")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                             # noqa: E402
from app.extensions import db                                          # noqa: E402
from app.models import (                                               # noqa: E402
    ConversationState, PipelineDefinition, PipelineStage, Tenant,
    LEAD_STATUSES, SALES_PIPELINE_KEY,
)
from app.services.sales_pipeline_seed import SalesPipelineSeeder       # noqa: E402
from app.services import sales_pipeline_service as sps                 # noqa: E402

TENANT = "t-10-7"
OTHER = "t-10-7-other"
EMPTY = "t-10-7-empty"          # tenant with a pipeline but no leads

ADMIN = {"authenticated": True, "username": "admin", "role": "ADMIN", "source": "SESSION"}
STAFF_ANJU = {"authenticated": True, "username": "Anju", "role": "STAFF", "source": "SESSION"}
STAFF_RAVI = {"authenticated": True, "username": "Ravi", "role": "STAFF", "source": "SESSION"}

_APP = create_app()


@pytest.fixture()
def ctx():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        for tid, name in ((TENANT, "A"), (OTHER, "B"), (EMPTY, "C")):
            db.session.add(Tenant(id=tid, name=name, slug=tid))
        db.session.commit()
        # Phase RC2.3E-1 Batch 1a: staff must exist as User rows.
        #
        # This fixture modelled a PRE-RC2.2 world in which staff existed only
        # as strings on the lead. Since RC2.2 the staff directory IS the User
        # table, and a session STAFF actor has a User row by definition —
        # they authenticated as one. _staff_ownership_clause now resolves the
        # actor to that row and fails CLOSED when it cannot, so string-only
        # staff correctly resolve to nothing.
        #
        # STAFF_NOBODY below deliberately keeps no User row: that is the
        # unresolvable case, and it must still see zero leads.
        from werkzeug.security import generate_password_hash
        from app.models import User
        for uname in ("Anju", "Ravi"):
            db.session.add(User(
                username=uname, email=f"{uname}@pipeline.test",
                password_hash=generate_password_hash("pw"), role="STAFF",
                tenant_id=TENANT, is_active=True,
                require_password_change=False))
        db.session.commit()
        yield
        db.session.remove()


def seed_all():
    SalesPipelineSeeder(dry_run=False).run()


def make_lead(phone, tenant=TENANT, status="Lead", staff=None, stage="new", score=0):
    lead = ConversationState(
        phone=phone, name=f"L{phone[-4:]}", tenant_id=tenant,
        stage=stage, course="", goal="", batch_time="", offer_course="",
        last_msg="", last_text="", lead_status=status,
        assigned_staff=staff, lead_score=score,
    )
    db.session.add(lead)
    db.session.commit()
    return lead


def by_name(summary):
    return {s["display_name"]: s for s in summary}


# ── Stage ordering ───────────────────────────────────────────────────────────

class TestStageOrdering:
    def test_ordered_by_order_index(self, ctx):
        seed_all()
        s = sps.get_pipeline_summary(TENANT, ADMIN)
        assert [x["order_index"] for x in s] == sorted(x["order_index"] for x in s)

    def test_order_matches_lead_statuses_sequence(self, ctx):
        seed_all()
        s = sps.get_pipeline_summary(TENANT, ADMIN)
        assert [x["display_name"] for x in s] == list(LEAD_STATUSES)

    def test_entry_stage_is_first(self, ctx):
        seed_all()
        s = sps.get_pipeline_summary(TENANT, ADMIN)
        assert s[0]["display_name"] == "Lead"


# ── Stage counts, incl. zero-count stages ────────────────────────────────────

class TestStageCounts:
    def test_counts_are_correct(self, ctx):
        seed_all()
        for i in range(3):
            make_lead(f"91900000000{i}", status="Lead")
        make_lead("919000000010", status="Interested")
        d = by_name(sps.get_pipeline_summary(TENANT, ADMIN))
        assert d["Lead"]["lead_count"] == 3
        assert d["Interested"]["lead_count"] == 1

    def test_zero_count_stages_still_appear(self, ctx):
        """The whole reason the query drives FROM pipeline_stages. Grouping
        from the lead side would drop every empty stage — exactly the part of
        a pipeline an operator most needs to see."""
        seed_all()
        make_lead("919000000020", status="Lead")
        s = sps.get_pipeline_summary(TENANT, ADMIN)
        assert len(s) == len(LEAD_STATUSES)
        d = by_name(s)
        assert d["Lost"]["lead_count"] == 0
        assert d["Negotiation"]["lead_count"] == 0

    def test_share_pct_sums_to_100(self, ctx):
        seed_all()
        for i in range(4):
            make_lead(f"91900000003{i}", status="Lead")
        make_lead("919000000040", status="Enrolled")
        s = sps.get_pipeline_summary(TENANT, ADMIN)
        assert round(sum(x["share_pct"] for x in s), 1) == 100.0

    def test_share_pct_values(self, ctx):
        seed_all()
        for i in range(3):
            make_lead(f"91900000005{i}", status="Lead")
        make_lead("919000000060", status="Enrolled")
        d = by_name(sps.get_pipeline_summary(TENANT, ADMIN))
        assert d["Lead"]["share_pct"] == 75.0
        assert d["Enrolled"]["share_pct"] == 25.0

    def test_counts_use_sales_stage_id_not_lead_status_string(self, ctx):
        """Clearing the link must drop the lead from the stage count even
        though the legacy string still says 'Lead'."""
        seed_all()
        lead = make_lead("919000000070", status="Lead")
        assert by_name(sps.get_pipeline_summary(TENANT, ADMIN))["Lead"]["lead_count"] == 1
        lead.sales_stage_id = None
        db.session.commit()
        assert lead._lead_status == "Lead"
        assert by_name(sps.get_pipeline_summary(TENANT, ADMIN))["Lead"]["lead_count"] == 0


# ── Empty pipeline handling ──────────────────────────────────────────────────

class TestEmptyPipeline:
    def test_tenant_with_pipeline_but_no_leads(self, ctx):
        seed_all()
        s = sps.get_pipeline_summary(EMPTY, ADMIN)
        assert len(s) == len(LEAD_STATUSES)
        assert all(x["lead_count"] == 0 for x in s)
        assert all(x["share_pct"] == 0.0 for x in s)   # no ZeroDivisionError

    def test_metrics_on_empty_pipeline(self, ctx):
        seed_all()
        m = sps.get_conversion_metrics(sps.get_pipeline_summary(EMPTY, ADMIN))
        assert m["total_leads"] == 0
        assert m["win_rate"] is None
        assert m["has_stages"] is True

    def test_tenant_without_any_pipeline(self, ctx):
        assert sps.get_pipeline_summary(TENANT, ADMIN) == []
        assert sps.get_conversion_metrics([])["has_stages"] is False

    def test_no_tenant_returns_empty(self, ctx):
        assert sps.get_pipeline_summary(None, ADMIN) == []


# ── Won / lost calculations ──────────────────────────────────────────────────

class TestWonLost:
    def test_categories_assigned(self, ctx):
        seed_all()
        d = by_name(sps.get_pipeline_summary(TENANT, ADMIN))
        assert d["Enrolled"]["stage_category"] == "won"
        assert d["Lost"]["stage_category"] == "lost"
        assert d["Not Interested"]["stage_category"] == "lost"
        assert d["Lead"]["stage_category"] == "open"

    def test_won_lost_open_counts(self, ctx):
        seed_all()
        make_lead("919000000080", status="Lead")
        make_lead("919000000081", status="Enrolled")
        make_lead("919000000082", status="Lost")
        make_lead("919000000083", status="Not Interested")
        m = sps.get_conversion_metrics(sps.get_pipeline_summary(TENANT, ADMIN))
        assert (m["open_count"], m["won_count"], m["lost_count"]) == (1, 1, 2)
        assert m["closed_count"] == 3

    def test_win_rate_is_share_of_closed_not_total(self, ctx):
        seed_all()
        for i in range(6):
            make_lead(f"9190000009{i}", status="Lead")     # still open
        make_lead("919000000100", status="Enrolled")
        make_lead("919000000101", status="Lost")
        m = sps.get_conversion_metrics(sps.get_pipeline_summary(TENANT, ADMIN))
        assert m["win_rate"] == 50.0, "1 won of 2 closed — open leads must not dilute it"

    def test_win_rate_none_when_nothing_closed(self, ctx):
        seed_all()
        make_lead("919000000110", status="Lead")
        m = sps.get_conversion_metrics(sps.get_pipeline_summary(TENANT, ADMIN))
        assert m["win_rate"] is None
        assert m["lost_recorded"] is False

    def test_lost_recorded_flag_drives_the_caveat(self, ctx):
        """With lost=0 the win rate reads 100%, which is true and misleading;
        the UI needs this flag to caveat it."""
        seed_all()
        make_lead("919000000120", status="Enrolled")
        m = sps.get_conversion_metrics(sps.get_pipeline_summary(TENANT, ADMIN))
        assert m["win_rate"] == 100.0
        assert m["lost_recorded"] is False


# ── Tenant isolation ─────────────────────────────────────────────────────────

class TestTenantIsolation:
    def test_summary_counts_only_own_tenant(self, ctx):
        seed_all()
        make_lead("919000000130", tenant=TENANT, status="Lead")
        for i in range(5):
            make_lead(f"91900000014{i}", tenant=OTHER, status="Lead")
        assert by_name(sps.get_pipeline_summary(TENANT, ADMIN))["Lead"]["lead_count"] == 1
        assert by_name(sps.get_pipeline_summary(OTHER, ADMIN))["Lead"]["lead_count"] == 5

    def test_stage_ids_differ_per_tenant(self, ctx):
        seed_all()
        a = by_name(sps.get_pipeline_summary(TENANT, ADMIN))["Lead"]["stage_id"]
        b = by_name(sps.get_pipeline_summary(OTHER, ADMIN))["Lead"]["stage_id"]
        assert a != b

    def test_get_stage_rejects_other_tenants_stage(self, ctx):
        """stage_id arrives from the URL and must never be trusted."""
        seed_all()
        other_id = by_name(sps.get_pipeline_summary(OTHER, ADMIN))["Lead"]["stage_id"]
        assert sps.get_stage(OTHER, other_id) is not None
        assert sps.get_stage(TENANT, other_id) is None

    def test_get_stage_rejects_ai_funnel_stage(self, ctx):
        """Only the 'sales' pipeline resolves here — not the bot's."""
        seed_all()
        bot_pipe = PipelineDefinition(tenant_id=TENANT, internal_key="legacy_compat",
                                      name="Bot", is_default=False, is_active=True)
        db.session.add(bot_pipe); db.session.flush()
        bot_stage = PipelineStage(pipeline_id=bot_pipe.id, internal_key="goal_selection",
                                  display_name="Goal Selection", stage_category="open",
                                  order_index=1, is_entry=False, is_terminal=False, is_active=True)
        db.session.add(bot_stage); db.session.commit()
        assert sps.get_stage(TENANT, bot_stage.id) is None

    def test_stage_leads_scoped_to_tenant(self, ctx):
        seed_all()
        make_lead("919000000150", tenant=TENANT, status="Lead")
        make_lead("919000000151", tenant=OTHER, status="Lead")
        sid = by_name(sps.get_pipeline_summary(TENANT, ADMIN))["Lead"]["stage_id"]
        page = sps.get_stage_leads(TENANT, sid, ADMIN)
        assert page.total == 1
        assert page.items[0].tenant_id == TENANT


# ── STAFF ownership — applied INSIDE the aggregate ───────────────────────────

class TestStaffOwnership:
    def test_staff_counts_exclude_other_staffs_leads(self, ctx):
        """A tenant-wide count is a leak even when no individual lead shows."""
        seed_all()
        for i in range(7):
            make_lead(f"91900000016{i}", status="Lead", staff="Ravi")
        make_lead("919000000170", status="Lead", staff="Anju")
        assert by_name(sps.get_pipeline_summary(TENANT, ADMIN))["Lead"]["lead_count"] == 8
        assert by_name(sps.get_pipeline_summary(TENANT, STAFF_ANJU))["Lead"]["lead_count"] == 1
        assert by_name(sps.get_pipeline_summary(TENANT, STAFF_RAVI))["Lead"]["lead_count"] == 7

    def test_staff_share_pct_is_relative_to_their_own_pipeline(self, ctx):
        seed_all()
        make_lead("919000000180", status="Lead", staff="Anju")
        make_lead("919000000181", status="Enrolled", staff="Anju")
        for i in range(6):
            make_lead(f"91900000019{i}", status="Lead", staff="Ravi")
        d = by_name(sps.get_pipeline_summary(TENANT, STAFF_ANJU))
        assert d["Lead"]["share_pct"] == 50.0
        assert d["Enrolled"]["share_pct"] == 50.0

    def test_staff_still_sees_all_stages_including_empty(self, ctx):
        """The ownership predicate lives in the JOIN, not a WHERE — a WHERE on
        the outer-joined table would drop unmatched stages for STAFF."""
        seed_all()
        make_lead("919000000200", status="Lead", staff="Anju")
        s = sps.get_pipeline_summary(TENANT, STAFF_ANJU)
        assert len(s) == len(LEAD_STATUSES)

    def test_staff_with_no_leads_sees_zeroed_pipeline(self, ctx):
        seed_all()
        make_lead("919000000210", status="Lead", staff="Ravi")
        s = sps.get_pipeline_summary(TENANT, STAFF_RAVI.copy() | {"username": "Nobody"})
        assert len(s) == len(LEAD_STATUSES)
        assert all(x["lead_count"] == 0 for x in s)

    def test_staff_ownership_is_case_and_space_insensitive(self, ctx):
        seed_all()
        make_lead("919000000220", status="Lead", staff="  anju  ")
        assert by_name(sps.get_pipeline_summary(TENANT, STAFF_ANJU))["Lead"]["lead_count"] == 1

    def test_stage_leads_filtered_for_staff(self, ctx):
        seed_all()
        make_lead("919000000230", status="Lead", staff="Anju")
        make_lead("919000000231", status="Lead", staff="Ravi")
        sid = by_name(sps.get_pipeline_summary(TENANT, ADMIN))["Lead"]["stage_id"]
        assert sps.get_stage_leads(TENANT, sid, ADMIN).total == 2
        assert sps.get_stage_leads(TENANT, sid, STAFF_ANJU).total == 1

    def test_admin_is_not_ownership_filtered(self, ctx):
        seed_all()
        make_lead("919000000240", status="Lead", staff="Ravi")
        make_lead("919000000241", status="Lead", staff=None)
        assert by_name(sps.get_pipeline_summary(TENANT, ADMIN))["Lead"]["lead_count"] == 2

    def test_no_actor_behaves_as_unfiltered(self, ctx):
        seed_all()
        make_lead("919000000250", status="Lead", staff="Ravi")
        assert by_name(sps.get_pipeline_summary(TENANT, None))["Lead"]["lead_count"] == 1


# ── Stage detail pagination ──────────────────────────────────────────────────

class TestStageLeads:
    def test_pagination_shape(self, ctx):
        seed_all()
        for i in range(30):
            make_lead(f"9190000030{i:02d}", status="Lead")
        sid = by_name(sps.get_pipeline_summary(TENANT, ADMIN))["Lead"]["stage_id"]
        page = sps.get_stage_leads(TENANT, sid, ADMIN, page=1, per_page=25)
        assert page.total == 30 and len(page.items) == 25 and page.pages == 2

    def test_second_page(self, ctx):
        seed_all()
        for i in range(30):
            make_lead(f"9190000031{i:02d}", status="Lead")
        sid = by_name(sps.get_pipeline_summary(TENANT, ADMIN))["Lead"]["stage_id"]
        assert len(sps.get_stage_leads(TENANT, sid, ADMIN, page=2, per_page=25).items) == 5

    def test_only_leads_of_that_stage(self, ctx):
        seed_all()
        make_lead("919000000260", status="Lead")
        make_lead("919000000261", status="Enrolled")
        sid = by_name(sps.get_pipeline_summary(TENANT, ADMIN))["Enrolled"]["stage_id"]
        page = sps.get_stage_leads(TENANT, sid, ADMIN)
        assert page.total == 1 and page.items[0].lead_status == "Enrolled"

    def test_missing_stage_returns_none(self, ctx):
        assert sps.get_stage_leads(TENANT, None, ADMIN) is None
        assert sps.get_stage_leads(None, 1, ADMIN) is None


# ── The AI funnel must be untouched ──────────────────────────────────────────

class TestBotPipelineUnaffected:
    def test_service_never_reads_bot_columns(self, ctx):
        """No AI-funnel attribute is accessed anywhere in the service.

        Uses AST rather than string matching: the module docstring legitimately
        *describes* pipeline_stage_id when explaining why it is avoided, and a
        substring check cannot tell prose from code. Walking Attribute nodes
        inspects only what actually executes.
        """
        import ast
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app", "services", "sales_pipeline_service.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        forbidden = {"pipeline_stage_id", "_stage"}
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        leaked = forbidden & used
        assert not leaked, f"service touches AI-funnel attribute(s): {leaked}"
        # `stage` alone is legitimate (PipelineStage.stage_category etc.), so
        # assert specifically that ConversationState.stage is never read.
        bad = [n for n in ast.walk(tree)
               if isinstance(n, ast.Attribute) and n.attr == "stage"
               and isinstance(n.value, ast.Name) and n.value.id == "ConversationState"]
        assert not bad, "service reads ConversationState.stage (AI funnel)"

    def test_bot_stage_unchanged_by_pipeline_reads(self, ctx):
        seed_all()
        lead = make_lead("919000000270", status="Lead", stage="demo_booked")
        before_stage, before_link = lead._stage, lead.pipeline_stage_id
        sps.get_pipeline_summary(TENANT, ADMIN)
        sid = by_name(sps.get_pipeline_summary(TENANT, ADMIN))["Lead"]["stage_id"]
        sps.get_stage_leads(TENANT, sid, ADMIN)
        db.session.refresh(lead)
        assert lead._stage == before_stage
        assert lead.pipeline_stage_id == before_link

    def test_sales_and_bot_stages_are_independent(self, ctx):
        seed_all()
        lead = make_lead("919000000280", status="Interested", stage="course_viewed")
        assert lead.sales_stage_id is not None
        assert lead.pipeline_stage_id is None
        assert lead.stage == "course_viewed"
        assert lead.lead_status == "Interested"
