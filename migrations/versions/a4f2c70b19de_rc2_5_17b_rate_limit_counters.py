"""RC2.5.17 Gate B — durable rate-limit counters

Revision ID: a4f2c70b19de
Revises: d1b6c48e7f92
Create Date: 2026-09-23

WHY
---
The only rate limiter this application has is a process-local dict
(app.routes.public._RATE_LIMITS). It resets on every deploy -- and this
service deploys on every push to main -- and it is per-worker, so its
correctness silently depends on WEB_CONCURRENCY staying at 1. That is
tolerable for the six things it guards and unusable for OTP, where each unit
of budget is a real message to a real handset with a real cost.

This migration adds the durable counter store. It is purely ADDITIVE: one new
table, no existing table touched, no backfill, no data read or rewritten. The
rate limiter is not wired to any route in this phase, so applying this
migration changes no observable behaviour -- the table is created empty and
stays empty until a later phase activates a caller.

THE UNIQUE CONSTRAINT IS NOT A TIDINESS CONSTRAINT
--------------------------------------------------
uq_rate_limit_counters_bucket is the precondition for the limiter's atomicity.
rate_limit_service consumes budget with a single
    INSERT ... ON CONFLICT (scope, subject, window_start) DO UPDATE
       SET count = count + 1 RETURNING count
and ON CONFLICT requires exactly this constraint to exist -- without it the
statement is a syntax-level failure, and the read-then-write fallback would be
wrong for the reason RC2.5.16 Gate B.1 demonstrated on this database:
concurrent transactions at READ COMMITTED cannot see each other's uncommitted
INSERTs, so each would create its own bucket and count only its own share.
Dropping this constraint would not degrade the limiter; it would break it.

RETENTION, NOT GROWTH
---------------------
ix_rate_limit_counters_window_start exists so the 7-day retention sweep can
delete by age without a sequential scan. Retention is short because `subject`
holds a phone number in clear. Nothing in the limiter's correctness depends on
the sweep running: a bucket outside the current hour is never read again, so a
purge that never runs leaks storage and PII but can never grant budget.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a4f2c70b19de'
down_revision = 'd1b6c48e7f92'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'rate_limit_counters',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('scope', sa.String(length=64), nullable=False),
        # 64, not 45. 45 is what the two subject kinds need today (a canonical
        # destination is <= 20, a canonical IP <= 45), but it is not
        # future-safe: a composite subject of the form
        # destination + "|" + purpose is 20 + 1 + 32 = 53 at maximum, which
        # overflows 45. PostgreSQL raises on overflow rather than truncating,
        # so that would surface as a hard error on a loaded limiter; a backend
        # that truncates silently would be worse still, collapsing two
        # subjects into one bucket -- a bypass that leaves no trace. See
        # RateLimitCounter.subject for the full reasoning.
        sa.Column('subject', sa.String(length=64), nullable=False),
        sa.Column('window_start', sa.DateTime(), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('scope', 'subject', 'window_start',
                            name='uq_rate_limit_counters_bucket'),
    )
    op.create_index('ix_rate_limit_counters_window_start',
                    'rate_limit_counters', ['window_start'])


def downgrade():
    # Reversible and complete: the table did not exist before this revision,
    # holds only ephemeral counters, and nothing references it by foreign key,
    # so dropping it restores the previous schema exactly. Dropping the index
    # explicitly first rather than relying on drop_table's cascade keeps the
    # downgrade symmetric with the upgrade on every dialect.
    op.drop_index('ix_rate_limit_counters_window_start',
                  table_name='rate_limit_counters')
    op.drop_table('rate_limit_counters')
