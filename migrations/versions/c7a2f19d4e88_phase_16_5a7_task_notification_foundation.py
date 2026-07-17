"""feat: Phase 16.5A7 task & notification foundation

Revision ID: c7a2f19d4e88
Revises: b6e1d4f82c9e
Create Date: 2026-07-17 09:00:00.000000

Phase 16.5A7 — Enterprise Task & Notification Foundation (ADR-021).

Additive only. Creates two new tables:

  CREATE TABLE tasks           — first-class Admin-owned staff task
  CREATE TABLE notifications   — per-recipient notification with read state

Safety notes:
  • Both tables are NEW. No existing table is altered, dropped or backfilled.
  • The Enterprise Data Layer (Phase 16.5A6: pipeline_definitions,
    pipeline_stages, offering, conversation_state_offerings) is NOT touched.
  • The legacy event-sourced task model (LeadEvent FOLLOW_UP_TASK /
    FOLLOW_UP_COMPLETED) is NOT migrated or removed — it remains the audit
    trail and keeps every existing analytics reader working (ADR-021).
  • No FK uses ondelete=CASCADE (SCHEMA_RULES §12 — forbidden).
  • Fully reversible: downgrade drops both tables in FK-safe order.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c7a2f19d4e88'
down_revision = 'b6e1d4f82c9e'
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. CREATE TABLE tasks ───────────────────────────────────────────────
    op.create_table(
        'tasks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('task_uid', sa.String(length=32), nullable=False),
        sa.Column('lead_phone', sa.String(length=20), nullable=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('staff_notes', sa.Text(), nullable=True),
        sa.Column('priority', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('due_date', sa.String(length=10), nullable=True),
        sa.Column('remind_at', sa.DateTime(), nullable=True),
        sa.Column('reminder_sent', sa.Boolean(), nullable=False),
        sa.Column('assigned_staff', sa.String(length=100), nullable=True),
        sa.Column('created_by', sa.String(length=100), nullable=True),
        sa.Column('completed_by', sa.String(length=100), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'task_uid', name='uq_task_tenant_uid'),
    )
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tasks_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_task_uid'), ['task_uid'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_lead_phone'), ['lead_phone'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_assigned_staff'), ['assigned_staff'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_remind_at'), ['remind_at'], unique=False)
        batch_op.create_index('idx_task_tenant_status', ['tenant_id', 'status'], unique=False)
        batch_op.create_index('idx_task_tenant_staff', ['tenant_id', 'assigned_staff'], unique=False)

    # ── 2. CREATE TABLE notifications (FKs tasks -> must follow) ────────────
    op.create_table(
        'notifications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('recipient', sa.String(length=100), nullable=False),
        sa.Column('notif_type', sa.String(length=30), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('body', sa.String(length=500), nullable=True),
        sa.Column('lead_phone', sa.String(length=20), nullable=True),
        sa.Column('task_id', sa.Integer(), nullable=True),
        sa.Column('is_read', sa.Boolean(), nullable=False),
        sa.Column('read_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notifications_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_notifications_recipient'), ['recipient'], unique=False)
        batch_op.create_index(batch_op.f('ix_notifications_created_at'), ['created_at'], unique=False)
        batch_op.create_index('idx_notif_recipient_unread', ['tenant_id', 'recipient', 'is_read'], unique=False)
        batch_op.create_index('idx_notif_recipient_created', ['tenant_id', 'recipient', 'created_at'], unique=False)


def downgrade():
    # Reverse order: notifications FK tasks, so drop notifications first.
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.drop_index('idx_notif_recipient_created')
        batch_op.drop_index('idx_notif_recipient_unread')
        batch_op.drop_index(batch_op.f('ix_notifications_created_at'))
        batch_op.drop_index(batch_op.f('ix_notifications_recipient'))
        batch_op.drop_index(batch_op.f('ix_notifications_tenant_id'))

    op.drop_table('notifications')

    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_index('idx_task_tenant_staff')
        batch_op.drop_index('idx_task_tenant_status')
        batch_op.drop_index(batch_op.f('ix_tasks_remind_at'))
        batch_op.drop_index(batch_op.f('ix_tasks_assigned_staff'))
        batch_op.drop_index(batch_op.f('ix_tasks_lead_phone'))
        batch_op.drop_index(batch_op.f('ix_tasks_task_uid'))
        batch_op.drop_index(batch_op.f('ix_tasks_tenant_id'))

    op.drop_table('tasks')
