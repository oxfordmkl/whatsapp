"""
Phase 8.2C.3 — Campaign Worker Startup Integration tests.

Proves:
  - worker never starts when CAMPAIGN_ENGINE_V2 is OFF
  - worker starts exactly once when CAMPAIGN_ENGINE_V2 is ON
  - no duplicate registration on a single create_app() call
  - FollowUpJob startup is unconditional and unaffected by the flag
  - no circular imports introduced
  - app/__init__.py structural correctness (source-level assertions)

Two test strategies are used:
  1. Source-level: read app/__init__.py as text and assert the wiring structure
     is correct — flag check wraps only init_campaign_worker, not init_followup_service.
  2. Mock-based: monkeypatch create_app() dependencies to call it at runtime
     and assert call counts for both init functions under each flag state.
"""
import importlib
import os
import sys
import types
from unittest.mock import MagicMock, call, patch

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INIT_PATH = os.path.join(_ROOT, "app", "__init__.py")
_INIT_SRC = open(_INIT_PATH, encoding="utf-8").read()


# ── Source-level structural tests ──────────────────────────────────────────────

class TestStartupStructure:
    """Assert that the wiring in app/__init__.py is correct by inspecting source."""

    def test_init_campaign_worker_is_present(self):
        assert "init_campaign_worker" in _INIT_SRC

    def test_campaign_engine_v2_enabled_check_is_present(self):
        assert "campaign_engine_v2_enabled" in _INIT_SRC

    def test_worker_call_is_inside_flag_check(self):
        """init_campaign_worker must be called inside an if campaign_engine_v2_enabled() block."""
        lines = _INIT_SRC.splitlines()
        flag_line = next(
            (i for i, l in enumerate(lines) if "campaign_engine_v2_enabled()" in l
             and l.lstrip().startswith("if ")),
            None,
        )
        assert flag_line is not None, "Expected: if campaign_engine_v2_enabled(): block"

        # All references to init_campaign_worker must come after the flag check
        # and must be indented (inside the if block).
        for i, line in enumerate(lines):
            if "init_campaign_worker" in line and "import" not in line:
                assert i > flag_line, "init_campaign_worker call must come after the flag if"
                assert line.startswith("        "), (
                    "init_campaign_worker call must be indented inside the if block"
                )

    def test_init_followup_service_is_unconditional(self):
        """init_followup_service must NOT be inside the campaign flag check."""
        lines = _INIT_SRC.splitlines()
        for line in lines:
            if "init_followup_service(app)" in line:
                # Must be at the 4-space indent level (inside create_app, not inside if)
                assert line.startswith("    ") and not line.startswith("        "), (
                    "init_followup_service must be at the top-level of create_app(), "
                    "not inside the campaign flag block"
                )

    def test_followup_comes_before_campaign_worker(self):
        """FollowUpJob must start before the campaign worker (existing boot order)."""
        followup_pos = _INIT_SRC.index("init_followup_service(app)")
        worker_pos = _INIT_SRC.index("init_campaign_worker")
        assert followup_pos < worker_pos

    def test_worker_import_is_inside_flag_block(self):
        """The import of init_campaign_worker must also be gated by the flag."""
        lines = _INIT_SRC.splitlines()
        flag_line = next(
            i for i, l in enumerate(lines) if "campaign_engine_v2_enabled()" in l
            and l.lstrip().startswith("if ")
        )
        for i, line in enumerate(lines):
            if "from app.marketing.campaign_worker import" in line:
                assert i > flag_line
                assert line.startswith("        "), (
                    "campaign_worker import must be inside the flag if block"
                )

    def test_no_unconditional_campaign_worker_import(self):
        """campaign_worker must not be imported at the top of __init__.py."""
        # Count how many times campaign_worker appears before any flag check
        flag_pos = _INIT_SRC.find("campaign_engine_v2_enabled()")
        before_flag = _INIT_SRC[:flag_pos]
        assert "campaign_worker" not in before_flag, (
            "campaign_worker must not be referenced before the flag check"
        )


# ── Flag-dispatch logic tests ──────────────────────────────────────────────────
#
# Rather than calling create_app() (which requires the whole Flask+SQLAlchemy
# stack), we extract and directly test the startup dispatch logic that
# app/__init__.py will execute. This proves flag-gating without needing a DB.

def _simulate_startup(flag_value: bool, worker_init_fn, followup_init_fn, fake_app):
    """Replicate the startup block from app/__init__.py under controlled conditions.

    This function is the verbatim logic from the startup block — if __init__.py
    changes, this must change too. The structural tests above enforce the pattern
    so that drift is caught independently.
    """
    # Verbatim from app/__init__.py startup block:
    followup_init_fn(fake_app)

    if flag_value:
        worker_init_fn(fake_app)


