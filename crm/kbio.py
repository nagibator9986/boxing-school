"""Безопасная правка базы знаний из CRM.

База знаний — единственный источник правды для бота: цены, адреса, расписание,
ответы на вопросы. Любая правка отсюда обязана пройти тот же контроль, что и
правка из Telegram-админки, поэтому порядок один и тот же:

1. **резервная копия всех семи файлов** (а не только изменённого: ошибка может
   вскрыться в перекрёстной проверке, где виноват другой файл);
2. **атомарная запись** через временный файл и ``replace`` — читатель никогда не
   увидит полуфайл;
3. **полная валидация** тем же :func:`app.kb.loader.load_sync`, которым база
   грузится на старте: схема, перекрёстные ссылки, наличие медиа на диске;
4. **откат при любой ошибке** — все файлы возвращаются из копии, а вызывающему
   уходит список всех проблем сразу, а не первой.

Пока новая база не прошла проверку, бот отвечает по старой. Это то же правило,
что и в загрузчике: половинчатая база хуже отсутствующей, потому что бот начнёт
врать клиентам, не сообщив об этом никому.

Комментарии в YAML сохраняются (``ruamel.yaml`` в режиме round-trip). Это не
косметика: в ``pricing.yaml`` и ``policies.yaml`` комментариями объяснены
решения — почему оговорка звучит до продажи, что означает конфликт C-6. Стирать
их при каждом сохранении значило бы медленно уничтожать знание о системе.
"""

from __future__ import annotations

import io
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import yaml as pyyaml
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from app.admin.kb_lock import DEFAULT_TIMEOUT_S as DEFAULT_LOCK_TIMEOUT_S
from app.admin.kb_lock import KBLockTimeout, kb_write_lock
from app.kb import loader as kb_loader
from app.kb.models import KBSnapshot
from app.types import KBValidationError

__all__ = ["BackupInfo", "KBEditError", "KBEditor", "SaveResult", "merge_into", "quote_ambiguous"]

#: Сколько резервных копий держим. Каждая — семь небольших файлов.
BACKUP_KEEP: Final[int] = 40


def _yaml() -> YAML:
    """Round-trip парсер с настройками, при которых диффы остаются читаемыми."""
    engine = YAML()
    engine.preserve_quotes = True
    # Широкая строка: иначе длинные ответы FAQ переносятся по словам, и правка
    # одного слова даёт диффом весь абзац.
    engine.width = 4096
    engine.indent(mapping=2, sequence=2, offset=0)
    # ``None`` печатаем как ``null``: пустое значение после двоеточия читается
    # человеком как «забыли заполнить», а это осмысленное «данных нет».
    engine.representer.add_representer(
        type(None), lambda dumper, _: dumper.represent_scalar("tag:yaml.org,2002:null", "null")
    )
    return engine


