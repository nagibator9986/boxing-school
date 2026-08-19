"""Первая ревизия: все таблицы проекта.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-10

Особенности:

* ревизия идемпотентна — существующие таблицы не пересоздаются. На Railway
  ``alembic upgrade head`` выполняется при каждом деплое, и база может оказаться
  уже накатанной (например, поднятой через ``metadata.create_all`` в раннем пилоте);
* ``JSONB`` объявлен через ``with_variant``: PostgreSQL получает JSONB, SQLite — JSON;
* таблиц согласий, аудита и колонок сроков хранения нет — юридический слой исключён
  из объёма работ (docs/SCOPE-OVERRIDE.md §1).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: TIMESTAMPTZ контракта.
TS = sa.DateTime(timezone=True)


def _json() -> sa.types.TypeEngine:
    """JSONB на PostgreSQL, JSON на SQLite."""
    return sa.JSON(none_as_null=True).with_variant(
        postgresql.JSONB(astext_type=sa.Text(), none_as_null=True), "postgresql"
    )


def _existing_tables() -> set[str]:
    """Имена таблиц, которые уже есть в базе.

    В offline-режиме (``alembic upgrade head --sql``) подключения нет и спросить некого —
    считаем базу пустой и печатаем полный DDL.
    """
    if context.is_offline_mode():
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()

    # ----------------------------------------------------------------- conversation
    if "conversation" not in existing:
        op.create_table(
            "conversation",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("conv_key", sa.Text(), nullable=False),
            sa.Column("channel_id", sa.Text(), nullable=False),
            sa.Column("chat_type", sa.Text(), nullable=False),
            sa.Column("chat_id", sa.Text(), nullable=False),
            sa.Column("contact_name", sa.Text(), nullable=True),
            sa.Column("instagram_username", sa.Text(), nullable=True),
            sa.Column("phone_e164", sa.Text(), nullable=True),
            sa.Column("lang", sa.Text(), nullable=True),
            sa.Column("lang_locked", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("state", sa.Text(), server_default=sa.text("'new'"), nullable=False),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("kb_hash_at_start", sa.Text(), nullable=True),
            sa.Column("msg_in_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("msg_out_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("bot_miss_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("first_inbound_at", TS, nullable=True),
            sa.Column("last_inbound_at", TS, nullable=True),
            sa.Column("last_outbound_at", TS, nullable=True),
            sa.Column("service_window_until", TS, nullable=True),
            sa.Column(
                "followup_stage", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
            ),
            sa.Column(
                "followup_blocked", sa.Boolean(), server_default=sa.false(), nullable=False
            ),
            sa.Column("created_at", TS, nullable=False),
            sa.Column("updated_at", TS, nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_conversation"),
            sa.UniqueConstraint("conv_key", name="uq_conversation_conv_key"),
        )
        op.create_index("ix_conversation_channel_chat", "conversation", ["channel_id", "chat_id"])
        op.create_index("ix_conversation_last_inbound", "conversation", ["last_inbound_at"])
        op.create_index(
            "ix_conversation_followup", "conversation", ["followup_blocked", "followup_stage"]
        )

    # ---------------------------------------------------------------------- message
    if "message" not in existing:
        op.create_table(
            "message",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("conversation_id", sa.Uuid(), nullable=False),
            sa.Column("direction", sa.Text(), nullable=False),
            sa.Column("author", sa.Text(), nullable=False),
            sa.Column("wazzup_message_id", sa.Text(), nullable=True),
            sa.Column("crm_message_id", sa.Uuid(), nullable=True),
            sa.Column("gemini_role", sa.Text(), nullable=True),
            sa.Column("gemini_content", _json(), nullable=True),
            sa.Column("msg_type", sa.Text(), nullable=False),
            sa.Column("text_raw", sa.Text(), nullable=True),
            sa.Column("content_uri", sa.Text(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("error_code", sa.Text(), nullable=True),
            sa.Column("error_description", sa.Text(), nullable=True),
            sa.Column("is_echo", sa.Boolean(), nullable=True),
            sa.Column("sent_from_app", sa.Boolean(), nullable=True),
            sa.Column("author_name", sa.Text(), nullable=True),
            sa.Column("author_id", sa.Text(), nullable=True),
            sa.Column("channel_dt", TS, nullable=True),
            sa.Column("created_at", TS, nullable=False),
            sa.ForeignKeyConstraint(
                ["conversation_id"],
                ["conversation.id"],
                name="fk_message_conversation_id_conversation",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_message"),
        )
        op.create_index("ix_message_conv_created", "message", ["conversation_id", "created_at"])
        op.create_index("ux_message_wazzup_id", "message", ["wazzup_message_id"], unique=True)
        op.create_index("ux_message_crm_id", "message", ["crm_message_id"], unique=True)
        op.create_index(
            "ix_message_error",
            "message",
            ["conversation_id", "created_at"],
            postgresql_where=sa.text("status = 'error'"),
            sqlite_where=sa.text("status = 'error'"),
        )

    # ------------------------------------------------------------------------- lead
    if "lead" not in existing:
        op.create_table(
            "lead",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("conversation_id", sa.Uuid(), nullable=False),
            sa.Column("created_at", TS, nullable=False),
            sa.Column("updated_at", TS, nullable=False),
            sa.Column("channel", sa.Text(), nullable=True),
            sa.Column("channel_user", sa.Text(), nullable=True),
            sa.Column("instagram_username", sa.Text(), nullable=True),
            sa.Column("lang", sa.Text(), nullable=True),
            sa.Column("parent_name", sa.Text(), nullable=True),
            sa.Column("parent_relation", sa.Text(), nullable=True),
            sa.Column("phone", sa.Text(), nullable=True),
            sa.Column(
                "phone_source", sa.Text(), server_default=sa.text("'none'"), nullable=False
            ),
            sa.Column("child_name", sa.Text(), nullable=False),
            sa.Column("child_age", sa.SmallInteger(), nullable=False),
            sa.Column("child_birth_year", sa.SmallInteger(), nullable=True),
            sa.Column(
                "child_gender", sa.Text(), server_default=sa.text("'unknown'"), nullable=False
            ),
            sa.Column("district", sa.Text(), nullable=True),
            sa.Column("gym_id", sa.Text(), nullable=True),
            sa.Column("trial_slot", TS, nullable=True),
            sa.Column("trial_slot_text", sa.Text(), nullable=True),
            sa.Column("motivation", sa.Text(), nullable=True),
            sa.Column("main_objection", sa.Text(), nullable=True),
            sa.Column("prior_experience", sa.Text(), nullable=True),
            sa.Column("health_notes", sa.Text(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("escalation", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("dialog_url", sa.Text(), nullable=True),
            sa.Column(
                "messages_count", sa.Integer(), server_default=sa.text("0"), nullable=False
            ),
            sa.Column("notified_at", TS, nullable=True),
            sa.ForeignKeyConstraint(
                ["conversation_id"],
                ["conversation.id"],
                name="fk_lead_conversation_id_conversation",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_lead"),
        )
        op.create_index("ux_lead_conversation", "lead", ["conversation_id"], unique=True)
        op.create_index("ix_lead_created_at", "lead", ["created_at"])
        op.create_index("ix_lead_status_created", "lead", ["status", "created_at"])

    # -------------------------------------------------------------- escalation_state
    if "escalation_state" not in existing:
        op.create_table(
            "escalation_state",
            sa.Column("conversation_id", sa.Uuid(), nullable=False),
            sa.Column("paused", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("paused_until", TS, nullable=True),
            sa.Column("pause_reason", sa.Text(), nullable=True),
            sa.Column("operator_last_seen_at", TS, nullable=True),
            sa.Column("operator_author_id", sa.Text(), nullable=True),
            sa.Column("operator_author_name", sa.Text(), nullable=True),
            sa.Column(
                "escalation_count", sa.Integer(), server_default=sa.text("0"), nullable=False
            ),
            sa.Column("last_escalated_at", TS, nullable=True),
            sa.Column("manager_notified_at", TS, nullable=True),
            sa.Column(
                "resume_policy", sa.Text(), server_default=sa.text("'timeout'"), nullable=False
            ),
            sa.Column("resumed_at", TS, nullable=True),
            sa.ForeignKeyConstraint(
                ["conversation_id"],
                ["conversation.id"],
                name="fk_escalation_state_conversation_id_conversation",
            ),
            sa.PrimaryKeyConstraint("conversation_id", name="pk_escalation_state"),
        )
        op.create_index("ix_escalation_state_paused_until", "escalation_state", ["paused_until"])

    # -------------------------------------------------------------------------- gym
    if "gym" not in existing:
        op.create_table(
            "gym",
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("scope", sa.Text(), nullable=True),
            sa.Column("settlement", sa.Text(), nullable=True),
            sa.Column("is_head", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("status", sa.Text(), nullable=True),
            sa.Column("title_ru", sa.Text(), nullable=True),
            sa.Column("title_kk", sa.Text(), nullable=True),
            sa.Column("address_ru", sa.Text(), nullable=True),
            sa.Column("address_kk", sa.Text(), nullable=True),
            sa.Column("landmark_ru", sa.Text(), nullable=True),
            sa.Column("landmark_kk", sa.Text(), nullable=True),
            sa.Column("district_ru", sa.Text(), nullable=True),
            sa.Column("district_kk", sa.Text(), nullable=True),
            sa.Column("district_aliases", _json(), nullable=True),
            sa.Column("lat", sa.Numeric(precision=9, scale=6), nullable=True),
            sa.Column("lon", sa.Numeric(precision=9, scale=6), nullable=True),
            sa.Column("map_url", sa.Text(), nullable=True),
            sa.Column("phone", sa.Text(), nullable=True),
            sa.Column("has_schedule", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("kb_hash", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id", name="pk_gym"),
        )
        op.create_index("ix_gym_scope_active", "gym", ["scope", "active"])

    # ------------------------------------------------------------- processed_webhook
    if "processed_webhook" not in existing:
        op.create_table(
            "processed_webhook",
            sa.Column("message_id", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("first_seen_at", TS, nullable=False),
            sa.PrimaryKeyConstraint("message_id", name="pk_processed_webhook"),
        )

    # --------------------------------------------------------------- outbox_message
    if "outbox_message" not in existing:
        op.create_table(
            "outbox_message",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("conversation_id", sa.Uuid(), nullable=True),
            sa.Column("crm_message_id", sa.Uuid(), nullable=False),
            sa.Column("payload", _json(), nullable=False),
            sa.Column("state", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
            sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("next_attempt_at", TS, nullable=True),
            sa.Column("wazzup_message_id", sa.Text(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", TS, nullable=False),
            sa.Column("updated_at", TS, nullable=False),
            sa.ForeignKeyConstraint(
                ["conversation_id"],
                ["conversation.id"],
                name="fk_outbox_message_conversation_id_conversation",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_outbox_message"),
        )
        op.create_index(
            "ux_outbox_crm_message_id", "outbox_message", ["crm_message_id"], unique=True
        )
        op.create_index("ix_outbox_pending", "outbox_message", ["state", "next_attempt_at"])
        op.create_index("ix_outbox_wazzup_message_id", "outbox_message", ["wazzup_message_id"])

    # ---------------------------------------------------------------------- llm_call
    if "llm_call" not in existing:
        op.create_table(
            "llm_call",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("conversation_id", sa.Uuid(), nullable=True),
            sa.Column("model", sa.Text(), nullable=True),
            sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("cached_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column(
                "candidates_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False
            ),
            sa.Column(
                "thoughts_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False
            ),
            sa.Column("latency_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("cost_usd", sa.Numeric(precision=10, scale=6), nullable=True),
            sa.Column("tool_calls", _json(), nullable=True),
            sa.Column("finish_reason", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("kb_hash", sa.Text(), nullable=True),
            sa.Column("created_at", TS, nullable=False),
            sa.ForeignKeyConstraint(
                ["conversation_id"],
                ["conversation.id"],
                name="fk_llm_call_conversation_id_conversation",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_llm_call"),
        )
        op.create_index("ix_llm_call_created_at", "llm_call", ["created_at"])
        op.create_index("ix_llm_call_conversation", "llm_call", ["conversation_id", "created_at"])

    # ----------------------------------------------------------------- followup_task
    if "followup_task" not in existing:
        op.create_table(
            "followup_task",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("conversation_id", sa.Uuid(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("run_at", TS, nullable=False),
            sa.Column("state", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
            sa.Column("attempt", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
            sa.Column("created_at", TS, nullable=False),
            sa.ForeignKeyConstraint(
                ["conversation_id"],
                ["conversation.id"],
                name="fk_followup_task_conversation_id_conversation",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_followup_task"),
        )
        op.create_index("ix_followup_task_due", "followup_task", ["state", "run_at"])
        op.create_index("ix_followup_task_conversation", "followup_task", ["conversation_id"])

    # -------------------------------------------------------------------- kb_version
    if "kb_version" not in existing:
        op.create_table(
            "kb_version",
            sa.Column("hash", sa.Text(), nullable=False),
            sa.Column("loaded_at", TS, nullable=False),
            sa.Column("files", _json(), nullable=True),
            sa.Column("valid", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.PrimaryKeyConstraint("hash", name="pk_kb_version"),
        )


def downgrade() -> None:
    """Полный откат. Порядок обратный созданию: сначала зависимые таблицы."""
    for table in (
        "kb_version",
        "followup_task",
        "llm_call",
        "outbox_message",
        "processed_webhook",
        "gym",
        "escalation_state",
        "lead",
        "message",
        "conversation",
    ):
        op.drop_table(table)
