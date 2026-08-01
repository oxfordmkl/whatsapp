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
    impersonated_by = Column(String(120))
    audience_segment = Column(String(100))
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


def _default_audience_resolve(tenant_id, segment):
    """ADR-025 D3 test double: one reachable recipient, any segment name."""
    return [{"phone": "9990000000", "name": "Test Lead"}]


def _default_audience_preview(tenant_id, segment):
    """ADR-025 D6.1 test double: fully reachable — no acknowledgement needed."""
    return {"segment": segment, "total_audience": 1, "opted_out_excluded": 0,
            "reachable_now": 1, "template_required": 0}


@pytest.fixture
def svc(session, monkeypatch):
    """Service + repository on one session, with the engine flag ON.

    Wired with a default one-recipient, fully-reachable audience so lifecycle
    tests that call mark_running() and don't care about audience specifics
    (TestLifecycleCommands etc.) keep testing lifecycle behaviour, not
    audience resolution — that is tested directly in test_audience_resolver.py
    and test_campaign_service.py's own audience-focused test classes.
    """
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
    return svc_mod.CampaignService(
        repository=repo, session=session,
        audience_resolve_fn=_default_audience_resolve,
        audience_preview_fn=_default_audience_preview,
    )


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
        svc.mark_running(T1, c.id, audience_segment="All Leads")
        assert c.status == S.RUNNING and c.started_at is not None
        svc.mark_completed(T1, c.id)
        assert c.status == S.COMPLETED and c.completed_at is not None

    def test_scheduled_path(self, svc):
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        when = datetime.utcnow() + timedelta(hours=2)
        svc.schedule(T1, c.id, when)
        assert c.status == S.SCHEDULED and c.scheduled_at == when
        svc.mark_running(T1, c.id, audience_segment="All Leads")
        assert c.status == S.RUNNING

    def test_failed_records_reason(self, svc):
        c = _draft(svc); svc.mark_validated(T1, c.id)
        svc.mark_running(T1, c.id, audience_segment="All Leads")
        svc.mark_failed(T1, c.id, "no WABA credentials")
        assert c.status == S.FAILED
        assert c.failure_reason == "no WABA credentials"
        assert c.completed_at is not None

    def test_cancel_and_archive(self, svc):
        c = _draft(svc); svc.mark_validated(T1, c.id)
        svc.mark_running(T1, c.id, audience_segment="All Leads")
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
        c = _draft(svc); svc.mark_validated(T1, c.id)
        svc.mark_running(T1, c.id, audience_segment="All Leads")
        svc.mark_completed(T1, c.id); svc.archive(T1, c.id)
        for fn in (svc.mark_running, svc.mark_completed, svc.cancel):
            with pytest.raises(svc_mod.CampaignTransitionError):
                fn(T1, c.id)

    def test_cancel_stops_queued_recipients_from_being_claimed(self, svc):
        """ADR-025 D9 / P7 regression: cancel() must actually stop sending,
        not just flip the campaign's own status."""
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        svc.mark_running(T1, c.id, audience_segment="All Leads")
        assert svc.repository.count_recipients(T1, c.id) == 1  # default stub recipient

        svc.cancel(T1, c.id)

        claimed = svc.repository.claim_next_batch(T1, campaign_id=c.id, limit=50)
        assert claimed == [], "a cancelled campaign's recipients must not be claimable"

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


class _MessageTemplate(_Base):
    __tablename__ = "message_templates"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(String(36), nullable=False)
    status = Column(String(20), nullable=False, default="draft")
    provider_template_id = Column(String(200))
    channel = Column(String(20), nullable=False, default="whatsapp")
    language = Column(String(10), nullable=False, default="en")
    variables = Column(String(500), default="[]")


def _insert_template(session, **kw):
    kw.setdefault("tenant_id", T1)
    kw.setdefault("status", "approved")
    kw.setdefault("provider_template_id", "meta_tpl_1")
    kw.setdefault("channel", "whatsapp")
    kw.setdefault("language", "en")
    kw.setdefault("variables", "[]")
    row = _MessageTemplate(**kw)
    session.add(row)
    session.commit()
    return row


