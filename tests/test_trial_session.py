"""Ближайшее занятие для пробного считает код — по расписанию зала.

Владелец 10.09.2026: «пускай бот полностью сам конвертирует: мы записали вас на
19:00, приходите заранее минут за 10». День недели и число модель путает, а
ошибка здесь — ребёнок, пришедший в закрытый зал.

Даты взяты из сентября 2026: 8-е — вторник.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.kb.models import KBSnapshot
from app.kb.render import render_trial_confirmation
from app.kb.sessions import normalize_time, resolve_trial_session
from app.types import Language

TZ = "Asia/Almaty"
KSK = "ksk_kairbekova_334"


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=ZoneInfo(TZ)).astimezone(timezone.utc)


def choose(kb: KBSnapshot, *, time, now, day=None, discipline=None, lead=2.0, gym=KSK):
    return resolve_trial_session(
        kb.gym(gym), time_text=time, day=day, discipline=discipline, now=now,
        tz_name=TZ, min_lead_hours=lead,
    )


def test_calendar_assumption() -> None:
    assert datetime(2026, 9, 8).weekday() == 1, "8 сентября 2026 — вторник"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("19:00", "19:00"), ("в 9", "09:00"), ("17.30", "17:30"), ("вечером", None), (None, None)],
)
def test_time_is_read_from_the_parents_words(text, expected) -> None:
    assert normalize_time(text) == expected


def test_nearest_session_for_the_chosen_time(kb: KBSnapshot) -> None:
    """Вторник утром, «на 19:00» — в КСК это кикбоксинг, ближайший в среду."""
    session = choose(kb, time="19:00", now=local(8, 10)).session

    assert session is not None
    assert (session.starts_at.day, session.weekday, session.discipline) == (9, "wed", "kickboxing")


def test_too_soon_moves_to_the_next_session(kb: KBSnapshot) -> None:
    """Среда, 18:00 — до занятия час, доехать не успеть. Записываем на пятницу."""
    session = choose(kb, time="19:00", now=local(9, 18)).session

    assert (session.starts_at.day, session.weekday) == (11, "fri")


def test_time_not_in_the_schedule_returns_options(kb: KBSnapshot) -> None:
    """Такого времени нет — ни выдумывать, ни подбирать похожее: предлагаем варианты."""
    choice = choose(kb, time="12:00", now=local(8, 10))

    assert choice.session is None and choice.problem == "unknown_time" and choice.options


def test_no_time_means_ask_the_parent(kb: KBSnapshot) -> None:
    assert choose(kb, time=None, now=local(8, 10)).problem == "need_time"


def test_both_sections_at_that_time_means_ask_which(kb: KBSnapshot) -> None:
    """В КСК по вторникам в 17:00 идут и бокс, и кикбоксинг — секцию выбирает родитель."""
    assert choose(kb, time="17:00", day="tue", now=local(8, 10)).problem == "need_discipline"

    session = choose(kb, time="17:00", day="tue", discipline="boxing", now=local(8, 10)).session
    assert (session.starts_at.day, session.discipline) == (8, "boxing")


def test_named_day_is_respected(kb: KBSnapshot) -> None:
    session = choose(kb, time="09:00", day="sat", discipline="boxing", now=local(8, 10)).session

    assert (session.starts_at.day, session.weekday) == (12, "sat")


def test_gym_without_schedule_is_reported(kb: KBSnapshot) -> None:
    gym = kb.gym(KSK).model_copy(update={"schedule": []})

    choice = resolve_trial_session(
        gym, time_text="19:00", day=None, discipline=None, now=local(8, 10), tz_name=TZ, min_lead_hours=2
    )

    assert choice.problem == "no_schedule"


def test_confirmation_is_complete_and_readable(kb: KBSnapshot) -> None:
    """Всё, что владелец перечислил: время, адрес, прийти заранее, подойти к тренеру, что взять."""
    session = choose(kb, time="19:00", now=local(8, 10)).session
    bring = next(entry for entry in kb.faq if entry.id == "gear_first_lesson").answer.ru

    text = render_trial_confirmation(
        kb, child="Иванов Али", gym=kb.gym(KSK), session=session, lang=Language.RU, bring=bring
    )

    for piece in (
        "Мы записали вас", "👤 Иванов Али", "Кикбоксинг", "В среду, 09.09 в 19:00", "Каирбекова 334",
        "за 10 минут", "подойдите к тренеру", "чешки",
    ):
        assert piece in text, f"нет «{piece}»:\n{text}"
    assert "администратор" not in text.lower()
    assert max(len(line) for line in text.splitlines()) <= 120


# --------------------------------------------------------------------------- #
# Что назвал сам клиент
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("time", "texts", "expected"),
    [
        ("19:00", ["В 19:00"], True),
        ("19:00", ["давайте в 7 вечера"], True),
        ("17:00", ["хочу во вторник в 17:00"], True),
        ("09:00", ["Иванов Али, 9 лет"], False),
        ("17:00", ["он в 5 классе"], False),
        ("17:00", ["Сериков Ержан, 8 лет"], False),
        ("19:00", [], False),
    ],
)
def test_time_counts_only_when_the_client_named_it(time, texts, expected) -> None:
    """Живой прогон: модель сама подставила «суббота 17:00» и записала ребёнка."""
    from app.kb.sessions import client_named_time

    assert client_named_time(time, texts) is expected


def test_section_counts_only_when_the_client_named_it() -> None:
    from app.kb.sessions import client_named_discipline

    assert client_named_discipline("boxing", ["На бокс"])
    assert not client_named_discipline("boxing", ["на кикбоксинг"]), "кикбоксинг — не бокс"
    assert client_named_discipline("kickboxing", ["на кикбоксинг"])
    assert not client_named_discipline("boxing", ["Сериков Ержан, 8 лет"])


def test_weekday_counts_only_when_the_client_named_it() -> None:
    from app.kb.sessions import client_named_day

    assert client_named_day("tue", ["хочу во вторник в 17:00"])
    assert client_named_day("sat", ["не во вторник, а в субботу"])
    assert client_named_day("tue", ["сейсенбі күні келеміз"])
    assert not client_named_day("sat", ["сейсенбі күні келеміз"]), "«сенбі» внутри «сейсенбі» — не суббота"
    assert not client_named_day("sat", ["Сериков Ержан, 8 лет"])
    assert not client_named_day(None, ["в субботу"])


def test_name_and_age_count_only_when_the_client_named_them() -> None:
    from app.kb.sessions import client_named_age, client_named_name

    assert client_named_name("Иванов Али", ["Иванов Али, 9 лет"])
    assert client_named_name("Иванов Али", ["али иванов"])
    assert client_named_name("Ержан", ["Erzhan, 8 let"])
    assert not client_named_name("Сериков Ержан", ["На бокс"])
    assert not client_named_name("Иванов Али", ["Али, 9 лет"]), "фамилию клиент не называл"
    assert client_named_age(9, ["Иванов Али, 9 лет"])
    assert client_named_age(7, ["ей семь лет"])
    assert not client_named_age(17, ["в 17:00"])
    assert not client_named_age(8, ["На бокс"])


def test_weekday_abbreviations_and_more_age_words() -> None:
    from app.kb.sessions import client_named_age, client_named_day

    assert client_named_day("mon", ["пн 19:00"])
    assert not client_named_day("sun", ["всё понятно"]), "«вс» внутри слова — не воскресенье"
    assert client_named_age(15, ["ему пятнадцать"])
    assert client_named_age(12, ["ұлым он екі жаста"])
    assert client_named_age(10, ["қызым он жаста"])
    assert not client_named_age(10, ["он хочет на бокс"]), "«он» без «жас» — местоимение"
    assert client_named_age(7, ["семилетний сын"])
    assert client_named_age(8, ["балам сегізде"])
