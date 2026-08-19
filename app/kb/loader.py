"""Загрузчик базы знаний: чтение YAML, валидация, атомарная подмена снимка.

Смысл модуля в трёх правилах:

1. **Невалидная KB не применяется никогда.** На старте это отказ поднять
   приложение, на hot-reload — HTTP 422 и продолжение работы на предыдущем
   снимке. Половинчатая база хуже отсутствующей: бот начнёт врать клиентам.
2. **Ошибки собираются все сразу.** Владелец школы правит YAML сам, и десять
   раундов «исправил одну — вылезла вторая» его прогонят.
3. **Подмена снимка атомарна.** Диалоги, идущие в момент перезагрузки, дочитают
   старый снимок до конца хода; следующий ход возьмёт новый.

Сети модуль не касается, окружение не читает: пути и версия схемы приходят
параметрами от вызывающего (lifespan приложения или CLI).
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Mapping, Sequence

import yaml
from pydantic import ValidationError

from app.types import KBNotLoadedError, KBValidationError
from app.kb.models import (
    Artifact,
    FaqFile,
    GymsFile,
    I18nFile,
    KBSnapshot,
    LexiconFile,
    MediaFile,
    PoliciesFile,
    PricingFile,
    cross_reference_errors,
)

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from sqlalchemy.ext.asyncio import AsyncSession

#: Имена файлов KB в порядке загрузки. Порядок влияет только на порядок ошибок.
KB_FILES: Final[tuple[str, ...]] = (
    "gyms.yaml",
    "pricing.yaml",
    "faq.yaml",
    "media.yaml",
    "policies.yaml",
    "i18n.yaml",
    "lexicon.yaml",
)

#: Колонки таблицы-зеркала `gym` (INTERFACES §5). Лишние игнорируются.
_GYM_COLUMNS: Final[tuple[str, ...]] = (
    "id", "scope", "settlement", "is_head", "active", "status",
    "title_ru", "title_kk", "address_ru", "address_kk",
    "landmark_ru", "landmark_kk", "district_ru", "district_kk",
    "district_aliases", "lat", "lon", "map_url", "phone", "has_schedule", "kb_hash",
)

_LOCK: Final[threading.RLock] = threading.RLock()
_SNAPSHOT: KBSnapshot | None = None


# --------------------------------------------------------------------------- #
# Чтение и хеш
# --------------------------------------------------------------------------- #
def _read_files(kb_dir: Path) -> dict[str, bytes]:
    """Читает все файлы KB как байты. Отсутствие файла — ошибка загрузки."""
    if not kb_dir.is_dir():
        raise KBValidationError(
            f"каталог базы знаний не найден: {kb_dir}",
            errors=(f"{kb_dir}: каталог kb/ отсутствует",),
        )
    raw: dict[str, bytes] = {}
    missing: list[str] = []
    for name in KB_FILES:
        path = kb_dir / name
        if not path.is_file():
            missing.append(f"{name}: файл отсутствует в {kb_dir}")
            continue
        raw[name] = path.read_bytes()
    if missing:
        raise KBValidationError("в каталоге kb/ не хватает файлов", errors=missing)
    return raw


def compute_kb_hash(files: Mapping[str, bytes]) -> str:
    """sha256 от детерминированной сериализации содержимого файлов.

    Имена сортируются, переводы строк нормализуются в ``\\n`` — иначе один и тот
    же контент, сохранённый на Windows и на macOS, дал бы разный хеш и без нужды
    сбросил implicit-кэш Gemini.
    """
    digest = hashlib.sha256()
    for name in sorted(files):
        body = files[name].replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        digest.update(name.encode("utf-8"))
        digest.update(b"\n")
        digest.update(body)
        digest.update(b"\n")
    return digest.hexdigest()


def _parse_yaml(name: str, body: bytes, errors: list[str]) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(body.decode("utf-8"))
    except UnicodeDecodeError as exc:
        errors.append(f"{name}: файл не в UTF-8: {exc}")
        return None
    except yaml.YAMLError as exc:
        errors.append(f"{name}: синтаксическая ошибка YAML: {_short(exc)}")
        return None
    if data is None:
        errors.append(f"{name}: файл пуст")
        return None
    if not isinstance(data, dict):
        errors.append(f"{name}: ожидался словарь верхнего уровня, получено {type(data).__name__}")
        return None
    return data


def _short(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")
    return text if len(text) <= 300 else text[:297] + "..."


def _validate_file(name: str, model: type, data: dict[str, Any], errors: list[str]) -> Any:
    """Валидирует один файл; ошибки кладёт в общий список в формате «файл: поле: текст»."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "<файл>"
            errors.append(f"{name}: {location}: {error['msg']}")
        return None


def _check_schema_version(name: str, data: dict[str, Any], expected: int, errors: list[str]) -> None:
    actual = data.get("schema_version")
    if actual != expected:
        errors.append(
            f"{name}: schema_version={actual!r}, а код ожидает {expected}. "
            "Это блокер старта: формат файла и код разошлись."
        )


