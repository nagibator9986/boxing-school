"""Async-движок, фабрика сессий и транзакционный контекст.

Поддерживаются оба диалекта, между которыми живёт проект:

* ``postgresql+asyncpg`` — Railway (плагин Postgres), продовый режим;
* ``sqlite+aiosqlite`` — тесты и локальный запуск без Docker.

Различия диалектов спрятаны в :func:`build_engine`: параметры пула к SQLite не применимы,
in-memory SQLite требует ``StaticPool``, а внешние ключи в SQLite приходится включать
``PRAGMA foreign_keys=ON`` на каждом соединении.

Сеть на импорте модуля не трогается: движок создаётся лениво, первым обращением.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Sequence

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings
from app.storage.models import Base
from app.types import StorageError

__all__ = [
    "Base",
    "build_engine",
    "build_sessionmaker",
    "dialect_name",
    "dispose_engine",
    "get_engine",
    "get_sessionmaker",
    "insert_or_ignore",
    "is_postgres_session",
    "ping",
    "session_scope",
    "set_engine",
    "supports_skip_locked",
]

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

#: Сколько ждать свободное соединение. Умолчание SQLAlchemy (30 с) в этом проекте
#: вредно: отправка исходящего успевает уйти в ретрай раньше, чем дождётся пула.
POOL_TIMEOUT_S: int = 10

#: Запас соединений сверх числа параллельных задач: сметки, /readyz, админка.
POOL_HEADROOM: int = 2


def _fit_pool_to_workers(
    pool_size: int, max_overflow: int, settings: Settings | None
) -> tuple[int, int]:
    """Не даёт пулу оказаться уже, чем число одновременных задач воркера.

    ``_run_turn`` держит сессию открытой всё время хода, включая сетевые вызовы
    Gemini (десятки секунд). При ``worker_max_jobs=10`` и пуле 5+5 все соединения
    заняты ожиданием модели, а ``send_outbox_job``, ``outbox_sweep_cron`` и запись
    статусов встают в очередь за соединением и падают по таймауту — ровно в момент
    пиковой нагрузки. Поэтому ёмкость пула поднимается до
    ``worker_max_jobs + POOL_HEADROOM``; заданные вручную бОльшие значения не трогаем.
    """
    jobs = int(getattr(settings, "worker_max_jobs", 0) or 0)
    if jobs <= 0:
        return pool_size, max_overflow
    needed = jobs + POOL_HEADROOM
    if pool_size + max_overflow >= needed:
        return pool_size, max_overflow
    return pool_size, needed - pool_size


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _is_memory_sqlite(url: str) -> bool:
    return _is_sqlite(url) and (":memory:" in url or "mode=memory" in url)


def _settings_or_none() -> Settings | None:
    """Настройки, если их вообще удаётся собрать.

    Нужно только для ветки с явным ``url`` (тесты, миграции, разовые скрипты):
    подключиться к конкретной базе там можно и без полностью валидного окружения.
    На штатном пути ``url=None`` настройки обязательны, и ошибка конфигурации летит наружу.
    """
    try:
        return get_settings()
    except Exception:
        return None


def build_engine(url: str | None = None) -> AsyncEngine:
    """Создаёт async-движок. Сеть не трогает: подключение ленивое.

    ``url`` по умолчанию берётся из :func:`app.config.get_settings` (там же Railway-URL
    приводится к ``postgresql+asyncpg``). Параметры пула передаются только PostgreSQL —
    SQLite их либо не понимает, либо ломается на in-memory базе.
    """
    settings: Settings | None = _settings_or_none() if url else get_settings()
    dsn = url or (settings.database_url if settings else "")
    kwargs: dict[str, Any] = {"echo": bool(settings and settings.db_echo), "future": True}

    if _is_sqlite(dsn):
        # aiosqlite держит соединение в отдельном потоке; для in-memory базы пул обязан
        # быть общим, иначе каждая сессия получит собственную пустую БД.
        kwargs["connect_args"] = {"check_same_thread": False}
        if _is_memory_sqlite(dsn):
            kwargs["poolclass"] = StaticPool
    else:
        pool_size = settings.db_pool_size if settings else 5
        max_overflow = settings.db_max_overflow if settings else 5
        pool_size, max_overflow = _fit_pool_to_workers(pool_size, max_overflow, settings)
        kwargs.update(
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
            pool_recycle=1800,
            # Явно и коротко: ждать соединение полминуты бессмысленно — отправка
            # уже готового ответа за это время успевает уйти в ретрай.
            pool_timeout=POOL_TIMEOUT_S,
        )

    engine = create_async_engine(dsn, **kwargs)

    if _is_sqlite(dsn):
        @sa.event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:  # pragma: no cover
            """Прагмы SQLite: внешние ключи, WAL и ожидание блокировки.

            Внешние ключи в SQLite выключены по умолчанию — включаем.

            WAL и ``busy_timeout`` нужны с того момента, как в одну базу пишут два
            процесса: приём Wazzup в веб-службе и опрос Telegram рядом. В журнале
            по умолчанию писатель один, читатели его блокируют, и второй процесс
            получает «database is locked» ровно в момент ответа клиенту. В WAL
            читатели писателю не мешают, а таймаут даёт пережить короткое
            пересечение двух записей вместо немедленной ошибки.

            Для базы в памяти WAL не применяется — там журнала нет.
            """
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                if not _is_memory_sqlite(dsn):
                    cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()

    return engine


def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """``expire_on_commit=False`` — объекты живут после коммита."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


