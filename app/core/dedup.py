"""Идемпотентность входящих: один ``messageId`` — ровно один ответ клиенту.

Wazzup повторяет доставку вебхука, если не получил ``200 OK`` вовремя, а Railway
рестартует службу когда угодно. Без дедупликации родитель получит один и тот же
ответ дважды, а бот — второй лид на того же ребёнка.

Барьера два, и они закрывают разные отказы:

* **Redis (``SET NX EX``)** — быстрый путь. Отрабатывает за доли миллисекунды,
  живёт ``dedup_ttl_seconds`` (сутки). Ключи выдаёт ``app.storage.state``.
* **Таблица ``processed_webhook``** — долговременный путь. Переживает падение
  Redis, очистку кэша и переезд плагина. ``INSERT ... ON CONFLICT DO NOTHING``:
  вставили мы — сообщение новое, конфликт — уже обрабатывали.

Порядок именно такой: сначала дешёвая проверка, потом надёжная. Если Redis
недоступен, дедупликация не ломается — она просто становится медленнее, а
решение принимает база.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.logging_conf import get_logger
from app.storage.db import insert_or_ignore
from app.storage.models import ProcessedWebhook, utcnow
from app.storage.state import StateStore, key_dedup_message, key_dedup_status
from app.types import MessageStatus, StorageError

__all__ = ["KIND_MESSAGE", "KIND_STATUS", "seen_message", "seen_status"]

_log = get_logger(__name__)

#: Значения колонки ``processed_webhook.kind``.
KIND_MESSAGE: Final[str] = "message"
KIND_STATUS: Final[str] = "status"

#: Значение ключа в Redis: содержимое не важно, важен сам факт наличия.
_MARK: Final[str] = "1"


async def seen_message(state: StateStore, session: AsyncSession, message_id: str) -> bool:
    """Двойной барьер: SETNX в Redis (быстрый путь) + ``INSERT ... ON CONFLICT DO NOTHING``
    в ``processed_webhook`` (надёжный). ``True`` — уже обрабатывали, задачу нужно бросить.

    Пустой ``message_id`` считается уже обработанным: без ключа идемпотентности
    отвечать нельзя — повтор вебхука породит второй ответ клиенту.
    """
    if not message_id:
        _log.warning("dedup_empty_message_id")
        return True
    return await _seen(state, session, key_dedup_message(message_id), message_id, KIND_MESSAGE)


async def seen_status(
    state: StateStore, session: AsyncSession, message_id: str, status: MessageStatus
) -> bool:
    """Статусы дедуплицируются по паре ``(messageId, status)``.

    Одно сообщение проходит ``sent → delivered → read``: это три разных события,
    и схлопывать их в одно нельзя.
    """
    if not message_id:
        return True
    composite = f"{message_id}:{status.value}"
    return await _seen(
        state, session, key_dedup_status(message_id, status.value), composite, KIND_STATUS
    )


async def _seen(
    state: StateStore, session: AsyncSession, state_key: str, row_key: str, kind: str
) -> bool:
    """Общая механика обоих барьеров."""
    settings = get_settings()
    ttl = max(1, settings.dedup_ttl_seconds)

    try:
        fresh = await state.set_if_absent(state_key, _MARK, ttl)
    except Exception as exc:  # Redis недоступен — решение примет база.
        _log.warning("dedup_state_unavailable", error=str(exc), kind=kind)
        fresh = True
    if not fresh:
        _log.info("dedup_hit", source="state", kind=kind)
        return True

    try:
        result = await session.execute(
            insert_or_ignore(
                session,
                ProcessedWebhook,
                {"message_id": row_key, "kind": kind, "first_seen_at": utcnow()},
                index_elements=["message_id"],
            )
        )
    except Exception as exc:  # pragma: no cover - отказ БД разбирает вызывающий
        raise StorageError(f"Дедупликация {kind} не выполнена: {exc}") from exc

    inserted = bool(result.rowcount)
    if not inserted:
        _log.info("dedup_hit", source="db", kind=kind)
    return not inserted
