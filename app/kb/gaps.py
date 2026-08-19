"""Реестр пробелов в данных и честные фразы-заглушки.

Пробел — это вопрос, который клиент задаёт, а ответа в базе знаний нет. Таких
вопросов у школы пятнадцать (G-1…G-15) плюс три нерешённых конфликта данных
(C-3, C-4, C-5). Модуль отвечает на три вопроса:

* **есть ли данные по теме** — :func:`has_data`, :func:`has_data_for_topic`;
* **что сказать, если данных нет** — :func:`say_no_data`, :func:`say_no_data_lang`;
* **какой пробел стоит за темой вопроса** — :func:`gap_for_topic`.

Юридических блокеров запуска в проекте нет: ни один пробел не мешает боту
работать, он лишь заставляет честно передать вопрос администратору
(:func:`is_blocking` всегда ``False``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, Mapping

from app.types import GapRef, Language, Plan, Scope
from app.kb.models import KBSnapshot

Priority = Literal["critical", "high", "medium"]


@dataclass(frozen=True, slots=True)
class GapInfo:
    """Одна строка реестра пробелов — то, что видит владелец школы в отчёте."""

    ref: GapRef
    title_ru: str
    where_ru: str
    question_ru: str
    priority: Priority
    i18n_key: str
    topics: tuple[str, ...] = ()
    faq_ids: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Соответствие пробел -> фраза
# --------------------------------------------------------------------------- #
#: Замороженная часть контракта (INTERFACES §8.4).
GAP_TO_I18N: Final[dict[GapRef, str]] = {
    GapRef.G1: "gap.schedule",
    GapRef.G2: "gap.contacts",
    GapRef.G3: "gap.region_address",
    GapRef.G4: "gap.trial_conditions",
    GapRef.G5: "gap.gear",
    GapRef.G6: "gap.docs",
    GapRef.G8: "gap.coaches",
    GapRef.G9: "gap.payment",
    GapRef.C3: "gap.kzhbi",
}

#: Дополнение к контракту: у остальных пробелов тоже должна быть внятная фраза,
#: иначе бот скажет общее «уточню у администратора» там, где можно точнее.
GAP_TO_I18N_EXTRA: Final[dict[GapRef, str]] = {
    GapRef.G7: "gap.age_limits",
    GapRef.G10: "gap.region_plans",
    GapRef.G11: "gap.enrollment",
    GapRef.G12: "gap.results",
    GapRef.G13: "gap.freeze",
    GapRef.G14: "gap.documents",
    GapRef.G15: "gap.location",
    GapRef.C4: "gap.payment",
    GapRef.C5: "gap.payment",
}

#: Фраза на случай пробела без собственного текста.
GAP_FALLBACK_I18N: Final[str] = "gap.generic"

#: Ключи i18n, которые произносит код вне пробелов. Их отсутствие обязано
#: вскрываться на загрузке KB, а не в живом диалоге.
CORE_I18N_KEYS: Final[tuple[str, ...]] = (
    "greeting.first",
    "escalation.handoff",
    "escalation.child_writing",
    "error.generic",
    "error.voice_message",
    "error.too_many_messages",
    "bridge.kk_offer",
    "lead_card.trial_booked",
    "lead_card.escalation",
    "system.lead_saved",
    "system.goodbye",
)


# --------------------------------------------------------------------------- #
# Реестр
# --------------------------------------------------------------------------- #
GAP_REGISTRY: Final[dict[GapRef, GapInfo]] = {
    GapRef.G1: GapInfo(
        ref=GapRef.G1,
        title_ru="Расписание тренировок",
        where_ru="gyms.yaml → gyms[].schedule",
        question_ru="Во сколько тренировки? Есть группа для шестилетнего утром?",
        priority="critical",
        i18n_key="gap.schedule",
        topics=("age_groups", "sessions_count"),
        faq_ids=("age_groups_split", "sessions_per_week", "session_duration"),
    ),
    GapRef.G2: GapInfo(
        ref=GapRef.G2,
        title_ru="Телефон администратора",
        where_ru="gyms.yaml → gyms[].phone",
        question_ru="Как с вами связаться? Дайте номер.",
        priority="critical",
        i18n_key="gap.contacts",
        topics=("contacts",),
        faq_ids=("contacts_phone",),
    ),
    GapRef.G3: GapInfo(
        ref=GapRef.G3,
        title_ru="Адреса секций в райцентрах",
        where_ru="gyms.yaml → gyms[].address у шести райцентров",
        question_ru="Где именно в Житикаре?",
        priority="high",
        i18n_key="gap.region_address",
        topics=("contacts",),
        faq_ids=("region_address",),
    ),
    GapRef.G4: GapInfo(
        ref=GapRef.G4,
        title_ru="Условия бесплатного пробного",
        where_ru="faq.yaml → trial_conditions",
        question_ru="Сколько раз бесплатно? Нужна ли запись заранее?",
        priority="critical",
        i18n_key="gap.trial_conditions",
        topics=("trial",),
        faq_ids=("trial_conditions",),
    ),
    GapRef.G5: GapInfo(
        ref=GapRef.G5,
        title_ru="Экипировка и что взять на занятие",
        where_ru="faq.yaml → gear_first_lesson, gear_cost",
        question_ru="Что взять на первое занятие? Сколько стоит экипировка?",
        priority="high",
        i18n_key="gap.gear",
        topics=("gear",),
        faq_ids=("gear_first_lesson", "gear_cost"),
    ),
    GapRef.G6: GapInfo(
        ref=GapRef.G6,
        title_ru="Медицинская справка",
        where_ru="faq.yaml → docs_medical",
        question_ru="Нужна справка? Какая именно?",
        priority="high",
        i18n_key="gap.docs",
        topics=("docs",),
        faq_ids=("docs_medical",),
    ),
    GapRef.G7: GapInfo(
        ref=GapRef.G7,
        title_ru="Верхний возраст, группы для девочек и взрослых",
        where_ru="faq.yaml → girls_groups, adults_groups",
        question_ru="Дочке 9, возьмёте? А мне 30, можно?",
        priority="high",
        i18n_key="gap.age_limits",
        topics=("girls", "adults"),
        faq_ids=("girls_groups", "adults_groups"),
    ),
    GapRef.G8: GapInfo(
        ref=GapRef.G8,
        title_ru="Тренеры залов",
        where_ru="gyms.yaml → gyms[].coaches, faq.yaml → coaches_info",
        question_ru="Кто тренер? Какой у него опыт?",
        priority="medium",
        i18n_key="gap.coaches",
        topics=("coaches",),
        faq_ids=("coaches_info", "coaches_language"),
    ),
    GapRef.G9: GapInfo(
        ref=GapRef.G9,
        title_ru="Способы оплаты и реквизиты",
        where_ru="pricing.yaml → payment_methods, payment_details",
        question_ru="Каспи принимаете? Как оплатить?",
        priority="high",
        i18n_key="gap.payment",
        topics=("payment",),
        faq_ids=("payment_methods", "payment_installments"),
    ),
    GapRef.G10: GapInfo(
        ref=GapRef.G10,
        title_ru="Гибкий тариф и разовые в райцентрах",
        where_ru="pricing.yaml → region_plans.flexible, region_single",
        question_ru="А разово в Фёдоровке можно?",
        priority="medium",
        i18n_key="gap.region_plans",
        topics=("payment",),
    ),
    GapRef.G11: GapInfo(
        ref=GapRef.G11,
        title_ru="Идёт ли набор, есть ли места, лето",
        where_ru="faq.yaml → enrollment_now, summer_schedule",
        question_ru="Сейчас набор идёт? Летом работаете?",
        priority="medium",
        i18n_key="gap.enrollment",
        topics=("sessions_count", "summer"),
        faq_ids=("enrollment_now", "summer_schedule"),
    ),
    GapRef.G12: GapInfo(
        ref=GapRef.G12,
        title_ru="Достижения учеников и соревнования",
        where_ru="faq.yaml → results_achievements, competitions_participation",
        question_ru="А результаты есть? Когда соревнования?",
        priority="medium",
        i18n_key="gap.results",
        topics=("results", "competitions"),
        faq_ids=("results_achievements", "competitions_participation"),
    ),
    GapRef.G13: GapInfo(
        ref=GapRef.G13,
        title_ru="Заморозка абонемента",
        where_ru="pricing.yaml → freeze_policy",
        question_ru="Ребёнок заболел на две недели — что с абонементом?",
        priority="medium",
        i18n_key="gap.freeze",
        topics=("freeze",),
        faq_ids=("freeze_policy",),
    ),
    GapRef.G14: GapInfo(
        ref=GapRef.G14,
        title_ru="Договор и публичные условия",
        where_ru="faq.yaml → offer_documents, media.yaml → offer_and_policy",
        question_ru="Договор заключаете? Где почитать условия?",
        priority="medium",
        i18n_key="gap.documents",
        topics=("offer",),
        faq_ids=("offer_documents",),
    ),
    GapRef.G15: GapInfo(
        ref=GapRef.G15,
        title_ru="Геометки залов",
        where_ru="gyms.yaml → geo_lat, geo_lon, map_url",
        question_ru="Скиньте локацию.",
        priority="medium",
        i18n_key="gap.location",
    ),
    GapRef.C3: GapInfo(
        ref=GapRef.C3,
        title_ru="Конфликт: зал на КЖБИ",
        where_ru="gyms.yaml → unresolved_kzhbi",
        question_ru="У вас есть зал на КЖБИ?",
        priority="high",
        i18n_key="gap.kzhbi",
        topics=("contacts",),
        faq_ids=("contacts_kzhbi",),
    ),
    GapRef.C4: GapInfo(
        ref=GapRef.C4,
        title_ru="Конфликт: семейная скидка на гибкий тариф",
        where_ru="pricing.yaml → city_family_discount.applies_to_status",
        question_ru="Скидка на второго ребёнка действует и на гибкий абонемент?",
        priority="high",
        i18n_key="gap.payment",
        topics=("payment",),
    ),
    GapRef.C5: GapInfo(
        ref=GapRef.C5,
        title_ru="Конфликт: от какого тарифа считается «второй ребёнок»",
        where_ru="pricing.yaml → city_family_discount.base_rule_status",
        question_ru="Дети на разных абонементах — от какого считается скидка?",
        priority="medium",
        i18n_key="gap.payment",
        topics=("payment",),
    ),
}

#: Тема FAQ -> пробел, который за ней стоит.
_TOPIC_TO_GAP: Final[dict[str, GapRef]] = {
    "trial": GapRef.G4,
    "docs": GapRef.G6,
    "gear": GapRef.G5,
    "payment": GapRef.G9,
    "freeze": GapRef.G13,
    "coaches": GapRef.G8,
    "results": GapRef.G12,
    "competitions": GapRef.G12,
    "girls": GapRef.G7,
    "adults": GapRef.G7,
    "contacts": GapRef.G2,
    "offer": GapRef.G14,
    "sessions_count": GapRef.G1,
    "age_groups": GapRef.G1,
    "summer": GapRef.G11,
}


# --------------------------------------------------------------------------- #
# Публичный интерфейс
# --------------------------------------------------------------------------- #
def i18n_key_for(gap: GapRef) -> str:
    """Ключ фразы-заглушки для пробела; для неизвестного — общая фраза."""
    return GAP_TO_I18N.get(gap) or GAP_TO_I18N_EXTRA.get(gap) or GAP_FALLBACK_I18N


def required_i18n_keys() -> tuple[str, ...]:
    """Ключи i18n, обязательные к присутствию в KB. Проверяется загрузчиком."""
    keys = set(GAP_TO_I18N.values()) | set(GAP_TO_I18N_EXTRA.values())
    keys.add(GAP_FALLBACK_I18N)
    keys.update(CORE_I18N_KEYS)
    return tuple(sorted(keys))


def say_no_data(snapshot: KBSnapshot, gap: GapRef) -> dict[str, str]:
    """``{'ru': ..., 'kk': ...}`` — готовая фраза-заглушка для ``say_if_no_data``."""
    return snapshot.bilingual_text(i18n_key_for(gap))


def say_no_data_lang(snapshot: KBSnapshot, gap: GapRef, lang: Language) -> str:
    """Фраза-заглушка на языке диалога."""
    return snapshot.text(i18n_key_for(gap), lang)


def gap_for_topic(topic: str) -> GapRef | None:
    """Пробел, стоящий за темой FAQ; ``None`` — по теме данные есть."""
    return _TOPIC_TO_GAP.get(topic)


def is_blocking(gap: GapRef) -> bool:
    """Всегда ``False``: юридических блокеров запуска в проекте нет."""
    return False


def info(gap: GapRef) -> GapInfo | None:
    """Карточка пробела для отчёта владельцу."""
    return GAP_REGISTRY.get(gap)


def all_gaps() -> tuple[GapRef, ...]:
    """Весь реестр в порядке G-1…G-15, затем конфликты C-*."""
    return tuple(sorted(GAP_REGISTRY, key=_sort_key))


def has_data(snapshot: KBSnapshot, gap: GapRef) -> bool:
    """Появились ли в KB данные, закрывающие этот пробел.

    Проверяются сами поля базы, а не пометки ``gap_refs``: владелец заполняет
    расписание и телефон, а не список пробелов, и отчёт обязан это увидеть.
    """
    probe = _PROBES.get(gap)
    return bool(probe(snapshot)) if probe is not None else False


def has_data_for_topic(snapshot: KBSnapshot, topic: str) -> bool:
    """Есть ли в KB готовый ответ по теме (хотя бы одна заполненная запись FAQ)."""
    return any(entry.answered for entry in snapshot.faq if entry.topic == topic)


def open_gaps(snapshot: KBSnapshot) -> tuple[GapRef, ...]:
    """Пробелы, которые всё ещё открыты: данных в KB нет."""
    return tuple(gap for gap in all_gaps() if not has_data(snapshot, gap))


def closed_gaps(snapshot: KBSnapshot) -> tuple[GapRef, ...]:
    """Пробелы, закрытые владельцем: данные появились."""
    return tuple(gap for gap in all_gaps() if has_data(snapshot, gap))


# --------------------------------------------------------------------------- #
# Пробы «есть ли данные»
# --------------------------------------------------------------------------- #
def _faq_answered(snapshot: KBSnapshot, *ids: str) -> bool:
    wanted = set(ids)
    found = [entry for entry in snapshot.faq if entry.id in wanted]
    return bool(found) and all(entry.answered for entry in found)


_PROBES: Final[Mapping[GapRef, object]] = {
    GapRef.G1: lambda s: any(gym.schedule for gym in s.active_gyms()),
    GapRef.G2: lambda s: any(gym.phone for gym in s.active_gyms()),
    GapRef.G3: lambda s: all(
        gym.address.filled for gym in s.active_gyms(Scope.REGION)
    ) and bool(s.active_gyms(Scope.REGION)),
    GapRef.G4: lambda s: _faq_answered(s, "trial_conditions"),
    GapRef.G5: lambda s: _faq_answered(s, "gear_first_lesson", "gear_cost"),
    GapRef.G6: lambda s: _faq_answered(s, "docs_medical"),
    GapRef.G7: lambda s: _faq_answered(s, "girls_groups", "adults_groups"),
    GapRef.G8: lambda s: any(gym.coaches for gym in s.active_gyms()),
    GapRef.G9: lambda s: bool(s.pricing.payment_methods),
    GapRef.G10: lambda s: s.pricing.region_plans.get(Plan.FLEXIBLE.value) is not None
    or s.pricing.region_single is not None,
    GapRef.G11: lambda s: _faq_answered(s, "enrollment_now", "summer_schedule"),
    GapRef.G12: lambda s: _faq_answered(s, "results_achievements", "competitions_participation"),
    GapRef.G13: lambda s: s.pricing.freeze_policy is not None and s.pricing.freeze_policy.filled,
    GapRef.G14: lambda s: _faq_answered(s, "offer_documents"),
    GapRef.G15: lambda s: any(
        gym.geo_lat is not None or gym.map_url for gym in s.active_gyms(Scope.CITY)
    ),
    GapRef.C3: lambda s: not s.unresolved_gyms(),
    GapRef.C4: lambda s: s.pricing.city_family_discount.applies_to_status == "confirmed",
    GapRef.C5: lambda s: s.pricing.city_family_discount.base_rule_status == "confirmed",
}


def _sort_key(gap: GapRef) -> tuple[str, int]:
    letter, _, number = gap.value.partition("-")
    return (letter, int(number))


__all__ = [
    "CORE_I18N_KEYS",
    "GAP_FALLBACK_I18N",
    "GAP_REGISTRY",
    "GAP_TO_I18N",
    "GAP_TO_I18N_EXTRA",
    "GapInfo",
    "all_gaps",
    "closed_gaps",
    "gap_for_topic",
    "has_data",
    "has_data_for_topic",
    "i18n_key_for",
    "info",
    "is_blocking",
    "open_gaps",
    "required_i18n_keys",
    "say_no_data",
    "say_no_data_lang",
]