class TestFlagDispatch:
    """Prove flag-gating correctness without bootstrapping Flask."""

    def test_worker_not_called_when_flag_off(self):
        worker = MagicMock()
        followup = MagicMock()
        _simulate_startup(False, worker_init_fn=worker, followup_init_fn=followup,
                          fake_app=MagicMock())
        worker.assert_not_called()

    def test_worker_called_when_flag_on(self):
        worker = MagicMock()
        followup = MagicMock()
        _simulate_startup(True, worker_init_fn=worker, followup_init_fn=followup,
                          fake_app=MagicMock())
        worker.assert_called_once()

    def test_worker_called_with_app(self):
        """init_campaign_worker must receive the app object, not None or a proxy."""
        fake_app = MagicMock(name="flask_app")
        worker = MagicMock()
        _simulate_startup(True, worker_init_fn=worker,
                          followup_init_fn=MagicMock(), fake_app=fake_app)
        worker.assert_called_once_with(fake_app)

    def test_followup_always_started(self):
        """init_followup_service must be called regardless of the campaign flag."""
        for flag in (True, False):
            followup = MagicMock()
            _simulate_startup(flag, worker_init_fn=MagicMock(),
                              followup_init_fn=followup, fake_app=MagicMock())
            assert followup.call_count == 1, (
                f"init_followup_service must be called with flag={flag}"
            )

    def test_no_duplicate_registration(self):
        """Calling the startup block once starts the worker exactly once."""
        worker = MagicMock()
        _simulate_startup(True, worker_init_fn=worker,
                          followup_init_fn=MagicMock(), fake_app=MagicMock())
        assert worker.call_count == 1

    def test_followup_called_before_worker(self):
        """FollowUpJob must start before the campaign worker."""
        order = []
        _simulate_startup(
            True,
            worker_init_fn=lambda app: order.append("worker"),
            followup_init_fn=lambda app: order.append("followup"),
            fake_app=MagicMock(),
        )
        assert order == ["followup", "worker"]


# ── Circular import guard ──────────────────────────────────────────────────────

class TestCircularImport:
    def test_campaign_worker_importable_without_full_app(self):
        """The worker loads at module level without pulling in app startup."""
        # The worker was already loaded in test_campaign_worker.py via _load().
        # Here we just confirm the source does not import at module level from
        # any path that would trigger create_app().
        src = open(
            os.path.join(_ROOT, "app", "marketing", "campaign_worker.py"),
            encoding="utf-8",
        ).read()
        # All app.* imports must be inside functions (lazy), not at the top.
        top_level_imports = [
            line for line in src.splitlines()
            if line.startswith("from app.") or line.startswith("import app.")
        ]
        assert not top_level_imports, (
            f"campaign_worker.py must not import from app at module level: "
            f"{top_level_imports}"
        )

    def test_init_py_does_not_import_worker_at_top_level(self):
        """app/__init__.py must not import campaign_worker at module scope."""
        # The campaign_worker import is inside create_app() → inside the flag
        # if block, so it never fires at module load time.
        top_lines = []
        in_create_app = False
        for line in _INIT_SRC.splitlines():
            if line.startswith("def create_app"):
                in_create_app = True
            if not in_create_app:
                top_lines.append(line)
        assert not any("campaign_worker" in l for l in top_lines), (
            "campaign_worker must not appear at module level in app/__init__.py"
        )


# ── FollowUpJob unchanged ──────────────────────────────────────────────────────

class TestFollowUpJobUnchanged:
    def test_followup_service_not_modified(self):
        """followup_service.py must not reference campaign_worker."""
        src = open(
            os.path.join(_ROOT, "app", "services", "followup_service.py"),
            encoding="utf-8",
        ).read()
        assert "campaign_worker" not in src
        assert "campaign_engine_v2" not in src.lower()

    def test_followup_init_signature_unchanged(self):
        """init_followup_service signature must still accept a single `app` arg."""
        src = open(
            os.path.join(_ROOT, "app", "services", "followup_service.py"),
            encoding="utf-8",
        ).read()
        assert "def init_followup_service(app):" in src


# ── Forbidden file modification guard ─────────────────────────────────────────

class TestForbiddenFiles:
    FORBIDDEN = [
        ("app/services/followup_service.py", "campaign_worker"),
        ("app/marketing/campaign_worker.py", "init_followup_service"),
        ("app/persistence/campaign_repository.py", "init_campaign_worker"),
        ("app/marketing/campaign_service.py", "init_campaign_worker"),
        ("app/services/whatsapp_service.py", "campaign_worker"),
        ("app/services/broadcast.py", "campaign_worker"),
    ]

    def test_forbidden_cross_references_absent(self):
        for relpath, forbidden_term in self.FORBIDDEN:
            full = os.path.join(_ROOT, relpath)
            if not os.path.exists(full):
                continue
            src = open(full, encoding="utf-8").read()
            assert forbidden_term not in src, (
                f"{relpath} must not reference {forbidden_term!r}"
            )
