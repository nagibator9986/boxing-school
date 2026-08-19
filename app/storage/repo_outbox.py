"""Репозиторий исходящей очереди (outbox).

Пайплайн только кладёт строку, отправляет её воркер. Захват строки обязан быть
атомарным, иначе два воркера отправят клиенту одно и то же сообщение дважды:

* PostgreSQL — ``SELECT ... FOR UPDATE SKIP LOCKED`` плюс перевод состояния;
* SQLite — деградация до ``UPDATE ... WHERE state='pending'`` с проверкой ``rowcount``
  (блокировок уровня строки в SQLite нет, но БД однопоточная, и гонки не возникает).

Идемпотентность отправки держится на ``crm_message_id``: он же UNIQUE в таблице,
он же уходит в Wazzup, он же возвращается в ошибке ``repeatedCrmMessageId``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.db import insert_or_ignore, supports_skip_locked
from app.storage.models import OutboxMessage, utcnow
from app.types import OutboundMessage, OutboxState, StorageError

__all__ = [
    "claim",
    "due",
    "enqueue",
    "exists_by_wazzup_message_id",
    "exists_recent_sent_text",
    "get",
    "mark_failed",
    "mark_sent",
    "mark_skipped",
    "pending_count",
]

#: Длина, до которой обрезается текст ошибки перед записью в ``last_error``.
_MAX_ERROR_CHARS: Final[int] = 1000


async def enqueue(session: AsyncSession, msg: OutboundMessage) -> UUID:
    """Кладёт исходящее в очередь. Идемпотентно по ``crm_message_id``.

    Повторный вызов с тем же ``crm_message_id`` возвращает id уже существующей строки
    и ничего не дублирует.
    """
    new_id = uuid4()
    now = utcnow()
    values: dict[str, Any] = {
        "id": new_id,
        "conversation_id": msg.conversation_id,
        "crm_message_id": msg.crm_message_id,
        "payload": msg.model_dump(mode="json"),
        "state": OutboxState.PENDING.value,
        "attempts": 0,
        "next_attempt_at": None,
        "created_at": now,
        "updated_at": now,
    }
    result = await session.execute(
        insert_or_ignore(session, OutboxMessage, values, index_elements=["crm_message_id"])
    )
    if result.rowcount:
        return new_id

    existing = (
        await session.execute(
            sa.select(OutboxMessage.id)
            .where(OutboxMessage.crm_message_id == msg.crm_message_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is None:  # pragma: no cover - защитная ветка
        raise StorageError(f"Строка outbox {msg.crm_message_id} не создана и не найдена")
    return existing


async def get(session: AsyncSession, outbox_id: UUID) -> OutboxMessage | None:
    """Строка очереди по id, без захвата."""
    return await session.get(OutboxMessage, outbox_id)


async def claim(session: AsyncSession, outbox_id: UUID) -> OutboxMessage | None:
    """Атомарно забирает строку в работу: ``pending`` → ``sending``, ``attempts += 1``.

    ``None`` — строку уже забрал другой воркер, её нет или она в терминальном состоянии.
    """
    if supports_skip_locked(session):
        locked = (
            await session.execute(
                sa.select(OutboxMessage.id)
                .where(
                    OutboxMessage.id == outbox_id,
                    OutboxMessage.state == OutboxState.PENDING.value,
                )
                .with_for_update(skip_locked=True, of=OutboxMessage)
            )
        ).scalar_one_or_none()
        if locked is None:
            return None

    result = await session.execute(
        sa.update(OutboxMessage)
        .where(
            OutboxMessage.id == outbox_id,
            OutboxMessage.state == OutboxState.PENDING.value,
        )
        .values(
            state=OutboxState.SENDING.value,
            attempts=OutboxMessage.attempts + 1,
            updated_at=utcnow(),
        )
    )
    if not result.rowcount:
        return None

    row = await session.get(OutboxMessage, outbox_id)
    if row is not None:
        await session.refresh(row)
    return row


async def mark_sent(
    session: AsyncSession, outbox_id: UUID, *, wazzup_message_id: str | None
) -> None:
    """Отправлено. ``wazzup_message_id`` нужен, чтобы узнать собственное эхо."""
    await session.execute(
        sa.update(OutboxMessage)
        .where(OutboxMessage.id == outbox_id)
        .values(
            state=OutboxState.SENT.value,
            wazzup_message_id=wazzup_message_id,
            last_error=None,
            next_attempt_at=None,
            updated_at=utcnow(),
        )
    )


async def mark_failed(
    session: AsyncSession, outbox_id: UUID, *, error: str, next_attempt_at: datetime | None
) -> None:
    """Отправка не удалась.

    ``next_attempt_at`` задан — строка возвращается в ``pending`` и будет взята повторно
    после этого момента; ``None`` — попытки исчерпаны, состояние терминальное ``failed``.
    """
    retry = next_attempt_at is not None
    await session.execute(
        sa.update(OutboxMessage)
        .where(OutboxMessage.id == outbox_id)
        .values(
            state=(OutboxState.PENDING if retry else OutboxState.FAILED).value,
            last_error=(error or "")[:_MAX_ERROR_CHARS],
            next_attempt_at=next_attempt_at,
            updated_at=utcnow(),
        )
    )


async def mark_skipped(session: AsyncSession, outbox_id: UUID, *, error: str) -> None:
    """Отправлять не будем (окно канала закрыто, бот на паузе, дубль). Ретраев нет."""
    await session.execute(
        sa.update(OutboxMessage)
        .where(OutboxMessage.id == outbox_id)
        .values(
            state=OutboxState.SKIPPED.value,
            last_error=(error or "")[:_MAX_ERROR_CHARS],
            next_attempt_at=None,
            updated_at=utcnow(),
        )
    )


async def exists_by_wazzup_message_id(session: AsyncSession, message_id: str) -> bool:
    """Наша ли это отправка. Основной признак «эхо не наше» в детекторе оператора."""
    stmt = (
        sa.select(sa.literal(1))
        .where(OutboxMessage.wazzup_message_id == message_id)
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


async def exists_recent_sent_text(
    session: AsyncSession,
    *,
    channel_id: str,
    chat_id: str,
    text: str,
    since: datetime,
    limit: int = 50,
) -> bool:
    """Отправляли ли мы недавно ровно этот текст в этот чат.

    Второй признак «эхо наше» — на случай, когда ``wazzup_message_id`` неизвестен.
    Так бывает при ``repeatedCrmMessageId``: сообщение ушло, а ответ Wazzup до
    нас не доехал, и связать эхо со строкой outbox по id уже нечем (в вебхуке
    ``crmMessageId`` не приходит). Без этой проверки бот принимает собственное
    сообщение за реплику оператора и ставит себе паузу.

    Сравнение идёт в Python, а не в SQL: ``payload`` — JSON, и запрос по его
    полям был бы разным для SQLite и PostgreSQL.
    """
    needle = (text or "").strip()
    if not needle:
        return False
    stmt = (
        sa.select(OutboxMessage.payload)
        .where(
            OutboxMessage.state == OutboxState.SENT.value,
            OutboxMessage.updated_at >= since,
        )
        .order_by(OutboxMessage.updated_at.desc())
        .limit(limit)
    )
    for (payload,) in (await session.execute(stmt)).all():
        if not isinstance(payload, dict):
            continue
        if str(payload.get("chat_id") or "") != chat_id:
            continue
        if channel_id and str(payload.get("channel_id") or "") != channel_id:
            continue
        if str(payload.get("text") or "").strip() == needle:
            return True
    return False


async def pending_count(session: AsyncSession) -> int:
    """Сколько строк ждёт отправки или уже в работе. Метрика глубины очереди."""
    stmt = (
        sa.select(sa.func.count())
        .select_from(OutboxMessage)
        .where(
            OutboxMessage.state.in_((OutboxState.PENDING.value, OutboxState.SENDING.value))
        )
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


async def due(session: AsyncSession, now: datetime, *, limit: int = 50) -> list[UUID]:
    """Пачка id, которые пора отправлять: ``pending`` и срок ретрая наступил.

    На PostgreSQL строки берутся с ``FOR UPDATE SKIP LOCKED`` — параллельные воркеры
    получают непересекающиеся пачки. На SQLite блокировка не нужна: писатель один.
    """
    stmt = (
        sa.select(OutboxMessage.id)
        .where(
            OutboxMessage.state == OutboxState.PENDING.value,
            sa.or_(
                OutboxMessage.next_attempt_at.is_(None),
                OutboxMessage.next_attempt_at <= now,
            ),
        )
        .order_by(
            sa.nulls_first(OutboxMessage.next_attempt_at.asc()),
            OutboxMessage.created_at.asc(),
        )
        .limit(max(1, limit))
    )
    if supports_skip_locked(session):
        stmt = stmt.with_for_update(skip_locked=True, of=OutboxMessage)
    return list((await session.execute(stmt)).scalars().all())