class TestLaunchTemplateGate:
    """ADR-024 D3: mark_running() must refuse an unusable template_id before
    any repository mutation — a launch-time gate, not a per-recipient one."""

    def _svc_with_templates(self, session, monkeypatch):
        """Same collaborators as `svc`, but with template_model wired to the
        local _MessageTemplate mapping so resolve_campaign_template runs
        against real SQLAlchemy filtering."""
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
        return svc_mod.CampaignService(
            repository=repo, session=session, template_model=_MessageTemplate,
            audience_resolve_fn=_default_audience_resolve,
            audience_preview_fn=_default_audience_preview,
        )

    def test_launch_refused_when_template_not_approved(self, session, monkeypatch):
        svc2 = self._svc_with_templates(session, monkeypatch)
        template = _insert_template(session, status="draft")
        c = _draft(svc2, message_body=None, template_id=template.id)
        svc2.mark_validated(T1, c.id)

        with pytest.raises(svc_mod.CampaignValidationError):
            svc2.mark_running(T1, c.id)
        assert svc_mod.CampaignService(
            repository=svc2.repository, session=session
        ).get_campaign(T1, c.id).status == S.VALIDATED

    def test_launch_refused_when_template_wrong_tenant(self, session, monkeypatch):
        svc2 = self._svc_with_templates(session, monkeypatch)
        template = _insert_template(session, tenant_id=T2)
        c = _draft(svc2, message_body=None, template_id=template.id)
        svc2.mark_validated(T1, c.id)

        with pytest.raises(svc_mod.CampaignValidationError):
            svc2.mark_running(T1, c.id)

    def test_launch_refused_when_template_id_nonexistent(self, session, monkeypatch):
        svc2 = self._svc_with_templates(session, monkeypatch)
        c = _draft(svc2, message_body=None, template_id=999999)
        svc2.mark_validated(T1, c.id)

        with pytest.raises(svc_mod.CampaignValidationError):
            svc2.mark_running(T1, c.id)

    def test_launch_succeeds_when_template_approved(self, session, monkeypatch):
        svc2 = self._svc_with_templates(session, monkeypatch)
        template = _insert_template(session)
        c = _draft(svc2, message_body=None, template_id=template.id)
        svc2.mark_validated(T1, c.id)

        svc2.mark_running(T1, c.id, audience_segment="All Leads")
        assert c.status == S.RUNNING

    def test_message_body_only_campaign_skips_template_gate_entirely(self, session, monkeypatch):
        """No template_id set — the D3 gate must not even query message_templates."""
        svc2 = self._svc_with_templates(session, monkeypatch)
        c = _draft(svc2)  # message_body-only, no template_id
        svc2.mark_validated(T1, c.id)

        svc2.mark_running(T1, c.id, audience_segment="All Leads")
        assert c.status == S.RUNNING

    def test_gate_does_not_fire_for_non_running_transitions(self, session, monkeypatch):
        """An unresolvable template must not block validate/schedule/cancel —
        only the transition INTO running is gated."""
        svc2 = self._svc_with_templates(session, monkeypatch)
        template = _insert_template(session, status="rejected")
        c = _draft(svc2, message_body=None, template_id=template.id)

        svc2.mark_validated(T1, c.id)  # draft -> validated must not raise
        assert c.status == S.VALIDATED


