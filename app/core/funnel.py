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

from typing import Final

from app.kb.models import KBSnapshot
from app.types import Language, LeadDraft

__all__ = ["ends_with_question", "next_step_key", "pending_question", "with_funnel_question"]

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


def _is_handover(text: str) -> bool:
    """Похоже ли, что этим ответом бот передаёт разговор человеку."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _HANDOVER_MARKERS)


def ends_with_question(text: str) -> bool:
    """Есть ли в конце ответа вопрос — то, на что клиенту хочется ответить."""
    tail = (text or "").strip()[-_TAIL_CHARS:]
    return "?" in tail


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
