"""
Phase 8.2B — CampaignService tests (lifecycle, validation, transactions).

Runs the REAL CampaignService against the REAL CampaignRepository on an
in-memory SQLite database, so lifecycle enforcement, transaction ownership and
the repository's no-commit contract are all genuinely exercised rather than
mocked. Only the models are re-declared locally (app.models needs the whole app
package) — both service and repository are model-injectable by design.

Central guarantees under test:
  * every illegal transition is rejected and leaves the row untouched
  * CampaignService owns commit/rollback; the repository never commits
  * a failed create rolls back completely — no orphan campaign, no partial audience
  * mutating operations refuse to run while CAMPAIGN_ENGINE_V2 is OFF
  * tenant_id is mandatory and enforced (ADR-021)
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
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(unique_name, relpath, register_as=None, monkeypatch=None):
    path = os.path.join(_ROOT, relpath)
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    if register_as and monkeypatch is not None:
        monkeypatch.setitem(sys.modules, register_as, mod)
    spec.loader.exec_module(mod)
    return mod


repo_mod = _load("_p82b_repo", "app/persistence/campaign_repository.py")
svc_mod = _load("_p82b_svc", "app/marketing/campaign_service.py")

S = svc_mod  # status constants live on the module


# ── Local mapping mirroring the production campaign schema ──────────────────
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
PHONES = ["+919000000001", "+919000000002"]


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
def svc(session, monkeypatch):
    """Service + repository on one session, with the engine flag ON."""
    monkeypatch.setenv("CAMPAIGN_ENGINE_V2", "true")
    flags = types.ModuleType("app.flags")
    flags.campaign_engine_v2_enabled = lambda: (
        os.environ.get("CAMPAIGN_ENGINE_V2", "false").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    monkeypatch.setitem(sys.modules, "app.flags", flags)

    repo = repo_mod.CampaignRepository(
        session=session, campaign_model=_Campaign, recipient_model=_CampaignRecipient
    )
    return svc_mod.CampaignService(repository=repo, session=session)


def _draft(svc, tenant=T1, **kw):
    kw.setdefault("message_body", "hello")
    return svc.create_campaign(tenant, kw.pop("name", "C"), **kw)


# ── Validation ───────────────────────────────────────────────────────────────
class TestValidateCampaign:
    def test_valid(self, svc):
        assert svc.validate_campaign(name="X", message_body="hi").ok

    @pytest.mark.parametrize("name", [None, "", "   "])
    def test_name_required(self, svc, name):
        r = svc.validate_campaign(name=name, message_body="hi")
        assert not r.ok and "name is required" in r.errors

    def test_name_length(self, svc):
        r = svc.validate_campaign(name="x" * 201, message_body="hi")
        assert not r.ok

    def test_content_required(self, svc):
        r = svc.validate_campaign(name="X")
        assert not r.ok
        assert "either message_body or template_id is required" in r.errors

    def test_content_mutually_exclusive(self, svc):
        r = svc.validate_campaign(name="X", message_body="hi", template_id=1)
        assert not r.ok
        assert "provide message_body or template_id, not both" in r.errors

    def test_template_alone_is_valid(self, svc):
        assert svc.validate_campaign(name="X", template_id=1).ok

    def test_long_description_warns_but_passes(self, svc):
        r = svc.validate_campaign(name="X", message_body="hi",
                                  description="d" * 5001)
        assert r.ok and r.warnings


class TestValidateRecipients:
    def test_valid(self, svc):
        assert svc.validate_recipients(PHONES).ok

    def test_accepts_dicts(self, svc):
        assert svc.validate_recipients([{"phone": PHONES[0], "name": "A"}]).ok

    def test_empty_rejected(self, svc):
        r = svc.validate_recipients([])
        assert not r.ok and "at least one recipient is required" in r.errors

    def test_blank_phone_rejected(self, svc):
        assert not svc.validate_recipients(["", None]).ok

    def test_implausible_phone_rejected(self, svc):
        assert not svc.validate_recipients(["123"]).ok

    def test_duplicates_rejected(self, svc):
        r = svc.validate_recipients([PHONES[0], PHONES[0]])
        assert not r.ok and any("duplicate" in e for e in r.errors)

    def test_cap_enforced(self, svc):
        many = [f"+9190000{i:05d}" for i in range(S.MAX_RECIPIENTS + 1)]
        r = svc.validate_recipients(many)
        assert not r.ok and any("exceeds the maximum" in e for e in r.errors)

    def test_cap_boundary_ok(self, svc):
        many = [f"+9190000{i:05d}" for i in range(S.MAX_RECIPIENTS)]
        assert svc.validate_recipients(many).ok

    def test_matches_legacy_cap(self):
        """Never more permissive than the engine it replaces."""
        legacy = open(os.path.join(_ROOT, "app/services/campaign_service.py"),
                      encoding="utf-8").read()
        assert "> 100" in legacy and S.MAX_RECIPIENTS == 100


class TestValidateSchedule:
    def test_future_ok(self, svc):
        assert svc.validate_schedule(datetime.utcnow() + timedelta(hours=1)).ok

    def test_required(self, svc):
        assert not svc.validate_schedule(None).ok

    def test_past_rejected(self, svc):
        assert not svc.validate_schedule(datetime.utcnow() - timedelta(hours=1)).ok

    def test_type_checked(self, svc):
        assert not svc.validate_schedule("tomorrow").ok


class TestValidationResultShape:
    def test_structured(self, svc):
        r = svc.validate_campaign(name=None)
        d = r.as_dict()
        assert set(d) == {"ok", "errors", "warnings"}
        assert d["ok"] is False and len(d["errors"]) >= 1

    def test_truthiness_and_merge(self, svc):
        good, bad = svc.validate_campaign(name="X", message_body="h"), \
                    svc.validate_campaign(name=None)
        assert bool(good) and not bool(bad)
        assert not good.merge(bad).ok


# ── Lifecycle rules (pure) ───────────────────────────────────────────────────
class TestLifecycleRules:
    @pytest.mark.parametrize("frm,to", [
        (S.DRAFT, S.VALIDATED),
        (S.VALIDATED, S.SCHEDULED), (S.VALIDATED, S.RUNNING),
        (S.SCHEDULED, S.RUNNING),
        (S.RUNNING, S.COMPLETED), (S.RUNNING, S.FAILED), (S.RUNNING, S.CANCELLED),
        (S.COMPLETED, S.ARCHIVED), (S.FAILED, S.ARCHIVED), (S.CANCELLED, S.ARCHIVED),
    ])
    def test_approved_transitions_allowed(self, frm, to):
        assert svc_mod.CampaignService.can_transition(frm, to)

    @pytest.mark.parametrize("frm,to", [
        (S.DRAFT, S.RUNNING), (S.DRAFT, S.SCHEDULED), (S.DRAFT, S.COMPLETED),
        (S.VALIDATED, S.COMPLETED), (S.SCHEDULED, S.COMPLETED),
        (S.COMPLETED, S.RUNNING), (S.FAILED, S.RUNNING),
        (S.CANCELLED, S.RUNNING), (S.ARCHIVED, S.DRAFT),
        (S.RUNNING, S.DRAFT), (S.RUNNING, S.VALIDATED),
    ])
    def test_illegal_transitions_rejected(self, frm, to):
        assert not svc_mod.CampaignService.can_transition(frm, to)

    def test_archived_is_terminal(self):
        assert svc_mod.CampaignService.is_terminal(S.ARCHIVED)
        assert svc_mod.CampaignService.allowed_next(S.ARCHIVED) == frozenset()

    def test_unknown_status_has_no_transitions(self):
        assert not svc_mod.CampaignService.can_transition("bogus", S.RUNNING)


# ── create_campaign ──────────────────────────────────────────────────────────
class TestCreateCampaign:
    def test_creates_draft(self, svc):
        c = _draft(svc, name="Diwali")
        assert c.id is not None and c.status == S.DRAFT
        assert c.tenant_id == T1

    def test_persists_recipients_and_counter(self, svc):
        c = svc.create_campaign(T1, "C", recipients=PHONES, message_body="hi")
        assert svc.repository.count_recipients(T1, c.id) == 2
        assert c.total_recipients == 2

    def test_recipients_optional(self, svc):
        c = _draft(svc)
        assert c.total_recipients == 0

    def test_commits(self, svc, session):
        c = _draft(svc)
        session.rollback()                    # service already committed
        assert svc.repository.get(T1, c.id) is not None

    def test_invalid_content_raises_and_persists_nothing(self, svc):
        with pytest.raises(svc_mod.CampaignValidationError):
            svc.create_campaign(T1, "C")      # no body, no template
        assert svc.repository.count_for_tenant(T1) == 0

    def test_invalid_recipients_raise_and_persist_nothing(self, svc):
        with pytest.raises(svc_mod.CampaignValidationError):
            svc.create_campaign(T1, "C", recipients=["bad"], message_body="hi")
        assert svc.repository.count_for_tenant(T1) == 0

    def test_error_carries_structured_result(self, svc):
        with pytest.raises(svc_mod.CampaignValidationError) as e:
            svc.create_campaign(T1, "")
        assert not e.value.result.ok and e.value.result.errors

    def test_rollback_leaves_no_partial_audience(self, session):
        """A failure after the campaign row must roll back the campaign too.

        CampaignRepository uses __slots__, so the failure is injected via a
        subclass rather than instance monkeypatching.
        """
        class _FailingRepo(repo_mod.CampaignRepository):
            def add_recipients(self, *a, **k):
                raise RuntimeError("db exploded")

        repo = _FailingRepo(session=session, campaign_model=_Campaign,
                            recipient_model=_CampaignRecipient)
        service = svc_mod.CampaignService(repository=repo, session=session)

        with pytest.raises(RuntimeError):
            service.create_campaign(T1, "C", recipients=PHONES,
                                    message_body="hi")
        assert repo.count_for_tenant(T1) == 0     # no orphan campaign


# ── Lifecycle commands (DB) ──────────────────────────────────────────────────
class TestLifecycleCommands:
    def test_happy_path_to_completed(self, svc):
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        assert c.status == S.VALIDATED
        svc.mark_running(T1, c.id)
        assert c.status == S.RUNNING and c.started_at is not None
        svc.mark_completed(T1, c.id)
        assert c.status == S.COMPLETED and c.completed_at is not None

    def test_scheduled_path(self, svc):
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        when = datetime.utcnow() + timedelta(hours=2)
        svc.schedule(T1, c.id, when)
        assert c.status == S.SCHEDULED and c.scheduled_at == when
        svc.mark_running(T1, c.id)
        assert c.status == S.RUNNING

    def test_failed_records_reason(self, svc):
        c = _draft(svc); svc.mark_validated(T1, c.id); svc.mark_running(T1, c.id)
        svc.mark_failed(T1, c.id, "no WABA credentials")
        assert c.status == S.FAILED
        assert c.failure_reason == "no WABA credentials"
        assert c.completed_at is not None

    def test_cancel_and_archive(self, svc):
        c = _draft(svc); svc.mark_validated(T1, c.id); svc.mark_running(T1, c.id)
        svc.cancel(T1, c.id)
        assert c.status == S.CANCELLED
        svc.archive(T1, c.id)
        assert c.status == S.ARCHIVED

    def test_illegal_transition_raises_and_preserves_state(self, svc):
        c = _draft(svc)
        with pytest.raises(svc_mod.CampaignTransitionError) as e:
            svc.mark_running(T1, c.id)        # draft -> running is illegal
        assert (e.value.from_status, e.value.to_status) == (S.DRAFT, S.RUNNING)
        assert svc.repository.get(T1, c.id).status == S.DRAFT

    def test_archived_is_immutable(self, svc):
        c = _draft(svc); svc.mark_validated(T1, c.id); svc.mark_running(T1, c.id)
        svc.mark_completed(T1, c.id); svc.archive(T1, c.id)
        for fn in (svc.mark_running, svc.mark_completed, svc.cancel):
            with pytest.raises(svc_mod.CampaignTransitionError):
                fn(T1, c.id)

    def test_schedule_rejects_past_time(self, svc):
        c = _draft(svc); svc.mark_validated(T1, c.id)
        with pytest.raises(svc_mod.CampaignValidationError):
            svc.schedule(T1, c.id, datetime.utcnow() - timedelta(hours=1))
        assert c.status == S.VALIDATED

    def test_schedule_from_draft_rejected(self, svc):
        c = _draft(svc)
        with pytest.raises(svc_mod.CampaignTransitionError):
            svc.schedule(T1, c.id, datetime.utcnow() + timedelta(hours=1))

    def test_missing_campaign_raises(self, svc):
        with pytest.raises(svc_mod.CampaignValidationError):
            svc.mark_validated(T1, 9999)

    def test_transition_commits(self, svc, session):
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        session.rollback()
        assert svc.repository.get(T1, c.id).status == S.VALIDATED

    def test_transition_rolls_back_on_repository_error(self, svc, session):
        """A repository error during a transition must leave status untouched."""
        c = _draft(svc)
        session.commit()

        class _FailingRepo(repo_mod.CampaignRepository):
            def update_status(self, *a, **k):
                raise RuntimeError("db exploded")

        repo = _FailingRepo(session=session, campaign_model=_Campaign,
                            recipient_model=_CampaignRecipient)
        service = svc_mod.CampaignService(repository=repo, session=session)

        with pytest.raises(RuntimeError):
            service.mark_validated(T1, c.id)
        assert repo.get(T1, c.id).status == S.DRAFT


# ── Tenant safety (ADR-021) ──────────────────────────────────────────────────
class TestTenantSafety:
    @pytest.mark.parametrize("tenant", [None, "", 0])
    def test_missing_tenant_refused(self, svc, tenant):
        with pytest.raises(svc_mod.CampaignValidationError):
            svc.create_campaign(tenant, "C", message_body="hi")

    def test_cross_tenant_transition_refused(self, svc):
        c = _draft(svc, tenant=T1)
        with pytest.raises(svc_mod.CampaignValidationError):
            svc.mark_validated(T2, c.id)      # not found for T2
        assert svc.repository.get(T1, c.id).status == S.DRAFT

    def test_reads_are_tenant_scoped(self, svc):
        c = _draft(svc, tenant=T1)
        assert svc.get_campaign(T1, c.id) is not None
        assert svc.get_campaign(T2, c.id) is None
        assert svc.list_campaigns(T2) == []


# ── Feature flag gating ──────────────────────────────────────────────────────
class TestFeatureFlagGating:
    def test_mutations_refused_when_off(self, svc, monkeypatch):
        monkeypatch.setenv("CAMPAIGN_ENGINE_V2", "false")
        with pytest.raises(svc_mod.CampaignEngineDisabled):
            svc.create_campaign(T1, "C", message_body="hi")

    def test_transitions_refused_when_off(self, svc, monkeypatch):
        c = _draft(svc)
        monkeypatch.setenv("CAMPAIGN_ENGINE_V2", "false")
        with pytest.raises(svc_mod.CampaignEngineDisabled):
            svc.mark_validated(T1, c.id)

    def test_validation_available_when_off(self, svc, monkeypatch):
        """Read-only helpers stay usable so they can be exercised safely."""
        monkeypatch.setenv("CAMPAIGN_ENGINE_V2", "false")
        assert svc.validate_campaign(name="X", message_body="h").ok
        assert svc_mod.CampaignService.can_transition(S.DRAFT, S.VALIDATED)

    def test_flag_read_dynamically(self, svc, monkeypatch):
        monkeypatch.setenv("CAMPAIGN_ENGINE_V2", "false")
        assert svc.engine_enabled() is False
        monkeypatch.setenv("CAMPAIGN_ENGINE_V2", "true")
        assert svc.engine_enabled() is True


# ── Scope + purity contract ──────────────────────────────────────────────────
class TestScopeContract:
    def _src(self):
        return open(os.path.join(_ROOT, "app/marketing/campaign_service.py"),
                    encoding="utf-8").read()

    @pytest.mark.parametrize("forbidden", [
        "send_text", "send_template", "send_automation", "whatsapp_service",
        "requests", "threading", "time.sleep", "Thread(",
    ])
    def test_never_sends_or_spawns(self, forbidden):
        assert forbidden not in self._src(), forbidden

    def test_does_not_touch_recipient_delivery_status(self):
        src = self._src()
        for forbidden in ("delivered_at", "read_at", "wa_message_id",
                          "retry_count", "last_attempt_at"):
            assert forbidden not in src, forbidden

    def test_audit_service_not_wired(self):
        """Hook points only — no real audit_service integration in 8.2B.

        Checks for actual wiring (import / call), not the word itself, which
        legitimately appears in the docstring explaining the deferral.
        """
        src = self._src()
        assert "from app.services.audit_service" not in src
        assert "import audit_service" not in src
        assert "log_audit(" not in src

    def test_audit_hook_points_exist(self):
        src = self._src()
        assert "_audit_campaign_created" in src
        assert "_audit_status_changed" in src

    def test_owns_transaction_boundary(self):
        src = self._src()
        assert "self.session.commit()" in src
        assert "self.session.rollback()" in src

    def test_repository_still_never_commits(self):
        repo_src = open(
            os.path.join(_ROOT, "app/persistence/campaign_repository.py"),
            encoding="utf-8").read()
        assert ".commit()" not in repo_src and ".rollback()" not in repo_src

    def test_legacy_engine_untouched(self):
        legacy = open(os.path.join(_ROOT, "app/services/campaign_service.py"),
                      encoding="utf-8").read()
        assert "def start_campaign" in legacy      # still the production entry
        assert "CampaignService" not in legacy     # no coupling introduced


class TestStatusConstantDriftGuard:
    """Status literals are mirrored, not imported — guard against renames."""

    def _models_src(self):
        return open(os.path.join(_ROOT, "app/models.py"), encoding="utf-8").read()

    @pytest.mark.parametrize("const,value", [
        ("CAMPAIGN_DRAFT", S.DRAFT), ("CAMPAIGN_VALIDATED", S.VALIDATED),
        ("CAMPAIGN_SCHEDULED", S.SCHEDULED), ("CAMPAIGN_RUNNING", S.RUNNING),
        ("CAMPAIGN_COMPLETED", S.COMPLETED), ("CAMPAIGN_CANCELLED", S.CANCELLED),
        ("CAMPAIGN_FAILED", S.FAILED), ("CAMPAIGN_ARCHIVED", S.ARCHIVED),
    ])
    def test_matches_model_constant(self, const, value):
        import re
        src = self._models_src()
        assert re.search(rf'{const}\s*=\s*"{value}"', src), f"{const} drift"
