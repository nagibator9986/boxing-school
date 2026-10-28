"""Номер попытки первого сообщения возврата: «Отправить снова» после недоставки.

Revision ID: 0004_campaign_send_attempt
Revises: 0003_campaign_draft_source
Create Date: 2026-10-28

На Railway колонку добавляет ``app/storage/upgrade.py`` при запуске; ревизия — для тех,
кто накатывает базу через ``make migrate``. Идемпотентна, как и прежние.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "0004_campaign_send_attempt"
down_revision: Union[str, None] = "0003_campaign_draft_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not context.is_offline_mode():
        columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("campaign_contact")}
        if "send_attempt" in columns:
            return
    op.add_column("campaign_contact", sa.Column("send_attempt", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaign_contact", "send_attempt")
