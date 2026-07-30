"""
Phase 10.8 — Pipeline Operations: stage movement recording.

Real models against a real database: the behaviour under test is what gets
written (and, just as importantly, what does NOT get written) when a lead's
sales stage changes.

Import isolation matches test_pipeline_foundation_10_6.py — tests/conftest.py
stubs `app` at collection time, so the real package is re-imported here. Safe
because this suite is run file-by-file; see that file for the full note.
"""
import os
import sys
import tempfile
from unittest.mock import patch

import pytest

for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_10_8_test.db")
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
    ConversationState, LeadEvent, LeadStageHistory, Notification,
    PipelineStage, Tenant, LEAD_STATUSES,
)
from app.services.sales_pipeline_seed import SalesPipelineSeeder      # noqa: E402
from app.services import sales_pipeline_service as sps                # noqa: E402

TENANT = "t-10-8"
OTHER = "t-10-8-other"
_APP = create_app()


@pytest.fixture()
def ctx():
    with _APP.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        db.session.add(Tenant(id=TENANT, name="A", slug=TENANT))
        db.session.add(Tenant(id=OTHER, name="B", slug=OTHER))
        db.session.commit()
        SalesPipelineSeeder(dry_run=False).run()
        yield
        db.session.remove()


def make_lead(phone="919000000001", tenant=TENANT, status="Lead", staff=None):
    lead = ConversationState(
        phone=phone, name="Test Lead", tenant_id=tenant,
        stage="new", course="", goal="", batch_time="", offer_course="",
        last_msg="", last_text="", lead_status=status, assigned_staff=staff,
    )
    db.session.add(lead)
    db.session.commit()
    return lead


def move(lead, to_status, **kw):
    """Change the status through the adapter, then record it."""
    before_id, before_status = lead.sales_stage_id, lead.lead_status
    lead.lead_status = to_status
    db.session.commit()
    return sps.record_stage_change(TENANT, lead, before_id, before_status, **kw)


def history(lead):
    return (LeadStageHistory.query
            .filter_by(conversation_state_id=lead.id)
            .order_by(LeadStageHistory.id).all())


# ── History recording ────────────────────────────────────────────────────────

class TestHistoryRecording:
    def test_movement_creates_one_row(self, ctx):
        lead = make_lead()
        move(lead, "Interested", actor="a@x.com")
        h = history(lead)
        assert len(h) == 1
        assert (h[0].from_status, h[0].to_status) == ("Lead", "Interested")

    def test_records_both_stage_ids(self, ctx):
        """Ids make history joinable and rename-proof; names alone are what
        makes the existing audit_log unusable for analytics."""
        lead = make_lead()
        before = lead.sales_stage_id
        move(lead, "Interested")
        h = history(lead)[0]
        assert h.from_stage_id == before
        assert h.to_stage_id == lead.sales_stage_id
        assert h.from_stage_id != h.to_stage_id
        assert db.session.get(PipelineStage, h.to_stage_id).display_name == "Interested"

    def test_first_entry_has_null_from_stage(self, ctx):
        lead = make_lead()
        sps.record_stage_change(TENANT, lead, None, None, actor="a@x.com")
        h = history(lead)[0]
        assert h.from_stage_id is None
        assert h.from_status is None
        assert h.to_status == "Lead"

    def test_actor_recorded(self, ctx):
        lead = make_lead()
        move(lead, "Contacted", actor="operator@example.com")
        assert history(lead)[0].actor == "operator@example.com"

    def test_tenant_stamped_on_row(self, ctx):
        lead = make_lead()
        move(lead, "Contacted")
        assert history(lead)[0].tenant_id == TENANT

    def test_changed_at_populated(self, ctx):
        lead = make_lead()
        move(lead, "Contacted")
        assert history(lead)[0].changed_at is not None

    def test_multiple_movements_ordered(self, ctx):
        lead = make_lead()
        for s in ("Contacted", "Interested", "Demo Scheduled"):
            move(lead, s)
        assert [h.to_status for h in history(lead)] == ["Contacted", "Interested", "Demo Scheduled"]

    def test_no_row_when_nothing_changed(self, ctx):
        """Guards against a form resubmit inflating the history."""
        lead = make_lead()
        assert sps.record_stage_change(
            TENANT, lead, lead.sales_stage_id, lead.lead_status) is None
        assert history(lead) == []

    def test_returns_stage_ids_for_audit_enrichment(self, ctx):
        lead = make_lead()
        before = lead.sales_stage_id
        out = move(lead, "Interested")
        assert out == {"from_stage_id": before, "to_stage_id": lead.sales_stage_id}

    def test_unmapped_status_still_recorded(self, ctx):
        """A status with no stage link must not vanish from history."""
        lead = make_lead()
        out = move(lead, "definitely-not-a-status")
        assert lead.sales_stage_id is None
        h = history(lead)[0]
        assert h.to_stage_id is None
        assert h.to_status == "definitely-not-a-status"
        assert out["to_stage_id"] is None


# ── Lead timeline ────────────────────────────────────────────────────────────

class TestTimeline:
    def test_stage_changed_event_written(self, ctx):
        lead = make_lead()
        move(lead, "Interested")
        ev = LeadEvent.query.filter_by(phone=lead.phone, event_type="STAGE_CHANGED").all()
        assert len(ev) == 1

    def test_event_payload_has_from_and_to(self, ctx):
        import json
        lead = make_lead()
        move(lead, "Interested")
        ev = LeadEvent.query.filter_by(event_type="STAGE_CHANGED").first()
        assert json.loads(ev.event_data) == {"from": "Lead", "to": "Interested"}

    def test_no_event_when_nothing_changed(self, ctx):
        lead = make_lead()
        sps.record_stage_change(TENANT, lead, lead.sales_stage_id, lead.lead_status)
        assert LeadEvent.query.filter_by(event_type="STAGE_CHANGED").count() == 0


