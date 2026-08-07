"""Tenant Isolation Suite (Constitution I.1) — repaired in Phase 14B.1.

WHY THIS FILE WAS REWRITTEN
---------------------------
The previous version never executed under pytest. Three independent causes:

  1. tests/conftest.py installs a bare `sys.modules["app"]` stub at collection
     time (see its docstring — it exists to stop the memory-provider suites
     contaminating each other). A bare ModuleType has no __path__, so
     `from app import create_app` raised
     "ImportError: cannot import name 'create_app' from 'app' (unknown
     location)". Every real-model suite in this repo purges those stubs first;
     this file predated that convention and never did.

  2. It was not a pytest suite. Assertions ran at IMPORT time into a `results`
     list via a `chk()` helper, with zero `def test_*` functions. Even with
     cause 1 fixed, pytest would have collected 0 tests and any failure would
     surface as a collection error rather than a named failing test.

  3. It ended in `sys.exit(0 if passed == len(results) else 1)` at module
     scope, which terminates the pytest process during collection.

`python tests/test_tenant_isolation.py` worked; `pytest` never did. So the one
suite whose job is proving tenant isolation has been absent from every
regression run while appearing to exist — the worst possible state for a
security suite, because it reads as coverage.

WHAT WAS PRESERVED
------------------
Every check from the original script, converted to a named test:
resolve_tenant_id() precedence, per-tenant rows for a duplicated phone,
phone_exists scoping, cross-tenant task read/update/delete refusal, tenant-
scoped log writes, and end-to-end webhook routing (known/unknown/suspended).

WHAT WAS ADDED
--------------
Route-layer coverage. The original suite stopped at the SERVICE layer, which
is tenant-aware and passes. The C1 defects live in the ROUTE layer, so the old
suite would not have caught them even had it run. That gap is why the defects
reached production unnoticed, and closing it is the point of this phase.

EXPECTED RESULT
---------------
This suite is EXPECTED TO FAIL until Phase 14B.2. Each failure is a real
cross-tenant defect, not a flaky test. They are deliberately NOT marked xfail:
a green run would hide precisely what this phase exists to expose. Tests
carrying known defects are named test_KNOWN_DEFECT_*.

NO PRODUCTION CODE IS MODIFIED BY THIS PHASE.
"""
import os
import sys
import tempfile

import pytest

