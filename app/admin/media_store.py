"""Регистрация фото и видео, присланных администратором в чат.

Тот же принцип, что и с расписанием: **не менять привычку человека**. Владелец
и так весь день пересылает фото и видео в мессенджере — здесь он делает ровно
то же самое, только боту, и одной строкой пишет, когда это показывать клиенту.

Ни путей, ни YAML, ни переноса файлов руками. Ни сопоставления имён вида
``WhatsApp Video 2026-08-05 at 11.22.50.mp4`` с содержимым — эту работу человек
всё равно сделал бы плохо.

Защита та же, что у расписания: резервная копия, атомарная запись, полная
валидация базы знаний и откат при любой ошибке. Пока новая база не прошла
проверку, бот отвечает по старой.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import yaml

from app.admin.kb_lock import kb_write_lock
from app.kb import loader as kb_loader
from app.logging_conf import get_logger

__all__ = ["MediaWriteError", "MediaRegistration", "register_media", "slugify"]

_log = get_logger(__name__)

#: Транслитерация для id артефакта: он обязан быть ``^[a-z0-9_]+$``.
_TRANSLIT: Final[dict[str, str]] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya", "ә": "a", "ғ": "g", "қ": "k", "ң": "n", "ө": "o",
    "ұ": "u", "ү": "u", "һ": "h", "і": "i",
}

#: Видео уходит вложением только в Telegram: у Wazzup потолок 10 МБ, а в
#: Instagram Direct видео не отправляется вовсе.
_VIDEO_CHANNELS: Final[dict[str, str]] = {
    "telegram": "allow", "whatsapp": "deny", "instagram": "deny"
}
_PHOTO_CHANNELS: Final[dict[str, str]] = {
    "telegram": "allow", "whatsapp": "allow", "instagram": "allow"
}


class MediaWriteError(RuntimeError):
    """Материал зарегистрировать не удалось. База знаний осталась прежней."""


@dataclass(slots=True)
class MediaRegistration:
    """Что именно записано в базу знаний."""

    artifact_id: str
    kind: str
    file_name: str
    size_bytes: int
    kb_hash: str


def slugify(text: str, *, limit: int = 40) -> str:
    """Русский текст → ``snake_case`` латиницей для id артефакта."""
    lowered = (text or "").strip().lower()
    out: list[str] = []
    for char in lowered:
        if char in _TRANSLIT:
            out.append(_TRANSLIT[char])
        elif char.isalnum() and char.isascii():
            out.append(char)
        elif char in " -_/\\.,":
            out.append("_")
    slug = re.sub(r"_+", "_", "".join(out)).strip("_")[:limit].strip("_")
    return slug or "material"


def _unique_id(base: str, taken: set[str]) -> str:
    """Уникальный id артефакта: к занятому добавляется номер."""
    candidate = base
    index = 2
    while candidate in taken:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def register_media(
    *,
    kb_dir: Path,
    media_dir: Path,
    schema_version: int,
    source: Path,
    kind: str,
    when_to_send: str,
    title_ru: str,
    gym_id: str | None = None,
) -> MediaRegistration:
    """Кладёт файл в ``media/`` и заводит артефакт в ``kb/media.yaml``.

    ``when_to_send`` — самое важное поле: по нему модель решает, в ответ на какой
    вопрос отправить материал. Имя файла для выбора не используется вовсе.

    :raises MediaWriteError: файл не читается либо база не прошла валидацию;
        в обоих случаях ни файл, ни база не остаются в промежуточном состоянии.
    """
    with kb_write_lock(kb_dir):
        return _register_media_locked(
            kb_dir=kb_dir,
            media_dir=media_dir,
            schema_version=schema_version,
            source=source,
            kind=kind,
            when_to_send=when_to_send,
            title_ru=title_ru,
            gym_id=gym_id,
        )


def _register_media_locked(
    *,
    kb_dir: Path,
    media_dir: Path,
    schema_version: int,
    source: Path,
    kind: str,
    when_to_send: str,
    title_ru: str,
    gym_id: str | None = None,
) -> MediaRegistration:
    """Тело регистрации материала. Вызывается только под блокировкой."""
    if kind not in ("image", "video"):
        raise MediaWriteError(f"неизвестный тип материала: {kind}")
    if not source.is_file():
        raise MediaWriteError(f"файл не найден: {source}")
    description = (when_to_send or "").strip()
    if len(description) < 5:
        raise MediaWriteError(
            "нужно одной строкой описать, когда отправлять материал — "
            "например «тренировка младшей группы, когда спрашивают как проходят занятия»"
        )

    path = kb_dir / "media.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise MediaWriteError(f"не читается media.yaml: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("artifacts"), list):
        raise MediaWriteError("media.yaml: ожидался список artifacts")

    taken = {a.get("id") for a in data["artifacts"] if isinstance(a, dict)}
    prefix = "video" if kind == "video" else "photo"
    artifact_id = _unique_id(f"{prefix}_{slugify(title_ru or description)}", taken)

    suffix = ".mp4" if kind == "video" else ".jpg"
    file_name = f"{artifact_id}{suffix}"
    target = media_dir / file_name
    media_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)

    blob = target.read_bytes()
    data["artifacts"].append(
        {
            "id": artifact_id,
            "kind": kind,
            "enabled": True,
            "scope": "any",
            "gym_id": gym_id,
            "title": {"ru": title_ru.strip(), "kk": title_ru.strip()},
            "when_to_send_ru": description,
            # Подпись не задаём: её пишет модель, на языке клиента и по контексту.
            # Хранить русский текст в казахском поле значило бы подсунуть
            # казахоязычному родителю русскую подпись.
            "body": {"ru": None, "kk": None},
            "file_path": file_name,
            "file_mime": "video/mp4" if kind == "video" else "image/jpeg",
            "file_bytes": len(blob),
            "file_sha256": hashlib.sha256(blob).hexdigest(),
            "channels": dict(_VIDEO_CHANNELS if kind == "video" else _PHOTO_CHANNELS),
            "max_send_per_dialog": 1,
            "gap_ref": None,
            "render_from": None,
        }
    )

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(f".yaml.bak-{stamp}")
    shutil.copy2(path, backup)
    tmp = path.with_suffix(".yaml.tmp")
    try:
        tmp.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8"
        )
        tmp.replace(path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise MediaWriteError(f"не удалось записать media.yaml: {exc}") from exc

    try:
        snapshot, problems = kb_loader.load_sync(
            kb_dir, media_dir=media_dir, schema_version=schema_version
        )
        if problems:
            raise ValueError("; ".join(problems[:3]))
    except Exception as exc:  # noqa: BLE001 — любая ошибка означает полный откат
        shutil.copy2(backup, path)
        target.unlink(missing_ok=True)
        raise MediaWriteError(
            f"материал отклонён, база осталась прежней: {exc}"
        ) from exc

    kb_loader.swap(snapshot)
    _log.info("media_registered", artifact_id=artifact_id, kind=kind, kb_hash=snapshot.kb_hash)
    return MediaRegistration(
        artifact_id=artifact_id,
        kind=kind,
        file_name=file_name,
        size_bytes=len(blob),
        kb_hash=snapshot.kb_hash,
    )
