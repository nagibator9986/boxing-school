"""Наводящий вопрос в конце ответа: бот ведёт клиента к записи.

Владелец 09.09.2026: «бот должен вести клиента, потому что это бот-консультант
плюс бот-продажник, в конце каждого сообщения он должен задавать наводящие по
воронке вопросы».

Правило записано и в промпт, но одного промпта мало: модель регулярно
заканчивает ответ фактом («Абонемент — 25 000 ₸.») и разговор упирается в
тишину. Здесь тот же шаг делается кодом — по тому, чего боту ещё не хватает для
записи, а не наугад.

Порядок шагов повторяет живой разговор администратора: возраст → район → время →
имя → номер → подтверждение. Спрашивается всегда ОДИН, ближайший незакрытый шаг.
"""

from __future__ import annotations

import re
from typing import Final, Sequence

from app.kb.models import KBSnapshot
from app.types import Language, LeadDraft, OutboundMessage

__all__ = [
    "ask_full_name",
    "drop_questions",
    "ends_with_question",
    "next_step_key",
    "pending_question",
    "turn_showed_a_gym",
    "with_funnel_question",
]

#: Карточки, после которых пора предлагать запись: клиенту уже показали зал.
_GYM_SHOWN_PREFIXES: Final[tuple[str, ...]] = ("route_", "schedule_")

_SENTENCE_SPLIT: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?])\s+")

#: Хвост ответа, в котором ищем вопрос. Знак вопроса в середине текста —
#: обычно цитата клиента или риторический оборот, а не приглашение ответить.
_TAIL_CHARS: Final[int] = 160

#: Шаги воронки: чего не хватает → какой вопрос задать.
_STEPS: Final[tuple[tuple[str, str], ...]] = (
    ("child_age", "funnel.age"),
    ("gym_id", "funnel.district"),
    ("child_name", "funnel.name"),
    ("contact", "funnel.contact"),
)

#: Когда для записи есть всё, остаётся предложить сам шаг записи.
_LAST_STEP: Final[str] = "funnel.confirm"

#: Ответ, которым бот передаёт разговор человеку. Дописывать к нему продающий
#: вопрос нельзя: клиент ждёт администратора, а не анкету. Живой пример —
#: жалоба на тренера, после которой бот спрашивал возраст ребёнка.
_HANDOVER_MARKERS: Final[tuple[str, ...]] = (
    "передаю администратор",
    "передам администратор",
    "ответит администратор",
    "передаю ваш вопрос",
    "передаю менеджер",
    "әкімшіге",
    "әкімші жауап",
)


#: Просьба вместо вопроса: «Подскажите фамилию сына.» ждёт ответа так же, как «?».
#: Живой прогон 10.09.2026: к такой просьбе воронка дописала «сколько лет ребёнку?».
_REQUEST_RU_RE: Final[re.Pattern[str]] = re.compile(
    r"^[\s\W]*(?:и\s+)?(?:пожалуйста,?\s+)?"
    r"(?:подскажите|напишите|уточните|скажите|назовите|пришлите|отправьте|выберите)(?![а-яё])",
    re.IGNORECASE,
)
_REQUEST_KK_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:жазыңыз|айтыңыз|таңдаңыз|жіберіңіз)[\s.!]*$", re.IGNORECASE
)
#: «Напишите, если появятся вопросы» — вежливое завершение, отвечать на него нечего.
_REQUEST_EXCEPT_RE: Final[re.Pattern[str]] = re.compile(r"(?<![а-яё])если(?![а-яё])|егер", re.IGNORECASE)

#: Вопрос об имени ребёнка без фамилии: «Как зовут ребёнка?», «Как его зовут?».
_NAME_QUESTION_RE: Final[re.Pattern[str]] = re.compile(
    r"как\s+(?:(?:его|её|ее)\s+)?(?:зовут|звать)(?:\s+(?:вашего|вашу|вашей)?\s*"
    r"(?:ребён|ребен|сын|доч|малыш|мальчик|девочк))?(?![а-яё])?"
    r"|имя\s+(?:вашего\s+|вашей\s+)?(?:ребён|ребен|сын|доч|малыш|мальчик|девочк)"
    r"|(?:баланың|ұлыңыздың|қызыңыздың)\s+(?:аты|есімі)",
    re.IGNORECASE,
)
_CHILD_WORD_RE: Final[re.Pattern[str]] = re.compile(
    r"ребён|ребен|сын|доч|малыш|мальчик|девочк|его\s+зовут|её\s+зовут|ее\s+зовут|бала|ұл|қыз",
    re.IGNORECASE,
)
_SURNAME_RE: Final[re.Pattern[str]] = re.compile(r"фамили|тегі", re.IGNORECASE)
_AGE_ASK_RE: Final[re.Pattern[str]] = re.compile(r"лет|возраст|жас", re.IGNORECASE)


