"""Определение языка диалога: русский или казахский, без тяжёлых зависимостей.

Костанай — русскоязычный город с растущей долей казахскоязычных семей и
**массовым код-свитчингом**: «Сәлеметсіз бе, сколько стоит?» — это норма, а не
исключение. Отсюда правила (research-funnel §5.2), которые здесь и реализованы:

1. язык считается по **последнему** сообщению клиента, а не по первому;
2. в смешанной реплике решает **смысловая часть**, а не приветствие;
3. меньше трёх слов и неоднозначно — отвечаем по-русски и один раз добавляем
   казахский мостик (``bridge.kk_offer``);
4. транслит казахского («salemetsiz be», «balam 7 jaste») — это ``kk``,
   но отвечаем кириллицей; транслит русского («skolko stoit») — ``ru``;
5. клиент переключил язык — переключаемся вместе с ним и обратно не возвращаемся
   (``locked=True``);
6. пустое сообщение, стикер, одни эмодзи — язык берётся из диалога, а не гадается.

Словарь признаков живёт в ``kb/lexicon.yaml``; в коде — только морфология
(казахские окончания) и служебные слова, которых в базе знаний быть не должно.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.core.lexicon import normalize, tokens
from app.kb.models import LexiconFile
from app.types import Language

__all__ = ["LanguageDecision", "detect", "is_foreign"]

#: Казахские специфические буквы. Одна такая буква — сильный признак.
_KK_GRAPHEMES: Final[frozenset[str]] = frozenset("әғқңөұүһі")

#: Буквы, которых в казахских словах практически не бывает (только в заимствованиях).
_RU_GRAPHEMES: Final[frozenset[str]] = frozenset("щъэё")

#: Казахские аффиксы. Слабый признак: вес меньше, чем у графем и слов.
_KK_SUFFIXES: Final[tuple[str, ...]] = (
    "ңыз", "ныз", "ңіз", "низ", "мын", "мін", "сыз", "сіз", "дар", "дер", "тар", "тер",
    "лар", "лер", "дың", "дің", "тың", "тің", "ның", "нің", "ған", "ген", "қан", "кен",
    "йды", "еді", "жат", "мыз", "міз",
)

#: Русские служебные и частотные слова. В KB им не место — это грамматика, не факты.
_RU_WORDS: Final[frozenset[str]] = frozenset(
    {
        "здравствуйте", "добрый", "доброе", "спасибо", "пожалуйста", "сколько", "стоит",
        "стоимость", "цена", "где", "когда", "как", "можно", "хочу", "хотим", "нужно",
        "надо", "есть", "будет", "ребенок", "ребенка", "ребенку", "сын", "сына", "сыну",
        "дочь", "дочка", "дочке", "лет", "года", "год", "занятия", "занятие", "тренировки",
        "адрес", "запись", "записать", "записаться", "телефон", "меня", "вас", "вы", "мы",
        "и", "или", "не", "да", "нет", "что", "это", "для", "на", "по", "за", "до", "от",
        "мальчик", "девочка", "школа", "район", "какой", "какие", "оплата", "тренер",
    }
)

#: Казахские слова без специфических букв — подстраховка на случай пустого словаря.
_KK_WORDS_FALLBACK: Final[frozenset[str]] = frozenset(
    {
        "канша", "бага", "сабак", "бала", "балам", "кесте", "жас", "жаста", "керек",
        "рахмет", "салеметсиз", "жок", "иа", "бар", "калай", "кайда", "жазыныз", "тегин",
    }
)

#: Приветствия. Их язык не определяет язык всей реплики (правило 2).
_KK_GREETINGS: Final[frozenset[str]] = frozenset(
    {"сәлеметсіз", "салеметсиз", "сәлем", "салем", "саламатсызба", "сәлеметсізбе",
     "ассалаумағалейкум", "уағалейкум", "қайырлы", "кайырлы"}
)
_RU_GREETINGS: Final[frozenset[str]] = frozenset(
    {"здравствуйте", "здравствуй", "здрасте", "привет", "приветствую", "добрый",
     "доброе", "доброго", "здравствуйте"}
)
#: Нейтральные приветствия и их «хвосты»: языка не выдают.
_NEUTRAL_GREETINGS: Final[frozenset[str]] = frozenset(
    {"ассалам", "ассаламу", "алейкум", "салам", "бе", "күн", "кун", "таң", "тан", "кеш",
     "день", "вечер", "утро", "ночи", "здравия"}
)

#: Казахская латиница: буквосочетания, которых в русской транслитерации не бывает.
_KK_TRANSLIT_MARKERS: Final[tuple[str, ...]] = (
    "siz", "syz", "ynyz", "iniz", "jas", "zhas", "kansha", "qansha", "kalai", "kalay",
    "balam", "bala", "rahmet", "rakhmet", "kerek", "salem", "salemetsiz", "bar ma", "zhok",
)
#: Русская латиница.
_RU_TRANSLIT_MARKERS: Final[tuple[str, ...]] = (
    "skolko", "stoit", "zdravstvuyte", "privet", "adres", "rebenok", "rebyonok",
    "trenirovka", "zapish", "raspisanie", "spasibo", "pozhaluysta", "hochu", "kogda",
)

#: Английские маркеры — по ним диалог уходит человеку (foreign_language).
_EN_MARKERS: Final[tuple[str, ...]] = (
    "hello", "hi there", "how much", "how many", "do you", "i want", "my son",
    "my daughter", "price", "training", "please tell", "good morning", "where is",
    "can i", "is it", "thanks", "thank you",
)

#: Письменности, которых в Костанае в переписке со школой быть не должно.
_FOREIGN_SCRIPT_RE: Final[re.Pattern[str]] = re.compile(
    "[؀-ۿ"      # арабица
    "֐-׿"       # иврит
    "Ͱ-Ͽ"       # греческий
    "԰-֏"       # армянский
    "Ⴀ-ჿ"       # грузинский
    "ऀ-ॿ"       # деванагари
    "฀-๿"       # тайский
    "一-鿿"       # китайский
    "぀-ヿ"       # японские каны
    "가-힯]"      # хангыль
)

_LATIN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z]")
_CYRILLIC_RE: Final[re.Pattern[str]] = re.compile(r"[а-яёәғқңөұүһі]")

#: Порог уверенности, ниже которого решение считается неоднозначным.
_MIN_SCORE: Final[float] = 1.0
#: Насколько сильным должен быть сигнал, чтобы сменить язык диалога.
_SWITCH_SCORE: Final[float] = 1.5
#: То же для диалога, где язык уже зафиксирован клиентом.
_SWITCH_SCORE_LOCKED: Final[float] = 2.0
#: Реплика короче — «очень короткая», к ней применяется правило мостика.
_SHORT_WORDS: Final[int] = 3


class LanguageDecision(BaseModel):
    """Решение о языке ответа на один ход."""

    model_config = ConfigDict(frozen=True)

    lang: Language
    locked: bool = False
    needs_bridge: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source: Literal["graphemes", "words", "translit", "previous", "default", "switch"] = "default"


@dataclass(frozen=True, slots=True)
class _Evidence:
    """Взвешенные признаки двух языков в куске текста."""

    kk: float
    ru: float
    source: Literal["graphemes", "words", "translit", "default"]

    @property
    def strongest(self) -> float:
        return max(self.kk, self.ru)

    @property
    def gap(self) -> float:
        return abs(self.kk - self.ru)


# --------------------------------------------------------------------------- #
# Публичное
# --------------------------------------------------------------------------- #
def detect(
    text: str,
    *,
    lexicon: LexiconFile,
    previous: Language | None = None,
    locked: bool = False,
) -> LanguageDecision:
    """Определяет язык ответа на очередное сообщение клиента.

    ``previous`` — язык диалога до этого сообщения, ``locked`` — признак, что
    клиент уже переключал язык осознанно. Липкость намеренная: одно «рахмет»
    в русском диалоге язык не меняет, а «Қанша тұрады?» — меняет.
    """
    normalized = normalize(text or "")
    has_letters = bool(_CYRILLIC_RE.search(normalized) or _LATIN_RE.search(normalized))
    if not normalized or not has_letters:
        # Пусто, стикер, одни эмодзи или только цифры — язык берём из диалога.
        return LanguageDecision(
            lang=previous or Language.RU,
            locked=locked,
            needs_bridge=False,
            confidence=0.2 if previous else 0.1,
            source="previous" if previous else "default",
        )

    core = _strip_greeting(normalized)
    evidence = _evidence(core, lexicon) if core else _Evidence(0.0, 0.0, "default")
    if evidence.strongest < _MIN_SCORE:
        # Смысловая часть молчит — смотрим на всю реплику, включая приветствие.
        evidence = _evidence(normalized, lexicon)

    word_count = len(tokens(core or normalized))
    detected, confidence = _decide(evidence)

    if detected is None:
        if previous is not None:
            return LanguageDecision(
                lang=previous,
                locked=locked,
                needs_bridge=False,
                confidence=0.4,
                source="previous",
            )
        # Правило 3: коротко и неоднозначно — русский плюс казахский мостик.
        return LanguageDecision(
            lang=Language.RU,
            locked=False,
            needs_bridge=True,
            confidence=0.3,
            source="default",
        )

    if previous is not None and detected is not previous:
        threshold = _SWITCH_SCORE_LOCKED if locked else _SWITCH_SCORE
        if evidence.strongest < threshold:
            # Сигнала на смену языка не хватило: диалог остаётся на своём языке.
            return LanguageDecision(
                lang=previous,
                locked=locked,
                needs_bridge=False,
                confidence=0.45,
                source="previous",
            )
        return LanguageDecision(
            lang=detected,
            locked=True,
            needs_bridge=False,
            confidence=confidence,
            source="switch",
        )

    needs_bridge = (
        previous is None
        and detected is Language.RU
        and word_count < _SHORT_WORDS
        and evidence.gap < _SWITCH_SCORE
    )
    return LanguageDecision(
        lang=detected,
        locked=locked,
        needs_bridge=needs_bridge,
        confidence=confidence,
        source=evidence.source if evidence.source != "default" else "default",
    )


def is_foreign(text: str, *, lexicon: LexiconFile) -> bool:
    """Язык вне ``{ru, kk}`` → эскалация с ``reason=foreign_language``.

    Проверка нарочно консервативна: ложное «иностранный» уводит нормального
    клиента к человеку без нужды. Срабатывает на чужой письменности и на
    развёрнутом английском; короткая латиница («ok», «salem») иностранной
    не считается — это транслит.
    """
    normalized = normalize(text or "")
    if not normalized:
        return False
    if _FOREIGN_SCRIPT_RE.search(normalized):
        return True
    if _CYRILLIC_RE.search(normalized):
        return False

    latin = _LATIN_RE.findall(normalized)
    if len(latin) < 6:
        return False
    if _count_markers(normalized, tuple(lexicon.kk_translit or ()) + _KK_TRANSLIT_MARKERS):
        return False
    if _count_markers(normalized, tuple(lexicon.ru_translit or ()) + _RU_TRANSLIT_MARKERS):
        return False
    return _count_markers(normalized, _EN_MARKERS) >= 2


# --------------------------------------------------------------------------- #
# Внутреннее
# --------------------------------------------------------------------------- #
def _strip_greeting(normalized: str) -> str:
    """Отрезает приветствие в начале реплики и возвращает смысловую часть.

    «Сәлеметсіз бе, сколько стоит?» → «сколько стоит»: приветствие на казахском
    вежливость, а вопрос — на русском, отвечать нужно по вопросу.
    """
    words = tokens(normalized)
    index = 0
    greetings = _KK_GREETINGS | _RU_GREETINGS | _NEUTRAL_GREETINGS
    while index < len(words) and words[index] in greetings:
        index += 1
    if index == 0 or index >= len(words):
        return "" if index and index >= len(words) else normalized
    return " ".join(words[index:])


def _evidence(piece: str, lexicon: LexiconFile) -> _Evidence:
    """Считает взвешенные признаки казахского и русского в куске текста."""
    if not piece:
        return _Evidence(0.0, 0.0, "default")

    words = tokens(piece)
    latin_letters = len(_LATIN_RE.findall(piece))
    cyrillic_letters = len(_CYRILLIC_RE.findall(piece))

    if latin_letters > cyrillic_letters:
        kk = 1.6 * _count_markers(
            piece, tuple(lexicon.kk_translit or ()) + _KK_TRANSLIT_MARKERS
        )
        ru = 1.6 * _count_markers(
            piece, tuple(lexicon.ru_translit or ()) + _RU_TRANSLIT_MARKERS
        )
        return _Evidence(kk, ru, "translit")

    graphemes = set(lexicon.kk_graphemes or ()) or _KK_GRAPHEMES
    kk_grapheme_hits = sum(1 for char in piece if char in graphemes)
    ru_grapheme_hits = sum(1 for char in piece if char in _RU_GRAPHEMES)

    kk_vocabulary = {w.lower() for w in (lexicon.kk_words or ())} | _KK_WORDS_FALLBACK | _KK_GREETINGS
    kk_word_hits = sum(1 for word in words if word in kk_vocabulary)
    ru_word_hits = sum(1 for word in words if word in _RU_WORDS or word in _RU_GREETINGS)
    kk_suffix_hits = sum(
        1 for word in words if len(word) >= 5 and word.endswith(_KK_SUFFIXES)
    )

    kk = 1.5 * min(kk_grapheme_hits, 3) + 1.0 * kk_word_hits + 0.5 * kk_suffix_hits
    ru = 1.0 * ru_word_hits + 0.5 * min(ru_grapheme_hits, 2)

    if kk_grapheme_hits and kk >= ru:
        return _Evidence(kk, ru, "graphemes")
    return _Evidence(kk, ru, "words")


def _decide(evidence: _Evidence) -> tuple[Language | None, float]:
    """Язык и уверенность по признакам; ``None`` — неоднозначно."""
    if evidence.strongest < _MIN_SCORE or evidence.kk == evidence.ru:
        return None, 0.0
    lang = Language.KK if evidence.kk > evidence.ru else Language.RU
    confidence = min(0.99, 0.55 + 0.12 * evidence.gap)
    return lang, round(confidence, 2)


def _count_markers(piece: str, markers: Sequence[str]) -> int:
    """Сколько маркеров из списка встречается в тексте."""
    return sum(1 for marker in markers if marker and marker.lower() in piece)
