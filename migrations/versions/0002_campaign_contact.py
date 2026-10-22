"""Таблица списка возврата клиентов.

Revision ID: 0002_campaign_contact
Revises: 0001_initial
Create Date: 2026-10-22

На Railway схему создаёт ``metadata.create_all`` при запуске службы; ревизия нужна тем,
кто накатывает базу через ``make migrate``. Как и первая, она идемпотентна.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "0002_campaign_contact"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    if not context.is_offline_mode() and "campaign_contact" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "campaign_contact",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), server_default=sa.text("'whatsapp'"), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=True),
        sa.Column("import_batch", sa.Text(), nullable=True),
        sa.Column("wa_name", sa.Text(), nullable=True),
        sa.Column("parent_name", sa.Text(), nullable=True),
        sa.Column("child_name", sa.Text(), nullable=True),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("segment", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("obstacle", sa.Text(), nullable=True),
        sa.Column("next_step", sa.Text(), nullable=True),
        sa.Column("check_note", sa.Text(), nullable=True),
        sa.Column("check_required", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("trial_info", sa.Text(), nullable=True),
        sa.Column("payment_info", sa.Text(), nullable=True),
        sa.Column("last_contact_at", TS, nullable=True),
        sa.Column("not_before", TS, nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("checked_at", TS, nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("conversation_id", sa.Text(), nullable=True),
        sa.Column("draft_text", sa.Text(), nullable=True),
        sa.Column("approved_text", sa.Text(), nullable=True),
        sa.Column("approved_at", TS, nullable=True),
        sa.Column("sent_at", TS, nullable=True),
        sa.Column("reminder_sent_at", TS, nullable=True),
        sa.Column("replied_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_campaign_contact"),
        sa.UniqueConstraint("chat_id", name="uq_campaign_contact_chat_id"),
    )
    op.create_index("ix_campaign_contact_status", "campaign_contact", ["status"])


def downgrade() -> None:
    op.drop_index("ix_campaign_contact_status", table_name="campaign_contact")
    op.drop_table("campaign_contact")