# ── Root cause 1: purge the conftest stubs before importing the real app ────
for _m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
    del sys.modules[_m]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB = os.path.join(tempfile.gettempdir(), "phase_14b1_isolation.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("ADMIN_KEY", "testkey")
os.environ.setdefault("SECRET_KEY", "testsecret")
os.environ.setdefault("BROADCAST_API_KEY", "testbroadcast")
os.environ.setdefault("AUTH_MODE", "SESSION_ONLY")
os.environ.setdefault("GEMINI_API_KEY", "")
if not os.environ.get("WABA_ENCRYPTION_KEY"):
    from cryptography.fernet import Fernet
    os.environ["WABA_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

# Tenant B's id sorts FIRST: if any code path regresses to Tenant.query.first()
# instead of PRIMARY_TENANT_ID, tests expecting tenant A fail loudly. That is
# the exact regression which mis-filed 25 lead_event rows in production.
A_ID = "tenant-a-primary"
B_ID = "aaa-tenant-b-first"
S_ID = "zzz-tenant-suspended"
os.environ["PRIMARY_TENANT_ID"] = A_ID

from app import create_app                                              # noqa: E402
from app.extensions import db                                           # noqa: E402
from app.models import (                                                # noqa: E402
    Tenant, User, ConversationState, ConversationMessage, LeadEvent,
    MessageLog, Task, PipelineStage, PipelineDefinition, FollowUpJob,
)
from app.services.sales_pipeline_seed import SalesPipelineSeeder        # noqa: E402

_APP = create_app()
_APP.config["TESTING"] = True

# The same phone number, deliberately present in BOTH tenants. Legitimate in
# reality — one person may be a lead at two institutes — and the sharpest probe
# available, because any lookup keyed on phone alone matches an arbitrary row.
SHARED_PHONE = "919000000001"
B_ONLY_PHONE = "919000000002"
A_ONLY_PHONE = "919000000003"
# A second shared number, seeded with B's row FIRST. An unscoped
# .filter_by(phone=...).first() therefore returns TENANT B's row, so a defect
# in a phone-keyed lookup is deterministic rather than dependent on which
# tenant happens to sit earlier in the table. Without this, a caller from
# tenant A "accidentally" hits its own row and the defect hides.
SHARED_PHONE_B_FIRST = "919000000004"


def _lead(phone, tenant_id, name, staff=None):
    lead = ConversationState(
        phone=phone, name=name, tenant_id=tenant_id, stage="new",
        course="", goal="", batch_time="", offer_course="",
        last_msg="", last_text="", lead_status="Lead",
        assigned_staff=staff, lead_score=50,
    )
    db.session.add(lead)
    db.session.commit()
    return lead


def _seed():
    """Create the two-tenant world. Caller supplies the app context."""
    db.session.remove()
    db.drop_all()
    db.create_all()

    db.session.add_all([
        Tenant(id=B_ID, name="Tenant B", slug="tenant-b", status="ACTIVE",
               waba_phone_number_id="PHONE_B", billing_exempt=True),
        Tenant(id=A_ID, name="Tenant A", slug="tenant-a", status="ACTIVE",
               waba_phone_number_id="PHONE_A", billing_exempt=True),
        Tenant(id=S_ID, name="Suspended", slug="suspended",
               status="SUSPENDED", waba_phone_number_id="PHONE_S",
               billing_exempt=True),
    ])
    db.session.commit()
    SalesPipelineSeeder(dry_run=False).run()

    from werkzeug.security import generate_password_hash
    # Phase H3-1B-b: 'staff-a'/'staff-b' must exist as real Users now.
    # task_service refuses an assignee who is not staff of the acting tenant,
    # so these fixtures create them rather than relying on assigned_staff
    # being free text — which is exactly the assumption H3 removed. Note they
    # are created PER TENANT: 'staff-a' in A only, 'staff-b' in B only, which
    # keeps the cross-tenant assertions meaningful.
    for tid, uname in ((A_ID, "admin-a"), (B_ID, "admin-b"),
                       (A_ID, "staff-a"), (B_ID, "staff-b")):
        db.session.add(User(username=uname, email=f"{uname}@x.test",
                            password_hash=generate_password_hash("pw"),
                            role="ADMIN" if uname.startswith("admin") else "STAFF",
                            tenant_id=tid, is_active=True,
                            require_password_change=False))
    db.session.commit()

    _lead(SHARED_PHONE, A_ID, "Alice-A", staff="staff-a")
    _lead(SHARED_PHONE, B_ID, "Bob-B", staff="staff-b")
    _lead(B_ONLY_PHONE, B_ID, "OnlyB", staff=None)
    _lead(A_ONLY_PHONE, A_ID, "OnlyA", staff=None)
    _lead(SHARED_PHONE_B_FIRST, B_ID, "BobFirst-B", staff="staff-b")
    _lead(SHARED_PHONE_B_FIRST, A_ID, "AliceSecond-A", staff="staff-a")


@pytest.fixture()
def iso():
    """Setup-only fixture for ROUTE tests.

    Deliberately does NOT hold an app context open. Flask reuses an already-
    pushed app context instead of creating a fresh one per request, so a
    fixture that held one would let per-request state (flask.g, the scoped
    session, tenant ContextVars) survive from one test_client request into the
    next. That produced a convincing but FALSE cross-tenant leak during 14B.1:
    whichever tenant requested first, both appeared to see that tenant's lead.
    Production never shares an app context between requests.

    Route tests therefore call the client with no context pushed, and open
    their own short-lived context for database assertions via `dbctx()`.
    """
    with _APP.app_context():
        _seed()
    yield
    with _APP.app_context():
        db.session.remove()


@pytest.fixture()
def iso_db():
    """Fixture for SERVICE/MODEL tests, which issue no HTTP requests and are
    therefore unaffected by the context-reuse hazard described in `iso`."""
    with _APP.app_context():
        _seed()
        yield
        db.session.remove()


def dbctx():
    """Short-lived app context for assertions in route tests."""
    return _APP.app_context()


def _client(tenant_id):
    """A test client authenticated as that tenant's ADMIN.

    Opens its own short-lived context to resolve the user id, then returns a
    client with NO context pushed — see `iso` for why that matters.
    """
    with _APP.app_context():
        user_id = User.query.filter_by(tenant_id=tenant_id,
                                       role="ADMIN").first().id
    client = _APP.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return client


def _staff_of(phone, tenant_id):
    """assigned_staff for one lead, read in its own context."""
    with dbctx():
        return _row(phone, tenant_id).assigned_staff


def _row(phone, tenant_id):
    return ConversationState.query.filter_by(phone=phone,
                                             tenant_id=tenant_id).first()


def _stages(tenant_id):
    return (db.session.query(PipelineStage)
            .join(PipelineDefinition,
                  PipelineStage.pipeline_id == PipelineDefinition.id)
            .filter(PipelineDefinition.tenant_id == tenant_id)
            .order_by(PipelineStage.order_index).all())


def wa_payload(phone_number_id, from_number, text, wamid):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone_number_id},
        "messages": [{"from": from_number, "type": "text", "id": wamid,
                      "text": {"body": text}}],
        "contacts": [{"profile": {"name": "WebhookUser"}}],
    }}]}]}


