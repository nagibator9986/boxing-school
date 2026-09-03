"""Дожимка: напомнить замолчавшему, но не тому, кто попрощался.

Владелец: «если клиент не отвечает в течение получаса — по контексту переписки
может быть, он забыл, клиента нужно дожать. Но если диалог пришёл к какому-то
завершению, напоминание отправлять не нужно, и если оператор взял клиента —
тоже».

Три правила, и все три проверяются здесь: срок, завершённый разговор и человек
в диалоге.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.kb.models import KBSnapshot
from app.llm.client import FakeLLMClient, FakeTurn
from app.storage.models import Conversation, FollowupTask
from app.types import FollowupKind
from app.workers.tasks_followup import is_closing_phrase

from tests.conftest import RecordingQueue, webhook_payload

UTC = timezone.utc
CHAT_ID = "77015557788"


# --------------------------------------------------------------------------- #
# Срок
# --------------------------------------------------------------------------- #
def test_soft_nudge_waits_half_an_hour(kb: KBSnapshot) -> None:
    """Через полчаса клиент ещё помнит разговор, через два часа — уже нет.

    Раньше мягкое напоминание уходило через два часа: столько времени на
    остывшего клиента расходовать незачем.
    """
    rule = next(r for r in kb.policies.followup_policy if r.event is FollowupKind.FU_SOFT)

    assert rule.delay_hours == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Завершённый разговор
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    ["Отлично спасибо большое", "Спасибо!", "Хорошо, спасибо", "До встречи", "Договорились"],
)
def test_farewell_closes_the_talk(text: str, kb: KBSnapshot) -> None:
    """После прощания напоминание — навязчивость."""
    assert is_closing_phrase(text, closing_words=kb.policies.followup_closing_words)


@pytest.mark.parametrize(
    "text",
    [
        "Спасибо, а во сколько занятие?",
        "Понял, а сколько стоит абонемент?",
        "Хочу записать ребёнка",
        "Спасибо за информацию, но надо подумать и посоветоваться с мужем",
    ],
)
def test_question_is_not_a_farewell(text: str, kb: KBSnapshot) -> None:
    """«Спасибо» в начале вопроса — вежливость, а не конец разговора.

    Именно таких клиентов и надо дожимать: они не отказались, а задумались.
    """
    assert not is_closing_phrase(text, closing_words=kb.policies.followup_closing_words)


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


async def client_says(deps, llm, message_id: str, text: str, answer: str = "Расскажу подробнее."):
    llm.reset([FakeTurn.answer(answer)])
    return await process_inbound(deps, webhook_payload(message_id, text, chat_id=CHAT_ID))


async def pending_nudges(deps) -> list[str]:
    """Какие напоминания назначены и ждут своего часа."""
    async with deps.sessionmaker() as db:
        rows = (
            await db.execute(
                sa.select(FollowupTask.kind).where(FollowupTask.state == "pending")
            )
        ).scalars().all()
    return list(rows)


async def test_silent_client_gets_a_nudge_scheduled(deps, llm) -> None:
    """Клиент спросил и замолчал — напоминание назначено."""
    await client_says(deps, llm, "nu-1", "А сколько стоит абонемент?")

    assert FollowupKind.FU_SOFT.value in await pending_nudges(deps)


async def test_farewell_cancels_the_nudge(deps, llm) -> None:
    """Клиент попрощался — напоминания не будет.

    Живая переписка 03.09.2026 заканчивалась словами «Отлично спасибо большое».
    Напоминание через полчаса после них выглядело бы навязчивостью.
    """
    await client_says(deps, llm, "nu-2", "А сколько стоит абонемент?")
    await client_says(deps, llm, "nu-3", "Отлично спасибо большое", answer="Спасибо, ждём вас!")

    assert await pending_nudges(deps) == []


async def test_operator_takeover_cancels_the_nudge(deps, llm) -> None:
    """Человек взял диалог — бот в него не возвращается даже напоминанием."""
    await client_says(deps, llm, "nu-4", "А сколько стоит абонемент?")
    assert await pending_nudges(deps), "опора теста: напоминание было назначено"

    await process_inbound(
        deps, webhook_payload("nu-5", "Здравствуйте, отвечает администратор", chat_id=CHAT_ID, is_echo=True)
    )

    assert await pending_nudges(deps) == []


async def test_nudge_is_not_sent_to_a_client_who_replied(deps, llm) -> None:
    """Клиент ответил сам — напоминание снимается, а не уходит вдогонку."""
    await client_says(deps, llm, "nu-6", "А сколько стоит?")
    await client_says(deps, llm, "nu-7", "А в каком зале?", answer="В любом из семи.")

    async with deps.sessionmaker() as db:
        conv = (await db.execute(sa.select(Conversation))).scalars().one()
        fresh = (
            await db.execute(
                sa.select(sa.func.count())
                .select_from(FollowupTask)
                .where(
                    FollowupTask.conversation_id == conv.id,
                    FollowupTask.kind == FollowupKind.FU_SOFT.value,
                    FollowupTask.state == "pending",
                    FollowupTask.run_at > datetime.now(tz=UTC) + timedelta(minutes=20),
                )
            )
        ).scalar_one()

    assert fresh == 1, "напоминание должно быть переназначено от последней реплики"
