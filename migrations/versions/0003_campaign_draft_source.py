"""Откуда черновик возврата: модель, шаблон или правка человека.

Revision ID: 0003_campaign_draft_source
Revises: 0002_campaign_contact
Create Date: 2026-10-27

На Railway колонку добавляет ``app/storage/upgrade.py`` при запуске; ревизия — для тех,
кто накатывает базу через ``make migrate``. Идемпотентна, как и прежние.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "0003_campaign_draft_source"
down_revision: Union[str, None] = "0002_campaign_contact"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not context.is_offline_mode():
        columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("campaign_contact")}
        if "draft_source" in columns:
            return
    op.add_column("campaign_contact", sa.Column("draft_source", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaign_contact", "draft_source")
