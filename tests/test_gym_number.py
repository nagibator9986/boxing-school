"""Номер зала из списка — это ответ клиента, а не догадка модели.

Живой прогон 10.09.2026: карточка звала «напишите номер зала», клиент писал
«7», и бот отвечал «в семь лет ребёнок уже отлично понимает тренера». На «8» —
«как зовут вашего ребёнка?». Кнопок в мессенджерах нет, номер и есть выбор, и
разбирать его должен код — ровно как цифру в меню.
"""

from __future__ import annotations

import sqlalchemy as sa

from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeLLMClient, FakeTurn
from app.storage.models import Conversation

from tests.conftest import RecordingQueue, webhook_payload

CHAT = "77015552211"


async def _deps(kb, state, sessionmaker, settings, llm):
    kb_loader.swap(kb)
    return PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=llm, kb=kb_loader.get_snapshot,
        queue=RecordingQueue(), settings=settings,
    )


async def _conv(sessionmaker) -> Conversation:
    async with sessionmaker() as db:
        return (await db.execute(sa.select(Conversation))).scalars().one()


async def test_number_after_the_list_picks_that_gym(kb, state, sessionmaker, settings) -> None:
    """«7» после карточки залов — это седьмой зал, а не возраст ребёнка."""
    from app.core.pipeline import _gym_choice

    llm = FakeLLMClient([FakeTurn.answer("Здравствуйте!")])
    deps = await _deps(kb, state, sessionmaker, settings, llm)
    await process_inbound(deps, webhook_payload("gn-1", "Здравствуйте", chat_id=CHAT))
    await process_inbound(deps, webhook_payload("gn-2", "2", chat_id=CHAT))

    conv = await _conv(sessionmaker)
    async with sessionmaker() as db:
        picked = await _gym_choice(db, conv, "7", kb=kb)

    assert picked is not None, "номер зала не опознан"
    assert "Западный микрорайон" in picked, picked


async def test_suburb_number_picks_tobyl(kb, state, sessionmaker, settings) -> None:
    """Восьмой пункт — Тобыл: владелец считает его восьмым залом школы."""
    from app.core.pipeline import _gym_choice

    llm = FakeLLMClient([FakeTurn.answer("Здравствуйте!")])
    deps = await _deps(kb, state, sessionmaker, settings, llm)
    await process_inbound(deps, webhook_payload("gn-3", "Здравствуйте", chat_id=CHAT))
    await process_inbound(deps, webhook_payload("gn-4", "2", chat_id=CHAT))

    conv = await _conv(sessionmaker)
    async with sessionmaker() as db:
        picked = await _gym_choice(db, conv, "8", kb=kb)

    assert picked and "Тобыл" in picked, picked


async def test_digit_without_the_list_is_left_alone(kb, state, sessionmaker, settings) -> None:
    """Без карточки залов «7» остаётся возрастом: там это самая частая цифра."""
    from app.core.pipeline import _gym_choice

    llm = FakeLLMClient([FakeTurn.answer("Здравствуйте!")])
    deps = await _deps(kb, state, sessionmaker, settings, llm)
    await process_inbound(deps, webhook_payload("gn-5", "Здравствуйте", chat_id=CHAT))

    conv = await _conv(sessionmaker)
    async with sessionmaker() as db:
        assert await _gym_choice(db, conv, "7", kb=kb) is None


async def test_number_outside_the_list_is_left_alone(kb, state, sessionmaker, settings) -> None:
    """«25» после списка — это не зал, а скорее возраст или сумма."""
    from app.core.pipeline import _gym_choice

    llm = FakeLLMClient([FakeTurn.answer("Здравствуйте!")])
    deps = await _deps(kb, state, sessionmaker, settings, llm)
    await process_inbound(deps, webhook_payload("gn-6", "Здравствуйте", chat_id=CHAT))
    await process_inbound(deps, webhook_payload("gn-7", "2", chat_id=CHAT))

    conv = await _conv(sessionmaker)
    async with sessionmaker() as db:
        assert await _gym_choice(db, conv, "25", kb=kb) is None


async def test_zero_is_not_the_last_gym(kb, state, sessionmaker, settings) -> None:
    """«0» — не зал. Без проверки границ он выбрал бы последний в списке.

    Отрицательный индекс в Python берёт элемент с конца, и «0» молча превратился
    бы в Тобыл — клиент получил бы расписание зала, о котором не спрашивал.
    """
    from app.core.pipeline import _gym_choice

    llm = FakeLLMClient([FakeTurn.answer("Здравствуйте!")])
    deps = await _deps(kb, state, sessionmaker, settings, llm)
    await process_inbound(deps, webhook_payload("gn-8", "Здравствуйте", chat_id=CHAT))
    await process_inbound(deps, webhook_payload("gn-9", "2", chat_id=CHAT))

    conv = await _conv(sessionmaker)
    async with sessionmaker() as db:
        assert await _gym_choice(db, conv, "0", kb=kb) is None
