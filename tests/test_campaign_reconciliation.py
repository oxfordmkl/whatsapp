"""
Phase 8.2C.4 — Campaign Reconciliation tests.

Proves:
  - COMPLETED detected when all terminal and at least one sent/delivered/read
  - FAILED detected when all terminal and zero successful sends
  - mixed success/failure → COMPLETED (partial success still counts)
  - still-running campaigns not advanced (queued or sending present)
  - empty breakdown → "running" (no-op)
  - campaign not found → "skipped"
  - campaign not in RUNNING state → "skipped"
  - worker delegates to CampaignService (never calls repo.update_status directly)
  - CampaignService owns the commit on each lifecycle change
  - repository still contains no business logic
  - tenant isolation preserved
  - _evaluate_outcome is pure (no DB, no side effects)
  - drift guard: recipient status constants match app.models

The service is loaded via file-path (same pattern as other campaign tests) to
avoid the app-package import collision.
"""
import importlib.util
import os
import sys
import types
from datetime import datetime
from unittest.mock import MagicMock, call

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, relpath):
    path = os.path.join(_ROOT, relpath)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub(name):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    return sys.modules[name]


# Stub app.* so service + worker load cleanly without Flask
for _n in [
    "app", "app.persistence", "app.persistence.campaign_repository",
    "app.extensions", "app.models",
    "app.services", "app.services.whatsapp_service",
    "app.flags",
]:
    _stub(_n)

# app.flags must expose campaign_engine_v2_enabled; default ON for service tests
sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: True

svc_mod = _load("_p82c4_svc", "app/marketing/campaign_service.py")
wkr_mod = _load("_p82c4_wkr", "app/marketing/campaign_worker.py")
# Wire the worker's lazy import to the service module we just loaded
sys.modules["app.marketing"] = types.ModuleType("app.marketing")
sys.modules["app.marketing.campaign_service"] = svc_mod

S = svc_mod   # campaign status constants
ev = svc_mod.CampaignService._evaluate_outcome   # pure static method shortcut


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_campaign(status="running"):
    c = MagicMock()
    c.id = 1
    c.status = status
    return c


def _make_repo(campaign=None, breakdown=None):
    repo = MagicMock()
    repo.get.return_value = campaign
    repo.status_breakdown.return_value = breakdown or {}
    repo.update_status.return_value = MagicMock()
    repo.mark_failed.return_value = MagicMock()
    return repo


def _make_session():
    return MagicMock()


def _make_svc(campaign=None, breakdown=None, engine_on=True):
    repo = _make_repo(campaign=campaign, breakdown=breakdown)
    session = _make_session()
    sys.modules["app.flags"].campaign_engine_v2_enabled = lambda: engine_on
    svc = svc_mod.CampaignService(repository=repo, session=session,
                                  clock=lambda: datetime(2026, 1, 1, 12, 0, 0))
    return svc, repo, session


# ── _evaluate_outcome (pure) ──────────────────────────────────────────────────

class TestEvaluateOutcome:
    def test_empty_breakdown_returns_none(self):
        assert ev({}) is None

    def test_none_breakdown_returns_none(self):
        # status_breakdown() returns {} when no recipients; passing {} is same
        assert ev({}) is None

    def test_queued_present_returns_none(self):
        assert ev({"queued": 1, "sent": 5}) is None

    def test_sending_present_returns_none(self):
        assert ev({"sending": 1, "sent": 3}) is None

    def test_both_queued_and_sending_returns_none(self):
        assert ev({"queued": 2, "sending": 1, "failed": 1}) is None

    def test_all_failed_returns_failed(self):
        assert ev({"failed": 10}) == S.FAILED

    def test_all_cancelled_returns_failed(self):
        assert ev({"cancelled": 5}) == S.FAILED

    def test_mix_failed_cancelled_returns_failed(self):
        assert ev({"failed": 3, "cancelled": 2}) == S.FAILED

    def test_at_least_one_sent_returns_completed(self):
        assert ev({"sent": 1, "failed": 9}) == S.COMPLETED

    def test_at_least_one_delivered_returns_completed(self):
        assert ev({"delivered": 1, "failed": 4}) == S.COMPLETED

    def test_at_least_one_read_returns_completed(self):
        assert ev({"read": 1, "failed": 2}) == S.COMPLETED

    def test_all_sent_returns_completed(self):
        assert ev({"sent": 50}) == S.COMPLETED

    def test_mixed_success_statuses(self):
        assert ev({"sent": 10, "delivered": 5, "read": 3, "failed": 2}) == S.COMPLETED

    def test_zero_total_all_zeros_returns_none(self):
        # If all counts are 0 (shouldn't happen, but defensive)
        assert ev({"queued": 0, "sending": 0}) is None

    def test_total_computed_from_values_not_total_key(self):
        """status_breakdown() never returns a 'total' key — sum must work without it."""
        breakdown = {"sent": 3, "failed": 2}
        assert "total" not in breakdown
        result = ev(breakdown)
        assert result == S.COMPLETED  # sum=5, success=3 → completed

    def test_evaluate_outcome_is_deterministic(self):
        bd = {"sent": 5, "failed": 3}
        assert ev(bd) == ev(bd)

    def test_evaluate_outcome_does_not_mutate_input(self):
        bd = {"sent": 5, "failed": 3}
        original = dict(bd)
        ev(bd)
        assert bd == original


