"""Phase 10.6 — add conversation_state.sales_stage_id (Sales Pipeline link)

Revision ID: e7b3a91d5c40
Revises: d4e8b1a37c62
Create Date: 2026-07-29

Why a SECOND stage column
-------------------------
conversation_state already has pipeline_stage_id, but that column is owned by
the AI conversation engine: the `stage` hybrid_property resolves through it,
and the bot reads and writes it on every inbound message via StateProxy.
Repointing it at sales stages would make `stage` return a sales key and send
the router down the wrong branch. One FK cannot serve two pipelines, so the
Sales Pipeline gets its own.

Both columns reference the same pipeline_stages table; they are kept apart by
their PipelineDefinition — the bot funnel's definition versus the tenant's
'sales' definition. That is what lets one stage table serve both systems
without them ever resolving through the same column.

Additive only
-------------
One nullable column plus its index. No column is altered or dropped, and no
data is written. Existing rows are valid the moment this lands: sales_stage_id
is NULL and the lead_status adapter falls back to the legacy string, so every
reader keeps working before the backfill runs.

Note there is no DDL for lead_status. The model now maps it as
`_lead_status = db.Column('lead_status', ...)`, which renames the Python
attribute while leaving the physical column exactly as it is — the same
technique already used for _stage/_course/_batch_time/_offer_course.

Locking
-------
ADD COLUMN ... NULL is a metadata-only operation on PostgreSQL: no table
rewrite, no row scan. The index build takes a brief SHARE lock, which at the
current table size (64 rows) is sub-millisecond. CONCURRENTLY is deliberately
not used — it cannot run inside Alembic's transaction block, and the table is
far too small to justify it.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'e7b3a91d5c40'
down_revision = 'd4e8b1a37c62'
branch_labels = None
depends_on = None

TABLE = 'conversation_state'
COLUMN = 'sales_stage_id'
INDEX = 'ix_conversation_state_sales_stage_id'
FK = 'fk_conversation_state_sales_stage_id'


def upgrade():
    # batch_alter_table mirrors how a5f0c3e91b7d added pipeline_stage_id — the
    # directly analogous column. It is also the only form SQLite accepts for
    # adding a constraint, which keeps the migration verifiable locally rather
    # than only against production PostgreSQL.
    #
    # All existing rows get sales_stage_id = NULL, so the FK has nothing to
    # validate and the table is never rewritten.
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.add_column(sa.Column(COLUMN, sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f(INDEX), [COLUMN], unique=False)
        batch_op.create_foreign_key(FK, 'pipeline_stages', [COLUMN], ['id'])


def downgrade():
    # Dropping the link returns every row to the legacy lead_status string via
    # the adapter's fallback, so no lead loses its status.
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(FK, type_='foreignkey')
        batch_op.drop_index(batch_op.f(INDEX))
        batch_op.drop_column(COLUMN)
