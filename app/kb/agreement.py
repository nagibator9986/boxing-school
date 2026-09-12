"""Короткое согласие клиента на предложение бота.

Живая переписка 11.09.2026: бот предложил «Занятия по боксу проходят по
понедельникам, средам и пятницам в 19:00. Записать Айназарова Али на ближайшее
занятие в понедельник?», клиент ответил «Да» — и разговор ушёл администратору.
Модель не видела, на что клиент согласился, а инструмент записи не принимал
время, которого клиент не набирал сам.

Голое «Да» здесь разворачивается в согласие на конкретный текст бота — так же,
как цифра меню разворачивается в пункт меню. Пометка остаётся в словах клиента,
поэтому её видят и модель, и проверки «это назвал сам клиент».
"""

from __future__ import annotations

import re
from typing import Final, Iterable

__all__ = [
    "CHOICE_OPEN",
    "MARKER_CLOSE",
    "MARKER_OPEN",
    "chosen_option",
    "is_bare_agreement",
    "is_option_line",
    "offers_choice",
    "offered_options",
    "question_sentence",
    "split_agreement",
    "with_agreement",
    "with_choice",
]

MARKER_OPEN: Final[str] = "[согласие на предложение бота: «"
MARKER_CLOSE: Final[str] = "»]"
#: Выбор из списка вариантов, который предложил бот: «2» → вариант номер два.
CHOICE_OPEN: Final[str] = "[выбор из вариантов бота: «"

#: «1. Бокс — Вт, Чт, Сб 19:00», «2) Кикбоксинг».
_NUMBERED_OPTION_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(\d{1,2})\s*[.)]\s+(\S.*?)\s*$")
#: «— Кикбоксинг — Вт, Чт, Сб 19:00», «• Бокс».
_BULLET_OPTION_RE: Final[re.Pattern[str]] = re.compile(r"^\s*[—–•·-]\s+(\S.*?)\s*$")
#: Просьба выбрать вместо вопроса: «Выберите время:», «Напишите номер или своими словами.»
_CHOICE_REQUEST_RE: Final[re.Pattern[str]] = re.compile(
    r"выбер\w*|напишите\s+(?:цифру|номер)|санын\s+жазыңыз|нөмірін\s+жазыңыз|таңдаңыз", re.IGNORECASE
)
#: Голый номер варианта: «2», «2.», «2)».
_BARE_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(\d{1,2})\s*[.)]?\s*$")

#: Длиннее — это уже не «да», а ответ со своим содержанием: его разбирает модель.
_MAX_AGREEMENT_WORDS: Final[int] = 6

#: С этими словами согласие остаётся согласием: «Да, в 19», «Давайте в понедельник».
#: Время и день из такой реплики разбираются как собственные слова клиента.
_TIME_WORDS: Final[frozenset[str]] = frozenset(
    {
        "в", "во", "на", "к", "с", "до", "часов", "часа", "час", "утра", "дня", "вечера",
        # «спасибо» сюда не входит: «Ок, спасибо» — чаще вежливое прощание, чем «да».
        "утром", "вечером", "тогда", "ну", "пожалуйста",
        "пн", "вт", "ср", "чт", "пт", "сб", "вс",
    }
)
_DAY_STEMS: Final[tuple[str, ...]] = (
    "понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресен",
    "дүйсенбі", "сейсенбі", "сәрсенбі", "бейсенбі", "жұма", "сенбі", "жексенбі",
)

#: Сколько последних знаков предложения бота сохраняется. Вопрос стоит в конце.
_MAX_PROPOSAL_CHARS: Final[int] = 400

_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[^\W\d_]+", re.UNICODE)
_SENTENCE_RE: Final[re.Pattern[str]] = re.compile(r"[^.!?\n]*\?")


def _norm(word: str) -> str:
    return word.casefold().replace("ё", "е")


def is_bare_agreement(text: str | None, words: Iterable[str]) -> bool:
    """Согласие без своего содержания: «Да», «Да, давайте», «Иә», «Да, в 19».

    Кроме слов согласия допускаются только время и день недели. «Да, но в среду» —
    уже возражение: его разбирает модель, и предложение бота к нему не приписывается.
    """
    tokens = [_norm(token) for token in _WORD_RE.findall(text or "")]
    if not tokens or len(tokens) > _MAX_AGREEMENT_WORDS:
        return False
    vocabulary = {_norm(word) for word in words if word}
    if not any(token in vocabulary for token in tokens):
        return False
    return all(
        token in vocabulary or token in _TIME_WORDS or token.startswith(_DAY_STEMS) for token in tokens
    )


