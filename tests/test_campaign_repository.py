"""
Phase 8.2A — CampaignRepository + CAMPAIGN_ENGINE_V2 flag tests.

The repository is exercised against REAL SQLAlchemy models on an in-memory
SQLite database, so the schema, relationship, UNIQUE constraint and NOT NULL
guards are all genuinely enforced rather than mocked.

app.models is heavy (imports flask_sqlalchemy + the whole app package), so the
models are declared here as an independent mapping that mirrors the production
schema for the two campaign tables. The repository is model-injectable by
design, which is precisely what makes this possible without an app bootstrap.

Central guarantees under test:
  * every method is tenant-scoped — cross-tenant access returns None / no-ops
  * the repository NEVER commits (transaction boundary belongs to the service)
  * no lifecycle validation happens here (that is CampaignService, 8.2B)
  * the flag is dynamic and defaults OFF
"""
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta

import pytest
from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, Text,
    UniqueConstraint, create_engine,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(unique_name, relpath):
    path = os.path.join(_ROOT, relpath)
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


repo_mod = _load("_p82a_repo", "app/persistence/campaign_repository.py")
flags = _load("_p82a_flags", "app/flags.py")


# ── Test mapping mirroring the production campaign schema ────────────────────
_Base = declarative_base()


class _Campaign(_Base):
    __tablename__ = "campaigns"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(String(36), nullable=False)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    status = Column(String(20), nullable=False, default="draft")
    message_body = Column(Text)
    template_id = Column(Integer)
    audience_rule_id = Column(Integer)
    scheduled_at = Column(DateTime)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    total_recipients = Column(Integer, nullable=False, default=0)
    sent_count = Column(Integer, nullable=False, default=0)
    failed_count = Column(Integer, nullable=False, default=0)
    created_by = Column(String(120))
    failure_reason = Column(Text)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                        onupdate=datetime.utcnow)
    recipients = relationship("_CampaignRecipient", back_populates="campaign",
                              cascade="all, delete-orphan", lazy="dynamic")


class _CampaignRecipient(_Base):
    __tablename__ = "campaign_recipients"
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"),
                         nullable=False)
    tenant_id = Column(String(36), nullable=False)
    phone = Column(String(20), nullable=False)
    name = Column(String(200))
    status = Column(String(20), nullable=False, default="queued")
    send_at = Column(DateTime)
    retry_count = Column(Integer, nullable=False, default=0)
    last_attempt_at = Column(DateTime)
    failure_reason = Column(Text)
    wa_message_id = Column(String(100))
    sent_at = Column(DateTime)
    delivered_at = Column(DateTime)
    read_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                        onupdate=datetime.utcnow)
    campaign = relationship("_Campaign", back_populates="recipients")
    __table_args__ = (
        UniqueConstraint("campaign_id", "phone",
                         name="uq_campaign_recipient_campaign_phone"),
    )


T1, T2 = "tenant-one", "tenant-two"


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    _Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def repo(session):
    return repo_mod.CampaignRepository(
        session=session, campaign_model=_Campaign, recipient_model=_CampaignRecipient
    )


# ── Feature flag ─────────────────────────────────────────────────────────────
class TestCampaignEngineFlag:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv(flags.CAMPAIGN_ENGINE_V2, raising=False)
        assert flags.campaign_engine_v2_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " on "])
    def test_truthy(self, monkeypatch, value):
        monkeypatch.setenv(flags.CAMPAIGN_ENGINE_V2, value)
        assert flags.campaign_engine_v2_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
    def test_falsey(self, monkeypatch, value):
        monkeypatch.setenv(flags.CAMPAIGN_ENGINE_V2, value)
        assert flags.campaign_engine_v2_enabled() is False

    def test_dynamic_reread(self, monkeypatch):
        """Instant rollback: a live env change is honoured without re-import."""
        monkeypatch.setenv(flags.CAMPAIGN_ENGINE_V2, "true")
        assert flags.campaign_engine_v2_enabled() is True
        monkeypatch.setenv(flags.CAMPAIGN_ENGINE_V2, "false")
        assert flags.campaign_engine_v2_enabled() is False

    def test_independent_of_other_flags(self, monkeypatch):
        monkeypatch.setenv(flags.CAMPAIGN_ENGINE_V2, "true")
        for other in (flags.STATE_ENGINE_V2, flags.WA_LIST_MESSAGES):
            monkeypatch.delenv(other, raising=False)
        assert flags.campaign_engine_v2_enabled() is True
        assert flags.state_engine_v2_enabled() is False
        assert flags.wa_list_messages_enabled() is False