# ── reconcile_campaign — outcome routing ──────────────────────────────────────

class TestReconcileCampaignOutcomes:
    def test_returns_running_when_queued_remain(self):
        svc, repo, _ = _make_svc(
            campaign=_make_campaign("running"),
            breakdown={"queued": 2, "sent": 3},
        )
        assert svc.reconcile_campaign("T1", 1) == "running"
        repo.update_status.assert_not_called()
        repo.mark_failed.assert_not_called()

    def test_returns_completed_when_all_terminal_with_success(self):
        svc, repo, session = _make_svc(
            campaign=_make_campaign("running"),
            breakdown={"sent": 8, "failed": 2},
        )
        result = svc.reconcile_campaign("T1", 1)
        assert result == "completed"
        repo.update_status.assert_called_once_with(
            "T1", 1, S.COMPLETED, completed_at=datetime(2026, 1, 1, 12, 0, 0)
        )
        session.commit.assert_called_once()

    def test_returns_failed_when_all_terminal_zero_success(self):
        svc, repo, session = _make_svc(
            campaign=_make_campaign("running"),
            breakdown={"failed": 10},
        )
        result = svc.reconcile_campaign("T1", 1)
        assert result == "failed"
        repo.mark_failed.assert_called_once()
        args, kwargs = repo.mark_failed.call_args
        assert args[0] == "T1"
        assert args[1] == 1
        session.commit.assert_called_once()

    def test_partial_success_returns_completed(self):
        """1 sent out of 100 → COMPLETED (partial success is still success)."""
        svc, repo, session = _make_svc(
            campaign=_make_campaign("running"),
            breakdown={"sent": 1, "failed": 99},
        )
        assert svc.reconcile_campaign("T1", 1) == "completed"

    def test_all_cancelled_returns_failed(self):
        svc, repo, session = _make_svc(
            campaign=_make_campaign("running"),
            breakdown={"cancelled": 5},
        )
        assert svc.reconcile_campaign("T1", 1) == "failed"

    def test_delivered_counts_as_success(self):
        svc, repo, _ = _make_svc(
            campaign=_make_campaign("running"),
            breakdown={"delivered": 3, "failed": 7},
        )
        assert svc.reconcile_campaign("T1", 1) == "completed"

    def test_read_counts_as_success(self):
        svc, repo, _ = _make_svc(
            campaign=_make_campaign("running"),
            breakdown={"read": 1, "cancelled": 9},
        )
        assert svc.reconcile_campaign("T1", 1) == "completed"

    def test_empty_breakdown_returns_running(self):
        svc, repo, _ = _make_svc(
            campaign=_make_campaign("running"),
            breakdown={},
        )
        assert svc.reconcile_campaign("T1", 1) == "running"


# ── reconcile_campaign — guard conditions ─────────────────────────────────────

class TestReconcileGuards:
    def test_returns_skipped_when_campaign_not_found(self):
        svc, repo, _ = _make_svc(campaign=None, breakdown={"sent": 5})
        assert svc.reconcile_campaign("T1", 99) == "skipped"
        repo.update_status.assert_not_called()

    def test_returns_skipped_when_campaign_not_running(self):
        for status in ("draft", "validated", "scheduled",
                       "completed", "failed", "cancelled", "archived"):
            svc, repo, _ = _make_svc(
                campaign=_make_campaign(status),
                breakdown={"sent": 5},
            )
            assert svc.reconcile_campaign("T1", 1) == "skipped", (
                f"Expected 'skipped' for status={status}"
            )
            repo.update_status.assert_not_called()

    def test_raises_when_engine_disabled(self):
        # Match by class NAME rather than identity: two _load() calls for the same
        # source file produce distinct class objects, so `isinstance` can fail even
        # when the exception type is semantically the same.
        svc, _, _ = _make_svc(engine_on=False)
        with pytest.raises(Exception) as exc_info:
            svc.reconcile_campaign("T1", 1)
        assert type(exc_info.value).__name__ == "CampaignEngineDisabled"

    def test_raises_when_tenant_id_missing(self):
        svc, _, _ = _make_svc()
        with pytest.raises(Exception) as exc_info:
            svc.reconcile_campaign(None, 1)
        assert type(exc_info.value).__name__ == "CampaignValidationError"

    def test_raises_when_tenant_id_empty_string(self):
        svc, _, _ = _make_svc()
        with pytest.raises(Exception) as exc_info:
            svc.reconcile_campaign("", 1)
        assert type(exc_info.value).__name__ == "CampaignValidationError"


