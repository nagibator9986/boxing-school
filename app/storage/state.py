"""TTL-состояния: дедуп, пауза бота, окно дебаунса, лок диалога, счётчики.

Всё, что живёт минуты-часы и не обязано пережить рестарт, лежит здесь, а не в БД.
Пространства ключей заморожены контрактом (INTERFACES §7.4) — строить их руками
не нужно, для каждого есть функция ``key_*``.

Две реализации:

* :class:`RedisStateStore` — прод (плагин Redis на Railway). Клиент создаётся лениво,
  на импорте модуля и в конструкторе сети нет.
* :class:`MemoryStateStore` — тесты и локальный запуск без Redis. Не переживает рестарт
  и не работает на два процесса; в проде запрещена.

Семантика :meth:`StateStore.ttl` повторяет Redis: ``-2`` — ключа нет, ``-1`` — ключ
без срока жизни, иначе секунды до истечения.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Final, Protocol
from uuid import uuid4

from app.config import Settings

__all__ = [
    "SqliteStateStore",
    "MemoryStateStore",
    "RedisStateStore",
    "StateStore",
    "build_state_store",
    "key_budget",
    "key_debounce",
    "key_dedup_message",
    "key_dedup_status",
    "key_lock_conversation",
    "key_pause",
    "key_rate",
]

#: Lua-скрипт снятия лока: удаляем ключ, только если он всё ещё наш.
_UNLOCK_LUA: Final[str] = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

#: Lua-скрипт инкремента: TTL ставится только при создании ключа.
_INCR_LUA: Final[str] = """
local value = redis.call('incr', KEYS[1])
if value == 1 then
    redis.call('expire', KEYS[1], ARGV[1])