# ── create_campaign ──────────────────────────────────────────────────────────
class TestCreateCampaign:
    def test_creates_and_assigns_pk(self, repo):
        c = repo.create_campaign(T1, "Diwali Offer")
        assert c.id is not None            # flushed
        assert c.tenant_id == T1
        assert c.status == "draft"         # model default

    def test_optional_fields_persisted(self, repo):
        when = datetime(2026, 8, 1, 10, 0)
        c = repo.create_campaign(
            T1, "Scheduled", description="d", message_body="hello",
            template_id=7, audience_rule_id=3, scheduled_at=when,
            created_by="admin@oxford",
        )
        assert (c.description, c.message_body) == ("d", "hello")
        assert (c.template_id, c.audience_rule_id) == (7, 3)
        assert c.scheduled_at == when
        assert c.created_by == "admin@oxford"

    def test_explicit_status_is_written_without_validation(self, repo):
        """No lifecycle validation in the repository — 8.2B owns that."""
        c = repo.create_campaign(T1, "X", status="running")
        assert c.status == "running"

    def test_does_not_commit(self, repo, session):
        repo.create_campaign(T1, "Uncommitted")
        session.rollback()
        assert repo.count_for_tenant(T1) == 0


# ── add_recipients ───────────────────────────────────────────────────────────
class TestAddRecipients:
    def test_accepts_plain_phone_strings(self, repo):
        c = repo.create_campaign(T1, "C")
        n = repo.add_recipients(T1, c.id, ["+919000000001", "+919000000002"])
        assert n == 2
        assert repo.count_recipients(T1, c.id) == 2

    def test_accepts_dicts_with_name_and_send_at(self, repo):
        c = repo.create_campaign(T1, "C")
        when = datetime(2026, 8, 1, 9, 0)
        repo.add_recipients(T1, c.id, [
            {"phone": "+919000000001", "name": "Asha", "send_at": when},
        ])
        row = repo.list_recipients(T1, c.id)[0]
        assert (row.name, row.send_at) == ("Asha", when)

    def test_defaults_applied(self, repo):
        c = repo.create_campaign(T1, "C")
        repo.add_recipients(T1, c.id, ["+919000000001"])
        row = repo.list_recipients(T1, c.id)[0]
        assert row.status == "queued"
        assert row.retry_count == 0
        assert row.created_at is not None

    def test_empty_input_is_noop(self, repo):
        c = repo.create_campaign(T1, "C")
        assert repo.add_recipients(T1, c.id, []) == 0
        assert repo.add_recipients(T1, c.id, None) == 0

    def test_blank_phones_skipped(self, repo):
        c = repo.create_campaign(T1, "C")
        n = repo.add_recipients(T1, c.id, ["+919000000001", "", None,
                                           {"phone": ""}])
        assert n == 1

    def test_duplicate_phone_rejected_by_db(self, repo):
        """UNIQUE(campaign_id, phone) is the schema-level double-send guard."""
        c = repo.create_campaign(T1, "C")
        repo.add_recipients(T1, c.id, ["+919000000001"])
        with pytest.raises(IntegrityError):
            repo.add_recipients(T1, c.id, ["+919000000001"])

    def test_same_phone_allowed_in_different_campaigns(self, repo):
        c1 = repo.create_campaign(T1, "C1")
        c2 = repo.create_campaign(T1, "C2")
        repo.add_recipients(T1, c1.id, ["+919000000001"])
        repo.add_recipients(T1, c2.id, ["+919000000001"])
        assert repo.count_recipients(T1, c1.id) == 1
        assert repo.count_recipients(T1, c2.id) == 1


