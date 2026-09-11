"""Посёлок клиента → городской или районный прайс.

Живой прогон 10.09.2026: клиент из Тобыла получил карточку «Районные центры —
10 000 ₸», хотя владелец подтвердил, что цена у школы одна. Модель решает, город
это или райцентр, по догадке; название посёлка в словах клиента решает надёжнее.
"""

from __future__ import annotations

import re
from typing import Final, Iterable

from app.kb.models import KBSnapshot
from app.types import Scope

__all__ = ["scope_by_place", "scope_by_texts"]

_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[^\W\d_]+", re.UNICODE)


def _norm(value: str) -> str:
    return value.strip().lower().replace("ё", "е")


def _stem(name: str) -> str:
    """Основа названия: «Тобыла» и «Тобыле» — это Тобыл, а имя «Фёдор» — не Фёдоровка."""
    clean = _norm(name)
    return clean[: max(5, len(clean) - 2)]


def scope_by_place(kb: KBSnapshot, text: str | None) -> Scope | None:
    """``CITY`` для города и его пригородов, ``REGION`` для райцентра, иначе ``None``."""
    city = [_stem(place) for place in (kb.pricing.city_settlement, *kb.gyms.city_suburbs)]
    region = [_stem(place) for place in kb.pricing.region_settlements]
    for word in _WORD_RE.findall(text or ""):
        lowered = _norm(word)
        if len(lowered) < 4:
            continue
        if any(lowered.startswith(stem) for stem in city):
            return Scope.CITY
        if any(lowered.startswith(stem) for stem in region):
            return Scope.REGION
    return None


def scope_by_texts(kb: KBSnapshot, texts: Iterable[str]) -> Scope | None:
    """Посёлок из последней реплики клиента, где он назван."""
    for text in reversed(list(texts)):
        scope = scope_by_place(kb, text)
        if scope is not None:
            return scope
    return None
