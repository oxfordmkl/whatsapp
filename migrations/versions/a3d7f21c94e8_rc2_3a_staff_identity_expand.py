"""Phase RC2.3A — staff identity EXPAND

Revision ID: a3d7f21c94e8
Revises: f2c9d4b81a37
Create Date: 2026-08-01

Why
---
Staff Management is backed by app/data/staff_master.json, a single global file
with no tenant dimension: every tenant reads and writes the same staff, and the
file ships Oxford's staff (ANJU / KIRAN / NISHA) to every tenant on deploy
(RC2.2). RC2.2C ratified User.id as the canonical staff identity.

This is the EXPAND step only. It adds the columns the later phases need and
changes nothing else.

Additive only
-------------
Three nullable columns and one index. No column is altered, renamed, dropped or
made NOT NULL. No data is written, read or migrated. No existing index is
touched. Running downgrade() returns the schema to exactly f2c9d4b81a37.

Nothing in the application reads or writes these columns after this migration —
the registry remains the source of truth and assigned_staff remains
authoritative. Behaviour is bit-for-bit unchanged.

users.display_name
------------------
username is a CREDENTIAL; display_name is what an operator reads. Their absence
is why display names were forced into username — production already stores
'NIBU S S' and 'Bibin Thomas' as usernames, and 'NIBU S S' exists in FOUR
tenants (uniqueness is (tenant_id, username), not global). Left NULL by this
migration; readers fall back to username.

*.assigned_user_id
------------------
assigned_staff holds a display name, ownership compares it to
current_user.username, and analytics compare it to normalize_staff_name().
Production has already drifted: 'kiran'/'anju' case variants and
'Anju_display', which resolves to no user at all and has propagated into
notifications as well as leads.

A user_id resolves to exactly one user in exactly one tenant, so cross-tenant
assignment becomes unrepresentable rather than merely filtered.

Deliberately NO server_default. A column default is applied at flush and never
passes through a setter — the Phase 10.8C defect that left every bot-created
lead with sales_stage_id NULL. This column must only ever be set explicitly.

idx_task_tenant_user
--------------------
Mirrors the existing idx_task_tenant_staff for the FK. Created here, ahead of
any reader, because adding it at the read-flip instead would mean the flip
silently loses index coverage on one of the two highest-growth tables.

NOT in this migration
---------------------
notifications.recipient_user_id is deliberately excluded. recipient holds
'Admin' in production — a value produced by _actor_name()'s fallback, which
collides with the title-cased form of the real user 'admin' — and notify()
matches recipients case-sensitively while its producers normalise
inconsistently. RC2.2C explicitly declined to ratify a bare FK there; adding
one now would bake in a design that was rejected. Notifications are their own
workstream, sequenced before their FK.

tasks.created_by / completed_by and the two actor columns are audit fields and
stay free text permanently: they legitimately hold 'broadcast-api' (x1797),
'whatsapp-inbound' and 'phase-10.6c-data-normalisation'.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a3d7f21c94e8'
down_revision = 'f2c9d4b81a37'
branch_labels = None
depends_on = None


def upgrade():
    # batch_alter_table matches f2c9d4b81a37 / e7b3a91d5c40 / a5f0c3e91b7d and
    # is the form SQLite accepts, keeping the migration verifiable locally
    # rather than only against production PostgreSQL.
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('display_name', sa.String(length=120),
                                      nullable=True))

    with op.batch_alter_table('conversation_state', schema=None) as batch_op:
        batch_op.add_column(sa.Column('assigned_user_id', sa.Integer(),
                                      nullable=True))
        batch_op.create_index(batch_op.f('ix_conversation_state_assigned_user_id'),
                              ['assigned_user_id'], unique=False)
        batch_op.create_foreign_key('fk_conversation_state_assigned_user',
                                    'users', ['assigned_user_id'], ['id'])

    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('assigned_user_id', sa.Integer(),
                                      nullable=True))
        batch_op.create_index(batch_op.f('ix_tasks_assigned_user_id'),
                              ['assigned_user_id'], unique=False)
        batch_op.create_index('idx_task_tenant_user',
                              ['tenant_id', 'assigned_user_id'], unique=False)
        batch_op.create_foreign_key('fk_tasks_assigned_user',
                                    'users', ['assigned_user_id'], ['id'])


def downgrade():
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_constraint('fk_tasks_assigned_user', type_='foreignkey')
        batch_op.drop_index('idx_task_tenant_user')
        batch_op.drop_index(batch_op.f('ix_tasks_assigned_user_id'))
        batch_op.drop_column('assigned_user_id')

    with op.batch_alter_table('conversation_state', schema=None) as batch_op:
        batch_op.drop_constraint('fk_conversation_state_assigned_user',
                                 type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_conversation_state_assigned_user_id'))
        batch_op.drop_column('assigned_user_id')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('display_name')