# ── update_status / counters / mark_failed / archive ─────────────────────────
class TestStatusAndCounters:
    def test_update_status(self, repo):
        c = repo.create_campaign(T1, "C")
        out = repo.update_status(T1, c.id, "scheduled")
        assert out.status == "scheduled"

    def test_update_status_stamps_timestamps_when_given(self, repo):
        c = repo.create_campaign(T1, "C")
        t0 = datetime(2026, 8, 1, 8, 0)
        t1 = datetime(2026, 8, 1, 9, 0)
        repo.update_status(T1, c.id, "running", started_at=t0)
        assert c.started_at == t0 and c.completed_at is None
        repo.update_status(T1, c.id, "completed", completed_at=t1)
        assert c.completed_at == t1

    def test_update_status_does_not_validate_transitions(self, repo):
        c = repo.create_campaign(T1, "C")
        assert repo.update_status(T1, c.id, "completed").status == "completed"
        assert repo.update_status(T1, c.id, "draft").status == "draft"

    def test_update_counters_absolute_and_partial(self, repo):
        c = repo.create_campaign(T1, "C")
        repo.update_counters(T1, c.id, total_recipients=10, sent_count=4,
                             failed_count=1)
        assert (c.total_recipients, c.sent_count, c.failed_count) == (10, 4, 1)
        repo.update_counters(T1, c.id, sent_count=9)     # partial update
        assert (c.total_recipients, c.sent_count, c.failed_count) == (10, 9, 1)

    def test_mark_failed(self, repo):
        c = repo.create_campaign(T1, "C")
        when = datetime(2026, 8, 1, 12, 0)
        out = repo.mark_failed(T1, c.id, "no WABA credentials", completed_at=when)
        assert out.status == "failed"
        assert out.failure_reason == "no WABA credentials"
        assert out.completed_at == when

    def test_archive_campaign(self, repo):
        c = repo.create_campaign(T1, "C")
        assert repo.archive_campaign(T1, c.id).status == "archived"

    def test_writes_do_not_commit(self, repo, session):
        c = repo.create_campaign(T1, "C")
        session.commit()                       # baseline committed by caller
        repo.update_status(T1, c.id, "running")
        repo.update_counters(T1, c.id, sent_count=5)
        session.rollback()                     # repository changes discarded
        assert repo.get(T1, c.id).status == "draft"
        assert repo.get(T1, c.id).sent_count == 0


# ── Tenant isolation (the critical property) ─────────────────────────────────
class TestTenantIsolation:
    def test_get_is_tenant_scoped(self, repo):
        c = repo.create_campaign(T1, "T1 campaign")
        assert repo.get(T1, c.id) is not None
        assert repo.get(T2, c.id) is None

    def test_list_and_count_are_tenant_scoped(self, repo):
        repo.create_campaign(T1, "a")
        repo.create_campaign(T1, "b")
        repo.create_campaign(T2, "c")
        assert repo.count_for_tenant(T1) == 2
        assert repo.count_for_tenant(T2) == 1
        assert {c.name for c in repo.list_for_tenant(T2)} == {"c"}

    @pytest.mark.parametrize("method,args", [
        ("update_status", ("running",)),
        ("archive_campaign", ()),
    ])
    def test_cross_tenant_write_is_noop(self, repo, method, args):
        c = repo.create_campaign(T1, "C")
        assert getattr(repo, method)(T2, c.id, *args) is None
        assert repo.get(T1, c.id).status == "draft"   # untouched

    def test_cross_tenant_mark_failed_is_noop(self, repo):
        c = repo.create_campaign(T1, "C")
        assert repo.mark_failed(T2, c.id, "boom") is None
        assert repo.get(T1, c.id).failure_reason is None

    def test_cross_tenant_counters_noop(self, repo):
        c = repo.create_campaign(T1, "C")
        assert repo.update_counters(T2, c.id, sent_count=99) is None
        assert repo.get(T1, c.id).sent_count == 0

    def test_recipients_are_tenant_scoped(self, repo):
        c = repo.create_campaign(T1, "C")
        repo.add_recipients(T1, c.id, ["+919000000001"])
        assert repo.count_recipients(T1, c.id) == 1
        assert repo.count_recipients(T2, c.id) == 0
        assert repo.list_recipients(T2, c.id) == []

    def test_status_breakdown_is_tenant_scoped(self, repo):
        c = repo.create_campaign(T1, "C")
        repo.add_recipients(T1, c.id, ["+919000000001", "+919000000002"])
        assert repo.status_breakdown(T1, c.id) == {"queued": 2}
        assert repo.status_breakdown(T2, c.id) == {}

    def test_wa_message_id_lookup_is_tenant_scoped(self, repo, session):
        c = repo.create_campaign(T1, "C")
        repo.add_recipients(T1, c.id, ["+919000000001"])
        row = repo.list_recipients(T1, c.id)[0]
        row.wa_message_id = "wamid.ABC"
        session.flush()
        assert repo.get_recipient_by_wa_message_id(T1, "wamid.ABC") is not None
        assert repo.get_recipient_by_wa_message_id(T2, "wamid.ABC") is None


