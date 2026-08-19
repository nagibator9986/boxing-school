"""Окружение Alembic на async-движке.

``psycopg2`` в зависимости проекта не входит (INTERFACES §18 п.10), поэтому синхронного
подключения здесь нет вообще: миграции идут через ``AsyncEngine`` и
``connection.run_sync(context.run_migrations)``.

URL берётся в таком порядке:

1. ``alembic -x db_url=...`` — разовый прогон (тесты, восстановление, локальная проверка);
2. ``sqlalchemy.url`` из ``alembic.ini`` — если кто-то всё же его заполнил;
3. ``app.config.get_settings().database_url`` — штатный путь, в том числе на Railway,
   где URL приходит переменной ``DATABASE_URL`` и нормализуется в ``app/config.py``.

На Railway ``alembic upgrade head`` выполняется при каждом деплое, поэтому повторный
прогон на уже накатанной базе обязан быть безобидным — см. ревизию ``0001_initial``.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.storage.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: Метаданные всех моделей проекта — источник правды для autogenerate.
target_metadata = Base.metadata


def _resolve_url() -> str:
    """Определяет URL подключения по приоритету, описанному в docstring модуля."""
    x_args = context.get_x_argument(as_dictionary=True)
    url = x_args.get("db_url") or x_args.get("sqlalchemy.url")
    if url:
        return url

    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url:
        return ini_url

    from app.config import get_settings  # локальный импорт: конфиг нужен только здесь

    return get_settings().database_url


def _configure(connection: Connection) -> None:
    """Общие параметры контекста для обоих режимов."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # SQLite не умеет ALTER COLUMN — будущие миграции пойдут через batch.
        render_as_batch=connection.dialect.name == "sqlite",
    )


def run_migrations_offline() -> None:
    """Режим ``--sql``: генерирует DDL без подключения к базе."""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    """Синхронная часть, выполняемая внутри ``run_sync``."""
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Штатный режим: async-подключение, миграции внутри ``run_sync``."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
