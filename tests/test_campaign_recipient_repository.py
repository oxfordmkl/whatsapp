"""
Phase 8.2C.1 — CampaignRepository recipient-level persistence tests.

Exercises the REAL repository against REAL SQLAlchemy on in-memory SQLite, so
the claim transition, retry metadata, reclaim behaviour and tenant scoping are
genuinely enforced rather than mocked. Only the models are re-declared locally
(app.models needs the whole app package); the repository is model-injectable by
design, which is what makes that possible.

Central guarantees under test:
  * claim moves queued → sending and returns only DUE rows
  * the repository never commits, never rolls back, never computes a backoff
  * retry metadata is persisted but the cap/backoff decisions stay with the worker
  * reclaim recovers rows stranded by a crash mid-send
  * every data method is tenant-scoped; the one global method returns IDs only
"""
import importlib.util
import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, Text,
    UniqueConstraint, create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(unique_name, relpath):
    path = os.path.join(_ROOT, relpath)
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


repo_mod = _load("_p82c1_repo", "app/persistence/campaign_repository.py")

QUEUED = repo_mod.RECIPIENT_QUEUED
SENDING = repo_mod.RECIPIENT_SENDING
SENT = repo_mod.RECIPIENT_SENT
FAILED = repo_mod.RECIPIENT_FAILED

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
NOW = datetime(2026, 8, 1, 12, 0, 0)


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


def _campaign_with(repo, tenant, recipients):
    c = repo.create_campaign(tenant, "C")
    repo.add_recipients(tenant, c.id, recipients)
    return c


# ── claim_next_batch ─────────────────────────────────────────────────────────
class TestClaim:
    def test_claims_queued_and_transitions_to_sending(self, repo):
        c = _campaign_with(repo, T1, ["+919000000001", "+919000000002"])
        rows = repo.claim_next_batch(T1, now=NOW)
        assert len(rows) == 2
        assert all(r.status == SENDING for r in rows)
        assert repo.count_recipients(T1, c.id, status=QUEUED) == 0

    def test_null_send_at_is_due_immediately(self, repo):
        _campaign_with(repo, T1, ["+919000000001"])
        assert len(repo.claim_next_batch(T1, now=NOW)) == 1

    def test_future_send_at_not_claimed(self, repo):
        c = repo.create_campaign(T1, "C")
        repo.add_recipients(T1, c.id, [
            {"phone": "+919000000001", "send_at": NOW + timedelta(hours=1)},
        ])
        assert repo.claim_next_batch(T1, now=NOW) == []

    def test_past_send_at_is_claimed(self, repo):
        c = repo.create_campaign(T1, "C")
        repo.add_recipients(T1, c.id, [
            {"phone": "+919000000001", "send_at": NOW - timedelta(minutes=5)},
        ])
        assert len(repo.claim_next_batch(T1, now=NOW)) == 1

    def test_respects_limit_and_is_ordered(self, repo):
        _campaign_with(repo, T1, [f"+9190000000{i:02d}" for i in range(5)])
        first = repo.claim_next_batch(T1, limit=2, now=NOW)
        assert len(first) == 2
        second = repo.claim_next_batch(T1, limit=2, now=NOW)
        assert [r.id for r in second] == [first[-1].id + 1, first[-1].id + 2]

    def test_already_sending_not_reclaimed_by_claim(self, repo):
        _campaign_with(repo, T1, ["+919000000001"])
        repo.claim_next_batch(T1, now=NOW)
        assert repo.claim_next_batch(T1, now=NOW) == []   # no double-claim

    def test_terminal_rows_never_claimed(self, repo, session):
        c = _campaign_with(repo, T1, ["+919000000001", "+919000000002"])
        rows = repo.list_recipients(T1, c.id)
        rows[0].status = SENT
        rows[1].status = FAILED
        session.flush()
        assert repo.claim_next_batch(T1, now=NOW) == []

    def test_can_scope_to_one_campaign(self, repo):
        c1 = _campaign_with(repo, T1, ["+919000000001"])
        _campaign_with(repo, T1, ["+919000000002"])
        rows = repo.claim_next_batch(T1, campaign_id=c1.id, now=NOW)
        assert len(rows) == 1 and rows[0].campaign_id == c1.id

    def test_claim_is_tenant_scoped(self, repo):
        _campaign_with(repo, T1, ["+919000000001"])
        assert repo.claim_next_batch(T2, now=NOW) == []

    def test_claim_does_not_commit(self, repo, session):
        _campaign_with(repo, T1, ["+919000000001"])
        session.commit()
        repo.claim_next_batch(T1, now=NOW)
        session.rollback()                    # worker would have committed
        rows = repo.list_recipients(T1, 1)
        assert rows[0].status == QUEUED       # claim discarded


