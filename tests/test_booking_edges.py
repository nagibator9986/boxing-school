"""Края записи, найденные живым прогоном 10.09.2026."""

from __future__ import annotations

import pytest

from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.kb.render import render_pricing_showcase
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
from app.types import Language

from tests.conftest import RecordingQueue, webhook_payload

KSK = "ksk_kairbekova_334"


async def _turn(kb, state, sessionmaker, settings, chat: str, text: str, turns):
    kb_loader.swap(kb)
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=FakeLLMClient(turns), kb=kb_loader.get_snapshot,
        queue=RecordingQueue(), settings=settings,
    )
    decisions = await process_inbound(deps, webhook_payload(f"{chat}-1", text, chat_id=chat))
    client = "\n".join(out.text or "" for d in decisions for out in d.outbound)
    return decisions, client


async def test_confirmation_is_not_spoiled_by_the_filter(kb, state, sessionmaker, settings) -> None:
    """Модель после записи написала «в пятницу» — подтверждение уходит чистым.

    Раньше фильтр снимал этот текст (который и так не отправляется), к
    подтверждению приклеивалось «ответит администратор», а диалог вставал на паузу.
    """
    decisions, client = await _turn(kb, state, sessionmaker, settings, "77015558101", "Сериков Ержан, 8 лет — давайте на 19:00", [
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Сериков Ержан", "child_age": 8, "gym_id": KSK,
            "parent_agreed": True, "session_time": "19:00",
        })),
        FakeTurn.answer("Отлично, ждём вас в пятницу!"),
    ])

    assert [d.action.value for d in decisions] == ["reply"], "подтверждение ушло в эскалацию"
    assert "Мы записали вас" in client and "👤 Сериков Ержан" in client
    assert "администратор" not in client.lower(), client


async def test_kinship_word_is_not_a_childs_name(kb, state, sessionmaker, settings) -> None:
    """«Айгуль сын» — имя мамы из контакта и слово родства. Такой записи быть не должно."""
    decisions, client = await _turn(kb, state, sessionmaker, settings, "77015558102", "На бокс", [
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Айгуль сын", "child_age": 9, "gym_id": KSK,
            "parent_agreed": True, "session_time": "17:00", "session_day": "tue", "discipline": "boxing",
        })),
        FakeTurn.answer("Подскажите фамилию и имя ребёнка?"),
    ])

    booking = [inv for d in decisions for inv in d.invocations if inv.name == "create_trial_lead"]
    assert booking and not booking[0].result.ok, "ребёнка записали под чужим именем"
    assert "Мы записали вас" not in client


async def test_no_booking_offer_when_the_client_already_asked_to_book(kb, state, sessionmaker, settings) -> None:
    """Клиент сам написал «хочу записать» — встречное «Записать ребёнка?» нелепо."""
    _, client = await _turn(
        kb, state, sessionmaker, settings, "77015558103",
        "Хочу записать сына на пробное в КСК",
        [
            FakeTurn.tool(FakeCall("get_schedule", {"gym_id": KSK})),
            FakeTurn.answer("Как зовут сына и сколько ему лет?"),
        ],
    )

    assert kb.text("funnel.book_trial", Language.RU) not in client, client
    assert kb.text("funnel.name_age", Language.RU) in client, "имя спрашивается вместе с фамилией"


def test_prompt_says_tobyl_uses_city_prices(kb) -> None:
    """Модель считала Тобыл райцентром и называла 10 000 ₸ — промпт обязан сказать иначе."""
    showcase = render_pricing_showcase(kb)

    assert "Костанай и Тобыл" in showcase
    assert "по городским ценам" in showcase


def _price_ctx(kb, client_texts: tuple[str, ...] = ()):
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.types import ChannelKind, ToolContext
    from tests.conftest import RecordingServices

    return ToolContext(
        conversation_id=uuid4(), conv_key="conv:wa", channel=ChannelKind.WHATSAPP, channel_id="wa",
        chat_id="7701", lang=Language.RU, kb=kb, kb_hash=kb.kb_hash,
        now=datetime(2026, 9, 10, 12, 0, tzinfo=UTC), correlation_id="test", services=RecordingServices(),
        client_texts=client_texts,
    )


async def test_tobyl_gets_the_city_price_even_if_asked_as_a_region(kb) -> None:
    """Живой прогон: модель спросила Тобыл как райцентр и назвала 10 000 ₸.

    Владелец: «у нас одна цена». Посёлок клиента решает цену надёжнее, чем догадка модели.
    """
    from app.tools.pricing import calculate_price

    result = await calculate_price(
        _price_ctx(kb), scope="region", plan="standard", children_count=1, settlement="Тобыла"
    )

    assert result.ok and result.data["scope"] == "city", result.data
    assert "25000" in str(result.data)