# ═══ Preserved from the original script ══════════════════════════════════════

class TestResolveTenantId:
    """The crutch regression: None must resolve to PRIMARY_TENANT_ID, never to
    Tenant.query.first()."""

    def test_precondition_first_row_is_not_the_primary_tenant(self, iso_db):
        assert Tenant.query.first().id == B_ID, "fixture ordering broken"

    def test_explicit_tenant_id_always_wins(self, iso_db):
        from app.services.log_service import resolve_tenant_id
        assert resolve_tenant_id(B_ID) == B_ID

    def test_none_resolves_to_primary_not_first_row(self, iso_db):
        from app.services.log_service import resolve_tenant_id
        assert resolve_tenant_id(None) == A_ID
        assert resolve_tenant_id(None) != B_ID


class TestServiceLayerLeadIsolation:
    def test_same_phone_creates_separate_rows(self, iso_db):
        assert ConversationState.query.filter_by(phone=SHARED_PHONE).count() == 2

    def test_reads_return_the_callers_own_row(self, iso_db):
        from app.state import get_or_create_state
        assert get_or_create_state(SHARED_PHONE, "x", tenant_id=A_ID)["name"] == "Alice-A"
        assert get_or_create_state(SHARED_PHONE, "x", tenant_id=B_ID)["name"] == "Bob-B"

    def test_phone_exists_is_tenant_scoped(self, iso_db):
        from app.state import phone_exists
        assert phone_exists(B_ONLY_PHONE, tenant_id=B_ID) is True
        assert phone_exists(B_ONLY_PHONE, tenant_id=A_ID) is False

    def test_write_through_one_scope_does_not_touch_the_other(self, iso_db):
        from app.state import get_or_create_state
        st = get_or_create_state(SHARED_PHONE, "Alice-A", tenant_id=A_ID)
        st["stage"] = "course_viewed"
        assert _row(SHARED_PHONE, B_ID).stage != "course_viewed"


