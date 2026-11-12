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

__all__ = ["place_words", "scope_by_place", "scope_by_texts"]

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
    # Райцентры выключены — районного прайса нет ни для кого, даже для назвавшего посёлок.
    region = [_stem(place) for place in kb.pricing.region_settlements] if kb.has_region_gyms() else []
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


#: Слова мест по версии базы: строятся один раз на снимок, а проверяются на каждом ходу.
_PLACE_WORDS: dict[str, frozenset[str]] = {}


def place_words(kb: KBSnapshot) -> frozenset[str]:
    """Слова, которыми называют залы и посёлки школы: названия залов, город, пригороды, райцентры.

    Нужны разбору имени: «Плаза» в ответ на «как зовут ребёнка?» — зал, а не имя. Список
    в коде знал только залы, бывшие при его написании; зал, заведённый в CRM, — нет.
    Районы и их другие имена сюда не входят: среди них «Арман» и «Алтын» — частые имена.
    """
    cached = _PLACE_WORDS.get(kb.kb_hash)
    if cached is not None:
        return cached
    sources: list[str] = [kb.gyms.city_settlement, *kb.gyms.city_suburbs, *kb.pricing.region_settlements]
    for gym in kb.gyms.gyms:
        sources.extend((gym.settlement, gym.title.ru or "", gym.title.kk or ""))
    words = frozenset(
        _norm(word) for source in sources for word in _WORD_RE.findall(source or "") if len(word) >= 3
    )
    if len(_PLACE_WORDS) > 8:
        _PLACE_WORDS.clear()
    _PLACE_WORDS[kb.kb_hash] = words
    return words
