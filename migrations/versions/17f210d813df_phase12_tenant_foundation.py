"""Phase 12-B Tenant Foundation (CORRECTED)

Revision ID: 17f210d813df
Revises: 623e5fa136ef
Create Date: 2026-06-10
"""
from alembic import op
import sqlalchemy as sa
import uuid

revision = '17f210d813df'
down_revision = '623e5fa136ef'
branch_labels = None
depends_on = None

# ─────────────────────────────────────────────
# CHILD TABLES requiring new tenant_id column
# ─────────────────────────────────────────────
CHILD_TABLES = [
    'conversation_state',
    'conversation_message',
    'message_log',
    'lead_event',
    'follow_up_jobs',
    'pending_messages',
]

# ALL tables requiring backfill + FK (incl. users)
ALL_TABLES = CHILD_TABLES + ['users']


def upgrade():
    # ─── PHASE 1: Create tenants table ───────────────────────────────
    op.create_table('tenants',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )

    # ─── PHASE 2: Generate UUID and insert root tenant ───────────────
    default_tenant_id = uuid.uuid4().hex
    op.execute(
        f"INSERT INTO tenants (id, name, created_at) "
        f"VALUES ('{default_tenant_id}', 'Oxford Computers', CURRENT_TIMESTAMP)"
    )

    # ─── PHASE 3: Add tenant_id as nullable=True to 6 child tables ───
    for table in CHILD_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column('tenant_id', sa.String(length=36), nullable=True))
            batch_op.create_index(f'ix_{table}_tenant_id', ['tenant_id'], unique=False)

    # ─── PHASE 4: Backfill all 7 tables ──────────────────────────────
    for table in ALL_TABLES:
        op.execute(
            f"UPDATE {table} SET tenant_id = '{default_tenant_id}' WHERE tenant_id IS NULL"
        )

    # ─── PHASE 5: Validate — hard abort if backfill missed any rows ───
    conn = op.get_bind()
    for table in CHILD_TABLES:
        result = conn.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id IS NULL")
        ).scalar()
        if result > 0:
            raise Exception(
                f"BACKFILL FAILED: {result} rows in '{table}' still have NULL tenant_id. "
                f"Aborting migration to protect data integrity."
            )

    # ─── PHASE 6: Enforce nullable=False on 6 child tables ───────────
    for table in CHILD_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(
                'tenant_id',
                existing_type=sa.String(length=36),
                nullable=False
            )

    # ─── PHASE 7: Add NAMED FK constraints to all 7 tables ───────────
    for table in ALL_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.create_foreign_key(
                f'fk_{table}_tenant_id',      # Explicit name — required for downgrade
                'tenants',
                ['tenant_id'],
                ['id']
            )

    # ─── PHASE 8: Replace conversation_state unique phone constraint ──
    with op.batch_alter_table('conversation_state', schema=None) as batch_op:
        batch_op.drop_index('ix_conversation_state_phone')  # Drop old unique-phone index
        batch_op.create_unique_constraint(
            'uq_conversation_state_phone_tenant',
            ['phone', 'tenant_id']
        )


def downgrade():
    # ─── REVERSE Phase 8 ─────────────────────────────────────────────
    with op.batch_alter_table('conversation_state', schema=None) as batch_op:
        batch_op.drop_constraint('uq_conversation_state_phone_tenant', type_='unique')
        batch_op.create_index('ix_conversation_state_phone', ['phone'], unique=True)

    # ─── REVERSE Phase 7: Drop NAMED FK constraints ───────────────────
    for table in reversed(ALL_TABLES):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(f'fk_{table}_tenant_id', type_='foreignkey')

    # ─── REVERSE Phase 6 + 3: Alter back to nullable, drop idx + col ──
    for table in CHILD_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(
                'tenant_id',
                existing_type=sa.String(length=36),
                nullable=True
            )
            batch_op.drop_index(f'ix_{table}_tenant_id')
            batch_op.drop_column('tenant_id')

    # ─── REVERSE Phase 1: Drop tenants table ─────────────────────────
    op.drop_table('tenants')
    # NOTE: users.tenant_id column is preserved intentionally (existed pre-Phase-12)