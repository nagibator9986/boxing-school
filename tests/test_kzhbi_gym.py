"""Новый зал на КЖБИ — Гашека 9/3, с 01.11.2026, Пн, Ср, Пт 19:00–20:00 (владелец 13.10.2026).

Бокс и кикбоксинг. До открытия зал виден с пометкой «Занятия с 01.11.2026», а пробное —
только на первые его занятия: 01.11.2026 — воскресенье, первое занятие — понедельник 02.11.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.core import postcheck
from app.kb import render
from app.kb.render import (
    render_gym_location,
    render_gyms_list_card,
    render_route_caption,
    render_schedule_card,
    render_system_prompt,
)
from app.kb.sessions import resolve_trial_session
from app.tools.booking import create_trial_lead
from app.tools.schedule import find_sessions, get_schedule
from app.types import Language, Scope

UTC = timezone.utc
ALMATY = ZoneInfo("Asia/Almaty")
KZHBI = "kzhbi_gasheka_9_3"
NOTE = "Занятия с 01.11.2026"
BEFORE = date(2026, 10, 13)
AFTER = date(2026, 11, 5)


@pytest.fixture
def before_opening(monkeypatch):
    monkeypatch.setattr(render, "school_today", lambda snapshot: BEFORE)


@pytest.fixture
def after_opening(monkeypatch):
    monkeypatch.setattr(render, "school_today", lambda snapshot: AFTER)


def _local(day: int, month: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ALMATY)


def test_the_gym_is_in_the_base_as_the_owner_wrote(kb) -> None:
    gym = kb.gym(KZHBI)

    assert gym is not None and gym.address.ru == "Гашека 9/3" and gym.opens_on == date(2026, 11, 1)
    assert {(slot.discipline, tuple(slot.days), slot.time_start, slot.time_end) for slot in gym.schedule} == {
        (discipline, ("mon", "wed", "fri"), "19:00", "20:00") for discipline in ("boxing", "kickboxing")
    }


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (_local(13, 10, 12), _local(2, 11, 19)),   # до открытия — первое занятие, пн 02.11
        (_local(2, 11, 20, 30), _local(4, 11, 19)),  # после открытия — как обычно, ближайшее
    ],
)
def test_trial_is_not_earlier_than_the_opening(kb, now, expected) -> None:
    choice = resolve_trial_session(
        kb.gym(KZHBI), time_text="19:00", day=None, discipline="boxing", now=now.astimezone(UTC),
        tz_name="Asia/Almaty", min_lead_hours=2,
    )

    assert choice.session is not None and choice.session.starts_at == expected, choice


async def test_booking_now_lands_on_the_first_class(make_ctx) -> None:
    texts = ("Мы на КЖБИ, бокс в 19:00", "Иванов Тимур, 8 лет")
    ctx = replace(make_ctx(client_texts=texts, dialog_client_texts=texts), now=_local(13, 10, 12).astimezone(UTC))

    result = await create_trial_lead(
        ctx, child_name="Иванов Тимур", child_age=8, gym_id=KZHBI, parent_agreed=True,
        session_time="19:00", discipline="boxing",
    )

    assert result.data["booked"] is True, result.data
    assert ctx.services.leads[-1].trial_slot.astimezone(ALMATY) == _local(2, 11, 19)
    confirmation = next(message.text for message in ctx.services.outbound if "Мы записали вас" in (message.text or ""))
    assert "В понедельник, 02.11 в 19:00" in confirmation, confirmation


def test_list_card_says_when_classes_start(kb, before_opening) -> None:
    card = render_gyms_list_card(kb, scope=Scope.CITY, lang=Language.RU)

    assert f"3. КЖБИ\n📍 Гашека 9/3\n🗓 {NOTE}" in card, card
    assert card.count(NOTE) == 1


def test_the_note_disappears_after_the_opening(kb, after_opening) -> None:
    gym = kb.gym(KZHBI)
    cards = (
        render_gyms_list_card(kb, scope=Scope.CITY, lang=Language.RU),
        render_schedule_card(kb, gym_id=KZHBI, slots=gym.schedule, lang=Language.RU),
        render_gym_location(kb, gym_id=KZHBI, lang=Language.RU),
    )

    assert not any(NOTE in card for card in cards), cards


def test_schedule_location_and_caption_carry_the_note_once(kb, before_opening) -> None:
    gym = kb.gym(KZHBI)

    assert NOTE in render_schedule_card(kb, gym_id=KZHBI, slots=gym.schedule, lang=Language.RU)
    assert NOTE in render_gym_location(kb, gym_id=KZHBI, lang=Language.RU)
    caption = render_route_caption(kb, gym_id=KZHBI, lang=Language.RU, with_schedule=True)
    assert caption.count(NOTE) == 1, caption
    assert "Сабақтар 01.11.2026 бастап" in render_gym_location(kb, gym_id=KZHBI, lang=Language.KK)


def test_model_registry_names_the_opening_date(kb) -> None:
    assert "kzhbi_gasheka_9_3 | район КЖБИ | занятия с 01.11.2026" in render_system_prompt(kb)


async def test_schedule_tool_warns_before_the_opening(ctx, before_opening, monkeypatch) -> None:
    from app.tools import gyms

    monkeypatch.setattr(gyms, "school_today", lambda snapshot: BEFORE)
    result = await get_schedule(ctx, gym_id=KZHBI)

    assert result.data["opens_on"] == "2026-11-01"
    assert any("01.11.2026" in caveat and "до этого дня" in caveat for caveat in result.caveats), result.caveats


async def test_schedule_tool_is_quiet_after_the_opening(ctx, after_opening, monkeypatch) -> None:
    from app.tools import gyms

    monkeypatch.setattr(gyms, "school_today", lambda snapshot: AFTER)
    result = await get_schedule(ctx, gym_id=KZHBI)

    assert not any("до этого дня" in caveat for caveat in result.caveats), result.caveats


async def test_find_sessions_skips_the_gym_before_it_opens(make_ctx, monkeypatch) -> None:
    from app.tools import gyms

    monkeypatch.setattr(gyms, "school_today", lambda snapshot: date(2026, 10, 19))
    monday = replace(make_ctx(client_texts=("Сегодня вечером бокс?",)), now=_local(19, 10, 12).astimezone(UTC))

    today = await find_sessions(monday, discipline="boxing", day="today")
    on_wednesdays = await find_sessions(monday, discipline="boxing", day="wed")

    assert KZHBI not in {item["gym_id"] for item in today.data["sessions"]}, "сегодня он ещё не работает"
    found = {item["gym_id"]: item for item in on_wednesdays.data["sessions"]}
    assert found[KZHBI]["opens_on"] == "2026-11-01"
    assert any("01.11.2026" in caveat for caveat in on_wednesdays.caveats)


def test_faq_now_says_there_is_a_gym(kb) -> None:
    entry = next(item for item in kb.faq if item.id == "contacts_kzhbi")

    assert "Гашека 9/3" in entry.answer.ru and "01.11.2026" in entry.answer.ru
    assert "отдельного зала нет" not in entry.answer.ru
    assert not entry.forbidden_claims, "«зал на КЖБИ есть» теперь правда"


@pytest.mark.parametrize(
    ("text", "times"),
    [
        ("Зал на КЖБИ открывается 01.11.2026", ()),
        ("Новый набор с 13.11.2026", ()),
        ("занятия с 01.11", ()),
        ("Записали на понедельник, 02.11 в 19:00", ("19:00",)),
        ("во вторник, 13.10 в 18:00", ("18:00",)),
        ("в 18.30", ("18:30",)),
        ("в 5.30 утра", ("05:30",)),
        ("в 18.00 или 19:00", ("18:00", "19:00")),
    ],
)
def test_dates_are_not_times_for_the_filter(text, times) -> None:
    assert postcheck.extract_times(text) == times


def test_all_gyms_fit_into_one_listing(kb) -> None:
    from app.tools.gyms import MAX_GYMS_LIMIT

    assert len(kb.active_gyms(Scope.ALL)) <= MAX_GYMS_LIMIT
