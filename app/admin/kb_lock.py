"""Блокировка записи в базу знаний между процессами.

Базу знаний правят из двух мест сразу: CRM в браузере и админка в Telegram — и
это **разные процессы**. Каждая правка устроена одинаково: снять копию, записать
файлы, проверить базу целиком, при ошибке откатить. Без общей блокировки две
правки складываются в неверный результат:

* обе снимают копию одного и того же состояния;
* первая записывает своё, вторая записывает своё поверх;
* если вторая не прошла проверку, она откатывает файлы к копии — и вместе со
  своей неудачной правкой стирает удачную правку первой.

Заметить это невозможно: обе стороны увидели «сохранено». Поэтому запись
сериализуется файловой блокировкой в каталоге базы знаний.

Блокировка нужна только на запись. Читателям она не мешает: запись атомарна
(временный файл и ``replace``), и читатель видит либо старый файл, либо новый.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from app.logging_conf import get_logger

__all__ = ["LOCK_NAME", "KBLockTimeout", "kb_write_lock"]

_log = get_logger(__name__)

#: Файл блокировки внутри каталога базы знаний.
LOCK_NAME: Final[str] = ".write.lock"

#: Сколько ждать освобождения. Правка занимает доли секунды; десять секунд —
#: это «другой процесс завис», и лучше сказать об этом человеку.
DEFAULT_TIMEOUT_S: Final[float] = 10.0

try:  # pragma: no cover - на Linux и macOS есть всегда
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


class KBLockTimeout(RuntimeError):
    """Другой процесс держит базу знаний дольше отведённого времени."""


@contextmanager
def kb_write_lock(kb_dir: Path, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> Iterator[None]:
    """Эксклюзивная блокировка на время правки базы знаний.

    :raises KBLockTimeout: блокировку не удалось получить за ``timeout_s``.
    """
    if fcntl is None:  # pragma: no cover - платформа без fcntl
        _log.warning("kb_lock_unavailable", hint="fcntl отсутствует, запись не сериализуется")
        yield
        return

    directory = Path(kb_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LOCK_NAME
    deadline = time.monotonic() + max(0.0, timeout_s)

    with path.open("a+") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise KBLockTimeout(
                        "база знаний занята другой правкой — попробуйте ещё раз через минуту"
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
