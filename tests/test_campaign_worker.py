"""
Phase 8.2C.2 — Campaign Worker tests.

Covers:
  - worker loop correctness (cycle runs, exceptions isolated)
  - commit order: claim committed before send
  - send success path (mark_recipient_sent called, commit called)
  - retry path (schedule_recipient_retry with correct backoff)
  - terminal failure path (mark_recipient_failed after MAX_RETRIES)
  - opt-out path (mark_recipient_failed, send skipped)
  - reclaim path (reclaim_stale_recipients called per tenant)
  - campaign completion (status→completed when all terminal)
  - exception isolation (per-recipient, per-tenant)
  - tenant isolation (no cross-tenant calls)
  - purity contract (no commits in repository)
  - circular import guard
  - CampaignService not imported by worker
  - forbidden file modification guard

The worker is loaded via file-path (same pattern as test_campaign_service.py)
to avoid the app-package import collision. Lazy imports inside worker functions
are satisfied by pre-populating sys.modules stubs; each test monkeypatches the
specific attribute it needs.
"""
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# ── Module loading ────────────────────────────────────────────────────────────

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(unique_name, relpath):
    path = os.path.join(_ROOT, relpath)
    spec = importlib.util.spec_from_file_location(unique_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub(name):
    """Ensure a sys.modules stub exists for `name`. Returns the stub."""
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    return sys.modules[name]


# Pre-populate stubs so the worker can be loaded without the full app package.
# The worker uses lazy (inside-function) imports only, so stubs just need to
# exist at load time; tests monkeypatch the specific attributes they need.
for _n in [
    "app", "app.persistence", "app.persistence.campaign_repository",
    "app.extensions", "app.models",
    "app.services", "app.services.whatsapp_service",
    "app.flags", "app.marketing", "app.marketing.campaign_service",
]:
    _stub(_n)

# Engine must appear enabled so CampaignService.reconcile_campaign() doesn't
# raise CampaignEngineDisabled when _check_campaign_completion delegates to it.
sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: True

# Load the real CampaignService into the stub so the worker's lazy import resolves.
_svc_mod = _load("_p82c2_svc", "app/marketing/campaign_service.py")
sys.modules["app.marketing.campaign_service"].CampaignService = (
    _svc_mod.CampaignService
)

_WKR_PATH = os.path.join(_ROOT, "app/marketing/campaign_worker.py")
wkr = _load("_p82c2_worker", "app/marketing/campaign_worker.py")


# ── Test helpers ──────────────────────────────────────────────────────────────

def _make_row(id=1, phone="919447000001", name="Alice", campaign_id=10,
              retry_count=0, send_at=None, status="sending"):
    row = MagicMock()
    row.id = id
    row.phone = phone
    row.name = name
    row.campaign_id = campaign_id
    row.retry_count = retry_count
    row.send_at = send_at
    row.status = status
    return row


def _make_campaign(id=10, message_body="Hello", status="running"):
    c = MagicMock()
    c.id = id
    c.message_body = message_body
    c.status = status
    return c


def _make_repo(claimed=None, campaign=None, breakdown=None,
               pending_ids=None, reclaim_count=0):
    repo = MagicMock()
    repo.claim_next_batch.return_value = claimed if claimed is not None else []
    repo.get.return_value = campaign
    repo.status_breakdown.return_value = breakdown or {"total": 0}
    repo.pending_tenant_ids.return_value = pending_ids or []
    repo.reclaim_stale_recipients.return_value = reclaim_count
    repo.mark_recipient_sent.return_value = MagicMock()
    repo.mark_recipient_failed.return_value = MagicMock()
    repo.schedule_recipient_retry.return_value = MagicMock()
    repo.update_status.return_value = MagicMock()
    return repo


def _make_session():
    return MagicMock()


def _good_response(wa_id="wamid.ABC"):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"messages": [{"id": wa_id}]}
    return r


def _bad_response(status=500):
    r = MagicMock()
    r.status_code = status
    r.text = "Internal Server Error"
    return r


def _patch_send_automation(response):
    """Patch send_automation on the stub module the worker lazily imports from."""
    sys.modules["app.services.whatsapp_service"].send_automation = MagicMock(
        return_value=response
    )


def _patch_conversation_state(opted_out=False, state_exists=True):
    """Return a fake ConversationState class that the worker can query."""
    state = MagicMock()
    state.is_opted_out = opted_out
    mock_cs = MagicMock()
    mock_cs.query.filter_by.return_value.first.return_value = (
        state if state_exists else None
    )
    sys.modules["app.models"].ConversationState = mock_cs
    return mock_cs


