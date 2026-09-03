"""Разговорный костанайский: нормализация текста и подсказки об интенте.

Модуль работает **до** модели и **до** guards. Его задача — превратить живую
речь родителя («скок стоит абик на сына, 6 жаста, кжби») в набор машинных
подсказок: интенты, возраст, пол, телефон, район. Модель эти подсказки получает
служебной заметкой и потому реже промахивается инструментом.

Правила модуля:

* источник словаря — только ``kb/lexicon.yaml`` (:class:`app.kb.models.LexiconFile`);
  в коде нет ни одного факта о школе, только грамматика и морфология;
* нормализация **не меняет смысл**: регистр, ``ё → е``, эмодзи, повторы пробелов;
  казахские специфические буквы (``ә ғ қ ң ө ұ ү һ і``) не трогаются никогда —
  по ним работает определитель языка;
* совпадения ищутся по границам слов, иначе короткий алиас ``кск`` попадёт
  внутрь слова ``кске`` и подскажет несуществующий район.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Final, Iterable, Sequence

from app.kb.models import Gym, LexiconFile
from app.types import normalize_phone_kz
from app.types import Gender, IntentHint

__all__ = [
    "MAX_PLAUSIBLE_AGE",
    "MIN_PLAUSIBLE_AGE",
    "extract_age",
    "extract_gender",
    "extract_phone",
    "intent_hints",
    "match_district",
    "normalize",
    "tokens",
]

#: Возраст вне этих границ — не возраст, а цена, год или номер дома.
MIN_PLAUSIBLE_AGE: Final[int] = 2
MAX_PLAUSIBLE_AGE: Final[int] = 99

#: Детский диапазон: при нескольких кандидатах выигрывает он.
_CHILD_AGE_RANGE: Final[tuple[int, int]] = (3, 17)

#: Эмодзи и пиктограммы: удаляются из нормализованного текста, но не из исходного.
_EMOJI_RE: Final[re.Pattern[str]] = re.compile(
    "[\U0001f000-\U0001faff"
    "☀-➿"
    "←-⇿"
    "⬀-⯿"
    "〰〽㊗㊙"
    "©®™"
    "︎️‍"
    "\U0001f3fb-\U0001f3ff]"
)

_ZERO_WIDTH_RE: Final[re.Pattern[str]] = re.compile("[​‌⁠﻿]")
_SPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[^\W\d_]+", re.UNICODE)

#: Кандидат на телефон: 10 значащих цифр в бытовом написании.
_PHONE_CANDIDATE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)(?:\+?7|8)?[\s\-()._]*\d{3}[\s\-()._]*\d{3}[\s\-()._]*\d{2}[\s\-()._]*\d{2}(?!\d)"
)

#: Подстраховка на случай пустого словаря: базовые формы возраста.
_DEFAULT_AGE_PATTERNS: Final[tuple[str, ...]] = (
    r"(\d{1,2})\s*(?:лет|года|годика|годик|год|л\.|жаста|жас)",
    r"(?:сыну|дочке|дочери|ребенку|ребёнку|ұлым|қызым|балама|баламыз)\s*(\d{1,2})",
    r"(\d{4})\s*(?:года рождения|г\.?\s?р\.?|жылы)",
)

#: Подстраховка на случай пустого словаря: маркеры пола.
_DEFAULT_GENDER_MARKERS: Final[dict[Gender, tuple[str, ...]]] = {
    Gender.M: ("сын", "сына", "сыну", "мальчик", "мальчика", "пацан", "ұл", "ұлым"),
    Gender.F: ("дочь", "дочка", "дочке", "дочери", "девочка", "девочку", "қыз", "қызым"),
}


# --------------------------------------------------------------------------- #
# Нормализация
# --------------------------------------------------------------------------- #
def normalize(text: str) -> str:
    """Нижний регистр, схлопывание пробелов, ``ё → е``, снятие эмодзи. Смысл не меняет.

    Пунктуация остаётся: поиск по словарю идёт по границам слов, а не по «голому»
    набору букв, и точка после ``цена.`` мешать не должна.
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFC", text)
    out = _ZERO_WIDTH_RE.sub("", out)
    out = _EMOJI_RE.sub(" ", out)
    out = out.replace("Ё", "Е").replace("ё", "е")
    out = out.lower()
    return _SPACE_RE.sub(" ", out).strip()


