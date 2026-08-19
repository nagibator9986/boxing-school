"""Залы школы — инструменты ``get_gyms`` и ``find_gym_by_district``.

Родитель почти никогда не называет зал так, как он записан в базе. Он пишет
«кск», «возле рахата», «жанакала», «6 мкр», «kzhbi», «шк 9» — и ошибается в
каждом втором слове. Поэтому здесь живёт собственный слой нормализации и
нечёткого сравнения: внешних библиотек (rapidfuzz, python-Levenshtein) в
проекте нет намеренно, зависимость ради одной функции — плохой обмен.

Два правила, которые важнее качества поиска:

* **зал не выдумывается никогда.** Нет совпадения — пустой список и предложение
  показать полный перечень, а не «ближайший, наверное, такой-то»;
* **два зала в «Центре» не сливаются в один.** Касымханова 10 — это Жана-Кала,
  Каирбекова 24 — это «Рахат». Абстрактного «центрального зала» не существует,
  и бот обязан различать их по ориентиру.
"""

from __future__ import annotations

import re
from typing import Any, Final, Sequence

from app.kb.gaps import say_no_data, say_no_data_lang
from app.kb.models import Gym, KBSnapshot
from app.types import (
    EscalationReason,
    GapRef,
    Language,
    RenderHint,
    Scope,
    ToolContext,
    ToolResult,
)

#: Сколько залов максимум отдаёт ``find_gym_by_district`` (контракт §11.4).
MAX_DISTRICT_MATCHES: Final[int] = 3

#: Потолок выдачи ``get_gyms``.
MAX_GYMS_LIMIT: Final[int] = 12

#: Ниже этого веса совпадение считается случайным и не показывается.
MIN_MATCH_SCORE: Final[int] = 50

#: Свёртка казахских букв и «ё» к русской основе: «жаңа қала» и «жана кала» —
#: для клиента одно и то же слово.
_FOLD_MAP: Final[dict[int, str]] = str.maketrans(
    {
        "ә": "а",
        "ғ": "г",
        "қ": "к",
        "ң": "н",
        "ө": "о",
        "ұ": "у",
        "ү": "у",
        "һ": "х",
        "і": "и",
        "ё": "е",
        "ъ": "",
        "ь": "",
    }
)

#: Латиница -> кириллица для транслита («zhitikara», «shkola»). Порядок важен:
#: двухбуквенные сочетания разбираются раньше одиночных букв.
_TRANSLIT_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    ("shch", "щ"), ("sch", "щ"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"),
    ("sh", "ш"), ("yu", "ю"), ("ya", "я"), ("yo", "е"), ("iy", "и"), ("ye", "е"),
    ("a", "а"), ("b", "б"), ("v", "в"), ("g", "г"), ("d", "д"), ("e", "е"),
    ("z", "з"), ("i", "и"), ("j", "ж"), ("k", "к"), ("l", "л"), ("m", "м"),
    ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"), ("s", "с"), ("t", "т"),
    ("u", "у"), ("f", "ф"), ("h", "х"), ("c", "к"), ("y", "и"), ("q", "к"),
    ("w", "в"), ("x", "кс"),
)

_LATIN_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+$")
_NON_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[^0-9a-zа-яё\s]+")
_SPACES_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Нормализация и расстояние
# --------------------------------------------------------------------------- #
def normalize_text(value: str | None) -> str:
    """Приводит текст клиента к сравнимому виду.

    Нижний регистр, свёртка казахских букв и «ё», выброс пунктуации, схлопывание
    пробелов. Результат используют все инструменты, где сравниваются слова
    клиента и строки базы знаний.
    """
    if not value:
        return ""
    folded = value.strip().lower().translate(_FOLD_MAP)
    cleaned = _NON_WORD_RE.sub(" ", folded)
    return _SPACES_RE.sub(" ", cleaned).strip()


def translit_to_cyrillic(value: str) -> str:
    """Грубый транслит латиницы в кириллицу: ``zhitikara`` -> ``житикара``.

    Нужен для клиентов, которые пишут русские слова латиницей. Обратное
    направление не требуется: латинские варианты уже лежат в ``district_aliases``.
    """
    result = value
    for latin, cyrillic in _TRANSLIT_PAIRS:
        result = result.replace(latin, cyrillic)
    return result


