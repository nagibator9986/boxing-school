"""Владелец 14.10.2026: «не делай разделения на бокс и кикбоксинг — пиши сразу вместе».

Скриншот: Тобыл, Пн/Ср/Пт 19:30 — бот спросил «На какую секцию записать ребёнка? 1. Бокс
2. Кикбоксинг». Там, где обе секции идут в одни дни и час, это одно занятие «Бокс и
кикбоксинг». В КСК они в разные дни (кикбоксинг Пн/Ср/Пт, бокс Вт/Чт/Сб) — выбор остаётся.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.core import pipeline
from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.kb.render import render_schedule_card, render_trial_confirmation
from app.kb.sessions import client_times, resolve_trial_session
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
from app.tools.booking import _options_line, create_trial_lead
from app.types import Language

from tests.conftest import RecordingQueue, webhook_payload

UTC = timezone.utc
ALMATY = ZoneInfo("Asia/Almaty")
TOBYL = "region_tobyl"
KSK = "ksk_kairbekova_334"
WEDNESDAY = datetime(2026, 10, 14, 12, 0, tzinfo=ALMATY)


def _choose(kb, gym, **kwargs):
    return resolve_trial_session(
        kb.gym(gym), now=WEDNESDAY.astimezone(UTC), tz_name="Asia/Almaty", min_lead_hours=2,
        time_text=kwargs.get("time"), day=kwargs.get("day"), discipline=kwargs.get("discipline"),
    )


def test_both_sections_at_one_time_are_one_class(kb) -> None:
    session = _choose(kb, TOBYL, day="mon").session

    assert session is not None and session.discipline == "boxing_kickboxing"
    assert (session.starts_at.astimezone(ALMATY).day, session.time_start) == (19, "19:30")


def test_a_section_the_parent_named_is_kept(kb) -> None:
    assert _choose(kb, TOBYL, discipline="boxing").session.discipline == "boxing"


def test_sections_on_different_days_are_still_asked(kb) -> None:
    """КСК в 17:00: кикбоксинг Пн/Ср/Пт, бокс Вт/Чт/Сб — секция здесь и есть выбор дней."""
    assert _choose(kb, KSK, time="17:00").problem == "need_discipline"


async def test_booking_in_tobyl_goes_through_without_the_section_question(make_ctx) -> None:
    texts = ("Мы из Тобыла", "Да, конечно. Давайте может с понедельника 19 числа", "Иванова Аружан, 8 лет")
    ctx = replace(make_ctx(client_texts=texts, dialog_client_texts=texts), now=WEDNESDAY.astimezone(UTC))

    result = await create_trial_lead(
        ctx, child_name="Иванова Аружан", child_age=8, gym_id=TOBYL, parent_agreed=True, session_day="mon",
    )

    assert result.data["booked"] is True, result.data
    confirmation = next(m.text for m in ctx.services.outbound if "Мы записали вас" in (m.text or ""))
    assert "🥊 Бокс и кикбоксинг" in confirmation, confirmation
    assert "В понедельник, 19.10 в 19:30" in confirmation


def test_confirmation_names_tobyl_once(kb) -> None:
    session = _choose(kb, TOBYL, day="mon").session

    text = render_trial_confirmation(kb, child="Иванова Аружан", gym=kb.gym(TOBYL), session=session, lang=Language.RU)

    assert "📍 Тобыл, улица Тәуелсіздік, 51" in text and "Тобыл, Тобыл" not in text, text


def test_schedule_card_writes_them_together(kb) -> None:
    tobyl = render_schedule_card(kb, gym_id=TOBYL, slots=kb.gym(TOBYL).schedule, lang=Language.RU)
    ksk = render_schedule_card(kb, gym_id=KSK, slots=kb.gym(KSK).schedule, lang=Language.RU)

    assert "🥊 Бокс и кикбоксинг\n🕒 Пн, Ср, Пт · 19:30–21:00" in tobyl, tobyl
    assert "🥊 Бокс\n" not in tobyl and "🥊 Кикбоксинг\n" not in tobyl
    assert "🥊 Кикбоксинг\n" in ksk and "🥊 Бокс\n" in ksk, "в КСК секции в разные дни"
    assert "Бокс и кикбоксинг" not in ksk


def test_options_merge_only_the_same_class(kb) -> None:
    options = [
        {"discipline": "boxing", "days": ["tue", "thu", "sat"], "time_start": "09:00"},
        {"discipline": "kickboxing", "days": ["tue", "thu", "sat"], "time_start": "09:00"},
        {"discipline": "kickboxing", "days": ["mon", "wed", "fri"], "time_start": "17:00"},
    ]

    assert pipeline._numbered_options(kb, Language.RU, options) == (
        "1. Бокс и кикбоксинг — Вт, Чт, Сб 09:00\n2. Кикбоксинг — Пн, Ср, Пт 17:00"
    )
    assert _options_line(kb, kb.gym(TOBYL).schedule) == "Бокс и кикбоксинг — Пн, Ср, Пт 19:30"


async def test_pipeline_books_tobyl_without_asking_the_section(kb, state, sessionmaker, settings) -> None:
    kb_loader.swap(kb)
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, kb=kb_loader.get_snapshot, queue=RecordingQueue(),
        settings=settings, llm=FakeLLMClient([
            FakeTurn.tool(FakeCall("create_trial_lead", {
                "child_name": "Иванова Аружан", "child_age": 8, "gym_id": TOBYL, "parent_agreed": True,
                "session_day": "mon",
            })),
            FakeTurn.answer("В зале Тобыл в это время проходят две секции."),
        ]),
    )

    decisions = await process_inbound(deps, webhook_payload(
        "s-1", "Иванова Аружан, 8 лет, в Тобыле, давайте с понедельника", chat_id="77015551415"
    ))

    text = "\n".join(out.text or "" for d in decisions for out in d.outbound)
    assert "Мы записали вас" in text and "Бокс и кикбоксинг" in text, text
    assert kb.text("funnel.discipline", Language.RU) not in text


def test_place_numbers_are_not_hours() -> None:
    assert client_times(["Хочу записать Али, 8 лет, в 6-й микрорайон"]) == frozenset()
    assert client_times(["Учимся в 9 школе", "мы в 7 мкр"]) == frozenset()
    assert "19:00" in client_times(["давайте в 7"])
