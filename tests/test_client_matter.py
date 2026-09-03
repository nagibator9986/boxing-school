"""Вопросы действующих клиентов ведёт человек, а не бот.

Владелец: «родители могут писать по любому вопросу — к примеру, Артём завтра
прийти не сможет, или нам неудобно заниматься в 19:00, давайте подумаем над
другим временем». Бот — консультант и продавец: журнала посещаемости у него
нет, переносить занятия он не вправе, и отвечать на такое моделью значит либо
выдумывать, либо обещать за администратора.

Ровно так 02.09.2026 и вышло: на вопрос про утро бот ответил, что утренних
групп нет, — и владелец написал «не верная информация».
"""

from __future__ import annotations

from typing import Sequence

import pytest

from app.core import guards
from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.kb.models import KBSnapshot
from app.llm.client import FakeLLMClient, FakeTurn
from app.types import EscalationReason, Language, PipelineDecision

from tests.conftest import RecordingQueue, webhook_payload

CHAT_ID = "77015553131"

#: Живые формулировки родителей: две из них владелец привёл сам.
CLIENT_MATTERS: tuple[str, ...] = (
    "Артем завтра прийти не сможет",
    "Артём сегодня не сможет прийти",
    "нам сейчас не удобно заниматься в 19:00 давайте подумаем над другим временем",
    "дочь заболела, пропустим тренировку в четверг",
    "хотим заморозить абонемент на месяц",
    "можно перенести занятие на пятницу?",
    "хотим поменять группу на более позднюю",
    "верните деньги за неиспользованные занятия",
    "мы опоздаем сегодня минут на пятнадцать",
)

#: Разговор о записи. Его бот ведёт сам — иначе он перестанет быть продавцом.
SALES_QUESTIONS: tuple[str, ...] = (
    "Здравствуйте, сколько стоит абонемент?",
    "А есть утренние группы?",
    "Какое расписание в зале на КСК?",
    "Хочу записать ребёнка на пробное занятие",
    "С какого возраста берёте детей?",
    "Где вы находитесь?",
)


@pytest.mark.parametrize("text", CLIENT_MATTERS)
def test_client_matters_go_to_a_human(text: str, kb: KBSnapshot) -> None:
    """Такие сообщения бот не отвечает моделью вовсе."""
    verdict = guards.scan(text, lang=Language.RU, lexicon=kb.lexicon, policies=kb.policies)

    assert verdict.blocked, f"бот взялся отвечать сам: {text}"
    assert verdict.escalate
    assert verdict.fixed_reply_key == "escalation.client_matter"


@pytest.mark.parametrize("text", SALES_QUESTIONS)
def test_sales_questions_stay_with_the_bot(text: str, kb: KBSnapshot) -> None:
    """Опора: разговор о записи не должен уехать к человеку целиком.

    Ради «пусть всё решает администратор» бота заводить незачем — он затем и
    нужен, чтобы отвечать на цены, залы, расписание и записывать на пробное.
    """
    verdict = guards.scan(text, lang=Language.RU, lexicon=kb.lexicon, policies=kb.policies)

    assert verdict.fixed_reply_key != "escalation.client_matter", f"уехало к человеку: {text}"


# --------------------------------------------------------------------------- #
# Сквозь пайплайн
# --------------------------------------------------------------------------- #
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


def replies(decisions: Sequence[PipelineDecision]) -> str:
    return "\n".join(out.text or "" for d in decisions for out in d.outbound)


async def test_absence_message_is_handed_over_without_the_model(deps, llm, kb) -> None:
    """Клиент получает честное «передаю администратору», а модель не зовётся.

    Проверяется и то, что модель не вызвана: пустой скрипт ``FakeLLMClient``
    упал бы, если бы пайплайн к ней обратился.
    """
    decisions = await process_inbound(
        deps, webhook_payload("cm-1", "Артем завтра прийти не сможет", chat_id=CHAT_ID)
    )

    assert [d.action.value for d in decisions] == ["escalate"]
    assert decisions[0].escalation_reason is EscalationReason.USER_REQUEST
    assert replies(decisions) == kb.text("escalation.client_matter", Language.RU)


async def test_reschedule_request_reaches_the_manager_card(deps, llm) -> None:
    """Администратор обязан узнать о просьбе: иначе клиент останется без ответа."""
    decisions = await process_inbound(
        deps,
        webhook_payload(
            "cm-2",
            "нам неудобно заниматься в 19:00, давайте подумаем над другим временем",
            chat_id=CHAT_ID,
        ),
    )

    assert any(d.manager_cards for d in decisions), "карточка администратору не собрана"


async def test_sales_question_still_answered_by_the_bot(deps, llm) -> None:
    """Опора теста: обычный вопрос о цене по-прежнему отвечает модель."""
    # Без чисел: цену бот обязан брать инструментом, иначе постфильтр снимет
    # ответ — и тест проверял бы уже не то, что задуман.
    llm.reset([FakeTurn.answer("Расскажу про абонементы. В каком районе удобно заниматься?")])

    decisions = await process_inbound(
        deps, webhook_payload("cm-3", "Сколько стоит абонемент?", chat_id=CHAT_ID)
    )

    assert [d.action.value for d in decisions] == ["reply"]


# --------------------------------------------------------------------------- #
# Просьба позвать человека: два разных случая
# --------------------------------------------------------------------------- #
def test_question_gets_a_short_handoff(kb: KBSnapshot) -> None:
    """Клиент уже задал вопрос — просить написать его ещё раз нельзя.

    03.09.2026 на «дайте номер телефона администратора» бот ответил «напишите,
    пожалуйста, ваш вопрос». Клиент его написал секундой раньше.
    """
    verdict = guards.scan(
        "Дайте номер телефона администратора",
        lang=Language.RU,
        lexicon=kb.lexicon,
        policies=kb.policies,
    )

    assert verdict.fixed_reply_key == "escalation.manager_with_question"


def test_bare_request_still_asks_for_the_question(kb: KBSnapshot) -> None:
    """Нажал пункт «Написать менеджеру» и ничего не спросил — вопрос уместен."""
    verdict = guards.scan(
        "Написать менеджеру", lang=Language.RU, lexicon=kb.lexicon, policies=kb.policies
    )

    assert verdict.fixed_reply_key == "escalation.manager_requested"


def test_forbidden_to_invent_reasons_behind_the_schedule(kb: KBSnapshot) -> None:
    """Причины расписания в базе знаний не записаны — выдумывать их запрещено.

    Живой аудит: «в других залах расписание зависит от смены в школе». Этого
    никто не говорил, а звучит как факт школы.
    """
    forbidden = " ".join(kb.policies.forbidden_behaviour).lower()

    assert "причин расписания" in forbidden