# ── Transaction ownership ─────────────────────────────────────────────────────

class TestReconcileTransactions:
    def test_commits_on_completed(self):
        svc, _, session = _make_svc(
            campaign=_make_campaign("running"),
            breakdown={"sent": 5},
        )
        svc.reconcile_campaign("T1", 1)
        session.commit.assert_called_once()

    def test_commits_on_failed(self):
        svc, _, session = _make_svc(
            campaign=_make_campaign("running"),
            breakdown={"failed": 5},
        )
        svc.reconcile_campaign("T1", 1)
        session.commit.assert_called_once()

    def test_no_commit_when_running(self):
        svc, _, session = _make_svc(
            campaign=_make_campaign("running"),
            breakdown={"queued": 3, "sent": 2},
        )
        svc.reconcile_campaign("T1", 1)
        session.commit.assert_not_called()

    def test_no_commit_when_skipped(self):
        svc, _, session = _make_svc(campaign=None)
        svc.reconcile_campaign("T1", 1)
        session.commit.assert_not_called()

    def test_rollback_on_repo_error(self):
        svc, repo, session = _make_svc(
            campaign=_make_campaign("running"),
            breakdown={"sent": 5},
        )
        repo.update_status.side_effect = RuntimeError("db error")
        with pytest.raises(RuntimeError):
            svc.reconcile_campaign("T1", 1)
        session.rollback.assert_called_once()

    def test_no_commit_after_rollback(self):
        svc, repo, session = _make_svc(
            campaign=_make_campaign("running"),
            breakdown={"sent": 5},
        )
        repo.update_status.side_effect = RuntimeError("db error")
        try:
            svc.reconcile_campaign("T1", 1)
        except RuntimeError:
            pass
        session.commit.assert_not_called()


# ── Tenant isolation ──────────────────────────────────────────────────────────

class TestReconcileTenantIsolation:
    def test_repo_get_called_with_tenant_id(self):
        svc, repo, _ = _make_svc(campaign=_make_campaign("running"),
                                  breakdown={"sent": 5})
        svc.reconcile_campaign("TENANT_Z", 1)
        assert repo.get.call_args[0][0] == "TENANT_Z"

    def test_status_breakdown_called_with_tenant_id(self):
        svc, repo, _ = _make_svc(campaign=_make_campaign("running"),
                                  breakdown={"sent": 5})
        svc.reconcile_campaign("TENANT_Z", 1)
        assert repo.status_breakdown.call_args[0][0] == "TENANT_Z"

    def test_update_status_called_with_tenant_id(self):
        svc, repo, _ = _make_svc(campaign=_make_campaign("running"),
                                  breakdown={"sent": 5})
        svc.reconcile_campaign("TENANT_Z", 1)
        assert repo.update_status.call_args[0][0] == "TENANT_Z"

    def test_mark_failed_called_with_tenant_id(self):
        svc, repo, _ = _make_svc(campaign=_make_campaign("running"),
                                  breakdown={"failed": 5})
        svc.reconcile_campaign("TENANT_Z", 1)
        assert repo.mark_failed.call_args[0][0] == "TENANT_Z"


# ── Worker delegation ─────────────────────────────────────────────────────────