# ── Purity contract ───────────────────────────────────────────────────────────

_WKR_SRC = open(_WKR_PATH, encoding='utf-8').read()


class TestPurityContract:
    def test_worker_does_not_import_legacy_campaign_service(self):
        # The worker may import the NEW marketing.campaign_service (CampaignService).
        # It must NOT import the LEGACY app.services.campaign_service.
        assert "from app.services.campaign_service" not in _WKR_SRC
        assert "services.campaign_service" not in _WKR_SRC

    def test_worker_does_not_import_broadcast(self):
        assert "broadcast" not in _WKR_SRC

    def test_worker_does_not_reference_followup_service(self):
        assert "followup_service" not in _WKR_SRC

    def test_repository_contains_no_commits(self):
        """The repository layer never commits — this is a worker concern."""
        repo_src = open(os.path.join(_ROOT, "app/persistence/campaign_repository.py"), encoding="utf-8").read()
        assert ".commit()" not in repo_src

    def test_constants_present(self):
        assert wkr.POLL_INTERVAL == 300
        assert wkr.CLAIM_BATCH == 50
        assert wkr.STALE_MINUTES == 10
        assert wkr.MAX_RETRIES == 3

    def test_no_circular_import_at_module_level(self):
        """The worker must be importable without triggering the full app."""
        assert wkr is not None

    def test_campaign_service_not_imported_at_module_level(self):
        # The import IS present but must be INSIDE a function (lazy), not at top level.
        top_lines = [l for l in _WKR_SRC.splitlines()
                     if l.startswith("from ") or l.startswith("import ")]
        for line in top_lines:
            assert "campaign_service" not in line, (
                f"campaign_service must not be a module-level import: {line!r}"
            )

    def test_init_campaign_worker_is_callable(self):
        assert callable(wkr.init_campaign_worker)

    def test_marketing_init_does_not_auto_import_worker(self):
        """app/marketing/__init__.py must not import the worker (it is UNWIRED)."""
        init_src = open(os.path.join(_ROOT, "app/marketing/__init__.py"), encoding="utf-8").read()
        assert "campaign_worker" not in init_src


# ── _extract_message_id ───────────────────────────────────────────────────────

class TestExtractMessageId:
    def test_extracts_from_valid_response(self):
        r = _good_response("wamid.XYZ")
        assert wkr._extract_message_id(r) == "wamid.XYZ"

    def test_returns_none_on_missing_key(self):
        r = MagicMock()
        r.json.return_value = {}
        assert wkr._extract_message_id(r) is None

    def test_returns_none_on_json_error(self):
        r = MagicMock()
        r.json.side_effect = ValueError("bad json")
        assert wkr._extract_message_id(r) is None

    def test_returns_none_on_empty_messages(self):
        r = MagicMock()
        r.json.return_value = {"messages": []}
        assert wkr._extract_message_id(r) is None


# ── _reclaim_stale ────────────────────────────────────────────────────────────

class TestReclaimStale:
    def test_calls_reclaim_with_increment_retry_false(self):
        repo = _make_repo(reclaim_count=3)
        session = _make_session()
        stale = datetime.utcnow() - timedelta(minutes=10)
        wkr._reclaim_stale(repo, session, "T1", stale)
        repo.reclaim_stale_recipients.assert_called_once_with(
            "T1", stale, increment_retry=False
        )

    def test_commits_when_rows_reclaimed(self):
        repo = _make_repo(reclaim_count=2)
        session = _make_session()
        wkr._reclaim_stale(repo, session, "T1", datetime.utcnow())
        session.commit.assert_called_once()

    def test_no_commit_when_nothing_reclaimed(self):
        repo = _make_repo(reclaim_count=0)
        session = _make_session()
        wkr._reclaim_stale(repo, session, "T1", datetime.utcnow())
        session.commit.assert_not_called()


# ── _handle_failure ───────────────────────────────────────────────────────────

