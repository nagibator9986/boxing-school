"""Номера, которым бот не отвечает.

Тренеры пишут в тот же WhatsApp, что и родители: спросить про зал, договориться
о замене. Бот отвечал им как клиентам и заводил заявки, которых не было.

Номера здесь вымышленные. Настоящие живут в настройках владельца и в публичный
репозиторий не попадают: это персональные данные сотрудников школы.
"""

from __future__ import annotations

from typing import Sequence

import pytest

from app.admin.runtime_settings import RuntimeSettings
from app.core import ignore_list
from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeLLMClient, FakeTurn
from app.types import PipelineDecision

from tests.conftest import RecordingQueue, webhook_payload

TRAINER = "77770000001"
PARENT = "77015559999"


# --------------------------------------------------------------------------- #
# Разбор списка
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "written",
    [
        "77770000001",
        "+7 777 000 00 01",
        "8 777 000 0001",
        "+7 (777) 000-00-01",
        "777 000 0001",
    ],
)
def test_one_number_written_five_ways(written: str) -> None:
    """Формат записи не должен решать, сработает исключение или нет.

    Владелец копирует номера из заметок и переписки как есть. Требовать единого
    написания — значит однажды гарантированно не совпасть.
    """
    assert ignore_list.is_ignored(
        ignore_list.parse(written), chat_id=TRAINER, phone=None
    )


def test_separators_are_free_form() -> None:
    """Запятая и перевод строки разделяют, а пробел живёт внутри номера."""
    parsed = ignore_list.parse("+7 777 000 00 01, 8 708 111 2233\n77762223344")

    assert len(parsed) == 3


def test_numbers_typed_in_a_row_do_not_glue_together() -> None:
    """Слипшиеся номера дали бы десять последних цифр несуществующего абонента.

    Пробел не разделитель — иначе от «+7 777 000 00 01» остаются обрывки. Но
    запись, где цифр больше, чем бывает в одном номере, всё же делится.
    """
    parsed = ignore_list.parse("77770000001 77081112233")

    assert parsed == frozenset({"7770000001", "7081112233"})


def test_empty_list_ignores_nobody() -> None:
    """Пустая настройка не имеет права молчать всем подряд."""
    assert ignore_list.parse("") == frozenset()
    assert not ignore_list.is_ignored(frozenset(), chat_id=TRAINER, phone=TRAINER)


def test_short_numbers_are_not_matched() -> None:
    """Обрывок номера не должен совпасть с кем попало."""
    assert ignore_list.parse("12345") == frozenset()
    assert not ignore_list.is_ignored(ignore_list.parse("77770000001"), chat_id="0001", phone=None)


def test_phone_field_is_checked_too() -> None:
    """В Instagram чат — не номер, телефон приходит отдельным полем."""
    assert ignore_list.is_ignored(
        ignore_list.parse(TRAINER), chat_id="instagram_17841400000000000", phone="+7 777 000 00 01"
    )


# --------------------------------------------------------------------------- #
# Поведение бота
# --------------------------------------------------------------------------- #
@pytest.fixture
async def llm() -> FakeLLMClient:
    return FakeLLMClient([])


@pytest.fixture
async def deps(kb, state, sessionmaker, settings, llm) -> PipelineDeps:
    """Пайплайн, где список исключений задан владельцем, как в CRM."""
    kb_loader.swap(kb)
    runtime = RuntimeSettings.from_values({"ignored_numbers": "+7 777 000 00 01"})
    return PipelineDeps(
        sessionmaker=sessionmaker,
        state=state,
        llm=llm,
        kb=kb_loader.get_snapshot,
        queue=RecordingQueue(),
        settings=settings,
        runtime=lambda: runtime,
    )


async def say(deps, llm, message_id: str, text: str, chat_id: str) -> Sequence[PipelineDecision]:
    llm.reset([FakeTurn.answer("Здравствуйте! Чем помочь?")])
    return await process_inbound(deps, webhook_payload(message_id, text, chat_id=chat_id))


async def test_trainer_gets_no_answer(deps, llm) -> None:
    """Сообщение тренера бот не обрабатывает вовсе."""
    decisions = await say(deps, llm, "ig-1", "Я завтра не смогу провести тренировку", TRAINER)

    assert [d.action.value for d in decisions] == ["drop"]
    assert [d.reason for d in decisions] == ["ignored_number"]
    assert not [out for d in decisions for out in d.outbound]


async def test_trainer_leaves_no_dialog_and_no_lead(deps, llm) -> None:
    """Рабочая переписка школы не должна выглядеть как поток клиентов."""
    import sqlalchemy as sa

    from app.storage.models import Conversation, Lead

    await say(deps, llm, "ig-2", "Здравствуйте", TRAINER)

    async with deps.sessionmaker() as db:
        conversations = (await db.execute(sa.select(sa.func.count()).select_from(Conversation))).scalar_one()
        leads = (await db.execute(sa.select(sa.func.count()).select_from(Lead))).scalar_one()

    assert conversations == 0
    assert leads == 0


async def test_parent_is_answered_as_before(deps, llm) -> None:
    """Опора теста: всем остальным бот отвечает по-прежнему."""
    decisions = await say(deps, llm, "ig-3", "Здравствуйте", PARENT)

    assert [d.action.value for d in decisions] == ["reply"]
