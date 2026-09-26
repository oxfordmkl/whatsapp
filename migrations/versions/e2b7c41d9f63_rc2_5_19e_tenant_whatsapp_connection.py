"""RC2.5.19-E — tenant WhatsApp Embedded Signup connection columns

Revision ID: e2b7c41d9f63
Revises: c7e19d4a2b58
Create Date: 2026-09-26

WHY
---
Embedded Signup binds a tenant to a WhatsApp Business Account (WABA) that is
verified server-side, then registers the number and subscribes the app to the
WABA as separate, retryable steps. The tenants table had nowhere to record
the WABA id or where the connection stands between those steps.

Approved decision E-D1 = A: four nullable columns on tenants, one connection
per tenant, no whatsapp_connections table.

    waba_id                     String(50)  + partial unique index
    whatsapp_connection_status  String(30)
    waba_connection_source      String(20)
    waba_token_obtained_at      DateTime    (audit metadata, never enforced)

PURELY ADDITIVE. Every column is nullable, nothing is backfilled and no
existing row is read or rewritten -- the primary tenant's manual binding keeps
all four NULL. The partial unique index covers only non-NULL waba_id values,
of which there are none at apply time, so it builds with no cleanup.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e2b7c41d9f63'
down_revision = 'c7e19d4a2b58'
branch_labels = None
depends_on = None

INDEX_NAME = 'uq_tenants_waba_id'
WHERE = "waba_id IS NOT NULL"


def upgrade():
    with op.batch_alter_table('tenants') as batch_op:
        batch_op.add_column(sa.Column('waba_id', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('whatsapp_connection_status', sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column('waba_connection_source', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('waba_token_obtained_at', sa.DateTime(), nullable=True))
    op.create_index(
        INDEX_NAME,
        'tenants',
        ['waba_id'],
        unique=True,
        postgresql_where=sa.text(WHERE),
        sqlite_where=sa.text(WHERE),
    )


def downgrade():
    op.drop_index(INDEX_NAME, table_name='tenants')
    with op.batch_alter_table('tenants') as batch_op:
        batch_op.drop_column('waba_token_obtained_at')
        batch_op.drop_column('waba_connection_source')
        batch_op.drop_column('whatsapp_connection_status')
        batch_op.drop_column('waba_id')
