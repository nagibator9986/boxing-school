"""Карточка возврата: сегмент, статус, сверка с базой бота и отметка исхода.

Список берётся из разбора старых переписок WhatsApp (Excel, 21.10.2026): этап общения,
итог, препятствие. Писать можно далеко не всем из него — 141 из 212 сначала проверяет
человек: был ли ребёнок на пробном, оплатил ли счёт. Иначе бот предложил бы пробное
тому, кто уже занимается.

Правило всего модуля: решения только **в сторону осторожности**. Сверка с ботом может
отложить или исключить контакт, но никогда не делает «можно писать» сама — это решает
человек отметкой исхода или чистый разбор («интерес затих», проверка не нужна).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Final

__all__ = [
    "RECENT_DAYS",
    "BotFacts",
    "CampaignOutcome",
    "CampaignSegment",
    "CampaignStatus",
    "Decision",
    "apply_outcome",
    "initial_decision",
    "reconcile",
    "segment_for",
]


class CampaignStatus(str, Enum):
    """Где контакт в работе. Отправки на этапе 1 нет: дальше ``READY`` ничто не двигает."""

    NEEDS_CHECK = "needs_check"
    READY = "ready"
    DRAFT = "draft"
    APPROVED = "approved"
    SENT = "sent"
    REPLIED = "replied"
    BOOKED = "booked"
    DECLINED = "declined"
    NO_ANSWER = "no_answer"
    OPTED_OUT = "opted_out"
    EXCLUDED = "excluded"


class CampaignSegment(str, Enum):
    """О чём контакту писать — от этого зависит первое сообщение."""

    FADED = "faded"                    # интерес затих до записи
    NO_REPLY = "no_reply"              # школа не ответила на его вопрос
    BOOKED_UNKNOWN = "booked_unknown"  # записан, дошёл ли — неизвестно
    AFTER_TRIAL = "after_trial"        # был на пробном, купил ли — неизвестно
    NO_SHOW = "no_show"                # записан, но не пришёл (после проверки)
    INVOICE = "invoice"                # просил счёт, оплата неизвестна
    LATER = "later"                    # просил написать позже
    FORMER = "former"                  # занимался раньше
    LIVE = "live"                      # разговор идёт прямо сейчас
    OTHER_PROGRAM = "other_program"    # спрашивал не про бокс и кикбоксинг
    CURRENT_CLIENT = "current_client"  # уже занимается
    REFUSED = "refused"                # отказался
    NOT_CLIENT = "not_client"          # не клиентский диалог
    UNKNOWN = "unknown"                # этап, которого разбор не знал


class CampaignOutcome(str, Enum):
    """Что выяснил человек, проверяя контакт."""

    BOUGHT = "bought"          # купил абонемент / занимается
    CAME = "came"              # был на пробном, не купил
    NO_SHOW = "no_show"        # на пробное не пришёл
    WRITE = "write"            # писать можно, как есть
    LATER = "later"            # написать позже, с даты
    REFUSED = "refused"        # отказался
    NOT_CLIENT = "not_client"  # сотрудник, служебный или чужой номер


#: После этих статусов контакт ведёт отправка или он закрыт: сверка и импорт их не трогают.
_SETTLED: Final[frozenset[CampaignStatus]] = frozenset(
    {
        CampaignStatus.SENT,
        CampaignStatus.REPLIED,
        CampaignStatus.BOOKED,
        CampaignStatus.DECLINED,
        CampaignStatus.NO_ANSWER,
        CampaignStatus.OPTED_OUT,
        CampaignStatus.EXCLUDED,
    }
)

#: Статусы «писать можно или почти можно» — их сверка с ботом возвращает на проверку.
_WRITABLE: Final[frozenset[CampaignStatus]] = frozenset(
    {CampaignStatus.READY, CampaignStatus.DRAFT, CampaignStatus.APPROVED}
)

#: Сколько дней после последней переписки с ботом диалог считается свежим: такой клиент
#: и так в разговоре, и «вы интересовались у нас…» ему писать нельзя.
RECENT_DAYS: Final[int] = 7

#: Этапы из Excel разбора (колонка «Этап и статус») → сегмент.
_STAGE_SEGMENTS: Final[dict[str, CampaignSegment]] = {
    "интерес затих до записи": CampaignSegment.FADED,
    "записан - исход пробного неизвестен": CampaignSegment.BOOKED_UNKNOWN,
    "после пробного / бесплатного курса": CampaignSegment.AFTER_TRIAL,
    "запросил счет - оплата неизвестна": CampaignSegment.INVOICE,
    "просил вернуться позже": CampaignSegment.LATER,
    "бывший посетитель - возможен возврат": CampaignSegment.FORMER,
    "текущий диалог": CampaignSegment.LIVE,
    "другие занятия - проверить предложение": CampaignSegment.OTHER_PROGRAM,
    # Листы «Все контакты» и «Проверить контекст» того же файла.
    "действующий клиент / сообщил об оплате": CampaignSegment.CURRENT_CLIENT,
    "отказ / предложение не подходит": CampaignSegment.REFUSED,
    "не клиентский диалог": CampaignSegment.NOT_CLIENT,
}

#: Сегменты, которым не пишем вовсе, и почему.
_CLOSED_SEGMENTS: Final[dict[CampaignSegment, str]] = {
    CampaignSegment.LIVE: "Разговор идёт сейчас — его продолжает администратор",
    CampaignSegment.OTHER_PROGRAM: "Спрашивал о другой программе — это вопрос администратору",
    CampaignSegment.CURRENT_CLIENT: "Уже занимается",
    CampaignSegment.REFUSED: "Отказался",
    CampaignSegment.NOT_CLIENT: "Не клиентский диалог",
}

#: Школа сама не ответила на вопрос — такому клиенту сначала извинение (колонка
#: «Статус первого анализа»: «Компания не вернулась к запросу»).
_NO_REPLY_RE: Final[re.Pattern[str]] = re.compile(r"не вернулась к запросу|не ответил[аи]? клиенту", re.I)


def _stage_key(text: str | None) -> str:
    value = (text or "").lower().replace("ё", "е")
    value = re.sub(r"[—–‒-]", "-", value)
    value = re.sub(r"\s*-\s*", " - ", value)
    return re.sub(r"\s+", " ", value).strip()


def segment_for(stage: str | None, first_status: str | None = None) -> CampaignSegment:
    """Сегмент по этапу из разбора. Незнакомый этап — ``UNKNOWN``: его проверит человек."""
    segment = _STAGE_SEGMENTS.get(_stage_key(stage), CampaignSegment.UNKNOWN)
    if segment is CampaignSegment.FADED and _NO_REPLY_RE.search(first_status or ""):
        return CampaignSegment.NO_REPLY
    return segment


@dataclass(frozen=True, slots=True)
class Decision:
    """Новое состояние контакта и причина — её видит администратор."""

    status: CampaignStatus
    segment: CampaignSegment
    reason: str | None = None


def initial_decision(segment: CampaignSegment, *, check_required: bool) -> Decision:
    """Статус свежего контакта из разбора.

    Сразу «можно писать» — только «интерес затих» без пометки «нужна проверка»: про
    остальных неизвестно главное — занимается ли ребёнок уже.
    """
    if segment in _CLOSED_SEGMENTS:
        return Decision(CampaignStatus.EXCLUDED, segment, _CLOSED_SEGMENTS[segment])
    if segment in (CampaignSegment.FADED, CampaignSegment.NO_REPLY) and not check_required:
        return Decision(CampaignStatus.READY, segment)
    return Decision(CampaignStatus.NEEDS_CHECK, segment)


@dataclass(frozen=True, slots=True)
class BotFacts:
    """Что знает о номере сам бот: его база видит то, чего не видел разбор Excel."""

    conversation_id: str | None = None
    last_activity_at: datetime | None = None
    stop_word: bool = False
    lead_status: str | None = None
    trial_at: datetime | None = None
    human_pause: bool = False
    ignored: bool = False


def reconcile(
    status: CampaignStatus,
    segment: CampaignSegment,
    facts: BotFacts,
    *,
    now: datetime,
    checked: bool,
    recent_days: int = RECENT_DAYS,
) -> Decision | None:
    """Сверка с базой бота. ``None`` — менять нечего.

    ``checked`` — исход уже отметил человек: тогда работают только жёсткие запреты
    (стоп-слово, сотрудник, купил, пробное впереди), а «свежий диалог» и «записан ботом»
    не возвращают контакт на проверку, которую человек уже сделал.
    """
    if status in _SETTLED:
        return None
    if facts.ignored:
        return Decision(CampaignStatus.EXCLUDED, segment, "Номер в списке исключений бота: сотрудник или рабочий чат")
    if facts.stop_word:
        return Decision(CampaignStatus.OPTED_OUT, segment, "Писал боту стоп-слово — больше не пишем")
    if facts.lead_status == "converted":
        return Decision(CampaignStatus.EXCLUDED, segment, "По заявке в боте — купил абонемент")
    if facts.lead_status == "trial_booked" and facts.trial_at is not None and facts.trial_at > now:
        return Decision(
            CampaignStatus.EXCLUDED, segment, f"Записан ботом на пробное {_local_day(facts.trial_at)} — оно ещё впереди"
        )
    if checked or status not in (_WRITABLE | {CampaignStatus.NEEDS_CHECK}):
        return None
    if facts.lead_status == "trial_booked" and facts.trial_at is not None:
        return Decision(
            CampaignStatus.NEEDS_CHECK,
            CampaignSegment.BOOKED_UNKNOWN,
            f"Записан ботом на пробное {_local_day(facts.trial_at)} — проверить, пришёл ли",
        )
    if facts.human_pause:
        return Decision(CampaignStatus.NEEDS_CHECK, segment, "В диалоге с ботом отвечает администратор")
    if facts.last_activity_at is not None and now - facts.last_activity_at < timedelta(days=recent_days):
        return Decision(
            CampaignStatus.NEEDS_CHECK,
            segment,
            f"Переписывался с ботом {_local_day(facts.last_activity_at)} — диалог свежий",
        )
    return None


def apply_outcome(
    outcome: CampaignOutcome, segment: CampaignSegment, *, not_before: datetime | None = None
) -> Decision:
    """Что делать с контактом после проверки человеком."""
    if outcome is CampaignOutcome.BOUGHT:
        return Decision(CampaignStatus.EXCLUDED, segment, "Купил абонемент / занимается")
    if outcome is CampaignOutcome.REFUSED:
        return Decision(CampaignStatus.EXCLUDED, segment, "Отказался")
    if outcome is CampaignOutcome.NOT_CLIENT:
        return Decision(CampaignStatus.EXCLUDED, segment, "Не клиент: сотрудник, служебный или чужой номер")
    if outcome is CampaignOutcome.CAME:
        return Decision(CampaignStatus.READY, CampaignSegment.AFTER_TRIAL, "Был на пробном, абонемент не купил")
    if outcome is CampaignOutcome.NO_SHOW:
        return Decision(CampaignStatus.READY, CampaignSegment.NO_SHOW, "Записывался, но на пробное не пришёл")
    if outcome is CampaignOutcome.LATER:
        if not_before is None:
            raise ValueError("для «позже» нужна дата")
        return Decision(CampaignStatus.READY, segment, f"Написать не раньше {_local_day(not_before)}")
    return Decision(CampaignStatus.READY, segment, "Проверено: писать можно")


def _local_day(value: datetime) -> str:
    """Дата по часам школы — так её читает администратор."""
    from zoneinfo import ZoneInfo

    from app.config import get_settings

    try:
        zone = ZoneInfo(get_settings().timezone)
    except Exception:  # noqa: BLE001 - кривое имя зоны не повод падать
        zone = ZoneInfo("Asia/Almaty")
    return value.astimezone(zone).strftime("%d.%m")
