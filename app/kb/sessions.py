"""Ближайшее занятие для записи на пробное — по расписанию зала.

Владелец 10.09.2026: бот сам доводит запись до конца — «мы записали вас на
19:00, приходите за 10 минут». Раньше время пробного назначал администратор,
потому что расписания в базе не было. Теперь оно есть, и конкретную дату
считает код, а не модель: модель, называющая день недели и число, ошибается
в календаре, а ошибка здесь — ребёнок, пришедший в закрытый зал.

Модуль чистый: на входе зал, выбор родителя и «сейчас», на выходе либо занятие,
либо причина, по которой записать нельзя, и варианты, из которых выбирать.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from app.kb.agreement import chosen_option, question_sentence, split_agreement
from app.kb.models import Gym, ScheduleSlot, age_value

__all__ = [
    "SessionChoice",
    "TrialSession",
    "client_named_discipline",
    "client_named_age",
    "client_named_day",
    "client_named_name",
    "client_named_time",
    "fold_name",
    "normalize_time",
    "resolve_trial_session",
    "weekday_code",
]

_WEEKDAYS: Final[tuple[str, ...]] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: «19:00», «19.00», «в 9», «17 30».
_TIME_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)([01]?\d|2[0-3])(?:[:.\s]([0-5]\d))?(?!\d)")

#: На сколько дней вперёд ищется занятие. Две недели покрывают любое недельное
#: расписание с запасом на ближайшие дни, в которые записываться уже поздно.
_HORIZON_DAYS: Final[int] = 14


@dataclass(frozen=True, slots=True)
class TrialSession:
    """Конкретное занятие: дата и время в часовом поясе школы."""

    starts_at: datetime
    weekday: str
    time_start: str
    time_end: str
    discipline: str


@dataclass(frozen=True, slots=True)
class SessionChoice:
    """Итог выбора: занятие либо причина и варианты.

    ``problem``: ``no_schedule`` — у зала нет расписания; ``need_time`` —
    родитель не назвал время; ``unknown_time`` — такого времени в расписании нет;
    ``unknown_day`` — в этот день такого занятия нет; ``unknown_discipline`` —
    такой секции в зале нет; ``need_discipline`` — в это время идут обе секции.
    """

    session: TrialSession | None = None
    problem: str | None = None
    options: tuple[ScheduleSlot, ...] = ()


def normalize_time(text: str | None) -> str | None:
    """Время из слов родителя в формате расписания ``HH:MM``. ``None`` — времени нет."""
    if not text:
        return None
    match = _TIME_RE.search(str(text))
    if match is None:
        return None
    return f"{int(match.group(1)):02d}:{int(match.group(2) or 0):02d}"


def weekday_code(value: str | None) -> str | None:
    """«mon»…«sun» из аргумента инструмента; всё прочее — ``None``."""
    day = (value or "").strip().lower()[:3]
    return day if day in _WEEKDAYS else None


_weekday = weekday_code

#: «19:00», «19.00».
_HHMM_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)")

#: «в 19», «к 7 вечера», «на 17» — но не «на 9 лет» и не «в 5 классе».
_PREP_HOUR_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[\s,])(?:в|к|на|с|до)\s+([01]?\d|2[0-3])(?![\d:.])"
    r"(?!\s*(?:лет|год|класс|мес|раз|дет|реб|жас))"
    r"(?:\s*(утра|дня|вечера|ночи))?",
    re.IGNORECASE,
)

#: «7 вечера», «17 часов».
_HOUR_WORD_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)([01]?\d|2[0-3])\s*(час\w*|утра|дня|вечера)", re.IGNORECASE
)

#: День недели в словах клиента. «сенбі» проверяется без буквы перед ним: иначе
#: оно находилось бы внутри «сейсенбі» и «жексенбі».
_WEEKDAY_WORDS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (code, re.compile(pattern, re.IGNORECASE))
    for code, pattern in (
        ("mon", r"понедельник\w*|дүйсенбі|(?<![а-яё])пн(?![а-яё])"),
        ("tue", r"вторник\w*|сейсенбі|(?<![а-яё])вт(?![а-яё])"),
        ("wed", r"\bсред[аеуы]\b|сәрсенбі|(?<![а-яё])ср(?![а-яё])"),
        ("thu", r"четверг\w*|бейсенбі|(?<![а-яё])чт(?![а-яё])"),
        ("fri", r"пятниц\w*|жұма|(?<![а-яё])пт(?![а-яё])"),
        ("sat", r"суббот\w*|(?<![а-яәөүұқғңһі])сенбі|(?<![а-яё])сб(?![а-яё])"),
        ("sun", r"воскресень\w*|жексенбі|(?<![а-яё])вс(?![а-яё])"),
    )
)


def _client_times(texts: tuple[str, ...] | list[str]) -> set[str]:
    found: set[str] = set()
    for raw in texts:
        text, proposal = split_agreement(raw)
        # Согласие на предложение бота засчитывается, только если время в нём одно:
        # «Да» на «в 09:00 или в 19:00?» ничего не выбирает.
        offered = _offered_times(proposal)
        if len(offered) == 1:
            found |= offered
        # Выбранный цифрой вариант — выбор родителя; время засчитывается, если оно там одно.
        picked = _offered_times(chosen_option(raw))
        if len(picked) == 1:
            found |= picked
        for match in _HHMM_RE.finditer(text):
            found.add(f"{int(match.group(1)):02d}:{match.group(2)}")
        for match in (*_PREP_HOUR_RE.finditer(text), *_HOUR_WORD_RE.finditer(text)):
            hour = int(match.group(1))
            part = (match.group(2) or "").lower()
            found.add(f"{hour:02d}:00")
            # «в 7» и «в 7 вечера» — это 19:00: дети не тренируются в семь утра.
            if hour < 12 and not part.startswith(("утр", "час", "ноч")):
                found.add(f"{hour + 12:02d}:00")
    return found


#: «19:00–20:30», «с 19:00 до 20:30»: конец занятия — не второе предложенное время.
_RANGE_END_RE: Final[re.Pattern[str]] = re.compile(
    r"((?<!\d)(?:[01]?\d|2[0-3])[:.][0-5]\d)\s*(?:–|—|-|до)\s*(?:[01]?\d|2[0-3])[:.][0-5]\d(?!\d)"
)


def _offered_times(proposal: str | None) -> set[str]:
    """Время начала в предложении бота, без концов диапазонов."""
    starts = _RANGE_END_RE.sub(r"\1", proposal or "")
    return {f"{int(match.group(1)):02d}:{match.group(2)}" for match in _HHMM_RE.finditer(starts)}


def client_named_time(time_text: str | None, texts) -> bool:
    """Назвал ли клиент это время сам."""
    wanted = normalize_time(time_text)
    return wanted is not None and wanted in _client_times(list(texts))


def client_named_discipline(discipline: str | None, texts) -> bool:
    """Назвал ли клиент эту секцию сам. «Кикбоксинг» боксом не считается."""
    named: set[str] = set()
    for raw in texts:
        text, proposal = split_agreement(raw)
        named |= _disciplines_in(text)
        # Из предложения бота секция засчитывается, только если она там одна.
        offered = _disciplines_in(proposal or "")
        if len(offered) == 1:
            named |= offered
        picked = _disciplines_in(chosen_option(raw) or "")
        if len(picked) == 1:
            named |= picked
    return discipline in named


def _disciplines_in(text: str) -> set[str]:
    lowered = (text or "").lower()
    found: set[str] = set()
    if "кикбокс" in lowered:
        found.add("kickboxing")
    if "бокс" in lowered.replace("кикбокс", ""):
        found.add("boxing")
    return found


#: Кириллица и латиница к одному написанию: «Ali» и «Али» — одно имя.
_TRANSLIT: Final[dict[int, str]] = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
        "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
        "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
        "я": "ya", "ә": "a", "ө": "o", "ү": "u", "ұ": "u", "қ": "k", "ғ": "g", "ң": "n",
        "һ": "h", "і": "i",
    }
)
_NAME_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[^\W\d_]{2,}")

#: Сколько первых букв сравнивать: «Серикова» и «Сериков», «Алиге» и «Али» совпадут.
_NAME_STEM: Final[int] = 3

#: «Семь лет», «сегіз жаста». Родительный падеж («с семи лет») — в ``models.AGE_WORDS``.
_AGE_NOMINATIVE: Final[dict[str, int]] = {
    "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
    "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
    "тринадцати": 13, "четырнадцати": 14, "пятнадцати": 15, "шестнадцати": 16, "семнадцати": 17,
    "үш": 3, "төрт": 4, "бес": 5, "алты": 6, "жеті": 7, "сегіз": 8, "тоғыз": 9,
}

#: «он екі жаста» — двенадцать. Только перед «жас»: иначе «он» — русское местоимение.
_KK_TENS_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![а-яәөүұқғңһі])он(?:\s+(бір|екі|үш|төрт|бес|алты|жеті|сегіз|тоғыз))?\s+жас"
)
_KK_UNITS: Final[dict[str, int]] = {
    "бір": 1, "екі": 2, "үш": 3, "төрт": 4, "бес": 5, "алты": 6, "жеті": 7, "сегіз": 8, "тоғыз": 9,
}
#: «жетіде», «сегізде» — казахский падеж после числа.
_KK_CASE_SUFFIXES: Final[tuple[str, ...]] = ("де", "те", "да", "та", "ге", "ке", "ға", "қа")
#: «семилетний», «пятнадцатилетняя».
_AGE_COMPOUND_STEMS: Final[dict[str, int]] = {
    "пяти": 5, "шести": 6, "семи": 7, "восьми": 8, "девяти": 9, "десяти": 10, "одиннадцати": 11,
    "двенадцати": 12, "тринадцати": 13, "четырнадцати": 14, "пятнадцати": 15, "шестнадцати": 16,
    "семнадцати": 17,
}


def _age_of_token(token: str) -> int | None:
    """Возраст из одного слова: «8», «восемь», «жетіде», «семилетний»."""
    value = age_value(token) or _AGE_NOMINATIVE.get(token)
    if value:
        return value
    for suffix in _KK_CASE_SUFFIXES:
        if token.endswith(suffix) and token[: -len(suffix)] in _AGE_NOMINATIVE:
            return _AGE_NOMINATIVE[token[: -len(suffix)]]
    if "летн" in token:
        for stem, years in sorted(_AGE_COMPOUND_STEMS.items(), key=lambda item: -len(item[0])):
            if token.startswith(stem):
                return years
    return None


_AGE_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\d:.])\d{1,2}(?![\d:.])|[^\W\d_]+")


def fold_name(word: str) -> str:
    """Имя без регистра и алфавита: «Ali», «али» и «Али» пишутся одинаково."""
    return word.casefold().translate(_TRANSLIT)


def _name_key(word: str) -> str:
    return fold_name(word)[:_NAME_STEM]


def client_named_name(name: str | None, texts) -> bool:
    """Назвал ли клиент каждое слово этого имени — фамилию и имя."""
    # Имя — только собственные слова клиента: «Да» на «Записать Ивана?» не делает
    # придуманное моделью имя названным.
    said = {
        _name_key(word)
        for text in texts
        for word in _NAME_WORD_RE.findall(split_agreement(text)[0])
    }
    tokens = _NAME_WORD_RE.findall(name or "")
    return bool(tokens) and all(_name_key(token) in said for token in tokens)


def client_named_age(age: int | None, texts) -> bool:
    """Называл ли клиент этот возраст — цифрой или словом. «17:00» возрастом не считается."""
    if age is None:
        return False
    for text in texts:
        own = split_agreement(text)[0].lower()
        if any(_age_of_token(token) == age for token in _AGE_TOKEN_RE.findall(own)):
            return True
        for match in _KK_TENS_RE.finditer(own):
            if 10 + _KK_UNITS.get(match.group(1) or "", 0) == age:
                return True
    return False


def client_named_day(day: str | None, texts) -> bool:
    """Упоминал ли клиент этот день недели.

    Проверяется только упоминание: «не во вторник, а в субботу» модель разберёт
    сама, а день, которого клиент не называл вовсе, в запись не попадёт.
    """
    code = weekday_code(day)
    pattern = dict(_WEEKDAY_WORDS).get(code) if code else None
    if pattern is None:
        return False
    for raw in texts:
        text, proposal = split_agreement(raw)
        if pattern.search(text):
            return True
        # В предложении бота день берётся только из самого вопроса и только если он
        # там один: «по понедельникам, средам и пятницам» — это расписание, а не выбор.
        asked = question_sentence(proposal)
        if asked and {other for other, found in _WEEKDAY_WORDS if found.search(asked)} == {code}:
            return True
        picked = chosen_option(raw)
        if picked and {other for other, found in _WEEKDAY_WORDS if found.search(picked)} == {code}:
            return True
    return False


def resolve_trial_session(
    gym: Gym,
    *,
    time_text: str | None,
    day: str | None,
    discipline: str | None,
    now: datetime,
    tz_name: str,
    min_lead_hours: float,
    horizon_days: int = _HORIZON_DAYS,
) -> SessionChoice:
    """Ближайшее занятие зала под выбор родителя, не раньше ``min_lead_hours``."""
    slots = list(gym.schedule)
    if not slots:
        return SessionChoice(problem="no_schedule")

    if discipline:
        chosen = [slot for slot in slots if slot.discipline == discipline]
        if not chosen:
            return SessionChoice(problem="unknown_discipline", options=tuple(slots))
        slots = chosen

    time = normalize_time(time_text)
    if time is None:
        # Время у выбранной секции одно (в «Кеме» бокс только в 19:00) — выбирать
        # родителю нечего, и вопрос «во сколько удобно?» только тянет запись.
        wanted = _weekday(day)
        starts = {slot.time_start for slot in slots if wanted is None or wanted in slot.days}
        if len(starts) != 1:
            return SessionChoice(problem="need_time", options=tuple(slots))
        time = next(iter(starts))
    timed = [slot for slot in slots if slot.time_start == time]
    if not timed:
        return SessionChoice(problem="unknown_time", options=tuple(slots))

    wanted_day = _weekday(day)
    if wanted_day is not None:
        on_day = [slot for slot in timed if wanted_day in slot.days]
        if not on_day:
            return SessionChoice(problem="unknown_day", options=tuple(timed))
        timed = on_day

    if len({slot.discipline for slot in timed}) > 1:
        return SessionChoice(problem="need_discipline", options=tuple(timed))

    tz = ZoneInfo(tz_name)
    local_now = now.astimezone(tz)
    earliest = local_now + timedelta(hours=max(0.0, min_lead_hours))
    best: TrialSession | None = None
    for slot in timed:
        hours, minutes = (int(part) for part in slot.time_start.split(":"))
        for offset in range(horizon_days + 1):
            date = (local_now + timedelta(days=offset)).date()
            weekday = _WEEKDAYS[date.weekday()]
            if weekday not in slot.days or (wanted_day is not None and weekday != wanted_day):
                continue
            starts = datetime(date.year, date.month, date.day, hours, minutes, tzinfo=tz)
            if starts < earliest:
                continue
            if best is None or starts < best.starts_at:
                best = TrialSession(starts, weekday, slot.time_start, slot.time_end, slot.discipline)
            break
    if best is None:
        return SessionChoice(problem="unknown_time", options=tuple(slots))
    return SessionChoice(session=best)
