"""Репозиторий диалогов.

Только доступ к данным: ни бизнес-правил, ни решений о паузе, языке и эскалации.
Все функции принимают ``session`` первым параметром, транзакцию не открывают
и не коммитят — этим управляет вызывающий через :func:`app.storage.db.session_scope`.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.db import insert_or_ignore
from app.storage.models import Conversation, utcnow
from app.types import ConversationState, InboundMessage, Language, StorageError

__all__ = [
    "bump_counters",
    "get_by_conv_key",
    "get_by_id",
    "get_or_create",
    "set_bot_miss",
    "set_followup",
    "set_kb_hash_at_start",
    "set_state",
    "set_summary",
    "touch_inbound",
    "touch_outbound",
    "update_language",
]


def _val(value: Enum | str | None) -> str | None:
    """Строковое значение enum'а (или строки как есть)."""
    if value is None:
        return None
    return value.value if isinstance(value, Enum) else value


async def get_by_conv_key(session: AsyncSession, conv_key: str) -> Conversation | None:
    """Находит диалог по естественному ключу ``{channel_id}:{chat_type}:{chat_id}``."""
    stmt = sa.select(Conversation).where(Conversation.conv_key == conv_key)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_by_id(session: AsyncSession, conv_id: UUID) -> Conversation | None:
    """Находит диалог по первичному ключу."""
    return await session.get(Conversation, conv_id)


async def get_or_create(
    session: AsyncSession, *, inbound: InboundMessage
) -> tuple[Conversation, bool]:
    """Возвращает диалог входящего сообщения и признак «создан только что».

    Вставка идёт через ``INSERT ... ON CONFLICT DO NOTHING`` по ``conv_key``: два
    параллельных вебхука одного контакта не должны порождать два диалога.
    У существующего диалога дополняются контактные поля, если они наконец приехали.
    """
    existing = await get_by_conv_key(session, inbound.conv_key)
    if existing is not None:
        _fill_contact(existing, inbound)
        return existing, False

    now = utcnow()
    values: dict[str, Any] = {
        "id": uuid4(),
        "conv_key": inbound.conv_key,
        "channel_id": inbound.channel_id,
        "chat_type": inbound.channel.value,
        "chat_id": inbound.chat_id,
        "contact_name": inbound.contact_name,
        "instagram_username": inbound.contact_username,
        "phone_e164": inbound.phone_e164,
        "state": ConversationState.NEW.value,
        "first_inbound_at": inbound.received_at,
        "last_inbound_at": inbound.received_at,
        "created_at": now,
        "updated_at": now,
    }
    result = await session.execute(
        insert_or_ignore(session, Conversation, values, index_elements=["conv_key"])
    )
    created = bool(result.rowcount)

    conversation = await get_by_conv_key(session, inbound.conv_key)
    if conversation is None:  # pragma: no cover - защитная ветка
        raise StorageError(f"Диалог {inbound.conv_key} не создан и не найден")
    if not created:
        _fill_contact(conversation, inbound)
    return conversation, created


def _fill_contact(conversation: Conversation, inbound: InboundMessage) -> None:
    """Дозаполняет контактные поля диалога тем, что пришло в вебхуке."""
    if inbound.contact_name and not conversation.contact_name:
        conversation.contact_name = inbound.contact_name
    if inbound.contact_username and not conversation.instagram_username:
        conversation.instagram_username = inbound.contact_username
    if inbound.phone_e164 and not conversation.phone_e164:
        conversation.phone_e164 = inbound.phone_e164


async def update_language(
    session: AsyncSession, conv_id: UUID, *, lang: Language, locked: bool
) -> None:
    """Записывает язык диалога и признак «язык зафиксирован клиентом»."""
    await session.execute(
        sa.update(Conversation)
        .where(Conversation.id == conv_id)
        .values(lang=lang.value, lang_locked=locked, updated_at=utcnow())
    )


async def set_state(session: AsyncSession, conv_id: UUID, state: ConversationState) -> None:
    """Переводит диалог в новое состояние."""
    await session.execute(
        sa.update(Conversation)
        .where(Conversation.id == conv_id)
        .values(state=_val(state), updated_at=utcnow())
    )


async def bump_counters(
    session: AsyncSession, conv_id: UUID, *, msg_in: int = 0, msg_out: int = 0
) -> None:
    """Увеличивает счётчики сообщений атомарно, без чтения строки."""
    if not msg_in and not msg_out:
        return
    await session.execute(
        sa.update(Conversation)
        .where(Conversation.id == conv_id)
        .values(
            msg_in_count=Conversation.msg_in_count + msg_in,
            msg_out_count=Conversation.msg_out_count + msg_out,
            updated_at=utcnow(),
        )
    )


async def set_bot_miss(session: AsyncSession, conv_id: UUID, value: int) -> None:
    """Ставит счётчик промахов бота. Достижение ``bot_miss_limit`` разбирает пайплайн."""
    await session.execute(
        sa.update(Conversation)
        .where(Conversation.id == conv_id)
        .values(bot_miss_count=max(0, value), updated_at=utcnow())
    )


async def touch_inbound(
    session: AsyncSession, conv_id: UUID, at: datetime, *, window_until: datetime | None
) -> None:
    """Отмечает момент последнего входящего и границу сервисного окна канала."""
    values: dict[str, Any] = {"last_inbound_at": at, "updated_at": utcnow()}
    if window_until is not None:
        values["service_window_until"] = window_until
    await session.execute(
        sa.update(Conversation).where(Conversation.id == conv_id).values(**values)
    )


async def touch_outbound(session: AsyncSession, conv_id: UUID, at: datetime) -> None:
    """Отмечает момент последнего исходящего (нужно follow-up'у и метрикам)."""
    await session.execute(
        sa.update(Conversation)
        .where(Conversation.id == conv_id)
        .values(last_outbound_at=at, updated_at=utcnow())
    )


async def set_summary(session: AsyncSession, conv_id: UUID, summary: str) -> None:
    """Сохраняет краткое резюме диалога (сжатая история для промпта)."""
    await session.execute(
        sa.update(Conversation)
        .where(Conversation.id == conv_id)
        .values(summary=summary, updated_at=utcnow())
    )


async def set_followup(
    session: AsyncSession,
    conv_id: UUID,
    *,
    stage: int | None = None,
    blocked: bool | None = None,
) -> None:
    """Частично обновляет состояние follow-up: стадию и/или блокировку."""
    values: dict[str, Any] = {}
    if stage is not None:
        values["followup_stage"] = stage
    if blocked is not None:
        values["followup_blocked"] = blocked
    if not values:
        return
    values["updated_at"] = utcnow()
    await session.execute(
        sa.update(Conversation).where(Conversation.id == conv_id).values(**values)
    )


async def set_kb_hash_at_start(session: AsyncSession, conv_id: UUID, kb_hash: str) -> None:
    """Фиксирует версию KB, на которой диалог начался (для разбора жалоб)."""
    await session.execute(
        sa.update(Conversation)
        .where(Conversation.id == conv_id, Conversation.kb_hash_at_start.is_(None))
        .values(kb_hash_at_start=kb_hash, updated_at=utcnow())
    )
