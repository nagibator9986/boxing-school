"""Тексты «Возврата клиентов»: первое сообщение, напоминание, заметка в истории модели.

Факты и вопрос сообщения — из базы знаний (``kb/i18n.yaml``, группа ``campaign``): их
правит владелец, кодом они не задаются. Модель только делает шаблон личным по итогу
прошлой переписки и проходит ту же проверку фактов, что и обычный ответ бота: без цен,
времени, адресов и чужих имён. Не прошла или недоступна — уходит шаблон как есть.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID, uuid5

from app.campaign.contacts import CampaignSegment
from app.kb.models import EMOJI_RE, MARKDOWN_RE, KBSnapshot
from app.types import Language

__all__ = [
    "DRAFT_INSTRUCTION",
    "MAX_DRAFT_CHARS",
    "NOTE_HEAD",
    "Brief",
    "address_name",
    "compose",
    "draft_prompt",
    "first_message_id",
    "history_note",
    "other_sport",
    "reminder_message_id",
    "reminder_text",
    "template_body",
    "validate_draft",
]

#: Ключи i18n. Сегмент без своего текста получает текст «интерес затих».
HELLO_KEY: Final[str] = "campaign.hello"
HELLO_ANON_KEY: Final[str] = "campaign.hello_anon"
REMINDER_KEY: Final[str] = "campaign.reminder"
_FIRST_PREFIX: Final[str] = "campaign.first_"
_FALLBACK_SEGMENT: Final[CampaignSegment] = CampaignSegment.FADED

#: Длиннее — уже не «напомнить о себе», а письмо, которое в WhatsApp не дочитают.
MAX_DRAFT_CHARS: Final[int] = 600

#: Начало служебной заметки в истории модели. Пайплайн не считает такие строки словами
#: клиента (``pipeline._user_words``): возраст из разбора не сойдёт за названный клиентом.
NOTE_HEAD: Final[str] = "[служебная заметка системы]"

#: Инструкция модели для черновика. Фактов о школе здесь нет — только формат: факты и
#: вопрос приходят шаблоном из базы знаний, сведения о клиенте — из разбора переписки.
DRAFT_INSTRUCTION: Final[str] = (
    "Ты помогаешь администратору детской спортивной школы написать одно короткое сообщение "
    "в WhatsApp родителю, который раньше писал в школу и пропал. Тебе дают ШАБЛОН — он "
    "задаёт смысл, факты и вопрос в конце — и ИТОГ прошлой переписки. Перепиши шаблон так, "
    "чтобы он звучал лично: можно назвать, о чём родитель спрашивал (вид спорта, для кого — "
    "сын или дочь, возраст ребёнка), если это прямо сказано в итоге. Чего в итоге нет, того "
    "не называй: не сказано, сын или дочь, — пиши «ребёнок»; не сказан вид спорта — «занятия». "
    "Предлагай только то, что предлагает шаблон: другого не предлагай и не упоминай — ни "
    "индивидуальных занятий, ни абонементов, тарифов и цен, ни других видов спорта, даже если "
    "о них есть в итоге. "
    "Правила: только факты из шаблона и итога; не называй цены, суммы, скидки, время, дни "
    "недели, даты, адреса, телефоны и имена — ни тренеров, ни детей, ни родителей; не "
    "пересказывай итог и не упоминай, что переписку разбирали; не называй, почему родитель "
    "пропал или ребёнок бросил (цена, переезд, болезнь, занятость); не обещай результатов; не "
    "дави и не торопи; на «вы»; без эмодзи, без markdown, без кавычек вокруг текста; не "
    "здоровайся — приветствие добавят отдельно; название школы оставь как в шаблоне; начни с "
    "заглавной буквы; до 350 знаков; закончи вопросом из шаблона. Верни только текст сообщения."
)

#: Приветствие, которое модель всё-таки дописала, несмотря на просьбу: его снимаем, а не
#: выбрасываем весь черновик — иначе «Здравствуйте!» встало бы дважды.
_LEADING_HELLO_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:здравствуйте|добрый\s+(?:день|вечер|утро)|доброе\s+утро|привет\w*|салам\w*|"
    r"сәлеметсіз\s+бе|сәлем\w*|ассалаумағалейкум)[^.!?\n]*[.!?\n]+\s*",
    re.IGNORECASE,
)
_URL_RE: Final[re.Pattern[str]] = re.compile(r"https?://|www\.|\.(?:kz|ru|com)\b", re.IGNORECASE)
#: Фамилия в «Петрова Анна»: в обращении нужна не она. Окончания русских и казахских фамилий.
_SURNAME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:ова|ева|ёва|ина|ына|ов|ев|ёв|ин|ын|ская|ский|цкая|цкий|енко|ук|юк|ұлы|улы|қызы|кызы)$",
    re.IGNORECASE,
)
_NAME_WORD_RE: Final[re.Pattern[str]] = re.compile(r"^[A-ZА-ЯЁӘҒҚҢӨҰҮҺІ][a-zа-яёәғқңөұүһі-]{1,19}$")
#: Слово с заглавной буквы внутри фразы — кандидат в имя. Первое слово фразы не в счёт:
#: там заглавная у любого слова.
_INNER_CAPITAL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<=[^.!?\n\s—–-])\s+([A-ZА-ЯЁӘҒҚҢӨҰҮҺІ][a-zа-яёәғқңөұүһі]+)"
)
#: Подробности, которые модель любит додумать: для кого (сын, дочь) и какой спорт. В
#: черновике их не больше, чем в шаблоне, — если итог переписки о них молчит. Шаблон
#: свой «бокса и кикбоксинга» несёт в названии школы, отсюда сравнение числа упоминаний.
_DETAILS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bсын", re.IGNORECASE),
    re.compile(r"\bдоч", re.IGNORECASE),
    re.compile(r"кикбоксинг", re.IGNORECASE),
    re.compile(r"(?<!кик)бокс", re.IGNORECASE),
)
#: Предложения, которых нет в шаблоне, — первое сообщение их не делает никогда. Живая
#: рассылка 27.10.2026: модель приняла совет разбора («предложить индивидуальные») за
#: задание, написала «подходят ли вам наши тарифы» и извинилась за неотвеченный вопрос
#: о борьбе — которой в школе нет.
_FOREIGN_OFFERS: Final[re.Pattern[str]] = re.compile(
    r"индивидуальн|персональн|тариф|абонемент|стоимост|\bцен[аыуеой]?\b|оплат|скидк", re.IGNORECASE
)
#: Виды спорта, которых школа не ведёт: о них первое сообщение молчит, а контакт, который
#: спрашивал только о них, решает человек.
_OTHER_SPORTS: Final[re.Pattern[str]] = re.compile(
    r"борьб|борец|футбол|плаван|карате|дзюдо|самбо|тхэквондо|таэквондо|гимнаст|\bмма\b|\bmma\b|"
    r"танц|шахмат|волейбол|баскетбол|хоккей|теннис|гандбол",
    re.IGNORECASE,
)
_OUR_SPORTS: Final[re.Pattern[str]] = re.compile(r"бокс", re.IGNORECASE)

#: Пространство идентификаторов сообщений рассылки. ``crm_message_id`` выводится из id
#: контакта: повтор прохода не создаст второе сообщение, а CRM найдёт строку отправки.
_MESSAGE_NS: Final[UUID] = UUID("6f1d2c3e-5a4b-4c7d-9e8f-0a1b2c3d4e5f")

#: Вежливые «Вы», «Вас», «Сіз» с заглавной — не имена.
_POLITE: Final[frozenset[str]] = frozenset({"вы", "вас", "вам", "ваш", "ваша", "ваше", "ваши", "вами", "сіз", "сізге", "сізді"})


@dataclass(frozen=True, slots=True)
class Brief:
    """Что известно о клиенте из разбора переписки и проверки — без цитат."""

    segment: CampaignSegment
    parent_name: str | None = None
    child_name: str | None = None
    summary: str | None = None
    obstacle: str | None = None
    next_step: str | None = None
    note: str | None = None
    last_contact_at: datetime | None = None


def first_message_id(contact_id: str) -> UUID:
    """``crm_message_id`` первого сообщения контакту. ``contact_id`` — 32 знака без дефисов."""
    return uuid5(_MESSAGE_NS, f"campaign:{contact_id}:first")


def reminder_message_id(contact_id: str) -> UUID:
    """``crm_message_id`` напоминания контакту."""
    return uuid5(_MESSAGE_NS, f"campaign:{contact_id}:reminder")


def other_sport(brief: Brief) -> str | None:
    """Спрашивал только о спорте, которого в школе нет, — слово из итога. ``None`` — нет.

    Такому «вы писали нам о борьбе — вопрос актуален?» обещает то, чего школа не даст, а
    приглашение на бокс ему, возможно, не нужно вовсе. Решает человек.
    """
    text = " ".join(value for value in (brief.summary, brief.obstacle) if value)
    found = _OTHER_SPORTS.search(text)
    if found is None or _OUR_SPORTS.search(text):
        return None
    return found.group(0).lower()


def address_name(parent_name: str | None) -> str | None:
    """Имя для обращения: одно слово-имя. Сомнение — ``None``, приветствие без имени.

    Из разбора приходит и «Анна», и «Петрова Анна», и «мама Тимура». «Здравствуйте,
    Петрова Анна!» и «Здравствуйте, мама Тимура!» хуже, чем просто «Здравствуйте!».
    """
    words = [word.strip(".,!") for word in (parent_name or "").split()]
    words = [word for word in words if word]
    if len(words) > 1:
        words = [word for word in words if not _SURNAME_RE.search(word)]
    if len(words) != 1 or not _NAME_WORD_RE.match(words[0]):
        return None
    return words[0]


def template_body(kb: KBSnapshot, segment: CampaignSegment, lang: Language) -> str | None:
    """Текст сегмента из базы знаний без приветствия. ``None`` — текстов возврата в базе нет."""
    for key in (f"{_FIRST_PREFIX}{segment.value}", f"{_FIRST_PREFIX}{_FALLBACK_SEGMENT.value}"):
        if key in kb.i18n.strings:
            body = kb.text(key, lang).strip()
            if body:
                return body
    return None


def compose(kb: KBSnapshot, body: str, *, name: str | None, lang: Language) -> str | None:
    """Приветствие и текст одним сообщением."""
    key = HELLO_KEY if name else HELLO_ANON_KEY
    if key not in kb.i18n.strings:
        return None
    hello = kb.text(key, lang, name=name or "").strip()
    return f"{hello} {body.strip()}".strip()


def reminder_text(kb: KBSnapshot, lang: Language) -> str | None:
    """Одно напоминание тому, кто молчит. ``None`` — текста в базе нет."""
    if REMINDER_KEY not in kb.i18n.strings:
        return None
    return kb.text(REMINDER_KEY, lang).strip() or None


def draft_prompt(brief: Brief, body: str, *, tz_name: str) -> str:
    """Запрос модели: шаблон и то, что известно из прошлой переписки."""
    lines = ["ШАБЛОН (смысл, факты и вопрос в конце — отсюда):", body, "", "ИТОГ ПРОШЛОЙ ПЕРЕПИСКИ:"]
    lines.extend(_known(brief, tz_name=tz_name))
    return "\n".join(lines)


def history_note(brief: Brief, *, tz_name: str) -> str:
    """Служебная заметка перед первым сообщением в истории модели.

    Клиент ответит на сообщение, которого модель не писала, — и без заметки не узнает,
    что разговор начала школа и о чём шла речь раньше.
    """
    known = "; ".join(line.removeprefix("- ") for line in _known(brief, tz_name=tz_name))
    return (
        f"{NOTE_HEAD} Сообщение ниже школа отправила первой: клиент из списка «Возврат "
        "клиентов» — раньше писал в школу, до бота, и пропал. Из разбора прошлой переписки "
        f"(это не слова клиента): {known}. Не пересказывай клиенту этот разбор. Имя и "
        "возраст ребёнка для записи уточни у самого клиента — разбор мог ошибиться."
    )


def validate_draft(
    text: str | None,
    *,
    kb: KBSnapshot,
    name: str | None,
    known_names: Sequence[str],
    lang: Language,
    segment: CampaignSegment | None = None,
    facts: str = "",
) -> str | None:
    """Готовое сообщение из ответа модели. ``None`` — не годится, нужен шаблон.

    Та же проверка фактов, что у обычного ответа бота (``postcheck.check``): инструментов
    у черновика нет, поэтому любая цена, время, день недели или адрес — выдумка. Имена
    строже, чем в обычном ответе: общий фильтр ловит имя рядом с записью и после
    приветствия, а «Рады видеть Тимура!» пропустил бы. Здесь годится только слово с
    заглавной, которое есть в шаблоне сегмента или в известных именах. ``facts`` — итог
    прошлой переписки: «сын», «дочь» и вид спорта годятся, только если они там есть.
    """
    from app.core import postcheck

    body = (text or "").strip().strip("«»\"'").strip()
    body = _LEADING_HELLO_RE.sub("", body, count=1).strip()
    if not body or len(body) > MAX_DRAFT_CHARS or "?" not in body:
        return None
    body = body[0].upper() + body[1:]
    if EMOJI_RE.search(body) or MARKDOWN_RE.search(body) or _URL_RE.search(body):
        return None
    template = template_body(kb, segment, lang) if segment is not None else None
    if _unknown_names(body, allowed=(template or "", *(name or "", *known_names))):
        return None
    if _invented_details(body, template=template or "", facts=facts):
        return None
    message = compose(kb, body, name=name, lang=lang)
    if message is None:
        return None
    verdict = postcheck.check(
        message,
        invocations=(),
        lang=lang,
        kb=kb,
        prompt_ngrams=frozenset(),
        known_names=[value for value in (name, *known_names) if value],
    )
    return verdict.text if verdict.ok else None


def _unknown_names(body: str, *, allowed: Sequence[str]) -> tuple[str, ...]:
    """Слова с заглавной внутри фраз, которых нет ни в шаблоне, ни в известных именах."""
    known = {_stem(word) for text in allowed for word in re.findall(r"\w+", text or "")}
    return tuple(
        word
        for word in _INNER_CAPITAL_RE.findall(body)
        if word.lower() not in _POLITE and _stem(word) not in known
    )


def _invented_details(body: str, *, template: str, facts: str) -> bool:
    """Назвал ли черновик сына, дочь или вид спорта, о которых итог переписки молчит, —
    или предложил то, чего нет в шаблоне, о чём бы ни говорил итог."""
    if any(
        len(pattern.findall(body)) > len(pattern.findall(template))
        for pattern in (_FOREIGN_OFFERS, _OTHER_SPORTS)
    ):
        return True
    return any(
        len(pattern.findall(body)) > len(pattern.findall(template)) and not pattern.search(facts)
        for pattern in _DETAILS
    )


def _stem(word: str) -> str:
    """Основа слова для сравнения имён: «Тимура» и «Тимур» — одно имя."""
    return word.lower()[:4]


def _known(brief: Brief, *, tz_name: str) -> list[str]:
    """Строки «что известно» — только заполненные."""
    lines: list[str] = []
    for title, value in (
        ("итог", brief.summary),
        ("что помешало", brief.obstacle),
        # «Следующий шаг» разбора сюда не идёт: это совет аналитика, и модель принимала
        # его за задание — «предложить индивидуальные тренировки» уходило клиенту.
        ("ребёнок", brief.child_name),
        ("заметка администратора", brief.note),
    ):
        if value and value.strip():
            lines.append(f"- {title}: {' '.join(value.split())}")
    if brief.last_contact_at is not None:
        lines.append(f"- последний раз писали: {_local(brief.last_contact_at, tz_name):%d.%m.%Y}")
    return lines or ["- подробностей нет"]


def _local(value: datetime, tz_name: str) -> datetime:
    from zoneinfo import ZoneInfo

    try:
        zone = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - кривое имя зоны не повод падать
        zone = ZoneInfo("Asia/Almaty")
    return value.astimezone(zone)
