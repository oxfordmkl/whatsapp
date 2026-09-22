"""RC2.5.15 — user phone identity foundation

Revision ID: b7d2e4f91a35
Revises: e6d1b9a37f24
Create Date: 2026-09-22

WHY
---
templates/public/register.html has rendered a "Phone Number" field since Phase
13-A2B, and app/routes/public.py read it into a local variable and discarded
it -- there was no column to hold it. Every business that has ever registered
typed a number that went nowhere. This migration creates the column that makes
that input storable, and a phone_verified_at mirroring email_verified_at.

ADDITIVE AND EMPTY
------------------
Two nullable columns and their indexes. This migration alters no existing
column, drops nothing, and BACKFILLS NOTHING: all 21 production users receive
NULL. Read-only pre-migration audit (RC2.5.15 Gate B) confirmed neither column
exists on the production users table.

NO UNIQUENESS CONSTRAINT -- DELIBERATELY
----------------------------------------
The Gate A audit named a live-data duplicate check as the precondition for any
uniqueness constraint. That check was performed and is vacuous BY CONSTRUCTION:
with no backfill, every row is NULL, so no constraint could be violated at
migration time. Uniqueness is nonetheless NOT added here, because the POLICY is
undecided and this table carries both precedents -- email is globally unique
(ix_users_email), username is unique per tenant (uq_users_tenant_username).
Nothing authenticates on phone yet, so a constraint would enforce nothing
today, while the wrong one is considerably harder to remove than the right one
is to add later. The phase that introduces phone login owns that decision.

VERIFIED-AT IS NOT SET BY ANYTHING
----------------------------------
phone_verified_at exists so the schema can express verification; no writer
sets it, in this phase or anywhere in the codebase. Supplying a number at
registration proves nothing about holding it.

NOTE FOR WHOEVER APPLIES THIS
-----------------------------
Production alembic_version is c1a7e93b45d2, one revision BEHIND the repository
head e6d1b9a37f24 (RC2.5.4c-x-6f2a payment ledger, which creates an empty
`payments` table -- confirmed absent from production). This revision chains
onto the true head rather than forking the history, so `alembic upgrade head`
will apply BOTH. That is correct alembic practice and was raised explicitly in
the RC2.5.15 Gate B report rather than left to be discovered at deploy time.
"""
from alembic import op
import sqlalchemy as sa

revision = 'b7d2e4f91a35'
down_revision = 'e6d1b9a37f24'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('phone', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('phone_verified_at', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_phone'), ['phone'], unique=False)
        batch_op.create_index(batch_op.f('ix_users_phone_verified_at'),
                              ['phone_verified_at'], unique=False)


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_phone_verified_at'))
        batch_op.drop_index(batch_op.f('ix_users_phone'))
        batch_op.drop_column('phone_verified_at')
        batch_op.drop_column('phone')
