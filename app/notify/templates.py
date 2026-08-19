"""Тексты, которые получает администратор школы.

Карточка — это не отчёт, а первая фраза телефонного звонка. Правила формата
(INTERFACES §14.3, research-funnel §6.2) жёсткие и проверяются глазами
администратора, а не тестом:

* заголовок капсом, ровно одна строка — единственное место, где капс разрешён;
* максимум :data:`MAX_CARD_LINES` строк, всё остальное живёт в БД;
* **пустые поля не печатаются вовсе** — «Имя родителя: не указано» это мусор,
  который отнимает строку у полезного;
* телефон отдельной строкой, начинается с ``+7``, без скобок и дефисов, иначе
  WhatsApp не сделает из него ссылку ``tel:``;
* никакого markdown: WhatsApp и Instagram его не рендерят, звёздочки уйдут как есть;
* карточка всегда по-русски — её читает сотрудник; строка ``Язык:`` подсказывает,
  на каком языке звонить родителю.

Заголовки берутся из ``kb/i18n.yaml`` (группа ``lead_card``), чтобы владелец мог
их переписать без правки кода: из шаблона используется **первая строка**, тело
собирается кодом — иначе не выполнить правило «пустые поля не печатать».
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.kb.models import KBSnapshot
from app.types import (
    ChannelKind,
    EscalationReason,
    Gender,
    Language,
    LeadDraft,
    LeadStatus,
    MAX_MESSAGE_CHARS,
    PhoneSource,
)

#: Больше 12 строк с телефона не читаются за пять секунд.
MAX_CARD_LINES: Final[int] = 12

#: Вопрос клиента в карточке эскалации: длиннее — это уже переписка, а не вопрос.
MAX_QUESTION_CHARS: Final[int] = 220

#: Значения полей режем, чтобы одно многословное поле не съело всю карточку.
MAX_FIELD_CHARS: Final[int] = 120

#: Фолбэки заголовков: KB может быть недогружена ровно в тот момент, когда
#: администратору нужна тревога. Тексты дублируют kb/i18n.yaml.
_FALLBACK_HEADERS: Final[dict[str, str]] = {
    "lead_card.trial_booked": "НОВАЯ ЗАПИСЬ НА ПРОБНОЕ",
    "lead_card.thinking": "ЛИД БЕЗ ЗАПИСИ — ДУМАЕТ",
    "lead_card.escalation": "НУЖЕН ЖИВОЙ ОТВЕТ",
    "lead_card.alert": "ТЕХНИЧЕСКАЯ ТРЕВОГА",
}

#: Заголовок карточки по статусу лида.
_STATUS_KEY: Final[dict[LeadStatus, str]] = {
    LeadStatus.TRIAL_BOOKED: "lead_card.trial_booked",
    LeadStatus.THINKING: "lead_card.thinking",
    LeadStatus.NEEDS_CALL: "lead_card.thinking",
    LeadStatus.ESCALATED: "lead_card.escalation",
    LeadStatus.NO_SHOW: "lead_card.thinking",
    LeadStatus.NOT_TARGET: "lead_card.thinking",
    LeadStatus.CONVERTED: "lead_card.trial_booked",
}

#: Язык клиента словами: администратор должен звонить на языке родителя.
_LANG_TITLE: Final[dict[Language, str]] = {
    Language.RU: "русский",
    Language.KK: "казахский — звонить на казахском",
}

_GENDER_TITLE: Final[dict[Gender, str]] = {Gender.M: "м", Gender.F: "ж"}

_CHANNEL_TITLE: Final[dict[ChannelKind, str]] = {
    ChannelKind.WHATSAPP: "WhatsApp",
    ChannelKind.INSTAGRAM: "Instagram",
    ChannelKind.TELEGRAM: "Telegram",
}

#: Причина эскалации по-русски. Это не факт о школе, а служебная подпись.
_REASON_TITLE: Final[dict[EscalationReason, str]] = {
    EscalationReason.USER_REQUEST: "клиент просит человека",
    EscalationReason.NO_DATA: "нет данных в базе знаний",
    EscalationReason.COMPLAINT: "жалоба",
    EscalationReason.MEDICAL: "вопрос про здоровье ребёнка",
    EscalationReason.PRICE_OFF_LIST: "цена вне прайса",
    EscalationReason.INSTALLMENTS: "рассрочка или скидка",
    EscalationReason.AGE_OUT_OF_RANGE: "возраст вне групп",
    EscalationReason.FOREIGN_LANGUAGE: "пишут не на русском и не на казахском",
    EscalationReason.REPEATED_MISS: "бот дважды не смог ответить",
    EscalationReason.POSTCHECK_FAIL: "ответ бота не прошёл проверку фактов",
    EscalationReason.LLM_FAILURE: "сбой модели",
    EscalationReason.INJECTION: "попытка подмены инструкций бота",
    EscalationReason.CHILD_WRITING: "за клавиатурой ребёнок",
    EscalationReason.OFF_TOPIC: "разговор не про школу",
    EscalationReason.BUDGET_GUARD: "исчерпан суточный лимит модели",
}


def render_lead_card(
    lead: LeadDraft,
    *,
    kb: KBSnapshot,
    gym_title: str | None,
    channel: ChannelKind,
    dialog_url: str | None,
    now: datetime,
) -> str:
    """Карточка лида для администратора.

    Заголовок капсом одной строкой, максимум 12 строк, без markdown.
    Пустые поля НЕ печатаются вовсе. Телефон — отдельной строкой, начинается
    с ``+7``, без скобок и дефисов, чтобы распознавался как ``tel:``.
    Карточка всегда по-русски: её читает сотрудник; поле ``Язык:`` говорит,
    на каком языке звонить.
    """
    header = _header(kb, _STATUS_KEY.get(lead.status, "lead_card.thinking"))
    lines: list[str] = []

    _add(lines, "Ребёнок", _child_line(lead))
    _add(lines, "Родитель", _parent_line(lead))
    _add(lines, "Телефон", format_phone(lead.phone))
    _add(lines, "Язык", _LANG_TITLE.get(lead.lang) if lead.lang else None)
    _add(lines, "Зал", gym_title or lead.gym_id)
    _add(lines, "Район", lead.district if not gym_title else None)
    _add(lines, "Когда", lead.trial_slot_text or _format_slot(lead.trial_slot))
    _add(lines, "Мотив", lead.motivation)
    _add(lines, "Тревога", lead.main_objection)
    _add(lines, "Опыт", lead.prior_experience)
    _add(lines, "Здоровье", lead.health_notes)
    _add(lines, "Канал", _channel_line(lead, channel=channel, now=now))
    _add(lines, "Диалог", dialog_url or lead.dialog_url)

    if lead.phone is None and lead.phone_source is PhoneSource.NONE:
        # Без телефона администратору придётся отвечать в переписке — это надо видеть сразу.
        _add(lines, "Связь", "номера нет, отвечать в переписке")

    return _assemble(header, lines)


def render_escalation_card(
    *,
    reason: EscalationReason,
    question: str,
    lang: Language,
    phone: str | None,
    channel: ChannelKind,
    dialog_url: str | None,
    now: datetime,
) -> str:
    """Карточка эскалации: почему бот замолчал и что именно спросили.

    Формат тот же, что у лид-карточки. Вопрос клиента приводится дословно —
    администратор должен видеть формулировку, а не пересказ бота.
    """
    header = _header(None, "lead_card.escalation")
    lines: list[str] = []

    _add(lines, "Телефон", format_phone(phone))
    _add(lines, "Вопрос", _clip(question, MAX_QUESTION_CHARS))
    _add(lines, "Язык", _LANG_TITLE.get(lang, _LANG_TITLE[Language.RU]))
    _add(lines, "Причина", _REASON_TITLE.get(reason, reason.value))
    _add(lines, "Канал", f"{_CHANNEL_TITLE.get(channel, channel.value)}, {_format_dt(now)}")
    _add(lines, "Диалог", dialog_url)
    lines.append("Бот не отвечал по существу, ждут вас.")

    return _assemble(header, lines)


def render_alert(text: str, *, code: str) -> str:
    """Техническая тревога: канал неисправен, спам-блок, KB не загрузилась, бюджет исчерпан.

    Читает тот же человек, что и лид-карточки, поэтому форма та же: заголовок,
    что случилось, когда и код для разработчика.
    """
    header = _header(None, "lead_card.alert")
    lines: list[str] = []
    _add(lines, "Что случилось", _clip(text, MAX_QUESTION_CHARS))
    _add(lines, "Код", code)
    _add(lines, "Когда", _format_dt(_now()))
    return _assemble(header, lines)


def format_phone(phone: str | None) -> str | None:
    """``+77051234567`` → ``+7 705 123 45 67``.

    Скобок и дефисов нет намеренно: WhatsApp распознаёт такую строку как ``tel:``,
    а с ними — не всегда. Нераспознанный формат возвращается как есть.
    """
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        return f"+7 {digits[1:4]} {digits[4:7]} {digits[7:9]} {digits[9:11]}"
    return phone.strip() or None


# --------------------------------------------------------------------------- #
# Внутреннее
# --------------------------------------------------------------------------- #
def _header(kb: KBSnapshot | None, key: str) -> str:
    """Первая строка шаблона из ``kb/i18n.yaml``; при любой проблеме — фолбэк."""
    fallback = _FALLBACK_HEADERS.get(key, "СООБЩЕНИЕ БОТА")
    if kb is None:
        return fallback
    try:
        template = kb.text(key, Language.RU)
    except Exception:  # noqa: BLE001 - карточка важнее, чем причина промаха по ключу
        return fallback
    first = (template or "").strip().splitlines()
    return first[0].strip() if first and first[0].strip() else fallback


def _add(lines: list[str], label: str, value: str | None) -> None:
    """Добавляет строку ``Метка: значение``; пустое значение не печатается вовсе."""
    if value is None:
        return
    text = " ".join(str(value).split())
    if not text:
        return
    lines.append(f"{label}: {_clip(text, MAX_FIELD_CHARS)}")


def _assemble(header: str, lines: list[str]) -> str:
    """Собирает карточку: заголовок, пустая строка, тело; режет по лимитам."""
    body = lines[: max(0, MAX_CARD_LINES - 2)]
    card = "\n".join([header, "", *body]).strip()
    if len(card) > MAX_MESSAGE_CHARS:
        card = card[: MAX_MESSAGE_CHARS - 1].rstrip() + "…"
    return card


def _child_line(lead: LeadDraft) -> str | None:
    """``Алихан, 8 лет (м)`` — имя, возраст, пол; чего нет, то не печатается."""
    parts: list[str] = []
    if lead.child_name:
        parts.append(lead.child_name.strip())
    if lead.child_age is not None:
        parts.append(f"{lead.child_age} лет")
    elif lead.child_birth_year is not None:
        parts.append(f"{lead.child_birth_year} г. р.")
    if not parts:
        return None
    line = ", ".join(parts)
    gender = _GENDER_TITLE.get(lead.child_gender)
    return f"{line} ({gender})" if gender else line


def _parent_line(lead: LeadDraft) -> str | None:
    """``Айгуль (мама)``."""
    if not lead.parent_name:
        return None
    name = lead.parent_name.strip()
    relation = (lead.parent_relation or "").strip()
    return f"{name} ({relation})" if relation else name


def _channel_line(lead: LeadDraft, *, channel: ChannelKind, now: datetime) -> str:
    """``Instagram @dana_mama, диалог от 09.08 21:14``."""
    title = _CHANNEL_TITLE.get(channel, channel.value)
    if channel is ChannelKind.INSTAGRAM and lead.instagram_username:
        username = lead.instagram_username.strip().lstrip("@")
        title = f"{title} @{username}"
    return f"{title}, диалог от {_format_dt(now)}"


def _format_slot(slot: datetime | None) -> str | None:
    """Время пробного в локальной зоне: ``ср 12.08 17:00``."""
    if slot is None:
        return None
    local = _to_local(slot)
    weekday = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")[local.weekday()]
    return f"{weekday} {local:%d.%m %H:%M}"


def _format_dt(value: datetime) -> str:
    """Момент времени в зоне школы: ``09.08 21:14``."""
    return f"{_to_local(value):%d.%m %H:%M}"


def _to_local(value: datetime) -> datetime:
    """Переводит момент в таймзону школы (``Settings.timezone``)."""
    try:
        tz = ZoneInfo(get_settings().timezone)
    except Exception:  # noqa: BLE001 - кривая зона не должна ронять уведомление
        tz = ZoneInfo("Asia/Almaty")
    if value.tzinfo is None:
        from datetime import timezone

        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz)


def _now() -> datetime:
    """Текущий момент в UTC (в локальную зону переводит форматирование)."""
    from datetime import timezone

    return datetime.now(tz=timezone.utc)


def _clip(text: str, limit: int) -> str:
    """Обрезает по границе слова с многоточием."""
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    cut = clean[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return f"{cut or clean[:limit]}…"


__all__ = [
    "MAX_CARD_LINES",
    "MAX_FIELD_CHARS",
    "MAX_QUESTION_CHARS",
    "format_phone",
    "render_alert",
    "render_escalation_card",
    "render_lead_card",
]
