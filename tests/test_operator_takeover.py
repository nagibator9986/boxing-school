"""Человек написал из аккаунта — бот замолкает и сам не возвращается.

Владелец просил об этом дважды. Первый раз пауза после ответа человека была
тридцать минут, потом сто двадцать — и каждый раз бот возвращался в разговор,
который человек вёл до сих пор. Второй раз это прозвучало прямо: «после того
как кто-то пишет с самого аккаунта, бот должен резко затихать».

Теперь ноль минут означает «молчать, пока бота не вернут вручную» — кнопкой в
карточке клиента или строкой «#бот» в переписке. Это и значение по умолчанию.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence

import pytest

from app.core import pause
from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeLLMClient, FakeTurn
from app.storage.models import Conversation
from app.types import PauseReason, PipelineDecision

from tests.conftest import RecordingQueue, webhook_payload

UTC = timezone.utc
CHAT_ID = "77015554455"


@pytest.fixture
async def llm() -> FakeLLMClient:
    return FakeLLMClient([])


@pytest.fixture
async def deps(kb, state, sessionmaker, settings, llm) -> PipelineDeps:
    kb_loader.swap(kb)
    return PipelineDeps(
        sessionmaker=sessionmaker,
        state=state,
        llm=llm,
        kb=kb_loader.get_snapshot,
        queue=RecordingQueue(),
        settings=settings,
    )


async def client_says(deps, llm, message_id: str, text: str) -> Sequence[PipelineDecision]:
    llm.reset([FakeTurn.answer("Здравствуйте! Чем помочь?")])
    return await process_inbound(deps, webhook_payload(message_id, text, chat_id=CHAT_ID))


async def human_says(deps, message_id: str, text: str) -> Sequence[PipelineDecision]:
    """Сообщение, отправленное человеком из самого аккаунта."""
    return await process_inbound(
        deps, webhook_payload(message_id, text, chat_id=CHAT_ID, is_echo=True)
    )


def actions(decisions: Sequence[PipelineDecision]) -> list[str]:
    return [d.action.value for d in decisions]


# --------------------------------------------------------------------------- #
# Затихание
# --------------------------------------------------------------------------- #
async def test_bot_goes_silent_at_once(deps, llm) -> None:
    """Следующее сообщение клиента бот уже не отвечает."""
    await client_says(deps, llm, "t-1", "Здравствуйте")
    await human_says(deps, "t-2", "Здравствуйте! Отвечает администратор школы.")

    after = await client_says(deps, llm, "t-3", "А сколько стоит абонемент?")

    assert actions(after) == ["silent"]


async def test_pause_does_not_expire_on_its_own(deps, llm) -> None:
    """Время паузу не снимает: разговор остаётся за человеком.

    Раньше через два часа бот возвращался сам — ровно в тот разговор, который
    человек вёл с перерывами.
    """
    await client_says(deps, llm, "t-4", "Здравствуйте")
    await human_says(deps, "t-5", "Я подключился, дальше отвечу сам.")

    from tests.conftest import MemoryStateStore

    async with deps.sessionmaker() as db:
        conv = (await db.execute(__import__("sqlalchemy").select(Conversation))).scalars().one()
        far_future = datetime.now(tz=UTC) + timedelta(days=30)
        # Хранилище чистое: проверяется строка в базе, а не переживший ключ.
        assert await pause.is_paused(MemoryStateStore(), db, conv.id, conv.conv_key, far_future)


async def test_manual_return_brings_the_bot_back(deps, llm) -> None:
    """Возврат остаётся за человеком — и он работает."""
    await client_says(deps, llm, "t-6", "Здравствуйте")
    await human_says(deps, "t-7", "Отвечаю сам.")

    async with deps.sessionmaker() as db:
        conv = (await db.execute(__import__("sqlalchemy").select(Conversation))).scalars().one()
        await pause.resume(deps.state, db, conv.id, conv.conv_key, by="admin")
        await db.commit()

    after = await client_says(deps, llm, "t-8", "А сколько стоит?")

    assert actions(after) == ["reply"]


async def test_resume_command_from_the_chat_still_works(deps, llm, settings) -> None:
    """Строка «#бот» в переписке возвращает бота, как и раньше."""
    await client_says(deps, llm, "t-9", "Здравствуйте")
    await human_says(deps, "t-10", "Дальше я сам.")

    await human_says(deps, "t-11", settings.operator_resume_command)

    after = await client_says(deps, llm, "t-12", "Сколько стоит?")
    assert actions(after) == ["reply"]


