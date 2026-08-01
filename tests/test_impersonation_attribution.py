"""
Phase 8.2E.6B — Impersonation attribution tests (ADR-023 D3 requirement 1).

Covers:
  - Campaign model has impersonated_by column (nullable String(120)).
  - Campaign repository create_campaign accepts and forwards impersonated_by.
  - CampaignService.create_campaign accepts and threads impersonated_by through
    to the repository — service never reads Flask session itself.
  - Route resolves impersonated_by from session and passes it to the service.
  - Route passes None when not impersonating.
  - Migration file has correct structure: revision, down_revision, symmetric
    upgrade/downgrade, no backfill.

Scope note: per-transition attribution is out of scope (Option A, create-time
only). The append-only audit_log carries per-transition evidence via
IMPERSONATION_START/END (Phase 8.2E.6A).
"""
import importlib.util
import os
import re
import sys
import types
from unittest.mock import MagicMock, call, patch

import pytest

_ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODELS_PATH  = os.path.join(_ROOT, "app", "models.py")
_REPO_PATH    = os.path.join(_ROOT, "app", "persistence", "campaign_repository.py")
_SVC_PATH     = os.path.join(_ROOT, "app", "marketing", "campaign_service.py")
_MKT_PATH     = os.path.join(_ROOT, "app", "routes", "marketing.py")
_MIG_PATH     = os.path.join(_ROOT, "migrations", "versions",
                              "a8d3f2e91c05_phase_8_2e_6b_campaign_impersonated_by.py")

with open(_MODELS_PATH, encoding="utf-8") as _fh:
    _MODELS_SRC = _fh.read()

with open(_SVC_PATH, encoding="utf-8") as _fh:
    _SVC_SRC = _fh.read()

with open(_MKT_PATH, encoding="utf-8") as _fh:
    _MKT_SRC = _fh.read()

with open(_MIG_PATH, encoding="utf-8") as _fh:
    _MIG_SRC = _fh.read()


# ── Model ─────────────────────────────────────────────────────────────────────

def test_model_has_impersonated_by_column():
    assert "impersonated_by" in _MODELS_SRC


def test_model_impersonated_by_is_nullable():
    """The column declaration must include nullable=True."""
    # Locate the impersonated_by line and verify nullable=True is present.
    for line in _MODELS_SRC.splitlines():
        if "impersonated_by" in line and "Column" in line:
            assert "nullable=True" in line, (
                "impersonated_by column must be nullable=True"
            )
            return
    pytest.fail("impersonated_by column declaration not found in models.py")


def test_model_impersonated_by_is_string120():
    for line in _MODELS_SRC.splitlines():
        if "impersonated_by" in line and "Column" in line:
            assert "String(120)" in line or "String(length=120)" in line, (
                "impersonated_by must be String(120) to mirror created_by"
            )
            return
    pytest.fail("impersonated_by column declaration not found in models.py")


def test_model_impersonated_by_after_created_by():
    """impersonated_by must sit in the provenance block alongside created_by."""
    created_idx = _MODELS_SRC.find("created_by")
    imp_idx = _MODELS_SRC.find("impersonated_by")
    assert created_idx != -1
    assert imp_idx != -1
    # Both appear within the Campaign class — created_by first is the natural order.
    assert created_idx < imp_idx


# ── Repository ────────────────────────────────────────────────────────────────

