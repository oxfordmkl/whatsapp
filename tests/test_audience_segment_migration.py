"""
Phase 8.2E.9-C — audience_segment migration structure tests (ADR-025 D8).

Source-level checks only, matching test_impersonation_attribution.py's
migration test pattern — no Alembic runtime in this repo's test harness.
"""
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MIG_PATH = os.path.join(
    _ROOT, "migrations", "versions",
    "c3f7e2a41d09_phase_8_2e_9c_campaign_audience_segment.py",
)

with open(_MIG_PATH, encoding="utf-8") as _fh:
    _MIG_SRC = _fh.read()


def test_migration_revision_is_correct():
    assert "revision = 'c3f7e2a41d09'" in _MIG_SRC


def test_migration_down_revision_is_impersonated_by_migration():
    """Must descend from a8d3f2e91c05 — the current campaigns head."""
    assert "down_revision = 'a8d3f2e91c05'" in _MIG_SRC


def test_migration_upgrade_adds_audience_segment():
    assert "add_column" in _MIG_SRC
    assert "audience_segment" in _MIG_SRC


def test_migration_downgrade_drops_audience_segment():
    assert "drop_column" in _MIG_SRC
    assert "def downgrade()" in _MIG_SRC


def test_migration_no_backfill():
    assert "UPDATE" not in _MIG_SRC.upper()


def test_migration_column_is_nullable():
    assert "nullable=True" in _MIG_SRC


def test_migration_column_is_string100():
    assert "String(length=100)" in _MIG_SRC or "String(100)" in _MIG_SRC


def test_migration_uses_batch_alter_table():
    assert "batch_alter_table" in _MIG_SRC
