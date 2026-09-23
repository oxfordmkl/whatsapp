"""RC2.5.16 Gate B.2 — latest-wins as a database invariant

Revision ID: d1b6c48e7f92
Revises: c9e5a1f38b64
Create Date: 2026-09-23

WHY
---
Gate B.1 validated the OTP primitive against PostgreSQL 18.4 at READ COMMITTED
and found one real defect: concurrent create_challenge() calls for the same
(destination, purpose) could each leave an ACTIVE challenge. Three
concurrently created challenges were shown to verify INDEPENDENTLY -- three
live credentials where the approved policy allows one, which multiplies the
brute-force surface and defeats the per-challenge attempt cap.

The cause is a phantom insert: each transaction's invalidating UPDATE cannot
see the other transactions' uncommitted INSERTs, so nobody invalidates the rows
the others are adding. No amount of application logic fixes that at READ
COMMITTED. SQLite serialises writers, which is why the SQLite suite passed.

THIS MIGRATION MAKES THE VIOLATION UNREPRESENTABLE
--------------------------------------------------
A partial UNIQUE index over (destination, purpose) restricted to rows that are
neither consumed nor invalidated. Two live challenges can no longer both exist:
the second INSERT blocks on the index and then fails, and create_challenge()
retries, invalidates the winner and inserts -- which is latest-wins.

WHY THE PREDICATE OMITS expires_at
----------------------------------
Because it must. A partial index predicate has to be IMMUTABLE, and PostgreSQL
rejects a volatile one outright -- verified on 18.4:

    CREATE UNIQUE INDEX ... WHERE ... AND expires_at > NOW();
    ERROR: functions in index predicate must be marked IMMUTABLE

That costs nothing, because this predicate is a SUPERSET of ACTIVE: ACTIVE
additionally requires expires_at > now. Uniqueness over a superset implies
uniqueness over its subset, so the index is STRICTLY STRONGER than "at most one
ACTIVE". It permits at most one row that is merely un-consumed and
un-invalidated, expired or not. Expiry stays derived and is still enforced in
the verification predicate; nothing here makes cleanup a security boundary.

AN EXPIRED ROW STILL OCCUPIES THE SLOT, AND THAT IS FINE
--------------------------------------------------------
create_challenge() invalidates rows matching EXACTLY this predicate before it
inserts -- the UPDATE's WHERE clause and this index's WHERE clause are
deliberately the same set -- so a stale expired challenge never blocks a
legitimate new one. Consumed and invalidated rows fall outside the predicate,
so unlimited history is retained.

ADDITIVE AND SAFE ON EXISTING DATA
----------------------------------
Adds one index. No column is added, altered or dropped, nothing is backfilled,
and no row is modified. The OTP table ships empty and production has not yet
received c9e5a1f38b64, so there is no existing data that could violate the new
constraint at creation time.

Dialect-qualified predicates are supplied for PostgreSQL and SQLite, which both
support partial indexes, so the invariant holds on the production database and
in the test suite alike.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd1b6c48e7f92'
down_revision = 'c9e5a1f38b64'
branch_labels = None
depends_on = None

_PREDICATE = 'consumed_at IS NULL AND invalidated_at IS NULL'


def upgrade():
    op.create_index(
        'uq_otp_challenges_one_active',
        'otp_challenges',
        ['destination', 'purpose'],
        unique=True,
        postgresql_where=sa.text(_PREDICATE),
        sqlite_where=sa.text(_PREDICATE),
    )


def downgrade():
    op.drop_index('uq_otp_challenges_one_active', table_name='otp_challenges')
