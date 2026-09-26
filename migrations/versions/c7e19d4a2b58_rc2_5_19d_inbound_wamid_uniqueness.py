"""RC2.5.19-D — inbound WhatsApp message id uniqueness

Revision ID: c7e19d4a2b58
Revises: a4f2c70b19de
Create Date: 2026-09-26

WHY
---
The webhook deduplicated inbound messages with a SELECT on
conversation_message.wa_message_id, but the row it looked for was written by a
daemon thread started part-way through processing, and the column carried
only a NON-unique index (idx_conv_msg_wa_id). A duplicate delivery arriving
before that thread committed was processed twice: a second AI reply to the
customer, duplicate lead events, a duplicate Sheets row.

RC2.5.19-D makes the webhook insert the inbound row synchronously, before any
side effect, as a CLAIM. This index is what makes the claim atomic: a second
insert of the same inbound wamid fails, and the loser stops.

SCOPE OF THE CONSTRAINT
-----------------------
Partial: only rows WHERE wa_message_id IS NOT NULL AND direction = 'incoming'.
Outgoing rows are untouched, and rows without an id stay unconstrained. The
existing non-unique idx_conv_msg_wa_id is kept.

PRE-CHECK (read-only, production, 2026-09-26, alembic head a4f2c70b19de)
------------------------------------------------------------------------
    incoming rows with a non-null wa_message_id   1168
    duplicate wamid groups                        0
    incoming rows with wa_message_id = ''         0
So the index builds with zero cleanup. If a re-check before execution finds
duplicates, this migration fails rather than choosing a row to delete; no data
is modified by it.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c7e19d4a2b58'
down_revision = 'a4f2c70b19de'
branch_labels = None
depends_on = None

INDEX_NAME = 'uq_conv_msg_incoming_wa_message_id'
WHERE = "wa_message_id IS NOT NULL AND direction = 'incoming'"


def upgrade():
    op.create_index(
        INDEX_NAME,
        'conversation_message',
        ['wa_message_id'],
        unique=True,
        postgresql_where=sa.text(WHERE),
        sqlite_where=sa.text(WHERE),
    )


def downgrade():
    op.drop_index(INDEX_NAME, table_name='conversation_message')
