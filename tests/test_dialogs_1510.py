"""Переписки 14–16.10.2026, аудио владельца 15.10.2026 и сообщения Зарины 16.10.2026.

Что было и что стало:

* ответ из CRM («Конечно можно, выздоравливайте») не попадал ни в историю модели, ни в
  отметку «человек ответил» — вернули бота, и он написал «Администратор уже в курсе»;
* «Да, но у нас нет ещё экипировки?» → «уточнит администратор», а после текста владельца
  — «передать оба вопроса?», и «Да пожалуйста» уводило разговор к администратору;
* «на вечернее время» не выбирало 19:00; «со следующего вторника можно, у нас отит» —
  передача администратору вместо переноса; «заболела», «опоздаем» у записанного на
  пробное — тоже; «Спасибо» после записи снимало напоминания о пробном;
* клиент из Instagram выбрал «среду, 17:00» по показанному расписанию, через четыре дня
  назвал фамилию — и получил список залов заново;
* «Как прошла тренировка?» через три минуты после начала занятия;
* «Тренер Марат Сапаров будет ждать» — такого тренера нет;
* «Выставите счёт удалённо» — к бухгалтеру; «Вы с 09:00 до пяти работаете?» — расписание
  всех залов; «Костанай 2, гостиница Остров» — Плаза, а не КСК;
* ссылка «Переписка:» в карточке открывала «Такого клиента нет».

Имена и номера в тестах вымышленные.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from app.admin.runtime_settings import RuntimeSettings
from app.channels.wazzup_schemas import SendMessageResponse
from app.core import funnel, guards, ignore_list, pause, pipeline
from app.core.pipeline import PipelineDeps, process_inbound
from app.core.postcheck import strip_invented_coaches
from app.kb import loader as kb_loader
from app.kb.agreement import with_agreement
from app.kb.sessions import client_day_parts, time_by_day_part
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
from app.storage import repo_conversation, repo_lead, repo_outbox
from app.storage.models import Conversation, FollowupTask, OutboxMessage
from app.tools.booking import create_trial_lead
from app.tools.escalation import escalate_to_manager
from app.tools.facts import get_kb_fact
from app.tools.gyms import find_gym_by_district, gyms_named_in
from app.types import (
    TRIAL_CONFIRMATION_ARTIFACT,
    ChannelKind,
    ConversationState,
    DecisionAction,
    FollowupKind,
    Language,
    LeadDraft,
    LeadStatus,
    OutboundKind,
    OutboundMessage,
    PauseReason,
    PipelineDecision,
    SavedLead,
    ToolStatus,
)
from app.workers.tasks_outbound import send_outbox_job

from tests.conftest import CHANNEL_ID, RecordingQueue, webhook_payload

UTC = timezone.utc
ALMATY = ZoneInfo("Asia/Almaty")
KSK = "ksk_kairbekova_334"
PLAZA = "plaza_szm_70"
TOBYL = "region_tobyl"
CENTER = "center_kasymkhanova_10"
#: Четверг 15.10.2026, 10:18 по Алматы — как в диалоге с переносом.
THURSDAY = datetime(2026, 10, 15, 10, 18, tzinfo=ALMATY)
ACCOUNTANT = "+7 700 000 00 99"


def _ctx(make_ctx, *texts: str, now: datetime = THURSDAY, **extra):
    return replace(make_ctx(client_texts=texts, dialog_client_texts=texts, **extra), now=now.astimezone(UTC))


def _deps(kb, state, sessionmaker, settings, turns=(), **extra) -> PipelineDeps:
    kb_loader.swap(kb)
    return PipelineDeps(
        sessionmaker=sessionmaker, state=state, kb=kb_loader.get_snapshot, queue=RecordingQueue(),
        settings=settings, llm=FakeLLMClient(list(turns)), **extra,
    )


def _client_text(decisions) -> str:
    return "\n".join(out.text or "" for d in decisions for out in d.outbound)


async def _say(deps: PipelineDeps, chat: str, number: int, text: str, *turns):
    deps.llm.reset(list(turns))
    return await process_inbound(deps, webhook_payload(f"{chat}-{number}", text, chat_id=chat))


async def _book(sessionmaker, conv_id, *, gym_id: str, slot: datetime, child: str = "Иванова Айша", age: int = 7):
    async with sessionmaker() as db:
        await repo_lead.upsert(
            db,
            LeadDraft(
                conversation_id=conv_id, lang=Language.RU, status=LeadStatus.TRIAL_BOOKED,
                child_name=child, child_age=age, gym_id=gym_id, channel=ChannelKind.WHATSAPP,
                trial_slot=slot,
            ),
        )
        await db.commit()


# --------------------------------------------------------------------------- #
# Ответ из CRM и возврат бота закрывают передачу администратору
# --------------------------------------------------------------------------- #
class _Wazzup:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, request):
        self.sent.append(request.text or "")
        return SendMessageResponse(messageId=str(uuid4()), chatId=request.chatId)


async def test_crm_reply_reaches_the_model_and_closes_the_handover(kb, state, sessionmaker, settings) -> None:
    deps = _deps(kb, state, sessionmaker, settings)
    now = datetime.now(tz=UTC)
    async with sessionmaker() as db:
        conv = Conversation(conv_key="wa:77015551501", channel_id=CHANNEL_ID, chat_type="whatsapp", chat_id="77015551501")
        db.add(conv)
        await db.flush()
        await pause.set_pause(
            state, db, conv.id, conv.conv_key, minutes=60, reason=PauseReason.ESCALATION,
            now=now - timedelta(minutes=30),
        )
        await db.commit()
        conv_id = conv.id
        assert await pause.escalation_open(db, conv_id, now=now, within_minutes=60)
        outbox_id = await repo_outbox.enqueue(db, OutboundMessage(
            conversation_id=conv_id, channel_id=CHANNEL_ID, channel=ChannelKind.WHATSAPP,
            chat_id="77015551501", lang=Language.RU, kind=OutboundKind.OPERATOR_REPLY,
            text="Конечно можно, выздоравливайте.",
        ))
        await db.commit()

    await send_outbox_job({"deps": deps, "wazzup": _Wazzup()}, str(outbox_id))

    async with sessionmaker() as db:
        conv = await repo_conversation.get_by_id(db, conv_id)
        stored = await pipeline._stored_contents(db, conv)
        assert "Конечно можно, выздоравливайте." in str(stored), "бот обязан видеть ответ человека"
        assert not await pause.escalation_open(db, conv_id, now=datetime.now(tz=UTC), within_minutes=60)


@pytest.mark.parametrize(("by", "closed"), [
    ("admin", True), ("operator_command", True), ("timeout", False), ("client_wrote_again", False),
])
async def test_only_a_human_resume_closes_the_handover(state, sessionmaker, by, closed) -> None:
    now = datetime.now(tz=UTC)
    async with sessionmaker() as db:
        conv = Conversation(conv_key=f"wa:{by}", channel_id=CHANNEL_ID, chat_type="whatsapp", chat_id="77015551502")
        db.add(conv)
        await db.flush()
        await pause.set_pause(
            state, db, conv.id, conv.conv_key, minutes=60, reason=PauseReason.ESCALATION,
            now=now - timedelta(minutes=1),
        )
        await pause.resume(state, db, conv.id, conv.conv_key, by=by)
        await db.commit()
        assert await pause.escalation_open(db, conv.id, now=datetime.now(tz=UTC), within_minutes=60) is not closed


# --------------------------------------------------------------------------- #
# Экипировка — ответ владельца, без «уточнит администратор»
# --------------------------------------------------------------------------- #
async def test_no_gear_yet_gets_the_owner_text(make_ctx) -> None:
    ctx = make_ctx(client_texts=("Да ,но у нас нет еще экипировки ?",))

    result = await get_kb_fact(ctx, topic="gear", question="сколько стоит экипировка")

    assert result.meta.get("sent_verbatim"), result
    assert ctx.services.outbound[-1].text.startswith("На первую тренировку ничего специального покупать")


async def test_gear_cost_is_answered_without_the_admin(make_ctx) -> None:
    ctx = make_ctx(client_texts=("А сколько стоит экипировка?",))

    result = await get_kb_fact(ctx, topic="gear", question="сколько стоит экипировка")

    assert result.status is not ToolStatus.NEEDS_OPERATOR, result
    assert "сменной обуви" in result.data["answer"]


@pytest.mark.parametrize(
    ("text", "handover"),
    [
        ("Обычно это спрашивают вместе с расписанием — передать оба вопроса?", True),
        ("Передать ему ваш вопрос, чтобы он связался с вами?", True),
        ("Передам вопросы администратору.", True),
        ("Записать ребёнка на первую пробную тренировку?", False),
        ("Передайте привет тренеру.", False),
    ],
)
def test_handover_phrasings(text, handover) -> None:
    assert funnel.is_handover(text) is handover


async def test_no_handover_question_after_the_owner_text(kb, state, sessionmaker, settings) -> None:
    """«Что требуется от ребенка?» — текст владельца и без «передать оба вопроса?»."""
    deps = _deps(kb, state, sessionmaker, settings)

    decisions = await _say(
        deps, "77015551503", 1, "Что требуется от ребенка?",
        FakeTurn.tool(FakeCall("get_kb_fact", {"topic": "gear", "question": "что требуется от ребенка"})),
        FakeTurn.answer("Обычно это спрашивают вместе с расписанием — передать оба вопроса?"),
    )

    text = _client_text(decisions)
    assert "С собой необходимо взять" in text, text
    assert "передать оба вопроса" not in text.lower(), text
    assert decisions[0].action is DecisionAction.REPLY


# --------------------------------------------------------------------------- #
# «На вечернее время» и перенос записи
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("texts", "parts"),
    [
        (("Да,в вечернее время",), {"evening"}),
        (("Да пожалуйста запишите, на вечернее время",), {"evening"}),
        (("Добрый вечер! Сколько стоит?",), set()),
        (("Доброе утро",), set()),
        (("с утра удобнее",), {"morning"}),
        (("утром или вечером",), {"morning", "evening"}),
        (("Кешке келеміз",), {"evening"}),
    ],
)
def test_day_parts_in_client_words(texts, parts) -> None:
    assert client_day_parts(texts) == parts


def test_a_day_part_picks_the_only_class_in_it(kb) -> None:
    plaza, ksk = kb.gym(PLAZA), kb.gym(KSK)

    assert time_by_day_part(plaza, ("Да, в вечернее время",)) == "19:00"
    assert time_by_day_part(plaza, ("С утра удобнее",)) == "09:00"
    assert time_by_day_part(ksk, ("вечером",), discipline="kickboxing") is None, "17:00 и 19:00 — выбор"
    assert time_by_day_part(ksk, ("вечером",), discipline="boxing", day="tue") == "17:00"
    assert time_by_day_part(plaza, ("утром или вечером",)) is None


async def test_evening_booking_needs_no_time_question(make_ctx) -> None:
    """Скриншот 16.10.2026: «Да, в вечернее время» → «Какое время вам удобнее? 1. 09:00 2. 19:00»."""
    ctx = _ctx(make_ctx, "Мы на Северо-Западе, сыну 8 лет", "Да,в вечернее время", "Иванов Тимур")

    result = await create_trial_lead(
        ctx, child_name="Иванов Тимур", child_age=8, gym_id=PLAZA, parent_agreed=True,
    )

    assert result.data["booked"] is True, result.data
    assert ctx.services.leads[-1].trial_slot.astimezone(ALMATY).hour == 19


def _booked_plaza(slot: datetime) -> SavedLead:
    return SavedLead(
        lead_id=uuid4(), status=LeadStatus.TRIAL_BOOKED.value, gym_id=PLAZA,
        trial_slot=slot.astimezone(UTC), child_name="Иванова Айша", child_age=7,
    )


async def test_next_tuesday_moves_the_booking_at_the_same_time(make_ctx) -> None:
    """«Со след вторника можно, у нас отит» — запись переносится на вторник, 19:00."""
    ctx = _ctx(
        make_ctx, "Мы на Северо-Западе", "Иванова Айша",
        "Со след вторника можно ,у нас отит ,она сейчас на больничном)",
        saved_lead=_booked_plaza(datetime(2026, 10, 15, 19, 0, tzinfo=ALMATY)),
    )

    # Возраст модель не передала — он есть в записи.
    result = await create_trial_lead(
        ctx, child_name="Иванова Айша", gym_id=PLAZA, parent_agreed=True,
        session_day="tue", health_notes="отит",
    )

    assert result.data["booked"] is True and result.data["moved"] is True, result.data
    assert ctx.services.leads[-1].trial_slot.astimezone(ALMATY) == datetime(2026, 10, 20, 19, 0, tzinfo=ALMATY)
    confirmation = ctx.services.outbound[-1].text
    assert confirmation.startswith("✅ Перенесли вашу запись на пробное занятие."), confirmation
    assert "Во вторник, 20.10 в 19:00" in confirmation, confirmation
    assert "С собой необходимо взять" not in confirmation, "что взять, клиент уже читал"


async def test_the_same_booking_is_not_confirmed_twice(make_ctx, services) -> None:
    """«Спасибо, в след вторник будем» после переноса — без второго подтверждения."""
    services.artifact_sends[TRIAL_CONFIRMATION_ARTIFACT] = 2
    ctx = _ctx(
        make_ctx, "Иванова Айша, 7 лет, Северо-Запад", "Спасибо, в след вторник будем 🌹",
        saved_lead=_booked_plaza(datetime(2026, 10, 20, 19, 0, tzinfo=ALMATY)),
    )

    result = await create_trial_lead(
        ctx, child_name="Иванова Айша", child_age=7, gym_id=PLAZA, parent_agreed=True, session_day="tue",
    )

    assert result.data["booked"] is True and result.data["confirmation_sent"] is False, result.data
    assert not [m for m in ctx.services.outbound if m.kind is OutboundKind.LEAD_CONFIRMATION]


@pytest.mark.parametrize("text", [
    "Она заболела, можно со следующего вторника?", "Мы немного опоздаем", "Сын болеет, не сможем прийти",
])
def test_a_booked_trial_turns_absence_into_the_bots_job(kb, text) -> None:
    ahead = guards.scan(text, lang=Language.RU, lexicon=kb.lexicon, policies=kb.policies, trial_upcoming=True)
    before = guards.scan(text, lang=Language.RU, lexicon=kb.lexicon, policies=kb.policies)

    assert not ahead.blocked, ahead
    assert before.blocked and before.escalate, "без записи это по-прежнему вопрос администратору"


def test_a_chronic_condition_stays_medical_with_a_trial_ahead(kb) -> None:
    verdict = guards.scan(
        "У сына астма, можно ему заниматься?", lang=Language.RU, lexicon=kb.lexicon,
        policies=kb.policies, trial_upcoming=True,
    )

    assert verdict.blocked and verdict.fixed_reply_key == "escalation.medical"


async def test_illness_moves_the_booking_instead_of_calling_the_admin(kb, state, sessionmaker, settings, monkeypatch) -> None:
    """Диалог 15.10.2026 целиком через пайплайн: перенос, пожелание — и никакой передачи."""
    monkeypatch.setattr(pipeline, "_utcnow", lambda: THURSDAY.astimezone(UTC))
    deps = _deps(kb, state, sessionmaker, settings)
    chat = "77015551504"
    first = await _say(deps, chat, 1, "Здравствуйте, мы на Северо-Западе", FakeTurn.answer("Подскажите возраст ребёнка?"))
    await _book(sessionmaker, first[0].conversation_id, gym_id=PLAZA, slot=datetime(2026, 10, 15, 19, 0, tzinfo=ALMATY))

    decisions = await _say(
        deps, chat, 2, "Она заболела, можно со следующего вторника?",
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Иванова Айша", "child_age": 7, "gym_id": PLAZA, "parent_agreed": True,
            "session_day": "tue",
        })),
        FakeTurn.answer("Выздоравливайте скорее! Перенёс запись на вторник, 20 октября."),
    )

    text = _client_text(decisions)
    assert decisions[0].action is DecisionAction.REPLY, decisions[0].reason
    assert "Перенесли вашу запись" in text and "20.10 в 19:00" in text, text
    assert text.rstrip().endswith("Выздоравливайте скорее!"), text
    assert "20 октября" not in text, "день и час — только из подтверждения кода"
    assert "администратор" not in text.lower(), text


def test_courtesy_keeps_no_day_or_hour() -> None:
    assert pipeline._courtesy("Выздоравливайте скорее! Перенёс на вторник в 19:00.") == "Выздоравливайте скорее!"
    assert pipeline._courtesy("Перенёс запись на вторник.") == ""
    assert pipeline._courtesy("Хорошо, передам ваш вопрос администратору.") == ""


# --------------------------------------------------------------------------- #
# Зал перед глазами: подтверждение вместо списка залов
# --------------------------------------------------------------------------- #
async def test_time_picked_from_the_shown_gym_is_confirmed_not_listed(make_ctx, kb) -> None:
    """Instagram 12–16.10.2026: «среда. Кикбоксингу 17.00 приедим» и фамилия через четыре дня."""
    texts = ("Хотим на кикбоксинг, сыну 9 лет", "среда. Кикбоксингу 17.00 приедим", "Сидоров Тимур")
    ctx = _ctx(make_ctx, *texts, gym_in_view=KSK)

    result = await create_trial_lead(
        ctx, child_name="Сидоров Тимур", child_age=9, gym_id=KSK, parent_agreed=True,
        session_time="17:00", session_day="wed", discipline="kickboxing",
    )

    assert result.data["booked"] is False and result.data["needs"][0] == "need_gym_confirm", result.data
    assert result.data["confirm_gym_id"] == KSK and "17:00" in result.data["confirm_when"]
    assert not ctx.services.leads

    question = pipeline._booking_question(kb, Language.RU, [_invocation(result)])
    assert question == f"Записать в зал «КСК — школа №9» — {result.data['confirm_when'][:1].lower()}{result.data['confirm_when'][1:]}?"

    agreed = with_agreement("Да", question, kb.lexicon.agreement)
    ctx = _ctx(make_ctx, *texts, agreed, gym_in_view=KSK)
    booked = await create_trial_lead(
        ctx, child_name="Сидоров Тимур", child_age=9, gym_id=KSK, parent_agreed=True,
        session_time="17:00", session_day="wed", discipline="kickboxing",
    )
    assert booked.data["booked"] is True, booked.data


async def test_shown_gym_without_a_pick_still_asks_for_the_gym(make_ctx) -> None:
    """У центра одно время (18:00) — но клиент его не выбирал: зал спрашивается списком."""
    center = "center_kasymkhanova_10"
    ctx = _ctx(make_ctx, "Хочу записать сына", "Сидоров Тимур, 9 лет", gym_in_view=center)

    result = await create_trial_lead(
        ctx, child_name="Сидоров Тимур", child_age=9, gym_id=center, parent_agreed=True,
    )

    assert result.data["needs"][0] == "need_gym", result.data


def _invocation(result):
    from app.types import ToolInvocation

    return ToolInvocation(call_id=None, name="create_trial_lead", args={}, result=result, latency_ms=0, loop_index=0)


# --------------------------------------------------------------------------- #
# Занятие идёт — оно ещё не прошло
# --------------------------------------------------------------------------- #
async def test_a_started_class_is_not_over(kb, state, sessionmaker, settings, monkeypatch) -> None:
    """Пробное в 19:30, «Да мы едем» в 19:33 — и «Как прошла тренировка у Тимура?»."""
    friday = datetime(2026, 10, 16, 19, 33, tzinfo=ALMATY)
    monkeypatch.setattr(pipeline, "_utcnow", lambda: friday.astimezone(UTC))
    deps = _deps(kb, state, sessionmaker, settings)
    chat = "77015551505"
    first = await _say(deps, chat, 1, "Мы из Тобыла", FakeTurn.answer("Подскажите возраст ребёнка?"))
    await _book(
        sessionmaker, first[0].conversation_id, gym_id=TOBYL, slot=datetime(2026, 10, 16, 19, 30, tzinfo=ALMATY),
        child="Петров Тимур", age=10,
    )

    await _say(deps, chat, 2, "Да мы едем", FakeTurn.answer("Ждём вас!"))

    note = deps.llm.requests[-1].dynamic_note
    assert "идёт прямо сейчас" in note and "закончится в 21:00" in note, note
    assert "уже прошло" not in note


# --------------------------------------------------------------------------- #
# Напоминания о пробном: «Спасибо» и передача администратору их не снимают
# --------------------------------------------------------------------------- #
async def _booked_conv(sessionmaker, chat: str, slot: datetime):
    async with sessionmaker() as db:
        conv = Conversation(conv_key=f"wa:{chat}", channel_id=CHANNEL_ID, chat_type="whatsapp", chat_id=chat)
        db.add(conv)
        await db.commit()
        conv_id = conv.id
    await _book(sessionmaker, conv_id, gym_id=PLAZA, slot=slot)
    return conv_id


async def _pending_kinds(sessionmaker, conv_id) -> set[str]:
    async with sessionmaker() as db:
        rows = await db.execute(
            sa.select(FollowupTask.kind).where(
                FollowupTask.conversation_id == conv_id, FollowupTask.state == "pending"
            )
        )
        return set(rows.scalars())


async def test_thanks_after_booking_keeps_the_trial_reminders(kb, sessionmaker) -> None:
    from app.workers.tasks_followup import schedule_followups

    slot = (datetime.now(tz=ALMATY) + timedelta(days=2)).replace(hour=19, minute=0, second=0, microsecond=0)
    conv_id = await _booked_conv(sessionmaker, "77015551506", slot)
    async with sessionmaker() as db:
        conv = await db.get(Conversation, conv_id)
        await schedule_followups(
            db, conv,
            decision=PipelineDecision(action=DecisionAction.REPLY, reason="reply", conversation_id=conv_id),
            policy=kb.policies.followup_policy,
            client_text="Спасибо, в след вторник будем",
            closing_words=kb.policies.followup_closing_words,
        )
        await db.commit()

    kinds = await _pending_kinds(sessionmaker, conv_id)
    assert {FollowupKind.TRIAL_REMINDER_MORNING.value, FollowupKind.TRIAL_REMINDER_2H.value} <= kinds, kinds
    assert FollowupKind.FU_SOFT.value not in kinds, "после «спасибо» не дожимаем"


async def test_handover_keeps_the_trial_reminders(sessionmaker) -> None:
    from app.workers.tasks_followup import cancel_followups

    conv_id = await _booked_conv(sessionmaker, "77015551507", datetime.now(tz=ALMATY) + timedelta(days=2))
    async with sessionmaker() as db:
        for kind in (FollowupKind.TRIAL_REMINDER_2H, FollowupKind.FU_SOFT):
            db.add(FollowupTask(
                conversation_id=conv_id, kind=kind.value, run_at=datetime.now(tz=UTC) + timedelta(days=1),
                state="pending", attempt=0,
            ))
        await db.commit()
        await cancel_followups(db, conv_id, reason="escalated", keep_trial=True)
        await db.commit()

    assert await _pending_kinds(sessionmaker, conv_id) == {FollowupKind.TRIAL_REMINDER_2H.value}


async def test_trial_reminder_goes_once_the_bots_own_pause_ended(kb, state, sessionmaker, settings) -> None:
    """Вопрос передан администратору, его пауза кончилась — «сегодня в 19:00» уходит."""
    from app.workers import tasks_followup

    conv_id = await _booked_conv(sessionmaker, "77015551508", datetime.now(tz=ALMATY) + timedelta(hours=2))
    now = datetime.now(tz=UTC)
    async with sessionmaker() as db:
        conv = await db.get(Conversation, conv_id)
        await pause.set_pause(
            state, db, conv_id, conv.conv_key, minutes=60, reason=PauseReason.ESCALATION, now=now - timedelta(hours=2),
        )
        conv.state = ConversationState.ESCALATED.value
        task = FollowupTask(conversation_id=conv_id, kind=FollowupKind.TRIAL_REMINDER_2H.value, run_at=now, state="pending", attempt=0)
        db.add(task)
        await db.commit()

        reason = await tasks_followup._skip_reason(
            db, conv, task, kind=FollowupKind.TRIAL_REMINDER_2H, settings=settings, now=now,
        )
        soft = await tasks_followup._skip_reason(
            db, conv, task, kind=FollowupKind.FU_SOFT, settings=settings, now=now,
        )

    assert reason != "operator", reason
    assert soft == "operator", "мягкие напоминания по-прежнему ждут администратора"


# --------------------------------------------------------------------------- #
# «Костанай 2» — Плаза; город сам по себе зал не называет
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "Живу Костанай 2 район гостиница Остров по оплате и график хотел узнать",
    "Костанай 2", "2 Костанай", "живу на втором Костанае", "гостиница Остров",
])
async def test_second_kostanay_is_plaza(ctx, kb, text) -> None:
    result = await find_gym_by_district(ctx, district_text=text)

    assert result.data["gyms"][0]["id"] == PLAZA, result.data
    assert gyms_named_in(kb, text) == {PLAZA}


async def test_the_city_alone_names_no_gym(ctx, kb) -> None:
    result = await find_gym_by_district(ctx, district_text="Мы в Костанае")
    alone = await find_gym_by_district(ctx, district_text="Костанай")

    assert len(result.data["gyms"]) > 1, "весь город — все залы, а не один"
    assert len(alone.data["gyms"]) > 1, "«Костанай» — не «Костанай Плаза»"
    assert gyms_named_in(kb, "Мы в Костанае") == frozenset()
    assert gyms_named_in(kb, "Костанай Плаза") == {PLAZA}


# --------------------------------------------------------------------------- #
# Выдуманный тренер
# --------------------------------------------------------------------------- #
def test_invented_coach_sentence_is_dropped(kb) -> None:
    text = (
        "Ждём вас сегодня в 19:30 в зале в Тобыле на улице Тәуелсіздік, 51.\n\n"
        "Тренер Марат Сапаров будет ждать — если вдруг возникнут вопросы по дороге, напишите мне здесь."
    )

    cleaned, names = strip_invented_coaches(text, kb)

    assert names == ("Марат Сапаров",)
    assert cleaned == "Ждём вас сегодня в 19:30 в зале в Тобыле на улице Тәуелсіздік, 51."


@pytest.mark.parametrize("text", [
    "Тренера я предупредил.", "Подойдите к тренеру — он вас отметит.", "Тренер Вас встретит.",
])
def test_coach_without_a_name_stays(kb, text) -> None:
    assert strip_invented_coaches(text, kb) == (text, ())


# --------------------------------------------------------------------------- #
# Счёт на оплату — бухгалтеру
# --------------------------------------------------------------------------- #
async def _manager_cards(sessionmaker) -> dict[str, list[str]]:
    async with sessionmaker() as db:
        payloads = (await db.execute(sa.select(OutboxMessage.payload))).scalars().all()
    cards: dict[str, list[str]] = {}
    for payload in payloads:
        if payload and payload.get("kind") == OutboundKind.MANAGER_CARD.value:
            cards.setdefault(str(payload.get("chat_id")), []).append(str(payload.get("text") or ""))
    return cards


async def test_invoice_request_goes_to_the_accountant(kb, state, sessionmaker, settings) -> None:
    """08.10.2026: «Можно сына записать… Выставите счёт удалённо» — счёт выставляет бухгалтер."""
    owner = RuntimeSettings.from_values({"accountant_contact": ACCOUNTANT})
    deps = _deps(
        kb, state, sessionmaker,
        settings.model_copy(update={"public_base_url": "https://bot.example", "wazzup_channel_id_whatsapp": CHANNEL_ID}),
        runtime=lambda: owner,
    )

    decisions = await process_inbound(deps, webhook_payload(
        "inv-1", "Здравствуйте\nМожно сына записать на тренировку\nВыставите счет удалено",
        chat_id="77015551509",
    ))

    text = _client_text(decisions)
    assert [d.reason for d in decisions] == ["invoice_to_accountant"], text
    assert "бухгалтер" in text and ACCOUNTANT in text, text
    card = (await _manager_cards(sessionmaker))["77000000099"][0]
    assert card.startswith("СЧЁТ НА ОПЛАТУ"), card
    assert "+7 701 555 15 09" in card and "Выставите счет удалено" in card, card
    assert f"Переписка: https://bot.example/crm/clients/{decisions[0].conversation_id.hex}" in card, card
    assert "Ученик:" not in card, "пустое поле не печатается"
    assert "Канал: WhatsApp," in card, card
    assert not deps.llm.requests, "ответ собирает код"


async def test_invoice_without_an_accountant_goes_to_the_admin(kb, state, sessionmaker, settings) -> None:
    deps = _deps(kb, state, sessionmaker, settings, runtime=lambda: RuntimeSettings.from_values({}))

    decisions = await process_inbound(deps, webhook_payload("inv-2", "Скиньте счёт на каспи", chat_id="77015551510"))

    assert decisions[0].action is DecisionAction.ESCALATE, decisions[0].reason
    assert _client_text(decisions) == kb.text("escalation.invoice_to_admin", Language.RU)


def test_the_bot_does_not_answer_the_accountant(settings) -> None:
    owner = RuntimeSettings.from_values({"accountant_contact": ACCOUNTANT})

    silent = ignore_list.silent_numbers(settings, owner)

    assert ignore_list.is_ignored(silent, chat_id="77000000099", phone=None)


# --------------------------------------------------------------------------- #
# «Вы с 09:00 до пяти работаете?» — расписание всех залов
# --------------------------------------------------------------------------- #
async def test_working_hours_question_gets_every_schedule(kb, state, sessionmaker, settings) -> None:
    deps = _deps(kb, state, sessionmaker, settings)

    decisions = await _say(deps, "77015551511", 1, "Вы с 09:00 до пяти работаете")

    text = _client_text(decisions)
    assert [d.reason for d in decisions] == ["schedule_overview"], text
    assert "🕒 Бокс: Вт, Чт, Сб · 09:00–10:30, 17:00–18:30 — абонемент 20 000 ₸" in text, text
    assert "Принимаем детей с 5 до 17 лет" in text
    assert len(text) <= 1000
    assert not deps.llm.requests


@pytest.mark.parametrize("text", [
    "Вы работаете с детьми 4 лет?", "Во сколько работаете в Тобыле?", "Во сколько по субботам?",
])
async def test_specific_time_questions_stay_with_the_model(kb, state, sessionmaker, settings, text) -> None:
    deps = _deps(kb, state, sessionmaker, settings)

    decisions = await _say(deps, "77015551512", 1, text, FakeTurn.answer("Подскажу."))

    assert decisions[0].reason != "schedule_overview"


async def test_the_overview_is_not_sent_endlessly(kb, state, sessionmaker, settings) -> None:
    deps = _deps(kb, state, sessionmaker, settings)
    chat = "77015551513"
    for number in (1, 2):
        assert [d.reason for d in await _say(deps, chat, number, "Какое расписание?")] == ["schedule_overview"]

    third = await _say(deps, chat, 3, "Какое расписание?", FakeTurn.answer("Расписание выше."))

    assert third[0].reason != "schedule_overview"


async def test_schedules_instead_of_a_handover(kb, state, sessionmaker, settings) -> None:
    """Вопрос о цене и времени сразу, а модель зовёт администратора — уходит расписание."""
    deps = _deps(kb, state, sessionmaker, settings)

    decisions = await _say(
        deps, "77015551514", 1, "Сколько стоит и во сколько работаете?",
        FakeTurn.answer("Здесь лучше ответит администратор, чтобы не сказать вам неточность."),
    )

    text = _client_text(decisions)
    assert "Наши залы в Костанае" in text and "09:00–10:30" in text, text
    assert "администратор" not in text.lower(), text


# --------------------------------------------------------------------------- #
# Ссылка на переписку в CRM и прочее
# --------------------------------------------------------------------------- #
async def test_escalation_card_links_to_the_dialog(kb, state, sessionmaker, settings) -> None:
    """«Администратор об этом знает вообще?» — карточка с прямой ссылкой на диалог."""
    deps = _deps(kb, state, sessionmaker, settings.model_copy(update={
        "public_base_url": "https://bot.example", "manager_notify_target": "77000000077",
    }))

    decisions = await process_inbound(deps, webhook_payload("esc-1", "Позовите менеджера", chat_id="77015551515"))

    link = f"https://bot.example/crm/clients/{decisions[0].conversation_id.hex}"
    assert any(link in card.text for card in decisions[0].manager_cards), decisions[0].manager_cards


async def test_escalation_tool_card_links_to_the_dialog(make_ctx, settings, monkeypatch) -> None:
    import app.tools.booking as booking

    monkeypatch.setattr(booking, "get_settings", lambda: settings.model_copy(update={"public_base_url": "https://bot.example"}))
    ctx = make_ctx(client_texts=("Позовите администратора",))

    await escalate_to_manager(ctx, reason="user_request", question_summary="просит администратора")

    assert f"Переписка: https://bot.example/crm/clients/{ctx.conversation_id.hex}" in ctx.services.cards[-1].text


def test_pointers_to_the_card_above_are_dropped() -> None:
    reply = "Арман, ближе всего к вам зал на КСК.\n\n💳 Цены и тарифы указаны в карточке выше.\n\nКакой возраст у ребёнка?"

    assert pipeline._without_card_pointers(reply) == "Арман, ближе всего к вам зал на КСК.\n\nКакой возраст у ребёнка?"


def test_session_length_comes_from_the_schedule(kb, settings) -> None:
    """Конец занятия — по расписанию зала: в центре 18:00–19:00, а не «полтора часа»."""
    from types import SimpleNamespace

    lead = SimpleNamespace(
        status=LeadStatus.TRIAL_BOOKED.value, gym_id="center_kasymkhanova_10",
        trial_slot=datetime(2026, 10, 15, 18, 0, tzinfo=ALMATY).astimezone(UTC),
    )

    start, end = pipeline._session_window(lead, kb, settings)

    assert end - start == timedelta(hours=1)


async def test_invented_coach_never_reaches_the_client(kb, state, sessionmaker, settings) -> None:
    deps = _deps(kb, state, sessionmaker, settings)

    decisions = await _say(
        deps, "77015551516", 1, "Как проходит первая тренировка?",
        FakeTurn.answer("Первая тренировка спокойная, всё покажут. Тренер Марат Сапаров будет ждать."),
    )

    text = _client_text(decisions)
    assert "Первая тренировка спокойная" in text and "Сапаров" not in text, text


async def test_pointer_to_the_card_above_is_not_sent(kb, state, sessionmaker, settings) -> None:
    deps = _deps(kb, state, sessionmaker, settings)

    decisions = await _say(
        deps, "77015551517", 1, "Сколько стоит абонемент в Костанае?",
        FakeTurn.tool(FakeCall("send_content", {"artifact_id": "price_card_city"})),
        FakeTurn.answer("💳 Цены и тарифы указаны в карточке выше.\n\nСколько лет ребёнку?"),
    )

    text = _client_text(decisions)
    assert "Стандартный — 25 000 ₸" in text and "карточке выше" not in text, text


async def test_yo_in_the_question_still_finds_the_answer(make_ctx) -> None:
    ctx = make_ctx(client_texts=("Что требуется от ребёнка?",))

    result = await get_kb_fact(ctx, topic="gear", question="")

    assert result.meta.get("sent_verbatim"), result
    from app.tools import facts

    assert facts._folded("Ребёнка ВСЁ") == "ребенка все", "«ребёнка» и «ребенка» — одно слово"


@pytest.mark.parametrize(("reply", "kept"), [
    ("Ближайший зал — Плаза.\n\n💳 Абонементы и цены на занятия можно посмотреть в нашей карточке.\n\nПрислать расписание?",
     "Ближайший зал — Плаза.\n\nПрислать расписание?"),
    ("Оплатить можно Kaspi-карточкой или наличными.", "Оплатить можно Kaspi-карточкой или наличными."),
])
def test_references_to_a_card_are_dropped_but_payment_by_card_stays(reply, kept) -> None:
    assert pipeline._without_card_pointers(reply) == kept


def test_the_model_is_told_the_gym_of_the_named_place(kb) -> None:
    """Живой прогон 19.10.2026: модель сама выбрала центр для «второго Костаная»."""
    said = pipeline._just_said(
        "Живу Костанай 2 район гостиница Остров по оплате и график хотел узнать",
        kb=kb, now=THURSDAY.astimezone(UTC),
    )

    assert any("Костанай Плаза — AIMED" in item and PLAZA in item for item in said), said
    for text in ("Мы в Костанае", "Нет КСК далеко час езды на автобусе На КСК только бокс ?", "Мы из Тобыла"):
        assert not any("ближайший" in item for item in pipeline._just_said(text, kb=kb, now=THURSDAY)), text


@pytest.mark.parametrize(("text", "gym"), [
    ("Живём в районе аэропорт", PLAZA),
    ("Живём в районе аэропорт, но ездим в КСК", None),   # назван сам зал — это не подсказка
    ("Мы возле школы 22", None),                          # район закреплён за двумя залами
])
def test_district_gym_only_for_a_district_of_one_gym(kb, text, gym) -> None:
    from app.tools.gyms import district_gym

    assert district_gym(kb, text) == gym



@pytest.mark.parametrize(("said", "escalated"), [
    ("Он ходил в том году", False),
    ("Вчера был у вас", False),
    ("Нужна ли справка для школы?", True),
    ("Справка нужна?", True),
    ("Летом тоже?", True),   # вопрос только по знаку
    ("Есть ли летние сборы", True),
    ("Жазғы лагерь бар ма", True),
    # Просьба — не рассказ: без данных её ведёт человек (перепроверка 19.10.2026).
    ("Отмените запись", True),
    ("Хочу перевести сына в другую группу", True),
    ("Нужна справка для школы", True),
])
async def test_no_data_handover_needs_a_question(make_ctx, said, escalated) -> None:
    """«Он ходил в том году» — рассказ, а не вопрос: администратор тут ни к чему."""
    ctx = make_ctx(client_texts=(said,))

    result = await escalate_to_manager(ctx, reason="no_data", question_summary="нет данных")

    assert (result.data.get("escalated") is True) is escalated, result



async def test_the_overview_does_not_need_the_media_record(tmp_path, state, sessionmaker, settings) -> None:
    """media.yaml, правленый владельцем в CRM, новую запись не получает — карточка всё равно уходит."""
    import shutil
    from pathlib import Path

    from app.kb.loader import load_sync

    root = Path(__file__).resolve().parent.parent
    target = tmp_path / "kb"
    shutil.copytree(root / "kb", target, ignore=shutil.ignore_patterns(".*"))
    media = target / "media.yaml"
    text = media.read_text(encoding="utf-8")
    start = text.index("- id: schedules_city")
    media.write_text(text[:start] + text[text.index("- id: gyms_list_all", start):], encoding="utf-8")
    snapshot, _ = load_sync(target, media_dir=root / "media", schema_version=1)
    assert snapshot.artifact("schedules_city") is None
    deps = _deps(snapshot, state, sessionmaker, settings)

    decisions = await _say(deps, "77015551518", 1, "Вы с 09:00 до пяти работаете")

    assert [d.reason for d in decisions] == ["schedule_overview"], _client_text(decisions)
    assert "2. КСК — школа №9" in _client_text(decisions)


# --------------------------------------------------------------------------- #
# Перепроверка 19.10.2026: что нашлось в собственных правках
# --------------------------------------------------------------------------- #
async def test_a_new_day_part_wins_over_the_booked_hour(make_ctx) -> None:
    """Записались «вечером», потом «А можно в субботу утром?» — суббота 09:00, а не 19:00."""
    ctx = _ctx(
        make_ctx, "Мы на Северо-Западе", "Иванова Айша, 7 лет, на вечернее время", "А можно в субботу утром?",
        saved_lead=_booked_plaza(datetime(2026, 10, 15, 19, 0, tzinfo=ALMATY)),
    )

    result = await create_trial_lead(
        ctx, child_name="Иванова Айша", child_age=7, gym_id=PLAZA, parent_agreed=True, session_day="sat",
    )

    assert result.data["booked"] is True, result.data
    assert ctx.services.leads[-1].trial_slot.astimezone(ALMATY) == datetime(2026, 10, 17, 9, 0, tzinfo=ALMATY)


async def test_a_day_part_the_day_lacks_is_asked_not_swapped(make_ctx) -> None:
    """«В субботу утром», а в центре суббота только в 18:00 — вопрос, а не молчаливая запись на вечер."""
    ctx = _ctx(make_ctx, "Мы в центре живём", "Сидоров Тимур, 9 лет", "В субботу утром можно?")

    result = await create_trial_lead(
        ctx, child_name="Сидоров Тимур", child_age=9, gym_id=CENTER, parent_agreed=True, session_day="sat",
    )

    assert result.data["booked"] is False, result.data
    assert result.data["needs"][0] == "unknown_time", result.data
    assert not ctx.services.leads or ctx.services.leads[-1].trial_slot is None

    # Без части дня суббота в центре одна — запись сразу, как раньше.
    ctx = _ctx(make_ctx, "Мы в центре живём", "Сидоров Тимур, 9 лет", "В субботу можно?")
    result = await create_trial_lead(
        ctx, child_name="Сидоров Тимур", child_age=9, gym_id=CENTER, parent_agreed=True, session_day="sat",
    )
    assert result.data["booked"] is True, result.data

    # Время названо словами клиента — его выбор, и «утром не можем» рядом его не отменяет.
    ctx = _ctx(make_ctx, "Мы в центре живём", "Сидоров Тимур, 9 лет", "В субботу в 18:00, утром не можем")
    result = await create_trial_lead(
        ctx, child_name="Сидоров Тимур", child_age=9, gym_id=CENTER, parent_agreed=True,
        session_day="sat", session_time="18:00",
    )
    assert result.data["booked"] is True, result.data


def test_the_latest_day_part_is_the_choice() -> None:
    from app.kb.sessions import latest_day_part

    assert latest_day_part(("вечером удобнее", "А можно в субботу утром?")) == "morning"
    assert latest_day_part(("вечером удобнее", "Иванова Айша")) == "evening"
    assert latest_day_part(("утром или вечером?",)) is None
    assert latest_day_part(("Добрый вечер",)) is None


async def test_a_day_at_the_shown_gym_asks_the_time_not_the_gym(make_ctx) -> None:
    """«В среду приедем» по расписанию КСК, а в среду кикбоксинг трижды — вопрос о времени."""
    ctx = _ctx(make_ctx, "Хотим на кикбоксинг, сыну 9 лет", "в среду приедем", "Сидоров Тимур", gym_in_view=KSK)

    result = await create_trial_lead(
        ctx, child_name="Сидоров Тимур", child_age=9, gym_id=KSK, parent_agreed=True,
        session_day="wed", discipline="kickboxing",
    )

    assert result.data["needs"][0] == "need_time", result.data
    assert "need_gym" not in result.data["needs"]
    assert {option["time_start"] for option in result.data["options"]} == {"09:00", "17:00", "19:00"}


async def test_a_pick_the_shown_gym_does_not_have_asks_the_gym(make_ctx) -> None:
    """По Плазе «в понедельник» — у Плазы понедельника нет: клиент смотрит не туда, список залов."""
    ctx = _ctx(make_ctx, "Сыну 9 лет", "в понедельник приедем", "Сидоров Тимур", gym_in_view=PLAZA)

    result = await create_trial_lead(
        ctx, child_name="Сидоров Тимур", child_age=9, gym_id=PLAZA, parent_agreed=True, session_day="mon",
    )

    assert result.data["needs"][0] == "need_gym", result.data


async def test_a_second_child_is_a_new_booking_not_a_move(make_ctx) -> None:
    ctx = _ctx(
        make_ctx, "Мы на Северо-Западе", "И сына запишите: Иванов Тимур, 8 лет, на субботу вечером",
        saved_lead=_booked_plaza(datetime(2026, 10, 15, 19, 0, tzinfo=ALMATY)),
    )

    result = await create_trial_lead(
        ctx, child_name="Иванов Тимур", child_age=8, gym_id=PLAZA, parent_agreed=True, session_day="sat",
    )

    assert result.data["booked"] is True and result.data["moved"] is False, result.data
    assert ctx.services.outbound[-1].text.startswith("✅ Мы записали вас"), ctx.services.outbound[-1].text


def test_the_same_child_is_found_by_a_fuller_or_shorter_name() -> None:
    """Записали «Айша», при переносе модель передала «Иванова Айша» — это она же, а не брат."""
    from app.tools.booking import _same_child

    booked = _booked_plaza(datetime(2026, 10, 15, 19, 0, tzinfo=ALMATY))
    assert _same_child(replace(booked, child_name="Айша"), LeadDraft(child_name="Иванова Айша"))
    assert _same_child(booked, LeadDraft(child_name="Айша"))
    assert not _same_child(booked, LeadDraft(child_name="Иванов Тимур"))
    assert not _same_child(booked, LeadDraft())


async def test_stop_words_beat_the_schedule_card(kb, state, sessionmaker, settings) -> None:
    """«Стоп, не пишите больше. Во сколько работаете?» — прощание и тишина, не расписание."""
    deps = _deps(kb, state, sessionmaker, settings)

    decisions = await _say(deps, "77015551519", 1, "Стоп, не пишите мне больше. Во сколько вы работаете?")

    assert decisions[0].reason != "schedule_overview", decisions[0].reason
    async with sessionmaker() as db:
        conv = await repo_conversation.get_by_id(db, decisions[0].conversation_id)
        assert conv.followup_blocked, "стоп-слово выключает напоминания"


async def test_a_manager_request_beats_the_invoice(kb, state, sessionmaker, settings) -> None:
    owner = RuntimeSettings.from_values({"accountant_contact": ACCOUNTANT})
    deps = _deps(kb, state, sessionmaker, settings, runtime=lambda: owner)

    decisions = await _say(deps, "77015551520", 1, "Позовите менеджера, выставите счёт")

    assert decisions[0].reason != "invoice_to_accountant"
    assert decisions[0].action is DecisionAction.ESCALATE


@pytest.mark.parametrize(("contact", "chat_type"), [
    (ACCOUNTANT, "instagram"),                     # номера клиента нет — счёт не на что выставить
    ("77000000077-1600000000@g.us", "whatsapp"),   # только групповой чат — номер клиенту не дать
])
async def test_invoice_falls_back_to_the_admin(kb, state, sessionmaker, settings, contact, chat_type) -> None:
    owner = RuntimeSettings.from_values({"accountant_contact": contact})
    deps = _deps(
        kb, state, sessionmaker, settings.model_copy(update={"wazzup_channel_id_whatsapp": CHANNEL_ID}),
        runtime=lambda: owner,
    )

    decisions = await process_inbound(deps, webhook_payload(
        "inv-3", "Выставите счёт за абонемент", chat_id="77015551521", chat_type=chat_type,
    ))

    assert decisions[0].action is DecisionAction.ESCALATE, decisions[0].reason
    text = _client_text(decisions)
    assert "@g.us" not in text and "бухгалтер" not in text, text


async def test_invoice_without_a_delivery_channel_goes_to_the_admin(kb, state, sessionmaker, settings) -> None:
    """Карточку бухгалтеру некуда отправить — клиенту нельзя говорить «передал»."""
    owner = RuntimeSettings.from_values({"accountant_contact": ACCOUNTANT})
    deps = _deps(kb, state, sessionmaker, settings, runtime=lambda: owner)

    decisions = await process_inbound(deps, webhook_payload("inv-4", "Скиньте счёт", chat_id="77015551522"))

    assert decisions[0].action is DecisionAction.ESCALATE, decisions[0].reason


async def test_schedules_are_not_sent_for_a_named_town(kb, state, sessionmaker, settings) -> None:
    """«Цены и во сколько в Тобыле?» и «уточнит администратор» — городское расписание не ответ."""
    deps = _deps(kb, state, sessionmaker, settings)

    decisions = await _say(
        deps, "77015551523", 1, "Сколько стоит и во сколько работаете в Тобыле?",
        FakeTurn.answer("Здесь лучше ответит администратор, чтобы не сказать вам неточность."),
    )

    assert "Наши залы в Костанае" not in _client_text(decisions)


def test_the_district_hint_reads_only_the_clients_words(kb) -> None:
    text = with_agreement("Да", "Вы живёте в районе Береке? Записать ребёнка?", kb.lexicon.agreement)

    assert not any("ближайший" in item for item in pipeline._just_said(text, kb=kb, now=THURSDAY))


def test_unreadable_owner_settings_keep_the_bot_silent(kb, state, sessionmaker, settings) -> None:
    def broken():
        raise OSError("admin.db повреждена")

    assert pipeline._bot_enabled(_deps(kb, state, sessionmaker, settings, runtime=broken)) is False
    assert pipeline._bot_enabled(_deps(kb, state, sessionmaker, settings)) is True, "без настроек — как раньше"


async def test_unreadable_owner_settings_hold_the_reminders(kb, state, sessionmaker, settings) -> None:
    from app.workers import tasks_followup

    def broken():
        raise OSError("admin.db повреждена")

    conv_id = await _booked_conv(sessionmaker, "77015551524", datetime.now(tz=ALMATY) + timedelta(hours=2))
    async with sessionmaker() as db:
        task = FollowupTask(
            conversation_id=conv_id, kind=FollowupKind.TRIAL_REMINDER_2H.value,
            run_at=datetime.now(tz=UTC), state="pending", attempt=0,
        )
        db.add(task)
        await db.commit()
        task_id = task.id

    deps = _deps(kb, state, sessionmaker, settings, runtime=broken)
    await tasks_followup.send_followup_job({"deps": deps, "correlation_id": "t"}, str(task_id))

    async with sessionmaker() as db:
        assert (await db.get(FollowupTask, task_id)).state == "pending", "ждёт, пока настройки прочитаются"


def test_coach_rule_keeps_times_and_places(kb) -> None:
    assert strip_invented_coaches("Ждём вас в 19.30 в зале. Тренер Марат Сапаров будет ждать.", kb) == (
        "Ждём вас в 19.30 в зале.", ("Марат Сапаров",),
    )
    # «19.30» — не конец фразы: снимается фраза целиком, без огрызка «Ждём вас в 19.».
    assert strip_invented_coaches("Ждём вас в 19.30, тренер Марат будет ждать. До встречи!", kb) == (
        "До встречи!", ("Марат",),
    )
    assert strip_invented_coaches("Тренер Тобыла ждёт вас.", kb) == ("Тренер Тобыла ждёт вас.", ())


def test_a_wrong_upper_age_is_caught(kb) -> None:
    from app.core.postcheck import _wrong_age_limit

    assert _wrong_age_limit("Принимаем детей с 5 до 14 лет.", kb)
    assert not _wrong_age_limit("Принимаем детей с 5 до 17 лет.", kb)
    # «до» перед нижней границей — час или число человек, а не возраст.
    assert not _wrong_age_limit("Занимаемся до 20:00, берём с 5 лет.", kb)
    assert not _wrong_age_limit("Занимаемся в группах до 12 человек с 5 лет.", kb)


def test_a_day_without_classes_is_unknown_not_a_time_question(kb) -> None:
    from app.kb.sessions import resolve_trial_session

    choice = resolve_trial_session(
        kb.gym(PLAZA), time_text=None, day="mon", discipline=None, now=THURSDAY.astimezone(UTC),
        tz_name="Asia/Almaty", min_lead_hours=2,
    )

    assert choice.session is None and choice.problem == "unknown_day"



async def test_another_day_part_is_not_the_booked_hour(make_ctx) -> None:
    """Записан на утро, «А можно в среду вечером?», а вечером в КСК 17:00 и 19:00 — вопрос."""
    saved = SavedLead(
        lead_id=uuid4(), status=LeadStatus.TRIAL_BOOKED.value, gym_id=KSK,
        trial_slot=datetime(2026, 10, 19, 9, 0, tzinfo=ALMATY).astimezone(UTC),
        child_name="Сидоров Тимур", child_age=9,
    )
    ctx = _ctx(make_ctx, "Кикбоксинг, КСК, Сидоров Тимур, 9 лет", "А можно в среду вечером?", saved_lead=saved)

    result = await create_trial_lead(
        ctx, child_name="Сидоров Тимур", child_age=9, gym_id=KSK, parent_agreed=True,
        session_day="wed", discipline="kickboxing",
    )

    assert result.data["booked"] is False and result.data["needs"][0] == "need_time", result.data
    assert not ctx.services.leads, "утренний час в вечер не переносится"


async def test_off_topic_messages_stay_with_the_model(kb, state, sessionmaker, settings) -> None:
    deps = _deps(kb, state, sessionmaker, settings)

    decisions = await _say(
        deps, "77015551525", 1, "Расскажи анекдот, во сколько вы работаете?", FakeTurn.answer("Подскажу расписание."),
    )

    assert decisions[0].reason != "schedule_overview"



async def test_an_invented_name_costs_one_sentence_not_the_reply(kb, state, sessionmaker, settings) -> None:
    """Живой прогон 19.10.2026: «Записать <выдуманное имя>…?» снимало весь ответ — и клиент
    получал расписания всех залов подряд и «администратор подключается»."""
    deps = _deps(kb, state, sessionmaker, settings)

    decisions = await _say(
        deps, "77015551526", 1, "Здравствуйте, какое расписание в 6 микрорайоне? Сыну 8 лет",
        FakeTurn.tool(FakeCall("get_schedule", {"gym_id": "mkr6_arystanbekova_6", "child_age": 8})),
        FakeTurn.answer("Занятия по вторникам, четвергам и субботам. Записать Айсултана на пробное?"),
    )

    text = _client_text(decisions)
    assert decisions[0].action is DecisionAction.REPLY, decisions[0].reason
    assert "Айсултан" not in text and "Наши залы" not in text, text
    assert "Расписание — 6-й микрорайон" in text and "Костанай Плаза" not in text, text


def test_a_request_is_not_a_lead_in() -> None:
    assert pipeline._lead_in("Хорошо. Подскажите, пожалуйста, фамилию и имя ребёнка.") == "Хорошо."



def test_a_later_time_cancels_an_older_day_part() -> None:
    """«Где у вас утренний кикбоксинг?», потом «среда, 17.00» — утро больше не выбор."""
    from app.kb.sessions import latest_day_part

    assert latest_day_part(("Где у вас утренний кикбоксинг?", "среда. Кикбоксингу 17.00 приедим", "Сидоров Тимур")) is None


async def test_the_clients_own_day_is_not_an_invention(kb, state, sessionmaker, settings) -> None:
    """«В среду на Плазе занятий нет» — день назвал клиент, ответ не снимается."""
    deps = _deps(kb, state, sessionmaker, settings)
    chat = "77015551527"
    await _say(deps, chat, 1, "Мы на Северо-Западе, сыну 9 лет", FakeTurn.answer("Записать на пробное?"))

    decisions = await _say(
        deps, chat, 2, "Да, в среду можно?",
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Сидоров Тимур", "child_age": 9, "gym_id": PLAZA, "parent_agreed": True, "session_day": "wed",
        })),
        FakeTurn.answer("В среду на Плазе занятий нет."),
    )

    assert decisions[0].postcheck_fail is None, decisions[0].reason
    assert "В среду на Плазе занятий нет." in _client_text(decisions)



def test_a_colon_inside_a_lead_in_keeps_the_sentence() -> None:
    """«Да, на КЖБИ есть наш зал: Гашека 9/3.» — без начала ответ читался как «Гашека 9/3»."""
    assert pipeline._lead_in(
        "Да, на КЖБИ есть наш зал: Гашека 9/3. Занятия начнутся 01.11.2026. В каком из залов вам удобнее?"
    ) == "Да, на КЖБИ есть наш зал: Гашека 9/3. Занятия начнутся 01.11.2026."
    assert pipeline._lead_in("Варианты на эту неделю:\n1. 17:00\n2. 19:00") == ""
    assert pipeline._lead_in("В 17:30 занятий нет. Какое время выбрать?") == "В 17:30 занятий нет."