# --------------------------------------------------------------------------- #
# Медиа
# --------------------------------------------------------------------------- #
def _verify_media(
    artifacts: Sequence[Artifact], media_dir: Path
) -> tuple[list[Artifact], list[str]]:
    """Сверяет файлы артефактов с диском.

    Расхождение не роняет загрузку: артефакт принудительно выключается, чтобы
    бот не отправил клиенту не тот файл, а причина уходит в предупреждения.
    """
    checked: list[Artifact] = []
    warnings: list[str] = []
    for artifact in artifacts:
        if not artifact.enabled or artifact.file_path is None:
            checked.append(artifact)
            continue
        path = media_dir / artifact.file_path
        problem: str | None = None
        if not path.is_file():
            problem = f"файл {path} не найден"
        else:
            size = path.stat().st_size
            if artifact.file_bytes is not None and size != artifact.file_bytes:
                problem = f"размер файла {size} байт, в media.yaml указано {artifact.file_bytes}"
            elif artifact.file_sha256 is not None:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != artifact.file_sha256:
                    problem = f"контрольная сумма не совпала (на диске {actual})"
        if problem is None:
            checked.append(artifact)
        else:
            warnings.append(f"media.yaml: артефакт '{artifact.id}' выключен: {problem}")
            checked.append(artifact.model_copy(update={"enabled": False}))
    return checked, warnings


# --------------------------------------------------------------------------- #
# Загрузка
# --------------------------------------------------------------------------- #
def load_sync(kb_dir: Path, *, media_dir: Path, schema_version: int) -> tuple[KBSnapshot, list[str]]:
    """Синхронная загрузка снимка. Возвращает ``(снимок, предупреждения)``.

    Отдельная синхронная функция нужна CLI-проверке: ей незачем поднимать
    event loop ради чтения семи файлов.
    """
    raw = _read_files(kb_dir)
    errors: list[str] = []
    parsed: dict[str, dict[str, Any]] = {}
    for name in KB_FILES:
        data = _parse_yaml(name, raw[name], errors)
        if data is not None:
            _check_schema_version(name, data, schema_version, errors)
            parsed[name] = data
    if errors:
        raise KBValidationError("база знаний не читается", errors=errors)

    gyms = _validate_file("gyms.yaml", GymsFile, parsed["gyms.yaml"], errors)
    pricing = _validate_file("pricing.yaml", PricingFile, parsed["pricing.yaml"], errors)
    faq_file = _validate_file("faq.yaml", FaqFile, parsed["faq.yaml"], errors)
    media_file = _validate_file("media.yaml", MediaFile, parsed["media.yaml"], errors)
    policies = _validate_file("policies.yaml", PoliciesFile, parsed["policies.yaml"], errors)
    i18n = _validate_file("i18n.yaml", I18nFile, parsed["i18n.yaml"], errors)
    lexicon = _validate_file("lexicon.yaml", LexiconFile, parsed["lexicon.yaml"], errors)
    if errors:
        raise KBValidationError("база знаний не прошла валидацию", errors=errors)

    artifacts, warnings = _verify_media(media_file.artifacts, media_dir)

    errors.extend(
        cross_reference_errors(
            gyms=gyms,
            pricing=pricing,
            faq=faq_file.entries,
            media=artifacts,
            policies=policies,
            i18n=i18n,
        )
    )
    errors.extend(_i18n_coverage_errors(i18n))
    if errors:
        raise KBValidationError("база знаний не прошла валидацию", errors=errors)

    snapshot = KBSnapshot(
        kb_hash=compute_kb_hash(raw),
        loaded_at=datetime.now(timezone.utc),
        gyms=gyms,
        pricing=pricing,
        faq=faq_file.entries,
        media=artifacts,
        policies=policies,
        i18n=i18n,
        lexicon=lexicon,
    )
    return snapshot, warnings


def _i18n_coverage_errors(i18n: I18nFile) -> list[str]:
    """Проверяет, что все ключи, которые произносит код, есть в ``i18n.yaml``.

    Отсутствие ключа обязано вскрываться на загрузке, а не в живом диалоге.
    """
    from app.kb.gaps import required_i18n_keys  # локальный импорт: gaps зависит от models

    missing = [key for key in required_i18n_keys() if key not in i18n.strings]
    return [f"i18n.yaml: отсутствует обязательный ключ '{key}'" for key in sorted(missing)]


async def load(kb_dir: Path, *, media_dir: Path, schema_version: int) -> KBSnapshot:
    """Читает 7 YAML, валидирует, проверяет медиа-файлы, считает kb_hash.

    Кидает :class:`app.types.KBValidationError` со списком всех найденных ошибок
    (не первой). Сети не касается; вызывается в lifespan и в ``/admin/kb/reload``.
    """
    snapshot, _ = await asyncio.to_thread(
        load_sync, kb_dir, media_dir=media_dir, schema_version=schema_version
    )
    return snapshot