def tokens(text: str) -> tuple[str, ...]:
    """Слова нормализованного текста без цифр и пунктуации."""
    return tuple(_TOKEN_RE.findall(normalize(text)))


def _boundary_re(phrase: str) -> re.Pattern[str]:
    """Регулярка «фраза как отдельное слово» с учётом кириллицы и латиницы."""
    return re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)")


def _find_phrase(haystack: str, phrase: str) -> int:
    """Позиция фразы по границам слов; ``-1`` — не найдена."""
    phrase = phrase.strip().lower()
    if not phrase:
        return -1
    match = _boundary_re(phrase).search(haystack)
    return match.start() if match else -1


def _damerau_close(left: str, right: str) -> bool:
    """Отличаются ли слова не более чем одной опечаткой (вставка/замена/перестановка).

    Дешёвая проверка без внешних зависимостей: применяется только к длинным
    словам, где случайное совпадение маловероятно.
    """
    if abs(len(left) - len(right)) > 1:
        return False
    if left == right:
        return True
    if len(left) == len(right):
        diff = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
        if len(diff) == 1:
            return True
        if len(diff) == 2 and diff[1] == diff[0] + 1:
            i, j = diff
            return left[i] == right[j] and left[j] == right[i]
        return False
    short, long = (left, right) if len(left) < len(right) else (right, left)
    for cut in range(len(long)):
        if long[:cut] + long[cut + 1 :] == short:
            return True
    return False


# --------------------------------------------------------------------------- #
# Интенты
# --------------------------------------------------------------------------- #
def intent_hints(text: str, *, lexicon: LexiconFile) -> tuple[IntentHint, ...]:
    """Подсказки об интенте по словарю разговорных форм.

    Возвращает интенты в порядке первого вхождения в текст: сообщение
    «здравствуйте, скок стоит и где вы» даёт ``(PRICE, LOCATION)``.
    Ничего не найдено — пустой кортеж (``OTHER`` не выдумывается).

    Опечатки: если прямого совпадения нет, однословные алиасы длиной от пяти
    букв сверяются с токенами текста с допуском в одну ошибку — «абонимнт»
    и «расписаниие» узнаются без отдельной записи в словаре.
    """
    haystack = normalize(text)
    if not haystack:
        return ()

    found: dict[IntentHint, int] = {}
    for intent, phrases in (lexicon.intents or {}).items():
        for phrase in phrases:
            position = _find_phrase(haystack, phrase)
            if position >= 0 and position < found.get(intent, 10**6):
                found[intent] = position

    if not found:
        word_list = tokens(haystack)
        for intent, phrases in (lexicon.intents or {}).items():
            if intent in found:
                continue
            for phrase in phrases:
                if " " in phrase or len(phrase) < 5:
                    continue
                for index, word in enumerate(word_list):
                    if len(word) >= 5 and _damerau_close(word, phrase.lower()):
                        found.setdefault(intent, index)
                        break
                if intent in found:
                    break

    ordered = sorted(found.items(), key=lambda item: (item[1], item[0].value))
    return tuple(intent for intent, _ in ordered if intent is not IntentHint.OTHER)