# ── mark_recipient_sent ──────────────────────────────────────────────────────
class TestMarkSent:
    def test_records_success(self, repo):
        c = _campaign_with(repo, T1, ["+919000000001"])
        rid = repo.claim_next_batch(T1, now=NOW)[0].id
        row = repo.mark_recipient_sent(T1, rid, wa_message_id="wamid.X",
                                       sent_at=NOW)
        assert row.status == SENT
        assert row.sent_at == NOW and row.last_attempt_at == NOW
        assert row.wa_message_id == "wamid.X"

    def test_does_not_set_delivered_or_read(self, repo):
        """Acceptance by Meta is not delivery — webhooks own those fields."""
        _campaign_with(repo, T1, ["+919000000001"])
        rid = repo.claim_next_batch(T1, now=NOW)[0].id
        row = repo.mark_recipient_sent(T1, rid)
        assert row.delivered_at is None and row.read_at is None

    def test_wa_message_id_optional(self, repo):
        _campaign_with(repo, T1, ["+919000000001"])
        rid = repo.claim_next_batch(T1, now=NOW)[0].id
        assert repo.mark_recipient_sent(T1, rid).wa_message_id is None

    def test_tenant_scoped(self, repo):
        _campaign_with(repo, T1, ["+919000000001"])
        rid = repo.claim_next_batch(T1, now=NOW)[0].id
        assert repo.mark_recipient_sent(T2, rid) is None


# ── failure + retry metadata ─────────────────────────────────────────────────
class TestFailureAndRetry:
    def test_mark_failed_is_terminal_and_counts_attempt(self, repo):
        _campaign_with(repo, T1, ["+919000000001"])
        rid = repo.claim_next_batch(T1, now=NOW)[0].id
        row = repo.mark_recipient_failed(T1, rid, "HTTP 500", attempted_at=NOW)
        assert row.status == FAILED
        assert row.retry_count == 1
        assert row.failure_reason == "HTTP 500"
        assert row.last_attempt_at == NOW

    def test_schedule_retry_returns_to_queue(self, repo):
        _campaign_with(repo, T1, ["+919000000001"])
        rid = repo.claim_next_batch(T1, now=NOW)[0].id
        nxt = NOW + timedelta(minutes=15)
        row = repo.schedule_recipient_retry(T1, rid, "timeout", nxt,
                                            attempted_at=NOW)
        assert row.status == QUEUED
        assert row.retry_count == 1
        assert row.send_at == nxt

    def test_retry_not_claimable_until_backoff_elapses(self, repo):
        """Persists the approved 15-minute backoff without computing it."""
        _campaign_with(repo, T1, ["+919000000001"])
        rid = repo.claim_next_batch(T1, now=NOW)[0].id
        repo.schedule_recipient_retry(T1, rid, "x", NOW + timedelta(minutes=15))
        assert repo.claim_next_batch(T1, now=NOW) == []
        later = NOW + timedelta(minutes=16)
        assert len(repo.claim_next_batch(T1, now=later)) == 1

    def test_retry_counter_accumulates_across_attempts(self, repo):
        """Worker applies the 15/30/45 policy; repository just accumulates."""
        _campaign_with(repo, T1, ["+919000000001"])
        rid = repo.claim_next_batch(T1, now=NOW)[0].id
        for attempt in (1, 2, 3):
            backoff = NOW + timedelta(minutes=15 * attempt)
            repo.schedule_recipient_retry(T1, rid, "x", backoff)
            row = repo.claim_next_batch(T1, now=backoff + timedelta(minutes=1))[0]
            assert row.retry_count == attempt
        repo.mark_recipient_failed(T1, rid, "gave up")
        assert repo._get_recipient(T1, rid).retry_count == 4

    def test_repository_computes_no_backoff(self):
        """Backoff arithmetic must live in the worker, not here."""
        src = open(os.path.join(_ROOT, "app/persistence/campaign_repository.py"),
                   encoding="utf-8").read()
        assert "timedelta" not in src

    def test_failure_writes_are_tenant_scoped(self, repo):
        _campaign_with(repo, T1, ["+919000000001"])
        rid = repo.claim_next_batch(T1, now=NOW)[0].id
        assert repo.mark_recipient_failed(T2, rid, "x") is None
        assert repo.schedule_recipient_retry(T2, rid, "x", NOW) is None
        assert repo._get_recipient(T1, rid).status == SENDING   # untouched


