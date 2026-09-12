"""Запись на конкретное время: вопрос о времени не снимается, напоминание не дублирует.

Владелец 10.09.2026: «пускай бот полностью сам конвертирует: мы записали вас на
19:00». Для этого бот спрашивает время из расписания — и этот вопрос обязан дойти
до клиента, а напоминание о пробном не должно прийти следом за подтверждением.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
from app.storage import repo_lead
from app.storage.models import Conversation, FollowupTask
from app.types import (
    ChannelKind,
    DecisionAction,
    FollowupKind,
    Language,
    LeadDraft,
    LeadStatus,
    PipelineDecision,
)
from app.workers.tasks_followup import schedule_followups

from tests.conftest import RecordingQueue, webhook_payload

UTC = timezone.utc
GYM = "ksk_kairbekova_334"


async def test_booking_without_a_time_asks_by_the_schedule(kb, state, sessionmaker, settings) -> None:
    """Время не выбрано — инструмент возвращает варианты, и вопрос по ним доходит до клиента.

    Раньше варианты шли текстом ошибки, и фильтр снял бы «в 17:00 или 19:00?» как
    выдуманное время — клиент получил бы «ответит администратор».
    """
    kb_loader.swap(kb)
    llm = FakeLLMClient(
        [
            FakeTurn.tool(
                FakeCall(
                    "create_trial_lead",
                    {"child_name": "Иванов Али", "child_age": 8, "gym_id": GYM, "parent_agreed": True},
                )
            ),
            FakeTurn.answer("В какое время удобнее: 09:00, 17:00 или 19:00?"),
        ]
    )
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=llm, kb=kb_loader.get_snapshot,
        queue=RecordingQueue(), settings=settings,
    )

    decisions = await process_inbound(
        deps, webhook_payload("tb-1", "Да, записывайте", chat_id="77015559001")
    )

    booking = [inv for d in decisions for inv in d.invocations if inv.name == "create_trial_lead"]
    assert booking and booking[0].result.data.get("booked") is False
    assert {"09:00", "17:00", "19:00"} <= {o["time_start"] for o in booking[0].result.data["options"]}
    assert [d.action.value for d in decisions] == ["reply"], "вопрос о времени снят фильтром"
    client = "\n".join(out.text or "" for d in decisions for out in d.outbound)
    assert "17:00" in client and "Мы записали вас" not in client


async def _booked_conversation(sessionmaker, chat: str, trial_slot: datetime):
    async with sessionmaker() as db:
        conv = Conversation(conv_key=f"wa:{chat}", channel_id="wa-1", chat_type="whatsapp", chat_id=chat)
        db.add(conv)
        await db.flush()
        await repo_lead.upsert(
            db,
            LeadDraft(
                conversation_id=conv.id, lang=Language.RU, status=LeadStatus.TRIAL_BOOKED,
                child_name="Иванов Али", child_age=8, gym_id=GYM,
                channel=ChannelKind.WHATSAPP, channel_user=chat, trial_slot=trial_slot,
            ),
        )
        await db.commit()
        return conv.id


async def _pending_kinds(sessionmaker, conv_id, kb) -> set[str]:
    async with sessionmaker() as db:
        conv = await db.get(Conversation, conv_id)
        await schedule_followups(
            db,
            conv,
            decision=PipelineDecision(action=DecisionAction.REPLY, reason="reply", conversation_id=conv_id),
            policy=kb.policies.followup_policy,
        )
        await db.commit()
        rows = (
            await db.execute(
                sa.select(FollowupTask.kind).where(
                    FollowupTask.conversation_id == conv_id, FollowupTask.state == "pending"
                )
            )
        ).scalars().all()
    return set(rows)


async def test_reminder_right_after_booking_is_not_scheduled(kb, sessionmaker) -> None:
    """Записали на занятие через 2,5 часа — «пробное уже сегодня» через полчаса дублирует подтверждение."""
    conv_id = await _booked_conversation(
        sessionmaker, "77015559002", datetime.now(tz=UTC) + timedelta(minutes=150)
    )

    kinds = await _pending_kinds(sessionmaker, conv_id, kb)

    assert FollowupKind.TRIAL_REMINDER_2H.value not in kinds, kinds


async def test_reminders_for_tomorrows_trial_are_kept(kb, sessionmaker) -> None:
    """Пробное завтра — оба напоминания на месте: они и есть защита от неявки."""
    conv_id = await _booked_conversation(
        sessionmaker, "77015559003", datetime.now(tz=UTC) + timedelta(hours=26)
    )

    kinds = await _pending_kinds(sessionmaker, conv_id, kb)

    assert {FollowupKind.TRIAL_REMINDER_20H.value, FollowupKind.TRIAL_REMINDER_2H.value} <= kinds, kinds
    assert FollowupKind.NO_SHOW.value not in kinds


# --------------------------------------------------------------------------- #
# Фамилия и имя
# --------------------------------------------------------------------------- #
async def _book(kb, state, sessionmaker, settings, chat: str, args: dict):
    kb_loader.swap(kb)
    llm = FakeLLMClient([FakeTurn.tool(FakeCall("create_trial_lead", args)), FakeTurn.answer("Хорошо.")])
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=llm, kb=kb_loader.get_snapshot,
        queue=RecordingQueue(), settings=settings,
    )
    decisions = await process_inbound(deps, webhook_payload(f"{chat}-1", "Да, Али, 8 лет, на 19:00", chat_id=chat))
    booking = [inv for d in decisions for inv in d.invocations if inv.name == "create_trial_lead"]
    client = "\n".join(out.text or "" for d in decisions for out in d.outbound)
    return booking[0].result, client


async def test_name_without_surname_is_asked_first(kb, state, sessionmaker, settings) -> None:
    """«Для записи спрашивать не только имя, а ФИ» — одно слово записью не считается."""
    result, client = await _book(kb, state, sessionmaker, settings, "77015559101", {
        "child_name": "Али", "child_age": 8, "gym_id": GYM, "parent_agreed": True,
        "session_time": "19:00",
    })

    assert result.data["booked"] is False and result.data["needs"] == ["need_surname"]
    assert "Мы записали вас" not in client


async def test_parent_who_declines_the_surname_is_still_booked(kb, state, sessionmaker, settings) -> None:
    """Не хочет называть фамилию — записываем как есть: заявку терять нельзя."""
    result, client = await _book(kb, state, sessionmaker, settings, "77015559102", {
        "child_name": "Али", "child_age": 8, "gym_id": GYM, "parent_agreed": True,
        "session_time": "19:00", "no_surname": True,
    })

    assert result.data["booked"] is True
    assert "Мы записали вас" in client and "👤 Али" in client


async def test_missing_surname_is_asked_before_time(kb, state, sessionmaker, settings) -> None:
    """Не хватает и фамилии, и времени — сначала фамилия, время следующим сообщением.

    Один вопрос за сообщение: на скриншоте владельца 11.09.2026 модель получила
    «спроси всё одним сообщением», выбрала одно, и возраст так и не спросили.
    """
    result, _ = await _book(kb, state, sessionmaker, settings, "77015559103", {
        "child_name": "Али", "child_age": 8, "gym_id": GYM, "parent_agreed": True,
    })

    assert result.data["needs"] == ["need_surname", "need_time"]
    assert any("Спроси только это" in caveat for caveat in result.caveats)
    assert not any("session_time" in caveat for caveat in result.caveats), "подсказка о времени — следующим шагом"