class TestDescribeCampaignTemplate:
    """ADR-025 D7: per-condition readiness for the audience preview.

    Unlike resolve_campaign_template() (D3 — collapses everything to None),
    every failure mode here must be independently observable so a preview can
    tell an operator WHICH condition is unmet.
    """

    def test_no_template_id_configured(self, session):
        out = svc_mod.describe_campaign_template(T1, None, session=session, model=_MessageTemplate)
        assert out == {
            "configured": False, "found": False, "tenant_match": None,
            "status": None, "provider_template_id_present": None,
            "channel": None, "ready": False,
        }

    def test_template_id_set_but_row_missing(self, session):
        out = svc_mod.describe_campaign_template(T1, 999999, session=session, model=_MessageTemplate)
        assert out["configured"] is True
        assert out["found"] is False
        assert out["tenant_match"] is None
        assert out["ready"] is False

    def test_wrong_tenant_reported_not_hidden(self, session):
        template = _insert_template(session, tenant_id=T2)
        out = svc_mod.describe_campaign_template(T1, template.id, session=session, model=_MessageTemplate)
        assert out["found"] is True
        assert out["tenant_match"] is False
        assert out["ready"] is False
        # The actual status is still reported — this is diagnostic, not a gate.
        assert out["status"] == "approved"

    def test_not_approved_reports_actual_status(self, session):
        for status in ("draft", "approval_pending", "rejected", "archived"):
            template = _insert_template(session, status=status)
            out = svc_mod.describe_campaign_template(T1, template.id, session=session, model=_MessageTemplate)
            assert out["status"] == status
            assert out["ready"] is False

    def test_missing_provider_template_id_reported(self, session):
        template = _insert_template(session, provider_template_id=None)
        out = svc_mod.describe_campaign_template(T1, template.id, session=session, model=_MessageTemplate)
        assert out["provider_template_id_present"] is False
        assert out["ready"] is False

    def test_wrong_channel_reported(self, session):
        template = _insert_template(session, channel="sms")
        out = svc_mod.describe_campaign_template(T1, template.id, session=session, model=_MessageTemplate)
        assert out["channel"] == "sms"
        assert out["ready"] is False

    def test_fully_ready_template(self, session):
        template = _insert_template(session)  # all defaults = approved/whatsapp/tenant T1
        out = svc_mod.describe_campaign_template(T1, template.id, session=session, model=_MessageTemplate)
        assert out == {
            "configured": True, "found": True, "tenant_match": True,
            "status": "approved", "provider_template_id_present": True,
            "channel": "whatsapp", "ready": True,
        }

    def test_readiness_matches_d3_resolution(self, session):
        """ready must exactly track resolve_campaign_template()'s condition
        set — this is the property that makes the diagnostic trustworthy."""
        cases = [
            _insert_template(session),
            _insert_template(session, status="draft"),
            _insert_template(session, tenant_id=T2),
            _insert_template(session, provider_template_id=None),
            _insert_template(session, channel="sms"),
        ]
        for template in cases:
            described = svc_mod.describe_campaign_template(
                T1, template.id, session=session, model=_MessageTemplate
            )
            resolved = svc_mod.resolve_campaign_template(
                T1, template.id, session=session, model=_MessageTemplate
            )
            assert described["ready"] == (resolved is not None), (
                f"drift for template {template.id} (status={template.status})"
            )