# ── stuck detection + reclaim ────────────────────────────────────────────────
class TestReclaim:
    def _stuck(self, repo, session, attempted_at):
        _campaign_with(repo, T1, ["+919000000001"])
        row = repo.claim_next_batch(T1, now=NOW)[0]
        row.last_attempt_at = attempted_at
        session.flush()
        return row

    def test_finds_stale_sending_rows(self, repo, session):
        self._stuck(repo, session, NOW - timedelta(hours=1))
        found = repo.find_stuck_recipients(T1, stale_before=NOW - timedelta(minutes=30))
        assert len(found) == 1

    def test_recent_sending_not_stale(self, repo, session):
        self._stuck(repo, session, NOW - timedelta(minutes=1))
        assert repo.find_stuck_recipients(T1, stale_before=NOW - timedelta(minutes=30)) == []

    def test_null_last_attempt_treated_as_stale(self, repo):
        """Crash between claim-commit and first attempt leaves no timestamp."""
        _campaign_with(repo, T1, ["+919000000001"])
        repo.claim_next_batch(T1, now=NOW)      # last_attempt_at stays NULL
        assert len(repo.find_stuck_recipients(T1, stale_before=NOW)) == 1

    def test_reclaim_returns_rows_to_queue(self, repo, session):
        self._stuck(repo, session, NOW - timedelta(hours=1))
        n = repo.reclaim_stale_recipients(T1, stale_before=NOW - timedelta(minutes=30))
        assert n == 1
        assert len(repo.claim_next_batch(T1, now=NOW)) == 1   # claimable again

    def test_reclaim_does_not_count_as_retry_by_default(self, repo, session):
        """A stuck row may have been delivered — the attempt is unknown."""
        row = self._stuck(repo, session, NOW - timedelta(hours=1))
        repo.reclaim_stale_recipients(T1, stale_before=NOW)
        assert row.retry_count == 0

    def test_reclaim_can_count_as_retry_when_asked(self, repo, session):
        row = self._stuck(repo, session, NOW - timedelta(hours=1))
        repo.reclaim_stale_recipients(T1, stale_before=NOW, increment_retry=True)
        assert row.retry_count == 1

    def test_reclaim_ignores_non_sending_rows(self, repo, session):
        c = _campaign_with(repo, T1, ["+919000000001", "+919000000002"])
        rows = repo.list_recipients(T1, c.id)
        rows[0].status = SENT
        rows[1].status = FAILED
        session.flush()
        assert repo.reclaim_stale_recipients(T1, stale_before=NOW) == 0

    def test_reclaim_is_tenant_scoped(self, repo, session):
        self._stuck(repo, session, NOW - timedelta(hours=1))
        assert repo.reclaim_stale_recipients(T2, stale_before=NOW) == 0
        assert repo.find_stuck_recipients(T2, stale_before=NOW) == []

    def test_reclaim_does_not_commit(self, repo, session):
        self._stuck(repo, session, NOW - timedelta(hours=1))
        session.commit()
        repo.reclaim_stale_recipients(T1, stale_before=NOW)
        session.rollback()
        assert repo.find_stuck_recipients(T1, stale_before=NOW) != []


# ── pending_tenant_ids (the one global method) ───────────────────────────────
class TestPendingTenantIds:
    def test_lists_tenants_with_due_work(self, repo):
        _campaign_with(repo, T1, ["+919000000001"])
        _campaign_with(repo, T2, ["+919000000002"])
        assert set(repo.pending_tenant_ids(now=NOW)) == {T1, T2}

    def test_excludes_tenants_without_due_work(self, repo):
        c = repo.create_campaign(T2, "C")
        repo.add_recipients(T2, c.id, [
            {"phone": "+919000000002", "send_at": NOW + timedelta(hours=1)},
        ])
        _campaign_with(repo, T1, ["+919000000001"])
        assert repo.pending_tenant_ids(now=NOW) == [T1]

    def test_excludes_claimed_and_terminal(self, repo):
        _campaign_with(repo, T1, ["+919000000001"])
        repo.claim_next_batch(T1, now=NOW)
        assert repo.pending_tenant_ids(now=NOW) == []

    def test_returns_ids_only_no_row_data(self, repo):
        """Global by necessity — must never expose another tenant's content."""
        _campaign_with(repo, T1, ["+919000000001"])
        out = repo.pending_tenant_ids(now=NOW)
        assert out == [T1]
        assert all(isinstance(x, str) for x in out)


# ── purity contract ──────────────────────────────────────────────────────────
class TestRepositoryPurity:
    def _src(self):
        return open(os.path.join(_ROOT, "app/persistence/campaign_repository.py"),
                    encoding="utf-8").read()

    def test_never_commits_or_rolls_back(self):
        src = self._src()
        assert ".commit()" not in src and ".rollback()" not in src

    @pytest.mark.parametrize("forbidden", [
        "requests", "send_text", "send_template", "send_automation",
        "whatsapp", "threading", "sleep", "Thread(", "log_audit",
    ])
    def test_no_io_or_orchestration(self, forbidden):
        assert forbidden not in self._src(), forbidden

    def test_no_row_locking_introduced(self):
        """Horizontal scaling is out of scope by approved policy.

        Checks for actual API usage rather than the words, which legitimately
        appear in the comment explaining why locking is absent.
        """
        src = self._src()
        for forbidden in ("with_for_update(", "skip_locked=", "pg_advisory",
                          'text("SELECT', "FOR UPDATE\""):
            assert forbidden not in src, forbidden

    def test_stays_model_injectable(self):
        for line in self._src().splitlines():
            assert not line.startswith("from app.models"), line


class TestRecipientStatusDriftGuard:
    """Status literals are mirrored, not imported — guard against renames."""

    @pytest.mark.parametrize("const,value", [
        ("RECIPIENT_QUEUED", QUEUED), ("RECIPIENT_SENDING", SENDING),
        ("RECIPIENT_SENT", SENT), ("RECIPIENT_FAILED", FAILED),
    ])
    def test_matches_model_constant(self, const, value):
        import re
        src = open(os.path.join(_ROOT, "app/models.py"), encoding="utf-8").read()
        assert re.search(rf'{const}\s*=\s*"{value}"', src), f"{const} drift"
