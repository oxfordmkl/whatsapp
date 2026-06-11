"""Phase 13-A2B — Identity Schema Migration

Expands the Tenant model with SaaS identity fields and transitions
username uniqueness from a global constraint to a per-tenant composite.

WHAT THIS MIGRATION DOES:
  STEP 1  — Add new Tenant columns (all nullable initially to avoid lock).
  STEP 2  — Backfill the Oxford production tenant.
  STEP 3  — Validation guard: abort if backfill is incomplete.
  STEP 4  — Enforce NOT NULL on required Tenant columns.
  STEP 5  — Create unique index on tenants.slug.
  STEP 6  — Add users.email (nullable, unique).
  STEP 7  — Drop global unique index on users.username.
  STEP 8  — Create composite unique constraint (tenant_id, username).

WHAT THIS MIGRATION DOES NOT DO:
  - Does not touch any other model or table.
  - Does not change any route, service, or auth logic.
  - Does not affect existing ADMIN_KEY access.
  - Does not affect existing WhatsApp bot.

Revision ID: a3f1b2c4d5e6
Revises: 17f210d813df
Create Date: 2026-06-11
"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime

revision = 'a3f1b2c4d5e6'
down_revision = '17f210d813df'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # ══════════════════════════════════════════════════════════════════════
    # STEP 1: Add all new Tenant columns as nullable=True.
    #         Adding as nullable first avoids PostgreSQL lock on ALTER.
    # ══════════════════════════════════════════════════════════════════════
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.add_column(sa.Column('slug',                       sa.String(30),  nullable=True))
        batch_op.add_column(sa.Column('status',                     sa.String(20),  nullable=True))
        batch_op.add_column(sa.Column('plan',                       sa.String(20),  nullable=True))
        batch_op.add_column(sa.Column('trial_ends_at',              sa.DateTime(),  nullable=True))
        batch_op.add_column(sa.Column('billing_email',              sa.String(100), nullable=True))
        batch_op.add_column(sa.Column('industry',                   sa.String(50),  nullable=True))
        batch_op.add_column(sa.Column('waba_phone_number_id',       sa.String(50),  nullable=True))
        batch_op.add_column(sa.Column('waba_access_token_encrypted',sa.Text(),      nullable=True))
        batch_op.add_column(sa.Column('ai_persona_name',            sa.String(50),  nullable=True))
        batch_op.add_column(sa.Column('ai_prompt_override',         sa.Text(),      nullable=True))
        batch_op.add_column(sa.Column('updated_at',                 sa.DateTime(),  nullable=True))

    # ══════════════════════════════════════════════════════════════════════
    # STEP 2: Backfill the existing Oxford production tenant.
    #         This is a single-tenant system — we target all rows (safe).
    #         Oxford receives ENTERPRISE + ACTIVE as a grandfathered client.
    #         slug = 'oxford-computers' (URL-safe, 3–30 chars, lowercase).
    # ══════════════════════════════════════════════════════════════════════
    now_ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(sa.text(
        f"UPDATE tenants SET "
        f"  slug     = 'oxford-computers', "
        f"  status   = 'ACTIVE', "
        f"  plan     = 'ENTERPRISE', "
        f"  industry = 'Education', "
        f"  updated_at = '{now_ts}' "
        f"WHERE slug IS NULL"
    ))

    # ══════════════════════════════════════════════════════════════════════
    # STEP 3: Validation guard — abort the entire migration if any row
    #         still has a NULL in a soon-to-be NOT NULL column.
    # ══════════════════════════════════════════════════════════════════════
    for col in ('slug', 'status', 'plan', 'industry', 'updated_at'):
        null_count = conn.execute(
            sa.text(f"SELECT COUNT(*) FROM tenants WHERE {col} IS NULL")
        ).scalar()
        if null_count > 0:
            raise Exception(
                f"PHASE 13-A2B BACKFILL FAILED: {null_count} row(s) in 'tenants' "
                f"still have NULL '{col}'. Aborting to protect data integrity."
            )

    # ══════════════════════════════════════════════════════════════════════
    # STEP 4: Enforce NOT NULL on the required Tenant columns.
    # ══════════════════════════════════════════════════════════════════════
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.alter_column('slug',       existing_type=sa.String(30), nullable=False)
        batch_op.alter_column('status',     existing_type=sa.String(20), nullable=False)
        batch_op.alter_column('plan',       existing_type=sa.String(20), nullable=False)
        batch_op.alter_column('industry',   existing_type=sa.String(50), nullable=False)
        batch_op.alter_column('updated_at', existing_type=sa.DateTime(), nullable=False)

    # ══════════════════════════════════════════════════════════════════════
    # STEP 5: Create the unique index on tenants.slug.
    #         slug must be globally unique — it is the URL routing key.
    # ══════════════════════════════════════════════════════════════════════
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.create_index('ix_tenants_slug', ['slug'], unique=True)

    # ══════════════════════════════════════════════════════════════════════
    # STEP 6: Add users.email column.
    #         nullable=True — Staff may never have emails.
    #         unique=True   — No two users can share an email across the platform.
    # ══════════════════════════════════════════════════════════════════════
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('email', sa.String(120), nullable=True))
        batch_op.create_index('ix_users_email', ['email'], unique=True)

    # ══════════════════════════════════════════════════════════════════════
    # STEP 7: Drop the global unique index on users.username.
    #         The index was created by batch_op.f('ix_users_username')
    #         in migration 5d03593d42b4_add_users_table.py.
    #         PostgreSQL index name = 'ix_users_username'.
    # ══════════════════════════════════════════════════════════════════════
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index('ix_users_username')

    # ══════════════════════════════════════════════════════════════════════
    # STEP 8: Create composite unique constraint (tenant_id, username).
    #         username is unique WITHIN a tenant — not globally.
    #         Constraint name: 'uq_users_tenant_username' matches models.py.
    # ══════════════════════════════════════════════════════════════════════
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.create_unique_constraint(
            'uq_users_tenant_username',
            ['tenant_id', 'username']
        )


def downgrade():
    # ══════════════════════════════════════════════════════════════════════
    # REVERSE STEP 8: Drop composite username constraint.
    # ══════════════════════════════════════════════════════════════════════
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint('uq_users_tenant_username', type_='unique')

    # ══════════════════════════════════════════════════════════════════════
    # REVERSE STEP 7: Restore global unique index on users.username.
    #   WARNING: If any duplicate usernames were inserted during the window
    #   between upgrade and downgrade, this step will FAIL. Manual cleanup
    #   of duplicates would be required before downgrade can succeed.
    # ══════════════════════════════════════════════════════════════════════
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.create_index('ix_users_username', ['username'], unique=True)

    # ══════════════════════════════════════════════════════════════════════
    # REVERSE STEP 6: Drop users.email.
    # ══════════════════════════════════════════════════════════════════════
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index('ix_users_email')
        batch_op.drop_column('email')

    # ══════════════════════════════════════════════════════════════════════
    # REVERSE STEP 5: Drop unique slug index.
    # ══════════════════════════════════════════════════════════════════════
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_index('ix_tenants_slug')

    # ══════════════════════════════════════════════════════════════════════
    # REVERSE STEPS 4 + 1: Drop all new Tenant columns.
    #   (No need to revert NOT NULL before dropping — column drop handles it.)
    # ══════════════════════════════════════════════════════════════════════
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_column('updated_at')
        batch_op.drop_column('ai_prompt_override')
        batch_op.drop_column('ai_persona_name')
        batch_op.drop_column('waba_access_token_encrypted')
        batch_op.drop_column('waba_phone_number_id')
        batch_op.drop_column('industry')
        batch_op.drop_column('billing_email')
        batch_op.drop_column('trial_ends_at')
        batch_op.drop_column('plan')
        batch_op.drop_column('status')
        batch_op.drop_column('slug')
