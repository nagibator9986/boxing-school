"""Скриншоты владельца 07.11.2026.

1. «Он расписание делит на два предложения и уточняет у родителей, какое время им больше
   подходит. А у нас это одно расписание» — Плаза: Вт, Чт 19:00 и Сб 19:30, одна группа.
2. На тот же вопрос клиент ответил «19 30», и бот задал его ещё раз.
3. «На втором Костанае у нас есть зал — Северо-Запад»: бот говорил «в вашем районе зала нет».
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.core import pipeline
from app.kb import render
from app.kb.agreement import with_choice
from app.kb.sessions import client_named_time, client_times, resolve_trial_session
from app.tools.booking import create_trial_lead
from app.tools.gyms import district_place, find_gym_by_district
from app.tools.schedule import get_schedule
from app.types import Language, Scope

UTC = timezone.utc
ALMATY = ZoneInfo("Asia/Almaty")
PLAZA = "plaza_szm_70"
KSK = "ksk_kairbekova_334"
#: Четверг 15.10.2026, 10:18 по Алматы.
THURSDAY = datetime(2026, 10, 15, 10, 18, tzinfo=ALMATY)


def _resolve(kb, now: datetime, *, time_text: str | None = None, day: str | None = None, gym_id: str = PLAZA,
             day_part: str | None = None):
    return resolve_trial_session(
        kb.gym(gym_id), time_text=time_text, day=day, discipline=None, now=now.astimezone(UTC),
        tz_name="Asia/Almaty", min_lead_hours=float(kb.policies.trial_min_lead_hours), day_part=day_part,
    )


def _ctx(make_ctx, *texts: str, now: datetime = THURSDAY):
    return replace(make_ctx(client_texts=texts, dialog_client_texts=texts), now=now.astimezone(UTC))


# --------------------------------------------------------------------------- #
# 1. Одна группа — одна запись, а не выбор
# --------------------------------------------------------------------------- #
def test_plaza_evening_is_one_group(kb) -> None:
    """Вечер — одна группа; утро 9:00 (вернулось 10.11.2026) — другая группа, без group."""
    groups = {(slot.time_start, slot.group) for slot in kb.gym(PLAZA).schedule}
    assert groups == {("09:00", None), ("19:00", "evening"), ("19:30", "evening")}, groups


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (THURSDAY, datetime(2026, 10, 15, 19, 0, tzinfo=ALMATY)),                        # сегодня, четверг
        (datetime(2026, 10, 16, 10, 0, tzinfo=ALMATY), datetime(2026, 10, 17, 19, 30, tzinfo=ALMATY)),  # пт → сб
        (datetime(2026, 10, 17, 5, 43, tzinfo=ALMATY), datetime(2026, 10, 17, 19, 30, tzinfo=ALMATY)),  # как на скриншоте
        (datetime(2026, 10, 17, 18, 0, tzinfo=ALMATY), datetime(2026, 10, 20, 19, 0, tzinfo=ALMATY)),   # сб поздно → вт
    ],
)
def test_evening_without_a_time_takes_the_nearest_class_of_the_group(kb, now, expected) -> None:
    choice = _resolve(kb, now, day_part="evening")

    assert choice.problem is None and choice.session is not None, choice
    assert choice.session.starts_at == expected
    assert choice.session.discipline == "boxing_kickboxing"


@pytest.mark.parametrize(("day", "time"), [("sat", "19:30"), ("tue", "19:00"), ("thu", "19:00")])
def test_a_named_day_keeps_its_own_evening_hour(kb, day, time) -> None:
    choice = _resolve(kb, THURSDAY, day=day, day_part="evening")

    assert choice.session is not None and choice.session.weekday == day and choice.session.time_start == time


def test_morning_or_evening_is_still_a_choice(kb) -> None:
    """Утро и вечер — разные группы: без части дня родителя спрашивают."""
    assert _resolve(kb, THURSDAY).problem == "need_time"
    assert _resolve(kb, THURSDAY, day="sat").problem == "need_time"
    morning = _resolve(kb, THURSDAY, day_part="morning").session
    assert morning is not None and morning.time_start == "09:00"


def test_gyms_without_a_group_still_ask_for_the_time(kb) -> None:
    """В КСК кикбоксинг в 09:00, 17:00 и 19:00 — это разные группы: вопрос остаётся."""
    assert _resolve(kb, THURSDAY, gym_id=KSK).problem == "need_time"


async def test_plaza_asks_morning_or_evening_and_the_evening_pick_books(make_ctx, kb) -> None:
    """Скриншот 07.11, 05:43: «1. Вт, Чт 19:00 2. Сб 19:30» — вечер делился надвое. С 10.11 вопрос
    — утро или вечер, и вечер в нём одним вариантом; «2» записывает на ближайшее вечернее."""
    from tests.test_dialogs_1510 import _invocation

    texts = ("Мы на Северо-Западе, сыну 8 лет", "Запишите", "Иванов Тимур")
    now = datetime(2026, 10, 17, 5, 43, tzinfo=ALMATY)
    result = await create_trial_lead(
        _ctx(make_ctx, *texts, now=now), child_name="Иванов Тимур", child_age=8, gym_id=PLAZA, parent_agreed=True,
    )
    assert result.data["needs"] == ["need_time"], result.data
    question = pipeline._booking_question(kb, Language.RU, [_invocation(result)])
    assert "1. Бокс и кикбоксинг — Вт, Чт, Сб 09:00\n2. Бокс и кикбоксинг — Вт, Чт 19:00, Сб 19:30" in question, question

    ctx = _ctx(make_ctx, *texts, with_choice("2", question), now=now)
    booked = await create_trial_lead(ctx, child_name="Иванов Тимур", child_age=8, gym_id=PLAZA, parent_agreed=True)
    assert booked.data["booked"] is True, booked.data
    assert ctx.services.leads[-1].trial_slot.astimezone(ALMATY) == datetime(2026, 10, 17, 19, 30, tzinfo=ALMATY)

    ctx = _ctx(make_ctx, *texts, "в вечернее время", now=now)
    booked = await create_trial_lead(ctx, child_name="Иванов Тимур", child_age=8, gym_id=PLAZA, parent_agreed=True)
    assert booked.data["booked"] is True, booked.data


def test_a_group_is_one_numbered_option(kb) -> None:
    """Если зал всё же спрашивает выбор, строки группы — одним вариантом, а не двумя."""
    options = [
        {"discipline": discipline, "days": days, "time_start": start, "time_end": end, "group": "evening"}
        for discipline in ("boxing", "kickboxing")
        for days, start, end in ((["tue", "thu"], "19:00", "20:30"), (["sat"], "19:30", "21:00"))
    ]
    other = {"discipline": "boxing", "days": ["mon"], "time_start": "17:00", "time_end": "18:30", "group": None}

    assert pipeline._numbered_options(kb, Language.RU, options) == "", "один вариант — не вопрос"
    text = pipeline._numbered_options(kb, Language.RU, [*options, other])
    assert "Бокс и кикбоксинг — Вт, Чт 19:00, Сб 19:30" in text and text.count("\n") == 1, text


def test_the_gyms_list_shows_the_group_on_one_line(kb) -> None:
    card = render.render_gyms_list_card(kb, scope=Scope.CITY, lang=Language.RU, with_schedule=True)

    assert "Бокс и кикбоксинг: Вт, Чт · 19:00–20:30; Сб · 19:30–21:00" in card, card
    assert card.count("Сб · 19:30") == 1, card


async def test_the_model_is_told_not_to_offer_the_group_as_a_choice(make_ctx, kb) -> None:
    result = await get_schedule(_ctx(make_ctx, "Какое расписание на Плазе?"), gym_id=PLAZA)
    told = " ".join(result.caveats)
    assert "Вт, Чт 19:00 и Сб 19:30 — одна группа" in told, told
    assert "Не спрашивай, какое из этих времён" in told, told

    ksk = await get_schedule(_ctx(make_ctx, "Какое расписание в КСК?"), gym_id=KSK)
    assert "одна группа" not in " ".join(ksk.caveats)


def test_the_system_prompt_names_the_group_without_hours(kb) -> None:
    overview = render.render_schedule_overview(kb)

    assert f"Одна группа в разные дни и часы: {PLAZA}." in overview, overview
    assert not re.search(r"\d{1,2}[:.]\d{2}", overview), "часы в промпте снимает фильтр"


# --------------------------------------------------------------------------- #
# 2. «19 30» — это время
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "times"),
    [
        ("19 30", {"19:30"}),
        ("в 19 30", {"19:30"}),
        ("давайте на 17 00", {"17:00"}),
        ("детям 10 12", set()),
        ("сыну 7, дочке 15", set()),
    ],
)
def test_time_with_a_space_is_a_time(text, times) -> None:
    assert client_times([text]) == times


async def test_19_30_answer_books_saturday(make_ctx) -> None:
    """Скриншот 07.11, 05:45: «19 30» — и тот же вопрос ещё раз."""
    ctx = _ctx(make_ctx, "Мы на Северо-Западе", "Иванов Тимур, 8 лет", "19 30")

    assert client_named_time("19:30", ctx.client_texts)
    result = await create_trial_lead(
        ctx, child_name="Иванов Тимур", child_age=8, gym_id=PLAZA, parent_agreed=True, session_time="19:30",
    )

    assert result.data["booked"] is True, result.data
    assert ctx.services.leads[-1].trial_slot.astimezone(ALMATY) == datetime(2026, 10, 17, 19, 30, tzinfo=ALMATY)


# --------------------------------------------------------------------------- #
# 3. Второй Костанай — район Плазы
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("query", ["второй Костанай", "Мы живём во втором Костанае", "2 Костанай", "Костанай 2"])
async def test_second_kostanay_has_its_own_gym(ctx, query) -> None:
    result = await find_gym_by_district(ctx, district_text=query)

    assert [gym["id"] for gym in result.data["gyms"]][:1] == [PLAZA], result.data
    assert result.data["nearby_only"] is False, result.data
    assert not any("зала НЕТ" in caveat for caveat in result.caveats), result.caveats


async def test_the_city_alone_still_names_no_gym(ctx) -> None:
    result = await find_gym_by_district(ctx, district_text="Мы в Костанае")

    assert len(result.data["gyms"]) > 1, result.data


@pytest.mark.parametrize(
    ("text", "place"),
    [
        ("Живу во втором Костанае", (PLAZA, True)),
        ("Живём в районе аэропорт", (PLAZA, False)),
        ("Нет КСК далеко час езды", None),       # назван сам зал — не подсказка
        ("Мы в Костанае", None),
    ],
)
def test_district_place_tells_inside_from_nearby(kb, text, place) -> None:
    assert district_place(kb, text) == place


# --------------------------------------------------------------------------- #
# 4. С 12 лет записываются сами
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "child"),
    [
        ("Мне 15", False),                                   # скриншот 06.11, 22:39
        ("Мне 12 лет", False),
        ("Маған 13 жас", False),
        ("я в 9 классе, хочу записаться", False),            # девятиклассник — 14
        ("мне 14, хочу записаться, родители разрешили", False),
        ("Мне 11", True),
        ("мне 9 лет, хочу на бокс", True),
        ("Маған 10 жас", True),
        ("я в 5 классе, хочу записаться", True),             # пятиклассник — 10
        ("хочу записаться, родители разрешили", True),       # возраста нет — как раньше
        ("Мне 10 минут ехать до зала", False),               # не возраст
    ],
)
def test_children_from_twelve_sign_up_alone(kb, text, child) -> None:
    from app.core.guards import detect_child_writing

    assert detect_child_writing(text, lang=Language.RU, self_signup_age=kb.policies.audience_self_signup_age) is child


def test_the_threshold_comes_from_the_policies(kb) -> None:
    from app.core import guards
    from app.types import GuardFlag

    assert kb.policies.audience_self_signup_age == 12
    assert guards.detect_child_writing("Мне 15", lang=Language.RU, self_signup_age=16), "порог из правил, не из кода"
    verdict = guards.scan("Мне 15", lang=Language.RU, lexicon=kb.lexicon, policies=kb.policies)
    assert not verdict.has(GuardFlag.CHILD_WRITING), verdict
    assert "учеником от 12 лет" in render.render_escalation_rules(kb)


async def test_a_teen_is_served_and_a_child_still_asked_for_parents(kb, state, sessionmaker, settings) -> None:
    """Скриншот 06.11, 22:39: «Мне 15» → «покажи это сообщение маме или папе»."""
    from app.core.pipeline import PipelineDeps, process_inbound
    from app.kb import loader as kb_loader
    from app.llm.client import FakeLLMClient, FakeTurn

    from tests.conftest import RecordingQueue, webhook_payload

    kb_loader.swap(kb)
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, kb=kb_loader.get_snapshot, queue=RecordingQueue(),
        settings=settings, llm=FakeLLMClient([]),
    )
    child_note = "За клавиатурой сам ребёнок"

    async def say(chat: str, number: int, text: str):
        deps.llm.reset([FakeTurn.answer("Отлично! Подскажите имя и фамилию?")])
        before = len(deps.llm.requests)
        decisions = await process_inbound(deps, webhook_payload(f"{chat}-{number}", text, chat_id=chat))
        note = deps.llm.requests[-1].dynamic_note if len(deps.llm.requests) > before else None
        return [d.reason for d in decisions], note

    reasons, note = await say("77015550711", 1, "Мне 15")
    assert reasons != ["escalate:child_writing"] and note is not None and child_note not in note, reasons

    reasons, _ = await say("77015550712", 1, "мне 10 лет хочу на бокс")
    assert reasons == ["escalate:child_writing"], reasons
    _, note = await say("77015550712", 2, "Родители не знают пока")
    assert note is not None and child_note in note, "ребёнку младше 12 запись по-прежнему не оформляют"
    # «Мне 13» после просьбы позвать родителей — ученик, которого записывают, до конца разговора.
    _, note = await say("77015550712", 3, "Мне 13 вообще-то")
    assert note is not None and child_note not in note, note
    _, note = await say("77015550712", 4, "Запишите на бокс")
    assert note is not None and child_note not in note, note


def test_an_evening_request_is_not_offered_the_morning(kb) -> None:
    """КСК, кикбоксинг: 09:00, 17:00, 19:00. На «вечером» в вопросе только вечер (жалоба 16.10.2026)."""
    choice = resolve_trial_session(
        kb.gym(KSK), time_text=None, day=None, discipline="kickboxing", now=THURSDAY.astimezone(UTC),
        tz_name="Asia/Almaty", min_lead_hours=2.0, day_part="evening",
    )

    assert choice.problem == "need_time"
    assert {slot.time_start for slot in choice.options} == {"17:00", "19:00"}, choice.options


def test_the_schedule_card_shows_the_group_on_one_line(kb) -> None:
    gym = kb.gym(PLAZA)
    card = render.render_schedule_card(kb, gym_id=PLAZA, slots=gym.schedule, lang=Language.RU)

    assert "🕒 Вт, Чт, Сб · 09:00–10:30\n🕒 Вт, Чт · 19:00–20:30; Сб · 19:30–21:00" in card, card