end
return value
"""


# --------------------------------------------------------------------------- #
# Замороженные пространства ключей
# --------------------------------------------------------------------------- #
def key_dedup_message(message_id: str) -> str:
    """Дедуп входящего сообщения. TTL — ``dedup_ttl_seconds``."""
    return f"wz:msg:{message_id}"


def key_dedup_status(message_id: str, status: str) -> str:
    """Дедуп обновления статуса. TTL — ``dedup_ttl_seconds``."""
    return f"wz:st:{message_id}:{status}"


def key_lock_conversation(conv_key: str) -> str:
    """Лок диалога: одновременно обрабатывается один ход. TTL — ``conv_lock_ttl_seconds``."""
    return f"lock:conv:{conv_key}"


def key_debounce(conv_key: str) -> str:
    """Окно склейки подряд идущих сообщений. TTL — ``debounce_max_seconds``."""
    return f"deb:conv:{conv_key}"


def key_pause(conv_key: str) -> str:
    """Быстрый путь проверки паузы бота. TTL — до ``paused_until``."""
    return f"pause:{conv_key}"


def key_rate(conv_key: str) -> str:
    """Счётчик входящих в окне. TTL — ``rate_limit_window_seconds``."""
    return f"rate:conv:{conv_key}"


def key_budget(day: str) -> str:
    """Суточный расход на модель, микроцентами. ``day`` — ``YYYY-MM-DD``. TTL — 172800."""
    return f"budget:llm:{day}"


# --------------------------------------------------------------------------- #
# Протокол
# --------------------------------------------------------------------------- #
class StateStore(Protocol):
    """Минимальный набор операций поверх key-value с TTL."""

    async def set_if_absent(self, key: str, value: str, ttl_s: int) -> bool: ...

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_s: int) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def incr(self, key: str, ttl_s: int) -> int: ...

    async def ttl(self, key: str) -> int: ...

    @asynccontextmanager
    def lock(self, key: str, ttl_s: int) -> AsyncIterator[bool]: ...


# --------------------------------------------------------------------------- #
# Redis
# --------------------------------------------------------------------------- #
class RedisStateStore:
    """Реализация поверх Redis. Клиент создаётся при первом обращении, не в ``__init__``."""

    def __init__(self, url: str, *, socket_timeout_s: float = 3.0) -> None:
        self._url = url
        self._socket_timeout_s = socket_timeout_s
        self._client: Any | None = None
        self._unlock_sha: Any | None = None
        self._incr_sha: Any | None = None

    def _redis(self) -> Any:
        """Ленивый клиент. ``from_url`` соединение не открывает — это делает первая команда."""
        if self._client is None:
            from redis.asyncio import Redis  # локальный импорт: модуль импортируется без redis

            self._client = Redis.from_url(
                self._url,
                decode_responses=True,
                socket_timeout=self._socket_timeout_s,
                socket_connect_timeout=self._socket_timeout_s,
                health_check_interval=30,
            )
            self._unlock_sha = self._client.register_script(_UNLOCK_LUA)
            self._incr_sha = self._client.register_script(_INCR_LUA)
        return self._client

    async def set_if_absent(self, key: str, value: str, ttl_s: int) -> bool:
        """``SET key value NX EX ttl``. ``True`` — ключа не было и он поставлен нами."""
        result = await self._redis().set(key, value, nx=True, ex=max(1, ttl_s))
        return bool(result)

    async def get(self, key: str) -> str | None:
        """Значение ключа или ``None``."""
        return await self._redis().get(key)

    async def set(self, key: str, value: str, ttl_s: int) -> None:
        """Безусловная запись со сроком жизни."""
        await self._redis().set(key, value, ex=max(1, ttl_s))

    async def delete(self, key: str) -> None:
        """Удаляет ключ; отсутствие ключа — не ошибка."""
        await self._redis().delete(key)

    async def incr(self, key: str, ttl_s: int) -> int:
        """Атомарный инкремент; TTL ставится только при создании ключа."""
        self._redis()  # инициализация скриптов
        assert self._incr_sha is not None
        value = await self._incr_sha(keys=[key], args=[max(1, ttl_s)])
        return int(value)

    async def ttl(self, key: str) -> int:
        """Секунды до истечения; ``-2`` — ключа нет, ``-1`` — без срока жизни."""
        return int(await self._redis().ttl(key))

    @asynccontextmanager
    async def lock(self, key: str, ttl_s: int) -> AsyncIterator[bool]:
        """Неблокирующий лок. Отдаёт ``True``, если захватили; снимает только свой ключ."""
        token = uuid.uuid4().hex
        acquired = await self.set_if_absent(key, token, ttl_s)
        try:
            yield acquired
        finally:
            if acquired:
                self._redis()
                assert self._unlock_sha is not None
                try:
                    await self._unlock_sha(keys=[key], args=[token])
                except Exception:  # pragma: no cover - лок всё равно истечёт по TTL
                    pass

    async def close(self) -> None:
        """Закрывает соединения. Вызывается в shutdown lifespan."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._unlock_sha = None
            self._incr_sha = None


