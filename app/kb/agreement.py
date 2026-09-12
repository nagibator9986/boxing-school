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
    "MARKER_CLOSE",
    "MARKER_OPEN",
    "is_bare_agreement",
    "question_sentence",
    "split_agreement",
    "with_agreement",
]

MARKER_OPEN: Final[str] = "[согласие на предложение бота: «"
MARKER_CLOSE: Final[str] = "»]"

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
    """Собственные слова клиента и текст предложения, на которое он согласился."""
    value = text or ""
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
