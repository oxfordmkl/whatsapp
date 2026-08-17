"""RC2.4.2 — WABA identity uniqueness

Revision ID: b8f4c2e97d15
Revises: a3d7f21c94e8
Create Date: 2026-08-17

WHY
---
tenants.waba_phone_number_id carried no constraint and no index, and the
settings save path validated only .isdigit() — no duplicate check of any kind.
Any tenant's own ADMIN could therefore enter another tenant's Meta Phone Number
ID through /tenant/whatsapp/save.

The inbound webhook resolves the tenant with

    Tenant.query.filter_by(waba_phone_number_id=phone_number_id).first()

on that non-unique column with no ORDER BY, so two matching rows would be
resolved arbitrarily and one tenant could begin receiving another tenant's
customer conversations. That is the same class of defect as the
Tenant.query.first() mis-filing traced in RC2.4.0.

PARTIAL, NOT A PLAIN UNIQUE CONSTRAINT
--------------------------------------
PostgreSQL's default is NULLS DISTINCT, so a plain UNIQUE would in fact permit
the 11 unconfigured tenants to keep their NULLs. The partial index is chosen
anyway because it is smaller, states the intent explicitly, and does not
require the reader to know that default. A blank string is NOT covered by NULL
semantics; production holds 0 blanks and the save path rejects them.

DATA STATE AT AUTHORING (verified read-only against production)
---------------------------------------------------------------
    tenants                       12
    non-null waba_phone_number_id  1   (The Oxford Computers)
    duplicates                     0
    blank strings                  0
    half-configured rows           0
    SUSPENDED holding an ID        0

So this applies with zero cleanup and zero downtime.
"""
from alembic import op
import sqlalchemy as sa

revision = 'b8f4c2e97d15'
down_revision = 'a3d7f21c94e8'
branch_labels = None
depends_on = None

INDEX_NAME = 'uq_tenants_waba_phone_number_id'


def upgrade():
    op.create_index(
        INDEX_NAME,
        'tenants',
        ['waba_phone_number_id'],
        unique=True,
        postgresql_where=sa.text('waba_phone_number_id IS NOT NULL'),
    )


def downgrade():
    op.drop_index(INDEX_NAME, table_name='tenants')
