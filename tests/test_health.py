"""Неисправности службы видны владельцу, а не только журналу Railway.

Каждая проверка отвечает на вопрос, который владелец уже задавал вслух:
«почему бот пишет про сбой», «почему во вкладку заявки ничего не приходит»,
«откуда администратору отвечать». Общее у поломок одно — снаружи служба
выглядела исправной.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.storage.models import Base
from crm.botdb import BotData
from crm.health import collect_issues

UTC = timezone.utc


class FakeSettings:
    """Настройки процесса в том объёме, в каком их читает проверка."""

    def __init__(self, **overrides: str) -> None:
        self.gemini_api_key = overrides.get("gemini_api_key", "ключ")
        self.manager_notify_target = overrides.get("manager_notify_target", "77010000000")
        self.wazzup_api_key = overrides.get("wazzup_api_key", "ключ")


@pytest.fixture
def bot_db(tmp_path: Path) -> Path:
    """Пустая, но настоящая база бота."""
    path = tmp_path / "bot.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


def add_outbox(bot_db: Path, *, state: str, age_minutes: int, error: str | None = None) -> None:
    """Строка очереди отправки заданного возраста и состояния."""
    stamp = (datetime.now(tz=UTC) - timedelta(minutes=age_minutes)).strftime("%Y-%m-%d %H:%M:%S.%f")
    engine = sa.create_engine(f"sqlite:///{bot_db}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO outbox_message (id, crm_message_id, payload, state, attempts,"
                " created_at, updated_at, last_error)"
                " VALUES (:id, :crm, '{}', :state, 0, :stamp, :stamp, :error)"
            ),
            {
                "id": uuid4().hex,
                "crm": uuid4().hex,
                "state": state,
                "stamp": stamp,
                "error": error,
            },
        )
    engine.dispose()


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #
def test_healthy_setup_reports_nothing(bot_db: Path) -> None:
    """Исправная служба обязана молчать — иначе панель перестанут читать."""
    assert collect_issues(BotData(bot_db), FakeSettings()) == []


def test_missing_gemini_key_is_reported(bot_db: Path) -> None:
    """Без ключа бот отвечает только карточками — это должно быть на экране."""
    issues = collect_issues(BotData(bot_db), FakeSettings(gemini_api_key=""))

    assert [issue.level for issue in issues] == ["error"]
    assert "GEMINI_API_KEY" in issues[0].detail


def test_missing_manager_target_is_reported(bot_db: Path) -> None:
    """Карточки эскалации уходят в никуда, а клиент остаётся без ответа."""
    issues = collect_issues(BotData(bot_db), FakeSettings(manager_notify_target=""))

    assert any("MANAGER_NOTIFY_TARGET" in issue.detail for issue in issues)


def test_missing_wazzup_key_is_reported(bot_db: Path) -> None:
    """Без ключа канала не работают ни приём, ни отправка."""
    issues = collect_issues(BotData(bot_db), FakeSettings(wazzup_api_key=""))

    assert any("WAZZUP_API_KEY" in issue.detail for issue in issues)


# --------------------------------------------------------------------------- #
# Данные: очередь честнее настроек
# --------------------------------------------------------------------------- #
def test_stuck_queue_is_an_error(bot_db: Path) -> None:
    """Настройки могут выглядеть верными, а сообщения — не уходить.

    Отозванный ключ, не тот номер канала, выключенный отправщик: причина разная,
    признак один — очередь стоит.
    """
    add_outbox(bot_db, state="pending", age_minutes=45)
    add_outbox(bot_db, state="sending", age_minutes=30)

    issues = collect_issues(BotData(bot_db), FakeSettings())

    assert issues and issues[0].is_error
    assert "Не отправлено сообщений: 2" in issues[0].title


def test_fresh_queue_is_not_an_error(bot_db: Path) -> None:
    """Только что поставленная строка — норма: отправщик подметает раз в минуту."""
    add_outbox(bot_db, state="pending", age_minutes=0)

    assert collect_issues(BotData(bot_db), FakeSettings()) == []


def test_failed_deliveries_are_shown_with_the_reason(bot_db: Path) -> None:
    """Отказ канала обязан быть виден вместе с причиной, а не одним числом."""
    add_outbox(bot_db, state="failed", age_minutes=60, error="INVALID_APIKEY")

    issues = collect_issues(BotData(bot_db), FakeSettings())

    assert len(issues) == 1
    assert issues[0].level == "warn"
    assert "INVALID_APIKEY" in issues[0].detail


def test_errors_come_before_warnings(bot_db: Path) -> None:
    """Сначала то, из-за чего бот не работает, потом то, что работает хуже."""
    add_outbox(bot_db, state="failed", age_minutes=60, error="INVALID_APIKEY")
    add_outbox(bot_db, state="pending", age_minutes=45)

    levels = [issue.level for issue in collect_issues(BotData(bot_db), FakeSettings())]

    assert levels == ["error", "warn"]


def test_missing_database_does_not_break_the_overview(tmp_path: Path) -> None:
    """Базы ещё нет — обзор обязан открыться, а не упасть."""
    issues = collect_issues(BotData(tmp_path / "нет.db"), FakeSettings())

    assert issues == []