class TestHandleFailure:
    def test_first_failure_schedules_retry_15min(self):
        repo = _make_repo()
        session = _make_session()
        row = _make_row(retry_count=0)
        now = datetime(2026, 1, 1, 12, 0, 0)

        wkr._handle_failure(repo, session, "T1", row, "timeout", now)

        expected_next = now + timedelta(minutes=15)
        repo.schedule_recipient_retry.assert_called_once_with(
            "T1", row.id,
            failure_reason="timeout",
            next_send_at=expected_next,
            attempted_at=now,
        )
        repo.mark_recipient_failed.assert_not_called()
        session.commit.assert_called_once()

    def test_second_failure_schedules_retry_30min(self):
        repo = _make_repo()
        session = _make_session()
        row = _make_row(retry_count=1)
        now = datetime(2026, 1, 1, 12, 0, 0)

        wkr._handle_failure(repo, session, "T1", row, "timeout", now)

        expected_next = now + timedelta(minutes=30)
        repo.schedule_recipient_retry.assert_called_once_with(
            "T1", row.id,
            failure_reason="timeout",
            next_send_at=expected_next,
            attempted_at=now,
        )

    def test_third_failure_marks_terminal(self):
        """attempt == MAX_RETRIES → terminal, no retry."""
        repo = _make_repo()
        session = _make_session()
        row = _make_row(retry_count=2)
        now = datetime(2026, 1, 1, 12, 0, 0)

        wkr._handle_failure(repo, session, "T1", row, "final error", now)

        repo.mark_recipient_failed.assert_called_once_with(
            "T1", row.id, failure_reason="final error", attempted_at=now
        )
        repo.schedule_recipient_retry.assert_not_called()
        session.commit.assert_called_once()

    def test_retry_count_none_treated_as_zero(self):
        repo = _make_repo()
        session = _make_session()
        row = _make_row(retry_count=None)
        wkr._handle_failure(repo, session, "T1", row, "err", datetime(2026, 1, 1))
        # attempt=1 < 3 → retry
        repo.schedule_recipient_retry.assert_called_once()

    def test_backoff_15_30_sequence(self):
        """15 * attempt minutes — matches FollowUpJob exactly."""
        now = datetime(2026, 1, 1, 12, 0, 0)
        expected = {0: 15, 1: 30}
        for retry_count, minutes in expected.items():
            repo = _make_repo()
            session = _make_session()
            row = _make_row(retry_count=retry_count)
            wkr._handle_failure(repo, session, "T1", row, "err", now)
            _, kwargs = repo.schedule_recipient_retry.call_args
            assert kwargs["next_send_at"] == now + timedelta(minutes=minutes), (
                f"retry_count={retry_count} should give {minutes}min backoff"
            )


# ── _send_one ─────────────────────────────────────────────────────────────────

