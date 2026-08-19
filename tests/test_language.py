"""Определение языка диалога: русский или казахский.

Костанай — русскоязычный город с массовым код-свитчингом. «Сәлеметсіз бе,
сколько стоит?» — это норма, и отвечать на такую реплику по-казахски значит
показать родителю, что его не поняли.

Проверяются четыре класса ошибок:

* язык определён по приветствию, а не по смысловой части;
* транслит принят за иностранный язык (или наоборот);
* язык «залип»: клиент перешёл на казахский, бот продолжает по-русски;
* язык «скачет»: одно «рахмет» в русском диалоге переключило весь разговор.
"""

from __future__ import annotations

import pytest

from app.core.language import detect, is_foreign
from app.types import Language


@pytest.fixture
def lexicon(kb):
    """Разговорный словарь из ``kb/lexicon.yaml``."""
    return kb.lexicon


# --------------------------------------------------------------------------- #
# Чистые языки
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "Здравствуйте, сколько стоит абонемент?",
        "Добрый день! Хочу записать сына на бокс.",
        "Где вы находитесь и какое расписание?",
        "Можно записаться на пробное занятие?",
    ],
)
def test_pure_russian(text, lexicon) -> None:
    """Чистый русский определяется без сомнений."""
    decision = detect(text, lexicon=lexicon)

    assert decision.lang is Language.RU
    assert decision.needs_bridge is False


@pytest.mark.parametrize(
    "text",
    [
        "Сәлеметсіз бе! Қанша тұрады?",
        "Балама 7 жаста, қай залға баруға болады?",
        "Жазылу керек пе? Кесте қандай?",
        "Абонемент қанша тұрады, айтыңызшы.",
    ],
)
def test_pure_kazakh(text, lexicon) -> None:
    """Казахские графемы — сильнейший признак, сомнений быть не должно."""
    decision = detect(text, lexicon=lexicon)

    assert decision.lang is Language.KK
    assert decision.confidence >= 0.7


# --------------------------------------------------------------------------- #
# Смешанные реплики
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "Сәлеметсіз бе, сколько стоит?",
        "Салеметсиз бе, где вы находитесь?",
        "Ассалаумағалейкум, хочу записать ребёнка",
    ],
)
def test_kazakh_greeting_with_russian_question_answers_in_russian(text, lexicon) -> None:
    """Решает смысловая часть, а не вежливость: вопрос задан по-русски."""
    assert detect(text, lexicon=lexicon).lang is Language.RU


def test_russian_greeting_with_kazakh_question_answers_in_kazakh(lexicon) -> None:
    """Симметрично: приветствие по-русски, вопрос по-казахски."""
    assert detect("Здравствуйте, қанша тұрады?", lexicon=lexicon).lang is Language.KK


# --------------------------------------------------------------------------- #
# Транслит
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("skolko stoit abonement", Language.RU),
        ("zdravstvuyte, hochu zapisat rebenka", Language.RU),
        ("salemetsiz be, balam 7 jaste", Language.KK),
        ("kansha turady abonement", Language.KK),
    ],
)
def test_translit_is_recognised_but_answered_in_cyrillic(text, expected, lexicon) -> None:
    """Транслит — это не иностранный язык, а тот же ru/kk. Отвечаем кириллицей."""
    decision = detect(text, lexicon=lexicon)

    assert decision.lang is expected
    assert decision.source == "translit"
    assert is_foreign(text, lexicon=lexicon) is False


# --------------------------------------------------------------------------- #
# Короткие реплики, эмодзи, пустое
# --------------------------------------------------------------------------- #
def test_short_ambiguous_first_message_gets_kazakh_bridge(lexicon) -> None:
    """Коротко и неоднозначно — отвечаем по-русски и один раз даём мостик."""
    decision = detect("ок", lexicon=lexicon)

    assert decision.lang is Language.RU
    assert decision.needs_bridge is True


