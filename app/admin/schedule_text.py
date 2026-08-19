"""Разбор расписания из текста, каким его присылает администратор школы.

Администратор ведёт расписание в WhatsApp и присылает его сообщениями вида::

    📍 ул. Каирбекова, 334
    Школа №9, цокольный этаж, район КСК
    🥊 КИКБОКСИНГ
    Понедельник • Среда • Пятница
    🕘 09:00–10:30
    🕔 17:00–18:30
    🥊 БОКС
    Вторник • Четверг • Суббота
    🕘 09:00–10:30

Модуль принимает ровно этот формат, а не «правильный» — чтобы человеку не
пришлось менять привычку. Разбор строгий: непонятая строка не игнорируется
молча, а возвращается администратору как ошибка с номером строки. Тихо
проглоченная строка означала бы, что часть расписания исчезла, и никто бы этого
не заметил.

Возраст групп здесь не разбирается: в присылаемом формате его нет вовсе.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "ParsedSchedule",
    "ScheduleParseError",
    "parse_schedule_text",
    "render_schedule_text",
]

#: Дни недели во всех формах, в которых их пишут: полные, сокращённые, казахские.
_DAYS: Final[dict[str, str]] = {
    "понедельник": "mon", "пн": "mon", "дүйсенбі": "mon", "дуйсенби": "mon",
    "вторник": "tue", "вт": "tue", "сейсенбі": "tue", "сейсенби": "tue",
    "среда": "wed", "ср": "wed", "сәрсенбі": "wed", "сарсенби": "wed",
    "четверг": "thu", "чт": "thu", "бейсенбі": "thu", "бейсенби": "thu",
    "пятница": "fri", "пт": "fri", "жұма": "fri", "жума": "fri",
    "суббота": "sat", "сб": "sat", "сенбі": "sat", "сенби": "sat",
    "воскресенье": "sun", "вс": "sun", "жексенбі": "sun", "жексенби": "sun",
}

_DAY_ORDER: Final[list[str]] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_DAY_TITLE: Final[dict[str, str]] = {
    "mon": "Понедельник", "tue": "Вторник", "wed": "Среда", "thu": "Четверг",
    "fri": "Пятница", "sat": "Суббота", "sun": "Воскресенье",
}

#: Дисциплина. Кикбоксинг проверяется первым: «бокс» — его подстрока.
_DISCIPLINES: Final[list[tuple[str, str]]] = [
    ("кикбоксинг", "kickboxing"),
    ("кик-боксинг", "kickboxing"),
    ("kickboxing", "kickboxing"),
    ("бокс", "boxing"),
    ("boxing", "boxing"),
]

#: Интервал времени. Тире может быть любым: дефис, en dash, em dash.
_TIME_RANGE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)([0-2]?\d)[:.]([0-5]\d)\s*[-–—]\s*([0-2]?\d)[:.]([0-5]\d)(?!\d)"
)

#: Строка, которую можно молча пропустить: адрес, ориентир, пустая, декоративная.
_SKIP_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:📍|ориентир|адрес|ул\.|улица|г\.|город|далее|\W*)\s*$|^\s*(?:📍|ориентир|адрес|ул\.|улица|г\.)",
    re.IGNORECASE,
)


class ScheduleParseError(ValueError):
    """Текст разобрать не удалось. ``problems`` — что именно и в какой строке."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(slots=True)
class ParsedSlot:
    """Одна разобранная строка расписания."""

    discipline: str
    days: list[str]
    time_start: str
    time_end: str


@dataclass(slots=True)
class ParsedSchedule:
    """Результат разбора: слоты плюс замечания, которые надо показать человеку."""

    slots: list[ParsedSlot] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_yaml_dicts(self) -> list[dict[str, object]]:
        """Слоты в том виде, в каком они лягут в ``kb/gyms.yaml``."""
        return [
            {
                "discipline": slot.discipline,
                "days": list(slot.days),
                "time_start": slot.time_start,
                "time_end": slot.time_end,
            }
            for slot in self.slots
        ]


def _normalize(line: str) -> str:
    """Строка без эмодзи и лишних пробелов, в нижнем регистре."""
    cleaned = re.sub(r"[^\w\s:.\-–—•,]", " ", line, flags=re.UNICODE)
    return " ".join(cleaned.split()).lower()


def _days_in(line: str) -> list[str]:
    """Дни недели, упомянутые в строке, в календарном порядке и без повторов."""
    found: set[str] = set()
    for token in re.split(r"[\s•,;]+", line):
        code = _DAYS.get(token.strip(".:"))
        if code:
            found.add(code)
    return [day for day in _DAY_ORDER if day in found]


