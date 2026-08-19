"""Запись расписания в ``kb/gyms.yaml`` из админки бота.

Правило одно: **битая правка не имеет права уронить работающего бота.**
Поэтому запись идёт по шагам — резервная копия, атомарная подмена файла,
полная валидация базы знаний, и при любой ошибке откат к копии. Пока новая база
не прошла проверку, бот продолжает отвечать по старой.

Расписание меняется прямо в YAML, а не в отдельной таблице: база знаний остаётся
единственным источником правды, и правка из чата ничем не отличается от правки
руками. Так же работает валидатор, git-история и откат.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.admin.kb_lock import kb_write_lock
from app.kb import loader as kb_loader
from app.logging_conf import get_logger

__all__ = ["ScheduleWriteError", "WriteResult", "apply_schedule", "clear_schedule", "read_schedule"]

_log = get_logger(__name__)


class ScheduleWriteError(RuntimeError):
    """Правку применить не удалось. Файл при этом остался прежним."""


@dataclass(slots=True)
class WriteResult:
    """Итог применённой правки."""

    gym_id: str
    slots_before: int
    slots_after: int
    kb_hash: str
    backup: Path


def _gyms_path(kb_dir: Path) -> Path:
    return kb_dir / "gyms.yaml"


def _load_raw(kb_dir: Path) -> dict[str, Any]:
    """Читает gyms.yaml как обычный YAML — без валидации, для точечной правки."""
    path = _gyms_path(kb_dir)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ScheduleWriteError(f"не читается {path.name}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("gyms"), list):
        raise ScheduleWriteError(f"{path.name}: ожидался список gyms")
    return data


def read_schedule(kb_dir: Path, gym_id: str) -> list[dict[str, Any]]:
    """Текущее расписание зала как список словарей."""
    for gym in _load_raw(kb_dir)["gyms"]:
        if isinstance(gym, dict) and gym.get("id") == gym_id:
            return list(gym.get("schedule") or [])
    raise ScheduleWriteError(f"в gyms.yaml нет зала '{gym_id}'")


def _write_and_validate(
    kb_dir: Path, data: dict[str, Any], *, media_dir: Path, schema_version: int
) -> tuple[str, Path]:
    """Атомарная запись с проверкой и откатом. Возвращает ``kb_hash`` и путь копии."""
    path = _gyms_path(kb_dir)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(f".yaml.bak-{stamp}")
    shutil.copy2(path, backup)

    tmp = path.with_suffix(".yaml.tmp")
    try:
        tmp.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100),
            encoding="utf-8",
        )
        tmp.replace(path)  # атомарная подмена в пределах одной файловой системы
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise ScheduleWriteError(f"не удалось записать {path.name}: {exc}") from exc

    try:
        snapshot, problems = kb_loader.load_sync(
            kb_dir, media_dir=media_dir, schema_version=schema_version
        )
    except Exception as exc:  # noqa: BLE001 — любая ошибка валидации означает откат
        shutil.copy2(backup, path)
        raise ScheduleWriteError(
            f"правка отклонена, файл возвращён к прежнему виду: {exc}"
        ) from exc

    if problems:
        shutil.copy2(backup, path)
        raise ScheduleWriteError(
            "правка отклонена, файл возвращён к прежнему виду: " + "; ".join(problems[:3])
        )

    kb_loader.swap(snapshot)
    _log.info("schedule_updated", kb_hash=snapshot.kb_hash)
    return snapshot.kb_hash, backup


def apply_schedule(
    kb_dir: Path,
    gym_id: str,
    slots: list[dict[str, Any]],
    *,
    media_dir: Path,
    schema_version: int,
) -> WriteResult:
    """Заменяет расписание зала целиком.

    Замена, а не добавление: администратор присылает расписание зала как единое
    сообщение, и «дописать к прежнему» дало бы дубли, которые он не увидит.

    Чтение, правка и запись идут под общей блокировкой: то же самое расписание
    в этот момент может править CRM из другого процесса, и без блокировки одна
    правка затирает другую (см. :mod:`app.admin.kb_lock`).
    """
    with kb_write_lock(kb_dir):
        return _apply_schedule_locked(
            kb_dir, gym_id, slots, media_dir=media_dir, schema_version=schema_version
        )


def _apply_schedule_locked(
    kb_dir: Path,
    gym_id: str,
    slots: list[dict[str, Any]],
    *,
    media_dir: Path,
    schema_version: int,
) -> WriteResult:
    """Тело замены расписания. Вызывается только под блокировкой."""
    data = _load_raw(kb_dir)
    target: dict[str, Any] | None = None
    for gym in data["gyms"]:
        if isinstance(gym, dict) and gym.get("id") == gym_id:
            target = gym
            break
    if target is None:
        raise ScheduleWriteError(f"в gyms.yaml нет зала '{gym_id}'")

    before = len(target.get("schedule") or [])
    target["schedule"] = slots

    # Пробел G-1 закрыт для этого зала — снимаем пометку, иначе бот продолжит
    # говорить «расписание уточнит администратор» при заполненном расписании.
    if slots and isinstance(target.get("gap_refs"), list):
        target["gap_refs"] = [ref for ref in target["gap_refs"] if ref != "G-1"]

    kb_hash, backup = _write_and_validate(
        kb_dir, data, media_dir=media_dir, schema_version=schema_version
    )
    return WriteResult(
        gym_id=gym_id,
        slots_before=before,
        slots_after=len(slots),
        kb_hash=kb_hash,
        backup=backup,
    )


def clear_schedule(
    kb_dir: Path, gym_id: str, *, media_dir: Path, schema_version: int
) -> WriteResult:
    """Очищает расписание зала: бот снова будет отправлять вопрос администратору."""
    with kb_write_lock(kb_dir):
        return _clear_schedule_locked(
            kb_dir, gym_id, media_dir=media_dir, schema_version=schema_version
        )


def _clear_schedule_locked(
    kb_dir: Path, gym_id: str, *, media_dir: Path, schema_version: int
) -> WriteResult:
    """Тело очистки. Вызывается только под блокировкой."""
    data = _load_raw(kb_dir)
    for gym in data["gyms"]:
        if isinstance(gym, dict) and gym.get("id") == gym_id:
            before = len(gym.get("schedule") or [])
            gym["schedule"] = []
            refs = gym.get("gap_refs")
            if isinstance(refs, list) and "G-1" not in refs:
                refs.append("G-1")
            kb_hash, backup = _write_and_validate(
                kb_dir, data, media_dir=media_dir, schema_version=schema_version
            )
            return WriteResult(
                gym_id=gym_id, slots_before=before, slots_after=0, kb_hash=kb_hash, backup=backup
            )
    raise ScheduleWriteError(f"в gyms.yaml нет зала '{gym_id}'")
