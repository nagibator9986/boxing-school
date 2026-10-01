"""Скриншоты владельца 01.10.2026: запись с возрастом цифрой и ложное «Записал…».

Родитель ответил «8» на «сколько лет ребёнку?», выбрал КСК, бокс, 17:00, написал
фамилию и имя — и получил «Записал Айназарова Али… на вторник, 6 октября, в 17:00»,
а следом «Подберу подходящую группу — сколько лет ребёнку?». Записи при этом не было:
«8» выпало из окна последних реплик, инструмент ответил «нужен возраст», а модель
написала своё.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.core.pipeline import _BOOKING_CLAIM_RE, PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.kb.sessions import age_answer
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
from app.storage.models import OutboxMessage
from app.types import Language, OutboundKind

from tests.conftest import RecordingQueue, webhook_payload

CHAT = "77015550110"
KSK = "ksk_kairbekova_334"
AGE_QUESTION = "Подберу подходящую группу — сколько лет ребёнку?"


# --------------------------------------------------------------------------- #
# Возраст цифрой
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "asked", "expected"),
    [
        ("8", "Отлично! Подскажите, пожалуйста, сколько лет ребёнку?", 8),
        ("ему 8", AGE_QUESTION, 8),
        ("восемь", "Сколько вашему ребёнку лет?", 8),
        ("8", "Напишите фамилию, имя и возраст ребёнка.\n\nА возраст?", 8),
        ("8", "Балаңыз неше жаста?", 8),
        # Без вопроса о возрасте цифра — что угодно, только не возраст.
        ("8", "Подскажите, что вам ближе:\n1. Запись\n2. Цены\nНапишите цифру", None),
        ("17", "Какое время из расписания вам больше подходит?", None),
        ("8", "В каком районе вам удобнее?", None),
        ("1", "Сколько лет ребёнку?", None),
        ("8 и 10", "Сколько лет детям?", None),
        # Бот спросил возраст и дал варианты под номерами: «2» — номер варианта.
        ("2", "Сколько лет ребёнку?\n1. 5–7 лет\n2. 8–10 лет\nНапишите цифру", None),
    ],
)
def test_bare_number_is_an_age_only_after_the_age_question(text, asked, expected) -> None:
    assert age_answer(text, asked) == expected


@pytest.mark.parametrize(
    "claim",
    ["Записал Айназарова Али на пробное", "Мы записали вас!", "Вы записаны на вторник",
     "Али записан на бокс", "Балаңызды жаздым", "Сынаққа жазылдыңыз"],
)
def test_booking_claims_are_recognized(claim) -> None:
    assert _BOOKING_CLAIM_RE.search(claim)


@pytest.mark.parametrize(
    "text",
    ["Запишу на бесплатное пробное", "Хотите записаться?", "Я ещё не записал ребёнка",
     "Записать ребёнка на пробное?", "Вы не записаны"],
)
def test_offers_and_negations_are_not_claims(text) -> None:
    assert not _BOOKING_CLAIM_RE.search(text)


def test_emoji_only_where_the_owner_sent_them() -> None:
    """Запрет эмодзи остаётся для всей базы; снимается только у текста владельца."""
    from pydantic import ValidationError

    from app.kb.models import FaqEntry

    entry = {"id": "x", "topic": "gear", "answer": {"ru": "👕 форма", "kk": "👕 киім"}}
    with pytest.raises(ValidationError):
        FaqEntry(**entry)
    assert FaqEntry(**entry, emoji_ok=True).answered


# --------------------------------------------------------------------------- #
# Сквозной прогон скриншота
# --------------------------------------------------------------------------- #
@pytest.fixture
async def deps(kb, state, sessionmaker, settings) -> PipelineDeps:
    kb_loader.swap(kb)
    return PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=FakeLLMClient([]),
        kb=kb_loader.get_snapshot, queue=RecordingQueue(), settings=settings,
    )


async def say(deps, number: int, text: str, *turns: FakeTurn) -> None:
    deps.llm.reset(list(turns))
    await process_inbound(deps, webhook_payload(f"s0110-{number}", text, chat_id=CHAT))


async def to_client(deps) -> list[str]:
    async with deps.sessionmaker() as db:
        payloads = (await db.execute(sa.select(OutboxMessage.payload))).scalars().all()
    return [
        str(p.get("text") or "") for p in payloads
        if p and p.get("chat_id") == CHAT and p.get("kind") != OutboundKind.MANAGER_CARD.value
    ]


BOOK_KSK_BOXING_17 = FakeCall("create_trial_lead", {
    "child_name": "Айназаров Али", "gym_id": KSK, "parent_agreed": True,
    "session_time": "17:00", "discipline": "boxing",
})
FALSE_CLAIM = "Записал Айназарова Али на пробное занятие по боксу на КСК в 17:00."


async def _dialog_up_to_the_name(deps, *, age_given: bool) -> None:
    """Скриншот: возраст цифрой в начале, потом шесть реплик о зале, секции и времени."""
    await say(deps, 1, "Хочу записать сына на пробное", FakeTurn.answer(AGE_QUESTION))
    if age_given:
        await say(deps, 2, "8", FakeTurn.answer("В каком районе вам удобнее?"))
    await say(deps, 3, "КСК", FakeTurn.answer("Записать на бокс или на кикбоксинг?"))
    await say(deps, 4, "а форма нужна?", FakeTurn.answer("Нет, приходите в удобной одежде. Какую секцию выбираете?"))
    await say(deps, 5, "понятно", FakeTurn.answer("Какую секцию выбираете?"))
    await say(deps, 6, "Бокс", FakeTurn.answer("Утром или вечером удобнее?"))
    await say(deps, 7, "вечером", FakeTurn.answer("Во сколько удобнее?"))
    await say(deps, 8, "17.00", FakeTurn.answer("Подскажите фамилию и имя ребёнка?"))


async def test_age_said_early_still_completes_the_booking(deps) -> None:
    """«8» ушло из окна последних реплик, но возраст остался в заявке — запись состоялась."""
    await _dialog_up_to_the_name(deps, age_given=True)

    await say(deps, 9, "Айназаров Али", FakeTurn.tool(BOOK_KSK_BOXING_17), FakeTurn.answer(FALSE_CLAIM))

    texts = "\n".join(await to_client(deps))
    assert "Мы записали вас на бесплатное пробное занятие" in texts, texts
    assert "👕 удобную спортивную одежду" in texts, "в подтверждении — текст владельца о том, что взять"
    assert "сколько лет ребёнку" not in texts.split("Записал")[-1].split("Мы записали вас")[-1]


async def test_no_false_booking_claim_when_the_booking_did_not_happen(deps) -> None:
    """Возраста нет нигде: инструмент не записал — клиент получает вопрос, а не «Записал»."""
    await _dialog_up_to_the_name(deps, age_given=False)
    before = len(await to_client(deps))

    await say(deps, 9, "Айназаров Али", FakeTurn.tool(BOOK_KSK_BOXING_17), FakeTurn.answer(FALSE_CLAIM))

    last = "\n".join((await to_client(deps))[before:])
    assert "Записал" not in last, last
    assert "сколько лет" in last, last


async def test_booked_client_may_hear_that_they_are_booked(deps) -> None:
    """Запись уже есть — «Вы записаны» правда, и её не подменяют вопросом."""
    await say(deps, 1, "Запишите Айназарова Али, 8 лет, на бокс в КСК в 17:00", FakeTurn.tool(
        FakeCall("create_trial_lead", {
            "child_name": "Айназаров Али", "child_age": 8, "gym_id": KSK, "parent_agreed": True,
            "session_time": "17:00", "discipline": "boxing",
        })
    ), FakeTurn.answer("Готово"))
    assert any("Мы записали вас" in text for text in await to_client(deps))
    before = len(await to_client(deps))

    await say(deps, 2, "Мы точно записаны?", FakeTurn.answer("Да, вы записаны, ждём вас на тренировке!"))

    assert "вы записаны" in "\n".join((await to_client(deps))[before:])


def test_age_from_the_bots_own_words_does_not_count(kb) -> None:
    """«Да» на «Записать Али, 8 лет?» — согласие, а не возраст: «8» здесь сказал бот."""
    from datetime import datetime, timezone

    from app.core.pipeline import _age_from_dialog

    contents = [
        {"role": "model", "parts": [{"text": "Записать Али, 8 лет, на бокс?"}]},
        {"role": "user", "parts": [{"text": "Да [согласие на предложение бота: «Записать Али, 8 лет, на бокс?»]"}]},
    ]
    now = datetime.now(tz=timezone.utc)

    assert _age_from_dialog(contents, "хорошо", kb=kb, now=now) is None
    contents.append({"role": "model", "parts": [{"text": AGE_QUESTION}]})
    assert _age_from_dialog(contents, "8", kb=kb, now=now) == 8, "а вот ответ на вопрос — возраст"


async def test_real_booking_without_a_schedule_keeps_its_words(deps, monkeypatch) -> None:
    """Райцентр без расписания: подтверждения от кода нет, «Записал…» модели — правда.

    Подменять его нельзя, даже если чтение сохранённой заявки не удалось: запись
    этого хода видна по ответу самого инструмента.
    """
    import app.core.pipeline as pipeline

    async def lookup_failed(*_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(pipeline, "_already_booked", lookup_failed)
    claim = "Записал Айназарова Али на пробное в Карабалыке, время подберёт администратор."

    await say(deps, 1, "Айназаров Али, 8 лет, Карабалык, запишите", FakeTurn.tool(
        FakeCall("create_trial_lead", {
            "child_name": "Айназаров Али", "child_age": 8, "gym_id": "region_karabalyk",
            "parent_agreed": True,
        })
    ), FakeTurn.answer(claim))

    assert any(text.startswith("Записал Айназарова Али") for text in await to_client(deps))


async def test_what_to_bring_goes_out_word_for_word(deps) -> None:
    """«Что взять с собой?» — текст владельца дословно, а от модели только вопрос дальше."""
    from app.kb.loader import get_snapshot

    owner = next(item for item in get_snapshot().faq if item.id == "gear_first_lesson").answer.ru
    paraphrase = (
        "Что взять с собой, чтобы ребёнку было комфортно:\n👕 удобная спортивная одежда;\n"
        "💧 вода;\nСпециально покупать форму заранее не нужно.\nВ каком районе вам удобнее заниматься?"
    )

    await say(deps, 1, "Что взять с собой на первую тренировку?", FakeTurn.tool(
        FakeCall("get_kb_fact", {"topic": "gear", "question": "что взять с собой на первую тренировку"})
    ), FakeTurn.answer(paraphrase))

    # Соседние тексты хода склеиваются в одно сообщение (поток из четырёх сообщений
    # подряд владелец просил не слать) — текст владельца идёт первым целым блоком.
    sent = "\n\n".join(await to_client(deps))
    assert sent.startswith(owner), sent
    rest = sent[len(owner):]
    assert "удобная спортивная одежда" not in rest and "вода" not in rest, rest
    assert rest.strip() == "В каком районе вам удобнее заниматься?", "от модели — только вопрос дальше"


async def test_what_to_bring_is_not_sent_twice_with_the_confirmation(deps) -> None:
    """Подтверждение записи уже несёт текст владельца — второй раз его не шлют."""
    from app.kb.loader import get_snapshot

    owner = next(item for item in get_snapshot().faq if item.id == "gear_first_lesson").answer.ru

    await say(deps, 1, "Запишите Айназарова Али, 8 лет, на бокс в КСК в 17:00, и что взять с собой?",
              FakeTurn.tool(FakeCall("create_trial_lead", {
                  "child_name": "Айназаров Али", "child_age": 8, "gym_id": KSK, "parent_agreed": True,
                  "session_time": "17:00", "discipline": "boxing",
              })),
              FakeTurn.tool(FakeCall("get_kb_fact", {"topic": "gear", "question": "что взять с собой"})),
              FakeTurn.answer("Готово"))

    sent = "\n\n".join(await to_client(deps))
    assert "Мы записали вас" in sent
    assert sent.count(owner) == 1, sent


async def test_no_handover_offer_after_the_owners_answer(deps) -> None:
    """Живой прогон 01.10.2026: «отправлено» модель приняла за «данных нет» и
    спросила «Передать ваш вопрос администратору?» — хотя ответ уже был у клиента."""
    from app.kb.loader import get_snapshot

    kb = get_snapshot()
    owner = next(item for item in kb.faq if item.id == "gear_first_lesson").answer.ru

    await say(deps, 1, "Что взять с собой на первую тренировку?", FakeTurn.tool(
        FakeCall("get_kb_fact", {"topic": "gear", "question": "что взять с собой"})
    ), FakeTurn.answer("Передать ваш вопрос администратору?"))

    sent = "\n\n".join(await to_client(deps))
    assert sent.startswith(owner), sent
    assert "администратор" not in sent[len(owner):], sent
    assert sent[len(owner):].strip() == kb.text("funnel.age", Language.RU), "вместо — следующий шаг записи"
