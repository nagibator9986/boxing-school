"""Сообщения одного чата уходят строго по очереди.

За один ход бот ставит в очередь несколько сообщений, а воркер отправлял их
параллельно. 10.09.2026 клиент видел «напишите номер зала» раньше самого списка
залов и «записать ребёнка?» раньше расписания и видео дороги.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import sqlalchemy as sa

from app.channels.wazzup_schemas import SendMessageResponse
from app.core.pipeline import PipelineDeps
from app.kb import loader as kb_loader
from app.llm.client import FakeLLMClient
from app.storage import repo_outbox
from app.storage.models import Conversation, OutboxMessage
from app.types import ChannelKind, Language, OutboundKind, OutboundMessage, OutboxState
from app.workers.tasks_outbound import send_outbox_job

from tests.conftest import RecordingQueue

UTC = timezone.utc


class RecordingWazzup:
    """Заглушка последней мили: запоминает порядок отправки."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, request):
        self.sent.append(request.text or request.contentUri or "")
        return SendMessageResponse(messageId=str(uuid4()), chatId=request.chatId)


async def _setup(kb, state, sessionmaker, settings, **overrides):
    kb_loader.swap(kb)
    queue = RecordingQueue()
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=FakeLLMClient([]), kb=kb_loader.get_snapshot,
        queue=queue, settings=settings.model_copy(update=overrides),
    )
    wazzup = RecordingWazzup()
    return {"deps": deps, "wazzup": wazzup}, queue, wazzup


async def _conversation(sessionmaker, chat: str = "77010001122"):
    async with sessionmaker() as db:
        conv = Conversation(conv_key=f"wa:{chat}", channel_id="wa-1", chat_type="whatsapp", chat_id=chat)
        db.add(conv)
        await db.commit()
        return conv.id


async def _put(sessionmaker, conv_id, *, text=None, uri=None, chat="77010001122"):
    async with sessionmaker() as db:
        outbox_id = await repo_outbox.enqueue(
            db,
            OutboundMessage(
                conversation_id=conv_id,
                channel_id="wa-1",
                channel=ChannelKind.WHATSAPP,
                chat_id=chat,
                lang=Language.RU,
                kind=OutboundKind.ARTIFACT,
                text=None if uri else text,
                content_uri=uri,
            ),
        )
        await db.commit()
        return outbox_id


async def _state_of(sessionmaker, outbox_id) -> str:
    async with sessionmaker() as db:
        return (await db.get(OutboxMessage, outbox_id)).state


async def test_later_message_waits_for_the_earlier_one(kb, state, sessionmaker, settings) -> None:
    """Третье сообщение хода не уходит раньше первого и второго."""
    ctx, queue, wazzup = await _setup(kb, state, sessionmaker, settings)
    conv = await _conversation(sessionmaker)
    first = await _put(sessionmaker, conv, text="💳 Прайс")
    second = await _put(sessionmaker, conv, text="🥊 Наши залы")
    third = await _put(sessionmaker, conv, text="Какой зал вам ближе?")

    await send_outbox_job(ctx, str(third))
    assert wazzup.sent == [], "третье сообщение обогнало первые два"
    assert (third, settings.outbox_order_retry_ms) in queue.outbox, "строка не переставлена"
    assert await _state_of(sessionmaker, third) == OutboxState.PENDING.value, "попытка сожжена"

    for outbox_id in (first, second, third):
        await send_outbox_job(ctx, str(outbox_id))

    assert wazzup.sent == ["💳 Прайс", "🥊 Наши залы", "Какой зал вам ближе?"]


async def test_text_after_video_waits_for_the_file(kb, state, sessionmaker, settings) -> None:
    """Wazzup ещё скачивает видео — вопрос после него ждёт, а не обгоняет."""
    ctx, queue, wazzup = await _setup(kb, state, sessionmaker, settings, media_settle_seconds=8.0)
    conv = await _conversation(sessionmaker)
    video = await _put(sessionmaker, conv, uri="https://example.org/route.mp4")
    question = await _put(sessionmaker, conv, text="Записать ребёнка на первую пробную тренировку?")

    await send_outbox_job(ctx, str(video))
    await send_outbox_job(ctx, str(question))

    assert wazzup.sent == ["https://example.org/route.mp4"], "вопрос обогнал видео"
    delays = [delay for outbox_id, delay in queue.outbox if outbox_id == question]
    assert delays and delays[-1] >= 7000, delays

    # Окно прошло — вопрос уходит.
    async with sessionmaker() as db:
        await db.execute(
            sa.update(OutboxMessage)
            .where(OutboxMessage.id == video)
            .values(updated_at=datetime.now(tz=UTC) - timedelta(seconds=30))
        )
        await db.commit()
    await send_outbox_job(ctx, str(question))

    assert wazzup.sent[-1] == "Записать ребёнка на первую пробную тренировку?"


async def test_another_chat_is_not_held(kb, state, sessionmaker, settings) -> None:
    """Очередь — внутри одного чата. Чужой клиент не ждёт чужих сообщений."""
    ctx, _, wazzup = await _setup(kb, state, sessionmaker, settings)
    first_conv = await _conversation(sessionmaker, chat="77010000001")
    other_conv = await _conversation(sessionmaker, chat="77010000002")
    await _put(sessionmaker, first_conv, text="ещё не ушло", chat="77010000001")
    other = await _put(sessionmaker, other_conv, text="другому клиенту", chat="77010000002")

    await send_outbox_job(ctx, str(other))

    assert wazzup.sent == ["другому клиенту"]


async def test_failed_message_does_not_hold_the_chat(kb, state, sessionmaker, settings) -> None:
    """Упавшее насовсем сообщение не должно навсегда заткнуть переписку."""
    ctx, _, wazzup = await _setup(kb, state, sessionmaker, settings)
    conv = await _conversation(sessionmaker)
    broken = await _put(sessionmaker, conv, text="не отправится")
    after = await _put(sessionmaker, conv, text="следующее")
    async with sessionmaker() as db:
        await repo_outbox.mark_failed(db, broken, error="fatal", next_attempt_at=None)
        await db.commit()

    await send_outbox_job(ctx, str(after))

    assert wazzup.sent == ["следующее"]


async def test_stuck_sending_does_not_hold_the_chat_forever(kb, state, sessionmaker, settings) -> None:
    """Процесс упал посреди отправки — строка осталась в «sending». Чат не молчит вечно."""
    ctx, _, wazzup = await _setup(kb, state, sessionmaker, settings, worker_job_timeout_s=180)
    conv = await _conversation(sessionmaker)
    stuck = await _put(sessionmaker, conv, text="застряло")
    after = await _put(sessionmaker, conv, text="следующее")
    async with sessionmaker() as db:
        await db.execute(
            sa.update(OutboxMessage)
            .where(OutboxMessage.id == stuck)
            .values(state=OutboxState.SENDING.value, updated_at=datetime.now(tz=UTC) - timedelta(hours=1))
        )
        await db.commit()

    await send_outbox_job(ctx, str(after))

    assert wazzup.sent == ["следующее"]