class TestTaskIsolation:
    def _b_task(self):
        from app.services.task_service import create_task
        create_task(tenant_id=B_ID, title="B secret task",
                    created_by="admin-b", assigned_staff="staff-b")
        return Task.query.filter_by(tenant_id=B_ID).first()

    def test_task_list_excludes_the_other_tenants_task(self, iso_db):
        from app.services.task_service import list_tasks
        self._b_task()
        assert all(t.title != "B secret task" for t in list_tasks(tenant_id=A_ID))

    def test_cross_tenant_task_update_is_refused(self, iso_db):
        from app.services.task_service import staff_update
        b_task = self._b_task()
        with pytest.raises(Exception):
            staff_update(tenant_id=A_ID, task_id=b_task.id, actor="staff-a",
                         is_admin=True, status="COMPLETED")
        db.session.rollback()
        assert Task.query.filter_by(tenant_id=B_ID).first().status != "COMPLETED"

    def test_cross_tenant_task_delete_is_refused(self, iso_db):
        from app.services.task_service import delete_task
        b_task = self._b_task()
        with pytest.raises(Exception):
            delete_task(tenant_id=A_ID, task_id=b_task.id, actor="admin-a")
        db.session.rollback()
        assert Task.query.filter_by(tenant_id=B_ID, id=b_task.id).count() == 1


class TestLogWritesAreTenantScoped:
    def test_all_three_log_writes_land_under_the_given_tenant(self, iso_db):
        from app.services.log_service import (
            log_message, save_conversation_message, log_lead_event)
        log_message(SHARED_PHONE, "inbound", "user", "hello A", tenant_id=A_ID)
        save_conversation_message(SHARED_PHONE, "incoming", "hello A", tenant_id=A_ID)
        log_lead_event(SHARED_PHONE, "COURSE_VIEWED", tenant_id=A_ID)

        for model in (MessageLog, ConversationMessage, LeadEvent):
            assert model.query.filter_by(tenant_id=A_ID).count() == 1
            assert model.query.filter_by(tenant_id=B_ID).count() == 0

    def test_implicit_write_files_under_primary_not_first_row(self, iso_db):
        from app.services.log_service import log_lead_event
        log_lead_event(A_ONLY_PHONE, "LEAD_CREATED", tenant_id=None)
        assert LeadEvent.query.filter_by(tenant_id=A_ID,
                                         phone=A_ONLY_PHONE).count() == 1
        assert LeadEvent.query.filter_by(tenant_id=B_ID,
                                         phone=A_ONLY_PHONE).count() == 0


class TestWebhookRouting:
    def test_message_to_known_waba_lands_under_that_tenant_only(self, iso):
        _APP.test_client().post(
            "/webhook", json=wa_payload("PHONE_B", "919111111111", "hi", "w.1"))
        with dbctx():
            assert ConversationState.query.filter_by(
                phone="919111111111", tenant_id=B_ID).count() == 1
            assert ConversationState.query.filter_by(
                phone="919111111111", tenant_id=A_ID).count() == 0

    def test_unknown_waba_id_creates_nothing(self, iso):
        _APP.test_client().post(
            "/webhook", json=wa_payload("PHONE_UNKNOWN", "919222222222", "hi", "w.2"))
        with dbctx():
            assert ConversationState.query.filter_by(phone="919222222222").count() == 0

    def test_suspended_tenants_message_is_dropped(self, iso):
        _APP.test_client().post(
            "/webhook", json=wa_payload("PHONE_S", "919333333333", "hi", "w.3"))
        with dbctx():
            assert ConversationState.query.filter_by(phone="919333333333").count() == 0


# ═══ The seven isolation domains ═════════════════════════════════════════════

class TestConversationIsolation:
    def test_messages_are_scoped_to_their_tenant(self, iso_db):
        for tid in (A_ID, B_ID):
            db.session.add(ConversationMessage(
                tenant_id=tid, phone=SHARED_PHONE, direction="incoming",
                message=f"secret-{tid}", wa_message_id=f"wamid-{tid}"))
        db.session.commit()
        a = ConversationMessage.query.filter_by(tenant_id=A_ID).all()
        assert all(B_ID not in (m.message or "") for m in a)

    def test_message_counts_are_independent(self, iso_db):
        db.session.add(ConversationMessage(
            tenant_id=B_ID, phone=SHARED_PHONE, direction="incoming",
            message="b", wa_message_id="wamid-b1"))
        db.session.commit()
        assert ConversationMessage.query.filter_by(tenant_id=A_ID).count() == 0
        assert ConversationMessage.query.filter_by(tenant_id=B_ID).count() == 1


