"""feat: Phase 16.5A5-H1 Enterprise Baseline v1.1 hotfix

Revision ID: b6e1d4f82c9e
Revises: a5f0c3e91b7d
Create Date: 2026-07-16 14:00:00.000000

Enterprise Baseline v1.1 — approved in Phase K2.1 governance review.

Changes in this migration (additive only — zero data loss):

  ALTER TABLE conversation_state
    ALTER COLUMN custom_attributes  TEXT  →  JSON
      (all existing rows are NULL — no data cast required)

  ALTER TABLE offering
    ADD COLUMN price               Numeric(12,2)  nullable
    ADD COLUMN custom_attributes   JSON           nullable

Governance:
  ADR-013 — JSON Column Standard    (db.JSON replaces db.Text for structured data)
  ADR-014 — Reserved ORM Attribute  (custom_attributes replaces metadata)

Safety notes:
  • conversation_state.custom_attributes was added in revision a5f0c3e91b7d
    as nullable with no backfill. All production rows are NULL. The type
    change TEXT → JSON therefore requires no USING-clause data conversion.
  • offering is empty on production (no backfill has run). ADD COLUMN on an
    empty table is instantaneous with no lock risk.
  • Downgrade restores all columns to their prior types/absence.
  • No DROP, DELETE, TRUNCATE, or destructive operation of any kind.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b6e1d4f82c9e'
down_revision = 'a5f0c3e91b7d'
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. ALTER conversation_state.custom_attributes: Text → JSON ──────────
    # All rows have NULL in this column (added nullable in a5f0c3e91b7d with no
    # backfill). On PostgreSQL batch mode emits:
    #   ALTER TABLE conversation_state ALTER COLUMN custom_attributes TYPE json
    #   USING custom_attributes::json
    # The USING clause is a no-op for NULL values — zero lock risk.
    # On SQLite, batch_alter_table recreates the table with TEXT storage
    # (SQLite has no native JSON type; SQLAlchemy JSON uses TEXT on SQLite).
    with op.batch_alter_table('conversation_state', schema=None) as batch_op:
        batch_op.alter_column('custom_attributes',
                              existing_type=sa.Text(),
                              type_=sa.JSON(),
                              existing_nullable=True,
                              postgresql_using='custom_attributes::json')

    # ── 2. ALTER offering: add price + custom_attributes ────────────────────
    # The offering table is empty — ADD COLUMN is instantaneous.
    with op.batch_alter_table('offering', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('price', sa.Numeric(precision=12, scale=2), nullable=True)
        )
        batch_op.add_column(
            sa.Column('custom_attributes', sa.JSON(), nullable=True)
        )


def downgrade():
    # ── Revert offering additions ────────────────────────────────────────────
    with op.batch_alter_table('offering', schema=None) as batch_op:
        batch_op.drop_column('custom_attributes')
        batch_op.drop_column('price')

    # ── Revert conversation_state.custom_attributes: JSON → Text ────────────
    with op.batch_alter_table('conversation_state', schema=None) as batch_op:
        batch_op.alter_column('custom_attributes',
                              existing_type=sa.JSON(),
                              type_=sa.Text(),
                              existing_nullable=True,
                              postgresql_using='custom_attributes::text')
