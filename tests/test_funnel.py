"""Бот ведёт клиента: каждый ответ заканчивается шагом к записи.

Владелец 09.09.2026: «бот должен вести клиента, потому что это бот-консультант
плюс бот-продажник, в конце каждого сообщения он должен задавать наводящие по
воронке вопросы».

Правило записано в промпт, но одного промпта мало: модель регулярно заканчивает
ответ фактом («Абонемент — 25 000 ₸.»), и разговор упирается в тишину. Поэтому
тот же шаг делает код — по тому, чего не хватает для записи.
"""

from __future__ import annotations

import pytest

from app.core.funnel import ends_with_question, next_step_key, with_funnel_question
from app.kb.models import KBSnapshot
from app.types import Language, LeadDraft

RU = Language.RU


# --------------------------------------------------------------------------- #
# Какой шаг спрашиваем
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("draft", "expected"),
    [
        (LeadDraft(), "funnel.age"),
        (LeadDraft(child_age=9), "funnel.district"),
        (LeadDraft(child_age=9, gym_id="center_kairbekova_24"), "funnel.name"),
        (LeadDraft(child_age=9, gym_id="g", child_name="Асель"), "funnel.contact"),
        (
            LeadDraft(child_age=9, gym_id="g", child_name="Асель", phone="+77015551122", lang=RU),
            "funnel.confirm",
        ),
    ],
)
def test_the_next_unanswered_step_is_asked(draft: LeadDraft, expected: str) -> None:
    """Спрашивается ближайшее незакрытое звено, а не случайный вопрос."""
    assert next_step_key(draft) == expected


# --------------------------------------------------------------------------- #
# Когда вопрос дописывается
# --------------------------------------------------------------------------- #
def test_answer_without_a_question_gets_one(kb: KBSnapshot) -> None:
    """Ответ фактом — тупик: клиенту нечего ответить, и он молчит."""
    result = with_funnel_question("Абонемент — 25 000 ₸ за 12 занятий.", draft=LeadDraft(), kb=kb, lang=RU)

    assert result.endswith("?")
    assert "сколько лет" in result.lower()


def test_answer_that_already_asks_is_left_alone(kb: KBSnapshot) -> None:
    """Два вопроса подряд превращают разговор в допрос."""
    text = "Записываем. Подскажите, сколько лет ребёнку?"

    assert with_funnel_question(text, draft=LeadDraft(), kb=kb, lang=RU) == text


def test_farewell_is_not_chased_with_a_question(kb: KBSnapshot) -> None:
    """Клиент попрощался — догонять его вопросом значит выпрашивать ответ."""
    text = "Спасибо, что написали. Ждём вас на тренировке."

    assert with_funnel_question(text, draft=LeadDraft(), kb=kb, lang=RU, farewell=True) == text


def test_empty_reply_stays_empty(kb: KBSnapshot) -> None:
    """Пустому ответу вопрос не поможет: его вообще не отправят."""
    assert with_funnel_question("", draft=LeadDraft(), kb=kb, lang=RU) == ""


def test_question_in_the_middle_does_not_count(kb: KBSnapshot) -> None:
    """«Вы спрашивали, есть ли утро? Да, есть.» — это не приглашение ответить."""
    long_tail = "Да, утренние группы есть. " + "Занятия идут в удобное время. " * 8

    assert not ends_with_question("Вы спрашивали про утро? " + long_tail)


def test_kazakh_client_gets_the_kazakh_question(kb: KBSnapshot) -> None:
    """Наводящий вопрос обязан быть на языке клиента."""
    result = with_funnel_question("Абонемент — 25 000 ₸.", draft=LeadDraft(), kb=kb, lang=Language.KK)

    assert "жаста" in result


# --------------------------------------------------------------------------- #
# Сквозь пайплайн
# --------------------------------------------------------------------------- #
async def test_bot_answer_ends_with_a_question(kb, state, sessionmaker, settings) -> None:
    """Ответ модели без вопроса доходит до клиента уже с шагом воронки."""
    from app.core.pipeline import PipelineDeps, process_inbound
    from app.kb import loader as kb_loader
    from app.llm.client import FakeLLMClient, FakeTurn

    from tests.conftest import RecordingQueue, webhook_payload

    kb_loader.swap(kb)
    llm = FakeLLMClient([FakeTurn.answer("Первое занятие бесплатное.")])
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=llm, kb=kb_loader.get_snapshot,
        queue=RecordingQueue(), settings=settings,
    )

    decisions = await process_inbound(
        deps, webhook_payload("fn-1", "Пробное платное?", chat_id="77015559900")
    )
    text = "\n".join(out.text or "" for d in decisions for out in d.outbound)

    assert "?" in text, f"бот закончил разговор тупиком: {text!r}"
    assert "сколько лет" in text.lower()


