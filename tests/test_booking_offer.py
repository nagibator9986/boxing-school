"""После расписания и видео — отдельным сообщением «Записать ребёнка?».

Владелец 10.09.2026: «вот после видео отдельным сообщением эту надпись:
записать ребёнка на первую пробную тренировку». На скриншоте вопрос о записи
пришёл раньше расписания и видео — внутри подписи с адресом.
"""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa

from app.core.funnel import drop_questions, turn_showed_a_gym
from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
from app.storage import repo_lead
from app.storage.models import Conversation
from app.types import ChannelKind, Language, LeadDraft, LeadStatus, OutboundKind, OutboundMessage

from tests.conftest import RecordingQueue, webhook_payload

SCHEDULE_TURN = [
    FakeTurn.tool(FakeCall("get_schedule", {"gym_id": "ksk_kairbekova_334"})),
    FakeTurn.answer("Зал совсем рядом с вами. Записать ребёнка на бесплатное пробное?"),
]


def _message(artifact_id: str | None) -> OutboundMessage:
    return OutboundMessage(
        conversation_id=uuid4(), channel_id="wa", channel=ChannelKind.WHATSAPP, chat_id="7701",
        lang=Language.RU, kind=OutboundKind.ARTIFACT, text="карточка", artifact_id=artifact_id,
    )


def test_schedule_or_route_means_a_gym_was_shown() -> None:
    assert turn_showed_a_gym([_message("schedule_ksk_kairbekova_334")])
    assert turn_showed_a_gym([_message("route_plaza_szm_70")])


def test_price_card_alone_is_not_a_gym() -> None:
    """После одних цен предлагать запись рано — клиент ещё не выбрал зал."""
    assert not turn_showed_a_gym([_message("price_card_city"), _message(None)])


def test_models_own_booking_offer_is_dropped() -> None:
    reply = "Зал совсем рядом с вами. Записать ребёнка на бесплатное пробное?"

    assert drop_questions(reply) == "Зал совсем рядом с вами."


def test_any_model_question_gives_way_to_the_offer() -> None:
    """Предложение записи — единственный вопрос хода.

    Живой прогон 10.09.2026: «Какое время из расписания вам подходит?» и следом
    «Записать ребёнка?» — два вопроса подряд.
    """
    assert drop_questions("Какое время из расписания выше вам подходит?") == ""
    assert drop_questions("Зал рядом с вами. Какое время подходит?") == "Зал рядом с вами."


async def _deps(kb, state, sessionmaker, settings, turns):
    kb_loader.swap(kb)
    llm = FakeLLMClient(turns)
    return llm, PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=llm, kb=kb_loader.get_snapshot,
        queue=RecordingQueue(), settings=settings,
    )


def _texts(decisions) -> list[str]:
    return [out.text or "" for d in decisions for out in d.outbound]


async def test_offer_goes_last_as_its_own_message(kb, state, sessionmaker, settings) -> None:
    """Расписание, затем короткий ответ, затем — отдельно — предложение записи."""
    _, deps = await _deps(kb, state, sessionmaker, settings, list(SCHEDULE_TURN))

    sent = _texts(await process_inbound(
        deps, webhook_payload("bo-1", "А какое расписание на КСК?", chat_id="77015556601")
    ))
    offer = kb.text("funnel.book_trial", Language.RU)

    assert "Расписание" in sent[0], sent
    assert sent[-1] == offer, sent
    assert sum("записат" in text.lower() for text in sent) == 1, f"предложение записи дважды: {sent}"
    assert all("?" not in text for text in sent[:-1]), f"вопрос помимо предложения записи: {sent}"


async def test_no_offer_when_no_gym_was_shown(kb, state, sessionmaker, settings) -> None:
    """Без расписания и видео предложение записи преждевременно — работает обычная воронка."""
    _, deps = await _deps(kb, state, sessionmaker, settings, [FakeTurn.answer("Первое занятие бесплатное.")])

    sent = _texts(await process_inbound(
        deps, webhook_payload("bo-2", "Пробное платное?", chat_id="77015556602")
    ))

    assert kb.text("funnel.book_trial", Language.RU) not in sent


async def test_no_offer_for_a_client_who_is_already_booked(kb, state, sessionmaker, settings) -> None:
    """Записанному второй раз «Записать ребёнка?» не шлём."""
    llm, deps = await _deps(kb, state, sessionmaker, settings, [FakeTurn.answer("Здравствуйте!")])
    await process_inbound(deps, webhook_payload("bo-3", "Добрый день", chat_id="77015556603"))
    async with sessionmaker() as db:
        conv = (await db.execute(sa.select(Conversation))).scalars().one()
        await repo_lead.upsert(
            db,
            LeadDraft(
                conversation_id=conv.id, lang=Language.RU, status=LeadStatus.TRIAL_BOOKED,
                child_name="Али", child_age=8, gym_id="ksk_kairbekova_334",
                channel=ChannelKind.WHATSAPP, channel_user="77015556603",
            ),
        )
        await db.commit()

    llm.reset(list(SCHEDULE_TURN))
    sent = _texts(await process_inbound(
        deps, webhook_payload("bo-4", "А расписание на КСК?", chat_id="77015556603")
    ))

    assert kb.text("funnel.book_trial", Language.RU) not in sent, sent
