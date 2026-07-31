"""
Phase 10.9A — lead_status validation on the two operator-facing form paths.

Gap found by the Phase 10.9 discovery audit: crm_lead_update assigned
request.form["lead_status"] straight to the model and crm_lead_new passed it
straight to the constructor. Neither validated. The dropdown was the only
constraint, and a crafted POST bypasses it.

That mattered beyond tidiness: an unrecognised status has no matching
PipelineStage, so _sync_sales_stage_link() clears sales_stage_id and the lead
drops out of the Sales Pipeline — silently undoing the 100% coverage
established in Phase 10.8C.3.

canonical_lead_status() is exercised directly (it is a pure function, so no app
context is needed) and the coverage consequence is proven end-to-end against a
real database.

Import isolation matches test_pipeline_foundation_10_6.py; see that file for
the note on tests/conftest.py stubbing `app` at collection time.
"""
import os
import sys
import tempfile

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_10_9a_test.db")
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
    ConversationState, PipelineStage, Tenant, LEAD_STATUSES,
)
from app.services.sales_pipeline_seed import SalesPipelineSeeder      # noqa: E402
from app.services import sales_pipeline_service as sps                # noqa: E402
from app.routes.admin import canonical_lead_status                    # noqa: E402

TENANT = "t-10-9a"
_APP = create_app()


@pytest.fixture()
def ctx():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=TENANT, name="A", slug=TENANT))
        db.session.commit()
        SalesPipelineSeeder(dry_run=False).run()
        yield
        db.session.remove()


def make_lead(phone="919000000001", status="Interested"):
    lead = ConversationState(
        phone=phone, name="Test", tenant_id=TENANT,
        stage="new", course="", goal="", batch_time="", offer_course="",
        last_msg="", last_text="", lead_status=status,
    )
    db.session.add(lead)
    db.session.commit()
    return lead


# ── The validator itself (pure function) ─────────────────────────────────────

class TestCanonicalLeadStatus:
    @pytest.mark.parametrize("value", list(LEAD_STATUSES))
    def test_every_approved_status_is_accepted(self, value):
        assert canonical_lead_status(value) == value

    @pytest.mark.parametrize("raw,expected", [
        ("enrolled", "Enrolled"),
        ("ENROLLED", "Enrolled"),
        ("not interested", "Not Interested"),
        ("DeMo ScHeDuLeD", "Demo Scheduled"),
        ("  Interested  ", "Interested"),
    ])
    def test_case_and_whitespace_canonicalised(self, raw, expected):
        assert canonical_lead_status(raw) == expected

    @pytest.mark.parametrize("raw", [
        "fresh", "bogus", "Dropped", "enroled", "Lead;DROP TABLE",
        "won", "open", "123", "-",
    ])
    def test_unknown_values_rejected(self, raw):
        assert canonical_lead_status(raw) is None

    @pytest.mark.parametrize("raw", ["", "   ", None])
    def test_blank_returns_default(self, raw):
        assert canonical_lead_status(raw) is None
        assert canonical_lead_status(raw, default="Interested") == "Interested"

    def test_rejected_value_returns_the_default(self, ctx):
        assert canonical_lead_status("fresh", default="Contacted") == "Contacted"

    def test_return_is_always_from_the_vocabulary_or_the_default(self):
        """The vocabulary can never be widened by this function."""
        sentinel = object()
        for raw in ("fresh", "Enrolled", "", None, "ENROLLED", "xyz"):
            out = canonical_lead_status(raw, default=sentinel)
            assert out is sentinel or out in LEAD_STATUSES


# ── crm_lead_new() semantics ─────────────────────────────────────────────────

class TestNewLeadDefaults:
    def test_valid_status_accepted(self, ctx):
        assert canonical_lead_status("Interested", default="Lead") == "Interested"

    def test_case_variant_accepted_and_canonicalised(self, ctx):
        assert canonical_lead_status("interested", default="Lead") == "Interested"

    def test_invalid_status_falls_back_to_entry_status(self, ctx):
        assert canonical_lead_status("fresh", default="Lead") == "Lead"

    def test_blank_falls_back_to_entry_status(self, ctx):
        assert canonical_lead_status("", default="Lead") == "Lead"

    def test_created_lead_with_rejected_status_is_still_linked(self, ctx):
        """The consequence that matters: a rejected value must not leave the
        new lead outside the pipeline."""
        lead = make_lead("919000000010",
                         status=canonical_lead_status("fresh", default="Lead"))
        assert lead.lead_status == "Lead"
        assert lead.sales_stage_id is not None
        assert db.session.get(PipelineStage, lead.sales_stage_id).display_name == "Lead"


# ── crm_lead_update() semantics ──────────────────────────────────────────────