def _load_repo():
    """Load campaign_repository via spec_from_file_location with stubs."""
    stubs = {
        "app": types.ModuleType("app"),
        "app.models": types.ModuleType("app.models"),
        "app.extensions": types.ModuleType("app.extensions"),
    }
    stubs["app.extensions"].db = MagicMock()
    for k, v in stubs.items():
        sys.modules.setdefault(k, v)

    spec = importlib.util.spec_from_file_location("_test_campaign_repo", _REPO_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_repository_create_campaign_accepts_impersonated_by():
    """Signature must include impersonated_by=None keyword argument."""
    import inspect
    repo_mod = _load_repo()
    sig = inspect.signature(repo_mod.CampaignRepository.create_campaign)
    assert "impersonated_by" in sig.parameters, (
        "CampaignRepository.create_campaign must accept impersonated_by kwarg"
    )
    assert sig.parameters["impersonated_by"].default is None


def test_repository_create_campaign_forwards_impersonated_by():
    """The repository must pass impersonated_by to the Campaign constructor."""
    repo_mod = _load_repo()
    campaign_cls = MagicMock()
    campaign_instance = MagicMock()
    campaign_cls.return_value = campaign_instance

    session = MagicMock()
    # Constructor uses positional names: session, campaign_model, recipient_model
    repo = repo_mod.CampaignRepository(
        session=session,
        campaign_model=campaign_cls,
        recipient_model=MagicMock(),
    )

    repo.create_campaign(
        "tenant-1", "My Campaign",
        created_by="admin@test.com",
        impersonated_by="super@platform.test",
    )

    ctor_kwargs = campaign_cls.call_args.kwargs
    assert ctor_kwargs.get("impersonated_by") == "super@platform.test", (
        "repository must forward impersonated_by to Campaign constructor"
    )


def test_repository_create_campaign_none_impersonated_by():
    """impersonated_by=None (no impersonation) must reach the constructor."""
    repo_mod = _load_repo()
    campaign_cls = MagicMock()
    session = MagicMock()
    repo = repo_mod.CampaignRepository(
        session=session,
        campaign_model=campaign_cls,
        recipient_model=MagicMock(),
    )

    repo.create_campaign("tenant-1", "Draft", impersonated_by=None)

    ctor_kwargs = campaign_cls.call_args.kwargs
    assert "impersonated_by" in ctor_kwargs
    assert ctor_kwargs["impersonated_by"] is None


# ── Service ───────────────────────────────────────────────────────────────────

def test_service_create_campaign_signature_has_impersonated_by():
    assert re.search(r"def create_campaign\(.*impersonated_by", _SVC_SRC, re.DOTALL), (
        "CampaignService.create_campaign must declare impersonated_by parameter"
    )


def test_service_threads_impersonated_by_to_repository():
    """service.create_campaign must pass impersonated_by to repository.create_campaign."""
    assert re.search(
        r"repository\.create_campaign\(.*impersonated_by\s*=\s*impersonated_by",
        _SVC_SRC,
        re.DOTALL,
    ), (
        "CampaignService must forward impersonated_by to repository.create_campaign"
    )


def test_service_does_not_import_flask_session():
    """CampaignService must never import Flask session — that belongs in routes.

    Note: self.session in CampaignService refers to the SQLAlchemy db.session
    (a positional attribute), NOT the Flask request-context session. This test
    checks that 'session' never appears in a Flask import line.
    """
    for line in _SVC_SRC.splitlines():
        stripped = line.strip()
        if stripped.startswith("from flask import") or stripped.startswith("import flask"):
            assert "session" not in stripped, (
                f"CampaignService must not import Flask session: {stripped!r}\n"
                "Flask session access must live in the route layer only (ADR-023 D3)"
            )


# ── Route ─────────────────────────────────────────────────────────────────────

def test_route_resolves_impersonated_by_from_session():
    """Route source must read impersonate_tenant_id from session."""
    assert "impersonate_tenant_id" in _MKT_SRC, (
        "create_campaign route must check session['impersonate_tenant_id']"
    )
    assert "impersonated_by" in _MKT_SRC, (
        "create_campaign route must resolve and forward impersonated_by"
    )


def test_route_passes_impersonated_by_to_service():
    """Route must pass impersonated_by= kwarg to svc.create_campaign()."""
    assert re.search(
        r"svc\.create_campaign\(.*impersonated_by\s*=\s*impersonated_by",
        _MKT_SRC,
        re.DOTALL,
    ), (
        "create_campaign route must forward impersonated_by to the service"
    )


def test_route_impersonated_by_none_when_not_impersonating():
    """Route must use _resolve_impersonated_by helper which uses session.get()."""
    # The helper exists and uses session.get so a missing key yields None.
    assert "_resolve_impersonated_by" in _MKT_SRC, (
        "create_campaign route must use _resolve_impersonated_by helper"
    )
    # Helper body must guard on session.get so absence → None.
    assert "session.get(\"impersonate_tenant_id\")" in _MKT_SRC or \
           "session.get('impersonate_tenant_id')" in _MKT_SRC, (
        "_resolve_impersonated_by must use session.get(...) so missing key yields None"
    )


def _extract_function_source(name: str, src: str) -> str:
    lines = src.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"def {name}("):
            start = i
            break
    assert start is not None, f"{name} not found in marketing.py"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("@") or lines[j].startswith("def "):
            end = j
            break
    return "\n".join(lines[start:end])


# ── Migration ─────────────────────────────────────────────────────────────────

def test_migration_revision_is_correct():
    assert "revision = 'a8d3f2e91c05'" in _MIG_SRC


def test_migration_down_revision_is_campaign_foundation():
    """Must descend from the campaign foundation migration (current campaigns head)."""
    assert "down_revision = 'f1c8d3a76b40'" in _MIG_SRC, (
        "migration must descend from f1c8d3a76b40 (Phase 8.1B campaign foundation)"
    )


def test_migration_upgrade_adds_impersonated_by():
    assert "add_column" in _MIG_SRC
    assert "impersonated_by" in _MIG_SRC


def test_migration_downgrade_drops_impersonated_by():
    assert "drop_column" in _MIG_SRC
    assert re.search(r"def downgrade\(\)", _MIG_SRC), "migration must have downgrade()"


def test_migration_no_backfill():
    """No UPDATE statement — NULL is the correct value for existing rows."""
    assert "UPDATE" not in _MIG_SRC.upper() or "-- No backfill" in _MIG_SRC, (
        "migration must not backfill impersonated_by (NULL is semantically correct)"
    )


def test_migration_column_is_nullable():
    assert "nullable=True" in _MIG_SRC, (
        "migration column must be nullable=True — existing rows get NULL"
    )


def test_migration_column_is_string120():
    assert "String(length=120)" in _MIG_SRC or "String(120)" in _MIG_SRC


def test_migration_uses_batch_alter_table():
    """Must use batch_alter_table style to match repo conventions."""
    assert "batch_alter_table" in _MIG_SRC
