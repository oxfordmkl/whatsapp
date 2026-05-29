"""Phase 5A conversation message CRM timeline

Revision ID: a1b2c3d4e5f6
Revises: d3c2ce4aa446
Create Date: 2026-05-29 10:49:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'd3c2ce4aa446'
branch_labels = None
depends_on = None


def upgrade():
    # ### Phase 5A: Structured CRM message timeline ###
    op.create_table('conversation_message',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('phone', sa.String(length=20), nullable=False),
    sa.Column('direction', sa.String(length=10), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('message_type', sa.String(length=20), nullable=True),
    sa.Column('source', sa.String(length=20), nullable=True),
    sa.Column('staff_name', sa.String(length=100), nullable=True),
    sa.Column('wa_message_id', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('conversation_message', schema=None) as batch_op:
        batch_op.create_index('idx_conv_msg_phone_created', ['phone', 'created_at'], unique=False)
        batch_op.create_index('idx_conv_msg_wa_id', ['wa_message_id'], unique=False)

    # ### end Alembic commands ###


def downgrade():
    # ### Phase 5A rollback ###
    with op.batch_alter_table('conversation_message', schema=None) as batch_op:
        batch_op.drop_index('idx_conv_msg_wa_id')
        batch_op.drop_index('idx_conv_msg_phone_created')

    op.drop_table('conversation_message')

    # ### end Alembic commands ###