def test_short_kazakh_greeting_is_kazakh(lexicon) -> None:
    """«салем» — короткое, но однозначное: это казахский."""
    assert detect("салем", lexicon=lexicon).lang is Language.KK


@pytest.mark.parametrize("text", ["👍", "😀🥊", "", "   ", "1234", "...", "🥊"])
def test_no_letters_keeps_conversation_language(text, lexicon) -> None:
    """Стикер, эмодзи и пустое сообщение язык не меняют — берём язык диалога."""
    kept = detect(text, lexicon=lexicon, previous=Language.KK, locked=True)

    assert kept.lang is Language.KK
    assert kept.source == "previous"
    assert kept.needs_bridge is False
    # Без истории диалога по умолчанию русский, гадать не на чем.
    assert detect(text, lexicon=lexicon).lang is Language.RU


def test_emoji_does_not_unlock_language(lexicon) -> None:
    """Эмодзи не имеет права сбросить осознанный выбор языка клиентом."""
    decision = detect("👍", lexicon=lexicon, previous=Language.KK, locked=True)

    assert decision.locked is True


# --------------------------------------------------------------------------- #
# Залипание и переключение
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", ["рахмет", "жарайды", "ок", "да", "👍"])
def test_single_word_does_not_flip_russian_dialog(text, lexicon) -> None:
    """Одно вежливое слово язык диалога не меняет: липкость намеренная."""
    decision = detect(text, lexicon=lexicon, previous=Language.RU)

    assert decision.lang is Language.RU
    assert decision.locked is False  # такой сигнал не фиксирует язык


def test_explicit_switch_to_kazakh_locks_language(lexicon) -> None:
    """«Қанша тұрады?» в русском диалоге — осознанная смена языка."""
    decision = detect("Қанша тұрады?", lexicon=lexicon, previous=Language.RU)

    assert decision.lang is Language.KK
    assert decision.source == "switch"
    assert decision.locked is True


def test_explicit_switch_back_to_russian_from_locked_kazakh(lexicon) -> None:
    """Обратный переход тоже возможен, но требует более сильного сигнала."""
    decision = detect(
        "Сколько стоит абонемент для второго ребёнка?",
        lexicon=lexicon,
        previous=Language.KK,
        locked=True,
    )

    assert decision.lang is Language.RU
    assert decision.source == "switch"


def test_weak_signal_does_not_break_locked_language(lexicon) -> None:
    """В зафиксированном диалоге порог смены выше — короткое «да» его не берёт."""
    decision = detect("да", lexicon=lexicon, previous=Language.KK, locked=True)

    assert decision.lang is Language.KK
    assert decision.locked is True


def test_language_is_taken_from_last_message_not_first(lexicon) -> None:
    """Язык считается по последнему сообщению — так написано в правилах воронки."""
    first = detect("Здравствуйте, сколько стоит?", lexicon=lexicon)
    assert first.lang is Language.RU

    second = detect(
        "Балама 8 жаста, қай зал жақын?",
        lexicon=lexicon,
        previous=first.lang,
        locked=first.locked,
    )
    assert second.lang is Language.KK


# --------------------------------------------------------------------------- #
# Иностранный язык
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "Hello, how much is the training for my son?",
        "Hi there, do you have a group for my daughter?",
        "你好，多少钱一个月",
        "مرحبا كم السعر",
    ],
)
def test_foreign_language_is_detected(text, lexicon) -> None:
    """Язык вне {ru, kk} уходит человеку: бот на нём не отвечает."""
    assert is_foreign(text, lexicon=lexicon) is True


@pytest.mark.parametrize(
    "text",
    ["ok", "salem", "ok!", "+", "77012345678", "", "Сәлеметсіз бе", "skolko stoit"],
)
def test_short_latin_is_not_foreign(text, lexicon) -> None:
    """Ложное «иностранный» уводит нормального клиента к человеку без нужды."""
    assert is_foreign(text, lexicon=lexicon) is False