# --------------------------------------------------------------------------- #
# Настройка со сроком по-прежнему работает
# --------------------------------------------------------------------------- #
async def test_owner_can_still_choose_a_timed_pause(deps, llm, settings) -> None:
    """Владелец вправе вернуть срок: ноль — значение по умолчанию, а не догма."""
    from app.admin.runtime_settings import RuntimeSettings

    timed = RuntimeSettings.from_values({"operator_pause_minutes": "30"})
    deps = PipelineDeps(
        sessionmaker=deps.sessionmaker,
        state=deps.state,
        llm=deps.llm,
        kb=deps.kb,
        queue=deps.queue,
        settings=deps.settings,
        runtime=lambda: timed,
    )

    await client_says(deps, llm, "t-13", "Здравствуйте")
    await human_says(deps, "t-14", "Отвечаю сам.")

    # Быстрый ключ живёт по настоящим часам, а тест сдвигает время вперёд —
    # поэтому истину спрашиваем у базы, с чистым хранилищем состояния.
    from tests.conftest import MemoryStateStore

    async with deps.sessionmaker() as db:
        conv = (await db.execute(__import__("sqlalchemy").select(Conversation))).scalars().one()
        later = datetime.now(tz=UTC) + timedelta(minutes=31)
        assert not await pause.is_paused(MemoryStateStore(), db, conv.id, conv.conv_key, later)


def test_zero_means_manual_only(settings) -> None:
    """Ноль сохраняется как ноль, а не поднимается до минуты."""
    assert pause.pause_minutes_for(PauseReason.OPERATOR_REPLY, settings) == 0


# --------------------------------------------------------------------------- #
# Ответ, уже стоящий в очереди
# --------------------------------------------------------------------------- #
async def queued_kinds(session, conv_id) -> list[tuple[str, str]]:
    """Что лежит в очереди: вид сообщения и его состояние."""
    import json

    import sqlalchemy as sa

    from app.storage.models import OutboxMessage

    rows = (
        await session.execute(
            sa.select(OutboxMessage.payload, OutboxMessage.state).where(
                OutboxMessage.conversation_id == conv_id
            )
        )
    ).all()
    out = []
    for payload, state in rows:
        data = json.loads(payload) if isinstance(payload, (str, bytes)) else dict(payload or {})
        out.append((str(data.get("kind")), str(state)))
    return out


async def test_queued_bot_reply_is_not_sent_after_a_takeover(deps, llm) -> None:
    """Строка ответа лежит в очереди до минуты — за это время вошёл человек.

    Проверка окна канала в отправщике была, а этой не было: бот писал поверх
    человека уже после того, как тот вступил в разговор.
    """
    import sqlalchemy as sa

    from app.storage.models import OutboxMessage
    from app.types import OutboundMessage
    from app.workers.tasks_outbound import _stale_after_takeover

    await client_says(deps, llm, "q-1", "Здравствуйте")
    await human_says(deps, "q-2", "Отвечаю сам, подождите.")

    async with deps.sessionmaker() as db:
        conv = (await db.execute(sa.select(Conversation))).scalars().one()
        row = (await db.execute(sa.select(OutboxMessage))).scalars().first()
        assert row is not None, "в очереди нет ни одной строки — проверять нечего"

        stale = OutboundMessage.model_validate(row.payload)
        assert await _stale_after_takeover(db, conv, row, stale, now=datetime.now(tz=UTC))