class TestPipelineIsolation:
    def test_each_tenant_has_its_own_stage_rows(self, iso_db):
        a = {s.id for s in _stages(A_ID)}
        b = {s.id for s in _stages(B_ID)}
        assert a and b and not (a & b), "stage rows must never be shared"

    def test_a_lead_links_to_its_own_tenants_stage(self, iso_db):
        assert _row(SHARED_PHONE, A_ID).sales_stage_id in {s.id for s in _stages(A_ID)}
        assert _row(SHARED_PHONE, B_ID).sales_stage_id in {s.id for s in _stages(B_ID)}

    def test_pipeline_summary_counts_only_the_callers_tenant(self, iso_db):
        from app.services import sales_pipeline_service as sps
        counted = sum(s["lead_count"] for s in sps.get_pipeline_summary(A_ID, None))
        assert counted == ConversationState.query.filter_by(tenant_id=A_ID).count()

    def test_get_stage_refuses_another_tenants_stage(self, iso_db):
        from app.services import sales_pipeline_service as sps
        assert sps.get_stage(A_ID, _stages(B_ID)[0].id) is None

    def test_transition_engine_derives_from_the_callers_tenant(self, iso_db):
        from app.services import sales_transition_service as sts
        b_stage = _stages(B_ID)[6]
        b_stage.display_name = "Haggling"
        db.session.commit()
        assert "Haggling" in sts.describe_allowed_transitions("Lead", tenant_id=B_ID)
        assert "Haggling" not in sts.describe_allowed_transitions("Lead", tenant_id=A_ID)


class TestCampaignIsolation:
    def test_campaign_rows_are_tenant_scoped(self, iso_db):
        from app.models import Campaign
        db.session.add(Campaign(tenant_id=B_ID, name="B campaign",
                                message_body="hi", status="draft"))
        db.session.commit()
        assert Campaign.query.filter_by(tenant_id=A_ID).count() == 0
        assert Campaign.query.filter_by(tenant_id=B_ID).count() == 1

    def test_campaign_list_page_hides_the_other_tenants_campaign(self, iso):
        from app.models import Campaign
        with dbctx():
            db.session.add(Campaign(tenant_id=B_ID, name="B-SECRET-CAMPAIGN",
                                    message_body="hi", status="draft"))
            db.session.commit()
        body = _client(A_ID).get("/crm/campaigns/center").get_data(as_text=True)
        assert "B-SECRET-CAMPAIGN" not in body


class TestStaffIsolation:
    def test_users_are_scoped_by_tenant(self, iso_db):
        """Asserts the SCOPING, not a head-count.

        This previously pinned `count() == 1`, which broke in H3-1B-b when the
        fixture gained a 'staff-a' User — needed because task_service now
        refuses an assignee who is not real staff. The cardinality was
        incidental; what the test is about is that tenant A sees only tenant
        A's users, and that B's staff never appear here.
        """
        a_users = User.query.filter_by(tenant_id=A_ID).all()
        assert {u.username for u in a_users} == {"admin-a", "staff-a"}
        assert all(u.tenant_id == A_ID for u in a_users)
        assert "staff-b" not in {u.username for u in a_users}

    def test_username_may_repeat_across_tenants(self, iso_db):
        """Uniqueness is per-tenant by constraint — two institutes may both
        employ a 'priya'. Any lookup by username alone is therefore unsafe."""
        from werkzeug.security import generate_password_hash
        db.session.add(User(username="admin-a", email="dup@x.test",
                            password_hash=generate_password_hash("pw"),
                            role="STAFF", tenant_id=B_ID, is_active=True))
        db.session.commit()
        assert User.query.filter_by(username="admin-a").count() == 2

    def test_staff_page_does_not_list_the_other_tenants_staff(self, iso):
        from werkzeug.security import generate_password_hash
        with dbctx():
            db.session.add(User(username="staff-b-secret", email="sb@x.test",
                                password_hash=generate_password_hash("pw"),
                                role="STAFF", tenant_id=B_ID, is_active=True))
            db.session.commit()
        body = _client(A_ID).get("/crm/staff-management").get_data(as_text=True)
        assert "staff-b-secret" not in body


