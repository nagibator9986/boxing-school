"""Данные школы, которые владелец подтвердил или поправил 10.09.2026.

Каждый тест — одно его замечание по живой переписке. Если данные в базе
случайно откатятся, бот снова начнёт говорить клиентам старое.
"""

from __future__ import annotations

from app.kb.models import KBSnapshot
from app.kb.render import render_price_card, render_schedule_card
from app.types import FollowupKind, Language, Scope


def _days(slot) -> list[str]:
    return [str(getattr(day, "value", day)) for day in slot.days]


def test_ksk_kickboxing_mon_wed_fri_and_discounted_boxing_tue_thu_sat(kb: KBSnapshot) -> None:
    """Владелец 12.10.2026: «Пн, Ср, Пт — кикбоксинг, абонемент 25 000 как был. Вт, Чт, Сб —
    бокс, утром в 9:00 и вечером в 17:00 — по 20 000, чтобы набрался трафик»."""
    gym = kb.gym("ksk_kairbekova_334")
    by_days: dict[tuple[str, ...], set[tuple[str, str, int | None]]] = {}
    for slot in gym.schedule:
        by_days.setdefault(tuple(_days(slot)), set()).add((slot.discipline, slot.time_start, slot.price))

    assert by_days[("mon", "wed", "fri")] == {
        ("kickboxing", "09:00", None), ("kickboxing", "17:00", None), ("kickboxing", "19:00", None),
    }
    assert by_days[("tue", "thu", "sat")] == {("boxing", "09:00", 20000), ("boxing", "17:00", 20000)}


def test_kasymkhanova_and_tobyl_new_hours(kb: KBSnapshot) -> None:
    """Владелец 12.10.2026: Касымханова 18:00–19:00, Тобыл 19:30–21:00."""
    assert {(s.time_start, s.time_end) for s in kb.gym("center_kasymkhanova_10").schedule} == {("18:00", "19:00")}
    assert {(s.time_start, s.time_end) for s in kb.gym("region_tobyl").schedule} == {("19:30", "21:00")}


def test_what_to_bring_is_answered_with_the_owners_text(kb: KBSnapshot) -> None:
    """Бот предложил рассказать, что взять с собой, а ответа в базе не было.

    На «Да» клиент получил «здесь лучше ответит администратор». Текст дал сам
    владелец.
    """
    entry = next(item for item in kb.faq if item.id == "gear_first_lesson")

    # Владелец 01.10.2026: «Сообщение должно быть такое» — дословно, с эмодзи.
    assert entry.answer.ru == (
        "На первую тренировку ничего специального покупать заранее не нужно.\n\n"
        "С собой необходимо взять:\n\n"
        "👕 удобную спортивную одежду;\n"
        "👟 чешки или носочки;\n"
        "💧 воду;\n\n"
        "Не переживайте: у нас дружная атмосфера, и на первой тренировке никто никого не бьёт.\n"
        "Ребёнок познакомится с тренером и ребятами и попробует себя в новом деле — будет интересно! 😊"
    )
    # «Прийти пораньше» уже есть в подтверждении записи — второй раз владелец просил не повторять.
    assert "заранее и настроиться" not in entry.answer.ru
    for mark in ("👕", "👟", "💧", "😊"):
        assert mark in entry.answer.kk, f"в казахском тексте нет «{mark}»"


def test_flexible_plan_explains_when_recalculation_applies(kb: KBSnapshot) -> None:
    """«С перерасчётом — если заранее предупреждают, и больничные по справке».

    За 25 000 ₸ перерасчёта нет никакого.
    """
    flexible = kb.pricing.city_plans["flexible"].note.ru.lower()
    standard = kb.pricing.city_plans["standard"].note.ru.lower()

    assert "заранее" in flexible and "справк" in flexible
    assert "нет" in standard


def test_price_card_lines_fit_a_phone_screen(kb: KBSnapshot) -> None:
    """Условие перерасчёта длинное — одной строкой оно превращалось в простыню."""
    card = render_price_card(kb, scope=Scope.CITY, lang=Language.RU)

    assert max(len(line) for line in card.splitlines()) <= 120


def test_no_show_message_is_disabled(kb: KBSnapshot) -> None:
    """«Вчера вас не было на пробном» получили бы и те, кто пришёл.

    Бот не знает, пришёл ли ребёнок, поэтому владелец это сообщение отключил.
    Напоминания до занятия остаются.
    """
    events = {rule.event for rule in kb.policies.followup_policy}

    assert FollowupKind.NO_SHOW not in events
    # Владелец 22.09.2026: «если записан на пятницу, в пятницу утром напомнить».
    assert FollowupKind.TRIAL_REMINDER_MORNING in events
    assert FollowupKind.TRIAL_REMINDER_20H not in events, "ночное «завтра» заменено утренним"
    assert FollowupKind.TRIAL_REMINDER_2H in events


def test_accepted_age_names_boys_and_girls(kb: KBSnapshot) -> None:
    """«Принимаем детей с 5 лет, мальчики и девочки» — без «возрастных групп нет»."""
    gym = kb.gym("ksk_kairbekova_334")
    text = render_schedule_card(kb, gym_id=gym.id, slots=gym.schedule, lang=Language.RU)

    assert "мальчиков, и девочек" in text
    assert "возрастных групп" not in text


def test_faq_never_promises_to_pass_a_message_to_the_coach(kb: KBSnapshot) -> None:
    """Связи с тренерами у бота нет — и постфильтр такие обещания снимает.

    Ответ FAQ про пропуск тренировки обещал «передам тренеру», то есть база
    знаний противоречила собственному фильтру.
    """
    for entry in kb.faq:
        for text in (entry.answer.ru, entry.answer.kk):
            assert "передам тренеру" not in (text or "").lower(), entry.id


def test_tobyl_is_priced_and_listed_as_the_city(kb: KBSnapshot) -> None:
    """«Цены ниже городских — не надо, у нас одна цена».

    В базе Тобыл стоял районным центром с абонементом 10 000 ₸ — бот называл
    клиентам из Тобыла втрое меньшую цену.
    """
    tobyl = kb.gym("region_tobyl")

    assert tobyl.scope is Scope.CITY
    assert "Тобыл" in kb.gyms.city_suburbs
    assert "Тобыл" not in kb.pricing.region_settlements