# ── Reads / filtering ────────────────────────────────────────────────────────
class TestReads:
    def test_list_filters_by_status_and_orders_newest_first(self, repo, session):
        old = repo.create_campaign(T1, "old")
        new = repo.create_campaign(T1, "new")
        old.created_at = datetime.utcnow() - timedelta(days=1)
        repo.update_status(T1, new.id, "running")
        session.flush()
        assert [c.name for c in repo.list_for_tenant(T1)] == ["new", "old"]
        assert [c.name for c in repo.list_for_tenant(T1, status="running")] == ["new"]

    def test_pagination(self, repo):
        for i in range(5):
            repo.create_campaign(T1, f"c{i}")
        assert len(repo.list_for_tenant(T1, limit=2)) == 2
        assert len(repo.list_for_tenant(T1, limit=2, offset=4)) == 1

    def test_status_breakdown_groups(self, repo, session):
        c = repo.create_campaign(T1, "C")
        repo.add_recipients(T1, c.id, ["+91900000001", "+91900000002",
                                       "+91900000003"])
        rows = repo.list_recipients(T1, c.id)
        rows[0].status = "sent"
        rows[1].status = "failed"
        session.flush()
        assert repo.status_breakdown(T1, c.id) == {"queued": 1, "sent": 1,
                                                   "failed": 1}

    def test_count_recipients_by_status(self, repo, session):
        c = repo.create_campaign(T1, "C")
        repo.add_recipients(T1, c.id, ["+91900000001", "+91900000002"])
        repo.list_recipients(T1, c.id)[0].status = "sent"
        session.flush()
        assert repo.count_recipients(T1, c.id, status="sent") == 1
        assert repo.count_recipients(T1, c.id, status="queued") == 1


# ── Repository purity contract ───────────────────────────────────────────────
class TestRepositoryPurity:
    def test_source_has_no_business_or_io_concerns(self):
        src = open(os.path.join(_ROOT, "app/persistence/campaign_repository.py"),
                   encoding="utf-8").read()
        for forbidden in ("requests", "send_text", "send_template", "whatsapp",
                          "threading", "time.sleep", "log_audit"):
            assert forbidden not in src, forbidden

    def test_never_commits_or_rolls_back(self):
        src = open(os.path.join(_ROOT, "app/persistence/campaign_repository.py"),
                   encoding="utf-8").read()
        assert ".commit()" not in src
        assert ".rollback()" not in src

    def test_does_not_read_feature_flags(self):
        """Gating is the caller's concern; the repository stays flag-agnostic."""
        src = open(os.path.join(_ROOT, "app/persistence/campaign_repository.py"),
                   encoding="utf-8").read()
        assert "campaign_engine_v2_enabled" not in src

    def test_stays_model_injectable(self):
        """No top-level app.models import — that would defeat injectability and
        drag the whole app package into every consumer."""
        src = open(os.path.join(_ROOT, "app/persistence/campaign_repository.py"),
                   encoding="utf-8").read()
        for line in src.splitlines():
            assert not line.startswith("from app.models"), line


class TestStatusConstantDriftGuard:
    """The repository mirrors two status literals instead of importing them.

    These tests read app/models.py as TEXT (no import, so no DATABASE_URL
    needed) and fail loudly if the canonical values are ever renamed.
    """

    def _models_src(self):
        return open(os.path.join(_ROOT, "app/models.py"), encoding="utf-8").read()

    def test_failed_matches_model_constant(self):
        assert f'CAMPAIGN_FAILED    = "{repo_mod.STATUS_FAILED}"' in self._models_src()

    def test_archived_matches_model_constant(self):
        assert f'CAMPAIGN_ARCHIVED  = "{repo_mod.STATUS_ARCHIVED}"' in self._models_src()
