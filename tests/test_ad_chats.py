"""Чат, пришедший с рекламы, обязан получать ответ.

02.09.2026 клиент пришёл по объявлению в Instagram, написал «хочу записать
ребёнка на пробную тренировку» и «13 лет девочка» — и не получил ничего.
Причина: WhatsApp Business сам отправляет автоприветствие, оно возвращается к
нам эхом, которого нет в нашем outbox, и бот принимал его за живого оператора.
Дальше он ставил себе паузу на два часа и молчал на всё, что писал человек.

Здесь проверяется и починка, и то, что она не отменила защиту «бот не пишет
поверх человека»: после первой реплики клиента эхо снова означает оператора.
"""

from __future__ import annotations

from typing import Sequence

import pytest

from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeLLMClient, FakeTurn
from app.types import PipelineDecision

from tests.conftest import RecordingQueue, webhook_payload

CHAT_ID = "77776171888"

#: Текст, который WhatsApp Business подставляет в чат из рекламы.
AD_GREETING = "ЖМИ ОТПРАВИТЬ!"


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


async def client_says(deps, llm, message_id: str, text: str, answer: str = "Хорошо."):
    """Реплика клиента; модель отвечает по скрипту."""
    llm.reset([FakeTurn.answer(answer)])
    return await process_inbound(deps, webhook_payload(message_id, text, chat_id=CHAT_ID))


async def echo(deps, message_id: str, text: str):
    """Исходящее, которого нет в нашем outbox: автоприветствие или человек."""
    return await process_inbound(
        deps, webhook_payload(message_id, text, chat_id=CHAT_ID, is_echo=True)
    )


def actions(decisions: Sequence[PipelineDecision]) -> list[str]:
    return [d.action.value for d in decisions]


def replies(decisions: Sequence[PipelineDecision]) -> str:
    return "\n".join(out.text or "" for d in decisions for out in d.outbound)


# --------------------------------------------------------------------------- #
# Реклама
# --------------------------------------------------------------------------- #
async def test_ad_greeting_does_not_silence_the_bot(deps, llm) -> None:
    """Автоприветствие рекламы приходит раньше клиента — перехватывать некого."""
    first = await echo(deps, "ad-echo", AD_GREETING)
    assert [d.reason for d in first] == ["auto_greeting"]

    answer = await client_says(
        deps,
        llm,
        "ad-1",
        "Здравствуйте, хочу записать ребенка на пробную тренировку!",
        answer="Здравствуйте! Запишем. В каком районе удобно заниматься?",
    )

    assert actions(answer) == ["reply"]
    assert "Запишем" in replies(answer)


async def test_second_client_message_after_ad_is_answered_too(deps, llm) -> None:
    """Клиент дописывает возраст — бот отвечает, а не молчит два часа."""
    await echo(deps, "ad-echo-2", AD_GREETING)
    await client_says(deps, llm, "ad-2", "Хочу записать ребёнка")

    answer = await client_says(deps, llm, "ad-3", "13 лет девочка", answer="Отлично, подходит.")

    assert actions(answer) == ["reply"]


# --------------------------------------------------------------------------- #
# Защита «бот не пишет поверх человека» — на месте
# --------------------------------------------------------------------------- #
async def test_operator_after_the_client_still_silences_the_bot(deps, llm) -> None:
    """Как только клиент написал, эхо снова означает живого человека в диалоге.

    Это главная страховка владельца: «если я уже отвечаю клиенту, чтобы бот не
    писал параллельно». Починка рекламы не имеет права её ослабить.
    """
    await client_says(deps, llm, "op-1", "Здравствуйте")

    entered = await echo(deps, "op-echo", "Добрый день, это администратор школы")
    assert [d.reason for d in entered] == ["operator_entered"]

    after = await client_says(deps, llm, "op-2", "А сколько стоит?")
    assert actions(after) == ["silent"]


async def test_operator_first_then_client_is_answered(deps, llm) -> None:
    """Оборотная сторона правила, названная прямо.

    Исходящее до первой реплики клиента бот считает автоприветствием и на
    следующее сообщение отвечает. Если человек начал переписку сам и хочет вести
    её дальше, ему достаточно ответить ещё раз — эхо после реплики клиента снова
    ставит паузу.
    """
    await echo(deps, "manual-echo", "Здравствуйте, это школа бокса")

    answer = await client_says(deps, llm, "manual-1", "Здравствуйте", answer="Здравствуйте!")

    assert actions(answer) == ["reply"]
