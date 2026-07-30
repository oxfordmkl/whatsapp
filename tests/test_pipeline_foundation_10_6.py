"""
Phase 10.6 — Sales Pipeline foundation.

Exercises the real models against a real database, because the thing under
test IS the ORM adapter: a source-level check could not prove that
ConversationState(lead_status=...) still constructs, that .notin_() still
compiles to working SQL, or that the bot funnel is genuinely untouched.

Env is set before importing `app` — config.py validates several secrets at
import time. A file-backed SQLite database is used rather than :memory: so the
background follow-up thread cannot interfere with the test session.
"""
import os
import sys
import tempfile

import pytest

# ── Import isolation ─────────────────────────────────────────────────────────
# tests/conftest.py deliberately installs stub modules for `app`, `app.models`
# and `app.config` at collection time, so the memory-provider tests can inject
# collaborators without loading the real package. That stub makes
# `from app import create_app` fail with "unknown location" — the same reason
# the pre-existing test_legacy_completion_auth_16_5a7d.py cannot collect.
#
# This file tests the real ORM adapter, so it needs the genuine package: a
# source-level check could not prove that ConversationState(lead_status=...)
# still constructs, or that .notin_() still compiles to working SQL.
#
# The stubs are therefore dropped here and the real package imported. That is
# safe because this project already runs its suite FILE BY FILE (the documented
# practice for avoiding the known _p82d5_* alias collision), so nothing else is
# in this process. Running it inside a shared pytest session would restore the
# stubs for later files and is not supported — hence the explicit guard below.
for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_10_6_test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("ADMIN_KEY", "testkey")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ.setdefault("BROADCAST_API_KEY", "testbroadcast")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                      # noqa: E402
from app.extensions import db                   # noqa: E402
from app.models import (                        # noqa: E402
    ConversationState, PipelineDefinition, PipelineStage, Tenant,
    LEAD_STATUSES, LEAD_TERMINAL_STATUSES, SALES_PIPELINE_KEY,
)
from app.services.sales_pipeline_seed import SalesPipelineSeeder  # noqa: E402

TENANT = "t-10-6"
OTHER = "t-other"


# The app is built once. create_app() starts the follow-up scheduler thread,
# so building it per test would spawn one thread per test and hold connections
# open — which on Windows also locks the SQLite file.
_APP = create_app()


@pytest.fixture()
def ctx():
    """Fresh schema per test, without deleting the database file.

    drop_all/create_all is used rather than removing the file because the
    background scheduler keeps a connection open and Windows refuses to unlink
    a file that is still held.
    """
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=TENANT, name="Tenant A", slug="tenant-a"))
        db.session.add(Tenant(id=OTHER, name="Tenant B", slug="tenant-b"))
        db.session.commit()
        yield
        db.session.remove()


def make_lead(phone, tenant=TENANT, status="Lead", stage="new"):
    lead = ConversationState(
        phone=phone, name="Test", tenant_id=tenant,
        stage=stage, course="", goal="", batch_time="", offer_course="",
        last_msg="", last_text="", lead_status=status,
    )
    db.session.add(lead)
    db.session.commit()
    return lead


def seed(tenant=TENANT):
    """Seed one tenant's sales pipeline directly (bypassing the runner)."""
    s = SalesPipelineSeeder(dry_run=False)
    from app.services.sales_pipeline_seed import SalesPipelineReport
    r = SalesPipelineReport(tenant)
    pid = s._step1_pipeline(tenant, r)
    return s._step2_stages(pid, r), pid, r


# ── The constructor bridge (highest-risk regression) ─────────────────────────

class TestConstructorBridge:
    def test_lead_status_kwarg_still_accepted(self, ctx):
        """lead_status is now a hybrid_property; without it in
        _ADAPTER_INIT_KEYS this raises TypeError and breaks manual lead
        creation and CSV import."""
        lead = make_lead("919000000001", status="Interested")
        assert lead.lead_status == "Interested"

    def test_kwarg_reaches_the_legacy_column(self, ctx):
        lead = make_lead("919000000002", status="Contacted")
        assert lead._lead_status == "Contacted"

    def test_lead_status_is_in_adapter_init_keys(self, ctx):
        assert "lead_status" in ConversationState._ADAPTER_INIT_KEYS


# ── Adapter read/write ───────────────────────────────────────────────────────