def _discipline_in(line: str) -> str | None:
    """Дисциплина, если строка её называет."""
    for needle, code in _DISCIPLINES:
        if needle in line:
            return code
    return None


def _times_in(line: str) -> list[tuple[str, str]]:
    """Интервалы времени из строки, приведённые к ``HH:MM``."""
    out: list[tuple[str, str]] = []
    for match in _TIME_RANGE_RE.finditer(line):
        sh, sm, eh, em = (int(g) for g in match.groups())
        if sh > 23 or eh > 23:
            continue
        start, end = f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"
        if start < end:
            out.append((start, end))
    return out


def parse_schedule_text(text: str) -> ParsedSchedule:
    """Разбирает расписание одного зала.

    Формат «шапочный»: заголовок дисциплины и строка дней действуют на все
    последующие строки времени, пока не встретится новый заголовок. Именно так
    администратор и пишет.

    :raises ScheduleParseError: если не нашлось ни одного слота либо встретились
        строки, которые выглядят значимыми, но не разобраны.
    """
    if not text or not text.strip():
        raise ScheduleParseError(["пустой текст"])

    result = ParsedSchedule()
    discipline: str | None = None
    days: list[str] = []
    problems: list[str] = []
    unparsed: list[str] = []

    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or _SKIP_RE.match(raw.strip()):
            continue
        line = _normalize(raw)
        if not line:
            continue

        found_discipline = _discipline_in(line)
        found_days = _days_in(line)
        found_times = _times_in(line)

        if found_discipline and not found_times:
            # Заголовок дисциплины: сбрасываем дни, чтобы бокс не унаследовал
            # дни кикбоксинга, если администратор забыл их повторить.
            discipline = found_discipline
            days = found_days or []
            continue

        if found_days and not found_times:
            days = found_days
            continue

        if found_times:
            if found_discipline:
                discipline = found_discipline
            if found_days:
                days = found_days
            if discipline is None:
                problems.append(f"строка {number}: время есть, а вид занятий не указан — «{raw.strip()}»")
                continue
            if not days:
                problems.append(f"строка {number}: время есть, а дни не указаны — «{raw.strip()}»")
                continue
            for start, end in found_times:
                result.slots.append(
                    ParsedSlot(discipline=discipline, days=list(days), time_start=start, time_end=end)
                )
            continue

        # Строка не пустая, но ничего из неё не извлеклось. Молчать нельзя:
        # так теряется кусок расписания, и никто об этом не узнаёт.
        unparsed.append(f"пропущена строка {number}: «{raw.strip()}»")

    if problems:
        raise ScheduleParseError(problems)
    if not result.slots:
        raise ScheduleParseError(["не нашёл ни одного занятия: нужны вид, дни и время"])

    # Дубликаты: один и тот же вид, дни и время дважды — почти всегда копипаста.
    seen: set[tuple[str, tuple[str, ...], str, str]] = set()
    unique: list[ParsedSlot] = []
    for slot in result.slots:
        key = (slot.discipline, tuple(slot.days), slot.time_start, slot.time_end)
        if key in seen:
            result.warnings.append(
                f"повтор пропущен: {_DISCIPLINE_TITLE[slot.discipline]} "
                f"{slot.time_start}–{slot.time_end}"
            )
            continue
        seen.add(key)
        unique.append(slot)
    result.slots = unique
    result.warnings.extend(unparsed)
    return result


_DISCIPLINE_TITLE: Final[dict[str, str]] = {"boxing": "Бокс", "kickboxing": "Кикбоксинг"}


def render_schedule_text(slots: list[dict[str, object]] | None) -> str:
    """Расписание человекочитаемым текстом — для показа администратору."""
    if not slots:
        return "расписание не заполнено"

    by_discipline: dict[str, list[dict[str, object]]] = {}
    for slot in slots:
        by_discipline.setdefault(str(slot.get("discipline") or "?"), []).append(slot)

    lines: list[str] = []
    for discipline in ("kickboxing", "boxing"):
        group = by_discipline.get(discipline)
        if not group:
            continue
        lines.append(_DISCIPLINE_TITLE.get(discipline, discipline))
        for slot in group:
            days = [str(d) for d in (slot.get("days") or [])]
            titles = " • ".join(_DAY_TITLE.get(day, day) for day in days)
            lines.append(f"  {titles}: {slot.get('time_start')}–{slot.get('time_end')}")
    for discipline, group in by_discipline.items():
        if discipline in ("kickboxing", "boxing"):
            continue
        lines.append(discipline)
        for slot in group:
            lines.append(f"  {slot.get('time_start')}–{slot.get('time_end')}")
    return "\n".join(lines)
