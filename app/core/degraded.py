"""Ответ из базы знаний на случай, когда модель недоступна.

Когда Gemini не отвечает — кончились кредиты на ключе, лежит сеть, ответ
забраковал постфильтр — клиент до сих пор получал одну строку «у меня сбой на
стороне сервиса». Это честно, но расточительно: цены, адреса залов и расписание
лежат в ``kb/*.yaml``, собираются кодом карточек и модели для этого не нужны
вовсе. Самые частые вопросы переписки — «сколько стоит», «где вы», «когда
занятия» — можно ответить и с мёртвой моделью.

Выдумать здесь нечего по построению: любые данные берутся из снимка KB тем же
рендером, что и в рабочем ходе. Отличается только выбор карточки — его делает
не модель, а интент из лексикона. Интент не распознан или тема деликатная —
функция возвращает ``None``, и наверху остаётся прежняя честная заглушка.

Ответ обязан быть утверждением, а не вопросом: после деградации бот встаёт на
паузу и зовёт человека, поэтому «напишите номер зала» осталось бы без ответа.
Поэтому на «когда занятия» без известного зала уходит расписание всех залов
города, а не просьба уточнить.
"""

from __future__ import annotations

from typing import Final, Sequence

from app.kb.models import KBSnapshot
from app.kb.render import render_gyms_list_card, render_price_card, render_schedule_card
from app.types import IntentHint, Language, Scope

__all__ = ["TAIL_KEY", "kb_answer"]

#: Ключ i18n с хвостовой строкой «администратор уже подключается».
TAIL_KEY: Final[str] = "error.degraded_tail"

#: Интенты, на которые база знаний отвечает сама. Порядок — приоритет: в
#: «сколько стоит и когда занятия» человек в первую очередь ждёт цену.
_ANSWERABLE: Final[tuple[IntentHint, ...]] = (
    IntentHint.PRICE,
    IntentHint.SCHEDULE,
    IntentHint.LOCATION,
)

#: Темы, на которые карточка неуместна, даже если рядом стоит подходящий интент.
#: «Отпишите меня» и «ребёнок получил травму» с прайсом в ответ — это хуже
#: молчания; такие сообщения обязан читать человек, и только он.
_SUPPRESS: Final[tuple[IntentHint, ...]] = (
    IntentHint.STOP,
    IntentHint.ERASE,
    IntentHint.SAFETY,
    IntentHint.MANAGER,
)


def _price(kb: KBSnapshot, lang: Language) -> str | None:
    """Оба тарифа сразу: город и районные центры.

    Какой из них нужен клиенту, обычно выясняет модель вопросом про район, а
    её здесь нет. Показать только городской нельзя: в райцентрах абонемент
    стоит 10 000 против 25 000, и «ошиблись в 2,5 раза» — это не деградация, а
    дезинформация. Два блока рядом честны при любом ответе на незаданный вопрос.
    """
    parts = [
        _safe(render_price_card, kb, scope=Scope.CITY, lang=lang),
        _safe(render_price_card, kb, scope=Scope.REGION, lang=lang),
    ]
    body = "\n\n".join(part for part in parts if part)
    return body or None


def _schedule(kb: KBSnapshot, lang: Language) -> str | None:
    """Расписание всех городских залов, у которых оно заполнено."""
    cards = []
    for gym in kb.active_gyms(Scope.CITY):
        if not gym.schedule:
            continue
        card = _safe(render_schedule_card, kb, gym_id=gym.id, slots=gym.schedule, lang=lang)
        if card:
            cards.append(card)
    return "\n\n".join(cards) or None


def _location(kb: KBSnapshot, lang: Language) -> str | None:
    """Список залов города; карточка сама допишет строку про область.

    Из карточки вычёркивается её последняя строка — «напишите номер зала,
    пришлю расписание и точку на карте». В рабочем ходе это приглашение к
    следующему шагу, а здесь бот сразу встаёт на паузу и зовёт человека:
    обещание, которого он не выполнит, хуже, чем его отсутствие.
    """
    card = _safe(render_gyms_list_card, kb, scope=Scope.CITY, lang=lang)
    return _without_line(card, kb.text("card.pick_gym", lang)) if card else None


def _without_line(card: str, line: str) -> str | None:
    """Убирает строку из готовой карточки, не оставляя пустого хвоста."""
    wanted = (line or "").strip()
    if not wanted:
        return card
    kept = [row for row in card.splitlines() if row.strip() != wanted]
    return "\n".join(kept).strip() or None


def _safe(render, kb: KBSnapshot, **kwargs) -> str | None:
    """Рендер, который не имеет права уронить и без того аварийный ход."""
    try:
        return (render(kb, **kwargs) or "").strip() or None
    except Exception:  # noqa: BLE001 - в аварии молчание лучше второго исключения
        return None


_RENDERERS: Final[dict[IntentHint, object]] = {
    IntentHint.PRICE: _price,
    IntentHint.SCHEDULE: _schedule,
    IntentHint.LOCATION: _location,
}


def kb_answer(
    kb: KBSnapshot, *, intents: Sequence[IntentHint], lang: Language
) -> str | None:
    """Готовая карточка на вопрос клиента или ``None``, если ответа в KB нет.

    Хвостовую строку про администратора добавляет вызывающий: он же знает,
    ушла карточка администратору или нет.
    """
    if any(intent in _SUPPRESS for intent in intents):
        return None
    for intent in _ANSWERABLE:
        if intent not in intents:
            continue
        body = _RENDERERS[intent](kb, lang)  # type: ignore[operator]
        if body:
            return body
    return None
