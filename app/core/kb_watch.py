"""Подхват правок базы знаний без перезапуска бота.

Модуль живёт в ``app.core``, а не в ``app.kb``: слой базы знаний по контракту
(§1.1, проверяется ``tests/test_layering.py``) не имеет права ни на логирование,
ни на оркестрацию. Он умеет читать и валидировать; решать, *когда* перечитывать,
— работа уровнем выше.

Задача узкая и важная: CRM и админка в Telegram правят ``kb/*.yaml`` **в другом
процессе**, а снимок базы знаний живёт в памяти бота. Без наблюдателя владелец
школы поменял бы цену в CRM, увидел «сохранено» — и бот продолжал бы называть
старую до перезапуска. Это худший вид ошибки: интерфейс говорит, что данные
изменились, а система работает по прежним.

Три правила, из которых состоит модуль:

1. **Дёшево проверять.** Отпечаток — это ``stat`` семи файлов (мтайм и размер),
   без чтения и без хеширования. Проверка вызывается на каждом ходу диалога,
   поэтому она обязана стоить микросекунды.
2. **Невалидная база не применяется.** Ошибка валидации не роняет бота и не
   очищает снимок: бот продолжает отвечать по прежней версии, а причина
   запоминается в :attr:`KBWatcher.last_error` — её показывает CRM.
3. **Не долбиться в сломанный файл.** Отпечаток запоминается и при неудаче:
   следующая попытка будет только после того, как файлы снова изменятся.
   Иначе каждый ход диалога упирался бы в разбор семи YAML.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.kb import loader as kb_loader
from app.logging_conf import get_logger
from app.types import KBValidationError

__all__ = ["KBWatcher", "ReloadResult", "kb_fingerprint"]

_log = get_logger(__name__)

#: Отпечаток одного файла: имя, время правки в наносекундах, размер.
Fingerprint = tuple[tuple[str, int, int], ...]


def kb_fingerprint(kb_dir: Path) -> Fingerprint:
    """Отпечаток каталога базы знаний. Отсутствующий файл — ``(-1, -1)``.

    Мтайм берётся в наносекундах: на быстром диске правка и проверка укладываются
    в одну секунду, и посекундной точности не хватило бы — правка осталась бы
    незамеченной.
    """
    out: list[tuple[str, int, int]] = []
    for name in kb_loader.KB_FILES:
        path = kb_dir / name
        try:
            stat = path.stat()
        except OSError:
            out.append((name, -1, -1))
            continue
        out.append((name, stat.st_mtime_ns, stat.st_size))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class ReloadResult:
    """Итог успешной перезагрузки."""

    old_hash: str
    new_hash: str
    warnings: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """Сменился ли снимок на самом деле."""
        return self.old_hash != self.new_hash


class KBWatcher:
    """Следит за каталогом ``kb/`` и подменяет снимок, когда файлы изменились.

    Наблюдатель не владеет снимком: он вызывает тот же
    :func:`app.kb.loader.load_sync` и ту же :func:`app.kb.loader.swap`, что и
    старт приложения. Значит, правило «невалидная база не применяется никогда»
    действует и здесь, без отдельной ветки кода.
    """

    def __init__(
        self,
        kb_dir: Path,
        *,
        media_dir: Path,
        schema_version: int,
        min_interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._kb_dir = Path(kb_dir)
        self._media_dir = Path(media_dir)
        self._schema_version = int(schema_version)
        self._min_interval_s = max(0.0, float(min_interval_s))
        self._clock = clock
        self._seen: Fingerprint = kb_fingerprint(self._kb_dir)
        self._last_check: float = clock()
        #: Последняя ошибка валидации. ``None`` — база в порядке.
        self.last_error: KBValidationError | None = None
        #: Предупреждения последней успешной загрузки (например, выключённое медиа).
        self.last_warnings: tuple[str, ...] = ()

    # ------------------------------------------------------------------ опрос
    def due(self) -> bool:
        """Пора ли вообще трогать диск. Ограничитель частоты, не логика."""
        if self._min_interval_s <= 0:
            return True
        return (self._clock() - self._last_check) >= self._min_interval_s

    def check(self) -> ReloadResult | None:
        """Синхронная проверка. ``None`` — изменений нет либо ещё рано.

        Единственная точка, где снимок подменяется по внешней правке.
        """
        if not self.due():
            return None
        self._last_check = self._clock()

        current = kb_fingerprint(self._kb_dir)
        if current == self._seen:
            return None

        # Отпечаток запоминаем ДО загрузки: если база сломана, повторять разбор
        # на каждом ходу бессмысленно — ждём следующей правки.
        self._seen = current

        try:
            snapshot, warnings = kb_loader.load_sync(
                self._kb_dir, media_dir=self._media_dir, schema_version=self._schema_version
            )
        except KBValidationError as exc:
            self.last_error = exc
            self.last_warnings = ()
            _log.error(
                "kb_reload_rejected",
                errors=list(exc.errors)[:5],
                hint="бот продолжает отвечать по прежней версии базы знаний",
            )
            return None

        old_hash = kb_loader.swap(snapshot)
        self.last_error = None
        self.last_warnings = tuple(warnings)
        result = ReloadResult(old_hash=old_hash, new_hash=snapshot.kb_hash, warnings=tuple(warnings))
        if result.changed:
            _log.info("kb_reloaded", old=old_hash[:12], new=snapshot.kb_hash[:12])
        return result

    async def maybe_reload(self) -> ReloadResult | None:
        """То же, но не блокирует цикл событий: разбор YAML уходит в поток."""
        if not self.due():
            return None
        return await asyncio.to_thread(self.check)


#: Сколько ошибок валидации показывать в одном сообщении оператору.
ERROR_PREVIEW: Final[int] = 5