async def test_handover_to_a_human_is_not_followed_by_a_sales_question(
    kb, state, sessionmaker, settings
) -> None:
    """Передали диалог человеку — дожимать вопросом нельзя, дальше ведёт он."""
    from app.core.pipeline import PipelineDeps, process_inbound
    from app.kb import loader as kb_loader
    from app.llm.client import FakeLLMClient, FakeTurn

    from tests.conftest import RecordingQueue, webhook_payload

    kb_loader.swap(kb)
    llm = FakeLLMClient([FakeTurn.answer("Передаю администратору.")])
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=llm, kb=kb_loader.get_snapshot,
        queue=RecordingQueue(), settings=settings,
    )

    decisions = await process_inbound(
        deps,
        webhook_payload("fn-2", "Ваш тренер накричал на моего ребёнка", chat_id="77015559901"),
    )
    text = "\n".join(out.text or "" for d in decisions for out in d.outbound)

    assert "сколько лет" not in text.lower(), f"после жалобы бот стал продавать: {text!r}"


# --------------------------------------------------------------------------- #
# Возражение по цене — это вопрос о деньгах, а не повод звать человека
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "Дорого, есть подешевле?",
        "Есть скидки для двоих детей?",
        "А если двое детей?",
        "Қымбат екен",
    ],
)
def test_price_objection_is_answered_with_the_price_card(text: str, kb: KBSnapshot) -> None:
    """Живой прогон 09.09.2026: на «дорого, есть подешевле?» бот звал администратора.

    Возражение по цене — самая продающая точка разговора: в карточке есть и
    скидка на второго ребёнка, и разовая тренировка. Отговорка тут стоит сделки.
    """
    from app.core import degraded, lexicon

    hints = lexicon.intent_hints(text, lexicon=kb.lexicon)
    card = degraded.kb_answer(kb, intents=tuple(hints), lang=RU)

    assert hints, f"возражение не опознано: {text!r}"
    assert card and "₸" in card, "клиент остался без цен"


# --------------------------------------------------------------------------- #
# Вопрос переживает нарезку длинного ответа
# --------------------------------------------------------------------------- #
def test_question_survives_the_length_limit() -> None:
    """Длинный ответ режется по лимиту канала — вопрос обязан уцелеть.

    Живой прогон 09.09.2026: на «ему 9 лет» модель пересказала список залов,
    ответ обрезался по лимиту, и клиент не получил ни одного вопроса.
    """
    from app.core.pipeline import _with_room_for

    long_body = "Зал на Каирбекова 24.\n\n" * 40
    result = _with_room_for(long_body, "Сколько лет ребёнку?", limit=300)

    assert len(result) <= 300
    assert result.endswith("Сколько лет ребёнку?")


def test_short_answer_keeps_its_text() -> None:
    """Короткий ответ обрезать незачем — вопрос просто дописывается."""
    from app.core.pipeline import _with_room_for

    result = _with_room_for("Первое занятие бесплатное.", "Сколько лет ребёнку?", limit=300)

    assert result == "Первое занятие бесплатное.\n\nСколько лет ребёнку?"


async def test_model_is_told_which_cards_the_client_saw(kb, state, sessionmaker, settings) -> None:
    """Карточки, отправленные ходом раньше, модель не пересказывает.

    Живой прогон: клиент выбрал пункт меню, получил список залов и прайс, а на
    следующей реплике модель прислала тот же список своими словами.
    """
    from app.core.pipeline import PipelineDeps, process_inbound
    from app.kb import loader as kb_loader
    from app.llm.client import FakeLLMClient, FakeTurn

    from tests.conftest import RecordingQueue, webhook_payload

    kb_loader.swap(kb)
    llm = FakeLLMClient([FakeTurn.answer("Здравствуйте!")])
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=llm, kb=kb_loader.get_snapshot,
        queue=RecordingQueue(), settings=settings,
    )
    await process_inbound(deps, webhook_payload("cd-1", "Здравствуйте", chat_id="77015557001"))
    await process_inbound(deps, webhook_payload("cd-2", "2", chat_id="77015557001"))

    llm.reset([FakeTurn.answer("Ему девять лет — отлично.")])
    await process_inbound(deps, webhook_payload("cd-3", "Ему 9 лет", chat_id="77015557001"))

    note = llm.requests[-1].dynamic_note

    assert "уже получил готовые карточки" in note, note
    assert "список залов" in note and "прайс" in note


