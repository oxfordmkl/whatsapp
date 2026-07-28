"""phase_8_2e_9c_campaign_audience_segment

Revision ID: c3f7e2a41d09
Revises: a8d3f2e91c05
Create Date: 2026-07-25

ADR-025 D8 (Phase 8.2E.9-C): add nullable audience_segment column to campaigns.

Records which named segment (from the eight produced by
_calculate_audiences()) mark_running() resolves at launch. Persisted rather
than passed as a launch-request parameter so a future scheduled-launch sweep
has something to read from (Campaign.scheduled_at already anticipates launch
without a live request), and so the audience decision has the same provenance
every other campaign field already has. NULL = no segment chosen yet (draft),
or predates this feature. No backfill is required; NULL is semantically
correct for all existing rows.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3f7e2a41d09'
down_revision = 'a8d3f2e91c05'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('campaigns', schema=None) as batch_op:
        batch_op.add_column(sa.Column('audience_segment', sa.String(length=100), nullable=True))
    # No backfill: NULL is the correct value for all pre-existing rows.


def downgrade():
    with op.batch_alter_table('campaigns', schema=None) as batch_op:
        batch_op.drop_column('audience_segment')
