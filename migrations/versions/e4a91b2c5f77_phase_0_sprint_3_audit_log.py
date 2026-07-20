"""Phase 0 Sprint 3 — sovereign append-only audit_log table (Constitution I.7)

Additive expand-phase migration: creates one new table, touches nothing else.
Rollback (downgrade) drops the table — safe because the table is new and
append-only; no other object references it.

Revision ID: e4a91b2c5f77
Revises: c7a2f19d4e88
Create Date: 2026-07-20

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e4a91b2c5f77'
down_revision = 'c7a2f19d4e88'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'audit_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=True),
        sa.Column('actor', sa.String(length=120), nullable=True),
        sa.Column('action', sa.String(length=40), nullable=False),
        sa.Column('target', sa.String(length=255), nullable=True),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('ip_address', sa.String(length=45), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_audit_log_tenant_id'), 'audit_log', ['tenant_id'])
    op.create_index(op.f('ix_audit_log_action'), 'audit_log', ['action'])
    op.create_index(op.f('ix_audit_log_created_at'), 'audit_log', ['created_at'])


def downgrade():
    op.drop_index(op.f('ix_audit_log_created_at'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_action'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_tenant_id'), table_name='audit_log')
    op.drop_table('audit_log')