def _variants(text: str) -> tuple[str, ...]:
    """Нормализованный текст плюс его транслит, если он был написан латиницей."""
    base = normalize_text(text)
    if not base:
        return ()
    if _LATIN_RE.match(base.replace(" ", "")):
        return (base, translit_to_cyrillic(base))
    return (base,)


def levenshtein(left: str, right: str, *, max_distance: int | None = None) -> int:
    """Расстояние Левенштейна. Своя реализация: внешних зависимостей нет.

    ``max_distance`` включает раннее прекращение — при явно далёких строках
    считать всю матрицу незачем.
    """
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    if max_distance is not None and abs(len(left) - len(right)) > max_distance:
        return max_distance + 1
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        best = i
        for j, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            best = min(best, value)
        previous = current
        if max_distance is not None and best > max_distance:
            return max_distance + 1
    return previous[-1]


def fuzzy_threshold(length: int) -> int:
    """Сколько опечаток прощаем строке такой длины."""
    if length <= 4:
        return 0
    if length <= 6:
        return 1
    if length <= 10:
        return 2
    return 3


def _digits(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\d+", value))


def is_fuzzy_match(left: str, right: str) -> tuple[bool, int]:
    """Похожи ли две уже нормализованные строки. Возвращает ``(да/нет, расстояние)``.

    Числа сравниваются точно, а не «на глаз»: «каирбекова 24» и «каирбекова 334» —
    это два разных зала на одной улице, и одна опечатка не имеет права их склеить.
    """
    if not left or not right:
        return False, 99
    left_digits, right_digits = _digits(left), _digits(right)
    if left_digits and right_digits and left_digits != right_digits:
        return False, 99
    limit = fuzzy_threshold(min(len(left), len(right)))
    if limit == 0:
        return left == right, 0 if left == right else 99
    distance = levenshtein(left, right, max_distance=limit)
    return distance <= limit, distance


def _contains_phrase(haystack: str, needle: str) -> bool:
    """Вхождение фразы по границам слов: «кск» не должен ловиться в «кскачество»."""
    if not needle or not haystack:
        return False
    return re.search(rf"(?<![0-9a-zа-я]){re.escape(needle)}(?![0-9a-zа-я])", haystack) is not None


# --------------------------------------------------------------------------- #
# Сериализация зала
# --------------------------------------------------------------------------- #
def _gym_payload(gym: Gym, lang: Language) -> dict[str, Any]:
    """Зал в том виде, в каком его увидит модель. Ничего сверх базы знаний."""
    return {
        "id": gym.id,
        "title": gym.title.get(lang) or gym.title.ru,
        "address": gym.address.get(lang),
        "landmark": gym.landmark.get(lang),
        "district": gym.district.get(lang),
        "settlement": gym.settlement,
        "scope": gym.scope.value,
        "is_head": gym.is_head,
        "has_schedule": gym.has_schedule,
        "phone": gym.phone,          # НП: проверить на проде — сегодня всегда null (G-2)
        "map_url": gym.map_url,      # сегодня всегда null (G-15)
    }


def _gap_caveats(kb: KBSnapshot, gyms: Sequence[Gym], lang: Language) -> list[str]:
    """Заглушки по пробелам, которые видны прямо в карточке зала."""
    caveats: list[str] = []
    if any(gym.phone is None for gym in gyms):
        caveats.append(say_no_data_lang(kb, GapRef.G2, lang))
    if any(gym.map_url is None and gym.geo_lat is None for gym in gyms):
        caveats.append(say_no_data_lang(kb, GapRef.G15, lang))
    if any(gym.scope is Scope.REGION and not gym.address.filled for gym in gyms):
        caveats.append(say_no_data_lang(kb, GapRef.G3, lang))
    if any(not gym.has_schedule for gym in gyms):
        caveats.append("Расписания в базе нет: время занятий не называй ни при каких условиях (G-1).")
    return caveats


#: Слова района, которые сами по себе ничего не различают.
_DISTRICT_STOPWORDS: Final[frozenset[str]] = frozenset({"район", "микрорайон", "мкр", "город"})


def _duplicate_districts(gyms: Sequence[Gym], lang: Language) -> list[str]:
    """Слова района, под которыми работает больше одного зала.

    Сравниваются именно слова, а не строка целиком: «Центр» и «Центр / Жана-Кала» —
    для родителя один и тот же «центр», и предлагать «центральный зал» без
    ориентира нельзя.
    """
    seen: dict[str, set[str]] = {}
    for gym in gyms:
        district = normalize_text(gym.district.get(lang) or gym.district.ru)
        for token in district.split():
            if len(token) >= 4 and token not in _DISTRICT_STOPWORDS:
                seen.setdefault(token, set()).add(gym.id)
    return sorted(token for token, owners in seen.items() if len(owners) > 1)


def _scope_of(value: str | None, *, allow_all: bool = True) -> Scope | None:
    """Строка -> :class:`Scope`; неизвестное значение -> ``None``."""
    if value is None:
        return None
    try:
        scope = Scope(str(value).strip().lower())
    except ValueError:
        return None
    if scope in (Scope.CITY, Scope.REGION):
        return scope
    if allow_all and scope in (Scope.ALL, Scope.ANY):
        return Scope.ALL
    return None


# --------------------------------------------------------------------------- #
# get_gyms
# --------------------------------------------------------------------------- #
async def get_gyms(
    ctx: ToolContext,
    *,
    scope: str,
    settlement: str | None = None,
    limit: int = MAX_GYMS_LIMIT,
) -> ToolResult:
    """Только ``active=True`` и ``status=open``, тексты на языке диалога. render_hint=VERBATIM."""
    kb = ctx.kb
    lang = ctx.lang
    parsed = _scope_of(scope)
    if parsed is None:
        return ToolResult.invalid_input(f"неизвестный scope '{scope}'")

    try:
        limit_value = int(limit)
    except (TypeError, ValueError):
        limit_value = MAX_GYMS_LIMIT
    limit_value = max(1, min(MAX_GYMS_LIMIT, limit_value))

    selected = list(kb.active_gyms(parsed))
    filtered_by_settlement = False
    if settlement:
        matched = _filter_by_settlement(selected, settlement)
        if matched:
            selected = matched
            filtered_by_settlement = True
        else:
            # Названный посёлок в базе не найден. Молча показать другие залы —
            # значит соврать, что «мы там есть».
            return ToolResult.success(
                data={
                    "scope": parsed.value,
                    "query_settlement": settlement,
                    "gyms": [],
                    "total": 0,
                    "total_in_scope": len(kb.active_gyms(parsed)),
                    "offer_full_list": True,
                    "settlements": sorted({gym.settlement for gym in kb.active_gyms(Scope.ALL)}),
                },
                render_hint=RenderHint.VERBATIM,
                caveats=[
                    "Совпадений по названному посёлку нет. Не подбирай похожий и не выдумывай зал: "
                    "предложи полный перечень точек и передай вопрос администратору."
                ],
                meta={"settlement_matched": False},
            )

    total = len(selected)
    shown = selected[:limit_value]
    caveats = _gap_caveats(kb, shown, lang)
    duplicates = _duplicate_districts(shown, lang)
    if duplicates:
        caveats.append(
            "В одном районе работает несколько залов: называй их только вместе с ориентиром "
            "(ориентир лежит в поле landmark), «центральный зал» без уточнения не существует."
        )
    if total > len(shown):
        caveats.append("Показаны не все точки — при необходимости скажи, что список можно продолжить.")

    return ToolResult.success(
        data={
            "scope": parsed.value,
            "query_settlement": settlement,
            "settlement_filtered": filtered_by_settlement,
            "gyms": [_gym_payload(gym, lang) for gym in shown],
            "total": len(shown),
            "total_in_scope": total,
            "city_settlement": kb.gyms.city_settlement,
            "duplicate_districts": duplicates,
        },
        render_hint=RenderHint.VERBATIM,
        caveats=caveats,
        meta={"gap_refs": ["G-2", "G-15"]},
    )


def _filter_by_settlement(gyms: Sequence[Gym], settlement: str) -> list[Gym]:
    """Отбор залов по названию населённого пункта с поправкой на опечатки."""
    queries = _variants(settlement)
    if not queries:
        return []
    matched: list[Gym] = []
    for gym in gyms:
        keys = {normalize_text(gym.settlement)} | {normalize_text(alias) for alias in gym.district_aliases}
        keys.discard("")
        for query in queries:
            if any(key == query or _contains_phrase(key, query) or _contains_phrase(query, key) for key in keys):
                matched.append(gym)
                break
            if any(is_fuzzy_match(query, key)[0] for key in keys):
                matched.append(gym)
                break
    return matched


# --------------------------------------------------------------------------- #
# find_gym_by_district
# --------------------------------------------------------------------------- #
#: Вес источника совпадения: (точное равенство, вхождение, опечатка).
_SOURCE_WEIGHTS: Final[dict[str, tuple[int, int, int]]] = {
    "alias": (100, 90, 80),
    "settlement": (95, 85, 75),
    "district": (75, 65, 55),
    "text": (70, 58, 50),
}

#: Какой ``match`` показывать модели для каждого источника (контракт §11.4).
_SOURCE_MATCH: Final[dict[str, str]] = {
    "alias": "alias",
    "settlement": "settlement",
    "district": "fallback",
    "text": "fallback",
}


def _gym_keys(gym: Gym) -> dict[str, tuple[str, ...]]:
    """Все строки зала, по которым имеет смысл искать, сгруппированные по источнику."""
    def _pair(*values: str | None) -> tuple[str, ...]:
        normalized = [normalize_text(value) for value in values]
        return tuple(dict.fromkeys(key for key in normalized if key))

    text_keys = _pair(
        gym.landmark.ru, gym.landmark.kk, gym.address.ru, gym.address.kk,
        gym.title.ru, gym.title.kk,
    )
    return {
        "alias": _pair(*gym.district_aliases),
        "settlement": _pair(gym.settlement),
        "district": _pair(gym.district.ru, gym.district.kk),
        "text": text_keys,
    }


def _query_forms(queries: Sequence[str]) -> tuple[tuple[str, int], ...]:
    """Формы запроса со штрафом: фраза целиком, пары слов, отдельные слова.

    Без разбора на слова «возле рахата» не найдёт зал с алиасом «рахат»: клиент
    почти никогда не пишет ровно то, что записано в базе.
    """
    forms: dict[str, int] = {}

    def _add(value: str, penalty: int) -> None:
        if value and forms.get(value, 99) > penalty:
            forms[value] = penalty

    for query in queries:
        _add(query, 0)
        tokens = [token for token in query.split() if len(token) >= 3]
        for index, token in enumerate(tokens):
            _add(token, 8)
            if index + 1 < len(tokens):
                _add(f"{token} {tokens[index + 1]}", 4)
    return tuple(sorted(forms.items(), key=lambda item: item[1]))


def _score_gym(gym: Gym, forms: Sequence[tuple[str, int]]) -> tuple[int, str, str]:
    """Вес совпадения зала с запросом: ``(score, тип совпадения, что совпало)``."""
    best_score = 0
    best_kind = "fallback"
    best_key = ""
    for source, keys in _gym_keys(gym).items():
        exact_w, contains_w, fuzzy_w = _SOURCE_WEIGHTS[source]
        for key in keys:
            for query, penalty in forms:
                score = 0
                if key == query:
                    score = exact_w
                elif _contains_phrase(query, key) or _contains_phrase(key, query):
                    score = contains_w - max(0, abs(len(key) - len(query)) // 8)
                else:
                    matched, distance = is_fuzzy_match(query, key)
                    if matched:
                        score = fuzzy_w - distance * 5
                score = score - penalty if score else 0
                if score > best_score:
                    best_score, best_kind, best_key = score, _SOURCE_MATCH[source], key
    return best_score, best_kind, best_key


async def find_gym_by_district(
    ctx: ToolContext,
    *,
    district_text: str,
    settlement: str | None = None,
) -> ToolResult:
    """Матчинг по district_aliases + лексикону, БЕЗ модели. До 3 залов, отсортированы по силе
    совпадения. Каждый элемент несёт match: "alias" | "settlement" | "fallback"."""
    kb = ctx.kb
    lang = ctx.lang
    if not district_text or not district_text.strip():
        return ToolResult.invalid_input("district_text пуст")

    queries = list(_variants(district_text))
    if settlement:
        queries.extend(_variants(settlement))
    queries = [query for query in dict.fromkeys(queries) if query]
    if not queries:
        return ToolResult.invalid_input("district_text не содержит слов для поиска")
    forms = _query_forms(queries)

    # Конфликт C-3: запись-заглушка КЖБИ живёт в базе с status=unresolved.
    # Попадание в неё запрещает утверждать и «зал есть», и «зала нет».
    unresolved_hit = None
    for gym in kb.unresolved_gyms():
        score, _, _ = _score_gym(gym, forms)
        if score >= MIN_MATCH_SCORE and (unresolved_hit is None or score > unresolved_hit[1]):
            unresolved_hit = (gym, score)

    scored: list[tuple[int, str, str, Gym]] = []
    for gym in kb.active_gyms(Scope.ALL):
        score, kind, key = _score_gym(gym, forms)
        if score >= MIN_MATCH_SCORE:
            scored.append((score, kind, key, gym))
    scored.sort(key=lambda row: (-row[0], row[3].id))

    if unresolved_hit is not None and (not scored or unresolved_hit[1] >= scored[0][0]):
        return ToolResult.needs_operator(
            say=say_no_data(kb, GapRef.C3),
            reason=EscalationReason.NO_DATA,
            gap_ref=GapRef.C3,
        )

    if not scored:
        known_without_gym = _known_district_without_gym(kb, queries)
        caveats = [
            "Совпадений нет. Зал придумывать нельзя: предложи полный перечень точек "
            "и спроси, какой район удобнее."
        ]
        if known_without_gym:
            caveats.insert(
                0,
                "Район клиенту знаком, но зала там нет — скажи об этом прямо и предложи "
                "ближайшие из существующих точек по списку.",
            )
        return ToolResult.success(
            data={
                "query": district_text,
                "gyms": [],
                "total": 0,
                "offer_full_list": True,
                "known_district_without_gym": known_without_gym,
                "districts": sorted(
                    {
                        gym.district.get(lang) or gym.district.ru or gym.settlement
                        for gym in kb.active_gyms(Scope.ALL)
                    }
                ),
            },
            render_hint=RenderHint.VERBATIM,
            caveats=caveats,
            meta={"matched": False},
        )

    top = scored[:MAX_DISTRICT_MATCHES]
    payload: list[dict[str, Any]] = []
    for score, kind, key, gym in top:
        item = _gym_payload(gym, lang)
        item["match"] = kind
        item["match_score"] = score
        item["matched_alias"] = key
        payload.append(item)

    gyms_only = [row[3] for row in top]
    caveats = _gap_caveats(kb, gyms_only, lang)
    duplicates = _duplicate_districts(gyms_only, lang)
    ambiguous = len(top) > 1 and top[0][0] == top[1][0]
    if duplicates or ambiguous:
        caveats.insert(
            0,
            "Подходит больше одного зала: перечисли их вместе с ориентирами (landmark) "
            "и спроси, какой удобнее. Абстрактного «центрального зала» не предлагай.",
        )
    return ToolResult.success(
        data={
            "query": district_text,
            "gyms": payload,
            "total": len(payload),
            "ambiguous": ambiguous or bool(duplicates),
            "duplicate_districts": duplicates,
        },
        render_hint=RenderHint.VERBATIM,
        caveats=caveats,
        meta={"matched": True, "best_score": top[0][0]},
    )


def _known_district_without_gym(kb: KBSnapshot, queries: Sequence[str]) -> bool:
    """Назвал ли клиент район Костаная, в котором зала нет (``lexicon.districts_extra``)."""
    for district in kb.lexicon.districts_extra:
        key = normalize_text(district)
        if not key:
            continue
        for query in queries:
            if key == query or _contains_phrase(query, key) or is_fuzzy_match(query, key)[0]:
                return True
    return False


__all__ = [
    "MAX_DISTRICT_MATCHES",
    "MAX_GYMS_LIMIT",
    "find_gym_by_district",
    "fuzzy_threshold",
    "get_gyms",
    "is_fuzzy_match",
    "levenshtein",
    "normalize_text",
    "translit_to_cyrillic",
]
