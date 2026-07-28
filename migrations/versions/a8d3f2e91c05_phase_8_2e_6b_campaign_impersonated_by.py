"""phase_8_2e_6b_campaign_impersonated_by

Revision ID: a8d3f2e91c05
Revises: f1c8d3a76b40
Create Date: 2026-07-25

ADR-023 D3 (Phase 8.2E.6B): add nullable impersonated_by column to campaigns.

Records the SUPER_ADMIN identity when a campaign was created while the operator
was impersonating a tenant. NULL = campaign was not created under impersonation
(or predates this feature). No backfill is required; NULL is semantically
correct for all existing rows.

Per-transition attribution (lifecycle status changes under impersonation) is
tracked via the append-only audit_log (IMPERSONATION_START/END events added in
Phase 8.2E.6A) — not here. This column covers create-time only (option A,
per discovery-audit approval).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a8d3f2e91c05'
down_revision = 'f1c8d3a76b40'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('campaigns', schema=None) as batch_op:
        batch_op.add_column(sa.Column('impersonated_by', sa.String(length=120), nullable=True))
    # No backfill: NULL is the correct value for all pre-existing rows.


def downgrade():
    with op.batch_alter_table('campaigns', schema=None) as batch_op:
        batch_op.drop_column('impersonated_by')
    # Any impersonated_by values written between deploy and rollback are lost.
    # Ground truth is preserved in the append-only audit_log.
