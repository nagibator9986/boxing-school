"""Что бот отправляет, не дожидаясь конца хода.

Замер 03.09.2026: карточка цен была готова через 1,5 секунды, а уходила клиенту
через 2,8 — всё это время она лежала в очереди, пока модель дописывала короткий
вопрос следом.

Отправить её раньше — значит разбить один ответ надвое: короткая карточка иначе
сливается с текстом модели. Владелец жаловался именно на поток сообщений,
поэтому по умолчанию рано уходит только файл — он и так отдельное сообщение.
Полный режим включается настройкой ``SEND_CARDS_EARLY``.
"""

from __future__ import annotations

from app.core.pipeline import _send_without_waiting
from app.types import ChannelKind, Language, OutboundKind, OutboundMessage
from uuid import uuid4


def card(text: str | None = "🗓 Расписание", *, kind=OutboundKind.ARTIFACT, uri: str | None = None):
    """Исходящее ровно с одним содержимым: текст или файл — так требует модель."""
    return OutboundMessage(
        conversation_id=uuid4(),
        channel_id="wa",
        channel=ChannelKind.WHATSAPP,
        chat_id="7701",
        lang=Language.RU,
        kind=kind,
        text=None if uri else text,
        content_uri=uri,
    )


def test_file_goes_at_once() -> None:
    """Видео дороги — отдельное сообщение, склеивать его не с чем."""
    assert _send_without_waiting(card(uri="https://example/route.mp4"), position=0, cards_early=False)


def test_card_waits_so_the_answer_stays_one_message() -> None:
    """Карточка ждёт текст модели: вместе они читаются как один ответ."""
    assert not _send_without_waiting(card(), position=0, cards_early=False)


def test_card_goes_at_once_when_the_owner_asks_for_speed() -> None:
    """С включённой настройкой карточка не ждёт — минус секунда, плюс сообщение."""
    assert _send_without_waiting(card(), position=0, cards_early=True)


def test_nothing_overtakes_what_is_already_queued() -> None:
    """Видео не должно обгонять расписание, к которому оно приложено."""
    assert not _send_without_waiting(
        card(uri="https://example/route.mp4"), position=1, cards_early=True
    )


def test_model_text_waits() -> None:
    """Ответ модели уходит в общем порядке: он и есть конец хода."""
    assert not _send_without_waiting(
        card("Подойдёт такое время?", kind=OutboundKind.BOT_REPLY), position=0, cards_early=True
    )


# --------------------------------------------------------------------------- #
# Сквозь пайплайн
# --------------------------------------------------------------------------- #
async def test_card_reaches_the_queue_before_the_turn_ends(kb, state, sessionmaker, settings) -> None:
    """С включённой настройкой карточка ставится в очередь ДО ответа модели.

    Это и есть выигранная секунда: проверяется по содержимому очереди в момент
    первой постановки — там уже карточка расписания и ещё нет текста модели.
    """
    import sqlalchemy as sa

    from app.core.pipeline import PipelineDeps, process_inbound
    from app.kb import loader as kb_loader
    from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
    from app.storage.models import OutboxMessage

    from tests.conftest import RecordingQueue, webhook_payload

    kb_loader.swap(kb)
    snapshots: list[list[str]] = []

    class WatchingQueue(RecordingQueue):
        async def enqueue_outbox(self, outbox_id, delay_ms=0):
            async with sessionmaker() as db:
                rows = (await db.execute(sa.select(OutboxMessage.payload))).scalars().all()
            snapshots.append([str((row or {}).get("text") or "") for row in rows])
            return await super().enqueue_outbox(outbox_id, delay_ms=delay_ms)

    llm = FakeLLMClient(
        [
            FakeTurn.tool(FakeCall("get_schedule", {"gym_id": "ksk_kairbekova_334"})),
            FakeTurn.answer("Какой ближе?"),
        ]
    )
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=llm, kb=kb_loader.get_snapshot,
        queue=WatchingQueue(), settings=settings.model_copy(update={"send_cards_early": True}),
    )

    decisions = await process_inbound(
        deps, webhook_payload("early-1", "А расписание?", chat_id="77015550001")
    )

    assert [d.action.value for d in decisions] == ["reply"]
    assert snapshots, "ни одной задачи отправки не поставлено"
    first = snapshots[0]
    assert any("Расписание" in text for text in first), f"карточки в очереди нет: {first}"
    assert not any("Какой ближе" in text for text in first), (
        f"карточка ждала ответа модели: {first}"
    )


