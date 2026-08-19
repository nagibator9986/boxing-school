"""ORM-модели AINAZAROV TOP TEAM (SQLAlchemy 2.0, ``Mapped`` / ``mapped_column``).

Имена таблиц и колонок — дословно из `INTERFACES.md` §7.1. Правила модуля:

* все имена таблиц в единственном числе;
* ``id`` — UUID, генерирует приложение (``server_default`` для него не используется);
* ``ts`` из контракта — это ``TIMESTAMPTZ`` (``DateTime(timezone=True)``);
* ``JSONB`` объявлен как ``JSON`` с вариантом ``JSONB`` для PostgreSQL — так одна и та же
  модель работает и на Railway (postgres+asyncpg), и на SQLite (тесты, локальный запуск);
* персональные данные (телефон, имя ребёнка, имя родителя) хранятся **открытым текстом**:
  юридический слой исключён из объёма работ, ``TypeDecorator``-ов и шифрования в проекте нет;
* таблиц ``consent_record`` и ``audit_event``, а также колонок ``delete_after`` не существует.

Значения enum-колонок хранятся строками (``Text``): контракт требует именно ``Text``,
а не native enum — добавление нового значения не должно требовать миграции. Преобразование
``Enum -> str`` делают репозитории.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Final
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# --------------------------------------------------------------------------- #
# Общие типы и соглашения
# --------------------------------------------------------------------------- #

#: Соглашение об именах ограничений. Нужно, чтобы у индексов и внешних ключей были
#: предсказуемые имена (иначе ``batch_alter_table`` на SQLite не умеет их дропать).
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: JSONB на PostgreSQL, обычный JSON на SQLite — [РЕШЕНО] INTERFACES §18 п.5.
#: ``none_as_null=True``: Python ``None`` пишется как SQL NULL, а не как JSON-литерал
#: ``null`` — иначе ``WHERE gemini_content IS NULL`` перестаёт различать строки.
JSON_TYPE: Final[sa.types.TypeEngine[Any]] = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(astext_type=sa.Text(), none_as_null=True), "postgresql"
)

#: ``ts`` из контракта — TIMESTAMPTZ.
TS: Final[sa.types.TypeEngine[datetime]] = sa.DateTime(timezone=True)


def utcnow() -> datetime:
    """Текущее время в UTC с таймзоной. Единственный источник времени для дефолтов."""
    return datetime.now(tz=timezone.utc)


class Base(DeclarativeBase):
    """Базовый класс всех моделей. ``metadata`` отдаётся alembic как ``target_metadata``."""

    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


# --------------------------------------------------------------------------- #
# conversation
# --------------------------------------------------------------------------- #
class Conversation(Base):
    """Диалог с одним контактом в одном канале. Естественный ключ — ``conv_key``."""

    __tablename__ = "conversation"
    __table_args__ = (
        # Основной путь чтения пайплайна: найти диалог по каналу и чату.
        sa.Index("ix_conversation_channel_chat", "channel_id", "chat_id"),
        sa.Index("ix_conversation_last_inbound", "last_inbound_at"),
        # Выборка кандидатов на follow-up: незаблокированные, по стадии.
        sa.Index("ix_conversation_followup", "followup_blocked", "followup_stage"),
    )

    id: Mapped[UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid4)
    conv_key: Mapped[str] = mapped_column(sa.Text(), nullable=False, unique=True)
    channel_id: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    chat_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    chat_id: Mapped[str] = mapped_column(sa.Text(), nullable=False)

    contact_name: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    instagram_username: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    phone_e164: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    lang: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    lang_locked: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default=sa.false()
    )
    state: Mapped[str] = mapped_column(
        sa.Text(), nullable=False, default="new", server_default=sa.text("'new'")
    )
    summary: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    kb_hash_at_start: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    msg_in_count: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default=sa.text("0")
    )
    msg_out_count: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default=sa.text("0")
    )
    bot_miss_count: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default=sa.text("0")
    )

    first_inbound_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_outbound_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    #: Instagram: окно ответа +7 дней от последнего входящего.
    service_window_until: Mapped[datetime | None] = mapped_column(TS, nullable=True)

    followup_stage: Mapped[int] = mapped_column(
        sa.SmallInteger(), nullable=False, default=0, server_default=sa.text("0")
    )
    followup_blocked: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default=sa.false()
    )

    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        TS, nullable=False, default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------- #
# message
# --------------------------------------------------------------------------- #
class Message(Base):
    """Одно сообщение диалога либо один элемент истории Gemini.

    Строки с непустым ``gemini_content`` — это дамп ``types.Content`` (история модели),
    строки с непустым ``text_raw`` — реальная переписка. Одна строка может быть и тем и другим.
    """

    __tablename__ = "message"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.id"],
            name="fk_message_conversation_id_conversation",
        ),
        sa.Index("ix_message_conv_created", "conversation_id", "created_at"),
        sa.Index("ux_message_wazzup_id", "wazzup_message_id", unique=True),
        sa.Index("ux_message_crm_id", "crm_message_id", unique=True),
        # Частичный индекс: строк с ошибкой мало, полный индекс по status не нужен.
        sa.Index(
            "ix_message_error",
            "conversation_id",
            "created_at",
            postgresql_where=sa.text("status = 'error'"),
            sqlite_where=sa.text("status = 'error'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(sa.Uuid(), nullable=False)

    direction: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    author: Mapped[str] = mapped_column(sa.Text(), nullable=False)

    #: Ключ дедупликации входящих (``messages[].messageId`` Wazzup).
    wazzup_message_id: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    #: Наш идемпотентный ключ отправки.
    crm_message_id: Mapped[UUID | None] = mapped_column(sa.Uuid(), nullable=True)

    gemini_role: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    gemini_content: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE, nullable=True)

    msg_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    text_raw: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    content_uri: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    status: Mapped[str] = mapped_column(sa.Text(), nullable=False)

    error_code: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    error_description: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    is_echo: Mapped[bool | None] = mapped_column(sa.Boolean(), nullable=True)
    sent_from_app: Mapped[bool | None] = mapped_column(sa.Boolean(), nullable=True)
    author_name: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    author_id: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    channel_dt: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)


# --------------------------------------------------------------------------- #
# lead
# --------------------------------------------------------------------------- #
class Lead(Base):
    """Лид-карточка. Один активный лид на диалог: повторный вызов обновляет строку."""

    __tablename__ = "lead"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.id"],
            name="fk_lead_conversation_id_conversation",
        ),
        sa.Index("ux_lead_conversation", "conversation_id", unique=True),
        # Витрина «последние лиды» и выборка по статусу за период.
        sa.Index("ix_lead_created_at", "created_at"),
        sa.Index("ix_lead_status_created", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(sa.Uuid(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        TS, nullable=False, default=utcnow, onupdate=utcnow
    )

    channel: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    channel_user: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    instagram_username: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    lang: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    parent_name: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    parent_relation: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    phone: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    phone_source: Mapped[str] = mapped_column(
        sa.Text(), nullable=False, default="none", server_default=sa.text("'none'")
    )

    child_name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    child_age: Mapped[int] = mapped_column(sa.SmallInteger(), nullable=False)
    child_birth_year: Mapped[int | None] = mapped_column(sa.SmallInteger(), nullable=True)
    child_gender: Mapped[str] = mapped_column(
        sa.Text(), nullable=False, default="unknown", server_default=sa.text("'unknown'")
    )

    district: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    gym_id: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    trial_slot: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    trial_slot_text: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    motivation: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    main_objection: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    prior_experience: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    health_notes: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    status: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    escalation: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default=sa.false()
    )
    dialog_url: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    messages_count: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default=sa.text("0")
    )
    notified_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


# --------------------------------------------------------------------------- #
# escalation_state
# --------------------------------------------------------------------------- #
class EscalationState(Base):
    """Пауза бота и след оператора. PK совпадает с диалогом: строка не более одной."""

    __tablename__ = "escalation_state"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.id"],
            name="fk_escalation_state_conversation_id_conversation",
        ),
        sa.Index("ix_escalation_state_paused_until", "paused_until"),
    )

    conversation_id: Mapped[UUID] = mapped_column(sa.Uuid(), primary_key=True)
    paused: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default=sa.false()
    )
    paused_until: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    pause_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    operator_last_seen_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    operator_author_id: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    operator_author_name: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    escalation_count: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default=sa.text("0")
    )
    last_escalated_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    manager_notified_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)

    resume_policy: Mapped[str] = mapped_column(
        sa.Text(), nullable=False, default="timeout", server_default=sa.text("'timeout'")
    )
    resumed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


# --------------------------------------------------------------------------- #
# gym
# --------------------------------------------------------------------------- #
class Gym(Base):
    """Read-model зала: зеркало ``kb/gyms.yaml``.

    Перезаписывается целиком и только загрузчиком KB, в одной транзакции.
    [РЕШЕНО] имя таблицы — ``gym`` (единственное число), ``district_aliases`` — JSONB,
    чтобы модель работала и на SQLite.
    """

    __tablename__ = "gym"
    __table_args__ = (sa.Index("ix_gym_scope_active", "scope", "active"),)

    id: Mapped[str] = mapped_column(sa.Text(), primary_key=True)
    scope: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    settlement: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    is_head: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default=sa.false()
    )
    active: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=True, server_default=sa.true()
    )
    status: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    title_ru: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    title_kk: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    address_ru: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    address_kk: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    landmark_ru: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    landmark_kk: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    district_ru: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    district_kk: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    district_aliases: Mapped[list[str] | None] = mapped_column(JSON_TYPE, nullable=True)

    lat: Mapped[Decimal | None] = mapped_column(sa.Numeric(9, 6), nullable=True)
    lon: Mapped[Decimal | None] = mapped_column(sa.Numeric(9, 6), nullable=True)
    map_url: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    phone: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    has_schedule: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=False, server_default=sa.false()
    )
    kb_hash: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)


# --------------------------------------------------------------------------- #
# processed_webhook
# --------------------------------------------------------------------------- #
class ProcessedWebhook(Base):
    """Долговременный дедуп вебхуков (страховка на случай, если Redis потерял ключ).

    Для статусов ключ составной строкой: ``f"{message_id}:{status}"``.
    """

    __tablename__ = "processed_webhook"

    message_id: Mapped[str] = mapped_column(sa.Text(), primary_key=True)
    kind: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)


# --------------------------------------------------------------------------- #
# outbox_message
# --------------------------------------------------------------------------- #
class OutboxMessage(Base):
    """Строка исходящей очереди. Отправку выполняет воркер, а не пайплайн."""

    __tablename__ = "outbox_message"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.id"],
            name="fk_outbox_message_conversation_id_conversation",
        ),
        sa.Index("ux_outbox_crm_message_id", "crm_message_id", unique=True),
        # Выборка неотправленного: WHERE state=... ORDER BY next_attempt_at.
        sa.Index("ix_outbox_pending", "state", "next_attempt_at"),
        # exists_by_wazzup_message_id — горячий путь детектора оператора.
        sa.Index("ix_outbox_wazzup_message_id", "wazzup_message_id"),
    )

    id: Mapped[UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    crm_message_id: Mapped[UUID] = mapped_column(sa.Uuid(), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)

    state: Mapped[str] = mapped_column(
        sa.Text(), nullable=False, default="pending", server_default=sa.text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default=sa.text("0")
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    wazzup_message_id: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        TS, nullable=False, default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------- #
# llm_call
# --------------------------------------------------------------------------- #
class LLMCall(Base):
    """Телеметрия одного вызова модели: токены, кэш, латентность, стоимость."""

    __tablename__ = "llm_call"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.id"],
            name="fk_llm_call_conversation_id_conversation",
        ),
        sa.Index("ix_llm_call_created_at", "created_at"),
        sa.Index("ix_llm_call_conversation", "conversation_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    model: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    prompt_tokens: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default=sa.text("0")
    )
    cached_tokens: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default=sa.text("0")
    )
    candidates_tokens: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default=sa.text("0")
    )
    thoughts_tokens: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default=sa.text("0")
    )
    latency_ms: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0, server_default=sa.text("0")
    )
    cost_usd: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 6), nullable=True)
    tool_calls: Mapped[Any | None] = mapped_column(JSON_TYPE, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    kb_hash: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)


# --------------------------------------------------------------------------- #
# followup_task
# --------------------------------------------------------------------------- #
class FollowupTask(Base):
    """Запланированное напоминание. ``kind`` — значение ``FollowupKind``."""

    __tablename__ = "followup_task"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.id"],
            name="fk_followup_task_conversation_id_conversation",
        ),
        # Выборка «что пора отправить».
        sa.Index("ix_followup_task_due", "state", "run_at"),
        sa.Index("ix_followup_task_conversation", "conversation_id"),
    )

    id: Mapped[UUID] = mapped_column(sa.Uuid(), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(sa.Uuid(), nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    run_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    state: Mapped[str] = mapped_column(
        sa.Text(), nullable=False, default="pending", server_default=sa.text("'pending'")
    )
    attempt: Mapped[int] = mapped_column(
        sa.SmallInteger(), nullable=False, default=0, server_default=sa.text("0")
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)


# --------------------------------------------------------------------------- #
# kb_version
# --------------------------------------------------------------------------- #
class KBVersion(Base):
    """След загруженного снимка базы знаний: по ``hash`` видно, на чём отвечал бот."""

    __tablename__ = "kb_version"

    hash: Mapped[str] = mapped_column(sa.Text(), primary_key=True)
    loaded_at: Mapped[datetime] = mapped_column(TS, nullable=False, default=utcnow)
    files: Mapped[Any | None] = mapped_column(JSON_TYPE, nullable=True)
    valid: Mapped[bool] = mapped_column(
        sa.Boolean(), nullable=False, default=True, server_default=sa.true()
    )


__all__ = [
    "Base",
    "Conversation",
    "EscalationState",
    "FollowupTask",
    "Gym",
    "JSON_TYPE",
    "KBVersion",
    "LLMCall",
    "Lead",
    "Message",
    "NAMING_CONVENTION",
    "OutboxMessage",
    "ProcessedWebhook",
    "TS",
    "utcnow",
]
