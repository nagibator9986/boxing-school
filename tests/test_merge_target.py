"""Склейка текста клиенту дописывает его только в строку очереди этого клиента.

Прод 11.11.2026: после записи на пробное последней в очереди хода стояла карточка
администратору, и текст клиенту дописался в неё. Карточка вышла длиннее 1000 знаков,
проверка отправки её отбросила — не получили ни клиент, ни администратор. Короче
1000 было бы хуже: текст клиенту молча ушёл бы администратору.

Тесты сравнивают именно строки outbox, а не ``services.messages``: в памяти склейка
выглядела верной, поэтому сквозные тесты диалогов ошибку не видели.
"""
from __future__ import annotations

import sqlalchemy as sa

from app.core import pipeline
from app.storage.models import Conversation, OutboxMessage
from app.types import ChannelKind, Language, OutboundKind, OutboundMessage, OutboxState
from tests.conftest import RecordingQueue

CLIENT = "77015550101"
ADMIN = "77015550199"
CARD_TEXT = "Новая заявка на пробное: " + "x" * 900


async def _services(kb, state, sessionmaker, settings, db, **overrides):
    deps = pipeline.PipelineDeps(
        sessionmaker=sessionmaker, state=state, kb=lambda: kb, queue=RecordingQueue(),
        settings=settings.model_copy(update=overrides) if overrides else settings, llm=None,
    )
    conv = Conversation(conv_key=f"wa:{CLIENT}", channel_id="wa-1", chat_type="whatsapp", chat_id=CLIENT)
    db.add(conv)
    await db.flush()
    return pipeline._Services(deps=deps, session=db, conv=conv), conv


def _to_client(conv, text, kind=OutboundKind.BOT_REPLY):
    return OutboundMessage(
        conversation_id=conv.id, channel_id="wa-1", channel=ChannelKind.WHATSAPP,
        chat_id=CLIENT, lang=Language.RU, kind=kind, text=text,
    )


def _card():
    return OutboundMessage(
        channel_id="wa-1", channel=ChannelKind.WHATSAPP, chat_id=ADMIN, lang=Language.RU,
        kind=OutboundKind.MANAGER_CARD, text=CARD_TEXT,
    )


async def _rows(db):
    return {row.id: row for row in (await db.execute(sa.select(OutboxMessage))).scalars()}


async def test_client_text_merges_past_a_manager_card_into_the_client_row(kb, state, sessionmaker, settings) -> None:
    async with sessionmaker() as db:
        services, conv = await _services(kb, state, sessionmaker, settings, db)
        first = await services.enqueue_outbound(_to_client(conv, "Вот расписание."))
        card = await services._enqueue(_card(), to_client=False)
        merged = await services.enqueue_outbound(_to_client(conv, "Мы записали вас на пробное."))

        rows = await _rows(db)
        assert rows[card].payload["text"] == CARD_TEXT, "текст клиента дописан в карточку администратору"
        assert merged == first
        assert rows[first].payload["text"] == "Вот расписание.\n\nМы записали вас на пробное."
        assert len(rows) == 2
        # Память хода и очередь говорят одно и то же.
        assert [m.text for m in services.messages] == [rows[first].payload["text"]]


async def test_text_after_an_early_sent_card_gets_its_own_row(kb, state, sessionmaker, settings) -> None:
    """Карточка ушла раньше конца хода — дописывать в неё уже поздно, и в карточку админу тоже нельзя."""
    async with sessionmaker() as db:
        services, conv = await _services(kb, state, sessionmaker, settings, db, send_cards_early=True)
        early = await services.enqueue_outbound(_to_client(conv, "🗓 Расписание", kind=OutboundKind.ARTIFACT))
        assert all(entry[0] != early for entry in services.outbox), "карточка должна была уйти раньше"
        card = await services._enqueue(_card(), to_client=False)
        own = await services.enqueue_outbound(_to_client(conv, "Какое время удобнее?"))

        rows = await _rows(db)
        assert own not in (early, card)
        assert rows[card].payload["text"] == CARD_TEXT
        assert rows[early].payload["text"] == "🗓 Расписание"
        assert rows[own].payload["text"] == "Какое время удобнее?"


async def test_text_after_the_queue_was_flushed_does_not_reopen_a_sent_row(kb, state, sessionmaker, settings) -> None:
    async with sessionmaker() as db:
        services, conv = await _services(kb, state, sessionmaker, settings, db)
        first = await services.enqueue_outbound(_to_client(conv, "Первый ответ."))
        await pipeline._flush_queue(services.deps, services)
        second = await services.enqueue_outbound(_to_client(conv, "Второй ответ."))

        rows = await _rows(db)
        assert second != first
        assert rows[first].payload["text"] == "Первый ответ."
        assert rows[first].state == OutboxState.PENDING.value
