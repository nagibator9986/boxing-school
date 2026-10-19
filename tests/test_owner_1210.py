"""Сообщения владельца 12.10.2026 в рабочем чате.

«Нужно убрать адрес Каирбекова 24 и Полевая». «График работы Тобол 19:30–21:00».
«График работы Касымханова 18:00–19:00». «На КСК Пн, Ср, Пт — кикбоксинг, абонемент
25 000 как был. Вт, Чт, Сб — бокс утром в 9:00 и вечером в 17:00 — по 20 000».
"""

from __future__ import annotations

from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.kb.render import render_price_card, render_schedule_card
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
from app.types import Language, Scope

from tests.conftest import RecordingQueue, webhook_payload

KSK = "ksk_kairbekova_334"


def test_ksk_schedule_shows_the_discounted_boxing_price(kb) -> None:
    card = render_schedule_card(kb, gym_id=KSK, slots=kb.gym(KSK).schedule, lang=Language.RU)
    lines = card.splitlines()

    priced = [line for line in lines if "абонемент 20 000 ₸" in line]
    assert priced == [
        "🕒 Вт, Чт, Сб · 09:00–10:30 — абонемент 20 000 ₸",
        "🕒 Вт, Чт, Сб · 17:00–18:30 — абонемент 20 000 ₸",
    ], card
    assert not any("Пн, Ср, Пт" in line and "абонемент" in line for line in lines), "кикбоксинг — по прайсу"


def test_city_price_card_names_the_special_price(kb) -> None:
    card = render_price_card(kb, scope=Scope.CITY, lang=Language.RU)

    assert "Стандартный — 25 000 ₸" in card, "общий прайс не меняется"
    # Владелец 15.10.2026: столбиком и с концом каждого занятия — «в 09:00, 17:00» родитель
    # прочитал как «вы с 09:00 до пяти работаете».
    assert "КСК — школа №9\nСекция бокса\nВт, Чт, Сб\n09:00–10:30\n17:00–18:30\nАбонемент — 20 000 ₸" in card, card
    assert "в 09:00, 17:00" not in card


async def test_price_and_schedule_tools_carry_the_special_price(ctx) -> None:
    from app.tools.pricing import calculate_price
    from app.tools.schedule import get_schedule

    price = await calculate_price(ctx, scope="city", plan="standard", children_count=1)
    assert price.data["special_prices"] == [{
        "gym_id": KSK, "discipline": "boxing", "days": ["tue", "thu", "sat"],
        "times": ["09:00", "17:00"], "ranges": ["09:00–10:30", "17:00–18:30"], "price": 20000,
    }]
    assert any("special_prices" in caveat for caveat in price.caveats)
    assert price.data["total"] == 25000, "общий расчёт не меняется"

    schedule = await get_schedule(ctx, gym_id=KSK)
    prices = {(s["discipline"], tuple(s["days"]), s["time_start"]): s["price"] for s in schedule.data["slots"]}
    assert prices[("boxing", ("tue", "thu", "sat"), "09:00")] == 20000
    assert prices[("kickboxing", ("mon", "wed", "fri"), "19:00")] is None


async def test_bot_may_quote_the_special_price(kb, state, sessionmaker, settings) -> None:
    """«20 000 ₸» на бокс в КСК — не выдумка: фильтр не снимает такой ответ."""
    import sqlalchemy as sa

    from app.storage.models import OutboxMessage

    kb_loader.swap(kb)
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, kb=kb_loader.get_snapshot, queue=RecordingQueue(),
        settings=settings, llm=FakeLLMClient([
            FakeTurn.tool(FakeCall("get_schedule", {"gym_id": KSK})),
            FakeTurn.answer("По вторникам, четвергам и субботам в 9:00 и 17:00 — бокс, абонемент 20 000 ₸. Записать?"),
        ]),
    )

    decisions = await process_inbound(deps, webhook_payload("o1210-1", "Сколько стоит бокс в КСК?", chat_id="77015550121"))

    assert decisions[0].postcheck_fail is None, decisions[0]
    async with sessionmaker() as db:
        texts = [str(p.get("text") or "") for p in (await db.execute(sa.select(OutboxMessage.payload))).scalars()]
    assert any(text.endswith("бокс, абонемент 20 000 ₸.") for text in texts), texts