def _is_request(sentence: str) -> bool:
    text = (sentence or "").strip()
    if not text or _REQUEST_EXCEPT_RE.search(text):
        return False
    return bool(_REQUEST_RU_RE.search(text) or _REQUEST_KK_RE.search(text))


def ask_full_name(reply: str, *, kb: KBSnapshot, lang: Language) -> str:
    """Вопрос «Как зовут ребёнка?» заменяется вопросом базы о фамилии и имени.

    Владелец 10.09.2026: «для записи спрашивать не только имя, а ФИ». Правило есть
    в промпте и в подсказке инструмента, но живой прогон всё равно дал «Как зовут
    ребёнка и сколько ему лет?». Ответ, где фамилию уже спрашивают, не трогается.
    """
    body = reply or ""
    if not body.strip() or _SURNAME_RE.search(body):
        return reply
    lines: list[str] = []
    replaced = False
    for line in body.splitlines():
        sentences = _SENTENCE_SPLIT.split(line)
        for index, sentence in enumerate(sentences):
            if replaced or not _NAME_QUESTION_RE.search(sentence) or not _CHILD_WORD_RE.search(sentence):
                continue
            key = "funnel.name_age" if _AGE_ASK_RE.search(sentence) else "funnel.name"
            question = (kb.text(key, lang) or "").strip()
            if question:
                sentences[index] = question
                replaced = True
        lines.append(" ".join(sentences))
    return "\n".join(lines) if replaced else reply


def _is_handover(text: str) -> bool:
    """Похоже ли, что этим ответом бот передаёт разговор человеку."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _HANDOVER_MARKERS)


def turn_showed_a_gym(messages: Sequence[OutboundMessage]) -> bool:
    """Ушли ли в этом ходу расписание или видео дороги до зала."""
    return any(
        str(getattr(message, "artifact_id", "") or "").startswith(_GYM_SHOWN_PREFIXES)
        for message in messages
    )


def drop_questions(reply: str) -> str:
    """Ответ модели без её вопросов: единственным вопросом хода станет предложение записи.

    Предложение записи после расписания и видео отправляет код — отдельным
    последним сообщением. Живой прогон 10.09.2026: клиент получил «Какое время из
    расписания вам подходит?» и следом «Записать ребёнка на первую пробную
    тренировку?» — два вопроса подряд. Остальные предложения строки сохраняются.
    """
    kept: list[str] = []
    for line in (reply or "").splitlines():
        sentences = [part for part in _SENTENCE_SPLIT.split(line) if part.strip()]
        useful = [part for part in sentences if "?" not in part and not _is_request(part)]
        if sentences and not useful:
            continue
        kept.append(" ".join(useful) if len(useful) != len(sentences) else line)
    return "\n".join(kept).strip()


def ends_with_question(text: str) -> bool:
    """Есть ли в конце ответа вопрос — то, на что клиенту хочется ответить."""
    body = (text or "").strip()
    if "?" in body[-_TAIL_CHARS:]:
        return True
    lines = [line for line in body.splitlines() if line.strip()]
    sentences = [part for part in _SENTENCE_SPLIT.split(lines[-1]) if part.strip()] if lines else []
    return bool(sentences) and _is_request(sentences[-1])


def next_step_key(draft: LeadDraft) -> str:
    """Ключ вопроса для ближайшего незакрытого шага воронки."""
    missing = set(draft.missing_required())
    for field, key in _STEPS:
        if field in missing:
            return key
    return _LAST_STEP


def pending_question(
    reply: str,
    *,
    draft: LeadDraft,
    kb: KBSnapshot,
    lang: Language,
    farewell: bool = False,
) -> str | None:
    """Какой вопрос дописать к ответу. ``None`` — дописывать не нужно.

    Отделено от самой склейки: длинный ответ режется по лимиту канала, и вопрос
    обязан попасть в ПОСЛЕДНЮЮ отправленную часть, а не потеряться вместе с
    отброшенным хвостом.
    """
    body = (reply or "").strip()
    if not body or farewell or _is_handover(body) or ends_with_question(body):
        return None
    question = (kb.text(next_step_key(draft), lang) or "").strip()
    if not question or question in body:
        return None
    return question


def with_funnel_question(
    reply: str, *, draft: LeadDraft, kb: KBSnapshot, lang: Language, farewell: bool = False
) -> str:
    """Дописывает наводящий вопрос, если модель закончила ответ без него.

    Ответ, который уже спрашивает клиента, не трогается: второй вопрос подряд
    читается как допрос, и владелец про это говорил отдельно.

    ``farewell`` — клиент попрощался. Догонять вопросом того, кто закончил
    разговор, значит выпрашивать ответ; на это владелец жаловался, когда
    напоминания уходили после «спасибо большое».

    Ответ, которым бот зовёт администратора, тоже остаётся без вопроса: после
    жалобы на тренера «сколько лет ребёнку?» выглядит издевательством.
    """
    question = pending_question(reply, draft=draft, kb=kb, lang=lang, farewell=farewell)
    if question is None:
        return reply
    return f"{(reply or '').strip()}\n\n{question}"