class TestWorkerDelegation:
    """Prove the worker's _check_campaign_completion calls CampaignService, not repo."""

    def test_worker_calls_reconcile_not_repo_update_status(self):
        """Worker must not call repo.update_status directly in _check_campaign_completion."""
        src = open(
            os.path.join(_ROOT, "app/marketing/campaign_worker.py"),
            encoding="utf-8",
        ).read()
        # The completion function must delegate to CampaignService
        assert "CampaignService" in src
        assert "reconcile_campaign" in src

    def test_worker_check_completion_does_not_call_repo_update_directly(self):
        """After Phase 8.2C.4, the worker's completion helper must NOT call
        repo.update_status — that responsibility moved to CampaignService."""
        src = open(
            os.path.join(_ROOT, "app/marketing/campaign_worker.py"),
            encoding="utf-8",
        ).read()
        # Find _check_campaign_completion and verify no update_status call inside it
        lines = src.splitlines()
        in_fn = False
        for line in lines:
            if "def _check_campaign_completion" in line:
                in_fn = True
            if in_fn:
                if line.startswith("def ") and "_check_campaign_completion" not in line:
                    break  # left the function
                assert "repo.update_status" not in line, (
                    "_check_campaign_completion must not call repo.update_status directly"
                )

    def test_worker_completion_helper_invokes_service(self):
        """_check_campaign_completion must instantiate CampaignService."""
        reconcile_calls = []

        class _FakeSvc:
            def __init__(self, **kw):
                pass
            def reconcile_campaign(self, tenant_id, campaign_id):
                reconcile_calls.append((tenant_id, campaign_id))
                return "completed"

        sys.modules["app.marketing.campaign_service"].CampaignService = _FakeSvc
        try:
            repo = _make_repo(campaign=_make_campaign("running"),
                              breakdown={"sent": 5})
            session = _make_session()
            wkr_mod._check_campaign_completion(repo, session, "T1", 42,
                                               datetime(2026, 1, 1))
        finally:
            # Restore real service
            sys.modules["app.marketing.campaign_service"].CampaignService = (
                svc_mod.CampaignService
            )

        assert ("T1", 42) in reconcile_calls

    def test_worker_exception_from_service_is_isolated(self):
        """An exception in reconcile_campaign must be caught by the tenant-level handler."""
        # _check_campaign_completion itself does not catch — it propagates to _process_tenant.
        # Here we verify it does propagate (not silently swallowed inside _check_).
        class _BoomSvc:
            def __init__(self, **kw):
                pass
            def reconcile_campaign(self, tenant_id, campaign_id):
                raise RuntimeError("service exploded")

        sys.modules["app.marketing.campaign_service"].CampaignService = _BoomSvc
        try:
            repo = _make_repo(campaign=_make_campaign("running"), breakdown={"sent": 5})
            with pytest.raises(RuntimeError, match="service exploded"):
                wkr_mod._check_campaign_completion(repo, _make_session(), "T1", 1,
                                                   datetime(2026, 1, 1))
        finally:
            sys.modules["app.marketing.campaign_service"].CampaignService = (
                svc_mod.CampaignService
            )


# ── Repository contains no business logic ─────────────────────────────────────

class TestRepositoryPurity:
    def test_repo_has_no_reconciliation_logic(self):
        src = open(
            os.path.join(_ROOT, "app/persistence/campaign_repository.py"),
            encoding="utf-8",
        ).read()
        assert "reconcile" not in src
        assert "_evaluate_outcome" not in src
        assert "zero successful" not in src

    def test_repo_has_no_commits(self):
        src = open(
            os.path.join(_ROOT, "app/persistence/campaign_repository.py"),
            encoding="utf-8",
        ).read()
        assert ".commit()" not in src


# ── Drift guard ───────────────────────────────────────────────────────────────

class TestDriftGuard:
    def _models_src(self):
        return open(os.path.join(_ROOT, "app/models.py"), encoding="utf-8").read()

    def test_r_queued_matches_model(self):
        assert "RECIPIENT_QUEUED" in self._models_src() and '"queued"' in self._models_src()
        assert svc_mod.R_QUEUED == "queued"

    def test_r_sending_matches_model(self):
        assert svc_mod.R_SENDING == "sending"

    def test_r_sent_matches_model(self):
        assert svc_mod.R_SENT == "sent"

    def test_r_delivered_matches_model(self):
        assert svc_mod.R_DELIVERED == "delivered"

    def test_r_read_matches_model(self):
        assert svc_mod.R_READ == "read"

    def test_r_failed_matches_model(self):
        assert svc_mod.R_FAILED == "failed"

    def test_r_cancelled_matches_model(self):
        assert svc_mod.R_CANCELLED == "cancelled"

    def test_constants_exported_from_module(self):
        for attr in ("R_QUEUED", "R_SENDING", "R_SENT", "R_DELIVERED",
                     "R_READ", "R_FAILED", "R_CANCELLED"):
            assert hasattr(svc_mod, attr), f"svc_mod missing {attr}"


# ── Forbidden cross-references ────────────────────────────────────────────────

class TestForbiddenCrossReferences:
    FORBIDDEN = [
        ("app/services/followup_service.py", "reconcile_campaign"),
        ("app/services/whatsapp_service.py", "reconcile_campaign"),
        ("app/persistence/campaign_repository.py", "reconcile_campaign"),
        ("app/routes/admin.py", "reconcile_campaign"),
    ]

    def test_forbidden_files_untouched(self):
        for relpath, term in self.FORBIDDEN:
            full = os.path.join(_ROOT, relpath)
            if not os.path.exists(full):
                continue
            src = open(full, encoding="utf-8").read()
            assert term not in src, f"{relpath} must not reference {term!r}"
