"""RC2.5.16 — OTP challenge primitive table

Revision ID: c9e5a1f38b64
Revises: b7d2e4f91a35
Create Date: 2026-09-22

WHY
---
An OTP cannot be a stateless signed token. email_service already issues
itsdangerous tokens and its own docstring calls them "signed, stateless" --
which is exactly what a one-time code must not be: with no server-side row
there is nothing to consume, so a code stays replayable until expiry,
attempts cannot be counted, and a resend cannot revoke its predecessor. This
table is that state.

FOUNDATION ONLY
---------------
Schema and service only. There is NO caller: no route, no delivery layer, no
registration or login change. The table is created EMPTY and stays empty until
an authorised phase builds a caller. Production row count after this migration
is 0.

ADDITIVE
--------
Creates one table. Alters nothing, backfills nothing, touches no existing
row. The only foreign keys point OUT of it, at tenants.id and users.id, so no
existing table's shape or constraints change.

NULLABLE tenant_id IS THE POINT
-------------------------------
A signup challenge exists BEFORE the tenant does, so tenant_id must be NULL
and no tenant may be invented for it. This matches audit_log.tenant_id, which
is already nullable for platform-level events, and users.tenant_id, which is
NULL for SUPER_ADMIN. Tenant isolation is not weakened: tenant_query() fails
closed, so a NULL-tenant row is invisible to every tenant-scoped query, and
the OTP service owns its own controlled access instead.

NO STATUS COLUMN
----------------
State is derived from three nullable timestamps plus the clock (ACTIVE /
CONSUMED / INVALIDATED / EXPIRED / EXHAUSTED). A stored status can disagree
with the facts; a derived one cannot. Expiry in particular is enforced in the
verification predicate and never by a cleanup job, so a cleanup that fails
cannot extend a code's life.

NO UNIQUENESS CONSTRAINT
------------------------
Latest-wins is implemented by invalidating prior ACTIVE rows on create, not by
a unique index. A partial unique index on (destination, purpose) WHERE active
would also fight the history this table deliberately retains.

THE CODE IS NOT IN HERE
-----------------------
code_hash is an HMAC-SHA256 hex digest keyed by OTP_HMAC_KEY, which lives in
the environment and never in the database. A leaked table alone therefore
yields nothing: a six-digit code has only 10**6 values and any unkeyed digest
would be exhaustible in milliseconds.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c9e5a1f38b64'
down_revision = 'b7d2e4f91a35'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'otp_challenges',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('purpose', sa.String(length=32), nullable=False),
        sa.Column('destination', sa.String(length=20), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('consumed_at', sa.DateTime(), nullable=True),
        sa.Column('invalidated_at', sa.DateTime(), nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('max_attempts', sa.Integer(), nullable=False,
                  server_default='5'),
        sa.Column('tenant_id', sa.String(length=36), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'],
                                name='fk_otp_challenges_tenant_id'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'],
                                name='fk_otp_challenges_user_id'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('otp_challenges', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_otp_challenges_tenant_id'),
                              ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_otp_challenges_user_id'),
                              ['user_id'], unique=False)
        # Latest-wins lookup, and the per-destination aggregate a future rate
        # limiter needs. Non-unique: several historical rows per destination
        # are expected and retained.
        batch_op.create_index('ix_otp_challenges_destination_purpose',
                              ['destination', 'purpose'], unique=False)
        # Retention sweeps and recency ordering.
        batch_op.create_index('ix_otp_challenges_expires_at',
                              ['expires_at'], unique=False)


def downgrade():
    with op.batch_alter_table('otp_challenges', schema=None) as batch_op:
        batch_op.drop_index('ix_otp_challenges_expires_at')
        batch_op.drop_index('ix_otp_challenges_destination_purpose')
        batch_op.drop_index(batch_op.f('ix_otp_challenges_user_id'))
        batch_op.drop_index(batch_op.f('ix_otp_challenges_tenant_id'))
    op.drop_table('otp_challenges')
