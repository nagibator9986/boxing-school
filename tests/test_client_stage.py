"""Бот помнит, на какой стадии клиент, и не начинает разговор заново.

Живой случай 03.09.2026. Менеджер вёл клиента к оплате абонемента: тот уже
сходил на пробное и написал «да, всё понравилось». Бот ответил: «Хотите
записать ребёнка на бесплатное пробное занятие?» — и сбил разговор.

Причин было две, и обе здесь закрыты: напоминание бота не попадало в его же
историю, а стадия клиента не доходила до модели вовсе.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeLLMClient, FakeTurn
from app.storage.models import Conversation, Lead
from app.types import LeadStatus

from tests.conftest import RecordingQueue, webhook_payload

CHAT_ID = "77015556161"


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


async def say(deps, llm, message_id: str, text: str, answer: str = "Хорошо."):
    llm.reset([FakeTurn.answer(answer)])
    return await process_inbound(deps, webhook_payload(message_id, text, chat_id=CHAT_ID))


async def set_status(deps, status: LeadStatus) -> None:
    """Заявка со статусом — то, что остаётся в базе после работы с клиентом."""
    from datetime import datetime, timezone
    from uuid import uuid4

    async with deps.sessionmaker() as db:
        conv = (await db.execute(sa.select(Conversation))).scalars().one()
        changed = (
            await db.execute(
                sa.update(Lead)
                .where(Lead.conversation_id == conv.id)
                .values(status=status.value)
            )
        ).rowcount
        if not changed:
            now = datetime.now(tz=timezone.utc)
            db.add(
                Lead(
                    id=uuid4(),
                    conversation_id=conv.id,
                    created_at=now,
                    updated_at=now,
                    channel="whatsapp",
                    child_name="",
                    child_age=0,
                    child_gender="unknown",
                    phone_source="none",
                    status=status.value,
                    escalation=False,
                    messages_count=1,
                )
            )
        await db.commit()


async def note_of_last_call(llm: FakeLLMClient) -> str:
    """Служебная заметка, которую модель получила последним элементом запроса."""
    request = llm.requests[-1]
    return request.dynamic_note or ""


# --------------------------------------------------------------------------- #
# Стадия клиента доходит до модели
# --------------------------------------------------------------------------- #
async def test_model_is_told_the_client_already_booked(deps, llm) -> None:
    """Клиент уже записан на пробное — модель обязана это знать.

    Иначе она предлагает записаться ещё раз тому, кто уже приходил.
    """
    await say(deps, llm, "st-1", "Здравствуйте, сколько стоит?")
    await set_status(deps, LeadStatus.TRIAL_BOOKED)

    await say(deps, llm, "st-2", "А во сколько занятия?")

    note = await note_of_last_call(llm)
    assert "уже записан на пробное" in note
    assert "Не предлагай записаться ещё раз" in note


async def test_model_is_told_the_client_pays_already(deps, llm) -> None:
    """Действующий клиент: ни пробного, ни продажи — его ведёт администратор."""
    await say(deps, llm, "st-3", "Здравствуйте")
    await set_status(deps, LeadStatus.CONVERTED)

    await say(deps, llm, "st-4", "А когда следующая тренировка?")

    assert "уже купил абонемент" in await note_of_last_call(llm)


async def test_new_client_gets_no_stage_note(deps, llm) -> None:
    """Опора: у нового клиента стадии нет, и выдумывать её нельзя."""
    await say(deps, llm, "st-5", "Здравствуйте, сколько стоит?")

    note = await note_of_last_call(llm)
    assert "уже записан" not in note
    assert "уже купил" not in note


# --------------------------------------------------------------------------- #
# Напоминание бота остаётся в его же истории
# --------------------------------------------------------------------------- #
async def test_followup_text_enters_the_model_history(deps, llm, monkeypatch) -> None:
    """Иначе следующий ход модель начинает с пустого места.

    Живой случай: бот спросил «как прошла первая тренировка», клиент ответил
    «да, всё понравилось» — а модель этого вопроса не видела и предложила
    записаться на пробное занятие тому, кто уже отзанимался.
    """
    from app.core import session as conv_session
    from app.storage.models import FollowupTask
    from app.types import FollowupKind
    from app.workers import tasks_followup

    # Тихие часы — отдельное правило со своими тестами. Здесь они только сделали
    # бы результат зависимым от времени суток на машине: ночью напоминание
    # переносится на утро и в историю, разумеется, не попадает.
    monkeypatch.setattr(tasks_followup, "is_quiet_hours", lambda *a, **k: False)

    await say(deps, llm, "fu-1", "Здравствуйте, сколько стоит?")

    async with deps.sessionmaker() as db:
        conv = (await db.execute(sa.select(Conversation))).scalars().one()
        before = len(await conv_session.load_history(db, conv, max_turns=20))
        message = await tasks_followup._build_message(
            deps, db, conv, kind=FollowupKind.FU_VALUE
        )
        assert message is not None and message.text, "шаблона напоминания нет — проверять нечего"

    ctx = {"deps": deps, "correlation_id": "test"}
    async with deps.sessionmaker() as db:
        task = FollowupTask(
            conversation_id=conv.id,
            kind=FollowupKind.FU_VALUE.value,
            run_at=__import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc),
            state="pending",
            attempt=0,
        )
        db.add(task)
        await db.commit()
        task_id = str(task.id)

    await tasks_followup.send_followup_job(ctx, task_id)

    async with deps.sessionmaker() as db:
        conv = (await db.execute(sa.select(Conversation))).scalars().one()
        history = await conv_session.load_history(db, conv, max_turns=20)

    assert len(history) > before, "напоминание не попало в историю модели"
    assert any(
        message.text[:30] in str(item) for item in history
    ), "в истории нет текста напоминания"


# --------------------------------------------------------------------------- #
# Сказанное секунду назад не переспрашивают
# --------------------------------------------------------------------------- #
async def test_note_marks_what_the_client_just_said(deps, llm) -> None:
    """Телефон и возраст из последней реплики попадают в заметку отдельно.

    Список «уже известно» модель читала и раньше, но с последней репликой не
    связывала: 03.09.2026 клиент написал «Айгерим, телефон 87015551122» и в
    ответ получил «как зовут дочку?».
    """
    await say(deps, llm, "js-1", "Сыну 9 лет, телефон 87015551122")

    note = await note_of_last_call(llm)
    assert "В последнем сообщении клиент назвал" in note
    assert "телефон" in note
    assert "возраст ребёнка — 9" in note


async def test_note_stays_clean_without_new_facts(deps, llm) -> None:
    """Клиент ничего не назвал — лишней строки в заметке нет."""
    await say(deps, llm, "js-2", "А где вы находитесь?")

    assert "В последнем сообщении клиент назвал" not in await note_of_last_call(llm)