class TestDashboardIsolation:
    def test_lead_counts_are_independent(self, iso_db):
        assert ConversationState.query.filter_by(tenant_id=A_ID).count() == 3
        assert ConversationState.query.filter_by(tenant_id=B_ID).count() == 3

    def test_leads_page_does_not_render_the_other_tenants_lead(self, iso):
        body = _client(A_ID).get("/crm/leads").get_data(as_text=True)
        assert "Bob-B" not in body, "tenant B lead rendered to tenant A"
        assert "OnlyB" not in body

    def test_pipeline_page_does_not_render_the_other_tenants_lead(self, iso):
        body = _client(A_ID).get("/crm/pipeline").get_data(as_text=True)
        assert "Bob-B" not in body and "OnlyB" not in body

    def test_dashboard_loads_for_each_tenant_independently(self, iso):
        assert _client(A_ID).get("/crm/home").status_code in (200, 302)
        assert _client(B_ID).get("/crm/home").status_code in (200, 302)


class TestDashboardCountsAreScoped:
    """Phase 14B.4 — aggregate figures rendered to a tenant must be derived
    from that tenant's rows only. A count is a smaller leak than a name, but
    it is still another institute's data on this institute's screen."""

    @staticmethod
    def _render_context(client, path):
        """Capture the context a template was rendered with, so the assertion
        is on the VALUE the page received rather than on scraped HTML."""
        from flask import template_rendered
        captured = {}

        def record(sender, template, context, **extra):
            captured.update(context)

        template_rendered.connect(record, _APP)
        try:
            client.get(path)
        finally:
            template_rendered.disconnect(record, _APP)
        return captured

    def test_pending_followups_on_the_leads_page_excludes_other_tenants(self, iso):
        from datetime import datetime
        with dbctx():
            for i in range(7):                       # tenant B only
                db.session.add(FollowUpJob(
                    phone=B_ONLY_PHONE, name="x", tenant_id=B_ID, done=False,
                    send_at=datetime.utcnow(), message="m", day=1))
            db.session.commit()

        ctx = self._render_context(_client(A_ID), "/crm/leads")
        assert ctx, "no template rendered"
        assert ctx.get("pending_fu") == 0, (
            f"tenant A shown {ctx.get('pending_fu')} pending follow-ups; "
            f"tenant B owns 7 and tenant A owns none")

    def test_pending_followups_counts_the_callers_own_jobs(self, iso):
        from datetime import datetime
        with dbctx():
            db.session.add(FollowUpJob(
                phone=A_ONLY_PHONE, name="x", tenant_id=A_ID, done=False,
                send_at=datetime.utcnow(), message="m", day=1))
            db.session.commit()

        ctx = self._render_context(_client(A_ID), "/crm/leads")
        assert ctx.get("pending_fu") == 1


class TestAuditIsolation:
    def test_audit_rows_carry_the_actors_tenant(self, iso_db):
        from app.services.audit_service import log_audit
        from app.models import AuditLog
        log_audit("LEAD_UPDATE", actor="admin-b", tenant_id=B_ID,
                  target="lead:x", detail={"k": "v"})
        assert AuditLog.query.filter_by(tenant_id=A_ID).count() == 0
        assert AuditLog.query.filter_by(tenant_id=B_ID).count() == 1

    def test_lead_events_are_tenant_scoped(self, iso_db):
        db.session.add(LeadEvent(tenant_id=B_ID, phone=SHARED_PHONE,
                                 event_type="TEST", event_data="{}"))
        db.session.commit()
        assert LeadEvent.query.filter_by(tenant_id=A_ID).count() == 0

    def test_message_logs_are_tenant_scoped(self, iso_db):
        db.session.add(MessageLog(tenant_id=B_ID, phone=SHARED_PHONE,
                                  direction="outbound", message_type="ai",
                                  message_text="b"))
        db.session.commit()
        assert MessageLog.query.filter_by(tenant_id=A_ID).count() == 0


