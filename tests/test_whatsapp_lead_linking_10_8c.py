"""
Phase 10.8C — WhatsApp inbound leads must receive a sales_stage_id.

Production defect: _load_or_create_row() created ConversationState without a
lead_status kwarg, so the value came from the COLUMN DEFAULT at flush time.
A column default never passes through the @lead_status.setter, so
_sync_sales_stage_link() never ran and bot-created leads entered with
sales_stage_id NULL — invisible to the Sales Pipeline. Observed live as
coverage falling from 64/64 to 64/65 on the first inbound lead after seeding.

These tests exercise the REAL _load_or_create_row against a real database,
because the defect was precisely a difference between two ways of setting the
same column — something no source-level check could distinguish.

Import isolation matches test_pipeline_foundation_10_6.py; see that file for
the full note on tests/conftest.py stubbing `app` at collection time.
"""
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_10_8c_test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB}")
os.environ.setdefault("ADMIN_KEY", "testkey")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ.setdefault("BROADCAST_API_KEY", "testbroadcast")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from app import create_app                                            # noqa: E402
from app.extensions import db                                         # noqa: E402
from app.models import (                                              # noqa: E402
    ConversationState, PipelineDefinition, PipelineStage, Tenant,
    SALES_PIPELINE_KEY,
)
from app.services.sales_pipeline_seed import SalesPipelineSeeder      # noqa: E402
from app.services import sales_pipeline_service as sps                # noqa: E402
from app.state import _load_or_create_row                             # noqa: E402

TENANT = "t-10-8c"
OTHER = "t-10-8c-other"
UNSEEDED = "t-10-8c-unseeded"
_APP = create_app()


@pytest.fixture()
def ctx():
    """Seeds sales pipelines for TENANT and OTHER only — UNSEEDED is created
    afterwards so it deliberately has no sales pipeline."""
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=TENANT, name="A", slug=TENANT))
        db.session.add(Tenant(id=OTHER, name="B", slug=OTHER))
        db.session.commit()
        SalesPipelineSeeder(dry_run=False).run()
        db.session.add(Tenant(id=UNSEEDED, name="C", slug=UNSEEDED))
        db.session.commit()
        yield
        db.session.remove()


def sales_stage_of(lead):
    return db.session.get(PipelineStage, lead.sales_stage_id)


# ── The fix ──────────────────────────────────────────────────────────────────

class TestInboundLeadIsLinked:
    def test_whatsapp_created_lead_receives_sales_stage_id(self, ctx):
        """The defect itself: this was None before the fix."""
        row, created = _load_or_create_row("919000000001", "Inbound", TENANT)
        assert created is True
        assert row.sales_stage_id is not None

    def test_linked_stage_belongs_to_the_tenants_sales_pipeline(self, ctx):
        row, _ = _load_or_create_row("919000000002", "Inbound", TENANT)
        stage = sales_stage_of(row)
        pipeline = db.session.get(PipelineDefinition, stage.pipeline_id)
        assert pipeline.tenant_id == TENANT
        assert pipeline.internal_key == SALES_PIPELINE_KEY

    def test_links_to_the_entry_stage(self, ctx):
        row, _ = _load_or_create_row("919000000003", "Inbound", TENANT)
        stage = sales_stage_of(row)
        assert stage.display_name == "Lead"
        assert stage.is_entry is True
        assert stage.stage_category == "open"

    def test_lead_appears_in_the_pipeline_summary(self, ctx):
        """The user-visible consequence: an unlinked lead is not counted."""
        before = {s["display_name"]: s["lead_count"]
                  for s in sps.get_pipeline_summary(TENANT, None)}
        _load_or_create_row("919000000004", "Inbound", TENANT)
        after = {s["display_name"]: s["lead_count"]
                 for s in sps.get_pipeline_summary(TENANT, None)}
        assert after["Lead"] == before["Lead"] + 1


# ── Bot behaviour must be identical ──────────────────────────────────────────