# ── Notification policy (the approved restrictions) ──────────────────────────

class TestNotificationPolicy:
    def test_notifies_assigned_staff_on_operator_move(self, ctx):
        lead = make_lead(staff="Anju")
        move(lead, "Interested", actor="admin@x.com", notify=True,
             notify_actor_name="Admin")
        n = Notification.query.filter_by(tenant_id=TENANT).all()
        assert len(n) == 1
        assert n[0].notif_type == Notification.TYPE_STAGE_CHANGED
        assert n[0].recipient == "Anju"

    def test_csv_import_does_not_notify(self, ctx):
        """A 5,000-row import must not emit 5,000 notifications."""
        lead = make_lead(staff="Anju")
        move(lead, "Interested", actor=sps.ACTOR_CSV_IMPORT, notify=False)
        assert Notification.query.count() == 0
        assert len(history(lead)) == 1, "history still recorded"

    def test_auto_admission_does_not_notify(self, ctx):
        lead = make_lead(staff="Anju")
        move(lead, "Enrolled", actor=sps.ACTOR_AUTO_ADMISSION, notify=False)
        assert Notification.query.count() == 0
        assert len(history(lead)) == 1

    def test_no_self_notification(self, ctx):
        """A staff member who moved their own lead need not be told."""
        lead = make_lead(staff="Anju")
        move(lead, "Interested", actor="anju@x.com", notify=True,
             notify_actor_name="Anju")
        assert Notification.query.count() == 0

    def test_self_notification_check_is_case_insensitive(self, ctx):
        lead = make_lead(staff="  ANJU ")
        move(lead, "Interested", notify=True, notify_actor_name="anju")
        assert Notification.query.count() == 0

    def test_unassigned_lead_notifies_nobody(self, ctx):
        lead = make_lead(staff=None)
        move(lead, "Interested", notify=True, notify_actor_name="Admin")
        assert Notification.query.count() == 0
        assert len(history(lead)) == 1

    def test_notify_defaults_to_false(self, ctx):
        """Opt-in by design — a caller that forgets stays silent."""
        lead = make_lead(staff="Anju")
        move(lead, "Interested")
        assert Notification.query.count() == 0

    def test_stage_changed_is_a_valid_notification_type(self, ctx):
        """notify() RAISES on a type absent from VALID_TYPES."""
        assert Notification.TYPE_STAGE_CHANGED in Notification.VALID_TYPES


# ── Never raises / never undoes the lead edit ────────────────────────────────

class TestFailureIsolation:
    def test_history_failure_does_not_raise(self, ctx):
        lead = make_lead()
        with patch.object(sps, "_write_history", side_effect=RuntimeError("boom")):
            assert move(lead, "Interested") is None
        assert lead.lead_status == "Interested", "the lead edit must survive"

    def test_notification_failure_does_not_raise(self, ctx):
        lead = make_lead(staff="Anju")
        with patch.object(sps, "_notify_stage_change", side_effect=RuntimeError("boom")):
            move(lead, "Interested", notify=True)
        assert lead.lead_status == "Interested"

    def test_timeline_failure_does_not_raise(self, ctx):
        lead = make_lead()
        with patch.object(sps, "_write_timeline", side_effect=RuntimeError("boom")):
            move(lead, "Interested")
        assert lead.lead_status == "Interested"


# ── Read-back / tenant isolation ─────────────────────────────────────────────

class TestHistoryReads:
    def test_get_stage_history_newest_first(self, ctx):
        lead = make_lead()
        for s in ("Contacted", "Interested", "Demo Scheduled"):
            move(lead, s)
        assert [h.to_status for h in sps.get_stage_history(TENANT, lead.id)][0] == "Demo Scheduled"

    def test_history_is_tenant_scoped(self, ctx):
        lead = make_lead()
        move(lead, "Interested")
        assert sps.get_stage_history(TENANT, lead.id)
        assert sps.get_stage_history(OTHER, lead.id) == []

    def test_limit_respected(self, ctx):
        lead = make_lead()
        for s in ("Contacted", "Interested", "Demo Scheduled", "Demo Done"):
            move(lead, s)
        assert len(sps.get_stage_history(TENANT, lead.id, limit=2)) == 2

    def test_missing_args_return_empty(self, ctx):
        assert sps.get_stage_history(None, 1) == []
        assert sps.get_stage_history(TENANT, None) == []


# ── The AI funnel and Phase 10.6 architecture stay untouched ─────────────────

class TestArchitecturePreserved:
    def test_movement_does_not_touch_bot_columns(self, ctx):
        lead = make_lead()
        before_stage, before_link = lead._stage, lead.pipeline_stage_id
        move(lead, "Interested", notify=False)
        db.session.refresh(lead)
        assert lead._stage == before_stage
        assert lead.pipeline_stage_id == before_link

    def test_sales_stage_id_never_written_directly_by_the_service(self, ctx):
        """Movement must flow through the lead_status adapter (Phase 10.6),
        not by assigning sales_stage_id."""
        import ast
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app", "services", "sales_pipeline_service.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                   for t in n.targets
                   if isinstance(t, ast.Attribute) and t.attr == "sales_stage_id"]
        assert not assigns, "service assigns sales_stage_id directly"

    def test_legacy_string_and_link_stay_consistent(self, ctx):
        lead = make_lead()
        move(lead, "Negotiation")
        stage = db.session.get(PipelineStage, lead.sales_stage_id)
        assert lead._lead_status == "Negotiation" == stage.display_name
