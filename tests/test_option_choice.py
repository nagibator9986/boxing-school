"""Цифра в ответ на список бота — выбор варианта; запись не уходит администратору.

Скриншот владельца 12.09.2026: бот предложил «— Кикбоксинг — Вт, Чт, Сб 19:00 /
— Бокс — Вт, Чт, Сб 19:00», клиент ответил «2», и бот передал запись
администратору. Воспроизведение на боевом ключе: модель цифру не поняла,
переспросила, а на второе «2» вызвала эскалацию «не смог помочь».
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.kb.agreement import chosen_option, offered_options, split_agreement, with_choice
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
from app.types import ChannelKind, Language, ToolContext

from tests.conftest import RecordingQueue, RecordingServices, webhook_payload

#: Бокс и кикбоксинг Вт·Чт·Сб в 19:00.
MKR6 = "mkr6_arystanbekova_6"
#: Бокс и кикбоксинг Пн·Ср·Пт в 09:00 и в 19:00.
RAHAT = "center_kairbekova_24"
SECTION_LIST = (
    "В это время в зале проходят и бокс, и кикбоксинг.\n\n"
    "На какую секцию записать Али?\n\n"
    "— Кикбоксинг — Вт, Чт, Сб 19:00\n"
    "— Бокс — Вт, Чт, Сб 19:00\n\n"
    "Напишите цифру или своими словами."
)


def _ctx(kb, *texts: str) -> ToolContext:
    return ToolContext(
        conversation_id=uuid4(), conv_key="conv:wa", channel=ChannelKind.WHATSAPP, channel_id="wa",
        chat_id="7701", lang=Language.RU, kb=kb, kb_hash=kb.kb_hash,
        now=datetime(2026, 9, 10, 7, 0, tzinfo=UTC), correlation_id="test", services=RecordingServices(),
        client_texts=texts,
    )


async def _deps(kb, state, sessionmaker, settings, llm) -> PipelineDeps:
    kb_loader.swap(kb)
    return PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=llm, kb=kb_loader.get_snapshot,
        queue=RecordingQueue(), settings=settings,
    )


# --------------------------------------------------------------------------- #
# Разбор списка
# --------------------------------------------------------------------------- #
def test_options_are_read_from_dashes_and_from_numbers() -> None:
    assert offered_options(SECTION_LIST) == ["Кикбоксинг — Вт, Чт, Сб 19:00", "Бокс — Вт, Чт, Сб 19:00"]
    assert offered_options("Какую?\n1. Бокс\n2) Кикбоксинг") == ["Бокс", "Кикбоксинг"]
    assert offered_options("Какую?\n1. Бокс\n3. Кикбоксинг") == [], "номера не подряд — это не список выбора"
    assert offered_options("Одна строка?\n— Бокс") == []


def test_digit_becomes_the_chosen_option() -> None:
    chosen = with_choice("2", SECTION_LIST)

    assert chosen_option(chosen) == "Бокс — Вт, Чт, Сб 19:00"
    assert split_agreement(chosen) == ("2", None), "выбранный вариант — не собственные слова клиента"
    assert with_choice("3", SECTION_LIST) == "3", "номера вне списка нет"
    assert with_choice("2", "Итак:\n— Бокс\n— Кикбоксинг") == "2", "без вопроса это не выбор"
    assert with_choice("Бокс", SECTION_LIST) == "Бокс"


def test_chosen_option_counts_for_section_and_time_but_not_for_age() -> None:
    from app.kb.sessions import client_named_age, client_named_day, client_named_discipline, client_named_time

    chosen = with_choice("2", SECTION_LIST)
    assert client_named_discipline("boxing", [chosen])
    assert not client_named_discipline("kickboxing", [chosen])
    assert client_named_time("19:00", [chosen])
    assert not client_named_day("tue", [chosen]), "«Вт, Чт, Сб» — расписание, а не выбор дня"
    assert not client_named_age(9, [with_choice("1", "Какую группу?\n1. Дети 9 лет\n2. Подростки")])


# --------------------------------------------------------------------------- #
# Запись и эскалация
# --------------------------------------------------------------------------- #
async def test_digit_choice_books_the_chosen_section(kb) -> None:
    from app.tools.booking import create_trial_lead

    ctx = _ctx(kb, "Айназаров Али, 8 лет", with_choice("2", SECTION_LIST))

    result = await create_trial_lead(
        ctx, child_name="Айназаров Али", child_age=8, gym_id=MKR6, parent_agreed=True, discipline="boxing"
    )

    assert result.data["booked"] is True, result.data


async def test_digit_choice_gives_the_time_when_the_section_has_two(kb) -> None:
    from zoneinfo import ZoneInfo

    from app.tools.booking import create_trial_lead

    offer = "Какое время удобнее?\n1. Бокс — Пн, Ср, Пт 09:00\n2. Бокс — Пн, Ср, Пт 19:00"
    ctx = _ctx(kb, "Айназаров Али, 8 лет, на бокс", with_choice("2", offer))

    result = await create_trial_lead(
        ctx, child_name="Айназаров Али", child_age=8, gym_id=RAHAT, parent_agreed=True,
        discipline="boxing", session_time="19:00",
    )

    assert result.data["booked"] is True, result.data
    assert ctx.services.leads[-1].trial_slot.astimezone(ZoneInfo("Asia/Almaty")).hour == 19


async def test_handover_on_a_bare_digit_or_a_choice_is_refused(kb) -> None:
    from app.tools.escalation import escalate_to_manager

    for latest in ("2", with_choice("2", SECTION_LIST)):
        for reason in ("repeated_miss", "no_data"):
            ctx = _ctx(kb, "Айназаров Али, 8 лет", latest)
            result = await escalate_to_manager(ctx, reason=reason, question_summary="Не понял цифру")
            assert not result.ok, (latest, reason)
            assert not ctx.services.pauses and not ctx.services.cards

    medical = _ctx(kb, "2")
    assert (await escalate_to_manager(medical, reason="medical", question_summary="Астма")).ok


# --------------------------------------------------------------------------- #
# Сквозь пайплайн
# --------------------------------------------------------------------------- #
async def test_screenshot_digit_after_the_section_list_books_the_child(kb, state, sessionmaker, settings) -> None:
    """Точная переписка со скриншота: список секций, ответ «2» — запись, а не администратор."""
    llm = FakeLLMClient([])
    deps = await _deps(kb, state, sessionmaker, settings, llm)
    chat = "77015558201"

    llm.reset([FakeTurn.tool(FakeCall("get_schedule", {"gym_id": MKR6})), FakeTurn.answer(SECTION_LIST)])
    await process_inbound(deps, webhook_payload(f"{chat}-1", "Хочу записать Айназарова Али, 8 лет, в 6-й микрорайон", chat_id=chat))
    llm.reset([
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Айназаров Али", "child_age": 8, "gym_id": MKR6, "parent_agreed": True,
            "discipline": "boxing",
        })),
        FakeTurn.answer(""),
    ])
    decisions = await process_inbound(deps, webhook_payload(f"{chat}-2", "2", chat_id=chat))

    request = [item for item in llm.requests if item.user_text][-1]
    assert "выбор из вариантов бота: «Бокс" in request.user_text, request.user_text
    assert [d.action.value for d in decisions] == ["reply"], [d.reason for d in decisions]
    client = "\n".join(out.text or "" for d in decisions for out in d.outbound)
    assert "Мы записали вас" in client, client
    assert not [card for d in decisions for card in d.manager_cards if card.kind.value == "escalation"]


async def test_filtered_answer_during_booking_asks_the_next_step_instead_of_handing_over(
    kb, state, sessionmaker, settings
) -> None:
    """Фильтр снял ответ посреди записи — бот сам спрашивает секцию нумерованным списком."""
    llm = FakeLLMClient([
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Айназаров Али", "child_age": 8, "gym_id": MKR6, "parent_agreed": True,
        })),
        FakeTurn.answer("Записал Али на вторник в 21:15."),
    ])
    deps = await _deps(kb, state, sessionmaker, settings, llm)

    decisions = await process_inbound(
        deps, webhook_payload("fb-1", "Хочу записать Айназарова Али, 8 лет, в 6-й микрорайон", chat_id="77015558202")
    )

    assert [d.action.value for d in decisions] == ["reply"], [d.reason for d in decisions]
    client = "\n".join(out.text or "" for d in decisions for out in d.outbound)
    assert kb.text("funnel.discipline", Language.RU) in client, client
    assert "1. " in client and "2. " in client and "21:15" not in client
    assert not [card for d in decisions for card in d.manager_cards]
    assert kb.text("escalation.handoff", Language.RU) not in client


async def test_digit_answers_the_latest_list_not_the_older_gym_list(kb, state, sessionmaker, settings) -> None:
    """После списка залов бот спросил секцию — «2» это секция, а не зал номер два."""
    llm = FakeLLMClient([])
    deps = await _deps(kb, state, sessionmaker, settings, llm)
    chat = "77015558203"

    await process_inbound(deps, webhook_payload(f"{chat}-1", "Здравствуйте", chat_id=chat))
    await process_inbound(deps, webhook_payload(f"{chat}-2", "2", chat_id=chat))
    llm.reset([FakeTurn.answer("На какую секцию записать?\n1. Бокс\n2. Кикбоксинг")])
    await process_inbound(deps, webhook_payload(f"{chat}-3", "Хочу записать сына", chat_id=chat))
    llm.reset([FakeTurn.answer("Отлично.")])
    await process_inbound(deps, webhook_payload(f"{chat}-4", "2", chat_id=chat))

    request = [item for item in llm.requests if item.user_text][-1]
    assert "выбор из вариантов бота: «Кикбоксинг»" in request.user_text, request.user_text
    assert "Расскажите про зал" not in request.user_text


def test_option_list_is_not_stripped_as_a_repeat_of_the_schedule_card(kb) -> None:
    """Расписание ушло в этом же ходу — строки вариантов всё равно остаются в ответе."""
    from app.core.reply_dedup import strip_card_repeats
    from app.kb.render import render_schedule_card

    gym = kb.gym(MKR6)
    card = render_schedule_card(kb, gym_id=MKR6, slots=gym.schedule, lang=Language.RU)

    cleaned = strip_card_repeats(SECTION_LIST, [card])

    assert "— Кикбоксинг — Вт, Чт, Сб 19:00" in cleaned and "— Бокс — Вт, Чт, Сб 19:00" in cleaned, cleaned


#: Дословно из живого прогона 12.09.2026: список без вопросительного знака.
TIME_LIST_WITHOUT_QUESTION = (
    "Выберите подходящее время для пробного занятия:\n"
    "— Кикбоксинг — Пн, Ср, Пт · 09:00\n"
    "— Кикбоксинг — Пн, Ср, Пт · 17:00\n"
    "— Кикбоксинг — Пн, Ср, Пт · 19:00\n"
    "— Бокс — Вт, Чт, Сб · 09:00\n"
    "— Бокс и кикбоксинг — Вт, Чт, Сб · 17:00\n"
    "Напишите номер или своими словами."
)


async def test_digit_after_a_request_to_choose_without_a_question_mark(kb) -> None:
    """«Выберите …: — … Напишите номер» — «2» тоже выбор, хотя вопросительного знака нет."""
    from app.tools.booking import create_trial_lead

    chosen = with_choice("2", TIME_LIST_WITHOUT_QUESTION)
    assert chosen_option(chosen) == "Кикбоксинг — Пн, Ср, Пт · 17:00", chosen
    assert with_choice("2", "Вот расписание:\n— Бокс\n— Кикбоксинг") == "2", "без вопроса и просьбы выбрать — не выбор"

    ctx = _ctx(kb, "Сериков Ержан, 8 лет", chosen)
    result = await create_trial_lead(
        ctx, child_name="Сериков Ержан", child_age=8, gym_id="ksk_kairbekova_334", parent_agreed=True,
        discipline="kickboxing", session_time="17:00",
    )
    assert result.data["booked"] is True, result.data