class TestMarkRunningAudience:
    """ADR-025 8.2E.9-C: mark_running()'s own audience mechanics — D2, D5,
    D6.2, D8, and the D2-before-D6.2 ordering (Phase 8.2E.9-D) — using a
    fully controllable injected resolver/preview, not the fixture default."""

    def _svc(self, session, monkeypatch, resolve_fn, preview_fn):
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
        return svc_mod.CampaignService(
            repository=repo, session=session,
            audience_resolve_fn=resolve_fn, audience_preview_fn=preview_fn,
        )

    def test_no_audience_segment_set_refuses(self, session, monkeypatch):
        svc = self._svc(session, monkeypatch,
                        lambda t, s: [{"phone": "p1", "name": "A"}],
                        lambda t, s: {"template_required": 0})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        with pytest.raises(svc_mod.CampaignValidationError):
            svc.mark_running(T1, c.id)  # no audience_segment given or set
        assert c.status == S.VALIDATED

    def test_audience_segment_persisted_on_campaign(self, session, monkeypatch):
        """D8: the segment is WRITTEN to the campaign, not just used ephemerally."""
        svc = self._svc(session, monkeypatch,
                        lambda t, s: [{"phone": "p1", "name": "A"}],
                        lambda t, s: {"template_required": 0})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        svc.mark_running(T1, c.id, audience_segment="HOT Leads")
        assert c.audience_segment == "HOT Leads"

    def test_previously_set_segment_used_when_omitted(self, session, monkeypatch):
        seen = []
        svc = self._svc(session, monkeypatch,
                        lambda t, s: seen.append(s) or [{"phone": "p1", "name": "A"}],
                        lambda t, s: {"template_required": 0})
        c = _draft(svc, audience_segment="WARM Leads")
        svc.mark_validated(T1, c.id)
        svc.mark_running(T1, c.id)  # no override
        assert seen == ["WARM Leads"]

    def test_zero_recipients_refuses_and_preserves_state(self, session, monkeypatch):
        """D2."""
        svc = self._svc(session, monkeypatch, lambda t, s: [], lambda t, s: {})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        with pytest.raises(svc_mod.CampaignValidationError):
            svc.mark_running(T1, c.id, audience_segment="All Leads")
        assert c.status == S.VALIDATED
        assert svc.repository.count_recipients(T1, c.id) == 0

    def test_zero_recipients_writes_no_recipient_rows(self, session, monkeypatch):
        svc = self._svc(session, monkeypatch, lambda t, s: [], lambda t, s: {})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        with pytest.raises(svc_mod.CampaignValidationError):
            svc.mark_running(T1, c.id, audience_segment="All Leads")
        assert svc.repository.list_recipients(T1, c.id) == []

    def test_exceeds_max_recipients_refuses(self, session, monkeypatch):
        """D5: enforced at materialisation, not only create_campaign()."""
        big = [{"phone": f"p{i}", "name": "A"} for i in range(svc_mod.MAX_RECIPIENTS + 1)]
        svc = self._svc(session, monkeypatch, lambda t, s: big,
                        lambda t, s: {"template_required": 0})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        with pytest.raises(svc_mod.CampaignValidationError):
            svc.mark_running(T1, c.id, audience_segment="All Leads")
        assert c.status == S.VALIDATED
        assert svc.repository.count_recipients(T1, c.id) == 0

    def test_at_max_recipients_succeeds(self, session, monkeypatch):
        at_max = [{"phone": f"p{i}", "name": "A"} for i in range(svc_mod.MAX_RECIPIENTS)]
        svc = self._svc(session, monkeypatch, lambda t, s: at_max,
                        lambda t, s: {"template_required": 0})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        svc.mark_running(T1, c.id, audience_segment="All Leads")
        assert c.status == S.RUNNING
        assert svc.repository.count_recipients(T1, c.id) == svc_mod.MAX_RECIPIENTS

    def test_acknowledgement_required_when_template_needed(self, session, monkeypatch):
        """D6.2: refused when recipients need a template and it wasn't acknowledged."""
        svc = self._svc(session, monkeypatch,
                        lambda t, s: [{"phone": "p1", "name": "A"}],
                        lambda t, s: {"template_required": 1})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        with pytest.raises(svc_mod.CampaignValidationError) as e:
            svc.mark_running(T1, c.id, audience_segment="All Leads")
        assert "D6.2" in str(e.value)
        assert c.status == S.VALIDATED
        assert svc.repository.count_recipients(T1, c.id) == 0

    def test_acknowledgement_accepted_launches(self, session, monkeypatch):
        svc = self._svc(session, monkeypatch,
                        lambda t, s: [{"phone": "p1", "name": "A"}],
                        lambda t, s: {"template_required": 1})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        svc.mark_running(T1, c.id, audience_segment="All Leads", acknowledged=True)
        assert c.status == S.RUNNING

    def test_no_acknowledgement_needed_when_fully_reachable(self, session, monkeypatch):
        svc = self._svc(session, monkeypatch,
                        lambda t, s: [{"phone": "p1", "name": "A"}],
                        lambda t, s: {"template_required": 0})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        svc.mark_running(T1, c.id, audience_segment="All Leads")  # acknowledged defaults False
        assert c.status == S.RUNNING

    def test_zero_recipient_refusal_precedes_acknowledgement_check(self, session, monkeypatch):
        """Phase 8.2E.9-D ordering: D2 before D6.2 — an impossible launch is
        refused outright, never offered for acknowledgement. If ordering were
        reversed, this would raise the D6.2 message instead (preview_fn here
        would never even be consulted for template_required, since there are
        no recipients to need one) — asserting on the D2 wording proves which
        branch actually fired."""
        svc = self._svc(session, monkeypatch, lambda t, s: [],
                        lambda t, s: {"template_required": 5})  # would also need ack
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        with pytest.raises(svc_mod.CampaignValidationError) as e:
            svc.mark_running(T1, c.id, audience_segment="All Leads")
        assert "D2" in str(e.value)
        assert "D6.2" not in str(e.value)

    def test_recipient_name_and_phone_snapshotted(self, session, monkeypatch):
        """D5: name/phone snapshot from the resolver reaches CampaignRecipient."""
        svc = self._svc(session, monkeypatch,
                        lambda t, s: [{"phone": "+919000000099", "name": "Priya"}],
                        lambda t, s: {"template_required": 0})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        svc.mark_running(T1, c.id, audience_segment="All Leads")
        row = svc.repository.list_recipients(T1, c.id)[0]
        assert (row.phone, row.name) == ("+919000000099", "Priya")

    def test_total_recipients_counter_set(self, session, monkeypatch):
        svc = self._svc(session, monkeypatch,
                        lambda t, s: [{"phone": "p1", "name": "A"},
                                     {"phone": "p2", "name": "B"}],
                        lambda t, s: {"template_required": 0})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        svc.mark_running(T1, c.id, audience_segment="All Leads")
        assert c.total_recipients == 2

    def test_atomicity_failed_launch_leaves_no_recipients(self, session, monkeypatch):
        """A launch that fails after resolving recipients (MAX_RECIPIENTS)
        must not leave a partial write — D5's single-transaction guarantee."""
        too_many = [{"phone": f"p{i}", "name": "A"} for i in range(svc_mod.MAX_RECIPIENTS + 5)]
        svc = self._svc(session, monkeypatch, lambda t, s: too_many,
                        lambda t, s: {"template_required": 0})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        with pytest.raises(svc_mod.CampaignValidationError):
            svc.mark_running(T1, c.id, audience_segment="All Leads")
        assert svc.repository.count_recipients(T1, c.id) == 0
        assert svc.repository.get(T1, c.id).status == S.VALIDATED