async def test_card_waits_by_default_and_arrives_as_one_message(
    kb, state, sessionmaker, settings
) -> None:
    """По умолчанию карточка и вопрос приходят одним сообщением, как раньше."""
    import sqlalchemy as sa

    from app.core.pipeline import PipelineDeps, process_inbound
    from app.kb import loader as kb_loader
    from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
    from app.storage.models import OutboxMessage

    from tests.conftest import RecordingQueue, webhook_payload

    kb_loader.swap(kb)
    llm = FakeLLMClient(
        [
            FakeTurn.tool(FakeCall("get_schedule", {"gym_id": "ksk_kairbekova_334"})),
            FakeTurn.answer("Какой ближе?"),
        ]
    )
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=llm, kb=kb_loader.get_snapshot,
        queue=RecordingQueue(), settings=settings,
    )

    await process_inbound(deps, webhook_payload("slow-1", "А расписание?", chat_id="77015550003"))

    async with sessionmaker() as db:
        rows = (await db.execute(sa.select(OutboxMessage.payload))).scalars().all()
    texts = [str((row or {}).get("text") or "") for row in rows]

    assert len(texts) == 1, f"ответ разбит на несколько сообщений: {texts}"
    assert "Расписание" in texts[0] and "Какой ближе" in texts[0]


async def test_nothing_is_sent_early_once_a_human_is_in_the_dialogue(
    kb, state, sessionmaker, settings
) -> None:
    """Человек в диалоге — ранняя отправка выключается: вернуть сообщение нельзя."""
    import sqlalchemy as sa

    from app.core import pause
    from app.core.pipeline import PipelineDeps, process_inbound
    from app.kb import loader as kb_loader
    from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
    from app.storage.models import Conversation

    from tests.conftest import RecordingQueue, webhook_payload

    kb_loader.swap(kb)
    llm = FakeLLMClient([FakeTurn.answer("Здравствуйте!")])
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=llm, kb=kb_loader.get_snapshot,
        queue=RecordingQueue(), settings=settings,
    )
    await process_inbound(deps, webhook_payload("hz-1", "Здравствуйте", chat_id="77015550002"))
    await process_inbound(
        deps, webhook_payload("hz-2", "Отвечает администратор", chat_id="77015550002", is_echo=True)
    )
    async with deps.sessionmaker() as db:
        conv = (await db.execute(sa.select(Conversation))).scalars().one()
        assert await pause.operator_last_seen(db, conv.id) is not None, "опора теста"

    llm.reset(
        [
            FakeTurn.tool(FakeCall("get_schedule", {"gym_id": "ksk_kairbekova_334"})),
            FakeTurn.answer("Какой ближе?"),
        ]
    )
    after = await process_inbound(
        deps, webhook_payload("hz-3", "Какие залы?", chat_id="77015550002")
    )

    assert [d.action.value for d in after] == ["silent"], "бот заговорил поверх человека"


# --------------------------------------------------------------------------- #
# Человек в диалоге
# --------------------------------------------------------------------------- #
def test_human_who_wrote_during_the_turn_stops_the_early_send() -> None:
    """Администратор ответил, пока бот думал, — карточку рано не отправляем."""
    from datetime import datetime, timedelta, timezone

    from app.core.pipeline import _human_joined_mid_turn

    started = datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc)

    assert _human_joined_mid_turn(started + timedelta(seconds=2), started)


def test_old_human_reply_does_not_block_the_early_send() -> None:
    """Реплика часовой давности бота не глушит: его уже вернули в диалог."""
    from datetime import datetime, timedelta, timezone

    from app.core.pipeline import _human_joined_mid_turn

    started = datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc)

    assert not _human_joined_mid_turn(started - timedelta(hours=1), started)
    assert not _human_joined_mid_turn(None, started)
