"""Номера, которым бот не отвечает.

Тренеры пишут в тот же WhatsApp, что и родители: спросить про зал, скинуть
расписание, договориться о замене. Отвечать им как клиентам бот не должен — это
шум в переписке и заявки, которых не было.

Сравнение идёт по последним десяти цифрам. Один и тот же номер приходит в
разном виде — ``+7 777 648 72 18``, ``8 777 648 7218``, ``77776487218`` — и
требовать от владельца единого написания значит гарантированно однажды не
совпасть. Десять цифр — это код оператора и номер, то есть всё, что различает
абонентов Казахстана и России; первая цифра (7 или 8) как раз и есть та, что
пишется по-разному.
"""

from __future__ import annotations

import re
from typing import Final, Iterable

__all__ = ["is_ignored", "parse", "tail"]

#: Сколько последних цифр сравнивается.
_TAIL: Final[int] = 10

_NON_DIGIT: Final[re.Pattern[str]] = re.compile(r"\D+")


def tail(value: str | None) -> str:
    """Последние десять цифр номера. Пустая строка — цифр слишком мало."""
    digits = _NON_DIGIT.sub("", value or "")
    return digits[-_TAIL:] if len(digits) >= _TAIL else ""


#: Больше цифр, чем бывает в одном номере: значит, в записи их несколько.
_MAX_DIGITS_IN_ONE: Final[int] = 11


def parse(raw: str | None) -> frozenset[str]:
    """Список номеров из настройки в набор для сравнения.

    Разделители — запятая, точка с запятой и перевод строки. Пробел таким быть
    не может: он живёт внутри самого номера («+7 777 000 00 01»), и разбор по
    пробелам оставлял от каждого номера обрывки, а список получался пустым.

    Но и пробел нельзя игнорировать совсем: номера, вписанные подряд через
    пробел, слиплись бы в одну строку, и её десять последних цифр совпали бы
    с несуществующим абонентом. Поэтому запись, где цифр больше, чем бывает в
    одном номере, дополнительно делится по пробелам.
    """
    numbers: set[str] = set()
    for chunk in re.split(r"[,;\n\r]+", (raw or "").strip()):
        pieces = [chunk]
        if len(_NON_DIGIT.sub("", chunk)) > _MAX_DIGITS_IN_ONE:
            pieces = chunk.split()
        numbers.update(item for item in (tail(piece) for piece in pieces) if item)
    return frozenset(numbers)


def is_ignored(ignored: Iterable[str] | None, *, chat_id: str | None, phone: str | None) -> bool:
    """Писал ли номер из списка исключений.

    Проверяются оба поля: в WhatsApp ``chat_id`` и есть номер, а в Instagram он
    приходит отдельно и может отсутствовать вовсе.
    """
    if not ignored:
        return False
    known = set(ignored)
    return any(candidate in known for candidate in (tail(chat_id), tail(phone)) if candidate)
