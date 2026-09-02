"""Меню из четырёх пунктов: когда показывается, а когда нет.

Владелец увидел на «здравствуйте» короткий ответ модели вместо списка «1–4» и
спросил, куда делся выбор. Ответ оказался в правиле «поздороваться ровно один
раз на диалог»: он писал в этот чат раньше, и для бота это была середина
разговора. Для человека, вернувшегося через недели, разговор начинается заново.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence

import pytest
import sqlalchemy as sa

from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeLLMClient, FakeTurn
from app.storage.models import Message
from app.types import Language, PipelineDecision

from tests.conftest import RecordingQueue, webhook_payload

UTC = timezone.utc
CHAT_ID = "77015552222"


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


async def say(deps, llm, message_id: str, text: str, script: Sequence[FakeTurn] = ()):
    """Один ход клиента; модель отвечает по скрипту, если до неё дойдёт."""
    llm.reset(list(script) or [FakeTurn.answer("Чем я могу вам помочь?")])
    return await process_inbound(deps, webhook_payload(message_id, text, chat_id=CHAT_ID))


def replies(decisions: Sequence[PipelineDecision]) -> str:
    return "\n".join(out.text or "" for d in decisions for out in d.outbound)


async def age_bot_messages(deps, *, days: int) -> None:
    """Отодвигает ответы бота в прошлое — так выглядит вернувшийся клиент."""
    moment = datetime.now(tz=UTC) - timedelta(days=days)
    async with deps.sessionmaker() as db:
        await db.execute(sa.update(Message).values(created_at=moment))
        await db.commit()


# --------------------------------------------------------------------------- #
# Когда меню показывается
# --------------------------------------------------------------------------- #
async def test_first_greeting_shows_the_menu(deps, llm, kb) -> None:
    """Первое «здравствуйте» — шаблон с выбором, без обращения к модели."""
    answer = replies(await say(deps, llm, "gr-1", "Здравствуйте"))

    assert kb.text("greeting.first", Language.RU) == answer
    assert "4. Написать менеджеру" in answer


async def test_second_greeting_in_the_same_talk_does_not_repeat_the_menu(deps, llm) -> None:
    """Внутри разговора повтор меню выглядел бы как сброс диалога."""
    await say(deps, llm, "gr-2", "Здравствуйте")
    answer = replies(await say(deps, llm, "gr-3", "Здравствуйте"))

    assert "1. Записаться" not in answer


async def test_returning_client_sees_the_menu_again(deps, llm, kb) -> None:
    """Клиент вернулся через месяц — для него разговор начинается заново.

    Ровно этот случай владелец и принял за поломку: «где выбор пути от 1 до 4».
    Правило «ровно один раз» на деле означало «один раз навсегда».
    """
    await say(deps, llm, "gr-4", "Здравствуйте")
    await age_bot_messages(deps, days=30)

    answer = replies(await say(deps, llm, "gr-5", "Здравствуйте"))

    assert kb.text("greeting.first", Language.RU) == answer


async def test_short_break_does_not_bring_the_menu_back(deps, llm) -> None:
    """Пауза в пару дней — это тот же разговор, а не новый."""
    await say(deps, llm, "gr-6", "Здравствуйте")
    await age_bot_messages(deps, days=2)

    answer = replies(await say(deps, llm, "gr-7", "Здравствуйте"))

    assert "1. Записаться" not in answer


async def test_question_never_gets_the_menu(deps, llm) -> None:
    """На вопрос отвечают, а не показывают список: это правило не менялось."""
    answer = replies(
        await say(
            deps,
            llm,
            "gr-8",
            "Здравствуйте, сколько стоит?",
            [FakeTurn.answer("Абонемент в Костанае — 25 000 ₸ за 12 занятий.")],
        )
    )

    assert "1. Записаться" not in answer


# --------------------------------------------------------------------------- #
# Завершение диалога из CRM
# --------------------------------------------------------------------------- #
async def test_closed_dialog_starts_over_with_the_menu(deps, llm, kb) -> None:
    """Владелец завершил разговор — клиент, написавший снова, начинает как новый.

    Ждать неделю ради этого незачем: решение «разговор окончен» принимает
    человек, и кнопка в CRM делает ровно это.
    """
    from app.storage.models import Conversation

    await say(deps, llm, "gr-9", "Здравствуйте")
    async with deps.sessionmaker() as db:
        await db.execute(sa.update(Conversation).values(state="closed"))
        await db.commit()

    answer = replies(await say(deps, llm, "gr-10", "Здравствуйте"))

    assert kb.text("greeting.first", Language.RU) == answer


async def test_reopened_dialog_returns_to_work(deps, llm) -> None:
    """Открыв диалог заново, клиент не остаётся в «завершённом» навсегда.

    Иначе меню показывалось бы на каждое «здравствуйте», а напоминания
    считали бы диалог мёртвым и не уходили никогда.
    """
    from app.storage.models import Conversation

    await say(deps, llm, "gr-11", "Здравствуйте")
    async with deps.sessionmaker() as db:
        await db.execute(sa.update(Conversation).values(state="closed"))
        await db.commit()

    await say(deps, llm, "gr-12", "Здравствуйте")

    async with deps.sessionmaker() as db:
        state = (await db.execute(sa.select(Conversation.state))).scalar_one()
    assert state == "active"

    answer = replies(await say(deps, llm, "gr-13", "Здравствуйте"))
    assert "1. Записаться" not in answer
