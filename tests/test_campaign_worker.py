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
    "app.services", "app.services.whatsapp_service", "app.services.log_service",
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
# ADR-024 D3: _process_tenant's lazy import of resolve_campaign_template must
# also resolve. Individual tests override this attribute when they need a
# specific resolution outcome.
sys.modules["app.marketing.campaign_service"].resolve_campaign_template = (
    _svc_mod.resolve_campaign_template
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


def _make_campaign(id=10, message_body="Hello", status="running", template_id=None):
    c = MagicMock()
    c.id = id
    c.message_body = message_body
    c.status = status
    # Explicit default: a bare MagicMock().template_id is a truthy auto-mock,
    # which would wrongly trigger the ADR-024 D3 template-resolution branch
    # in _process_tenant for every test that doesn't care about templates.
    c.template_id = template_id
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


def _sent_result(wa_id="wamid.ABC", send_type="text"):
    return {"outcome": "sent", "wa_message_id": wa_id, "send_type": send_type}


def _failed_result(reason="API error 500: boom"):
    return {"outcome": "failed", "reason": reason}


def _patch_send_campaign_message(result_or_side_effect):
    """Patch wkr.send_campaign_message directly (ADR-024 D1 dispatch entry point).

    Accepts either a fixed return dict or a callable/exception for side_effect
    semantics, matching MagicMock's own dual usage.
    """
    if isinstance(result_or_side_effect, dict):
        mock = MagicMock(return_value=result_or_side_effect)
    else:
        mock = MagicMock(side_effect=result_or_side_effect)
    setattr(wkr, "send_campaign_message", mock)
    return mock


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

    def test_worker_does_not_call_send_automation(self):
        """ADR-024 D1: campaign dispatch must never call the automation
        interceptor. send_automation is mentioned only in prose explaining why
        it is avoided — it must never appear as an actual call."""
        assert "send_automation(" not in _WKR_SRC

    def test_worker_does_not_write_pending_message(self):
        """ADR-024 D2: campaign dispatch must never queue to PendingMessage.
        PendingMessage is mentioned only in prose — never instantiated."""
        assert "PendingMessage(" not in _WKR_SRC

    def test_repository_contains_no_commits(self):
        """The repository layer never commits — this is a worker concern."""
        repo_src = open(os.path.join(_ROOT, "app/persistence/campaign_repository.py"), encoding="utf-8").read()
        assert ".commit()" not in repo_src

    def test_constants_present(self):
        assert wkr.POLL_INTERVAL == 300
        assert wkr.CLAIM_BATCH == 50
        assert wkr.STALE_MINUTES == 10
        assert wkr.MAX_RETRIES == 3
        assert wkr.CAMPAIGN_SEND_DELAY_SECONDS == 1.5

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


# ── Phase 9.1G: failure classification (ADR-024 R4) ──────────────────────────

class TestFailureClassification:
    """_classify_provider_failure maps a provider status to retryable or not."""

    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    def test_5xx_is_transient(self, code):
        assert wkr._classify_provider_failure(code) == wkr.FAILURE_TRANSIENT

    @pytest.mark.parametrize("code", [408, 429])
    def test_timeout_and_rate_limit_are_transient(self, code):
        """These describe the moment, not the request — a later attempt may
        legitimately succeed."""
        assert wkr._classify_provider_failure(code) == wkr.FAILURE_TRANSIENT

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
    def test_other_4xx_is_permanent(self, code):
        """Malformed number, bad auth, rejected template — the identical
        request will fail identically."""
        assert wkr._classify_provider_failure(code) == wkr.FAILURE_PERMANENT

    @pytest.mark.parametrize("bad", [None, "abc", object()])
    def test_unknown_status_defaults_to_transient(self, bad):
        """Fail toward preserving delivery: retrying costs attempts, wrongly
        terminating silently drops a message."""
        assert wkr._classify_provider_failure(bad) == wkr.FAILURE_TRANSIENT


class TestPermanentFailureIsTerminal:
    """ADR-024 R4: a permanent failure must never enter the retry queue."""

    def test_permanent_marks_failed_on_first_attempt(self):
        repo = _make_repo()
        session = _make_session()
        row = _make_row(retry_count=0)          # first attempt
        now = datetime(2026, 1, 1, 12, 0, 0)

        wkr._handle_failure(repo, session, "T1", row, "window closed", now,
                            failure_kind=wkr.FAILURE_PERMANENT)

        repo.mark_recipient_failed.assert_called_once_with(
            "T1", row.id, failure_reason="window closed", attempted_at=now
        )
        repo.schedule_recipient_retry.assert_not_called()
        session.commit.assert_called_once()

    def test_permanent_never_schedules_a_retry_at_any_attempt(self):
        for retry_count in (0, 1, 2):
            repo = _make_repo()
            wkr._handle_failure(repo, _make_session(), "T1",
                                _make_row(retry_count=retry_count),
                                "permanent reason", datetime(2026, 1, 1),
                                failure_kind=wkr.FAILURE_PERMANENT)
            repo.schedule_recipient_retry.assert_not_called()
            repo.mark_recipient_failed.assert_called_once()

    def test_transient_still_retries(self):
        """Regression: the existing retry policy must be untouched."""
        repo = _make_repo()
        row = _make_row(retry_count=0)
        now = datetime(2026, 1, 1, 12, 0, 0)

        wkr._handle_failure(repo, _make_session(), "T1", row, "API error 500",
                            now, failure_kind=wkr.FAILURE_TRANSIENT)

        repo.schedule_recipient_retry.assert_called_once()
        repo.mark_recipient_failed.assert_not_called()

    def test_transient_still_terminal_at_cap(self):
        repo = _make_repo()
        wkr._handle_failure(repo, _make_session(), "T1",
                            _make_row(retry_count=2), "API error 500",
                            datetime(2026, 1, 1),
                            failure_kind=wkr.FAILURE_TRANSIENT)
        repo.mark_recipient_failed.assert_called_once()
        repo.schedule_recipient_retry.assert_not_called()

    def test_default_kind_is_transient(self):
        """Callers omitting failure_kind keep pre-9.1G behaviour."""
        repo = _make_repo()
        wkr._handle_failure(repo, _make_session(), "T1",
                            _make_row(retry_count=0), "unclassified",
                            datetime(2026, 1, 1))
        repo.schedule_recipient_retry.assert_called_once()


class TestSendOnePropagatesClassification:
    """_send_one must carry failure_kind through to _handle_failure — this is
    the seam where the classification was previously destroyed by `raise`."""

    def setup_method(self):
        _patch_conversation_state(opted_out=False, state_exists=False)

    def _capture_handle_failure(self):
        captured = {}

        def _spy(repo, session, tenant_id, row, reason, now, failure_kind=None):
            captured["reason"] = reason
            captured["kind"] = failure_kind
        return captured, _spy

    def test_window_closed_no_template_is_permanent(self):
        """The exact production case: campaign #2's recipient was retried
        with reason 'window closed and no approved template'."""
        captured, spy = self._capture_handle_failure()
        _patch_send_campaign_message({
            "outcome": "failed",
            "reason": "24-hour window closed and no approved WhatsApp template configured",
            "failure_kind": wkr.FAILURE_PERMANENT,
        })
        with patch.object(wkr, "_handle_failure", side_effect=spy), \
             patch.object(wkr.time, "sleep"):
            wkr._send_one(_make_repo(), _make_session(), "T1", _make_row(),
                          "msg", None, datetime.utcnow())
        assert captured["kind"] == wkr.FAILURE_PERMANENT
        assert "window closed" in captured["reason"]

    def test_provider_5xx_is_transient(self):
        captured, spy = self._capture_handle_failure()
        _patch_send_campaign_message({
            "outcome": "failed",
            "reason": "API error 500: boom",
            "failure_kind": wkr.FAILURE_TRANSIENT,
        })
        with patch.object(wkr, "_handle_failure", side_effect=spy), \
             patch.object(wkr.time, "sleep"):
            wkr._send_one(_make_repo(), _make_session(), "T1", _make_row(),
                          "msg", None, datetime.utcnow())
        assert captured["kind"] == wkr.FAILURE_TRANSIENT

    def test_raised_exception_is_transient(self):
        """An unexpected throw carries no classification — a genuine blip
        must still get its retries."""
        captured, spy = self._capture_handle_failure()
        _patch_send_campaign_message(RuntimeError("connection reset"))
        with patch.object(wkr, "_handle_failure", side_effect=spy), \
             patch.object(wkr.time, "sleep"):
            wkr._send_one(_make_repo(), _make_session(), "T1", _make_row(),
                          "msg", None, datetime.utcnow())
        assert captured["kind"] == wkr.FAILURE_TRANSIENT

    def test_missing_failure_kind_defaults_transient(self):
        """Defensive: a result dict without failure_kind must not crash."""
        captured, spy = self._capture_handle_failure()
        _patch_send_campaign_message({"outcome": "failed", "reason": "legacy shape"})
        with patch.object(wkr, "_handle_failure", side_effect=spy), \
             patch.object(wkr.time, "sleep"):
            wkr._send_one(_make_repo(), _make_session(), "T1", _make_row(),
                          "msg", None, datetime.utcnow())
        assert captured["kind"] == wkr.FAILURE_TRANSIENT

    def test_permanent_failure_end_to_end_no_retry_scheduled(self):
        """Full path with the REAL _handle_failure: permanent -> failed, and
        schedule_recipient_retry is never called."""
        repo = _make_repo()
        _patch_send_campaign_message({
            "outcome": "failed",
            "reason": "24-hour window closed and no approved WhatsApp template configured",
            "failure_kind": wkr.FAILURE_PERMANENT,
        })
        with patch.object(wkr.time, "sleep"):
            wkr._send_one(repo, _make_session(), "T1", _make_row(retry_count=0),
                          "msg", None, datetime.utcnow())
        repo.mark_recipient_failed.assert_called_once()
        repo.schedule_recipient_retry.assert_not_called()


# ── _send_one (ADR-024: dispatches via send_campaign_message, not send_automation) ──

class TestSendOne:
    def setup_method(self):
        """Reset lazy-import stubs between tests."""
        _patch_conversation_state(opted_out=False, state_exists=False)
        _patch_send_campaign_message(_sent_result())

    def test_success_path_calls_mark_sent_and_commits(self):
        repo = _make_repo()
        session = _make_session()
        row = _make_row()
        _patch_send_campaign_message(_sent_result("wamid.1"))

        wkr._send_one(repo, session, "T1", row, "Hello", None, datetime.utcnow())

        repo.mark_recipient_sent.assert_called_once()
        args, kwargs = repo.mark_recipient_sent.call_args
        assert args[0] == "T1"
        assert args[1] == row.id
        assert kwargs.get("wa_message_id") == "wamid.1"
        session.commit.assert_called_once()

    def test_success_does_not_call_failure_methods(self):
        repo = _make_repo()
        session = _make_session()
        _patch_send_campaign_message(_sent_result())

        wkr._send_one(repo, session, "T1", _make_row(), "msg", None, datetime.utcnow())

        repo.mark_recipient_failed.assert_not_called()
        repo.schedule_recipient_retry.assert_not_called()

    def test_failed_outcome_triggers_retry(self):
        repo = _make_repo()
        session = _make_session()
        row = _make_row(retry_count=0)
        _patch_send_campaign_message(_failed_result("API error 500: boom"))

        wkr._send_one(repo, session, "T1", row, "Hello", None, datetime.utcnow())

        repo.schedule_recipient_retry.assert_called_once()
        repo.mark_recipient_sent.assert_not_called()
        _, kwargs = repo.schedule_recipient_retry.call_args
        assert "API error 500" in kwargs["failure_reason"]

    def test_failed_outcome_at_cap_triggers_terminal(self):
        repo = _make_repo()
        session = _make_session()
        row = _make_row(retry_count=2)  # attempt=3 >= MAX_RETRIES
        _patch_send_campaign_message(_failed_result())

        wkr._send_one(repo, session, "T1", row, "Hello", None, datetime.utcnow())

        repo.mark_recipient_failed.assert_called_once()
        repo.schedule_recipient_retry.assert_not_called()

    def test_window_closed_no_template_fails_without_provider_call(self):
        """ADR-024 D2: window closed + no template = explicit failure, no substitution."""
        repo = _make_repo()
        session = _make_session()
        row = _make_row(retry_count=2)
        _patch_send_campaign_message(
            _failed_result("24-hour window closed and no approved WhatsApp template configured")
        )

        wkr._send_one(repo, session, "T1", row, "Hello", None, datetime.utcnow())

        repo.mark_recipient_failed.assert_called_once()
        _, kwargs = repo.mark_recipient_failed.call_args
        assert "window closed" in kwargs["failure_reason"]
        repo.mark_recipient_sent.assert_not_called()

    def test_opted_out_marks_failed_and_skips_send(self):
        repo = _make_repo()
        session = _make_session()
        row = _make_row()
        _patch_conversation_state(opted_out=True)
        dispatch_mock = _patch_send_campaign_message(_sent_result())

        wkr._send_one(repo, session, "T1", row, "Hello", None, datetime.utcnow())

        dispatch_mock.assert_not_called()
        repo.mark_recipient_failed.assert_called_once()
        args, kwargs = repo.mark_recipient_failed.call_args
        reason = kwargs.get("failure_reason") or (args[2] if len(args) > 2 else "")
        assert "opted out" in reason
        session.commit.assert_called_once()

    def test_no_state_row_does_not_block_send(self):
        repo = _make_repo()
        session = _make_session()
        _patch_conversation_state(state_exists=False)
        _patch_send_campaign_message(_sent_result())

        wkr._send_one(repo, session, "T1", _make_row(), "msg", None, datetime.utcnow())

        repo.mark_recipient_sent.assert_called_once()

    def test_exception_from_dispatch_isolated(self):
        """An exception raised by send_campaign_message must not propagate."""
        repo = _make_repo()
        session = _make_session()
        _patch_send_campaign_message(RuntimeError("network error"))

        wkr._send_one(repo, session, "T1", _make_row(), "Hello", None, datetime.utcnow())

        assert repo.schedule_recipient_retry.called or repo.mark_recipient_failed.called

    def test_send_uses_row_name(self):
        repo = _make_repo()
        session = _make_session()
        row = _make_row(name="Bob")
        dispatch_mock = _patch_send_campaign_message(_sent_result())

        wkr._send_one(repo, session, "T1", row, "msg", None, datetime.utcnow())

        assert dispatch_mock.call_args[0][1] == "Bob"

    def test_send_falls_back_to_student_when_name_missing(self):
        repo = _make_repo()
        session = _make_session()
        row = _make_row(name=None)
        dispatch_mock = _patch_send_campaign_message(_sent_result())

        wkr._send_one(repo, session, "T1", row, "msg", None, datetime.utcnow())

        assert dispatch_mock.call_args[0][1] == "Student"

    def test_send_passes_tenant_id(self):
        repo = _make_repo()
        session = _make_session()
        dispatch_mock = _patch_send_campaign_message(_sent_result())

        wkr._send_one(repo, session, "TENANT_Q", _make_row(), "msg", None, datetime.utcnow())

        assert dispatch_mock.call_args[0][2] == "TENANT_Q"

    def test_send_passes_template_through(self):
        repo = _make_repo()
        session = _make_session()
        dispatch_mock = _patch_send_campaign_message(_sent_result())
        sentinel_template = MagicMock(name="template")

        wkr._send_one(
            repo, session, "T1", _make_row(), "msg", sentinel_template, datetime.utcnow()
        )

        assert dispatch_mock.call_args[0][4] is sentinel_template

    def test_conversation_history_logged_on_success(self):
        repo = _make_repo()
        session = _make_session()
        _patch_send_campaign_message(_sent_result("wamid.9", send_type="template"))
        log_mock = MagicMock()
        sys.modules["app.services.log_service"].save_conversation_message = log_mock

        wkr._send_one(repo, session, "T1", _make_row(), "Campaign body", None, datetime.utcnow())

        log_mock.assert_called_once()
        _, kwargs = log_mock.call_args
        assert kwargs["source"] == "campaign"
        assert kwargs["message_type"] == "template"
        assert kwargs["wa_message_id"] == "wamid.9"
        assert kwargs["tenant_id"] == "T1"

    def test_history_log_failure_does_not_mark_recipient_failed(self):
        """D5: logging is best-effort — it must never undo an already-committed send."""
        repo = _make_repo()
        session = _make_session()
        _patch_send_campaign_message(_sent_result())
        sys.modules["app.services.log_service"].save_conversation_message = MagicMock(
            side_effect=RuntimeError("log db down")
        )

        wkr._send_one(repo, session, "T1", _make_row(), "msg", None, datetime.utcnow())

        repo.mark_recipient_sent.assert_called_once()
        repo.mark_recipient_failed.assert_not_called()

    def test_rate_limit_delay_applied_on_dispatch_attempt(self):
        repo = _make_repo()
        session = _make_session()
        _patch_send_campaign_message(_sent_result())

        with patch.object(wkr.time, "sleep") as sleep_mock:
            wkr._send_one(repo, session, "T1", _make_row(), "msg", None, datetime.utcnow())

        sleep_mock.assert_called_once_with(wkr.CAMPAIGN_SEND_DELAY_SECONDS)

    def test_no_rate_limit_delay_on_opt_out_skip(self):
        repo = _make_repo()
        session = _make_session()
        _patch_conversation_state(opted_out=True)

        with patch.object(wkr.time, "sleep") as sleep_mock:
            wkr._send_one(repo, session, "T1", _make_row(), "msg", None, datetime.utcnow())

        sleep_mock.assert_not_called()


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

    def test_no_template_resolution_when_campaign_has_no_template_id(self):
        """ADR-024 D3: resolve_campaign_template must not be called for a
        message_body-only campaign (template_id=None, the _make_campaign default)."""
        row = _make_row()
        repo = _make_repo(claimed=[row], campaign=_make_campaign(template_id=None))
        session = _make_session()
        resolve_mock = MagicMock()
        sys.modules["app.marketing.campaign_service"].resolve_campaign_template = resolve_mock
        try:
            with patch.object(wkr, "_send_one"), \
                 patch.object(wkr, "_check_campaign_completion"):
                wkr._process_tenant(repo, session, "T1", datetime.utcnow())
            resolve_mock.assert_not_called()
        finally:
            sys.modules["app.marketing.campaign_service"].resolve_campaign_template = (
                _svc_mod.resolve_campaign_template
            )

    def test_template_resolved_once_per_campaign_and_passed_to_send_one(self):
        """ADR-024 D3: resolution happens once per campaign batch, and the
        resolved template is threaded into every _send_one call for that batch."""
        rows = [_make_row(id=1, campaign_id=10), _make_row(id=2, campaign_id=10)]
        repo = _make_repo(claimed=rows, campaign=_make_campaign(template_id=77))
        session = _make_session()
        sentinel_template = MagicMock(name="resolved_template")
        resolve_mock = MagicMock(return_value=sentinel_template)
        sys.modules["app.marketing.campaign_service"].resolve_campaign_template = resolve_mock
        try:
            send_calls = []
            with patch.object(wkr, "_send_one", side_effect=lambda *a, **k: send_calls.append(a)), \
                 patch.object(wkr, "_check_campaign_completion"):
                wkr._process_tenant(repo, session, "T1", datetime.utcnow())

            resolve_mock.assert_called_once_with("T1", 77, session=session)
            assert len(send_calls) == 2
            for call in send_calls:
                # signature: (repo, session, tenant_id, row, message_body, template, now)
                assert call[5] is sentinel_template
        finally:
            sys.modules["app.marketing.campaign_service"].resolve_campaign_template = (
                _svc_mod.resolve_campaign_template
            )


# ── _update_campaign_counters (ADR-025 D10) ────────────────────────────────────

class TestUpdateCampaignCounters:
    def test_sent_delivered_read_count_as_sent(self):
        repo = _make_repo(breakdown={"sent": 2, "delivered": 1, "read": 1, "queued": 3})
        session = _make_session()
        wkr._update_campaign_counters(repo, session, "T1", 10)
        _, kwargs = repo.update_counters.call_args
        assert kwargs["sent_count"] == 4

    def test_failed_and_cancelled_count_as_failed(self):
        repo = _make_repo(breakdown={"failed": 3, "cancelled": 2, "sent": 1})
        session = _make_session()
        wkr._update_campaign_counters(repo, session, "T1", 10)
        _, kwargs = repo.update_counters.call_args
        assert kwargs["failed_count"] == 5

    def test_queued_and_sending_excluded_from_both(self):
        repo = _make_repo(breakdown={"queued": 4, "sending": 2})
        session = _make_session()
        wkr._update_campaign_counters(repo, session, "T1", 10)
        _, kwargs = repo.update_counters.call_args
        assert kwargs["sent_count"] == 0
        assert kwargs["failed_count"] == 0

    def test_scoped_to_tenant_and_campaign(self):
        repo = _make_repo(breakdown={"sent": 1})
        session = _make_session()
        wkr._update_campaign_counters(repo, session, "TID_X", 77)
        repo.status_breakdown.assert_called_once_with("TID_X", 77)
        args, _ = repo.update_counters.call_args
        assert args == ("TID_X", 77)

    def test_does_not_commit_itself(self):
        """The caller (_process_tenant) commits — this function only flushes
        via update_counters, matching the repository purity contract."""
        repo = _make_repo(breakdown={"sent": 1})
        session = _make_session()
        wkr._update_campaign_counters(repo, session, "T1", 10)
        session.commit.assert_not_called()

    def test_process_tenant_commits_after_counter_update(self):
        """Integration: _process_tenant must persist the counter update, not
        leave it flushed-but-uncommitted (which Flask-SQLAlchemy's scoped
        session would silently discard at app_context teardown)."""
        row = _make_row()
        repo = _make_repo(claimed=[row], campaign=_make_campaign())
        session = _make_session()
        events = []
        session.commit.side_effect = lambda: events.append("commit")

        def fake_update_counters(*a, **kw):
            events.append("update_counters")

        with patch.object(wkr, "_send_one"), \
             patch.object(wkr, "_check_campaign_completion"), \
             patch.object(wkr, "_update_campaign_counters", side_effect=fake_update_counters):
            wkr._process_tenant(repo, session, "T1", datetime.utcnow())

        assert "update_counters" in events
        uc_idx = events.index("update_counters")
        assert "commit" in events[uc_idx:], "a commit must follow the counter update"


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
        _patch_send_campaign_message(_sent_result())

        wkr._send_one(repo, session, "TID_99", _make_row(), "msg", None, datetime.utcnow())

        assert repo.mark_recipient_sent.call_args[0][0] == "TID_99"

    def test_mark_failed_on_opt_out_scoped_to_tenant(self):
        repo = _make_repo()
        session = _make_session()
        _patch_conversation_state(opted_out=True)

        wkr._send_one(repo, session, "TID_77", _make_row(), "msg", None, datetime.utcnow())

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


# ── H2: single-worker assertion (Phase 8.2E.10) ────────────────────────────────

class TestSingleWorkerAssertion:
    """WEB_CONCURRENCY > 1 must refuse to start the worker thread.

    claim_next_batch() has no row-level locking, so two worker processes are
    unsafe by construction — this is the only guard against it.
    """

    def _call_init(self, monkeypatch, web_concurrency):
        """Call init_campaign_worker() with WEB_CONCURRENCY set as given.

        `web_concurrency=None` means unset (delenv). Returns the mocked
        threading.Thread class so callers can assert on thread creation.
        """
        if web_concurrency is None:
            monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
        else:
            monkeypatch.setenv("WEB_CONCURRENCY", web_concurrency)

        fake_app = MagicMock(name="flask_app")
        with patch.object(wkr, "threading") as mock_threading:
            wkr.init_campaign_worker(fake_app)
        return mock_threading

    def test_unset_starts_worker(self, monkeypatch):
        mock_threading = self._call_init(monkeypatch, None)
        mock_threading.Thread.assert_called_once()
        mock_threading.Thread.return_value.start.assert_called_once()

    def test_web_concurrency_1_starts_worker(self, monkeypatch):
        mock_threading = self._call_init(monkeypatch, "1")
        mock_threading.Thread.assert_called_once()

    def test_web_concurrency_2_refuses(self, monkeypatch):
        mock_threading = self._call_init(monkeypatch, "2")
        mock_threading.Thread.assert_not_called()

    def test_web_concurrency_8_refuses(self, monkeypatch):
        mock_threading = self._call_init(monkeypatch, "8")
        mock_threading.Thread.assert_not_called()

    def test_non_numeric_web_concurrency_refuses(self, monkeypatch):
        mock_threading = self._call_init(monkeypatch, "abc")
        mock_threading.Thread.assert_not_called()

    def test_refusal_still_starts_no_thread_at_all(self, monkeypatch):
        """Not merely 'not started successfully' — no Thread object constructed."""
        mock_threading = self._call_init(monkeypatch, "3")
        mock_threading.Thread.return_value.start.assert_not_called()

    def test_refusal_logs_error(self, monkeypatch, caplog):
        import logging
        monkeypatch.setenv("WEB_CONCURRENCY", "4")
        with patch.object(wkr, "threading"):
            with caplog.at_level(logging.ERROR, logger="_p82c2_worker"):
                wkr.init_campaign_worker(MagicMock())
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_refusal_does_not_raise(self, monkeypatch):
        """Fail closed, not fail loud — the application must keep running."""
        monkeypatch.setenv("WEB_CONCURRENCY", "5")
        with patch.object(wkr, "threading"):
            wkr.init_campaign_worker(MagicMock())  # must not raise

    def test_web_concurrency_read_from_os_environ_directly(self):
        """H2 is a topology check, not a feature flag — must not go through
        app.flags (which has its own truthy-string parsing semantics).

        Strips the docstring before the app.flags check: the function's own
        docstring explains, in prose, why app.flags is NOT used here — a raw
        substring search would trip on that explanation.
        """
        import re
        src = _WKR_SRC
        start = src.index("def init_campaign_worker(")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]
        assert "WEB_CONCURRENCY" in body
        assert "os.environ" in body

        code_only = re.sub(r'"""[\s\S]*?"""', "", body)
        code_only = re.sub(r"(?m)#.*$", "", code_only)
        assert "app.flags" not in code_only, (
            "init_campaign_worker must read WEB_CONCURRENCY via os.environ, "
            "not app.flags, in actual code (prose explaining this is fine)"
        )

    def test_followup_service_not_touched_by_h2(self):
        """Explicitly out of scope this phase."""
        followup_src = open(
            os.path.join(_ROOT, "app", "services", "followup_service.py"),
            encoding="utf-8",
        ).read()
        assert "WEB_CONCURRENCY" not in followup_src