async def test_operator_reply_from_crm_is_sent_even_during_a_takeover(deps, llm) -> None:
    """Ответ самого человека из CRM уходит: это и есть его разговор."""
    import sqlalchemy as sa

    from app.storage.models import OutboxMessage
    from app.types import OutboundKind, OutboundMessage
    from app.workers.tasks_outbound import _stale_after_takeover

    await client_says(deps, llm, "q-3", "Здравствуйте")
    await human_says(deps, "q-4", "Отвечаю сам.")

    async with deps.sessionmaker() as db:
        conv = (await db.execute(sa.select(Conversation))).scalars().one()
        row = (await db.execute(sa.select(OutboxMessage))).scalars().first()
        message = OutboundMessage.model_validate(row.payload).model_copy(
            update={"kind": OutboundKind.OPERATOR_REPLY}
        )

        assert not await _stale_after_takeover(db, conv, row, message, now=datetime.now(tz=UTC))


async def test_escalation_notice_of_the_same_turn_still_goes_out(deps, llm) -> None:
    """«Передаю администратору» ставит тот же ход, что и паузу.

    Если считать «стоит ли пауза», эта строка не ушла бы никогда, и клиент
    остался бы вообще без ответа. Поэтому сравнивается порядок во времени.
    """
    import sqlalchemy as sa

    from app.storage.models import OutboxMessage
    from app.types import OutboundMessage
    from app.workers.tasks_outbound import _stale_after_takeover

    await process_inbound(
        deps, webhook_payload("q-5", "свяжите меня с администратором", chat_id=CHAT_ID)
    )

    async with deps.sessionmaker() as db:
        conv = (await db.execute(sa.select(Conversation))).scalars().one()
        row = (await db.execute(sa.select(OutboxMessage))).scalars().first()
        assert row is not None
        message = OutboundMessage.model_validate(row.payload)

        assert not await _stale_after_takeover(db, conv, row, message, now=datetime.now(tz=UTC))


# --------------------------------------------------------------------------- #
# Своя эскалация — не перехват
# --------------------------------------------------------------------------- #
async def test_bot_handing_over_still_answers_the_client(deps, llm) -> None:
    """Бот передал диалог человеку сам — клиент обязан это услышать.

    Живой аудит 03.09.2026: на «кто у вас тренер», на жалобу и на вопрос без
    данных клиент не получал НИЧЕГО. Карточка администратору уходила, а человек
    оставался в тишине. Причина — инструмент передачи ставит паузу, а проверка
    «не вошёл ли человек» считала паузой приход человека и отменяла ответ.
    """
    from app.llm.client import FakeCall

    llm.reset(
        [
            FakeTurn.tool(
                FakeCall(
                    "escalate_to_manager",
                    {"reason": "no_data", "question_summary": "кто тренер"},
                )
            ),
            FakeTurn.answer("Уточню у администратора и вернусь с ответом."),
        ]
    )

    decisions = await process_inbound(
        deps, webhook_payload("esc-1", "Кто у вас тренер?", chat_id=CHAT_ID)
    )

    texts = [out.text for d in decisions for out in d.outbound if out.text]
    assert texts, "клиент остался без ответа при передаче администратору"
    assert any(d.manager_cards for d in decisions), "администратор не получил карточку"


async def test_human_entering_during_the_turn_still_cancels_the_reply(deps, llm, monkeypatch) -> None:
    """Опора: главная страховка на месте.

    Пока модель думает, человек успевает ответить сам. Такой ответ бота обязан
    быть отменён — иначе он пишет поверх человека. Проверяется именно этот
    случай, а не «стоит ли пауза»: паузу в том же ходу ставит и сам бот.
    """
    from app.core import pipeline as pipeline_module

    await client_says(deps, llm, "race-1", "Здравствуйте")

    async def wrote_while_the_model_worked(*args, **kwargs):  # type: ignore[no-untyped-def]
        return datetime.now(tz=UTC)

    monkeypatch.setattr(
        pipeline_module.pause, "operator_last_seen", wrote_while_the_model_worked
    )

    decisions = await client_says(deps, llm, "race-2", "А сколько стоит?")

    assert actions(decisions) == ["silent"]
    assert not [out for d in decisions for out in d.outbound]