async def test_real_district_centre_keeps_its_own_price(kb) -> None:
    """Райцентр остаётся райцентром, даже если модель по ошибке спросила город."""
    from app.tools.pricing import calculate_price

    result = await calculate_price(
        _price_ctx(kb), scope="city", plan="standard", children_count=1, settlement="Карабалык"
    )

    assert result.ok and result.data["scope"] == "region", result.data


async def test_time_invented_by_the_model_is_not_booked(kb, state, sessionmaker, settings) -> None:
    """Клиент назвал только имя и возраст — модель подставила «бокс, суббота 17:00».

    Живой прогон 10.09.2026: ребёнка записали на время, которое никто не выбирал,
    а через ход — ещё раз, уже на 19:00. Время спрашивается у родителя.
    """
    decisions, client = await _turn(kb, state, sessionmaker, settings, "77015558104", "Сериков Ержан, 8 лет", [
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Сериков Ержан", "child_age": 8, "gym_id": KSK, "parent_agreed": True,
            "session_time": "17:00", "session_day": "sat", "discipline": "boxing",
        })),
        FakeTurn.answer("В какое время удобнее?"),
    ])

    booking = [inv for d in decisions for inv in d.invocations if inv.name == "create_trial_lead"]
    assert booking[0].result.data["booked"] is False
    assert "need_time" in booking[0].result.data["needs"]
    assert "Мы записали вас" not in client


async def test_price_card_follows_the_clients_settlement(kb) -> None:
    """«Мы из Тобыла» — даже если модель попросила карточку райцентров, уходит городская."""
    from app.tools.content import send_content

    ctx = _price_ctx(kb, client_texts=("Мы из Тобыла, сколько стоит абонемент?",))

    result = await send_content(ctx, artifact_id="price_card_region")

    sent = "\n".join(message.text or "" for message in ctx.services.outbound)
    assert result.ok
    assert "25 000" in sent and "10 000" not in sent, sent


async def test_price_follows_the_clients_settlement_without_an_argument(kb) -> None:
    """Модель не передала посёлок — цену всё равно решают слова клиента."""
    from app.tools.pricing import calculate_price

    ctx = _price_ctx(kb, client_texts=("Мы из Тобыла, сколько стоит абонемент?",))

    result = await calculate_price(ctx, scope="region", plan="standard", children_count=1)

    assert result.data["scope"] == "city", result.data


async def test_section_invented_by_the_model_is_asked(kb, state, sessionmaker, settings) -> None:
    """Время клиент назвал, секцию — нет: в 17:00 в КСК идут и бокс, и кикбоксинг."""
    decisions, client = await _turn(kb, state, sessionmaker, settings, "77015558105", "Иванов Али, 9 лет, в 17:00", [
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Иванов Али", "child_age": 9, "gym_id": KSK, "parent_agreed": True,
            "session_time": "17:00", "discipline": "boxing",
        })),
        FakeTurn.answer("На бокс или на кикбоксинг?"),
    ])

    booking = [inv for d in decisions for inv in d.invocations if inv.name == "create_trial_lead"]
    assert booking[0].result.data["booked"] is False
    assert booking[0].result.data["needs"] == ["need_discipline"], booking[0].result.data
    assert "Мы записали вас" not in client


@pytest.mark.parametrize(
    ("client_text", "model_day", "expected_weekday"),
    [
        # Четверг 10.09, 17:00 по Алматы: сегодняшнее занятие уже не успеть — ближайшее в субботу.
        ("Иванов Али, 9 лет, в 17:00 на бокс", "tue", 5),
        ("Иванов Али, 9 лет, во вторник в 17:00 на бокс", "tue", 1),
    ],
)
async def test_day_is_taken_only_from_the_client(kb, client_text, model_day, expected_weekday) -> None:
    from zoneinfo import ZoneInfo

    from app.tools.booking import create_trial_lead

    ctx = _price_ctx(kb, client_texts=(client_text,))
    result = await create_trial_lead(
        ctx, child_name="Иванов Али", child_age=9, gym_id=KSK, parent_agreed=True,
        session_time="17:00", session_day=model_day, discipline="boxing",
    )

    assert result.data["booked"] is True, result.data
    slot = ctx.services.leads[-1].trial_slot.astimezone(ZoneInfo("Asia/Almaty"))
    assert (slot.weekday(), slot.hour) == (expected_weekday, 17), slot