# --------------------------------------------------------------------------- #
# Память
# --------------------------------------------------------------------------- #
class MemoryStateStore:
    """Только тесты и локальный запуск. Не переживает рестарт, не работает на 2 процесса."""

    def __init__(self) -> None:
        #: ключ -> (значение, момент истечения по monotonic или None)
        self._data: dict[str, tuple[str, float | None]] = {}
        self._guard = asyncio.Lock()

    def _expired(self, key: str) -> bool:
        item = self._data.get(key)
        if item is None:
            return True
        _, expires_at = item
        if expires_at is not None and expires_at <= time.monotonic():
            self._data.pop(key, None)
            return True
        return False

    async def set_if_absent(self, key: str, value: str, ttl_s: int) -> bool:
        """Ставит ключ, только если его нет (или он уже протух)."""
        async with self._guard:
            if not self._expired(key):
                return False
            self._data[key] = (value, time.monotonic() + max(1, ttl_s))
            return True

    async def get(self, key: str) -> str | None:
        """Значение ключа или ``None``."""
        async with self._guard:
            if self._expired(key):
                return None
            return self._data[key][0]

    async def set(self, key: str, value: str, ttl_s: int) -> None:
        """Безусловная запись со сроком жизни."""
        async with self._guard:
            self._data[key] = (value, time.monotonic() + max(1, ttl_s))

    async def delete(self, key: str) -> None:
        """Удаляет ключ; отсутствие ключа — не ошибка."""
        async with self._guard:
            self._data.pop(key, None)

    async def incr(self, key: str, ttl_s: int) -> int:
        """Инкремент; TTL ставится только при создании ключа."""
        async with self._guard:
            if self._expired(key):
                self._data[key] = ("1", time.monotonic() + max(1, ttl_s))
                return 1
            value, expires_at = self._data[key]
            new_value = int(value) + 1
            self._data[key] = (str(new_value), expires_at)
            return new_value

    async def ttl(self, key: str) -> int:
        """Секунды до истечения; ``-2`` — ключа нет, ``-1`` — без срока жизни."""
        async with self._guard:
            if self._expired(key):
                return -2
            expires_at = self._data[key][1]
            if expires_at is None:
                return -1
            return max(0, int(round(expires_at - time.monotonic())))

    @asynccontextmanager
    async def lock(self, key: str, ttl_s: int) -> AsyncIterator[bool]:
        """Неблокирующий лок с той же семантикой, что и в Redis."""
        token = uuid.uuid4().hex
        acquired = await self.set_if_absent(key, token, ttl_s)
        try:
            yield acquired
        finally:
            if acquired:
                async with self._guard:
                    item = self._data.get(key)
                    if item is not None and item[0] == token:
                        self._data.pop(key, None)

    async def close(self) -> None:
        """Совместимость с :class:`RedisStateStore`: чистит память."""
        async with self._guard:
            self._data.clear()

    def clear(self) -> None:
        """Синхронный сброс между тестами."""
        self._data.clear()


def build_state_store(settings: Settings) -> StateStore:
    """Выбирает бэкенд состояния. По умолчанию — SQLite.

    ``sqlite`` — рабочий режим: состояние переживает перезапуск, внешних служб
    не нужно. Пауза бота и дедуп обязаны жить дольше процесса: иначе рестарт
    снимает паузу посреди разговора с оператором и открывает дорогу повторным
    ответам на уже обработанные сообщения.

    ``memory`` остаётся для тестов, ``redis`` — на случай, если однажды
    понадобится несколько процессов; при пустом ``redis_url`` он молча
    вырождается в SQLite, а не роняет запуск.
    """
    if settings.state_backend == "memory":
        return MemoryStateStore()
    if settings.state_backend == "redis" and settings.redis_url:
        return RedisStateStore(settings.redis_url)
    return SqliteStateStore(settings.state_sqlite_path)


# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #
class SqliteStateStore:
    """TTL-состояние в файле SQLite: замена Redis для одного процесса.

    Здесь живут дедуп входящих, пауза бота, окно склейки сообщений и лок диалога.
    Память для этого не годится: перезапуск процесса снимал бы паузу бота посреди
    разговора с оператором и открывал бы дорогу повторным ответам на уже
    обработанные сообщения.

    Соединение отдельное от основного движка приложения и синхронное: операции
    здесь короткие и частые, а асинхронная обёртка вокруг такой мелочи стоила бы
    дороже самой работы. Доступ сериализуется одним asyncio-локом — процесс один,
    конкуренции между машинами нет по построению.

    ``busy_timeout`` и WAL включены, чтобы редкие параллельные записи (воркер и
    админка) не роняли друг друга ошибкой «database is locked».
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_obj = asyncio.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kv_state ("
            " key TEXT PRIMARY KEY,"
            " value TEXT NOT NULL,"
            " expires_at REAL NOT NULL)"
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS ix_kv_expires ON kv_state(expires_at)")
        self._conn.commit()

    # ------------------------------------------------------------- служебное
    def _purge(self, now: float) -> None:
        """Убирает протухшие ключи. Дешевле, чем проверять срок при каждом чтении."""
        self._conn.execute("DELETE FROM kv_state WHERE expires_at <= ?", (now,))

    def _alive(self, key: str, now: float) -> tuple[str, float] | None:
        row = self._conn.execute(
            "SELECT value, expires_at FROM kv_state WHERE key = ? AND expires_at > ?",
            (key, now),
        ).fetchone()
        return (row[0], row[1]) if row else None

    # -------------------------------------------------------------- операции
    async def set_if_absent(self, key: str, value: str, ttl_s: int) -> bool:
        """Атомарная установка «если ключа нет» — основа дедупа и лока диалога."""
        async with self._lock_obj:
            now = time.time()
            self._purge(now)
            try:
                self._conn.execute(
                    "INSERT INTO kv_state(key, value, expires_at) VALUES (?, ?, ?)",
                    (key, value, now + max(1, ttl_s)),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                # Ключ уже есть и не протух: значит эту работу кто-то уже взял.
                self._conn.rollback()
                return False

    async def get(self, key: str) -> str | None:
        """Значение ключа или ``None``, если его нет либо срок вышел."""
        async with self._lock_obj:
            found = self._alive(key, time.time())
            return found[0] if found else None

    async def set(self, key: str, value: str, ttl_s: int) -> None:
        """Устанавливает значение, перезаписывая прежнее."""
        async with self._lock_obj:
            now = time.time()
            self._conn.execute(
                "INSERT INTO kv_state(key, value, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "expires_at = excluded.expires_at",
                (key, value, now + max(1, ttl_s)),
            )
            self._conn.commit()

    async def delete(self, key: str) -> None:
        """Удаляет ключ. Отсутствие ключа ошибкой не является."""
        async with self._lock_obj:
            self._conn.execute("DELETE FROM kv_state WHERE key = ?", (key,))
            self._conn.commit()

    async def incr(self, key: str, ttl_s: int) -> int:
        """Счётчик с TTL: используется для ограничения частоты сообщений."""
        async with self._lock_obj:
            now = time.time()
            found = self._alive(key, now)
            if found is None:
                self._conn.execute(
                    "INSERT INTO kv_state(key, value, expires_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "expires_at = excluded.expires_at",
                    (key, "1", now + max(1, ttl_s)),
                )
                self._conn.commit()
                return 1
            value = int(found[0]) + 1
            # Срок НЕ продлеваем: окно должно быть скользящим от первого события,
            # иначе активный спамер держал бы счётчик живым бесконечно.
            self._conn.execute(
                "UPDATE kv_state SET value = ? WHERE key = ?", (str(value), key)
            )
            self._conn.commit()
            return value

    async def ttl(self, key: str) -> int:
        """Сколько секунд осталось жить ключу; ``-1`` — ключа нет."""
        async with self._lock_obj:
            found = self._alive(key, time.time())
            return int(found[1] - time.time()) if found else -1

    @asynccontextmanager
    async def lock(self, key: str, ttl_s: int) -> AsyncIterator[bool]:
        """Взаимное исключение поверх ``set_if_absent``: снимаем только свой лок."""
        token = uuid4().hex
        acquired = await self.set_if_absent(key, token, ttl_s)
        try:
            yield acquired
        finally:
            if acquired:
                async with self._lock_obj:
                    row = self._conn.execute(
                        "SELECT value FROM kv_state WHERE key = ?", (key,)
                    ).fetchone()
                    if row and row[0] == token:
                        self._conn.execute("DELETE FROM kv_state WHERE key = ?", (key,))
                        self._conn.commit()

    async def aclose(self) -> None:
        """Закрывает соединение. Повторный вызов безопасен."""
        async with self._lock_obj:
            self._conn.close()
