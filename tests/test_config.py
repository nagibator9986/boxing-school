"""Нормализация ``DATABASE_URL`` и блокеры старта.

Плагин Postgres на Railway отдаёт ``postgresql://…`` (иногда legacy
``postgres://…``) и добавляет ``?sslmode=require``, которого asyncpg не понимает
и на котором соединение падает. Заказчик обязан иметь возможность вставить
переменную Railway как есть — значит, нормализуем сами.

Таблица истинности — ``docs/INTERFACES.md`` §4.1 (строки 1326-1341).
"""

from __future__ import annotations

import pytest

from app.config import Settings, normalize_database_url


# --------------------------------------------------------------------------- #
# Таблица истинности INTERFACES §4.1
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql://u:p@h/db?sslmode=require", "postgresql+asyncpg://u:p@h/db"),
        ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("sqlite:///./data/bot.db", "sqlite+aiosqlite:///./data/bot.db"),
        ("sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite:///:memory:"),
        ("", ""),
    ],
)
def test_normalize_database_url_truth_table(raw, expected) -> None:
    """Каждая строка таблицы контракта проверяется дословно."""
    assert normalize_database_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql://u:p@h/db?sslmode=require",
        "postgresql://u:p@h/db?channel_binding=require",
        "postgresql://u:p@h/db?target_session_attrs=read-write",
        "postgresql://u:p@h/db?gssencmode=disable",
        "postgres://u:p@h/db?sslmode=require&channel_binding=require",
    ],
)
def test_asyncpg_incompatible_params_are_dropped(raw) -> None:
    """Эти параметры asyncpg не принимает — соединение падало бы на старте."""
    result = normalize_database_url(raw)

    assert result.startswith("postgresql+asyncpg://")
    for name in ("sslmode", "channel_binding", "target_session_attrs", "gssencmode"):
        assert name not in result


def test_unknown_query_params_are_preserved() -> None:
    """Всё, что asyncpg понимает, обязано пережить нормализацию."""
    result = normalize_database_url("postgresql://u:p@h/db?sslmode=require&application_name=bot")

    assert "application_name=bot" in result
    assert "sslmode" not in result


@pytest.mark.parametrize(
    "password",
    ["p@ssw0rd", "pass/word", "p@ss/w:rd", "postgres://", "a@b@c"],
)
def test_password_with_special_characters_survives(password) -> None:
    """Пароль с ``@`` и ``/`` обязан пережить нормализацию.

    Именно поэтому подмена идёт строго по префиксу строки, а не регуляркой по
    всему URL: пароль Railway генерирует случайно, и в нём бывает что угодно.
    """
    raw = f"postgres://user:{password}@host:5432/railway"

    result = normalize_database_url(raw)

    assert result == f"postgresql+asyncpg://user:{password}@host:5432/railway"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sqlite://", "sqlite+aiosqlite://"),
        ("sqlite:///:memory:", "sqlite+aiosqlite:///:memory:"),
        ("sqlite+pysqlite:///./bot.db", "sqlite+pysqlite:///./bot.db"),
        ("mysql://u:p@h/db", "mysql://u:p@h/db"),  # чужой диалект не трогаем
        ("   postgres://u:p@h/db   ", "postgresql+asyncpg://u:p@h/db"),
    ],
)
def test_other_shapes(raw, expected) -> None:
    """Уже нормализованные и чужие URL не переписываются."""
    assert normalize_database_url(raw) == expected


def test_settings_normalizes_url_on_creation() -> None:
    """Нормализация обязана происходить в самих настройках, а не у вызывающего."""
    settings = Settings(database_url="postgres://u:p@h:5432/db?sslmode=require")

    assert settings.database_url == "postgresql+asyncpg://u:p@h:5432/db"
    assert settings.is_postgres is True
    assert settings.is_sqlite is False


def test_settings_detect_sqlite() -> None:
    """Тесты и локальный пилот идут на sqlite."""
    settings = Settings(database_url="sqlite:///./data/bot.db")

    assert settings.is_sqlite is True
    assert settings.is_postgres is False


# --------------------------------------------------------------------------- #
# Модуль обязан импортироваться без ключей
# --------------------------------------------------------------------------- #
def test_settings_build_with_empty_environment(monkeypatch) -> None:
    """Пустое окружение не должно ронять создание настроек — только блокеры старта."""
    for name in (
        "WAZZUP_API_KEY",
        "GEMINI_API_KEY",
        "WAZZUP_WEBHOOK_SECRET",
        "DATABASE_URL",
        "REDIS_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.app_env == "local"
    assert settings.gemini_api_key == ""


# --------------------------------------------------------------------------- #
# Блокеры старта
# --------------------------------------------------------------------------- #
def test_local_env_has_no_key_blockers() -> None:
    """Локально бот поднимается без ключей — иначе разработку не начать."""
    settings = Settings(app_env="local", database_url="sqlite:///./bot.db", state_backend="memory")

    assert settings.startup_blockers() == []


def test_prod_env_requires_keys_and_https() -> None:
    """Прод без ключей, без https и без адресата лидов стартовать не имеет права."""
    settings = Settings(
        app_env="prod",
        database_url="postgres://u:p@h/db",
        redis_url="redis://h:6379/0",
        state_backend="redis",
        wazzup_api_key="",
        wazzup_webhook_secret="short",
        gemini_api_key="",
        public_base_url="http://example.com",
        manager_notify_target=None,
    )

    blockers = settings.startup_blockers()

    assert any("WAZZUP_API_KEY" in item for item in blockers)
    assert any("WAZZUP_WEBHOOK_SECRET" in item for item in blockers)
    assert any("GEMINI_API_KEY" in item for item in blockers)
    assert any("https" in item for item in blockers)
    assert any("MANAGER_NOTIFY_TARGET" in item for item in blockers)


def test_redis_backend_without_url_is_a_blocker() -> None:
    """``STATE_BACKEND=redis`` при пустом ``REDIS_URL`` — тихая потеря дедупликации."""
    settings = Settings(
        app_env="local",
        database_url="sqlite:///./bot.db",
        state_backend="redis",
        redis_url="",
    )

    assert any("REDIS_URL" in item for item in settings.startup_blockers())


def test_webhook_path_and_url_use_secret() -> None:
    """Секрет живёт в пути вебхука — сравнение потом идёт constant-time."""
    settings = Settings(
        wazzup_webhook_secret="s" * 32, public_base_url="https://bot.up.railway.app/"
    )

    assert settings.webhook_path() == "/wazzup/webhook/" + "s" * 32
    assert settings.webhook_url() == "https://bot.up.railway.app/wazzup/webhook/" + "s" * 32


def test_media_signing_key_falls_back_to_webhook_secret() -> None:
    """Отдельный ключ подписи медиа не задан — берётся секрет вебхука."""
    settings = Settings(wazzup_webhook_secret="w" * 32, media_token_secret="")

    assert settings.media_signing_key == "w" * 32


def test_railway_port_variable_wins() -> None:
    """Railway передаёт порт только через ``PORT``; захардкоженный порт — провал деплоя."""
    settings = Settings(PORT=12345)

    assert settings.port == 12345
