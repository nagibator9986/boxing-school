"""Трёхстороннее слияние файла базы знаний: правки из репозитория поверх правок из CRM.

11.11.2026 выпуск принёс Плазе и Тобылу отметку смены расписания, а ``gyms.yaml`` на томе за
час до того сохранили в CRM — и выпуск файл не тронул целиком: правило запуска было «файл,
который правил владелец, важнее файла из репозитория». Владелец 12.11.2026 попросил, чтобы
всё правилось в CRM, а значит, правленым в CRM скоро будет каждый файл — и ни одна правка
из репозитория больше не доехала бы до бота.

Слияние сравнивает три версии: **base** — что положил прошлый выпуск, **ours** — что сейчас на
томе (с правками CRM), **theirs** — новый выпуск. Правило на каждое значение одно:

* выпуск значение не менял — остаётся как на томе;
* на томе его не меняли — берётся из выпуска;
* поменяли оба по-разному — остаётся правка владельца, а путь значения уходит в конфликты.

Словари сливаются по ключам, списки записей с ``id`` (залы, вопросы, материалы) — по ``id``.
Остальные списки (дни занятий, районы) — одно значение целиком: склеивать их по элементам
значило бы получить расписание, которого не задавал никто.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["MergeReport", "merge3", "plain"]

_MISSING: Any = object()


@dataclass(slots=True)
class MergeReport:
    """Что из выпуска применено и где обе стороны поменяли одно и то же."""

    applied: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def plain(value: Any) -> Any:
    """Значение без оболочек YAML: словари без порядка ключей, списки, скаляры."""
    if value is _MISSING:
        return _MISSING
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


#: Отметки «кто и когда правил»: их ставят и CRM, и выпуск, и конфликтом это не считается —
#: остаётся отметка тома.
_STAMPS: frozenset[str] = frozenset({"updated_at", "updated_by"})


def merge3(base: Any, ours: Any, theirs: Any) -> MergeReport:
    """Переносит в ``ours`` (меняется на месте) правки ``theirs`` относительно ``base``."""
    report = MergeReport()
    if isinstance(base, Mapping) and isinstance(ours, Mapping) and isinstance(theirs, Mapping):
        _merge_mapping(base, ours, theirs, "", report)
    elif plain(ours) != plain(theirs) and plain(theirs) != plain(base):
        report.conflicts.append("<файл>")
    return report


def _merge_mapping(base: Mapping[Any, Any], ours: Any, theirs: Mapping[Any, Any], path: str, report: MergeReport) -> None:
    for key in dict.fromkeys([*theirs.keys(), *base.keys()]):
        if not path and key in _STAMPS:
            continue
        here = f"{path}.{key}" if path else str(key)
        b = base.get(key, _MISSING)
        o = ours.get(key, _MISSING) if key in ours else _MISSING
        t = theirs.get(key, _MISSING)
        pb, po, pt = plain(b), plain(o), plain(t)
        if pt == pb or po == pt:
            continue
        if po == pb:
            if t is _MISSING:
                del ours[key]
            else:
                ours[key] = t
            report.applied.append(here)
            continue
        if isinstance(b, Mapping) and isinstance(o, Mapping) and isinstance(t, Mapping):
            _merge_mapping(b, o, t, here, report)
        elif _keyed(b) and _keyed(o) and _keyed(t):
            _merge_keyed(b, o, t, here, report)
        else:
            report.conflicts.append(here)


def _keyed(value: Any) -> bool:
    """Список записей с уникальным ``id``: залы, вопросы, материалы.

    Пустой список с ними совместим: в прошлом выпуске записей могло не быть вовсе, и тогда
    новые записи выпуска и записи, заведённые в CRM, должны остаться и те и другие.
    """
    if not isinstance(value, list):
        return False
    if not value:
        return True
    ids = [item.get("id") if isinstance(item, Mapping) else None for item in value]
    return all(isinstance(item_id, str) for item_id in ids) and len(set(ids)) == len(ids)


def _merge_keyed(base: list[Any], ours: list[Any], theirs: list[Any], path: str, report: MergeReport) -> None:
    by_base = {item["id"]: item for item in base}
    by_theirs = {item["id"]: item for item in theirs}

    def position(item_id: str) -> int:
        return next((i for i, item in enumerate(ours) if item.get("id") == item_id), -1)

    previous: str | None = None
    for item_id, t in by_theirs.items():
        here = f"{path}[{item_id}]"
        at = position(item_id)
        b = by_base.get(item_id, _MISSING)
        if at < 0:
            if b is _MISSING:
                # Новая запись выпуска — встаёт за той, за которой стоит в выпуске.
                anchor = position(previous) if previous is not None else -1
                ours.insert(anchor + 1, t)
                report.applied.append(here)
            elif plain(t) != plain(b):
                # Владелец запись удалил, а выпуск её поменял: удаление остаётся.
                report.conflicts.append(here)
        else:
            o = ours[at]
            pb, po, pt = plain(b), plain(o), plain(t)
            if pt == pb or po == pt:
                pass
            elif po == pb:
                ours[at] = t
                report.applied.append(here)
            elif isinstance(b, Mapping) and isinstance(o, Mapping):
                _merge_mapping(b, o, t, here, report)
            else:
                report.conflicts.append(here)
        if position(item_id) >= 0:
            previous = item_id

    for item_id, b in by_base.items():
        if item_id in by_theirs:
            continue
        at = position(item_id)
        if at < 0:
            continue
        if plain(ours[at]) == plain(b):
            del ours[at]
            report.applied.append(f"{path}[{item_id}]")
        else:
            # Выпуск запись удалил, а владелец её правил: правка остаётся.
            report.conflicts.append(f"{path}[{item_id}]")