class TestAdapter:
    def test_unlinked_row_falls_back_to_legacy_string(self, ctx):
        lead = make_lead("919000000010", status="Interested")
        assert lead.sales_stage_id is None
        assert lead.lead_status == "Interested"

    def test_setter_creates_the_link_once_seeded(self, ctx):
        seed()
        lead = make_lead("919000000011", status="Lead")
        lead.lead_status = "Demo Scheduled"
        db.session.commit()
        assert lead.sales_stage_id is not None, "setter must CREATE the link"
        assert lead.lead_status == "Demo Scheduled"

    def test_getter_returns_display_name_not_internal_key(self, ctx):
        """internal_key would silently break templates, CSV export and every
        LEAD_STATUSES comparison."""
        seed()
        lead = make_lead("919000000012", status="Demo Scheduled")
        stage = db.session.get(PipelineStage, lead.sales_stage_id)
        assert stage.internal_key == "demo_scheduled"
        assert lead.lead_status == "Demo Scheduled"

    def test_unrecognised_value_clears_stale_link(self, ctx):
        """Otherwise the getter keeps reporting the previous stage after the
        status has changed."""
        seed()
        lead = make_lead("919000000013", status="Interested")
        assert lead.sales_stage_id is not None
        lead.lead_status = "fresh"          # the known out-of-vocabulary value
        db.session.commit()
        assert lead.sales_stage_id is None
        assert lead.lead_status == "fresh", "must fall back, not report the old stage"

    def test_legacy_column_always_written(self, ctx):
        """Rollback safety: clearing sales_stage_id must restore the status."""
        seed()
        lead = make_lead("919000000014", status="Negotiation")
        assert lead._lead_status == "Negotiation"
        lead.sales_stage_id = None
        db.session.commit()
        assert lead.lead_status == "Negotiation"

    def test_to_dict_uses_the_adapter(self, ctx):
        seed()
        lead = make_lead("919000000015", status="Enrolled")
        assert lead.to_dict()["lead_status"] == "Enrolled"


# ── SQL expression (used by crm_my_leads / crm_staff_dashboard) ──────────────

class TestSqlExpression:
    def test_notin_terminal_filters_unlinked_rows(self, ctx):
        make_lead("919000000020", status="Lead")
        make_lead("919000000021", status="Enrolled")
        rows = (ConversationState.query
                .filter(ConversationState.tenant_id == TENANT,
                        ConversationState.lead_status.notin_(tuple(LEAD_TERMINAL_STATUSES)))
                .all())
        assert [r.phone for r in rows] == ["919000000020"]

    def test_notin_terminal_filters_linked_rows(self, ctx):
        seed()
        make_lead("919000000022", status="Lead")
        make_lead("919000000023", status="Enrolled")
        rows = (ConversationState.query
                .filter(ConversationState.tenant_id == TENANT,
                        ConversationState.lead_status.notin_(tuple(LEAD_TERMINAL_STATUSES)))
                .all())
        phones = sorted(r.phone for r in rows)
        assert phones == ["919000000022"], "linked Enrolled row must still be excluded"

    def test_equality_filter_works_when_linked(self, ctx):
        seed()
        make_lead("919000000024", status="Contacted")
        rows = ConversationState.query.filter(
            ConversationState.lead_status == "Contacted").all()
        assert len(rows) == 1


# ── The AI conversation engine must be untouched ─────────────────────────────

class TestBotEngineUnaffected:
    def test_stage_adapter_still_reads_pipeline_stage_id(self, ctx):
        lead = make_lead("919000000030", stage="goal_selection")
        assert lead.stage == "goal_selection"
        assert lead.pipeline_stage_id is None

    def test_sales_link_does_not_change_stage(self, ctx):
        """The core separation guarantee: linking a sales stage must not move
        the bot funnel."""
        seed()
        lead = make_lead("919000000031", status="Enrolled", stage="demo_booked")
        assert lead.sales_stage_id is not None
        assert lead.stage == "demo_booked", "bot funnel must be unaffected"
        assert lead._stage == "demo_booked"

    def test_the_two_columns_are_independent(self, ctx):
        seed()
        lead = make_lead("919000000032", status="Interested", stage="course_viewed")
        assert lead.pipeline_stage_id is None
        assert lead.sales_stage_id is not None
        assert lead.pipeline_stage_id != lead.sales_stage_id

    def test_setting_stage_does_not_touch_sales_link(self, ctx):
        seed()
        lead = make_lead("919000000033", status="Interested", stage="new")
        before = lead.sales_stage_id
        lead.stage = "payment_pending"       # bot write
        db.session.commit()
        assert lead.sales_stage_id == before
        assert lead.lead_status == "Interested"


# ── Seeder ───────────────────────────────────────────────────────────────────