async def test_name_invented_by_the_model_is_asked_in_full(kb, state, sessionmaker, settings) -> None:
    """«На бокс, в 17:00» — ребёнка ещё не называли, а модель спрашивает «фамилию сына»."""
    from app.types import Language

    decisions, client = await _turn(kb, state, sessionmaker, settings, "77015558106", "На бокс, в 17:00", [
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Сына", "child_age": 9, "gym_id": KSK, "parent_agreed": True,
            "session_time": "17:00", "discipline": "boxing",
        })),
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Али", "child_age": 9, "gym_id": KSK, "parent_agreed": True,
            "session_time": "17:00", "discipline": "boxing",
        })),
        FakeTurn.answer("Как зовут сына и сколько ему лет?"),
    ])

    booking = [inv for d in decisions for inv in d.invocations if inv.name == "create_trial_lead"]
    assert not booking[0].result.ok, "«сына» — не имя"
    data = booking[1].result.data
    assert data["booked"] is False and data["needs"] == ["need_name", "need_age"], data
    assert data["child_name"] is None
    assert kb.text("funnel.name_age", Language.RU) in client, client
    assert "Как зовут" not in client


async def test_clients_time_outside_the_schedule_is_not_blocked(kb, state, sessionmaker, settings) -> None:
    """«В 17:30» в КСК: занятия нет — бот так и говорит, а не зовёт администратора.

    Живой прогон 10.09.2026: фильтр не нашёл 17:30 в данных хода, снял ответ
    и поставил диалог на паузу.
    """
    decisions, client = await _turn(
        kb, state, sessionmaker, settings, "77015558107", "Сериков Ержан, 8 лет, в 17:30 на кикбоксинг", [
            FakeTurn.tool(FakeCall("create_trial_lead", {
                "child_name": "Сериков Ержан", "child_age": 8, "gym_id": KSK, "parent_agreed": True,
                "session_time": "17:30", "discipline": "kickboxing",
            })),
            FakeTurn.answer("В 17:30 занятий нет. На кикбоксинг можно в 17:00 или 19:00 — какое время выбрать?"),
        ],
    )

    booking = [inv for d in decisions for inv in d.invocations if inv.name == "create_trial_lead"]
    assert booking[0].result.data["status"] == "unknown_time", booking[0].result.data
    assert "В 17:30 занятий нет" in client, client
    assert "администратор" not in client


async def test_child_named_earlier_counts_from_the_lead(kb) -> None:
    """Имя и возраст назвали давно — они уже в заявке, переспрашивать не нужно."""
    from dataclasses import replace

    from app.tools.booking import create_trial_lead
    from app.types import LeadDraft

    ctx = replace(
        _price_ctx(kb, client_texts=("в 17:00 на бокс",)),
        lead_draft=LeadDraft(child_name="Иванов Али", child_age=9),
    )
    result = await create_trial_lead(
        ctx, child_name="Иванов Али", child_age=9, gym_id=KSK, parent_agreed=True,
        session_time="17:00", discipline="boxing",
    )

    assert result.data["booked"] is True, result.data


async def _two_turns(kb, state, sessionmaker, settings, chat: str, *turns):
    kb_loader.swap(kb)
    llm = FakeLLMClient([])
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=llm, kb=kb_loader.get_snapshot,
        queue=RecordingQueue(), settings=settings,
    )
    results = []
    for index, (text, script) in enumerate(turns, 1):
        llm.reset(script)
        results.append(await process_inbound(deps, webhook_payload(f"{chat}-{index}", text, chat_id=chat)))
    return results


async def test_time_from_the_schedule_already_sent_is_not_blocked(kb, state, sessionmaker, settings) -> None:
    """Живой прогон 10.09.2026: расписание КСК пришло ходом раньше, на «На бокс» модель
    повторила «17:00–18:30» — фильтр снял ответ, клиенту ушло «ответит администратор»."""
    _, second = await _two_turns(
        kb, state, sessionmaker, settings, "77015558108",
        ("Хочу записать сына на пробное в КСК", [
            FakeTurn.tool(FakeCall("get_schedule", {"gym_id": KSK})),
            FakeTurn.answer("Какая секция интересует?"),
        ]),
        ("На бокс", [FakeTurn.answer("Бокс идёт в 17:00–18:30. Подскажите фамилию, имя и возраст ребёнка?")]),
    )

    assert not [d.postcheck_fail for d in second if d.postcheck_fail]
    client = "\n".join(out.text or "" for d in second for out in d.outbound)
    assert "17:00–18:30" in client, client