class TestLaunchRefusalLeavesNothingBehind:
    """Phase 8.2E.9-E B1: a refused launch must write NOTHING.

    These tests deliberately COMMIT after the refusal instead of rolling
    back. A rollback would hide the defect being guarded against: the
    original implementation mutated campaign.audience_segment on the
    persistent object before the transactional block, so the value survived
    in the session's identity map and any later commit — anywhere on the same
    session, e.g. the worker's long-lived app_context — persisted an audience
    choice for a launch that was refused. Committing here is what makes these
    tests fail if that mutation moves back outside the try block.
    """

    def _svc(self, session, monkeypatch, resolve_fn, preview_fn):
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
        return svc_mod.CampaignService(
            repository=repo, session=session,
            audience_resolve_fn=resolve_fn, audience_preview_fn=preview_fn,
        )

    def _assert_untouched(self, svc, session, campaign_id, expect_segment):
        """Commit, expire, re-read from the DB, assert nothing was written."""
        session.commit()          # NOT rollback — see class docstring
        session.expire_all()
        row = svc.repository.get(T1, campaign_id)
        assert row.status == S.VALIDATED, "status must be unchanged"
        assert row.audience_segment == expect_segment, (
            "audience_segment must be unchanged by a refused launch"
        )
        assert row.total_recipients == 0, "total_recipients must be unchanged"
        assert svc.repository.count_recipients(T1, campaign_id) == 0, (
            "no CampaignRecipient rows may be created by a refused launch"
        )

    def test_zero_recipient_refusal_writes_nothing(self, session, monkeypatch):
        svc = self._svc(session, monkeypatch, lambda t, s: [], lambda t, s: {})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        with pytest.raises(svc_mod.CampaignValidationError):
            svc.mark_running(T1, c.id, audience_segment="HOT Leads")
        self._assert_untouched(svc, session, c.id, expect_segment=None)

    def test_max_recipients_refusal_writes_nothing(self, session, monkeypatch):
        too_many = [{"phone": f"p{i}", "name": "A"}
                    for i in range(svc_mod.MAX_RECIPIENTS + 1)]
        svc = self._svc(session, monkeypatch, lambda t, s: too_many,
                        lambda t, s: {"template_required": 0})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        with pytest.raises(svc_mod.CampaignValidationError):
            svc.mark_running(T1, c.id, audience_segment="WARM Leads")
        self._assert_untouched(svc, session, c.id, expect_segment=None)

    def test_acknowledgement_refusal_writes_nothing(self, session, monkeypatch):
        svc = self._svc(session, monkeypatch,
                        lambda t, s: [{"phone": "p1", "name": "A"}],
                        lambda t, s: {"template_required": 3})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        with pytest.raises(svc_mod.CampaignValidationError):
            svc.mark_running(T1, c.id, audience_segment="Critical Leads")
        self._assert_untouched(svc, session, c.id, expect_segment=None)

    def test_missing_segment_refusal_writes_nothing(self, session, monkeypatch):
        svc = self._svc(session, monkeypatch,
                        lambda t, s: [{"phone": "p1", "name": "A"}],
                        lambda t, s: {"template_required": 0})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        with pytest.raises(svc_mod.CampaignValidationError):
            svc.mark_running(T1, c.id)  # no segment given, none set
        self._assert_untouched(svc, session, c.id, expect_segment=None)

    def test_refusal_does_not_overwrite_a_previously_set_segment(self, session, monkeypatch):
        """The strongest form: a campaign that ALREADY has a segment must not
        have it replaced by the one supplied to a refused launch."""
        svc = self._svc(session, monkeypatch, lambda t, s: [], lambda t, s: {})
        c = _draft(svc, audience_segment="WARM Leads")
        svc.mark_validated(T1, c.id)
        with pytest.raises(svc_mod.CampaignValidationError):
            svc.mark_running(T1, c.id, audience_segment="HOT Leads")
        self._assert_untouched(svc, session, c.id, expect_segment="WARM Leads")

    def test_successful_launch_does_persist_the_segment(self, session, monkeypatch):
        """Control case — proves the above tests aren't passing because the
        assignment was simply dropped."""
        svc = self._svc(session, monkeypatch,
                        lambda t, s: [{"phone": "p1", "name": "A"}],
                        lambda t, s: {"template_required": 0})
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        svc.mark_running(T1, c.id, audience_segment="HOT Leads")
        session.expire_all()
        row = svc.repository.get(T1, c.id)
        assert row.status == S.RUNNING
        assert row.audience_segment == "HOT Leads"
        assert row.total_recipients == 1


