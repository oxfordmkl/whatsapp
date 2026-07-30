"""Phase 10.8 — create lead_stage_history

Revision ID: f2c9d4b81a37
Revises: e7b3a91d5c40
Create Date: 2026-07-30

Why
---
Sales stage movement already works (through the Phase 10.6 lead_status
adapter) but leaves almost no trace. audit_log records THAT a status changed
and stores display names only, so a row cannot be joined to a stage and a
stage rename orphans the history. This table is the substrate for
time-in-stage, velocity and conversion analytics, none of which are possible
today.

Additive only
-------------
One new table. No existing column is added, altered or dropped, and no data is
written or migrated. Dropping the table returns the system to exactly today's
behaviour — leads keep their stage, only the movement log is lost.

No backfill
-----------
History starts at deployment, deliberately. The only available source is
audit_log, whose rows carry names without stage ids and predate the sales
pipeline entirely; reconstructing from them would be guesswork presented as
fact.

Nullability
-----------
from_stage_id is nullable because a lead's FIRST entry into the pipeline has
no prior stage — inventing a synthetic one would corrupt any time-in-stage
calculation built on this table. to_stage_id is nullable so a move to an
unmapped status (no stage link) is still recorded rather than silently
dropped; the *_status columns preserve what the operator saw in that case.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'f2c9d4b81a37'
down_revision = 'e7b3a91d5c40'
branch_labels = None
depends_on = None

TABLE = 'lead_stage_history'


def upgrade():
    op.create_table(
        TABLE,
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('conversation_state_id', sa.Integer(), nullable=False),
        sa.Column('from_stage_id', sa.Integer(), nullable=True),
        sa.Column('to_stage_id', sa.Integer(), nullable=True),
        sa.Column('from_status', sa.String(length=50), nullable=True),
        sa.Column('to_status', sa.String(length=50), nullable=True),
        sa.Column('actor', sa.String(length=120), nullable=True),
        sa.Column('changed_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
        sa.ForeignKeyConstraint(['conversation_state_id'], ['conversation_state.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['from_stage_id'], ['pipeline_stages.id'], ),
        sa.ForeignKeyConstraint(['to_stage_id'], ['pipeline_stages.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    # batch_alter_table matches how a5f0c3e91b7d and e7b3a91d5c40 add indexes,
    # and is the form SQLite accepts, keeping the migration verifiable locally
    # rather than only against production PostgreSQL.
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_lead_stage_history_tenant_id'),
                              ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_lead_stage_history_conversation_state_id'),
                              ['conversation_state_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_lead_stage_history_changed_at'),
                              ['changed_at'], unique=False)
        # The read pattern: one lead's movements in order. tenant_id leads so
        # the same index also serves tenant-wide velocity queries.
        batch_op.create_index('ix_lead_stage_history_tenant_lead_time',
                              ['tenant_id', 'conversation_state_id', 'changed_at'],
                              unique=False)


def downgrade():
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.drop_index('ix_lead_stage_history_tenant_lead_time')
        batch_op.drop_index(batch_op.f('ix_lead_stage_history_changed_at'))
        batch_op.drop_index(batch_op.f('ix_lead_stage_history_conversation_state_id'))
        batch_op.drop_index(batch_op.f('ix_lead_stage_history_tenant_id'))
    op.drop_table(TABLE)
