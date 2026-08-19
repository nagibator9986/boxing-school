"""Репозиторий сообщений и истории модели.

В таблице ``message`` живут две сущности сразу:

* реальная переписка — строки с ``text_raw`` / ``content_uri`` (их читает
  :func:`load_transcript`, они же считаются в счётчиках диалога);
* история Gemini — строки с ``gemini_content`` (дампы ``types.Content``), которые
  пишет :func:`add_llm_turn` и читает :func:`load_history`.

Типы ``google.genai`` сюда не попадают: история — это ``list[dict[str, Any]]``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final, Sequence
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.db import insert_or_ignore
from app.storage.models import Message, OutboxMessage, utcnow
from app.types import (
    Author,
    Direction,
    InboundMessage,
    MessageStatus,
    MsgType,
    OutboundMessage,
    StatusUpdate,
)

__all__ = [
    "add_inbound",
    "add_llm_turn",
    "add_outbound",
    "count_artifact_sends",
    "exists_wazzup_id",
    "load_history",
    "load_transcript",
    "update_status",
]

#: Порядок «зрелости» статуса: более ранний статус не затирает более поздний,
#: потому что вебхуки Wazzup приходят не по порядку.
_STATUS_RANK: Final[dict[str, int]] = {
    MessageStatus.QUEUED.value: 0,
    MessageStatus.INBOUND.value: 0,
    MessageStatus.SENT.value: 1,
    MessageStatus.DELIVERED.value: 2,
    MessageStatus.READ.value: 3,
    MessageStatus.EDITED.value: 4,
    MessageStatus.ERROR.value: 5,
}

#: Расширения, по которым определяется тип исходящего вложения.
_DOC_SUFFIXES: Final[tuple[str, ...]] = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt")
_VIDEO_SUFFIXES: Final[tuple[str, ...]] = (".mp4", ".mov", ".3gp")
_AUDIO_SUFFIXES: Final[tuple[str, ...]] = (".mp3", ".ogg", ".oga", ".m4a", ".wav")


async def exists_wazzup_id(session: AsyncSession, message_id: str) -> bool:
    """Видели ли мы уже это ``messageId``. Долговременная страховка дедупа поверх Redis."""
    stmt = sa.select(sa.literal(1)).where(Message.wazzup_message_id == message_id).limit(1)
    return (await session.execute(stmt)).first() is not None


async def add_inbound(session: AsyncSession, conv_id: UUID, msg: InboundMessage) -> UUID:
    """Сохраняет входящее сообщение. Идемпотентно по ``wazzup_message_id``.

    Возвращает id строки: новой либо уже существовавшей (повторная доставка вебхука).
    """
    new_id = uuid4()
    values: dict[str, Any] = {
        "id": new_id,
        "conversation_id": conv_id,
        "direction": msg.direction.value,
        "author": msg.author.value,
        "wazzup_message_id": msg.message_id,
        "msg_type": msg.msg_type.value,
        "text_raw": msg.text,
        "content_uri": msg.content_uri,
        "status": msg.status.value,
        "error_code": msg.error_code,
        "error_description": msg.error_description,
        "is_echo": msg.is_echo,
        "sent_from_app": msg.sent_from_app,
        "author_name": msg.author_name,
        "author_id": msg.author_id,
        "channel_dt": msg.channel_dt,
        "created_at": msg.received_at,
    }
    result = await session.execute(
        insert_or_ignore(session, Message, values, index_elements=["wazzup_message_id"])
    )
    if result.rowcount:
        return new_id

    existing = (
        await session.execute(
            sa.select(Message.id).where(Message.wazzup_message_id == msg.message_id).limit(1)
        )
    ).scalar_one_or_none()
    return existing or new_id


async def add_outbound(
    session: AsyncSession,
    conv_id: UUID,
    msg: OutboundMessage,
    *,
    wazzup_message_id: str | None,
) -> UUID:
    """Сохраняет отправленное ботом сообщение. Идемпотентно по ``crm_message_id``."""
    new_id = uuid4()
    values: dict[str, Any] = {
        "id": new_id,
        "conversation_id": conv_id,
        "direction": Direction.OUT.value,
        "author": Author.BOT.value,
        "wazzup_message_id": wazzup_message_id,
        "crm_message_id": msg.crm_message_id,
        "msg_type": _outbound_msg_type(msg).value,
        "text_raw": msg.text,
        "content_uri": msg.content_uri,
        "status": (MessageStatus.SENT if wazzup_message_id else MessageStatus.QUEUED).value,
        "created_at": utcnow(),
    }
    result = await session.execute(
        insert_or_ignore(session, Message, values, index_elements=["crm_message_id"])
    )
    if result.rowcount:
        return new_id

    existing = (
        await session.execute(
            sa.select(Message.id).where(Message.crm_message_id == msg.crm_message_id).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None and wazzup_message_id:
        await session.execute(
            sa.update(Message)
            .where(Message.id == existing, Message.wazzup_message_id.is_(None))
            .values(wazzup_message_id=wazzup_message_id, status=MessageStatus.SENT.value)
        )
    return existing or new_id


def _outbound_msg_type(msg: OutboundMessage) -> MsgType:
    """Тип исходящего: текст либо вложение, распознанное по расширению ``content_uri``."""
    if msg.text is not None:
        return MsgType.TEXT
    uri = (msg.content_uri or "").split("?", 1)[0].lower()
    if uri.endswith(_DOC_SUFFIXES):
        return MsgType.DOCUMENT
    if uri.endswith(_VIDEO_SUFFIXES):
        return MsgType.VIDEO
    if uri.endswith(_AUDIO_SUFFIXES):
        return MsgType.AUDIO
    return MsgType.IMAGE


async def add_llm_turn(
    session: AsyncSession, conv_id: UUID, contents: Sequence[dict[str, Any]]
) -> None:
    """Дописывает историю модели: по строке на каждый ``types.Content``.

    ``created_at`` внутри одного хода разводится на микросекунды — иначе порядок
    элементов хода не восстановить при чтении.
    """
    if not contents:
        return
    base = utcnow()
    rows: list[dict[str, Any]] = []
    for index, content in enumerate(contents):
        role = str(content.get("role") or "user")
        rows.append(
            {
                "id": uuid4(),
                "conversation_id": conv_id,
                "direction": (Direction.OUT if role == "model" else Direction.IN).value,
                "author": (Author.BOT if role == "model" else Author.CLIENT).value,
                "gemini_role": role,
                "gemini_content": content,
                "msg_type": MsgType.SYSTEM.value,
                "status": MessageStatus.QUEUED.value,
                "created_at": base + timedelta(microseconds=index),
            }
        )
    await session.execute(sa.insert(Message), rows)


async def load_history(
    session: AsyncSession, conv_id: UUID, *, max_turns: int
) -> list[dict[str, Any]]:
    """Последние ``max_turns`` элементов истории модели в хронологическом порядке."""
    if max_turns <= 0:
        return []
    stmt = (
        sa.select(Message.gemini_content)
        .where(Message.conversation_id == conv_id, Message.gemini_content.is_not(None))
        .order_by(Message.created_at.desc())
        .limit(max_turns)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [row for row in reversed(rows) if isinstance(row, dict)]


async def load_transcript(
    session: AsyncSession, conv_id: UUID, *, limit: int = 40
) -> list[tuple[Author, str]]:
    """Последние реплики живой переписки — для карточки менеджеру и разбора диалога."""
    if limit <= 0:
        return []
    stmt = (
        sa.select(Message.author, Message.text_raw)
        .where(
            Message.conversation_id == conv_id,
            Message.text_raw.is_not(None),
            Message.gemini_content.is_(None),
        )
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    transcript: list[tuple[Author, str]] = []
    for author, text in reversed(rows):
        try:
            parsed = Author(author)
        except ValueError:  # pragma: no cover - чужое значение в БД
            parsed = Author.SYSTEM
        transcript.append((parsed, text or ""))
    return transcript


async def update_status(
    session: AsyncSession, wazzup_message_id: str, update: StatusUpdate
) -> bool:
    """Применяет статус доставки к нашему сообщению.

    ``True`` — строка найдена (статус применён либо намеренно пропущен как устаревший),
    ``False`` — такого сообщения у нас нет, значит статус относится к чужой отправке.
    """
    row = (
        await session.execute(
            sa.select(Message.id, Message.status)
            .where(Message.wazzup_message_id == wazzup_message_id)
            .limit(1)
        )
    ).first()
    if row is None:
        return False

    message_id, current = row
    new_status = update.status.value
    if _STATUS_RANK.get(new_status, 0) < _STATUS_RANK.get(current, 0):
        return True

    await session.execute(
        sa.update(Message)
        .where(Message.id == message_id)
        .values(
            status=new_status,
            error_code=update.error_code,
            error_description=update.error_description,
        )
    )
    return True


async def count_artifact_sends(session: AsyncSession, conv_id: UUID, artifact_id: str) -> int:
    """Сколько раз артефакт уже уходил в этом диалоге.

    Считается по очереди исходящих: ``artifact_id`` живёт в JSON-дампе ``OutboundMessage``.
    Учитываются и ещё не отправленные строки (``pending``/``sending``) — иначе бот
    успеет поставить второй такой же артефакт до отправки первого.
    """
    stmt = (
        sa.select(sa.func.count())
        .select_from(OutboxMessage)
        .where(
            OutboxMessage.conversation_id == conv_id,
            OutboxMessage.state.in_(("pending", "sending", "sent")),
            OutboxMessage.payload["artifact_id"].as_string() == artifact_id,
        )
    )
    return int((await session.execute(stmt)).scalar_one() or 0)