class TestSendOne:
    def setup_method(self):
        """Reset lazy-import stubs between tests."""
        _patch_conversation_state(opted_out=False, state_exists=False)
        _patch_send_automation(_good_response())

    def test_success_path_calls_mark_sent_and_commits(self):
        repo = _make_repo()
        session = _make_session()
        row = _make_row()
        _patch_send_automation(_good_response("wamid.1"))

        wkr._send_one(repo, session, "T1", row, "Hello", datetime.utcnow())

        repo.mark_recipient_sent.assert_called_once()
        args, kwargs = repo.mark_recipient_sent.call_args
        assert args[0] == "T1"
        assert args[1] == row.id
        assert kwargs.get("wa_message_id") == "wamid.1"
        session.commit.assert_called_once()

    def test_success_does_not_call_failure_methods(self):
        repo = _make_repo()
        session = _make_session()
        _patch_send_automation(_good_response())

        wkr._send_one(repo, session, "T1", _make_row(), "msg", datetime.utcnow())

        repo.mark_recipient_failed.assert_not_called()
        repo.schedule_recipient_retry.assert_not_called()

    def test_api_failure_triggers_retry(self):
        repo = _make_repo()
        session = _make_session()
        row = _make_row(retry_count=0)
        _patch_send_automation(_bad_response(500))

        wkr._send_one(repo, session, "T1", row, "Hello", datetime.utcnow())

        repo.schedule_recipient_retry.assert_called_once()
        repo.mark_recipient_sent.assert_not_called()

    def test_api_failure_at_cap_triggers_terminal(self):
        repo = _make_repo()
        session = _make_session()
        row = _make_row(retry_count=2)  # attempt=3 >= MAX_RETRIES
        _patch_send_automation(_bad_response(500))

        wkr._send_one(repo, session, "T1", row, "Hello", datetime.utcnow())

        repo.mark_recipient_failed.assert_called_once()
        repo.schedule_recipient_retry.assert_not_called()

    def test_opted_out_marks_failed_and_skips_send(self):
        repo = _make_repo()
        session = _make_session()
        row = _make_row()
        _patch_conversation_state(opted_out=True)
        send_mock = MagicMock()
        sys.modules["app.services.whatsapp_service"].send_automation = send_mock

        wkr._send_one(repo, session, "T1", row, "Hello", datetime.utcnow())

        send_mock.assert_not_called()
        repo.mark_recipient_failed.assert_called_once()
        args, kwargs = repo.mark_recipient_failed.call_args
        reason = kwargs.get("failure_reason") or (args[2] if len(args) > 2 else "")
        assert "opted out" in reason
        session.commit.assert_called_once()

    def test_no_state_row_does_not_block_send(self):
        repo = _make_repo()
        session = _make_session()
        _patch_conversation_state(state_exists=False)
        _patch_send_automation(_good_response())

        wkr._send_one(repo, session, "T1", _make_row(), "msg", datetime.utcnow())

        repo.mark_recipient_sent.assert_called_once()

    def test_exception_from_send_isolated(self):
        """An exception in send_automation must not propagate — failure is recorded."""
        repo = _make_repo()
        session = _make_session()
        sys.modules["app.services.whatsapp_service"].send_automation = MagicMock(
            side_effect=RuntimeError("network error")
        )

        wkr._send_one(repo, session, "T1", _make_row(), "Hello", datetime.utcnow())

        assert repo.schedule_recipient_retry.called or repo.mark_recipient_failed.called

    def test_send_uses_row_name(self):
        repo = _make_repo()
        session = _make_session()
        row = _make_row(name="Bob")
        send_mock = MagicMock(return_value=_good_response())
        sys.modules["app.services.whatsapp_service"].send_automation = send_mock

        wkr._send_one(repo, session, "T1", row, "msg", datetime.utcnow())

        call_args = send_mock.call_args
        name_val = call_args[1].get("name") if call_args[1] else call_args[0][2]
        assert name_val == "Bob"

    def test_send_falls_back_to_student_when_name_missing(self):
        repo = _make_repo()
        session = _make_session()
        row = _make_row(name=None)
        send_mock = MagicMock(return_value=_good_response())
        sys.modules["app.services.whatsapp_service"].send_automation = send_mock

        wkr._send_one(repo, session, "T1", row, "msg", datetime.utcnow())

        call_args = send_mock.call_args
        name_val = call_args[1].get("name") if call_args[1] else call_args[0][2]
        assert name_val == "Student"

    def test_send_passes_tenant_id(self):
        repo = _make_repo()
        session = _make_session()
        send_mock = MagicMock(return_value=_good_response())
        sys.modules["app.services.whatsapp_service"].send_automation = send_mock

        wkr._send_one(repo, session, "TENANT_Q", _make_row(), "msg", datetime.utcnow())

        call_args = send_mock.call_args
        tid = call_args[1].get("tenant_id") if call_args[1] else None
        assert tid == "TENANT_Q"


# ── _process_tenant ───────────────────────────────────────────────────────────

class TestProcessTenant:
    def test_no_claimed_rows_skips_commit(self):
        repo = _make_repo(claimed=[], campaign=_make_campaign())
        session = _make_session()
        with patch.object(wkr, "_send_one"), patch.object(wkr, "_check_campaign_completion"):
            wkr._process_tenant(repo, session, "T1", datetime.utcnow())
        session.commit.assert_not_called()

    def test_claim_commit_precedes_send(self):
        """The first commit must be the claim commit, before any _send_one call."""
        events = []
        row = _make_row()
        repo = _make_repo(claimed=[row], campaign=_make_campaign())
        session = _make_session()
        session.commit.side_effect = lambda: events.append("commit")

        def fake_send(*a, **kw):
            events.append("send")

        with patch.object(wkr, "_send_one", side_effect=fake_send), \
             patch.object(wkr, "_check_campaign_completion"):
            wkr._process_tenant(repo, session, "T1", datetime.utcnow())

        assert events[0] == "commit", "Claim commit must be first event"
        assert "send" in events

    def test_completion_checked_per_unique_campaign(self):
        rows = [
            _make_row(id=1, campaign_id=10),
            _make_row(id=2, campaign_id=20),
            _make_row(id=3, campaign_id=10),
        ]
        repo = _make_repo(claimed=rows, campaign=_make_campaign())
        session = _make_session()
        completion_calls = []

        def fake_completion(r, s, tid, cid, now):
            completion_calls.append(cid)

        with patch.object(wkr, "_send_one"), \
             patch.object(wkr, "_check_campaign_completion", side_effect=fake_completion):
            wkr._process_tenant(repo, session, "T1", datetime.utcnow())

        assert set(completion_calls) == {10, 20}

    def test_campaign_fetched_once_per_unique_campaign(self):
        rows = [_make_row(id=i, campaign_id=10) for i in range(1, 4)]
        repo = _make_repo(claimed=rows, campaign=_make_campaign())
        session = _make_session()

        with patch.object(wkr, "_send_one"), \
             patch.object(wkr, "_check_campaign_completion"):
            wkr._process_tenant(repo, session, "T1", datetime.utcnow())

        # repo.get called exactly once for the campaign batch, plus potentially
        # once more inside _check_campaign_completion (patched out here).
        assert repo.get.call_count == 1

    def test_claim_tenant_id_passed_through(self):
        repo = _make_repo(claimed=[], campaign=_make_campaign())
        session = _make_session()
        wkr._process_tenant(repo, session, "MY_TENANT", datetime.utcnow())
        repo.claim_next_batch.assert_called_once()
        assert repo.claim_next_batch.call_args[0][0] == "MY_TENANT"