def get_engine() -> AsyncEngine:
    """Ленивый синглтон движка процесса."""
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Ленивый синглтон фабрики сессий."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = build_sessionmaker(get_engine())
    return _sessionmaker


def set_engine(engine: AsyncEngine | None) -> None:
    """Подменяет движок процесса (тесты, отдельный движок под миграции).

    ``None`` сбрасывает состояние: следующий вызов создаст движок заново.
    """
    global _engine, _sessionmaker
    _engine = engine
    _sessionmaker = build_sessionmaker(engine) if engine is not None else None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Транзакция: commit на выходе, rollback на исключении.

    Ошибки SQLAlchemy заворачиваются в :class:`app.types.StorageError` — наружу
    драйверные исключения не выходят (контракт исключений, INTERFACES §15).
    """
    session = get_sessionmaker()()
    try:
        yield session
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise StorageError(f"Сбой транзакции: {exc.__class__.__name__}: {exc}") from exc
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


# --------------------------------------------------------------------------- #
# Диалект-зависимые помощники (используются репозиториями)
# --------------------------------------------------------------------------- #
def dialect_name(session: AsyncSession) -> str:
    """Имя диалекта сессии: ``postgresql`` или ``sqlite``."""
    return session.get_bind().dialect.name


def is_postgres_session(session: AsyncSession) -> bool:
    """Идёт ли сессия в PostgreSQL (а не в SQLite тестов)."""
    return dialect_name(session) == "postgresql"


def supports_skip_locked(session: AsyncSession) -> bool:
    """Умеет ли БД ``SELECT ... FOR UPDATE SKIP LOCKED``. SQLite — нет."""
    return is_postgres_session(session)


def insert_or_ignore(
    session: AsyncSession, model: Any, values: dict[str, Any], *, index_elements: Sequence[str]
) -> Any:
    """``INSERT ... ON CONFLICT DO NOTHING`` в диалекте текущей сессии.

    Нужен там, где две параллельные обработки могут вставить одну и ту же строку
    (диалог, дедуп входящего, строка outbox). Savepoint'ы для этого не используются:
    на SQLite они работают ненадёжно из-за неявных транзакций драйвера.
    ``rowcount == 1`` означает «вставили мы», ``0`` — «строка уже была».
    """
    if is_postgres_session(session):
        from sqlalchemy.dialects.postgresql import insert as _pg_insert

        return _pg_insert(model).values(**values).on_conflict_do_nothing(
            index_elements=list(index_elements)
        )
    from sqlalchemy.dialects.sqlite import insert as _sqlite_insert

    return _sqlite_insert(model).values(**values).on_conflict_do_nothing(
        index_elements=list(index_elements)
    )


async def ping(session: AsyncSession) -> bool:
    """``SELECT 1`` для ``/readyz``. Никогда не кидает: недоступность БД — это ``False``."""
    try:
        result = await session.execute(sa.text("SELECT 1"))
        return result.scalar_one() == 1
    except Exception:
        return False


async def dispose_engine() -> None:
    """Закрывает пул. Вызывается в shutdown lifespan."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