# ═══ Explicit regression requirements (item 4) ═══════════════════════════════

class TestDuplicatePhoneNeverLeaks:
    def test_the_two_rows_are_distinct_records(self, iso_db):
        assert _row(SHARED_PHONE, A_ID).id != _row(SHARED_PHONE, B_ID).id

    def test_unscoped_lookup_is_ambiguous_by_construction(self, iso_db):
        """Documents WHY phone-keyed lookups are unsafe: two rows match, and
        .first() picks whichever the database returns."""
        assert ConversationState.query.filter_by(phone=SHARED_PHONE).count() == 2

    def test_lead_detail_page_shows_only_the_callers_row(self, iso):
        body = _client(A_ID).get(f"/crm/lead/{SHARED_PHONE}").get_data(as_text=True)
        assert "Bob-B" not in body, "tenant B's lead rendered for tenant A"

    def test_each_tenant_sees_its_own_name_for_the_shared_phone(self, iso):
        a_body = _client(A_ID).get(f"/crm/lead/{SHARED_PHONE}").get_data(as_text=True)
        b_body = _client(B_ID).get(f"/crm/lead/{SHARED_PHONE}").get_data(as_text=True)
        assert "Alice-A" in a_body or "Bob-B" not in a_body
        assert "Alice-A" not in b_body


class TestTenantQueryScoping:
    def test_scopes_when_a_tenant_id_is_given(self, iso_db):
        from app.routes.admin import tenant_query
        rows = tenant_query(ConversationState, A_ID).all()
        assert rows and all(r.tenant_id == A_ID for r in rows)

    def test_scopes_to_the_logged_in_user(self, iso):
        """Exercised through a real request so current_user is populated."""
        assert "Alice-A" not in _client(B_ID).get("/crm/leads").get_data(as_text=True)

    def test_tenant_filter_scopes_when_a_tenant_id_is_given(self, iso_db):
        from app.routes.admin import tenant_filter
        q = tenant_filter(db.session.query(ConversationState),
                          ConversationState, A_ID)
        rows = q.all()
        assert rows and all(r.tenant_id == A_ID for r in rows)

    def test_tenant_filter_fails_closed_with_no_tenant_context(self, iso_db):
        """Phase 14B.4 — the twin of C2. tenant_filter() carried the identical
        fail-open defect and had NO test coverage: reverting its fix broke
        nothing, which is how the defect survived the C2 phase. Two
        similarly-named primitives behaving oppositely under the same failure
        is worse than either being wrong alone."""
        from app.routes.admin import tenant_filter
        q = tenant_filter(db.session.query(ConversationState),
                          ConversationState, None)
        assert q.all() == [], "tenant_filter fell open"

    def test_KNOWN_DEFECT_C2_fails_open_with_no_tenant_context(self, iso_db):
        """C2 — tenant_query() returns model.query UNFILTERED when it cannot
        resolve a tenant. The primitive the entire codebase trusts defaults to
        exposing every tenant's rows.

        It must fail CLOSED: no tenant context means no rows, so a missing
        scope surfaces as a visible bug instead of a silent leak."""
        from app.routes.admin import tenant_query
        rows = tenant_query(ConversationState, None).all()
        assert rows == [], (
            f"tenant_query fell open: {len(rows)} rows across "
            f"{len({r.tenant_id for r in rows})} tenants")