class TestSeeder:
    def test_seeds_one_stage_per_approved_status(self, ctx):
        stage_map, pid, _ = seed()
        stages = PipelineStage.query.filter_by(pipeline_id=pid).all()
        assert len(stages) == len(LEAD_STATUSES)
        assert {s.display_name for s in stages} == set(LEAD_STATUSES)

    def test_defines_won_and_lost_categories(self, ctx):
        """The bot pipeline has 0 'lost' stages, which made win/loss
        reporting impossible (Phase 10.4)."""
        _, pid, _ = seed()
        cats = {}
        for s in PipelineStage.query.filter_by(pipeline_id=pid).all():
            cats.setdefault(s.stage_category, []).append(s.display_name)
        assert cats.get("won") == ["Enrolled"]
        assert sorted(cats.get("lost", [])) == ["Lost", "Not Interested"]
        assert len(cats.get("open", [])) == 7

    def test_terminal_flags_match_the_constant(self, ctx):
        _, pid, _ = seed()
        for s in PipelineStage.query.filter_by(pipeline_id=pid).all():
            assert s.is_terminal == (s.display_name in LEAD_TERMINAL_STATUSES)

    def test_ordering_follows_lead_statuses(self, ctx):
        _, pid, _ = seed()
        ordered = (PipelineStage.query.filter_by(pipeline_id=pid)
                   .order_by(PipelineStage.order_index).all())
        assert [s.display_name for s in ordered] == list(LEAD_STATUSES)

    def test_pipeline_is_separate_from_the_bot_pipeline(self, ctx):
        _, pid, _ = seed()
        p = db.session.get(PipelineDefinition, pid)
        assert p.internal_key == SALES_PIPELINE_KEY
        assert p.tenant_id == TENANT

    def test_idempotent(self, ctx):
        _, pid, _ = seed()
        n = PipelineStage.query.filter_by(pipeline_id=pid).count()
        _, pid2, r2 = seed()
        assert pid2 == pid
        assert PipelineStage.query.filter_by(pipeline_id=pid).count() == n
        assert r2.stages_reused == len(LEAD_STATUSES)
        assert r2.stages_created == 0

    def test_dry_run_writes_nothing(self, ctx):
        SalesPipelineSeeder(dry_run=True).run()
        assert PipelineDefinition.query.filter_by(
            internal_key=SALES_PIPELINE_KEY).count() == 0

    def test_links_existing_leads(self, ctx):
        make_lead("919000000040", status="Interested")
        make_lead("919000000041", status="Enrolled")
        reports = SalesPipelineSeeder(dry_run=False).run()
        rep = next(r for r in reports if r.tenant_id == TENANT)
        assert rep.leads_linked == 2
        for lead in ConversationState.query.filter_by(tenant_id=TENANT).all():
            assert lead.sales_stage_id is not None

    def test_out_of_vocabulary_left_unlinked_and_reported(self, ctx):
        make_lead("919000000042", status="fresh")
        reports = SalesPipelineSeeder(dry_run=False).run()
        rep = next(r for r in reports if r.tenant_id == TENANT)
        assert rep.leads_unlinkable == 1
        assert "fresh" in rep.unlinkable_values
        lead = ConversationState.query.filter_by(phone="919000000042").first()
        assert lead.sales_stage_id is None
        assert lead.lead_status == "fresh", "must remain readable and editable"

    def test_reads_legacy_column_not_the_adapter(self, ctx):
        """Re-running must read the source string, never its own output."""
        make_lead("919000000043", status="Interested")
        SalesPipelineSeeder(dry_run=False).run()
        reports = SalesPipelineSeeder(dry_run=False).run()
        rep = next(r for r in reports if r.tenant_id == TENANT)
        assert rep.leads_linked == 0
        assert rep.leads_already == 1


# ── Tenant isolation ─────────────────────────────────────────────────────────

class TestTenantIsolation:
    def test_each_tenant_gets_its_own_pipeline(self, ctx):
        SalesPipelineSeeder(dry_run=False).run()
        pipelines = PipelineDefinition.query.filter_by(
            internal_key=SALES_PIPELINE_KEY).all()
        assert {p.tenant_id for p in pipelines} == {TENANT, OTHER}

    def test_lead_links_only_to_its_own_tenants_stage(self, ctx):
        SalesPipelineSeeder(dry_run=False).run()
        a = make_lead("919000000050", tenant=TENANT, status="Interested")
        b = make_lead("919000000051", tenant=OTHER, status="Interested")
        assert a.sales_stage_id != b.sales_stage_id
        sa = db.session.get(PipelineStage, a.sales_stage_id)
        sb = db.session.get(PipelineStage, b.sales_stage_id)
        assert db.session.get(PipelineDefinition, sa.pipeline_id).tenant_id == TENANT
        assert db.session.get(PipelineDefinition, sb.pipeline_id).tenant_id == OTHER

    def test_no_link_without_a_tenant_pipeline(self, ctx):
        seed(TENANT)                      # only tenant A seeded
        lead = make_lead("919000000052", tenant=OTHER, status="Interested")
        assert lead.sales_stage_id is None
        assert lead.lead_status == "Interested"