# ── _check_campaign_completion (Phase 8.2C.4: delegates to CampaignService) ──
#
# Outcome logic (completed/failed/running rules) is now owned by CampaignService
# and is fully tested in test_campaign_reconciliation.py. Tests here prove only
# the delegation contract: _check_campaign_completion must call
# CampaignService.reconcile_campaign and must not call repo.update_status itself.

class TestCheckCampaignCompletion:
    def _mock_reconcile(self, return_value="running"):
        """Replace CampaignService.reconcile_campaign on the stub module."""
        mock = MagicMock(return_value=return_value)
        _svc_cls = _svc_mod.CampaignService

        class _PatchedSvc(_svc_cls):
            def reconcile_campaign(self, tid, cid):
                return mock(tid, cid)

        sys.modules["app.marketing.campaign_service"].CampaignService = _PatchedSvc
        return mock

    def teardown_method(self, _):
        # Restore real CampaignService after each test
        sys.modules["app.marketing.campaign_service"].CampaignService = (
            _svc_mod.CampaignService
        )

    def test_delegates_to_service_reconcile_campaign(self):
        mock = self._mock_reconcile("running")
        repo = _make_repo(campaign=_make_campaign(), breakdown={"sent": 5})
        wkr._check_campaign_completion(repo, _make_session(), "T1", 42,
                                       datetime(2026, 1, 1))
        mock.assert_called_once_with("T1", 42)

    def test_worker_does_not_call_repo_update_status(self):
        """CampaignService owns update_status — the worker must not call it directly."""
        self._mock_reconcile("completed")
        repo = _make_repo(campaign=_make_campaign(), breakdown={"sent": 5})
        wkr._check_campaign_completion(repo, _make_session(), "T1", 10,
                                       datetime(2026, 1, 1))
        repo.update_status.assert_not_called()

    def test_running_result_is_silent(self):
        """A 'running' result must not produce any repo writes or session commits."""
        self._mock_reconcile("running")
        repo = _make_repo(campaign=_make_campaign(), breakdown={"queued": 3})
        session = _make_session()
        wkr._check_campaign_completion(repo, session, "T1", 10, datetime(2026, 1, 1))
        repo.update_status.assert_not_called()
        session.commit.assert_not_called()

    def test_skipped_result_is_silent(self):
        self._mock_reconcile("skipped")
        repo = _make_repo(campaign=None, breakdown={})
        session = _make_session()
        wkr._check_campaign_completion(repo, session, "T1", 10, datetime(2026, 1, 1))
        session.commit.assert_not_called()


# ── _run_cycle ────────────────────────────────────────────────────────────────

