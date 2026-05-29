"""Phase 6A lead event tracking

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-29 11:43:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    # ### Phase 6A: Lead event table — additive only ###
    op.create_table('lead_event',
        sa.Column('id',         sa.Integer(),     nullable=False),
        sa.Column('phone',      sa.String(20),    nullable=False),
        sa.Column('event_type', sa.String(50),    nullable=False),
        sa.Column('event_data', sa.Text(),        nullable=True),
        sa.Column('created_at', sa.DateTime(),    nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('lead_event', schema=None) as batch_op:
        batch_op.create_index('idx_lead_event_phone_created',
                              ['phone', 'created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_lead_event_phone'),
                              ['phone'], unique=False)
    # ### end Alembic commands ###


def downgrade():
    # ### Phase 6A rollback — drops new table only ###
    with op.batch_alter_table('lead_event', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_lead_event_phone'))
        batch_op.drop_index('idx_lead_event_phone_created')
    op.drop_table('lead_event')
    # ### end Alembic commands ###