class TestUpdatePreservesExistingValue:
    def test_valid_status_applied(self, ctx):
        lead = make_lead("919000000020", status="Lead")
        lead.lead_status = canonical_lead_status("Interested") or lead.lead_status
        db.session.commit()
        assert lead.lead_status == "Interested"

    def test_invalid_status_preserves_existing(self, ctx):
        lead = make_lead("919000000021", status="Interested")
        lead.lead_status = canonical_lead_status("fresh") or lead.lead_status
        db.session.commit()
        assert lead.lead_status == "Interested", "existing value must survive"

    def test_blank_submission_preserves_existing(self, ctx):
        lead = make_lead("919000000022", status="Contacted")
        lead.lead_status = canonical_lead_status("") or lead.lead_status
        db.session.commit()
        assert lead.lead_status == "Contacted"

    def test_case_variant_updates_to_canonical_form(self, ctx):
        lead = make_lead("919000000023", status="Lead")
        lead.lead_status = canonical_lead_status("enrolled") or lead.lead_status
        db.session.commit()
        assert lead.lead_status == "Enrolled"


# ── The coverage guarantee (the reason this phase exists) ────────────────────

class TestCoverageCannotBeBroken:
    def test_rejected_status_does_not_clear_sales_stage_id(self, ctx):
        lead = make_lead("919000000030", status="Interested")
        before = lead.sales_stage_id
        assert before is not None

        lead.lead_status = canonical_lead_status("fresh") or lead.lead_status
        db.session.commit()

        assert lead.sales_stage_id == before, "link must survive a rejected status"
        assert lead.lead_status == "Interested"

    def test_unvalidated_write_WOULD_break_coverage(self, ctx):
        """Demonstrates the defect this phase fixes: assigning an unvalidated
        value clears the link and drops the lead out of the pipeline."""
        lead = make_lead("919000000031", status="Interested")
        assert lead.sales_stage_id is not None

        lead.lead_status = "fresh"          # what the route did before 10.9A
        db.session.commit()

        assert lead.sales_stage_id is None, "this is the behaviour being prevented"

    def test_pipeline_coverage_holds_across_crafted_submissions(self, ctx):
        for i in range(5):
            make_lead(f"91900000004{i}", status="Lead")
        total = ConversationState.query.filter_by(tenant_id=TENANT).count()
        assert ConversationState.query.filter(
            ConversationState.tenant_id == TENANT,
            ConversationState.sales_stage_id.isnot(None)).count() == total

        for lead in ConversationState.query.filter_by(tenant_id=TENANT).all():
            for hostile in ("fresh", "", "bogus", "Dropped", "won"):
                lead.lead_status = canonical_lead_status(hostile) or lead.lead_status
        db.session.commit()

        linked = ConversationState.query.filter(
            ConversationState.tenant_id == TENANT,
            ConversationState.sales_stage_id.isnot(None)).count()
        assert linked == total, "coverage must remain 100%"

    def test_all_leads_remain_countable_in_the_dashboard(self, ctx):
        for i in range(3):
            make_lead(f"91900000005{i}", status="Lead")
        lead = ConversationState.query.filter_by(tenant_id=TENANT).first()
        lead.lead_status = canonical_lead_status("not-a-status") or lead.lead_status
        db.session.commit()

        summary = sps.get_pipeline_summary(TENANT, None)
        counted = sum(s["lead_count"] for s in summary)
        assert counted == ConversationState.query.filter_by(tenant_id=TENANT).count()


# ── Backward compatibility / scope containment ───────────────────────────────

class TestScopeAndCompatibility:
    def test_vocabulary_not_widened(self, ctx):
        assert len(LEAD_STATUSES) == 10
        assert "fresh" not in LEAD_STATUSES
        assert "Dropped" not in LEAD_STATUSES

    def test_legacy_stored_value_is_readable_and_correctable(self, ctx):
        """A pre-existing out-of-vocabulary row must stay editable — validation
        applies to what is WRITTEN, not to what is already stored."""
        lead = make_lead("919000000060", status="Lead")
        lead._lead_status = "fresh"          # simulate legacy data
        lead.sales_stage_id = None
        db.session.commit()
        assert lead.lead_status == "fresh"

        lead.lead_status = canonical_lead_status("Interested") or lead.lead_status
        db.session.commit()
        assert lead.lead_status == "Interested"
        assert lead.sales_stage_id is not None, "correcting it re-links the lead"

    def test_no_transition_rules_introduced(self, ctx):
        """Phase 10.9A is validation only — any approved status may follow any
        other. Transition rules are Phase 10.9B."""
        lead = make_lead("919000000061", status="Enrolled")
        lead.lead_status = canonical_lead_status("Lead") or lead.lead_status
        db.session.commit()
        assert lead.lead_status == "Lead", "won -> open must still be permitted"

    def test_bot_fields_untouched_by_validation(self, ctx):
        lead = make_lead("919000000062", status="Interested")
        before_stage, before_link = lead._stage, lead.pipeline_stage_id
        lead.lead_status = canonical_lead_status("fresh") or lead.lead_status
        db.session.commit()
        assert lead._stage == before_stage
        assert lead.pipeline_stage_id == before_link
