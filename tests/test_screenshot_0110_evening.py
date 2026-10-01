"""Голосовые и скриншоты владельца 01.10.2026, вечер (диалог 18:15–18:28).

1. «Повторно просит ФИО ребёнка — я в самом начале писал».
2. «Не минусики, а поочерёдность: 1, 2, 3, 4 — и прямо так и написать».
3. «Прийти пораньше» дважды — вместо второго подбодрить ребёнка.
4. «Мне не подходит ни один зал — в конце спросить, в каком районе я живу».
6. «Бот не предложил: напишите свой микрорайон — я написал КЖБИ наугад».
7. Приветствие: «я бот, работаю 24/7, быстро запишу без менеджера».
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.kb.agreement import numbered_choices, with_choice
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
from app.storage.models import OutboxMessage
from app.types import Language, OutboundKind

from tests.conftest import RecordingQueue, webhook_payload

CHAT = "77015550118"
VOINOV = "magazin15_voinov_8b"
FOOTER = "Напишите только цифру: {numbers}"


# --------------------------------------------------------------------------- #
# Варианты под номерами
# --------------------------------------------------------------------------- #
def test_dash_choices_become_numbered_with_plain_digits() -> None:
    text = (
        "Какое время и направление подойдут для Али?\n\n"
        "— Бокс — Вт, Чт, Сб 09:00\n— Бокс — Вт, Чт, Сб 18:45\n"
        "— Кикбоксинг — Вт, Чт, Сб 09:00\n— Кикбоксинг — Вт, Чт, Сб 18:45\n\n"
        "Напишите цифру или своими словами."
    )

    result = numbered_choices(text, FOOTER)

    assert "1. Бокс — Вт, Чт, Сб 09:00" in result and "4. Кикбоксинг — Вт, Чт, Сб 18:45" in result
    assert "— Бокс" not in result and "своими словами" not in result
    assert result.endswith("Напишите только цифру: 1, 2, 3, 4")


def test_a_list_without_a_request_to_choose_is_left_alone() -> None:
    """Рассказ о зале списком и вопрос «Записать?» — не выбор из вариантов."""
    text = "В зале есть:\n— раздевалка\n— душ\n\nЗаписать ребёнка на пробное?"

    assert numbered_choices(text, FOOTER) == text


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("9", "Бокс — Вт, Чт, Сб 09:00"),        # «не обязательно же писать 09:00»
        ("9.", "Бокс — Вт, Чт, Сб 09:00"),
        ("2", "Бокс — Вт, Чт, Сб 18:45"),        # номер варианта — по-прежнему номер
        ("19", None),                             # такого часа в списке нет
    ],
)
def test_hour_answers_the_list_when_it_is_unambiguous(answer, expected) -> None:
    bot = "Какое время удобнее?\n1. Бокс — Вт, Чт, Сб 09:00\n2. Бокс — Вт, Чт, Сб 18:45\nНапишите цифру"

    expanded = with_choice(answer, bot)

    assert (expected in expanded) if expected else expanded == answer


def test_hour_shared_by_two_sections_is_not_guessed() -> None:
    bot = ("Какое время удобнее?\n1. Бокс — Вт, Чт, Сб 09:00\n"
           "2. Кикбоксинг — Вт, Чт, Сб 09:00\nНапишите цифру")

    assert with_choice("9", bot) == "9"


# --------------------------------------------------------------------------- #
# Карточка залов и приветствие
# --------------------------------------------------------------------------- #
def test_gym_list_marks_the_main_gym_and_invites_a_district(kb) -> None:
    from app.kb.render import render_gyms_list_card
    from app.types import Scope

    card = render_gyms_list_card(kb, scope=Scope.CITY, lang=Language.RU)

    assert card.count("основной зал") == 1
    ksk_block = next(block for block in card.split("\n\n") if "КСК" in block)
    assert "старший тренер" in ksk_block
    assert card.rstrip().endswith("в каком районе вы живёте?")


# --------------------------------------------------------------------------- #
# Диалог со скриншота целиком
# --------------------------------------------------------------------------- #
@pytest.fixture
async def deps(kb, state, sessionmaker, settings) -> PipelineDeps:
    kb_loader.swap(kb)
    return PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=FakeLLMClient([]),
        kb=kb_loader.get_snapshot, queue=RecordingQueue(), settings=settings,
    )


async def say(deps, number: int, text: str, *turns: FakeTurn) -> list[str]:
    """Реплика клиента; возвращает то, что бот ответил на неё."""
    before = len(await to_client(deps))
    deps.llm.reset(list(turns))
    await process_inbound(deps, webhook_payload(f"s0110e-{number}", text, chat_id=CHAT))
    return (await to_client(deps))[before:]


async def to_client(deps) -> list[str]:
    async with deps.sessionmaker() as db:
        payloads = (await db.execute(sa.select(OutboxMessage.payload))).scalars().all()
    return [
        str(p.get("text") or "") for p in payloads
        if p and p.get("chat_id") == CHAT and p.get("kind") != OutboundKind.MANAGER_CARD.value
    ]


DASH_LIST = (
    "Какое время и направление подойдут для Али?\n\n"
    "— Бокс — Вт, Чт, Сб 09:00\n— Бокс — Вт, Чт, Сб 18:45\n"
    "— Кикбоксинг — Вт, Чт, Сб 09:00\n— Кикбоксинг — Вт, Чт, Сб 18:45\n\n"
    "Напишите цифру или своими словами."
)


async def test_owner_dialog_books_without_asking_the_name_twice(deps, kb) -> None:
    greeting = await say(deps, 1, "Здравствуйте")
    assert "24/7" in "".join(greeting)

    await say(deps, 2, "1", FakeTurn.answer("Запишу на бесплатное пробное — подскажите фамилию и имя ребёнка?"))
    await say(deps, 3, "Айназаров Али", FakeTurn.answer("Сколько лет ребёнку?"))

    after_age = await say(deps, 4, "8")
    assert after_age == [kb.text("funnel.district", Language.RU)], "после возраста — вопрос о районе, кодом"

    await say(deps, 5, "Кжби", FakeTurn.answer("Рядом 15-й магазин и 6-й микрорайон. Какой из них вам ближе?"))
    await say(deps, 6, "а форма нужна?", FakeTurn.answer("Нет, достаточно удобной одежды. Какой зал вам ближе?"))
    await say(deps, 7, "15 магазин", FakeTurn.answer("Записать ребёнка на первую пробную тренировку?"))

    options = "\n".join(await say(deps, 8, "Да", FakeTurn.tool(FakeCall("create_trial_lead", {
        "child_name": "Айназаров Али", "child_age": 8, "gym_id": VOINOV, "parent_agreed": True,
    })), FakeTurn.answer(DASH_LIST)))
    assert "1. Бокс — Вт, Чт, Сб 09:00" in options, options
    assert "— Бокс" not in options
    assert "Напишите только цифру: 1, 2, 3, 4" in options

    booked = "\n".join(await say(deps, 9, "1", FakeTurn.tool(FakeCall("create_trial_lead", {
        "child_name": "Айназаров Али", "child_age": 8, "gym_id": VOINOV, "parent_agreed": True,
        "session_time": "09:00", "discipline": "boxing",
    })), FakeTurn.answer("Готово")))
    assert "Мы записали вас" in booked, booked
    assert "фамилию и имя" not in booked, "имя назвали в начале — второй раз не спрашиваем"


async def test_district_is_not_asked_when_the_place_is_already_named(deps, kb) -> None:
    """Клиент сам назвал КЖБИ — на «8» вопрос о районе был бы повтором; ведёт модель."""
    await say(deps, 1, "Хочу записать сына, мы на КЖБИ", FakeTurn.answer("Сколько лет ребёнку?"))

    reply = await say(deps, 2, "8", FakeTurn.answer("Рядом 15-й магазин и 6-й микрорайон. Какой ближе?"))

    assert reply and kb.text("funnel.district", Language.RU) not in reply


async def test_time_options_come_from_the_code_even_without_a_list_from_the_model(deps) -> None:
    """Модель спросила «Какое время удобнее?» без вариантов — варианты добавляет код."""
    reply = "\n".join(await say(deps, 1, "Запишите Айназарова Али, 8 лет, в зал у 15-го магазина",
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Айназаров Али", "child_age": 8, "gym_id": VOINOV, "parent_agreed": True,
        })),
        FakeTurn.answer("Какое время вам удобнее?")))

    assert "1. Бокс — Вт, Чт, Сб 09:00" in reply, reply
    assert reply.rstrip().endswith("Напишите только цифру: 1, 2, 3, 4")


async def test_models_own_dash_list_is_numbered(deps) -> None:
    """Список без инструмента записи — тоже под номерами и с цифрами в конце."""
    reply = "\n".join(await say(deps, 1, "Какие у вас секции?", FakeTurn.answer(
        "Выберите секцию:\n— Бокс\n— Кикбоксинг\n\nНапишите цифру или своими словами."
    )))

    assert "1. Бокс" in reply and "2. Кикбоксинг" in reply and "— Бокс" not in reply, reply
    assert reply.rstrip().endswith("Напишите только цифру: 1, 2")


async def test_the_city_itself_is_not_a_district(deps, kb) -> None:
    """«Мы в городе Костанай» не говорит, какой зал ближе, — вопрос о районе нужен."""
    await say(deps, 1, "Хочу записать сына, мы в городе Костанай", FakeTurn.answer("Сколько лет ребёнку?"))

    assert await say(deps, 2, "8") == [kb.text("funnel.district", Language.RU)]


# --------------------------------------------------------------------------- #
# Живой прогон того же вечера: «9» → «выберите из списка выше» → «1» → ложное «записан»
# --------------------------------------------------------------------------- #
async def _time_list(deps) -> None:
    """Вопрос о времени, как в жизни: варианты — от инструмента записи, список собирает код."""
    await say(deps, 1, "Запишите Айназарова Али, 8 лет, в зал у 15-го магазина", FakeTurn.tool(
        FakeCall("create_trial_lead", {
            "child_name": "Айназаров Али", "child_age": 8, "gym_id": VOINOV, "parent_agreed": True,
        })
    ), FakeTurn.answer("Какое время вам удобнее?"))


async def test_hour_shared_by_two_sections_narrows_the_list(deps, kb) -> None:
    """«9» — это 09:00; в 09:00 и бокс, и кикбоксинг: код спрашивает секцию с номерами."""
    await _time_list(deps)

    reply = await say(deps, 2, "9")   # модель не вызывается: её сценарий пуст

    assert reply == [
        f"{kb.text('funnel.discipline', Language.RU)}\n\n"
        "1. Бокс — Вт, Чт, Сб 09:00\n2. Кикбоксинг — Вт, Чт, Сб 09:00\n\n"
        "Напишите только цифру: 1, 2"
    ], reply
    await say(deps, 3, "1", FakeTurn.answer("Отлично!"))
    assert "«Бокс — Вт, Чт, Сб 09:00»" in deps.llm.requests[-1].user_text, "«1» — бокс из сужённого списка"


async def test_digit_after_choose_from_the_list_above_counts_as_a_choice(deps) -> None:
    await _time_list(deps)
    await say(deps, 2, "не знаю", FakeTurn.answer("Выберите, пожалуйста, один из вариантов из списка выше."))

    await say(deps, 3, "3", FakeTurn.answer("Отлично!"))

    assert "«Кикбоксинг — Вт, Чт, Сб 09:00»" in deps.llm.requests[-1].user_text


def test_lead_in_never_carries_a_booking_claim() -> None:
    from app.core.pipeline import _lead_in

    reply = "В 17:30 занятий нет. Али записан на бокс в 09:00. Какое время выбрать?"

    assert _lead_in(reply) == "В 17:30 занятий нет."


# --------------------------------------------------------------------------- #
# Перепроверка: вопрос о районе не повторяет уже выбранный зал
# --------------------------------------------------------------------------- #
KSK = "ksk_kairbekova_334"


async def test_age_after_a_gym_picked_by_number_is_neither_a_gym_nor_a_district_question(deps, kb) -> None:
    """«2» → список залов → «3» (КСК) → «Сколько лет?» → «8».

    «8» — ответ на вопрос о возрасте, а не зал №8 (Тобыл) из списка двумя сообщениями
    выше. И вопрос о районе не нужен: зал выбран цифрой — это служебная заметка, а не
    слова клиента, и заявки на этом шаге ещё нет.
    """
    await say(deps, 1, "Здравствуйте")
    await say(deps, 2, "2")
    await say(deps, 3, "3", FakeTurn.answer("Сколько лет ребёнку?"))

    reply = await say(deps, 4, "8", FakeTurn.answer("Отлично! Записать на пробное в КСК?"))

    assert deps.llm.requests[-1].user_text == "8", "возраст ушёл модели как есть, а не как зал №8"
    assert kb.text("funnel.district", Language.RU) not in "\n".join(reply), reply


async def test_no_district_question_after_a_gym_schedule_was_sent(deps, kb) -> None:
    await say(deps, 1, "Какое у вас расписание?", FakeTurn.tool(
        FakeCall("get_schedule", {"gym_id": KSK})
    ), FakeTurn.answer("Подходит вам такое время?"))
    await say(deps, 2, "да, подходит", FakeTurn.answer("Сколько лет ребёнку?"))

    reply = await say(deps, 3, "8", FakeTurn.answer("Отлично! Записать на пробное?"))

    assert kb.text("funnel.district", Language.RU) not in "\n".join(reply), reply


async def test_district_check_failure_leaves_the_turn_to_the_model(deps, kb, monkeypatch) -> None:
    """Не прочли, уходили ли карточки зала, — не рискуем: вопрос задаёт модель."""
    from app.storage import repo_message

    async def broken(*_args, **_kwargs):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(repo_message, "any_artifact_sent", broken)
    await say(deps, 1, "Хочу записать сына", FakeTurn.answer("Сколько лет ребёнку?"))

    reply = await say(deps, 2, "8", FakeTurn.answer("Отлично! Где вам удобнее заниматься?"))

    assert reply and kb.text("funnel.district", Language.RU) not in reply


def test_instruction_steps_are_not_a_choice() -> None:
    text = "Чтобы записаться:\n1. Выберите зал\n2. Напишите удобное время\n\nЖду ваш ответ."

    assert numbered_choices(text, FOOTER) == text


async def test_hour_shared_by_different_days_asks_for_the_option_not_the_section(deps, kb) -> None:
    """«17» в КСК: бокс и кикбоксинг в двух наборах дней — выбирают вариант, а не секцию."""
    await say(deps, 1, "Запишите Айназарова Али, 8 лет, в КСК", FakeTurn.tool(
        FakeCall("create_trial_lead", {
            "child_name": "Айназаров Али", "child_age": 8, "gym_id": KSK, "parent_agreed": True,
        })
    ), FakeTurn.answer("Какое время вам удобнее?"))

    reply = "\n".join(await say(deps, 2, "17"))

    assert reply.startswith(kb.text("funnel.pick_option", Language.RU)), reply
    assert reply.count("17:00") == 3 and reply.rstrip().endswith("Напишите только цифру: 1, 2, 3")



async def test_model_is_told_which_gym_the_client_is_looking_at(deps) -> None:
    """После расписания КСК модель знает зал, даже когда выбор выпал из окна истории."""
    await say(deps, 1, "Какое у вас расписание?", FakeTurn.tool(
        FakeCall("get_schedule", {"gym_id": KSK})
    ), FakeTurn.answer("Подходит вам такое время?"))

    await say(deps, 2, "да", FakeTurn.answer("Сколько лет ребёнку?"))

    assert f"выбранный зал: {KSK}" in deps.llm.requests[-1].dynamic_note
