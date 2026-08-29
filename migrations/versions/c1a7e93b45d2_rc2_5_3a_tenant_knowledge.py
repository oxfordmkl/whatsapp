"""RC2.5.3a — tenant knowledge foundation

Revision ID: c1a7e93b45d2
Revises: b8f4c2e97d15
Create Date: 2026-08-28

WHY
---
Business knowledge -- courses, fees, FAQs, services -- is currently hardcoded
inside AALIZA_PROMPT and app/bot/constants.py. RC2.5.2 made tenant IDENTITY
configurable, so a second tenant's bot now correctly introduces itself, while
still quoting Oxford's course list and Oxford's fees to that tenant's own
customers. This table is where per-tenant knowledge goes instead.

ONE TABLE, NOT FIVE
-------------------
A course, a product, a menu item, a service and an FAQ are all "a titled thing
with a body and some attributes" to prompt composition. `kind` discriminates
them. Typed tables are deferred until a vertical needs relational structure
(variants -> inventory -> orders), which is a commerce concern and commerce is
not built yet.

`kind` is a plain string rather than an Enum precisely so that adding a
vertical never requires a migration.

COLUMNS VS JSON
---------------
Columns for what is filtered on (tenant_id, kind, is_active); `attributes`
JSON text for what is only rendered (fee, duration, payment_url, sku).
Same boundary TenantSettings.settings established, and the same convention:
parsed in Python, never queried server-side.

ISOLATION
---------
tenant_id is NOT NULL, indexed, and carries a composite index matching the
exact retrieval shape. This is a READ path feeding the AI: a missing filter
leaks one tenant's pricing into another tenant's customer conversation. The
filter is enforced inside knowledge_service.fetch_knowledge(), with dedicated
cross-tenant tripwire tests.

DATA STATE AT AUTHORING (verified read-only against production)
---------------------------------------------------------------
    tenants                        12
    tenant_settings rows           12
    tenants with business_profile   0
    non-null waba_phone_number_id   1   (The Oxford Computers)

ADDITIVE AND EMPTY
------------------
This migration only CREATES a table. It alters nothing existing, backfills
nothing, and the table starts empty. With zero rows, fetch_knowledge() returns
nothing, the composer's L3 slot stays empty, and every tenant's prompt is
byte-identical to RC2.5.2 -- the dual-read fallback. Rollback is a plain
DROP TABLE with no data loss, because there is no data yet.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c1a7e93b45d2'
down_revision = 'b8f4c2e97d15'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'tenant_knowledge',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False,
                  server_default='faq'),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('attributes', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('is_active', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('sort_order', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('tenant_knowledge', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tenant_knowledge_tenant_id'),
                              ['tenant_id'], unique=False)
        batch_op.create_index('idx_tenant_knowledge_lookup',
                              ['tenant_id', 'kind', 'is_active'], unique=False)


def downgrade():
    with op.batch_alter_table('tenant_knowledge', schema=None) as batch_op:
        batch_op.drop_index('idx_tenant_knowledge_lookup')
        batch_op.drop_index(batch_op.f('ix_tenant_knowledge_tenant_id'))

    op.drop_table('tenant_knowledge')