# --------------------------------------------------------------------------- #
# Снимок в памяти
# --------------------------------------------------------------------------- #
def get_snapshot() -> KBSnapshot:
    """Текущий снимок. До первой загрузки — :class:`KBNotLoadedError`."""
    snapshot = _SNAPSHOT
    if snapshot is None:
        raise KBNotLoadedError("база знаний ещё не загружена")
    return snapshot


def current_hash() -> str:
    """``kb_hash`` текущего снимка или пустая строка, если снимка нет."""
    snapshot = _SNAPSHOT
    return snapshot.kb_hash if snapshot is not None else ""


def swap(snapshot: KBSnapshot) -> str:
    """Атомарная подмена ссылки. Возвращает предыдущий ``kb_hash`` (или ``''``)."""
    global _SNAPSHOT
    with _LOCK:
        previous = _SNAPSHOT.kb_hash if _SNAPSHOT is not None else ""
        _SNAPSHOT = snapshot
    return previous


def clear() -> None:
    """Сброс снимка. Только для тестов."""
    global _SNAPSHOT
    with _LOCK:
        _SNAPSHOT = None


async def reload(kb_dir: Path, *, media_dir: Path, schema_version: int) -> tuple[str, str]:
    """Загрузка + swap. Возвращает ``(old_hash, new_hash)``.

    При ошибке валидации исключение уходит вызывающему, а прежний снимок
    остаётся в памяти нетронутым — бот продолжает работать на старой версии.
    """
    snapshot = await load(kb_dir, media_dir=media_dir, schema_version=schema_version)
    old_hash = swap(snapshot)
    return old_hash, snapshot.kb_hash


# --------------------------------------------------------------------------- #
# Зеркало в БД
# --------------------------------------------------------------------------- #
async def mirror_to_db(session: "AsyncSession", snapshot: KBSnapshot) -> int:
    """Полная перезапись таблицы ``gym`` в транзакции. Возвращает число строк.

    Таблица — read-model для отчётов и джойнов; источником правды остаётся YAML.
    Схема таблицы читается отражением: колонки принадлежат слою хранения, и
    загрузчик не имеет права от него зависеть.
    """
    from sqlalchemy import MetaData, Table  # локальный импорт: на старте не нужен

    metadata = MetaData()

    def _reflect(sync_session: Any) -> Table:
        return Table("gym", metadata, autoload_with=sync_session.get_bind())

    table = await session.run_sync(_reflect)
    columns = {name for name in table.columns.keys() if name in _GYM_COLUMNS}

    rows: list[dict[str, Any]] = []
    for gym in snapshot.gyms.gyms:
        row = {
            "id": gym.id,
            "scope": gym.scope.value,
            "settlement": gym.settlement,
            "is_head": gym.is_head,
            "active": gym.active,
            "status": gym.status.value,
            "title_ru": gym.title.ru,
            "title_kk": gym.title.kk,
            "address_ru": gym.address.ru,
            "address_kk": gym.address.kk,
            "landmark_ru": gym.landmark.ru,
            "landmark_kk": gym.landmark.kk,
            "district_ru": gym.district.ru,
            "district_kk": gym.district.kk,
            "district_aliases": list(gym.district_aliases),
            "lat": gym.geo_lat,
            "lon": gym.geo_lon,
            "map_url": gym.map_url,
            "phone": gym.phone,
            "has_schedule": gym.has_schedule,
            "kb_hash": snapshot.kb_hash,
        }
        rows.append({key: value for key, value in row.items() if key in columns})

    await session.execute(table.delete())
    if rows:
        await session.execute(table.insert(), rows)
    return len(rows)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    """CLI ``python -m app.kb.loader --check``: 0 — валидна, 1 — нет."""
    args = list(sys.argv[1:] if argv is None else argv)
    kb_dir = Path("kb")
    media_dir = Path("media")
    schema_version = 1
    for index, arg in enumerate(args):
        if arg == "--kb-dir" and index + 1 < len(args):
            kb_dir = Path(args[index + 1])
        elif arg == "--media-dir" and index + 1 < len(args):
            media_dir = Path(args[index + 1])
        elif arg == "--schema-version" and index + 1 < len(args):
            schema_version = int(args[index + 1])
    try:
        snapshot, warnings = load_sync(kb_dir, media_dir=media_dir, schema_version=schema_version)
    except KBValidationError as exc:
        print(f"БАЗА ЗНАНИЙ НЕВАЛИДНА: {exc.message}")
        for line in exc.errors:
            print(f"  - {line}")
        return 1
    for line in warnings:
        print(f"  ! {line}")
    open_gaps = ", ".join(gap.value for gap in snapshot.gaps()) or "нет"
    print(f"База знаний валидна. kb_hash={snapshot.kb_hash[:12]} пробелов: {open_gaps}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "KB_FILES",
    "clear",
    "compute_kb_hash",
    "current_hash",
    "get_snapshot",
    "load",
    "load_sync",
    "main",
    "mirror_to_db",
    "reload",
    "swap",
]