def with_agreement(text: str, bot_text: str | None, words: Iterable[str]) -> str:
    """«Да» → «Да [согласие на предложение бота: «…»]». Иначе текст без изменений.

    Разворачивается только ответ на вопрос: «Да» на утверждение бота ничего не
    выбирает, и приписывать клиенту согласие там не на что.
    """
    proposal = " ".join((bot_text or "").split())
    if "?" not in proposal or MARKER_OPEN in (text or "") or not is_bare_agreement(text, words):
        return text
    if len(proposal) > _MAX_PROPOSAL_CHARS:
        proposal = "…" + proposal[-_MAX_PROPOSAL_CHARS:]
    return f"{(text or '').strip()} {MARKER_OPEN}{proposal}{MARKER_CLOSE}"


def split_agreement(text: str | None) -> tuple[str, str | None]:
    """Собственные слова клиента и текст предложения, на которое он согласился.

    Выбранный из списка вариант (:data:`CHOICE_OPEN`) словами клиента не считается —
    его отдаёт :func:`chosen_option`: в «Бокс — для детей 7–9 лет» нет ни имени, ни
    возраста ребёнка.
    """
    value = text or ""
    choice = value.find(CHOICE_OPEN)
    if choice >= 0:
        return value[:choice].strip(), None
    start = value.find(MARKER_OPEN)
    if start < 0:
        return value, None
    end = value.rfind(MARKER_CLOSE)
    body_end = end if end > start else len(value)
    return value[:start].strip(), value[start + len(MARKER_OPEN) : body_end]


def question_sentence(proposal: str | None) -> str:
    """Последнее вопросительное предложение: на него клиент и ответил «да»."""
    found = _SENTENCE_RE.findall(proposal or "")
    return found[-1].strip() if found else ""


def offered_options(bot_text: str | None) -> list[str]:
    """Варианты, которые бот предложил списком, по порядку номеров.

    Нумерованный список берётся по номерам — только если они идут подряд с единицы.
    Маркированный («— Бокс», «• Кикбоксинг») — по порядку строк: модель пишет варианты
    и так, а клиент всё равно отвечает цифрой.
    """
    numbered: dict[int, str] = {}
    bullets: list[str] = []
    for line in (bot_text or "").splitlines():
        numbered_match = _NUMBERED_OPTION_RE.match(line)
        if numbered_match:
            numbered.setdefault(int(numbered_match.group(1)), numbered_match.group(2))
            continue
        bullet_match = _BULLET_OPTION_RE.match(line)
        if bullet_match:
            bullets.append(bullet_match.group(1))
    if len(numbered) >= 2 and sorted(numbered) == list(range(1, len(numbered) + 1)):
        return [numbered[number] for number in sorted(numbered)]
    return bullets if len(bullets) >= 2 else []


def offers_choice(bot_text: str | None) -> bool:
    """Предлагает ли бот выбрать из списка: два варианта и больше, вопрос или просьба выбрать.

    Живой прогон 12.09.2026: «Выберите подходящее время для пробного занятия: — …
    Напишите номер или своими словами.» — без единого вопросительного знака.
    """
    text = bot_text or ""
    return len(offered_options(text)) >= 2 and ("?" in text or _CHOICE_REQUEST_RE.search(text) is not None)


def is_option_line(line: str) -> bool:
    """Строка варианта в списке выбора: «1. Бокс», «— Кикбоксинг — Вт, Чт, Сб 19:00»."""
    return bool(_NUMBERED_OPTION_RE.match(line) or _BULLET_OPTION_RE.match(line))


def with_choice(text: str, bot_text: str | None) -> str:
    """«2» → «2 [выбор из вариантов бота: «Бокс — Вт, Чт, Сб 19:00»]». Иначе текст как есть.

    Кнопок в мессенджере нет: на список бота клиент отвечает цифрой. Скриншот
    владельца 12.09.2026: бот предложил «— Кикбоксинг … — Бокс …», клиент ответил «2»,
    а модель цифру не поняла, переспросила и передала запись администратору.
    Разворачивается только ответ на вопрос со списком.
    """
    number = _BARE_NUMBER_RE.match(text or "")
    if number is None or not offers_choice(bot_text):
        return text
    options = offered_options(bot_text)
    index = int(number.group(1))
    if not 1 <= index <= len(options):
        return text
    return f"{(text or '').strip()} {CHOICE_OPEN}{options[index - 1]}{MARKER_CLOSE}"


def chosen_option(text: str | None) -> str | None:
    """Вариант, который клиент выбрал цифрой. ``None`` — это не выбор из списка."""
    value = text or ""
    start = value.find(CHOICE_OPEN)
    if start < 0:
        return None
    end = value.rfind(MARKER_CLOSE)
    return value[start + len(CHOICE_OPEN) : end if end > start else len(value)]