class TestRunCycle:
    def _setup_repo_stub(self, tenant_ids):
        fake_repo = _make_repo(pending_ids=tenant_ids)
        fake_repo_cls = MagicMock(return_value=fake_repo)
        sys.modules["app.persistence.campaign_repository"].CampaignRepository = (
            fake_repo_cls
        )
        fake_db = MagicMock()
        sys.modules["app.extensions"].db = fake_db
        return fake_repo, fake_db

    def test_cycles_over_all_pending_tenants(self):
        processed = []
        fake_repo, _ = self._setup_repo_stub(["T1", "T2", "T3"])

        def fake_process(repo, session, tid, now):
            processed.append(tid)

        with patch.object(wkr, "_reclaim_stale"), \
             patch.object(wkr, "_process_tenant", side_effect=fake_process):
            wkr._run_cycle()

        assert set(processed) == {"T1", "T2", "T3"}

    def test_tenant_exception_does_not_abort_others(self):
        processed = []
        self._setup_repo_stub(["T1", "T2"])

        def fake_process(repo, session, tid, now):
            if tid == "T1":
                raise RuntimeError("T1 explosion")
            processed.append(tid)

        with patch.object(wkr, "_reclaim_stale"), \
             patch.object(wkr, "_process_tenant", side_effect=fake_process):
            wkr._run_cycle()

        assert "T2" in processed

    def test_reclaim_called_before_process(self):
        order = []
        self._setup_repo_stub(["T1"])

        def fake_reclaim(repo, session, tid, stale):
            order.append(("reclaim", tid))

        def fake_process(repo, session, tid, now):
            order.append(("process", tid))

        with patch.object(wkr, "_reclaim_stale", side_effect=fake_reclaim), \
             patch.object(wkr, "_process_tenant", side_effect=fake_process):
            wkr._run_cycle()

        assert order == [("reclaim", "T1"), ("process", "T1")]

    def test_no_tenants_does_nothing(self):
        self._setup_repo_stub([])

        with patch.object(wkr, "_reclaim_stale") as mock_r, \
             patch.object(wkr, "_process_tenant") as mock_p:
            wkr._run_cycle()

        mock_r.assert_not_called()
        mock_p.assert_not_called()


# ── Tenant isolation ──────────────────────────────────────────────────────────

class TestTenantIsolation:
    def test_claim_scoped_to_tenant(self):
        row = _make_row(campaign_id=10)
        repo = _make_repo(claimed=[row], campaign=_make_campaign())
        session = _make_session()

        with patch.object(wkr, "_send_one"), \
             patch.object(wkr, "_check_campaign_completion"):
            wkr._process_tenant(repo, session, "TENANT_X", datetime.utcnow())

        repo.claim_next_batch.assert_called_once()
        assert repo.claim_next_batch.call_args[0][0] == "TENANT_X"

    def test_get_campaign_scoped_to_tenant(self):
        row = _make_row(campaign_id=10)
        repo = _make_repo(claimed=[row], campaign=_make_campaign())
        session = _make_session()

        with patch.object(wkr, "_send_one"), \
             patch.object(wkr, "_check_campaign_completion"):
            wkr._process_tenant(repo, session, "TENANT_X", datetime.utcnow())

        assert repo.get.call_args[0][0] == "TENANT_X"

    def test_mark_sent_scoped_to_tenant(self):
        repo = _make_repo()
        session = _make_session()
        _patch_conversation_state(state_exists=False)
        _patch_send_automation(_good_response())

        wkr._send_one(repo, session, "TID_99", _make_row(), "msg", datetime.utcnow())

        assert repo.mark_recipient_sent.call_args[0][0] == "TID_99"

    def test_mark_failed_on_opt_out_scoped_to_tenant(self):
        repo = _make_repo()
        session = _make_session()
        _patch_conversation_state(opted_out=True)

        wkr._send_one(repo, session, "TID_77", _make_row(), "msg", datetime.utcnow())

        assert repo.mark_recipient_failed.call_args[0][0] == "TID_77"

    def test_reclaim_scoped_to_tenant(self):
        repo = _make_repo(reclaim_count=1)
        session = _make_session()
        stale = datetime.utcnow()
        wkr._reclaim_stale(repo, session, "SPECIFIC_TENANT", stale)
        assert repo.reclaim_stale_recipients.call_args[0][0] == "SPECIFIC_TENANT"


# ── Model attribute contract ──────────────────────────────────────────────────
# The worker reads specific attributes from Campaign / CampaignRecipient rows.
# Rather than importing app.models (which pulls in Flask-SQLAlchemy), we verify
# that the production schema file defines the expected columns. This matches the
# pattern used by test_campaign_service.py and test_campaign_repository.py.

class TestModelAttributeContract:
    def _models_src(self):
        return open(os.path.join(_ROOT, "app/models.py"), encoding="utf-8").read()

    def test_campaign_has_required_columns(self):
        src = self._models_src()
        for col in ("tenant_id", "status", "message_body"):
            assert col in src, f"models.py missing Campaign column: {col}"

    def test_campaign_recipient_has_required_columns(self):
        src = self._models_src()
        for col in ("campaign_id", "phone", "retry_count", "send_at", "wa_message_id"):
            assert col in src, f"models.py missing CampaignRecipient column: {col}"

    def test_worker_module_loads_cleanly(self):
        assert wkr.init_campaign_worker is not None
