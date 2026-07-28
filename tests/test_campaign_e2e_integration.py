"""
Phase 8.2E.9-E — end-to-end integration (ADR-025 enablement gate item 6).

Closes the gate requirement: "a materialised audience runs end-to-end through
ADR-024 dispatch to a terminal reconciled campaign state."

Unlike every other campaign test file, this one wires the REAL
CampaignService, REAL CampaignRepository and REAL campaign_worker functions
together on ONE in-memory SQLite session, so the seams between them are
actually exercised:

    mark_running()  -> materialises CampaignRecipient rows (ADR-025 D1/D5)
      -> _process_tenant() -> claims, dispatches (ADR-024 D1/D2/D4)
        -> recipient statuses updated
          -> _update_campaign_counters() (ADR-025 D10)
            -> reconcile_campaign() -> terminal campaign state (ADR-024)

Only the true externals are stubbed: the WhatsApp provider (send_text /
send_template), conversation-history logging, and time.sleep. Everything
between launch and reconciliation is production code.

The audience resolver is injected rather than run against the real
_calculate_audiences(), which lives in app/routes/admin.py and cannot be
imported in a test process (it pulls app.config, which raises without a
DATABASE_URL). Resolver behaviour itself is covered by
test_audience_resolver.py; what this file proves is the wiring.
"""
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text,
    UniqueConstraint, create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(alias, relpath):
    spec = importlib.util.spec_from_file_location(
        alias, os.path.join(_ROOT, relpath)
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub(name):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    return sys.modules[name]


for _n in [
    "app", "app.persistence", "app.persistence.campaign_repository",
    "app.extensions", "app.models", "app.services",
    "app.services.whatsapp_service", "app.services.log_service",
    "app.flags", "app.marketing", "app.marketing.campaign_service",
]:
    _stub(_n)

sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: True

repo_mod = _load("_e2e_repo", "app/persistence/campaign_repository.py")
svc_mod = _load("_e2e_svc", "app/marketing/campaign_service.py")
# The worker lazily imports CampaignService from this dotted path for
# reconciliation — point it at the same module object the test uses.
sys.modules["app.marketing.campaign_service"].CampaignService = svc_mod.CampaignService
sys.modules["app.marketing.campaign_service"].resolve_campaign_template = (
    svc_mod.resolve_campaign_template
)
wkr = _load("_e2e_wkr", "app/marketing/campaign_worker.py")


# ── Local schema mirroring production ────────────────────────────────────────

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
    audience_segment = Column(String(100))
    scheduled_at = Column(DateTime)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    total_recipients = Column(Integer, nullable=False, default=0)
    sent_count = Column(Integer, nullable=False, default=0)
    failed_count = Column(Integer, nullable=False, default=0)
    created_by = Column(String(120))
    impersonated_by = Column(String(120))
    failure_reason = Column(Text)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
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
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    campaign = relationship("_Campaign", back_populates="recipients")
    __table_args__ = (UniqueConstraint("campaign_id", "phone"),)


T1 = "tenant-one"


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


# ── Externals: provider, history logging, sleep, ConversationState ───────────

def _wa_response(status=200, wa_id="wamid.OK"):
    r = MagicMock()
    r.status_code = status
    r.text = "OK" if status == 200 else "provider error"
    r.json.return_value = {"messages": [{"id": wa_id}]}
    return r


@pytest.fixture(autouse=True)
def _externals():
    """Stub only genuine externals; everything else is production code."""
    wa = sys.modules["app.services.whatsapp_service"]
    wa.send_text = MagicMock(side_effect=lambda *a, **k: _wa_response())
    wa.send_template = MagicMock(side_effect=lambda *a, **k: _wa_response())
    sys.modules["app.services.log_service"].save_conversation_message = MagicMock()
    with patch.object(wkr.time, "sleep"):     # skip the 1.5s D6 rate limit
        yield wa


def _set_conversation_state(window_open_phones=(), opted_out_phones=()):
    """Drive the worker's opt-out check and _window_open() lookups.

    Both do ConversationState.query.filter_by(phone=..., tenant_id=...).first(),
    so a per-phone dispatch table reproduces production behaviour exactly.
    """
    now = datetime.utcnow()
    recent = now.isoformat()
    stale = (now - timedelta(hours=48)).isoformat()

    def _first_for(**kw):
        phone = kw.get("phone")
        row = MagicMock()
        row.is_opted_out = phone in opted_out_phones
        row.last_msg = recent if phone in window_open_phones else stale
        result = MagicMock()
        result.first.return_value = row
        return result

    cs = MagicMock()
    cs.query.filter_by.side_effect = _first_for
    sys.modules["app.models"].ConversationState = cs
    return cs


def _make_service(repo, session, recipients, template_required=0):
    return svc_mod.CampaignService(
        repository=repo, session=session,
        audience_resolve_fn=lambda t, seg: list(recipients),
        audience_preview_fn=lambda t, seg: {"template_required": template_required},
    )


def _launch(svc, segment="All Leads", **kw):
    c = svc.create_campaign(T1, "E2E Campaign", message_body="Hello from campaign")
    svc.mark_validated(T1, c.id)
    svc.mark_running(T1, c.id, audience_segment=segment, **kw)
    return c


def _run_worker_cycle(repo, session, now=None):
    wkr._process_tenant(repo, session, T1, now or datetime.utcnow())


def _drain(repo, session, campaign_id, max_cycles=6):
    """Run worker cycles, advancing the clock past each retry backoff, until
    every recipient is terminal. Returns the number of cycles used.

    Since Phase 9.1G a window-closed-with-no-template recipient is terminal on
    its FIRST cycle — it is classified FAILURE_PERMANENT and never enters the
    retry queue (ADR-024 R4). This helper is retained because a genuinely
    transient failure (provider 5xx) still consumes the retry budget across
    cycles, and callers should not have to care which kind they provoked.

    History: before 9.1G, _send_one() flattened every non-`sent` outcome into
    a bare exception, so _handle_failure() could not tell a permanent
    condition from a transient one and retried both. This helper's callers
    asserted `cycles > 1` to pin that defect; 9.1G corrects it and those
    assertions were inverted accordingly.
    """
    now = datetime.utcnow()
    non_terminal = {"queued", "sending"}
    for cycle in range(1, max_cycles + 1):
        _run_worker_cycle(repo, session, now=now)
        session.expire_all()
        if not (set(repo.status_breakdown(T1, campaign_id)) & non_terminal):
            return cycle
        now += timedelta(minutes=60)     # clear the largest backoff
    raise AssertionError(
        f"campaign {campaign_id} still non-terminal after {max_cycles} cycles: "
        f"{repo.status_breakdown(T1, campaign_id)}"
    )


# ── The gate-item-6 scenarios ────────────────────────────────────────────────

class TestEndToEndAllReachable:
    """Happy path: every recipient inside the 24h window -> all text sends."""

    def test_full_lifecycle_to_completed(self, repo, session):
        phones = [f"+9190000000{i}" for i in range(3)]
        _set_conversation_state(window_open_phones=phones)
        svc = _make_service(repo, session,
                            [{"phone": p, "name": f"Lead{i}"}
                             for i, p in enumerate(phones)])

        c = _launch(svc)

        # --- materialisation (ADR-025 D1/D5) ---
        assert c.status == "running"
        assert c.total_recipients == 3
        assert repo.status_breakdown(T1, c.id) == {"queued": 3}
        rows = repo.list_recipients(T1, c.id)
        assert {r.tenant_id for r in rows} == {T1}
        assert {r.name for r in rows} == {"Lead0", "Lead1", "Lead2"}

        # --- dispatch (ADR-024) ---
        _run_worker_cycle(repo, session)
        session.expire_all()

        assert repo.status_breakdown(T1, c.id) == {"sent": 3}
        assert sys.modules["app.services.whatsapp_service"].send_text.call_count == 3
        assert sys.modules["app.services.whatsapp_service"].send_template.call_count == 0

        # --- counters (ADR-025 D10) + reconciliation (ADR-024) ---
        final = repo.get(T1, c.id)
        assert final.sent_count == 3
        assert final.failed_count == 0
        assert final.status == "completed"
        assert final.completed_at is not None

    def test_wa_message_id_recorded_per_recipient(self, repo, session):
        phones = ["+919000000001"]
        _set_conversation_state(window_open_phones=phones)
        svc = _make_service(repo, session, [{"phone": phones[0], "name": "A"}])
        c = _launch(svc)
        _run_worker_cycle(repo, session)
        session.expire_all()
        assert repo.list_recipients(T1, c.id)[0].wa_message_id == "wamid.OK"


class TestEndToEndPartialSuccess:
    """ADR-025's documented expected outcome: a mixed audience with no
    approved template completes with real sends AND counted failures."""

    def test_reachable_sent_unreachable_failed_campaign_completed(self, repo, session):
        reachable = ["+919000000001"]
        unreachable = [f"+91900000010{i}" for i in range(3)]
        _set_conversation_state(window_open_phones=reachable)
        svc = _make_service(
            repo, session,
            [{"phone": p, "name": "L"} for p in reachable + unreachable],
            template_required=len(unreachable),
        )

        # D6.2: acknowledgement required because recipients need a template
        c = _launch(svc, acknowledged=True)
        assert c.total_recipients == 4, "D6.3: unreachable are materialised too"

        cycles = _drain(repo, session, c.id)
        assert cycles == 1, (
            "ADR-024 R4 / Phase 9.1G: window-closed-with-no-template is a "
            "PERMANENT failure and must reach terminal state on the first "
            "cycle, without consuming the retry budget"
        )

        bd = repo.status_breakdown(T1, c.id)
        assert bd == {"sent": 1, "failed": 3}

        final = repo.get(T1, c.id)
        assert final.sent_count == 1
        assert final.failed_count == 3
        # success > 0 -> COMPLETED (lifecycle state, not a success rate)
        assert final.status == "completed"

    def test_unreachable_failure_reason_is_explicit(self, repo, session):
        """ADR-024 D2: no silent substitution — the reason must name the cause."""
        _set_conversation_state(window_open_phones=[])
        svc = _make_service(repo, session,
                            [{"phone": "+919000000001", "name": "A"}],
                            template_required=1)
        c = _launch(svc, acknowledged=True)
        _drain(repo, session, c.id)

        row = repo.list_recipients(T1, c.id)[0]
        assert row.status == "failed"
        assert "window closed" in (row.failure_reason or "")
        assert sys.modules["app.services.whatsapp_service"].send_template.call_count == 0

    def test_all_unreachable_reconciles_to_failed(self, repo, session):
        """Zero successful sends -> FAILED, not COMPLETED."""
        phones = [f"+91900000020{i}" for i in range(2)]
        _set_conversation_state(window_open_phones=[])
        svc = _make_service(repo, session,
                            [{"phone": p, "name": "L"} for p in phones],
                            template_required=2)
        c = _launch(svc, acknowledged=True)
        _drain(repo, session, c.id)

        final = repo.get(T1, c.id)
        assert repo.status_breakdown(T1, c.id) == {"failed": 2}
        assert final.sent_count == 0
        assert final.failed_count == 2
        assert final.status == "failed"
        assert "zero successful sends" in (final.failure_reason or "")


class TestEndToEndOptOut:
    """D4 defence in depth: a contact who opts out AFTER materialisation is
    still caught at send time, because the snapshot is immutable (D1)."""

    def test_opt_out_after_launch_is_caught_at_send_time(self, repo, session):
        phones = ["+919000000001", "+919000000002"]
        # Both resolvable and in-window at launch...
        _set_conversation_state(window_open_phones=phones)
        svc = _make_service(repo, session,
                            [{"phone": p, "name": "L"} for p in phones])
        c = _launch(svc)
        assert c.total_recipients == 2

        # ...but one opts out before the worker runs.
        _set_conversation_state(window_open_phones=phones,
                                opted_out_phones=[phones[1]])
        _run_worker_cycle(repo, session)
        session.expire_all()

        rows = {r.phone: r for r in repo.list_recipients(T1, c.id)}
        assert rows[phones[0]].status == "sent"
        assert rows[phones[1]].status == "failed"
        assert "opted out" in (rows[phones[1]].failure_reason or "")
        assert repo.get(T1, c.id).failed_count == 1


class TestEndToEndCancellation:
    """ADR-025 D9 + B2, proven against the real worker rather than in isolation."""

    def test_cancel_stops_dispatch_and_counters_stay_true(self, repo, session):
        phones = [f"+91900000030{i}" for i in range(4)]
        _set_conversation_state(window_open_phones=phones)
        svc = _make_service(repo, session,
                            [{"phone": p, "name": "L"} for p in phones])
        c = _launch(svc)

        svc.cancel(T1, c.id)
        session.expire_all()

        # B2: counters reflect the cancellation immediately
        cancelled = repo.get(T1, c.id)
        assert repo.status_breakdown(T1, c.id) == {"cancelled": 4}
        assert cancelled.failed_count == 4
        assert cancelled.sent_count == 0

        # D9: a subsequent worker cycle must send nothing
        _run_worker_cycle(repo, session)
        session.expire_all()
        assert sys.modules["app.services.whatsapp_service"].send_text.call_count == 0
        assert repo.status_breakdown(T1, c.id) == {"cancelled": 4}
        assert repo.get(T1, c.id).status == "cancelled"

    def test_cancel_midway_preserves_already_sent(self, repo, session):
        """Sends that already happened stay sent and stay counted."""
        phones = [f"+91900000040{i}" for i in range(3)]
        _set_conversation_state(window_open_phones=phones)
        svc = _make_service(repo, session,
                            [{"phone": p, "name": "L"} for p in phones])
        c = _launch(svc)

        # One recipient completes before the operator cancels.
        first = repo.list_recipients(T1, c.id)[0]
        repo.mark_recipient_sent(T1, first.id, wa_message_id="wamid.EARLY")
        session.commit()

        svc.cancel(T1, c.id)
        session.expire_all()

        assert repo.status_breakdown(T1, c.id) == {"sent": 1, "cancelled": 2}
        final = repo.get(T1, c.id)
        assert final.sent_count == 1
        assert final.failed_count == 2


class TestEndToEndCounterParityInvariant:
    """Gate item 8, asserted as an invariant rather than a fixed number."""

    def _assert_parity(self, repo, campaign_id):
        row = repo.get(T1, campaign_id)
        bd = repo.status_breakdown(T1, campaign_id)
        assert row.sent_count == sum(
            bd.get(s, 0) for s in ("sent", "delivered", "read")
        )
        assert row.failed_count == sum(bd.get(s, 0) for s in ("failed", "cancelled"))

    def test_parity_holds_after_dispatch(self, repo, session):
        phones = [f"+91900000050{i}" for i in range(3)]
        _set_conversation_state(window_open_phones=phones[:2])
        svc = _make_service(repo, session,
                            [{"phone": p, "name": "L"} for p in phones],
                            template_required=1)
        c = _launch(svc, acknowledged=True)
        _drain(repo, session, c.id)
        self._assert_parity(repo, c.id)

    def test_parity_holds_after_cancellation(self, repo, session):
        phones = [f"+91900000060{i}" for i in range(3)]
        _set_conversation_state(window_open_phones=phones)
        svc = _make_service(repo, session,
                            [{"phone": p, "name": "L"} for p in phones])
        c = _launch(svc)
        svc.cancel(T1, c.id)
        session.expire_all()
        self._assert_parity(repo, c.id)

    def test_parity_holds_on_a_freshly_launched_campaign(self, repo, session):
        phones = ["+919000000701"]
        _set_conversation_state(window_open_phones=phones)
        svc = _make_service(repo, session, [{"phone": phones[0], "name": "L"}])
        c = _launch(svc)
        session.expire_all()
        self._assert_parity(repo, c.id)   # 0/0 against {"queued": 1}