def quote_ambiguous(node: Any) -> Any:
    """Расставляет кавычки там, где без них YAML прочитает строку как число.

    Ловушка стоила бы дорого. Загрузчик бота читает YAML библиотекой ``pyyaml``
    в редакции 1.1, где ``10:30`` — это **шестидесятеричное число** 630, а
    ``yes`` — булево «истина». ``ruamel`` пишет по редакции 1.2, где всё это
    строки, и кавычек не ставит. В результате сохранённое из CRM время занятия
    возвращалось в базу целым числом, и зал терял расписание — при том, что на
    экране всё выглядело правильно.

    Поэтому каждая однострочная строка проверяется тем же ``pyyaml``, которым
    её будет читать бот: не совпало — заворачиваем в кавычки.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            node[key] = quote_ambiguous(value)
        return node
    if isinstance(node, list):
        for index, value in enumerate(node):
            node[index] = quote_ambiguous(value)
        return node
    if isinstance(node, DoubleQuotedScalarString) or not isinstance(node, str):
        return node
    if "\n" in node or not node:
        # Многострочный текст ruamel и так оформит блоком, а пустая строка
        # схемой запрещена и до записи не дойдёт.
        return node
    try:
        parsed = pyyaml.safe_load(node)
    except Exception:  # noqa: BLE001 - строка, которую YAML не разберёт вовсе
        return DoubleQuotedScalarString(node)
    return node if isinstance(parsed, str) and parsed == node else DoubleQuotedScalarString(node)


def merge_into(existing: Any, new: Any) -> Any:
    """Обновляет значение на месте, сохраняя комментарии соседних строк.

    Присвоить новый словарь вместо старого — значит выбросить комментарии,
    которые ruamel хранит привязанными к узлу. В ``pricing.yaml`` так исчезало
    объяснение конфликта C-4 — знание, которое дороже самой правки.
    """
    if isinstance(existing, dict) and isinstance(new, dict):
        for key in [key for key in existing if key not in new]:
            del existing[key]
        for key, value in new.items():
            existing[key] = merge_into(existing.get(key), value)
        return existing
    return new


class KBEditError(RuntimeError):
    """База знаний отклонена. На диске осталась прежняя версия.

    ``errors`` — все найденные проблемы, в формате «файл: поле: что не так».
    """

    def __init__(self, message: str, errors: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.errors = list(errors)


@dataclass(frozen=True, slots=True)
class SaveResult:
    """Итог удачного сохранения."""

    kb_hash: str
    warnings: tuple[str, ...]
    backup: str
    changed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """Одна резервная копия базы знаний."""

    stamp: str
    when: datetime
    files: int

    @property
    def title(self) -> str:
        """Человеческая подпись для списка в интерфейсе."""
        return self.when.strftime("%d.%m.%Y %H:%M:%S")


class KBEditor:
    """Чтение и запись ``kb/*.yaml`` с валидацией и откатом."""

    def __init__(
        self,
        kb_dir: Path,
        *,
        media_dir: Path,
        schema_version: int,
        lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
        tz: str = "Asia/Almaty",
    ) -> None:
        self._kb_dir = Path(kb_dir)
        self._media_dir = Path(media_dir)
        self._schema_version = int(schema_version)
        self._lock_timeout_s = float(lock_timeout_s)
        try:
            self._tz = ZoneInfo(tz)
        except Exception:  # noqa: BLE001 - кривое имя зоны не повод падать
            self._tz = UTC
        self._yaml = _yaml()

    # ------------------------------------------------------------------ чтение
    @property
    def backups_dir(self) -> Path:
        """Каталог резервных копий."""
        return self._kb_dir / ".backups"

    def load(self, name: str) -> Any:
        """Читает один файл базы знаний с сохранением комментариев."""
        path = self._kb_dir / name
        if not path.is_file():
            raise KBEditError(f"файл {name} не найден в {self._kb_dir}")
        return self._yaml.load(path.read_text(encoding="utf-8"))

    def snapshot(self) -> KBSnapshot:
        """Текущий валидный снимок базы знаний.

        CRM работает на собственном снимке: он нужен, чтобы показывать данные в
        том же виде, в каком их видит бот, — с проверенными ссылками и
        выключенными артефактами, у которых потерялся файл.
        """
        snapshot, _ = kb_loader.load_sync(
            self._kb_dir, media_dir=self._media_dir, schema_version=self._schema_version
        )
        return snapshot

    def dump(self, document: Any) -> str:
        """Документ обратно в YAML — для показа «как это ляжет в файл»."""
        buffer = io.StringIO()
        self._yaml.dump(quote_ambiguous(document), buffer)
        return buffer.getvalue()

    # ------------------------------------------------------------------ запись
    def save(self, documents: dict[str, Any], *, actor: str = "admin") -> SaveResult:
        """Записывает изменённые файлы, валидирует базу целиком, при ошибке откатывает.

        :param documents: ``{имя файла: документ}``. Незатронутые файлы
            перечислять не нужно, но резервируются всё равно все.
        :raises KBEditError: база не прошла проверку; на диске прежняя версия.
        """
        unknown = set(documents) - set(kb_loader.KB_FILES)
        if unknown:
            raise KBEditError(f"неизвестные файлы базы знаний: {', '.join(sorted(unknown))}")
        if not documents:
            raise KBEditError("нечего сохранять")

        # Правку из Telegram и правку из CRM делают разные процессы. Без общей
        # блокировки откат неудачной правки затирает удачную чужую, и обе
        # стороны видят «сохранено».
        try:
            with kb_write_lock(self._kb_dir, timeout_s=self._lock_timeout_s):
                return self._save_locked(documents, actor=actor)
        except KBLockTimeout as exc:
            raise KBEditError(str(exc)) from exc

    def _save_locked(self, documents: dict[str, Any], *, actor: str) -> SaveResult:
        """Тело сохранения. Вызывается только под блокировкой."""
        backup = self._backup()
        for name, document in documents.items():
            self._stamp(document, actor=actor)
            self._write_atomic(self._kb_dir / name, self.dump(document))

        try:
            snapshot, warnings = kb_loader.load_sync(
                self._kb_dir, media_dir=self._media_dir, schema_version=self._schema_version
            )
        except KBValidationError as exc:
            self._restore(backup)
            raise KBEditError(
                "правка отклонена, база знаний осталась прежней", exc.errors
            ) from exc
        except Exception as exc:
            self._restore(backup)
            raise KBEditError("правка отклонена, база знаний осталась прежней", [str(exc)]) from exc

        # Свой снимок обновляем сразу: иначе CRM показывала бы старые данные до
        # перезагрузки страницы. Бот подхватит правку сам, по отпечатку каталога.
        kb_loader.swap(snapshot)
        self._prune()
        return SaveResult(
            kb_hash=snapshot.kb_hash,
            warnings=tuple(warnings),
            backup=backup.name,
            changed=tuple(sorted(documents)),
        )

    # --------------------------------------------------------------- откат
    def backups(self) -> list[BackupInfo]:
        """Список резервных копий, свежие сверху."""
        if not self.backups_dir.is_dir():
            return []
        out: list[BackupInfo] = []
        for directory in sorted(self.backups_dir.iterdir(), reverse=True):
            if not directory.is_dir():
                continue
            try:
                # Имя копии — время в UTC; показываем костанайское, иначе список
                # копий отстаёт от часов на стене на пять часов.
                when = datetime.strptime(directory.name[:15], "%Y%m%d-%H%M%S").replace(
                    tzinfo=UTC
                ).astimezone(self._tz)
            except ValueError:
                continue
            out.append(
                BackupInfo(stamp=directory.name, when=when, files=len(list(directory.glob("*.yaml"))))
            )
        return out

    def restore(self, stamp: str) -> SaveResult:
        """Возвращает базу знаний к состоянию из копии ``stamp``.

        Перед откатом снимается ещё одна копия: откат — тоже правка, и вернуться
        из него должно быть так же просто, как в него уйти.
        """
        source = self.backups_dir / stamp
        if not source.is_dir():
            raise KBEditError(f"копия {stamp} не найдена")
        try:
            with kb_write_lock(self._kb_dir, timeout_s=self._lock_timeout_s):
                return self._restore_locked(source)
        except KBLockTimeout as exc:
            raise KBEditError(str(exc)) from exc

    def _restore_locked(self, source: Path) -> SaveResult:
        """Тело отката. Вызывается только под блокировкой."""
        safety = self._backup()
        for name in kb_loader.KB_FILES:
            candidate = source / name
            if candidate.is_file():
                self._write_atomic(self._kb_dir / name, candidate.read_text(encoding="utf-8"))
        try:
            snapshot, warnings = kb_loader.load_sync(
                self._kb_dir, media_dir=self._media_dir, schema_version=self._schema_version
            )
        except Exception as exc:
            self._restore(safety)
            raise KBEditError("копия не восстановилась, база осталась прежней", [str(exc)]) from exc
        kb_loader.swap(snapshot)
        return SaveResult(
            kb_hash=snapshot.kb_hash,
            warnings=tuple(warnings),
            backup=safety.name,
            changed=tuple(kb_loader.KB_FILES),
        )

    # ------------------------------------------------------------ внутреннее
    def _stamp(self, document: Any, *, actor: str) -> None:
        """Проставляет, кто и когда правил. Поля есть в схеме каждого файла.

        Дата местная, костанайская: сервер живёт по UTC, и правка в девять
        вечера записалась бы вчерашним числом.
        """
        try:
            document["updated_at"] = datetime.now(tz=self._tz).date().isoformat()
            if "updated_by" in document:
                document["updated_by"] = "admin" if actor != "dev" else "dev"
        except (TypeError, KeyError):  # pragma: no cover - документ не словарь
            pass

    def _write_atomic(self, path: Path, text: str) -> None:
        """Запись через временный файл: читатель не увидит половину файла."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def _backup(self) -> Path:
        """Копирует все файлы базы знаний в каталог с отметкой времени."""
        stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
        target = self.backups_dir / stamp
        suffix = 1
        while target.exists():  # правки в одну секунду — редкость, но возможны
            suffix += 1
            target = self.backups_dir / f"{stamp}-{suffix}"
        target.mkdir(parents=True)
        for name in kb_loader.KB_FILES:
            source = self._kb_dir / name
            if source.is_file():
                shutil.copy2(source, target / name)
        return target

    def _restore(self, backup: Path) -> None:
        """Возвращает все файлы из копии. Вызывается только при ошибке."""
        for name in kb_loader.KB_FILES:
            candidate = backup / name
            if candidate.is_file():
                self._write_atomic(self._kb_dir / name, candidate.read_text(encoding="utf-8"))

    def _prune(self) -> None:
        """Оставляет последние :data:`BACKUP_KEEP` копий."""
        directories = sorted(
            (d for d in self.backups_dir.iterdir() if d.is_dir()), reverse=True
        ) if self.backups_dir.is_dir() else []
        for stale in directories[BACKUP_KEEP:]:
            shutil.rmtree(stale, ignore_errors=True)
