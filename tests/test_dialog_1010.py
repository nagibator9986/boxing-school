"""Диалог 10.10.2026 из CRM: семь ошибок одной записи на пробное.

Клиент спросил, чем бокс отличается от кикбоксинга, и написал «На пробное придем в
понедельник 17:00». Дальше: «Да все верно» на вопрос о районе ушло администратору;
под карточкой КСК модель пересказала адрес; на «фамилию и имя ребёнка?» родитель
ответил «Учимся в 9 школе» — и ребёнок записался как «Учимся 9 школа», 9 лет; на «Ок»
после подтверждения бот спросил район; утром пришло «сегодня в 22:00» вместо 17:00,
а на «А почему 22:00?» без модели (кончились кредиты) ушёл прайс.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from app.core import lexicon
from app.core.pipeline import PipelineDeps, process_inbound
from app.core.reply_dedup import strip_card_repeats
from app.kb import loader as kb_loader
from app.kb.agreement import is_bare_agreement
from app.kb.sessions import age_answer, client_named_age
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
from app.storage import repo_lead
from app.storage.models import Conversation, FollowupTask, Lead, OutboxMessage
from app.tools.booking import _booking_key, create_trial_lead
from app.types import (
    ChannelKind,
    DecisionAction,
    FollowupKind,
    IntentHint,
    Language,
    LeadDraft,
    LeadStatus,
)

from tests.conftest import RecordingQueue, webhook_payload

UTC = timezone.utc
ALMATY = ZoneInfo("Asia/Almaty")
KSK = "ksk_kairbekova_334"


# --------------------------------------------------------------------------- #
# Время пробного: «сегодня в 22:00» вместо 17:00
# --------------------------------------------------------------------------- #
async def _booked_at(sessionmaker, chat: str, slot: datetime):
    """Запись ровно так, как её сохраняет инструмент: время с поясом школы."""
    async with sessionmaker() as db:
        conv = Conversation(conv_key=f"wa:{chat}", channel_id="wa-1", chat_type="whatsapp", chat_id=chat)
        db.add(conv)
        await db.flush()
        await repo_lead.upsert(
            db,
            LeadDraft(
                conversation_id=conv.id, lang=Language.RU, status=LeadStatus.TRIAL_BOOKED,
                child_name="Иванова Аружан", child_age=8, gym_id=KSK,
                channel=ChannelKind.WHATSAPP, channel_user=chat, trial_slot=slot,
            ),
        )
        await db.commit()
        return conv.id


def _monday_17() -> datetime:
    """Ближайший понедельник, 17:00 по Алматы, не раньше чем через сутки."""
    day = datetime.now(tz=ALMATY) + timedelta(days=1)
    day += timedelta(days=(7 - day.weekday()) % 7)
    return day.replace(hour=17, minute=0, second=0, microsecond=0)


async def test_slot_is_stored_as_the_same_moment(kb, sessionmaker) -> None:
    """17:00 по Алматы читается обратно как 12:00 UTC, а не как «17:00 UTC»."""
    slot = _monday_17()
    conv_id = await _booked_at(sessionmaker, "77015551011", slot)

    async with sessionmaker() as db:
        stored = (await db.execute(sa.select(Lead.trial_slot).where(Lead.conversation_id == conv_id))).scalar_one()

    aware = stored if stored.tzinfo is not None else stored.replace(tzinfo=UTC)
    assert aware == slot, (stored, slot)


async def test_morning_reminder_names_the_booked_hour(kb, sessionmaker) -> None:
    from app.workers import tasks_followup

    conv_id = await _booked_at(sessionmaker, "77015551012", _monday_17())

    async with sessionmaker() as db:
        conv = await db.get(Conversation, conv_id)
        message = await tasks_followup._build_message(
            SimpleNamespace(kb=lambda: kb), db, conv, kind=FollowupKind.TRIAL_REMINDER_MORNING
        )

    assert message is not None
    assert "сегодня в 17:00 пробная тренировка у Иванова Аружан" in message.text, message.text


async def test_two_hour_reminder_comes_before_the_class(kb, sessionmaker) -> None:
    from app.workers.tasks_followup import schedule_followups
    from app.types import PipelineDecision

    slot = _monday_17()
    conv_id = await _booked_at(sessionmaker, "77015551013", slot)

    async with sessionmaker() as db:
        conv = await db.get(Conversation, conv_id)
        await schedule_followups(
            db, conv,
            decision=PipelineDecision(action=DecisionAction.REPLY, reason="reply", conversation_id=conv_id),
            policy=kb.policies.followup_policy,
        )
        await db.commit()
        rows = dict((await db.execute(
            sa.select(FollowupTask.kind, FollowupTask.run_at).where(FollowupTask.state == "pending")
        )).all())

    def local(value: datetime) -> datetime:
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(ALMATY)

    assert local(rows[FollowupKind.TRIAL_REMINDER_2H.value]) == slot - timedelta(hours=2)
    assert (local(rows[FollowupKind.TRIAL_REMINDER_MORNING.value]).hour, slot.hour) == (9, 17)


async def test_reminder_after_the_class_started_is_not_sent(kb, state, sessionmaker, settings, monkeypatch) -> None:
    """Задача со старым, сдвинутым сроком: «за два часа» в 20:00 о занятии в 17:00 не уходит."""
    from app.workers import tasks_followup

    monkeypatch.setattr(tasks_followup, "is_quiet_hours", lambda *a, **k: False)
    slot = datetime.now(tz=ALMATY).replace(second=0, microsecond=0) - timedelta(hours=3)
    conv_id = await _booked_at(sessionmaker, "77015551014", slot)
    async with sessionmaker() as db:
        task = FollowupTask(
            conversation_id=conv_id, kind=FollowupKind.TRIAL_REMINDER_2H.value,
            run_at=datetime.now(tz=UTC), state="pending", attempt=0,
        )
        db.add(task)
        await db.commit()
        task_id = task.id
    kb_loader.swap(kb)
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=FakeLLMClient([]), kb=kb_loader.get_snapshot,
        queue=RecordingQueue(), settings=settings,
    )

    await tasks_followup.send_followup_job({"deps": deps, "correlation_id": "t"}, str(task_id))

    async with sessionmaker() as db:
        assert (await db.get(FollowupTask, task_id)).state == "cancelled"
        assert not (await db.execute(sa.select(OutboxMessage.id))).all()


async def test_reminder_before_the_class_is_still_sent(kb, state, sessionmaker, settings, monkeypatch) -> None:
    """Контроль к тесту выше: занятие впереди — напоминание уходит."""
    from app.workers import tasks_followup

    monkeypatch.setattr(tasks_followup, "is_quiet_hours", lambda *a, **k: False)
    conv_id = await _booked_at(sessionmaker, "77015551015", datetime.now(tz=ALMATY) + timedelta(hours=2))
    async with sessionmaker() as db:
        conv = await db.get(Conversation, conv_id)
        conv.last_inbound_at = datetime.now(tz=UTC)
        task = FollowupTask(
            conversation_id=conv_id, kind=FollowupKind.TRIAL_REMINDER_2H.value,
            run_at=datetime.now(tz=UTC), state="pending", attempt=0,
        )
        db.add(task)
        await db.commit()
        task_id = task.id
    kb_loader.swap(kb)
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=FakeLLMClient([]), kb=kb_loader.get_snapshot,
        queue=RecordingQueue(), settings=settings,
    )

    await tasks_followup.send_followup_job({"deps": deps, "correlation_id": "t"}, str(task_id))

    async with sessionmaker() as db:
        assert (await db.get(FollowupTask, task_id)).state == "sent"


def test_saved_and_new_slot_of_the_same_booking_match() -> None:
    """Сохранённый слот (UTC без пояса) и тот же слот черновика — одна запись, не перенос."""
    slot = datetime(2026, 10, 12, 17, 0, tzinfo=ALMATY)
    saved = slot.astimezone(UTC).replace(tzinfo=None)

    assert _booking_key(KSK, saved, "Иванова Аружан", 8) == _booking_key(KSK, slot, "Иванова Аружан", 8)
    assert _booking_key(KSK, saved, "Иванова Аружан", 8) != _booking_key(
        KSK, slot + timedelta(hours=2), "Иванова Аружан", 8
    )


async def test_old_rows_are_moved_to_utc_once(tmp_path) -> None:
    """Строки, записанные до исправления местным временем, сдвигаются один раз —
    вместе с уже назначенными по ним напоминаниями."""
    from app.storage import db as storage_db
    from app.storage.models import Base
    from app.storage.upgrade import DATA_VERSION, upgrade_sqlite_data

    engine = storage_db.build_engine(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    maker = storage_db.build_sessionmaker(engine)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        evening = await _booked_at(maker, "77015551016", _monday_17())
        morning = await _booked_at(maker, "77015551017", _monday_17())
        # Как лежало до 12.10.2026: местное время без пояса, сроки посчитаны от него как от UTC.
        old = {
            evening: ("2026-10-12 17:00:00.000000", {
                "trial_reminder_2h": "2026-10-12 15:00:00.000000",  # «за 2 часа» в 20:00 по Алматы
                "trial_reminder_morning": "2026-10-12 04:30:00.000000",  # 09:30 по Алматы
                "fu_soft": "2026-10-11 10:00:00.000000",
            }),
            morning: ("2026-10-13 09:00:00.000000", {
                "trial_reminder_morning": "2026-10-13 04:30:00.000000",  # 09:30 — после начала
            }),
        }
        async with engine.begin() as conn:
            for conv_id, (slot, tasks) in old.items():
                await conn.execute(
                    sa.update(Lead.__table__).where(Lead.__table__.c.conversation_id == conv_id)
                    .values(trial_slot=datetime.fromisoformat(slot))
                )
                for kind, run_at in tasks.items():
                    await conn.execute(sa.insert(FollowupTask.__table__).values(
                        id=uuid4(), conversation_id=conv_id, kind=kind, state="pending", attempt=0,
                        run_at=datetime.fromisoformat(run_at), created_at=datetime(2026, 10, 10),
                    ))

        for _ in range(2):  # второй запуск ничего не сдвигает
            async with engine.begin() as conn:
                await upgrade_sqlite_data(conn, timezone_name="Asia/Almaty")

        async with engine.connect() as conn:
            slots = dict((await conn.execute(
                sa.select(Lead.__table__.c.conversation_id, Lead.__table__.c.trial_slot)
            )).all())
            tasks = {
                (conv_id, kind): (str(run_at), state)
                for conv_id, kind, run_at, state in (await conn.execute(sa.select(
                    FollowupTask.__table__.c.conversation_id, FollowupTask.__table__.c.kind,
                    FollowupTask.__table__.c.run_at, FollowupTask.__table__.c.state,
                ))).all()
            }
            version = (await conn.exec_driver_sql("PRAGMA user_version")).scalar_one()
    finally:
        await engine.dispose()

    assert slots[evening] == datetime(2026, 10, 12, 12, 0) and slots[morning] == datetime(2026, 10, 13, 4, 0)
    assert tasks[(evening, "trial_reminder_2h")] == ("2026-10-12 10:00:00", "pending"), "15:00 по Алматы"
    assert tasks[(evening, "trial_reminder_morning")] == ("2026-10-12 04:30:00", "pending"), "по-прежнему 09:30"
    assert tasks[(evening, "fu_soft")] == ("2026-10-11 10:00:00", "pending"), "не о пробном — не трогается"
    assert tasks[(morning, "trial_reminder_morning")][1] == "cancelled", "занятие в 09:00 — утро опоздало бы"
    assert version == DATA_VERSION


# --------------------------------------------------------------------------- #
# «Учимся в 9 школе» — не имя и не возраст
# --------------------------------------------------------------------------- #
_DIALOG = (
    "Скажите пожалуйста чем отличаются бокс от кикбоксинга ?",
    "На пробное придем в понедельник 17:00",
    "Кск",
    "Учимся в 9 школе",
)


async def test_school_is_not_taken_for_the_childs_name(make_ctx) -> None:
    ctx = make_ctx(client_texts=_DIALOG, dialog_client_texts=_DIALOG)

    result = await create_trial_lead(
        ctx, child_name="Учимся 9 школа", gym_id=KSK, child_age=9, parent_agreed=True,
        session_time="17:00", session_day="mon", discipline="kickboxing",
    )

    assert result.status.value == "invalid_input", result
    assert "фамилию и имя" in (result.error or ""), result


async def test_school_number_is_not_the_childs_age(make_ctx) -> None:
    texts = (*_DIALOG, "Иванова Аружан")
    ctx = make_ctx(client_texts=texts, dialog_client_texts=texts)

    result = await create_trial_lead(
        ctx, child_name="Иванова Аружан", gym_id=KSK, child_age=9, parent_agreed=True,
        session_time="17:00", session_day="mon", discipline="kickboxing",
    )

    assert result.data["booked"] is False and "need_age" in result.data["needs"], result.data


def test_numbers_of_places_are_not_ages() -> None:
    for said in ("Учимся в 9 школе", "школа №9", "в 9-ой школе", "во 2 классе", "7 мкр", "мкр 7"):
        number = 2 if "класс" in said else (7 if "мкр" in said else 9)
        assert not client_named_age(number, [said]), said
    assert client_named_age(8, ["Иванов Али 8 лет, 16 школа"]), "возраст рядом с номером школы остаётся"
    assert client_named_age(9, ["ему 9"])
    assert age_answer("9", "Сколько лет ребёнку?") == 9
    assert age_answer("9 школа", "Сколько лет ребёнку?") is None


def test_real_surnames_that_look_like_school_words_are_names() -> None:
    from app.tools.booking import _clean_name

    for name in ("Садикова Аружан", "Школьников Иван", "Классен Анна", "Мкртчян Арам"):
        assert _clean_name(name) == name
    for junk in ("Учимся 9 школа", "Учимся школа", "Али 2", "Али, школа"):
        assert _clean_name(junk) is None, junk


# --------------------------------------------------------------------------- #
# Ход записи: «Да все верно», повтор адреса, «Ок» после записи
# --------------------------------------------------------------------------- #
def _deps(kb, state, sessionmaker, settings, turns) -> PipelineDeps:
    kb_loader.swap(kb)
    return PipelineDeps(
        sessionmaker=sessionmaker, state=state, kb=kb_loader.get_snapshot, queue=RecordingQueue(),
        settings=settings, llm=FakeLLMClient(turns),
    )


async def _say(deps: PipelineDeps, chat: str, texts) -> list:
    decisions = []
    for number, text in enumerate(texts):
        decisions.append(await process_inbound(deps, webhook_payload(f"{chat}-{number}", text, chat_id=chat)))
    return decisions


_OPENING = [
    FakeTurn.answer(
        "В боксе удары наносятся только руками, а в кикбоксинге — руками и ногами. "
        "Хотите записать дочку на бесплатное пробное занятие?"
    ),
    FakeTurn.answer("В каком районе вы живёте? Подберу ближайший к вам зал."),
]


async def test_confirmation_without_a_district_reasks_with_the_gym_list(kb, state, sessionmaker, settings) -> None:
    """Модель повторила «понедельник 17:00» клиента, фильтр снял ответ — вместо
    администратора уходит список залов с тем же вопросом о районе."""
    deps = _deps(kb, state, sessionmaker, settings, [
        *_OPENING, FakeTurn.answer("Отлично! Ждём вас в понедельник в 17:00."),
    ])

    turns = await _say(deps, "77015551021", _DIALOG[:2] + ("Да все верно",))

    last = turns[-1]
    assert [d.action for d in last] == [DecisionAction.REPLY], [(d.action, d.reason) for d in last]
    text = "\n".join(out.text or "" for d in last for out in d.outbound)
    assert kb.text("card.gyms_city_title", Language.RU) in text, text
    assert "в каком районе вы живёте?" in text, text
    assert "администратор" not in text


@pytest.mark.parametrize(
    "question",
    [
        "А сколько стоит для двоих детей?",
        "сколько стоит для двоих детей",  # без знака вопроса, но о цене
        "А можно прийти с братом?",  # знак вопроса без темы из словаря
    ],
)
async def test_a_question_after_a_blocked_reply_still_goes_to_the_admin(
    kb, state, sessionmaker, settings, question
) -> None:
    """Повтор вопроса записи — только когда клиент сам ничего не спросил."""
    deps = _deps(kb, state, sessionmaker, settings, [
        *_OPENING, FakeTurn.answer("Абонемент стоит 31 000 ₸."),
    ])

    turns = await _say(deps, "77015551022", _DIALOG[:2] + (question,))

    assert [d.action for d in turns[-1]] == [DecisionAction.ESCALATE], turns[-1]


async def test_yes_all_correct_is_an_answer_not_a_question_without_data(kb, state, sessionmaker, settings) -> None:
    """«Да все верно» — согласие: позвать администратора «нет данных» модель не может."""
    deps = _deps(kb, state, sessionmaker, settings, [
        *_OPENING,
        FakeTurn.tool(FakeCall("escalate_to_manager", {"reason": "no_data", "question_summary": "район не назван"})),
        FakeTurn.answer("Подскажите, пожалуйста, в каком районе вы живёте?"),
    ])

    turns = await _say(deps, "77015551023", _DIALOG[:2] + ("Да все верно",))

    escalation = [inv for d in turns[-1] for inv in d.invocations if inv.name == "escalate_to_manager"]
    assert escalation and escalation[0].result.status.value == "invalid_input"
    assert [d.action for d in turns[-1]] == [DecisionAction.REPLY]


def test_agreement_words_with_all() -> None:
    words = ["да", "верно", "правильно"]
    assert is_bare_agreement("Да все верно", words)
    assert is_bare_agreement("Да, всё правильно", words)
    assert not is_bare_agreement("Всё", words), "«всё» само по себе не «да»"


def test_address_retold_under_the_location_card_is_dropped(kb) -> None:
    card = (
        "📍 КСК — школа №9\nКаирбекова 334, школа №9 (цокольный этаж)\n\n"
        "🗺 На карте:\nhttps://2gis.kz/kostanay/geo/70000001062421593"
    )
    reply = (
        "Зал на КСК находится по адресу Каирбекова 334, в цокольном этаже школы №9.\n\n"
        "Запишу на бесплатное пробное — подскажите фамилию и имя ребёнка?"
    )

    assert strip_card_repeats(reply, [card]) == "Запишу на бесплатное пробное — подскажите фамилию и имя ребёнка?"


async def test_no_district_question_after_the_booking(kb, state, sessionmaker, settings) -> None:
    """На «Ок» после подтверждения записи вопрос «В каком районе вы живёте?» не дописывается."""
    deps = _deps(kb, state, sessionmaker, settings, [
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Иванова Аружан", "child_age": 8, "gym_id": KSK, "parent_agreed": True,
            "session_time": "17:00", "session_day": "mon", "discipline": "kickboxing",
        })),
        FakeTurn.answer(""),
        FakeTurn.answer("Рад был помочь! Если появятся вопросы перед занятием, смело пишите сюда. Хорошего дня!"),
    ])

    turns = await _say(deps, "77015551024", (
        "Да, Иванова Аружан, 8 лет, кикбоксинг в КСК в понедельник в 17:00", "Ок",
    ))

    booked = [inv for d in turns[0] for inv in d.invocations if inv.name == "create_trial_lead"]
    assert booked and booked[0].result.data.get("booked") is True, booked[0].result
    text = "\n".join(out.text or "" for d in turns[-1] for out in d.outbound)
    assert text.startswith("Рад был помочь!"), text
    assert kb.text("funnel.district", Language.RU) not in text, text


async def test_funnel_question_still_follows_an_open_answer(kb, state, sessionmaker, settings) -> None:
    """Контроль: не записанному клиенту вопрос воронки дописывается по-прежнему."""
    deps = _deps(kb, state, sessionmaker, settings, [
        FakeTurn.answer("В боксе удары наносятся только руками, а в кикбоксинге — и ногами."),
    ])

    turns = await _say(deps, "77015551025", ("Чем отличается бокс от кикбоксинга?",))

    text = "\n".join(out.text or "" for d in turns[-1] for out in d.outbound)
    assert text.rstrip().endswith("?"), text


# --------------------------------------------------------------------------- #
# «А почему 22:00?» — не вопрос о цене
# --------------------------------------------------------------------------- #
def test_why_is_not_a_price_question(kb) -> None:
    assert lexicon.intent_hints("Здравствуйте! А почему 22:00?", lexicon=kb.lexicon) == ()
    assert lexicon.intent_hints("почем абонемент?", lexicon=kb.lexicon) == (IntentHint.PRICE,)
    assert lexicon.intent_hints("Почему так дорого?", lexicon=kb.lexicon) == (IntentHint.PRICE,)
    assert lexicon.intent_hints("абонимнт", lexicon=kb.lexicon) == (IntentHint.PRICE,), "опечатки прощаются"
    # Словарь на диске владелец правил раньше, и нового списка в нём может не быть.
    without_list = kb.lexicon.model_copy(update={"typo_exceptions": []})
    assert lexicon.intent_hints("А почему 22:00?", lexicon=without_list) == ()


def test_owner_can_add_a_real_word_to_the_typo_exceptions(kb) -> None:
    """Список в lexicon.yaml работает сам по себе, не только встроенное «почему»."""
    assert lexicon.intent_hints("адреса", lexicon=kb.lexicon) == (IntentHint.LOCATION,)
    edited = kb.lexicon.model_copy(update={"typo_exceptions": ["адреса"]})

    assert lexicon.intent_hints("адреса", lexicon=edited) == ()