# --------------------------------------------------------------------------- #
# Возраст, пол, телефон
# --------------------------------------------------------------------------- #
def extract_age(text: str, *, lexicon: LexiconFile, now: datetime) -> int | None:
    """Понимает «8 лет», «сыну 8», «2018 г.р.» (год рождения → возраст на дату ``now``).

    Числа без возрастного контекста игнорируются: «25000 тг» и «дом 12» возрастом
    не считаются. Из нескольких кандидатов выигрывает попавший в детский диапазон
    3–17 лет, потому что запись ведут ребёнку, а не взрослому.
    """
    haystack = normalize(text)
    if not haystack:
        return None

    patterns = tuple(lexicon.age_patterns or ()) + _DEFAULT_AGE_PATTERNS
    candidates: list[int] = []
    for pattern in patterns:
        try:
            compiled = re.compile(pattern)
        except re.error:  # pragma: no cover - словарь валидируется загрузчиком
            continue
        for match in compiled.finditer(haystack):
            for group in match.groups():
                age = _age_from_group(group, now=now)
                if age is not None:
                    candidates.append(age)

    if not candidates:
        return None
    low, high = _CHILD_AGE_RANGE
    for age in candidates:
        if low <= age <= high:
            return age
    return candidates[0]


def _age_from_group(group: str | None, *, now: datetime) -> int | None:
    """Число из группы регулярки в возраст. Четырёхзначное — это год рождения."""
    if not group or not group.isdigit():
        return None
    value = int(group)
    if len(group) == 4:
        if not 1900 <= value <= now.year:
            return None
        value = now.year - value
    if MIN_PLAUSIBLE_AGE <= value <= MAX_PLAUSIBLE_AGE:
        return value
    return None


def extract_gender(text: str, *, lexicon: LexiconFile) -> Gender:
    """Пол ребёнка по словам «сын / дочь / ұлым / қызым».

    Если в сообщении есть маркеры обоих полов («сын и дочка»), возвращается
    :attr:`Gender.UNKNOWN` — угадывать в такой ситуации хуже, чем не знать.
    """
    haystack = normalize(text)
    if not haystack:
        return Gender.UNKNOWN

    markers: dict[Gender, Iterable[str]] = {
        Gender.M: tuple((lexicon.gender_markers or {}).get(Gender.M, ()))
        or _DEFAULT_GENDER_MARKERS[Gender.M],
        Gender.F: tuple((lexicon.gender_markers or {}).get(Gender.F, ()))
        or _DEFAULT_GENDER_MARKERS[Gender.F],
    }
    hits: dict[Gender, int] = {}
    for gender, phrases in markers.items():
        for phrase in phrases:
            position = _find_phrase(haystack, phrase)
            if position >= 0 and position < hits.get(gender, 10**6):
                hits[gender] = position
    if len(hits) != 1:
        return Gender.UNKNOWN
    return next(iter(hits))


def extract_phone(text: str) -> str | None:
    """Обёртка над :func:`app.types.normalize_phone_kz` — одна регулярка на весь проект.

    Из текста вырезаются кандидаты бытового вида (``8 705 123 45 67``,
    ``+7 (705) 123-45-67``), приведение к E.164 и отбраковка мусора — в ``booking``.
    """
    if not text:
        return None
    for match in _PHONE_CANDIDATE_RE.finditer(text):
        phone = normalize_phone_kz(match.group(0))
        if phone:
            return phone
    return None


#: Слова, которые выглядят как имя, но именем не являются: вежливость, город,
#: названия залов и ориентиров. Список короткий сознательно — он нужен ровно для
#: коротких ответов вида «Асель, 87015551122», а не для разбора длинных фраз.
_NOT_A_NAME: Final[frozenset[str]] = frozenset(
    """здравствуйте здрасьте привет добрый доброе спасибо хорошо конечно ладно
    костанай тобыл рудный житикара аркалык денисовка карабалык боровской
    рахат романтик жана кала кск центр микрорайон школа зал бокс кикбоксинг
    каирбекова касымханова арыстанбекова воинов интернационалистов полевая
    сәлеметсіз сәлем рақмет жақсы
    ему ей его её ему им них нему мне нам вам они она оно мой моя наша наши
    это эта этот тут там вот уже ещё еще если когда куда где как что сколько
    очень можно нужно надо хочу хотим будем будет были дочь дочка сын сына
    ребёнок ребенок мальчик девочка год года лет зовут звать имя""".split()
)

#: Имя в короткой реплике: «Асель», «Асель, 87015551122», «Данияр 8 лет».
_NAME_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"\b([А-ЯЁӘҒҚҢӨҰҮҺІ][а-яёәғқңөұүһі]{2,14})\b"
)