class TestCancelCounterParity:
    """Phase 8.2E.9-E B2: D9's cancellation must leave D10's counters correct."""

    def _svc(self, session, monkeypatch, n_recipients=5):
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
        return svc_mod.CampaignService(
            repository=repo, session=session,
            audience_resolve_fn=lambda t, s: [
                {"phone": f"p{i}", "name": "A"} for i in range(n_recipients)
            ],
            audience_preview_fn=lambda t, s: {"template_required": 0},
        )

    def _launched(self, svc):
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        svc.mark_running(T1, c.id, audience_segment="All Leads")
        return c

    def test_counters_match_breakdown_after_cancel(self, session, monkeypatch):
        svc = self._svc(session, monkeypatch, n_recipients=5)
        c = self._launched(svc)
        svc.cancel(T1, c.id)
        session.expire_all()

        row = svc.repository.get(T1, c.id)
        bd = svc.repository.status_breakdown(T1, c.id)
        assert bd == {"cancelled": 5}
        assert row.failed_count == 5, (
            "cancelled recipients are terminal-unsuccessful and must be counted"
        )
        assert row.sent_count == 0
        assert row.total_recipients == 5

    def test_counter_source_is_the_recipient_breakdown(self, session, monkeypatch):
        """Parity is asserted against status_breakdown(), not a hard-coded
        number — the campaign columns must never be an independent tally."""
        svc = self._svc(session, monkeypatch, n_recipients=3)
        c = self._launched(svc)
        svc.cancel(T1, c.id)
        session.expire_all()

        row = svc.repository.get(T1, c.id)
        bd = svc.repository.status_breakdown(T1, c.id)
        derived_failed = bd.get("failed", 0) + bd.get("cancelled", 0)
        derived_sent = bd.get("sent", 0) + bd.get("delivered", 0) + bd.get("read", 0)
        assert row.failed_count == derived_failed
        assert row.sent_count == derived_sent

    def test_already_sent_recipients_not_cancelled_and_still_counted_as_sent(
            self, session, monkeypatch):
        """Cancellation must not rewrite terminal history: a recipient already
        `sent` stays sent, and keeps counting toward sent_count."""
        svc = self._svc(session, monkeypatch, n_recipients=3)
        c = self._launched(svc)
        rows = svc.repository.list_recipients(T1, c.id)
        svc.repository.mark_recipient_sent(T1, rows[0].id, wa_message_id="wamid.1")
        session.commit()

        svc.cancel(T1, c.id)
        session.expire_all()

        bd = svc.repository.status_breakdown(T1, c.id)
        assert bd == {"sent": 1, "cancelled": 2}
        row = svc.repository.get(T1, c.id)
        assert row.sent_count == 1, "a completed send must not be un-counted"
        assert row.failed_count == 2

    def test_in_flight_sending_recipient_not_cancelled(self, session, monkeypatch):
        """R7: a claimed (`sending`) row has an unknown outcome and is left
        alone — so it counts as neither sent nor failed at cancel time."""
        svc = self._svc(session, monkeypatch, n_recipients=3)
        c = self._launched(svc)
        svc.repository.claim_next_batch(T1, campaign_id=c.id, limit=1)
        session.commit()

        svc.cancel(T1, c.id)
        session.expire_all()

        bd = svc.repository.status_breakdown(T1, c.id)
        assert bd == {"sending": 1, "cancelled": 2}
        row = svc.repository.get(T1, c.id)
        assert row.failed_count == 2
        assert row.sent_count == 0

    def test_cancel_is_only_legal_from_running(self, session, monkeypatch):
        """Documents why "cancel a campaign with no recipients" is unreachable:
        ALLOWED_TRANSITIONS permits CANCELLED only from RUNNING, and D2
        guarantees a RUNNING campaign has at least one recipient. So the
        counter-sync in the CANCELLED branch always has rows to count.

        NOTE: app/routes/marketing.py's cancel route docstring claims
        "running/validated/scheduled -> cancelled", which overstates this.
        Reported as a finding rather than edited — out of B1/B2 scope.
        """
        svc = self._svc(session, monkeypatch)
        c = _draft(svc)
        svc.mark_validated(T1, c.id)
        with pytest.raises(svc_mod.CampaignTransitionError):
            svc.cancel(T1, c.id)
        assert svc_mod.ALLOWED_TRANSITIONS[S.RUNNING] >= {S.CANCELLED}
        assert S.CANCELLED not in svc_mod.ALLOWED_TRANSITIONS[S.VALIDATED]
        assert S.CANCELLED not in svc_mod.ALLOWED_TRANSITIONS[S.SCHEDULED]

    def test_counters_committed_not_merely_flushed(self, session, monkeypatch):
        """B2's update must be inside cancel()'s transaction, not left pending
        for a later commit that may never come."""
        svc = self._svc(session, monkeypatch, n_recipients=4)
        c = self._launched(svc)
        svc.cancel(T1, c.id)
        session.rollback()            # discard anything uncommitted
        session.expire_all()
        assert svc.repository.get(T1, c.id).failed_count == 4


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
