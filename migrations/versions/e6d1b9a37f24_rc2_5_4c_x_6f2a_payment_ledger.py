"""RC2.5.4c-x-6f2a — tenant-scoped payment ledger foundation

Revision ID: e6d1b9a37f24
Revises: c1a7e93b45d2
Create Date: 2026-09-16

WHY
---
RC2.5.4c-x-6f1 contained a P0: customer-typed text at the payment_pending
stage was treated as proof of payment. It no longer is. The x-6f2 audit then
established that the platform holds no payment-provider credentials for any
tenant, and -- separately -- that there is nowhere to record a payment even
once verification becomes possible: no payment table, no provider identity,
no amount, and no idempotency key. This migration creates that record.

FOUNDATION ONLY
---------------
Schema only. No writer exists, and none is added by this phase: not the bot,
not the webhook, not the payment-link resolver, not the staff forms. The
table is created EMPTY and stays empty until an authorised, evidence-based
writer is built in a later phase. Production row count after this migration
is 0.

IDEMPOTENCY IS A CONSTRAINT, NOT A CHECK
-----------------------------------------
UNIQUE(provider, provider_payment_id) makes a duplicate provider payment
unrepresentable. A lookup-then-insert check races with a webhook retry; a
constraint does not. Both columns are NOT NULL, because PostgreSQL treats
NULLs as distinct under UNIQUE -- nullable identity would silently permit
unlimited duplicates and defeat the whole point.

ISOLATION
---------
tenant_id is NOT NULL, FK to tenants.id, indexed, with composite indexes for
the three query shapes a future verifier and the CRM actually need:
(tenant_id, provider), (tenant_id, status), (tenant_id, course_code). There
is no global payment and no primary-tenant fallback. course_code carries the
STABLE course code (commercial.code) and has no FK, because courses live in
TenantKnowledge JSON attributes -- and the same code may exist independently
under different tenants.

ADDITIVE AND EMPTY
------------------
This migration only CREATES a table. It alters no existing table, backfills
nothing, and touches no historical data -- including the 12 historical false
confirmations, which are a separate remediation phase. Rollback is a plain
DROP TABLE with no data loss, because there is no data.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e6d1b9a37f24'
down_revision = 'c1a7e93b45d2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'payments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('provider', sa.String(length=20), nullable=False),
        sa.Column('provider_payment_id', sa.String(length=100), nullable=False),
        sa.Column('conversation_state_id', sa.Integer(), nullable=True),
        sa.Column('course_code', sa.String(length=32), nullable=True),
        sa.Column('expected_amount_minor', sa.Integer(), nullable=True),
        sa.Column('paid_amount_minor', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('verification_source', sa.String(length=20), nullable=True),
        sa.Column('verified_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
        sa.ForeignKeyConstraint(['conversation_state_id'],
                                ['conversation_state.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider', 'provider_payment_id',
                            name='uq_payment_provider_payment_id'),
    )
    with op.batch_alter_table('payments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_payments_tenant_id'),
                              ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_payments_conversation_state_id'),
                              ['conversation_state_id'], unique=False)
        batch_op.create_index('ix_payments_tenant_provider',
                              ['tenant_id', 'provider'], unique=False)
        batch_op.create_index('ix_payments_tenant_status',
                              ['tenant_id', 'status'], unique=False)
        batch_op.create_index('ix_payments_tenant_course_code',
                              ['tenant_id', 'course_code'], unique=False)


def downgrade():
    with op.batch_alter_table('payments', schema=None) as batch_op:
        batch_op.drop_index('ix_payments_tenant_course_code')
        batch_op.drop_index('ix_payments_tenant_status')
        batch_op.drop_index('ix_payments_tenant_provider')
        batch_op.drop_index(batch_op.f('ix_payments_conversation_state_id'))
        batch_op.drop_index(batch_op.f('ix_payments_tenant_id'))

    op.drop_table('payments')