class TestBotBehaviourUnchanged:
    def test_lead_status_value_is_still_Lead(self, ctx):
        """The whole safety argument: the stored value is what the column
        default produced before, so nothing the bot reads has changed."""
        row, _ = _load_or_create_row("919000000010", "Inbound", TENANT)
        assert row._lead_status == "Lead"
        assert row.lead_status == "Lead"

    def test_stage_remains_new(self, ctx):
        row, _ = _load_or_create_row("919000000011", "Inbound", TENANT)
        assert row._stage == "new"
        assert row.stage == "new"

    def test_pipeline_stage_id_unchanged(self, ctx):
        """The AI funnel link must stay NULL — _sync_sales_stage_link writes
        sales_stage_id and nothing else."""
        row, _ = _load_or_create_row("919000000012", "Inbound", TENANT)
        assert row.pipeline_stage_id is None

    def test_the_two_stage_columns_are_independent(self, ctx):
        row, _ = _load_or_create_row("919000000013", "Inbound", TENANT)
        assert row.sales_stage_id is not None
        assert row.pipeline_stage_id is None
        assert row.sales_stage_id != row.pipeline_stage_id

    def test_other_bot_fields_untouched(self, ctx):
        row, _ = _load_or_create_row("919000000014", "Inbound Name", TENANT)
        assert row.name == "Inbound Name"
        assert row.course == ""
        assert row.goal == ""
        assert row.batch_time == ""
        assert row.offer_course == ""
        assert row.last_text == ""
        assert row.last_msg  # timestamp set by the bot path

    def test_returns_row_and_created_flag(self, ctx):
        row, created = _load_or_create_row("919000000015", "X", TENANT)
        assert isinstance(row, ConversationState)
        assert created is True


# ── Returning-lead path must be unaffected ───────────────────────────────────

class TestReturningLead:
    def test_second_call_returns_existing_row_not_created(self, ctx):
        first, c1 = _load_or_create_row("919000000020", "Inbound", TENANT)
        second, c2 = _load_or_create_row("919000000020", "Different Name", TENANT)
        assert c1 is True and c2 is False
        assert first.id == second.id

    def test_returning_lead_keeps_its_existing_stage(self, ctx):
        """The early return must not re-link or reset a lead that has since
        been moved along the pipeline."""
        row, _ = _load_or_create_row("919000000021", "Inbound", TENANT)
        row.lead_status = "Interested"
        db.session.commit()
        moved_stage = row.sales_stage_id

        again, created = _load_or_create_row("919000000021", "Inbound", TENANT)
        assert created is False
        assert again.sales_stage_id == moved_stage
        assert again.lead_status == "Interested", "must not be reset to Lead"

    def test_returning_lead_name_not_overwritten(self, ctx):
        _load_or_create_row("919000000022", "Original", TENANT)
        again, _ = _load_or_create_row("919000000022", "Changed", TENANT)
        assert again.name == "Original"

    def test_only_one_row_per_phone_tenant(self, ctx):
        for _ in range(3):
            _load_or_create_row("919000000023", "Inbound", TENANT)
        assert ConversationState.query.filter_by(
            phone="919000000023", tenant_id=TENANT).count() == 1


# ── Tenant without a sales pipeline — graceful fallback ──────────────────────

class TestUnseededTenantFallback:
    def test_creation_succeeds_without_a_sales_pipeline(self, ctx):
        """Must not raise — a tenant that has never been seeded still needs to
        receive WhatsApp leads."""
        row, created = _load_or_create_row("919000000030", "Inbound", UNSEEDED)
        assert created is True
        assert row.sales_stage_id is None

    def test_lead_status_falls_back_to_the_legacy_string(self, ctx):
        row, _ = _load_or_create_row("919000000031", "Inbound", UNSEEDED)
        assert row._lead_status == "Lead"
        assert row.lead_status == "Lead", "adapter must fall back, not fail"

    def test_bot_fields_still_correct_when_unlinked(self, ctx):
        row, _ = _load_or_create_row("919000000032", "Inbound", UNSEEDED)
        assert row._stage == "new"
        assert row.pipeline_stage_id is None


# ── Tenant isolation ─────────────────────────────────────────────────────────

class TestTenantIsolation:
    def test_lead_links_to_its_own_tenants_stage(self, ctx):
        a, _ = _load_or_create_row("919000000040", "A", TENANT)
        b, _ = _load_or_create_row("919000000041", "B", OTHER)
        assert a.sales_stage_id != b.sales_stage_id
        pa = db.session.get(PipelineDefinition, sales_stage_of(a).pipeline_id)
        pb = db.session.get(PipelineDefinition, sales_stage_of(b).pipeline_id)
        assert pa.tenant_id == TENANT
        assert pb.tenant_id == OTHER

    def test_same_phone_in_two_tenants_links_separately(self, ctx):
        a, _ = _load_or_create_row("919000000042", "A", TENANT)
        b, _ = _load_or_create_row("919000000042", "B", OTHER)
        assert a.id != b.id
        assert a.sales_stage_id != b.sales_stage_id

    def test_lead_counted_only_in_its_own_tenant_pipeline(self, ctx):
        _load_or_create_row("919000000043", "A", TENANT)
        a = {s["display_name"]: s["lead_count"] for s in sps.get_pipeline_summary(TENANT, None)}
        b = {s["display_name"]: s["lead_count"] for s in sps.get_pipeline_summary(OTHER, None)}
        assert a["Lead"] == 1
        assert b["Lead"] == 0
