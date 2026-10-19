"""Диалог 12–13.10.2026 из CRM и просьбы владельца 13.10.2026.

Клиент с рекламы: «Хочу записать ребёнка» → «Где находится» → «5 лет» — и модель сама
показала КСК; «Бокс», «На завтра на 9.00», «Иванов Тимур» — запись в КСК на 09:00;
«Нет в центре» — и запись молча уехала в центр на 18:00. Наутро: «Вчера на 9.00
договорились», «Можно по раньше», «К 15.00» — и два одинаковых «Здесь лучше ответит
администратор…». Владелец: у «Романтика» утренних занятий нет; на Плазе только Вт, Чт, Сб
в 9:00 и 19:00; Каирбекова 24 так и не убрали.

Номера в тестах вымышленные.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from app.core import pipeline
from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.kb.agreement import numbered_choices, with_agreement, with_choice
from app.kb.render import render_schedule_card
from app.kb.sessions import client_named_discipline, client_named_time
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
from app.llm.dynamic import build_dynamic_note
from app.storage import repo_lead, repo_message, repo_outbox
from app.storage.models import Conversation
from app.tools.booking import create_trial_lead
from app.tools.escalation import escalate_to_manager
from app.tools.gyms import find_gym_by_district, gyms_named_in
from app.tools.schedule import find_sessions
from app.types import (
    TRIAL_CONFIRMATION_ARTIFACT,
    ChannelKind,
    DecisionAction,
    Language,
    LeadDraft,
    LeadStatus,
    OutboundKind,
    OutboundMessage,
    SavedLead,
)

from tests.conftest import RecordingQueue, webhook_payload

UTC = timezone.utc
ALMATY = ZoneInfo("Asia/Almaty")
KSK = "ksk_kairbekova_334"
CENTER = "center_kasymkhanova_10"
PLAZA = "plaza_szm_70"
ROMANTIK = "magazin15_voinov_8b"
#: Понедельник 12.10.2026, 14:05 по Алматы — как в диалоге.
MONDAY = datetime(2026, 10, 12, 14, 5, tzinfo=ALMATY)
FOOTER = "Напишите только цифру: {numbers}"


def _ctx(make_ctx, *texts: str, now: datetime = MONDAY, **extra):
    return replace(make_ctx(client_texts=texts, dialog_client_texts=texts, **extra), now=now.astimezone(UTC))


def _deps(kb, state, sessionmaker, settings, turns) -> PipelineDeps:
    kb_loader.swap(kb)
    return PipelineDeps(
        sessionmaker=sessionmaker, state=state, kb=kb_loader.get_snapshot, queue=RecordingQueue(),
        settings=settings, llm=FakeLLMClient(turns),
    )


def _client_text(decisions) -> str:
    return "\n".join(out.text or "" for d in decisions for out in d.outbound)


# --------------------------------------------------------------------------- #
# Расписание и залы — просьбы владельца
# --------------------------------------------------------------------------- #
def test_romantik_has_no_morning_classes(kb) -> None:
    starts = {(slot.discipline, slot.time_start) for slot in kb.gym(ROMANTIK).schedule}
    assert starts == {("boxing", "18:45"), ("kickboxing", "18:45")}, starts


def test_plaza_trains_only_tue_thu_sat_at_nine_and_seven(kb) -> None:
    slots = {(slot.discipline, tuple(slot.days), slot.time_start, slot.time_end) for slot in kb.gym(PLAZA).schedule}
    days = ("tue", "thu", "sat")
    assert slots == {
        (discipline, days, start, end)
        for discipline in ("boxing", "kickboxing")
        for start, end in (("09:00", "10:30"), ("19:00", "20:30"))
    }, slots
    card = render_schedule_card(kb, gym_id=PLAZA, slots=kb.gym(PLAZA).schedule, lang=Language.RU)
    assert "Пн" not in card and "17:00" not in card, card


@pytest.mark.parametrize("query", ["центр", "в центре", "Нет в центре"])
async def test_the_centre_is_the_centre_gym_itself(ctx, query) -> None:
    """Зал в центре один — «в центре зала нет, ближайший…» было бы неправдой."""
    result = await find_gym_by_district(ctx, district_text=query)

    assert result.data["gyms"][0]["id"] == CENTER, result.data
    assert not result.data.get("nearby_only"), result.data


async def test_numbered_shop_finds_romantik(ctx) -> None:
    result = await find_gym_by_district(ctx, district_text="мы у 15-го магазина")

    assert result.data["gyms"][0]["id"] == ROMANTIK, result.data


# --------------------------------------------------------------------------- #
# Дата для модели
# --------------------------------------------------------------------------- #
def test_model_sees_school_time_with_today_and_tomorrow() -> None:
    note = build_dynamic_note(
        lang=Language.RU, now=MONDAY, lead=LeadDraft(), intents=(), injection_suspected=False,
    )

    assert "Сейчас по времени школы: 12.10.2026 14:05" in note, note
    assert "Сегодня — понедельник, 12.10; завтра — вторник, 13.10" in note, note


def test_pipeline_passes_school_time_not_utc(settings) -> None:
    late = datetime(2026, 10, 12, 20, 30, tzinfo=UTC)  # 01:30 вторника по Алматы
    assert pipeline._school_time(late, settings).strftime("%d.%m %H:%M") == "13.10 01:30"


# --------------------------------------------------------------------------- #
# Зал выбирает родитель
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "gyms"),
    [
        ("Кск", {KSK}), ("Нет в центре", {CENTER}), ("Касымханова 10", {CENTER}),
        ("в зал у 15-го магазина", {ROMANTIK}), ("2", set()), ("Бокс — Вт, Чт, Сб 18:45", set()),
        ("Иванов Тимур", set()), ("На завтра тогда на 9.00", set()),
        # Кусок ключа — не зал: возраст «9», «в школе», «возле магазина».
        ("9", set()), ("Учимся в школе", set()), ("возле магазина", set()),
    ],
)
def test_gyms_named_in_client_words(kb, text, gyms) -> None:
    assert set(gyms_named_in(kb, text)) == gyms


async def test_gym_the_client_never_named_is_not_booked(make_ctx) -> None:
    """Диалог 12.10.2026: КСК выбрала модель, клиенту был нужен центр."""
    ctx = _ctx(make_ctx, "Хочу записать ребёнка", "5 лет", "Бокс", "На завтра тогда на 9.00", "Иванов Тимур")

    result = await create_trial_lead(
        ctx, child_name="Иванов Тимур", child_age=5, gym_id=KSK, parent_agreed=True,
        session_time="09:00", discipline="boxing",
    )

    assert result.data["booked"] is False and result.data["needs"][0] == "need_gym", result.data
    assert result.data["gym_id"] is None
    assert not ctx.services.leads


async def test_gym_picked_by_number_from_the_list_counts(make_ctx) -> None:
    ctx = _ctx(
        make_ctx, "5 лет, бокс, на завтра на 9.00", "Иванов Тимур",
        list_choices=("Расскажите про зал «КСК — школа №9» (Каирбекова 334): расписание и как добраться",),
    )

    result = await create_trial_lead(
        ctx, child_name="Иванов Тимур", child_age=5, gym_id=KSK, parent_agreed=True,
        session_time="09:00", discipline="boxing",
    )

    assert result.data["booked"] is True, result.data


async def test_yes_to_an_offer_naming_one_gym_counts(make_ctx, kb) -> None:
    agreed = with_agreement("Да", "Записать Тимура в КСК на бокс во вторник в 09:00?", kb.lexicon.agreement)
    ctx = _ctx(make_ctx, "Иванов Тимур, 5 лет", agreed)

    result = await create_trial_lead(
        ctx, child_name="Иванов Тимур", child_age=5, gym_id=KSK, parent_agreed=True,
        session_time="09:00", discipline="boxing",
    )

    assert result.data["booked"] is True, result.data


async def test_pipeline_asks_the_gym_with_the_numbered_list(kb, state, sessionmaker, settings) -> None:
    deps = _deps(kb, state, sessionmaker, settings, [
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Иванов Тимур", "child_age": 5, "gym_id": KSK, "parent_agreed": True,
            "discipline": "boxing",
        })),
        FakeTurn.answer("Хорошо."),
    ])

    decisions = await process_inbound(deps, webhook_payload("g-1", "Иванов Тимур, 5 лет, бокс", chat_id="77015551301"))

    text = _client_text(decisions)
    assert kb.text("card.gyms_city_title", Language.RU) in text, text
    assert "Напишите номер зала — пришлю расписание и точку на карте." in text, "список залов не испорчен"
    assert "Напишите только цифру" not in text
    assert "Мы записали вас" not in text


# --------------------------------------------------------------------------- #
# Время, которого родитель не выбирал
# --------------------------------------------------------------------------- #
async def test_other_time_than_asked_is_confirmed_first(make_ctx) -> None:
    """«На завтра на 9.00», а в центре бокс только в 18:00 — сначала вопрос."""
    ctx = _ctx(make_ctx, "Бокс", "На завтра тогда на 9.00", "Иванов Тимур, 5 лет", "Нет, в центре")

    result = await create_trial_lead(
        ctx, child_name="Иванов Тимур", child_age=5, gym_id=CENTER, parent_agreed=True,
        discipline="boxing",
    )

    assert result.data["needs"] == ["need_time_confirm"], result.data
    assert result.data["confirm_kind"] == "book" and result.data["confirm_when"] == "Во вторник, 13.10 в 18:00"
    assert not ctx.services.leads


async def test_yes_to_the_confirmation_books(make_ctx, kb) -> None:
    agreed = with_agreement(
        "Да", "Записать в зал «Центр — Жана-Кала» — во вторник, 13.10 в 18:00?", kb.lexicon.agreement
    )
    ctx = _ctx(make_ctx, "Бокс", "На завтра тогда на 9.00", "Иванов Тимур, 5 лет", "Нет, в центре", agreed)

    result = await create_trial_lead(
        ctx, child_name="Иванов Тимур", child_age=5, gym_id=CENTER, parent_agreed=True,
        discipline="boxing",
    )

    assert result.data["booked"] is True, result.data


def test_date_in_the_bots_question_is_not_a_second_time(kb) -> None:
    """«13.10 в 18:00» — одно время: «Да» на такой вопрос выбирает 18:00."""
    agreed = with_agreement("Да", "Записать — во вторник, 13.10 в 18:00?", kb.lexicon.agreement)

    assert client_named_time("18:00", [agreed])
    assert not client_named_time("13:10", [agreed])


async def test_moving_a_booking_to_another_hour_is_confirmed(make_ctx) -> None:
    saved = SavedLead(
        lead_id=__import__("uuid").uuid4(), status=LeadStatus.TRIAL_BOOKED.value, gym_id=KSK,
        trial_slot=datetime(2026, 10, 13, 9, 0, tzinfo=ALMATY).astimezone(UTC).replace(tzinfo=None),
        child_name="Иванов Тимур", child_age=5,
    )
    ctx = _ctx(make_ctx, "Бокс", "Иванов Тимур, 5 лет", "Нет в центре", "Надо", saved_lead=saved)

    result = await create_trial_lead(
        ctx, child_name="Иванов Тимур", child_age=5, gym_id=CENTER, parent_agreed=True,
        discipline="boxing",
    )

    assert result.data["needs"] == ["need_time_confirm"] and result.data["confirm_kind"] == "move", result.data


async def test_pipeline_asks_to_confirm_the_move(kb, state, sessionmaker, settings) -> None:
    """Вопрос задаёт код, и общее «Записать ребёнка?» после карточки зала его не вытесняет."""
    deps = _deps(kb, state, sessionmaker, settings, [
        FakeTurn.tool(FakeCall("get_schedule", {"gym_id": CENTER})),
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Иванов Тимур", "child_age": 5, "gym_id": CENTER, "parent_agreed": True,
            "discipline": "boxing",
        })),
        FakeTurn.answer("В центре утром занятий нет."),
    ])

    decisions = await process_inbound(deps, webhook_payload(
        "m-1", "Иванов Тимур, 5 лет, бокс, на 9.00, но в центре", chat_id="77015551302"
    ))

    text = _client_text(decisions)
    assert "Записать в зал «Центр — Жана-Кала» — " in text and text.rstrip().endswith("в 18:00?"), text
    assert kb.text("funnel.book_trial", Language.RU) not in text, text


# --------------------------------------------------------------------------- #
# Секция — один раз за разговор
# --------------------------------------------------------------------------- #
def test_section_said_once_counts_for_the_whole_dialog() -> None:
    dialog = ["Бокс", "Сегодня", "На завтра", "Иванов Тимур", "Нет в центре", "Надо", "Касымханова 10"]

    assert client_named_discipline("boxing", dialog, single_only=True)
    assert not client_named_discipline(
        "boxing", ["Чем отличаются бокс от кикбоксинга?"], single_only=True
    ), "вопрос о двух секциях — не выбор"


async def test_section_is_not_asked_again_after_the_gym_changes(make_ctx) -> None:
    texts = ("Бокс", "Сегодня", "На завтра", "Иванов Тимур, 5 лет", "Нет в центре", "Надо", "Касымханова 10")
    ctx = replace(make_ctx(client_texts=texts[-6:], dialog_client_texts=texts), now=MONDAY.astimezone(UTC))

    result = await create_trial_lead(
        ctx, child_name="Иванов Тимур", child_age=5, gym_id=CENTER, parent_agreed=True,
        discipline="boxing",
    )

    assert "need_discipline" not in (result.data.get("needs") or []), result.data


# --------------------------------------------------------------------------- #
# Списки с номерами и цифра в ответ
# --------------------------------------------------------------------------- #
def test_dashes_under_a_choice_question_get_numbers() -> None:
    assert numbered_choices("На какую секцию записать?\n\n— Бокс\n— Кикбоксинг", FOOTER) == (
        "На какую секцию записать?\n\n1. Бокс\n2. Кикбоксинг\n\nНапишите только цифру: 1, 2"
    )
    with_our_footer = "Какую секцию?\n— Бокс\n— Кикбоксинг\n\nНапишите только цифру: 1, 2"
    assert "1. Бокс" in numbered_choices(with_our_footer, FOOTER)
    story = "Расписание:\n— Бокс Вт, Чт 18:00\n— Кикбоксинг Пн 19:00\nЗаписать?"
    assert numbered_choices(story, FOOTER) == story, "рассказ о зале — не выбор"


def _history(*bot_texts: str) -> list[dict]:
    items = []
    for text in bot_texts:
        items += [{"role": "model", "parts": [{"text": text}]}, {"role": "user", "parts": [{"text": "…"}]}]
    return items[:-1]


def test_digit_after_a_re_asked_choice_goes_to_the_list() -> None:
    listing = "На какую секцию записать ребёнка?\n\n1. Бокс — Вт, Чт, Сб 18:00\n2. Кикбоксинг — Вт, Чт, Сб 18:00"
    re_asked = _history(listing, "Какую именно секцию — бокс или кикбоксинг?")
    unrelated = _history(listing, "Сколько лет ребёнку?")

    assert pipeline._choice_context(re_asked) == listing
    assert pipeline._choice_context(unrelated) == "Сколько лет ребёнку?"
    assert "«Бокс — Вт, Чт, Сб 18:00»" in with_choice("1", pipeline._choice_context(re_asked))


def test_welcome_is_not_a_lead_in_to_a_choice() -> None:
    assert pipeline._lead_in("Ждём вас на первой тренировке!\n\nНа какую секцию записать?") == ""
    assert pipeline._lead_in("В 17:30 занятий нет. Какое время?") == "В 17:30 занятий нет."


# --------------------------------------------------------------------------- #
# Подтверждение записи, склеенное с карточкой
# --------------------------------------------------------------------------- #
async def test_model_text_after_a_merged_confirmation_is_dropped(kb, state, sessionmaker, settings) -> None:
    """Расписание и запись разными шагами: подтверждение склеилось с карточкой расписания."""
    deps = _deps(kb, state, sessionmaker, settings, [
        FakeTurn.tool(FakeCall("get_schedule", {"gym_id": KSK})),
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Иванов Тимур", "child_age": 5, "gym_id": KSK, "parent_agreed": True,
            "session_time": "09:00", "discipline": "boxing", "session_day": "tue",
        })),
        FakeTurn.answer("Пожалуйста, приходите за 10 минут до начала. Ждём вас!"),
    ])

    decisions = await process_inbound(deps, webhook_payload(
        "c-1", "Иванов Тимур, 5 лет, бокс в КСК во вторник в 9:00", chat_id="77015551303"
    ))

    text = _client_text(decisions)
    assert "Мы записали вас" in text and "Ждём вас!" not in text, text
    async with sessionmaker() as db:
        conv = (await db.execute(sa.select(Conversation))).scalars().one()
        assert await repo_message.count_artifact_sends(db, conv.id, TRIAL_CONFIRMATION_ARTIFACT) == 1


async def test_card_merged_into_a_text_still_counts_as_sent(sessionmaker) -> None:
    async with sessionmaker() as db:
        conv = Conversation(conv_key="wa:1", channel_id="wa-1", chat_type="whatsapp", chat_id="1")
        db.add(conv)
        await db.flush()
        common = dict(conversation_id=conv.id, channel_id="wa-1", channel=ChannelKind.WHATSAPP, chat_id="1", lang=Language.RU)
        first = await repo_outbox.enqueue(db, OutboundMessage(**common, text="Вот расписание:"))
        assert await repo_outbox.append_text(db, first, "🗓 карточка", artifact_ids=("schedule_ksk_kairbekova_334",))

        assert await repo_message.count_artifact_sends(db, conv.id, "schedule_ksk_kairbekova_334") == 1
        assert await repo_message.any_artifact_sent(db, conv.id, ("schedule_",))
        assert await repo_message.last_artifact_with_prefix(db, conv.id, ("schedule_",)) == "schedule_ksk_kairbekova_334"


# --------------------------------------------------------------------------- #
# Модель знает запись: время относительно сегодня и переносы
# --------------------------------------------------------------------------- #
async def _booked_with_history(sessionmaker, kb):
    async with sessionmaker() as db:
        conv = Conversation(conv_key="wa:2", channel_id="wa-1", chat_type="whatsapp", chat_id="2")
        db.add(conv)
        await db.flush()
        await repo_lead.upsert(db, LeadDraft(
            conversation_id=conv.id, lang=Language.RU, status=LeadStatus.TRIAL_BOOKED,
            child_name="Иванов Тимур", child_age=5, gym_id=CENTER, channel=ChannelKind.WHATSAPP,
            channel_user="2", trial_slot=datetime(2026, 10, 13, 18, 0, tzinfo=ALMATY),
        ))
        header = kb.text("system.trial_confirmed", Language.RU)
        common = dict(conversation_id=conv.id, channel_id="wa-1", channel=ChannelKind.WHATSAPP, chat_id="2", lang=Language.RU)
        for place, when in (
            ("КСК — школа №9, Каирбекова 334", "Во вторник, 13.10 в 09:00"),
            ("Центр — Жана-Кала, Касымханова 10", "Во вторник, 13.10 в 18:00"),
        ):
            await repo_outbox.enqueue(db, OutboundMessage(
                **common, kind=OutboundKind.LEAD_CONFIRMATION, artifact_id=TRIAL_CONFIRMATION_ARTIFACT,
                text=f"✅ {header}\n\n👤 Иванов Тимур\n🥊 Бокс\n📍 {place}\n🗓 {when}",
            ))
        await db.commit()
        return conv.id


async def test_stage_note_says_today_and_the_move(kb, sessionmaker, settings) -> None:
    conv_id = await _booked_with_history(sessionmaker, kb)
    morning = datetime(2026, 10, 13, 9, 33, tzinfo=ALMATY).astimezone(UTC)

    async with sessionmaker() as db:
        conv = await db.get(Conversation, conv_id)
        note = await pipeline._client_stage(db, conv, kb=kb, now=morning, settings=settings)

    assert "Действующая запись: Центр — Жана-Кала, занятие сегодня в 18:00." in note, note
    assert "переносили: Во вторник, 13.10 в 09:00, КСК — школа №9" in note, note


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 10, 12, 14, 14, tzinfo=ALMATY), "занятие завтра, 13.10 (вторник) в 18:00"),
        (datetime(2026, 10, 13, 20, 0, tzinfo=ALMATY), "занятие уже прошло"),
    ],
)
async def test_stage_note_counts_from_today(kb, sessionmaker, settings, moment, expected) -> None:
    conv_id = await _booked_with_history(sessionmaker, kb)

    async with sessionmaker() as db:
        conv = await db.get(Conversation, conv_id)
        note = await pipeline._client_stage(db, conv, kb=kb, now=moment.astimezone(UTC), settings=settings)

    assert expected in note, note


# --------------------------------------------------------------------------- #
# Администратор уже позван
# --------------------------------------------------------------------------- #
async def test_second_handover_is_refused_while_the_first_is_open(make_ctx) -> None:
    ctx = _ctx(make_ctx, "Позовите администратора")
    ctx.services.escalated_recently = True

    result = await escalate_to_manager(ctx, reason="no_data", question_summary="хочет пораньше")

    assert not result.ok and "уже получил" in (result.error or ""), result
    assert not ctx.services.cards and not ctx.services.pauses


async def test_medical_question_is_handed_over_even_when_one_is_open(make_ctx) -> None:
    ctx = _ctx(make_ctx, "У ребёнка астма, можно ли заниматься?")
    ctx.services.escalated_recently = True

    result = await escalate_to_manager(ctx, reason="medical", question_summary="астма")

    assert result.ok and ctx.services.cards


async def test_blocked_replies_do_not_repeat_the_handover(kb, state, sessionmaker, settings) -> None:
    """«Можно по раньше», «К 15.00» — одно «передаю администратору», затем «уже у него», затем тишина."""
    invented = FakeTurn.answer("Можно в 15:30 в КСК.")
    deps = _deps(kb, state, sessionmaker, settings, [])
    chat = "77015551304"
    handoff = kb.text("escalation.handoff", Language.RU)
    pending = kb.text("escalation.pending", Language.RU)

    texts, cards = [], []
    for number, said in enumerate(("Можно по раньше?", "К 15.00?", "Ну так можно?"), 1):
        deps.llm.reset([invented])
        decisions = await process_inbound(deps, webhook_payload(f"e-{number}", said, chat_id=chat))
        texts.append(_client_text(decisions))
        cards.append(sum(1 for d in decisions for card in d.manager_cards if card.kind.value == "escalation"))

    assert texts[0] == handoff, texts
    assert texts[1] == pending, texts
    assert texts[2] == "", "третий раз — тишина"
    assert cards == [1, 0, 0], cards


# --------------------------------------------------------------------------- #
# Вопрос о зале вслепую и свой список залов модели
# --------------------------------------------------------------------------- #
async def test_which_gym_question_comes_with_the_list(kb, state, sessionmaker, settings) -> None:
    deps = _deps(kb, state, sessionmaker, settings, [FakeTurn.answer(
        "Здравствуйте! С радостью запишу ребёнка.\n\nВ каком из наших залов вам будет удобнее заниматься?"
    )])

    decisions = await process_inbound(deps, webhook_payload("w-1", "Хочу записать ребёнка на пробную", chat_id="77015551305"))

    text = _client_text(decisions)
    assert text.startswith("Здравствуйте! С радостью запишу ребёнка."), text
    assert kb.text("card.gyms_city_title", Language.RU) in text, text
    assert "В каком из наших залов" not in text


async def test_models_own_gym_list_is_replaced_by_the_card(kb, state, sessionmaker, settings) -> None:
    deps = _deps(kb, state, sessionmaker, settings, [FakeTurn.answer(
        "Наши залы:\n1. Центр (Касымханова 10)\n2. КСК (Кайырбекова 334)\n3. Плаза\nПодсказать расписание?"
    )])

    decisions = await process_inbound(deps, webhook_payload("w-2", "Где у вас залы?", chat_id="77015551306"))

    text = _client_text(decisions)
    assert kb.text("card.gyms_city_title", Language.RU) in text and "Кайырбекова" not in text, text


async def test_which_gym_after_the_list_is_left_alone(kb, state, sessionmaker, settings) -> None:
    deps = _deps(kb, state, sessionmaker, settings, [])
    chat = "77015551307"
    deps.llm.reset([FakeTurn.tool(FakeCall("send_content", {"artifact_id": "gyms_list_city"})), FakeTurn.answer("")])
    await process_inbound(deps, webhook_payload("w-3", "Где находится?", chat_id=chat))
    deps.llm.reset([FakeTurn.answer("Какой зал вам ближе?")])

    second = await process_inbound(deps, webhook_payload("w-4", "5 лет", chat_id=chat))

    assert _client_text(second) == "Какой зал вам ближе?"


async def test_schedule_question_after_the_list_asks_for_the_number(kb, state, sessionmaker, settings) -> None:
    """«Когда можно» после списка залов, ответ снят фильтром — бот просит номер зала."""
    deps = _deps(kb, state, sessionmaker, settings, [])
    chat = "77015551308"
    deps.llm.reset([FakeTurn.tool(FakeCall("send_content", {"artifact_id": "gyms_list_city"})), FakeTurn.answer("")])
    await process_inbound(deps, webhook_payload("s-1", "Где находится?", chat_id=chat))
    deps.llm.reset([FakeTurn.answer("Занятия по вторникам и четвергам.")])

    second = await process_inbound(deps, webhook_payload("s-2", "Когда можно?", chat_id=chat))

    assert [d.action for d in second] == [DecisionAction.REPLY], [d.reason for d in second]
    assert _client_text(second) == kb.text("card.pick_gym", Language.RU)


async def test_schedule_question_after_the_list_gets_every_schedule(kb, state, sessionmaker, settings) -> None:
    """«Какое расписание?» после списка залов — расписание всех залов с теми же номерами."""
    deps = _deps(kb, state, sessionmaker, settings, [])
    chat = "77015551309"
    deps.llm.reset([FakeTurn.tool(FakeCall("send_content", {"artifact_id": "gyms_list_city"})), FakeTurn.answer("")])
    await process_inbound(deps, webhook_payload("s-1", "Где находится?", chat_id=chat))
    deps.llm.reset([])

    second = await process_inbound(deps, webhook_payload("s-2", "Какое расписание?", chat_id=chat))

    text = _client_text(second)
    assert [d.reason for d in second] == ["schedule_overview"], text
    assert "2. КСК — школа №9\n🕒 Кикбоксинг: Пн, Ср, Пт" in text, text
    assert text.rstrip().endswith(kb.text("card.pick_gym_scheduled", Language.RU)), text


# --------------------------------------------------------------------------- #
# «Можно пораньше» — поиск по всем залам
# --------------------------------------------------------------------------- #
async def test_find_sessions_offers_earlier_boxing_in_other_gyms(make_ctx) -> None:
    morning = datetime(2026, 10, 13, 9, 34, tzinfo=ALMATY)
    ctx = _ctx(make_ctx, "Можно по раньше", now=morning)

    result = await find_sessions(ctx, discipline="boxing", day="today", time_to="18:00")

    found = [(item["gym_id"], item["time_start"]) for item in result.data["sessions"]]
    assert found == [(KSK, "17:00")], found
    started = {(item["gym_id"], item["time_start"]) for item in result.data["already_started_today"]}
    assert {(KSK, "09:00"), (PLAZA, "09:00")} <= started, started


async def test_todays_weekday_code_is_today(make_ctx) -> None:
    morning = datetime(2026, 10, 13, 9, 34, tzinfo=ALMATY)
    ctx = _ctx(make_ctx, "Можно по раньше", now=morning)

    result = await find_sessions(ctx, discipline="boxing", day="tue", time_to="18:00")

    assert [item["time_start"] for item in result.data["sessions"]] == ["17:00"], result.data


async def test_client_time_without_classes_is_not_blocked(kb, state, sessionmaker, settings, monkeypatch) -> None:
    """«К 15.00» → «В 15:00 занятий нет…» — время клиента, а не выдумка модели."""
    # Вторник 13.10 09:35, как в диалоге: бокс в КСК в 17:00 есть только по Вт, Чт, Сб.
    monkeypatch.setattr(pipeline, "_utcnow", lambda: datetime(2026, 10, 13, 9, 35, tzinfo=ALMATY).astimezone(UTC))
    deps = _deps(kb, state, sessionmaker, settings, [
        FakeTurn.tool(FakeCall("find_sessions", {"discipline": "boxing", "day": "today"})),
        FakeTurn.answer("В 15:00 занятий по боксу нет. Ближайшее — в 17:00 в КСК."),
    ])

    decisions = await process_inbound(deps, webhook_payload("f-1", "К 15.00", chat_id="77015551309"))

    assert not [d.postcheck_fail for d in decisions if d.postcheck_fail], [d.reason for d in decisions]
    assert "В 15:00 занятий по боксу нет" in _client_text(decisions)


async def test_find_sessions_rejects_nonsense(make_ctx) -> None:
    ctx = _ctx(make_ctx, "?")

    assert not (await find_sessions(ctx, discipline="karate")).ok
    assert not (await find_sessions(ctx, day="someday")).ok
    assert not (await find_sessions(ctx, time_to="поздно")).ok


def test_find_sessions_is_known_to_the_model_and_the_filter() -> None:
    from app.core.postcheck import PostcheckFailKind
    from app.tools.registry import TOOL_ALLOWS, TOOL_NAMES, tool_meta

    assert "find_sessions" in TOOL_NAMES
    assert PostcheckFailKind.TIME in TOOL_ALLOWS["find_sessions"]
    assert tool_meta("find_sessions")["side_effect"] is False


async def test_route_caption_skips_a_schedule_already_sent(make_ctx) -> None:
    from app.tools.content import send_content

    ctx = _ctx(make_ctx, "Нет в центре")
    ctx.services.artifact_sends[f"schedule_{CENTER}"] = 1
    telegram = replace(ctx, channel=ChannelKind.TELEGRAM)

    result = await send_content(telegram, artifact_id=f"route_{CENTER}")

    captions = [message.text for message in telegram.services.outbound if message.text]
    assert result.ok and captions and "🗓" not in captions[0], captions


async def test_model_request_carries_the_school_time(kb, state, sessionmaker, settings, monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "_utcnow", lambda: MONDAY.astimezone(UTC))
    deps = _deps(kb, state, sessionmaker, settings, [FakeTurn.answer("Здравствуйте! Чем помочь?")])

    await process_inbound(deps, webhook_payload("t-1", "Добрый день, есть вопрос", chat_id="77015551310"))

    assert "Сейчас по времени школы: 12.10.2026 14:05" in deps.llm.requests[-1].dynamic_note


def test_digit_after_what_did_you_choose_goes_to_the_list() -> None:
    listing = "На какую секцию записать?\n\n1. Бокс\n2. Кикбоксинг"

    assert pipeline._choice_context(_history(listing, "А что вы выбрали?")) == listing


async def test_booking_again_where_already_booked_needs_no_gym_words(make_ctx) -> None:
    saved = SavedLead(
        lead_id=__import__("uuid").uuid4(), status=LeadStatus.TRIAL_BOOKED.value, gym_id=KSK,
        trial_slot=datetime(2026, 10, 13, 9, 0, tzinfo=ALMATY).astimezone(UTC).replace(tzinfo=None),
        child_name="Иванов Тимур", child_age=5,
    )
    ctx = _ctx(make_ctx, "Иванов Тимур, 5 лет, бокс", "на 9:00", saved_lead=saved)

    result = await create_trial_lead(
        ctx, child_name="Иванов Тимур", child_age=5, gym_id=KSK, parent_agreed=True,
        session_time="09:00", discipline="boxing",
    )

    assert result.data["booked"] is True, result.data


def test_list_choices_are_read_from_the_history() -> None:
    note = pipeline._choice_note("2", "Расскажите про зал «КСК — школа №9»")
    contents = [
        {"role": "user", "parts": [{"text": "<user_message>\n2\n</user_message>"}, {"text": note}]},
        {"role": "model", "parts": [{"text": "🗓 Расписание"}]},
    ]

    assert pipeline._list_choices(contents, "выбор этого хода") == (
        "Расскажите про зал «КСК — школа №9»", "выбор этого хода",
    )


async def test_escalation_is_open_until_a_human_answers(sessionmaker) -> None:
    from app.core import pause
    from app.storage.models import EscalationState

    now = datetime(2026, 10, 13, 4, 34, tzinfo=UTC)
    async with sessionmaker() as db:
        conv = Conversation(conv_key="wa:3", channel_id="wa-1", chat_type="whatsapp", chat_id="3")
        db.add(conv)
        await db.flush()
        assert not await pause.escalation_open(db, conv.id, now=now, within_minutes=60), "передачи не было"
        row = EscalationState(conversation_id=conv.id, last_escalated_at=now - timedelta(minutes=5))
        db.add(row)
        await db.flush()
        assert await pause.escalation_open(db, conv.id, now=now, within_minutes=60)
        assert not await pause.escalation_open(db, conv.id, now=now + timedelta(hours=2), within_minutes=60)
        row.operator_last_seen_at = now - timedelta(minutes=1)
        await db.flush()
        assert not await pause.escalation_open(db, conv.id, now=now, within_minutes=60), "человек ответил"


async def test_find_sessions_keeps_to_the_section(make_ctx) -> None:
    ctx = _ctx(make_ctx, "Кикбоксинг в понедельник?")

    result = await find_sessions(ctx, discipline="kickboxing", day="mon")

    assert result.data["sessions"] and {item["discipline"] for item in result.data["sessions"]} == {"kickboxing"}
    assert {item["gym_id"] for item in result.data["sessions"]} >= {KSK}
