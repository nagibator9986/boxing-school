"""Разбор данных формы. Пустое поле — это ``None``, а не пустая строка.

Разница принципиальная: в базе знаний ``null`` означает «данных нет, бот честно
скажет об этом и позовёт администратора», а пустая строка означала бы «данные
есть, и они пустые» — бот отправил бы клиенту пустоту. Поэтому весь ввод
проходит через эти функции, а не читается из формы напрямую.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "bilingual",
    "choice",
    "csv_list",
    "flag",
    "integer",
    "lines",
    "opt_float",
    "opt_integer",
    "opt_text",
    "text",
]


def text(form: Mapping[str, Any], key: str, *, default: str = "") -> str:
    """Строка без окружающих пробелов."""
    return str(form.get(key, default) or "").strip()


def opt_text(form: Mapping[str, Any], key: str) -> str | None:
    """Строка или ``None``, если поле пустое."""
    value = text(form, key)
    return value or None


def integer(form: Mapping[str, Any], key: str, *, default: int = 0) -> int:
    """Целое число; мусор превращается в ``default``, а не в исключение."""
    try:
        return int(str(form.get(key, "")).strip())
    except (TypeError, ValueError):
        return default


def opt_integer(form: Mapping[str, Any], key: str) -> int | None:
    """Целое или ``None`` для пустого поля."""
    raw = text(form, key)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def opt_float(form: Mapping[str, Any], key: str) -> float | None:
    """Дробное или ``None``. Запятая принимается наравне с точкой."""
    raw = text(form, key).replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def flag(form: Mapping[str, Any], key: str) -> bool:
    """Галочка. В HTML невыбранный чекбокс не отправляется вовсе."""
    return str(form.get(key, "")).strip().lower() in ("1", "on", "true", "yes", "да")


def lines(form: Mapping[str, Any], key: str) -> list[str]:
    """Многострочное поле в список: одна строка — один элемент."""
    raw = str(form.get(key, "") or "")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def csv_list(form: Mapping[str, Any], key: str) -> list[str]:
    """Поле «через запятую» в список."""
    raw = str(form.get(key, "") or "")
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def bilingual(form: Mapping[str, Any], prefix: str) -> dict[str, str | None]:
    """Пара полей ``<prefix>_ru`` и ``<prefix>_kk`` в двуязычное значение."""
    return {"ru": opt_text(form, f"{prefix}_ru"), "kk": opt_text(form, f"{prefix}_kk")}


def choice(form: Mapping[str, Any], key: str, allowed: Iterable[str], *, default: str) -> str:
    """Значение из списка допустимых.

    Форму можно подделать, а невалидное значение положить в базу знаний нельзя:
    валидатор его поймает, но пользователь получит непонятную ошибку схемы
    вместо тихого возврата к разумному значению.
    """
    value = text(form, key)
    return value if value in set(allowed) else default
