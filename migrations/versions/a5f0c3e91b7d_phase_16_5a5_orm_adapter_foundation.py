"""feat: Phase 16.5A5 ORM adapter foundation (schema expansion)

Revision ID: a5f0c3e91b7d
Revises: 258025fe9676
Create Date: 2026-07-16 12:00:00.000000

Phase 16.5A5 schema expansion — approved in Phase 16.5A5-B Discovery Audit.

Adds ONLY the additive schema required before dual-write ORM adapters can be
written:
  CREATE TABLE offering
  CREATE TABLE conversation_state_offerings   (bridge: conversation ↔ offering)
  CREATE TABLE conversation_state_tags        (bridge: conversation ↔ tag)
  ALTER  TABLE conversation_state
           ADD COLUMN pipeline_stage_id  (nullable FK → pipeline_stages.id)
           ADD COLUMN custom_attributes  (Text, JSON blob — SQLite+Postgres safe)

Legacy columns (stage, course, is_admitted, offer_course, batch_time) are
RETAINED and untouched. No destructive operations. Fully reversible.

Batch mode note: op.batch_alter_table is used for the conversation_state ALTER
because SQLite (local/test) cannot ADD a FOREIGN KEY via plain ALTER. On
PostgreSQL (Railway) batch mode with recreate='auto' emits direct
`ALTER TABLE ... ADD COLUMN` / `ADD CONSTRAINT` statements and does NOT rebuild
the table — safe for the high-volume conversation_state table.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a5f0c3e91b7d'
down_revision = '258025fe9676'
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. CREATE TABLE offering ────────────────────────────────────────────
    op.create_table('offering',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.String(length=36), nullable=False),
    sa.Column('internal_key', sa.String(length=50), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'internal_key', name='uq_offering_tenant_key')
    )
    with op.batch_alter_table('offering', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_offering_tenant_id'), ['tenant_id'], unique=False)

    # ── 2. CREATE TABLE conversation_state_offerings (bridge) ───────────────
    op.create_table('conversation_state_offerings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_state_id', sa.Integer(), nullable=False),
    sa.Column('offering_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_state_id'], ['conversation_state.id'], ),
    sa.ForeignKeyConstraint(['offering_id'], ['offering.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('conversation_state_id', 'offering_id', name='uq_conv_offering')
    )
    with op.batch_alter_table('conversation_state_offerings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_conversation_state_offerings_conversation_state_id'), ['conversation_state_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversation_state_offerings_offering_id'), ['offering_id'], unique=False)

    # ── 3. CREATE TABLE conversation_state_tags (bridge) ────────────────────
    op.create_table('conversation_state_tags',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_state_id', sa.Integer(), nullable=False),
    sa.Column('tag_definition_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_state_id'], ['conversation_state.id'], ),
    sa.ForeignKeyConstraint(['tag_definition_id'], ['tag_definitions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('conversation_state_id', 'tag_definition_id', name='uq_conv_tag')
    )
    with op.batch_alter_table('conversation_state_tags', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_conversation_state_tags_conversation_state_id'), ['conversation_state_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_conversation_state_tags_tag_definition_id'), ['tag_definition_id'], unique=False)

    # ── 4. ALTER TABLE conversation_state — add nullable FK + JSON blob ─────
    # pipeline_stages already exists (revision 258025fe9676), so the FK target
    # is valid. All existing rows get pipeline_stage_id = NULL → no FK validation
    # scan, no lock beyond the brief metadata change + index build.
    with op.batch_alter_table('conversation_state', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pipeline_stage_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('custom_attributes', sa.Text(), nullable=True))
        batch_op.create_index(batch_op.f('ix_conversation_state_pipeline_stage_id'), ['pipeline_stage_id'], unique=False)
        batch_op.create_foreign_key('fk_conversation_state_pipeline_stage', 'pipeline_stages', ['pipeline_stage_id'], ['id'])


def downgrade():
    # Reverse dependency order. Legacy conversation_state columns
    # (stage, course, is_admitted, offer_course, batch_time) are NEVER touched.

    # ── Revert conversation_state ALTER first (drop FK before target tables) ─
    with op.batch_alter_table('conversation_state', schema=None) as batch_op:
        batch_op.drop_constraint('fk_conversation_state_pipeline_stage', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_conversation_state_pipeline_stage_id'))
        batch_op.drop_column('custom_attributes')
        batch_op.drop_column('pipeline_stage_id')

    # ── Drop bridge tables (they FK conversation_state + offering/tag) ──────
    with op.batch_alter_table('conversation_state_tags', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_conversation_state_tags_tag_definition_id'))
        batch_op.drop_index(batch_op.f('ix_conversation_state_tags_conversation_state_id'))

    op.drop_table('conversation_state_tags')

    with op.batch_alter_table('conversation_state_offerings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_conversation_state_offerings_offering_id'))
        batch_op.drop_index(batch_op.f('ix_conversation_state_offerings_conversation_state_id'))

    op.drop_table('conversation_state_offerings')

    # ── Drop offering last (bridges reference it) ──────────────────────────
    with op.batch_alter_table('offering', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_offering_tenant_id'))

    op.drop_table('offering')