#: Длиннее этого реплика перестаёт быть ответом «как зовут» и становится рассказом.
_NAME_MAX_WORDS: Final[int] = 6

#: Длина основы для сверки со стоп-списком: хватает, чтобы падежи сошлись.
_NAME_STEM: Final[int] = 5


def extract_name(text: str) -> str | None:
    """Имя, названное в короткой реплике. ``None`` — имени не видно.

    Живой прогон 20 диалогов: на вопрос «как зовут ребёнка» клиент ответил
    «Асель, 87015551122», и бот спросил снова — «как зовут сына?». Телефон и
    возраст разбирались, а имя нет, поэтому служебная заметка «клиент только что
    назвал» про имя молчала.

    Разбор нарочно узкий: короткая реплика, слово с заглавной буквы, не из списка
    городов, залов и вежливых слов. Ошибиться здесь дёшево — заметка лишь
    запрещает переспрашивать, — но и ошибаться незачем.
    """
    body = (text or "").strip()
    if not body or len(body.split()) > _NAME_MAX_WORDS:
        return None
    stops = {word[:_NAME_STEM] for word in _NOT_A_NAME}
    for match in _NAME_TOKEN_RE.finditer(body):
        word = match.group(1)
        # Сравнение по основе: «Тобыле» и «Костанае» — это те же город и посёлок,
        # только в падеже, и именами от этого не становятся.
        if word.lower()[:_NAME_STEM] in stops:
            continue
        return word
    return None


# --------------------------------------------------------------------------- #
# Районы
# --------------------------------------------------------------------------- #
def match_district(
    text: str, *, lexicon: LexiconFile, gyms: Sequence[Gym]
) -> tuple[str, ...]:
    """Возвращает id залов-кандидатов по алиасам района; используется ``find_gym_by_district``.

    Сопоставляются: ``district_aliases`` зала, название района и населённый пункт
    из KB. Районы из ``lexicon.districts_extra`` — это места, где зала нет: они
    узнаются, но кандидатов не дают, и это правильно — бот должен понять вопрос
    и честно предложить ближайший зал.

    Порядок результата — от самого длинного (самого специфичного) совпадения
    к короткому, поэтому «6 микрорайон» выигрывает у «6 мкр».
    """
    haystack = normalize(text)
    if not haystack:
        return ()

    scored: dict[str, int] = {}
    for gym in gyms:
        if not getattr(gym, "active", True):
            continue
        for alias in _gym_aliases(gym):
            if _find_phrase(haystack, alias) >= 0:
                scored[gym.id] = max(scored.get(gym.id, 0), len(alias))
    ordered = sorted(scored.items(), key=lambda item: (-item[1], item[0]))
    return tuple(gym_id for gym_id, _ in ordered)


def _gym_aliases(gym: Gym) -> tuple[str, ...]:
    """Все разговорные имена зала: алиасы, район, населённый пункт, ориентир."""
    values: list[str] = list(gym.district_aliases or ())
    for candidate in (
        gym.district.ru,
        gym.district.kk,
        gym.settlement,
        gym.landmark.ru,
        gym.landmark.kk,
    ):
        if candidate:
            values.append(candidate)
    cleaned = {normalize(value) for value in values}
    return tuple(sorted((value for value in cleaned if len(value) >= 3), key=len, reverse=True))


def known_district(text: str, *, lexicon: LexiconFile, gyms: Sequence[Gym]) -> bool:
    """Узнан ли район вообще — включая районы без зала (``districts_extra``).

    Нужен пост-фильтру: упоминание района, которого нет ни в KB, ни в словаре,
    подозрительно и разбирается отдельно.
    """
    haystack = normalize(text)
    if not haystack:
        return False
    if match_district(text, lexicon=lexicon, gyms=gyms):
        return True
    return any(
        _find_phrase(haystack, normalize(alias)) >= 0
        for alias in (lexicon.districts_extra or ())
    )