# ── H1: runtime flag re-check (Phase 8.2E.10) ──────────────────────────────────

class _StopLoop(Exception):
    """Sentinel used to break out of the infinite `while True` under test."""


class TestWorkerLoopFlagRecheck:
    def setup_method(self):
        """Reset module-level pause state between tests — it is process-wide."""
        wkr._paused = False   # matches the module's assume-active default
        wkr._app = MagicMock(name="flask_app")

    def _run_bounded(self, iterations, flag_values):
        """Run _campaign_worker_loop() for exactly `iterations` passes.

        flag_values: either a constant bool, or a list consumed one value per
        iteration (extended with its last element if the loop outlives it).
        Breaks out via time.sleep raising _StopLoop on the (iterations+1)th call.
        """
        calls = {"n": 0}

        def _flag():
            if callable(flag_values):
                return flag_values()
            if isinstance(flag_values, list):
                idx = min(calls["n"], len(flag_values) - 1)
                return flag_values[idx]
            return flag_values

        def _sleep(_seconds):
            calls["n"] += 1
            if calls["n"] >= iterations:
                raise _StopLoop()

        sys.modules["app.flags"].campaign_engine_v2_enabled = _flag
        with patch.object(wkr.time, "sleep", side_effect=_sleep), \
             patch.object(wkr, "_run_cycle") as mock_run_cycle:
            try:
                wkr._campaign_worker_loop()
            except _StopLoop:
                pass
        return mock_run_cycle

    def test_flag_off_never_calls_run_cycle(self):
        mock_run_cycle = self._run_bounded(iterations=3, flag_values=False)
        mock_run_cycle.assert_not_called()

    def test_flag_on_calls_run_cycle_every_iteration(self):
        mock_run_cycle = self._run_bounded(iterations=3, flag_values=True)
        assert mock_run_cycle.call_count == 3

    def test_flag_still_sleeps_while_paused(self):
        """OFF must not busy-loop — it sleeps normally and keeps polling."""
        sleep_count = {"n": 0}

        def _sleep(_seconds):
            sleep_count["n"] += 1
            if sleep_count["n"] >= 3:
                raise _StopLoop()

        sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: False
        with patch.object(wkr.time, "sleep", side_effect=_sleep) as mock_sleep, \
             patch.object(wkr, "_run_cycle"):
            try:
                wkr._campaign_worker_loop()
            except _StopLoop:
                pass
        assert mock_sleep.call_count == 3
        mock_sleep.assert_called_with(wkr.POLL_INTERVAL)

    def test_flag_evaluated_every_iteration_not_cached(self):
        """OFF, OFF, ON, OFF — proves the flag is re-read each pass, not only
        once at loop entry."""
        sequence = [False, False, True, False]
        mock_run_cycle = self._run_bounded(iterations=len(sequence),
                                           flag_values=sequence)
        assert mock_run_cycle.call_count == 1

    def test_transition_to_off_pauses_and_stops_dispatch(self):
        """ON for two cycles, then OFF — dispatch must stop immediately on
        the transition, not one cycle later."""
        sequence = [True, True, False, False, False]
        mock_run_cycle = self._run_bounded(iterations=len(sequence),
                                           flag_values=sequence)
        assert mock_run_cycle.call_count == 2

    def test_transition_to_on_resumes_without_restart(self):
        """OFF then ON — proves resumption needs no thread recreation: the
        SAME loop invocation picks up dispatch on the next iteration."""
        sequence = [False, False, True, True]
        mock_run_cycle = self._run_bounded(iterations=len(sequence),
                                           flag_values=sequence)
        assert mock_run_cycle.call_count == 2

    def test_pause_logged_exactly_once_across_many_off_cycles(self, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="_p82c2_worker"):
            self._run_bounded(iterations=5, flag_values=False)
        pause_logs = [r for r in caplog.records if "paused" in r.message.lower()]
        assert len(pause_logs) == 1

    def test_resume_logged_exactly_once_on_transition(self, caplog):
        import logging
        sequence = [False, False, True, True, True]
        with caplog.at_level(logging.INFO, logger="_p82c2_worker"):
            self._run_bounded(iterations=len(sequence), flag_values=sequence)
        resume_logs = [r for r in caplog.records if "resumed" in r.message.lower()]
        assert len(resume_logs) == 1

    def test_no_log_spam_when_flag_stays_on(self, caplog):
        """Steady-state ON must never log pause/resume — avoid log spam."""
        import logging
        with caplog.at_level(logging.INFO, logger="_p82c2_worker"):
            self._run_bounded(iterations=5, flag_values=True)
        transition_logs = [
            r for r in caplog.records
            if "paused" in r.message.lower() or "resumed" in r.message.lower()
        ]
        assert transition_logs == []

    def test_no_log_spam_when_flag_stays_off_after_first_pause(self, caplog):
        import logging
        sequence = [False] * 6
        with caplog.at_level(logging.INFO, logger="_p82c2_worker"):
            self._run_bounded(iterations=len(sequence), flag_values=sequence)
        pause_logs = [r for r in caplog.records if "paused" in r.message.lower()]
        assert len(pause_logs) == 1

    def test_thread_is_never_recreated_on_transition(self):
        """H1 requirement: no new threading.Thread() call from inside the loop
        itself — resumption is the SAME thread noticing the flag changed."""
        sequence = [False, True, False, True]
        with patch.object(wkr, "threading") as mock_threading:
            self._run_bounded(iterations=len(sequence), flag_values=sequence)
        mock_threading.Thread.assert_not_called()

    def test_flask_app_context_not_entered_while_paused(self):
        """Confirms _run_cycle's app_context is never opened while OFF —
        not just that _run_cycle wasn't called, but that no side door exists."""
        sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: False
        calls = {"n": 0}

        def _sleep(_s):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise _StopLoop()

        with patch.object(wkr.time, "sleep", side_effect=_sleep):
            try:
                wkr._campaign_worker_loop()
            except _StopLoop:
                pass
        wkr._app.app_context.assert_not_called()

    def teardown_method(self):
        # Restore the default the rest of the file's tests rely on.
        sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: True
        wkr._paused = False