class TestEveryLeadLookupPathIsTenantAware:
    """Item 4c — the five route-layer lookups the audit identified as C1.

    These drive the REAL routes through a REAL client authenticated as tenant
    A's ADMIN. Service-layer tests cannot catch these, which is exactly why the
    defects survived: the previous suite stopped at the service boundary.
    """

    def test_KNOWN_DEFECT_C1_auto_assign_preview_leaks_other_tenants_leads(self, iso):
        """/crm/leads/unassigned/auto-assign-preview selects every tenant's
        unassigned leads, disclosing names, phones and scores."""
        body = _client(A_ID).post(
            "/crm/leads/unassigned/auto-assign-preview").get_data(as_text=True)
        assert B_ONLY_PHONE not in body and "OnlyB" not in body, (
            "tenant B's unassigned lead disclosed to tenant A")

    def test_KNOWN_DEFECT_C1_reassignment_preview_leaks_by_phone(self, iso):
        """/crm/reassignment-center/preview resolves phones with no tenant
        filter."""
        body = _client(A_ID).post(
            "/crm/reassignment-center/preview",
            json={"phones": [B_ONLY_PHONE], "target_staff": "staff-a"},
        ).get_data(as_text=True)
        assert "OnlyB" not in body, "tenant B's lead disclosed to tenant A"

    def test_KNOWN_DEFECT_C1_reassignment_confirm_modifies_other_tenant(self, iso):
        before = _staff_of(B_ONLY_PHONE, B_ID)
        _client(A_ID).post("/crm/reassignment-center/confirm",
                           json={"phones": [B_ONLY_PHONE],
                                 "target_staff": "staff-a"})
        assert _staff_of(B_ONLY_PHONE, B_ID) == before, (
            "tenant A modified tenant B's lead")

    def test_KNOWN_DEFECT_C1_unassigned_assign_modifies_by_phone(self, iso):
        """filter_by(phone=...).first() returns whichever row the database
        yields first, with no tenant predicate. SHARED_PHONE_B_FIRST is seeded
        so that row belongs to tenant B — so tenant A's assignment lands on
        tenant B's lead."""
        before_b = _staff_of(SHARED_PHONE_B_FIRST, B_ID)
        assert before_b == "staff-b"
        _client(A_ID).post("/crm/leads/unassigned/assign",
                           data={"phone": SHARED_PHONE_B_FIRST,
                                 "target_staff": "staff-a-new"})
        assert _staff_of(SHARED_PHONE_B_FIRST, B_ID) == before_b, (
            "tenant A's assignment altered tenant B's row")

    def test_KNOWN_DEFECT_C1_auto_assign_confirm_modifies_other_tenant(self, iso):
        before = _staff_of(B_ONLY_PHONE, B_ID)
        _client(A_ID).post("/crm/leads/unassigned/auto-assign-confirm",
                           json={"assignments": [{"phone": B_ONLY_PHONE,
                                                  "target_staff": "staff-a"}]})
        assert _staff_of(B_ONLY_PHONE, B_ID) == before, (
            "tenant A auto-assigned tenant B's lead")


class TestSuiteActuallyRuns:
    """Guards the three root causes this phase repaired. If any regresses the
    suite goes silent again — and a silent security suite is worse than none,
    because it reads as coverage."""

    def test_app_package_is_the_real_one_not_the_conftest_stub(self):
        import app
        assert hasattr(app, "create_app"), "conftest stub leaked back in"
        assert getattr(app, "__file__", None), "app package has no file location"

    def test_module_exposes_collectable_pytest_classes(self):
        import inspect
        mod = sys.modules[__name__]
        classes = [n for n, o in inspect.getmembers(mod)
                   if inspect.isclass(o) and n.startswith("Test")]
        assert len(classes) >= 12, "suite must be pytest classes, not a script"

    def test_module_has_no_import_time_sys_exit(self):
        """The original ended in sys.exit(), which killed collection."""
        import ast
        path = os.path.abspath(__file__)
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in tree.body:                      # module scope only
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "exit"):
                    if isinstance(node, (ast.Expr, ast.If)):
                        raise AssertionError("module-scope sys.exit() returned")
