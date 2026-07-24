"""Phase 8.1B — Campaign Foundation (campaigns + campaign_recipients)

ADDITIVE ONLY. Creates two new tables and their indexes. Touches no existing
table, column, index or constraint. No ALTER of production objects, no DROP,
no data migration.

Rollback (downgrade) drops only the two new tables — safe because both are new
and nothing else references them.

Revision ID: f1c8d3a76b40
Revises: e4a91b2c5f77
Create Date: 2026-07-23

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f1c8d3a76b40'
down_revision = 'e4a91b2c5f77'
branch_labels = None
depends_on = None


def upgrade():
    # ── campaigns ──────────────────────────────────────────────────────────
    op.create_table(
        'campaigns',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False,
                  server_default='draft'),
        sa.Column('message_body', sa.Text(), nullable=True),
        sa.Column('template_id', sa.Integer(), nullable=True),
        sa.Column('audience_rule_id', sa.Integer(), nullable=True),
        sa.Column('scheduled_at', sa.DateTime(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('total_recipients', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('sent_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('failed_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_by', sa.String(length=120), nullable=True),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_campaigns_tenant_id'), 'campaigns', ['tenant_id'])
    op.create_index(op.f('ix_campaigns_status'), 'campaigns', ['status'])
    op.create_index(op.f('ix_campaigns_scheduled_at'), 'campaigns', ['scheduled_at'])
    op.create_index(op.f('ix_campaigns_created_at'), 'campaigns', ['created_at'])
    op.create_index('ix_campaigns_tenant_created', 'campaigns',
                    ['tenant_id', 'created_at'])
    op.create_index('ix_campaigns_tenant_status', 'campaigns',
                    ['tenant_id', 'status'])

    # ── campaign_recipients ────────────────────────────────────────────────
    op.create_table(
        'campaign_recipients',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('campaign_id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('phone', sa.String(length=20), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False,
                  server_default='queued'),
        sa.Column('send_at', sa.DateTime(), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('wa_message_id', sa.String(length=100), nullable=True),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('delivered_at', sa.DateTime(), nullable=True),
        sa.Column('read_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('campaign_id', 'phone',
                            name='uq_campaign_recipient_campaign_phone'),
    )
    op.create_index(op.f('ix_campaign_recipients_campaign_id'),
                    'campaign_recipients', ['campaign_id'])
    op.create_index(op.f('ix_campaign_recipients_tenant_id'),
                    'campaign_recipients', ['tenant_id'])
    op.create_index(op.f('ix_campaign_recipients_phone'),
                    'campaign_recipients', ['phone'])
    op.create_index(op.f('ix_campaign_recipients_status'),
                    'campaign_recipients', ['status'])
    op.create_index(op.f('ix_campaign_recipients_send_at'),
                    'campaign_recipients', ['send_at'])
    op.create_index(op.f('ix_campaign_recipients_wa_message_id'),
                    'campaign_recipients', ['wa_message_id'])
    op.create_index('ix_campaign_recipients_campaign_status',
                    'campaign_recipients', ['campaign_id', 'status'])
    op.create_index('ix_campaign_recipients_tenant_status_send_at',
                    'campaign_recipients', ['tenant_id', 'status', 'send_at'])


def downgrade():
    # Drop children before parent (FK order). Both tables are new in this
    # revision, so nothing outside them is affected.
    op.drop_index('ix_campaign_recipients_tenant_status_send_at',
                  table_name='campaign_recipients')
    op.drop_index('ix_campaign_recipients_campaign_status',
                  table_name='campaign_recipients')
    op.drop_index(op.f('ix_campaign_recipients_wa_message_id'),
                  table_name='campaign_recipients')
    op.drop_index(op.f('ix_campaign_recipients_send_at'),
                  table_name='campaign_recipients')
    op.drop_index(op.f('ix_campaign_recipients_status'),
                  table_name='campaign_recipients')
    op.drop_index(op.f('ix_campaign_recipients_phone'),
                  table_name='campaign_recipients')
    op.drop_index(op.f('ix_campaign_recipients_tenant_id'),
                  table_name='campaign_recipients')
    op.drop_index(op.f('ix_campaign_recipients_campaign_id'),
                  table_name='campaign_recipients')
    op.drop_table('campaign_recipients')

    op.drop_index('ix_campaigns_tenant_status', table_name='campaigns')
    op.drop_index('ix_campaigns_tenant_created', table_name='campaigns')
    op.drop_index(op.f('ix_campaigns_created_at'), table_name='campaigns')
    op.drop_index(op.f('ix_campaigns_scheduled_at'), table_name='campaigns')
    op.drop_index(op.f('ix_campaigns_status'), table_name='campaigns')
    op.drop_index(op.f('ix_campaigns_tenant_id'), table_name='campaigns')
    op.drop_table('campaigns')