async def test_no_second_question_after_a_card_that_already_asks(kb, state, sessionmaker, settings) -> None:
    """Список залов кончается «Какой из них вам ближе?» — вопрос воронки следом лишний."""
    _, client = await _turn(kb, state, sessionmaker, settings, "77015558109", "Скиньте залы", [
        FakeTurn.tool(FakeCall("send_content", {"artifact_id": "gyms_list_city"})),
        FakeTurn.answer("У нас восемь залов в городе."),
    ])

    assert kb.text("card.pick_gym", Language.RU) in client, client
    for key in ("funnel.age", "funnel.district", "funnel.time", "funnel.name"):
        assert kb.text(key, Language.RU) not in client, (key, client)


async def test_price_from_the_card_already_sent_is_not_blocked(kb, state, sessionmaker, settings) -> None:
    """Прайс пришёл ходом раньше — повтор его цены без инструмента не выдумка."""
    _, second = await _two_turns(
        kb, state, sessionmaker, settings, "77015558110",
        ("Сколько стоит абонемент?", [
            FakeTurn.tool(FakeCall("send_content", {"artifact_id": "price_card_city"})),
            FakeTurn.answer("Сколько лет ребёнку?"),
        ]),
        ("А стандартный какой?", [FakeTurn.answer("Стандартный абонемент — 25 000 ₸. Сколько лет ребёнку?")]),
    )

    assert not [d.postcheck_fail for d in second if d.postcheck_fail]
    assert "25 000" in "\n".join(out.text or "" for d in second for out in d.outbound)


async def test_models_own_question_after_a_card_that_asks_is_dropped(kb, state, sessionmaker, settings) -> None:
    """Карточка спросила «Какой из них вам ближе?» — свой вопрос модели следом лишний."""
    _, client = await _turn(kb, state, sessionmaker, settings, "77015558111", "Скиньте залы", [
        FakeTurn.tool(FakeCall("send_content", {"artifact_id": "gyms_list_city"})),
        FakeTurn.answer("Залы есть в разных районах города. Какой район вам ближе?"),
    ])

    assert kb.text("card.pick_gym", Language.RU) in client, client
    assert "Какой район вам ближе?" not in client, client
    assert "Залы есть в разных районах города." in client, client


async def test_blocked_reply_does_not_stay_in_the_models_memory(kb, state, sessionmaker, settings) -> None:
    """Снятый фильтром ответ не остаётся в истории модели — иначе она повторяет ошибку."""
    import sqlalchemy as sa

    from app.core import session as conv_session
    from app.storage.models import Conversation

    chat = "77015558112"
    decisions, client = await _turn(kb, state, sessionmaker, settings, chat, "Во сколько бокс в КСК?", [
        FakeTurn.answer("Бокс в КСК во вторник в 21:15. Записать?"),
    ])
    assert [d.postcheck_fail for d in decisions if d.postcheck_fail], "ответ должен был сняться"
    # Время, которого нет ни в одном расписании: карточки деградированного ответа
    # законно содержат настоящие часы других залов.
    assert "21:15" not in client

    async with sessionmaker() as db:
        conv = (
            await db.execute(sa.select(Conversation).where(Conversation.conv_key.like(f"%{chat}")))
        ).scalars().first()
        history = await conv_session.load_history(db, conv, max_turns=20)
    said = [
        part.get("text") or ""
        for item in history if item.get("role") == "model"
        for part in item.get("parts") or []
    ]
    assert said, history
    assert not any("21:15" in text for text in said), said


async def test_degraded_price_after_a_blocked_reply_keeps_the_clients_city(kb, state, sessionmaker, settings) -> None:
    """Живой прогон 10.09.2026: клиент спросил про Костанай, на «Дорого, есть подешевле?»
    модель выдумала сумму, фильтр снял ответ — и запасной ответ прислал городской прайс
    второй раз плюс районный «10 000 ₸»."""
    from app.kb.render import render_price_card
    from app.types import Scope

    _, second = await _two_turns(
        kb, state, sessionmaker, settings, "77015558113",
        ("Сколько стоит абонемент в Костанае?", [
            FakeTurn.tool(FakeCall("send_content", {"artifact_id": "price_card_city"})),
            FakeTurn.answer("Хотите записать ребёнка на пробное?"),
        ]),
        ("Дорого, есть подешевле?", [FakeTurn.answer("Со скидкой выйдет 1 979 ₸ за занятие.")]),
    )

    assert [d.postcheck_fail for d in second if d.postcheck_fail], "ответ должен был сняться"
    client = "\n".join(out.text or "" for d in second for out in d.outbound)
    assert render_price_card(kb, scope=Scope.REGION, lang=Language.RU).splitlines()[0] not in client, client
    assert render_price_card(kb, scope=Scope.CITY, lang=Language.RU).splitlines()[0] not in client, client