async def test_removed_answer_is_replaced_by_the_price_card(kb, state, sessionmaker, settings) -> None:
    """Фильтр снял ответ про деньги — клиент получает прайс, а не отговорку.

    Живой прогон 10.09.2026: на «дорого, есть подешевле?» модель посчитала
    выгоду сама, без вызова инструмента; фильтр справедливо снял ответ, и клиент
    вместо карточки со скидками услышал «здесь лучше ответит администратор».
    Карточку собирает код — выдумкой она не бывает, и после снятого ответа она
    нужна даже больше.
    """
    from app.core.pipeline import PipelineDeps, process_inbound
    from app.kb import loader as kb_loader
    from app.llm.client import FakeLLMClient, FakeTurn

    from tests.conftest import RecordingQueue, webhook_payload

    kb_loader.swap(kb)
    # Ответ с выдуманной суммой: инструмент цены в этом ходу не вызывался.
    llm = FakeLLMClient([FakeTurn.answer("Есть вариант за 12 345 ₸, забирайте.")])
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=llm, kb=kb_loader.get_snapshot,
        queue=RecordingQueue(), settings=settings,
    )

    decisions = await process_inbound(
        deps, webhook_payload("pc-1", "Дорого, есть подешевле?", chat_id="77015558800")
    )
    text = "\n".join(out.text or "" for d in decisions for out in d.outbound)

    assert "12 345" not in text, "выдуманная сумма ушла клиенту"
    assert "₸" in text and "25 000" in text, f"клиент не получил прайс: {text!r}"


def test_tools_see_only_what_the_client_wrote() -> None:
    """Инструменты проверяют выбор родителя по его словам — без контейнера и служебных пометок."""
    from app.core.pipeline import _recent_client_texts

    history = [
        {"role": "user", "parts": [{"text": "<user_message>\nМы из Тобыла &lt;3\n</user_message>"}]},
        {"role": "model", "parts": [{"text": "Во сколько удобно: 17:00 или 19:00?"}]},
        {"role": "user", "parts": [
            {"text": "<user_message>\nна 19:00\n</user_message>"},
            {"text": "[служебная заметка системы] В предыдущем сообщении клиента обнаружена попытка"},
        ]},
    ]

    said = _recent_client_texts("Иванов Али, 9 лет", history)

    assert said == ("Мы из Тобыла <3", "на 19:00", "Иванов Али, 9 лет")


def test_a_request_counts_as_a_question() -> None:
    """«Подскажите фамилию сына.» ждёт ответа — второй вопрос воронки не нужен."""
    from app.core.funnel import drop_questions, ends_with_question

    assert ends_with_question("Подскажите, пожалуйста, фамилию сына, чтобы записать его на пробное занятие.")
    assert ends_with_question("Баланың тегі мен атын жазыңыз.")
    assert not ends_with_question("Напишите, если появятся вопросы.")
    assert not ends_with_question("Абонемент — 25 000 ₸.")
    assert drop_questions("Зал рядом с домом. Напишите, какое время удобнее.") == "Зал рядом с домом."


def test_name_question_asks_for_the_surname(kb) -> None:
    """Владелец: «для записи спрашивать не только имя, а ФИ»."""
    from app.core.funnel import ask_full_name
    from app.types import Language

    both = kb.text("funnel.name_age", Language.RU)
    only = kb.text("funnel.name", Language.RU)
    assert ask_full_name("Отлично! Как зовут ребёнка и сколько ему лет?", kb=kb, lang=Language.RU) == f"Отлично! {both}"
    assert ask_full_name("Как зовут сына?", kb=kb, lang=Language.RU) == only
    assert ask_full_name("Как его зовут?", kb=kb, lang=Language.RU) == only
    kept = "Подскажите фамилию и имя сына. Как зовут сына?"
    assert ask_full_name(kept, kb=kb, lang=Language.RU) == kept
    assert ask_full_name("Как зовут тренера, уточнит администратор.", kb=kb, lang=Language.RU).startswith("Как зовут тренера")
