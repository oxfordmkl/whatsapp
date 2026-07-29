"""Phase 10.2A — composite index on conversation_state (tenant_id, updated_at)

Revision ID: d4e8b1a37c62
Revises: c3f7e2a41d09
Create Date: 2026-07-29

Why
---
Every lead list is "this tenant's leads, newest activity first":

    crm_leads     ORDER BY conversation_state.updated_at DESC   (tenant-filtered)
    crm_my_leads  ORDER BY conversation_state.updated_at DESC   (tenant-filtered)

`tenant_id` was already indexed on its own, so the filter was assisted but the
sort was not — Postgres had to sort the whole tenant slice on every page view.
This composite covers filter and order together.

It is deliberately (tenant_id, updated_at) in that order: tenant_id is always an
equality predicate and updated_at is always the range/sort key, so tenant_id
must lead for the index to serve the ORDER BY.

Additive only
-------------
No column is added, altered or dropped. No existing index is removed — the
standalone ix on tenant_id stays, because other queries filter by tenant
without touching updated_at. Nothing reads or writes row data here, so the
migration cannot corrupt or lose anything.

Locking note
------------
A plain CREATE INDEX takes a SHARE lock, blocking writes to conversation_state
for its duration. On the current production table (60 rows) that is
sub-millisecond. CONCURRENTLY is deliberately NOT used: it cannot run inside a
transaction block, which is how Alembic executes migrations by default, and the
table is far too small to justify that complexity. Should this table reach a
size where the lock matters, build the index out-of-band with
CREATE INDEX CONCURRENTLY and stamp the revision instead.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd4e8b1a37c62'
down_revision = 'c3f7e2a41d09'
branch_labels = None
depends_on = None

INDEX_NAME = 'ix_conversation_state_tenant_updated'
TABLE_NAME = 'conversation_state'


def upgrade():
    # if_not_exists keeps this re-runnable: the index is also declared in
    # ConversationState.__table_args__, so a db.create_all() on a fresh
    # database (tests, local bootstrap) may already have created it.
    op.create_index(
        INDEX_NAME,
        TABLE_NAME,
        ['tenant_id', 'updated_at'],
        unique=False,
        if_not_exists=True,
    )


def downgrade():
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME, if_exists=True)
