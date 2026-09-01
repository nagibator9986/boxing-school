"""Ответ клиенту из CRM: строка в очередь, пауза боту, правила каналов.

CRM показывала переписку и эскалацию, но вступить в разговор не давала —
владелец шёл отвечать в WhatsApp с телефона. Здесь проверяется, что строка,
созданная интерфейсом, ничем не отличается от строки самого бота: её забирает
настоящая выборка отправщика и разбирает настоящая модель исходящего.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.storage import repo_outbox
from app.storage.db import build_engine, build_sessionmaker
from app.storage.models import Base
from app.types import OutboundMessage
from crm.botdb import BotData

UTC = timezone.utc


def _conversation(conn: sa.Connection, *, conv_id: str, channel: str, last_inbound: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO conversation (id, conv_key, channel_id, chat_type, chat_id,"
            " contact_name, lang, lang_locked, state, msg_in_count, msg_out_count,"
            " bot_miss_count, followup_stage, followup_blocked, created_at, updated_at,"
            " first_inbound_at, last_inbound_at)"
            " VALUES (:id, :key, 'ch-1', :channel, '77015550001', 'Гульнара', 'ru', 0, 'new',"
            " 1, 0, 0, 0, 0, :now, :now, :now, :last)"
        ),
        {
            "id": conv_id,
            "key": f"ch-1:{channel}:77015550001",
            "channel": channel,
            "now": "2026-09-01 10:00:00",
            "last": last_inbound,
        },
    )


#: Идентификаторы диалогов — 32 шестнадцатеричных знака, как их пишет
#: SQLAlchemy: с короткими «wa»/«ig» ORM не смогла бы прочитать строку.
CONV: dict[str, str] = {name: uuid4().hex for name in ("wa", "ig", "tg")}


@pytest.fixture
def bot_db(tmp_path: Path) -> Path:
    """База с тремя диалогами: свежий WhatsApp, протухший Instagram, Telegram."""
    path = tmp_path / "bot.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    fresh = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
    stale = (datetime.now(tz=UTC) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    with engine.begin() as conn:
        _conversation(conn, conv_id=CONV["wa"], channel="whatsapp", last_inbound=fresh)
        _conversation(conn, conv_id=CONV["ig"], channel="instagram", last_inbound=stale)
        _conversation(conn, conv_id=CONV["tg"], channel="telegram", last_inbound=fresh)
    engine.dispose()
    return path


def rows(bot_db: Path) -> list[dict]:
    """Строки очереди отправки, как их увидит отправщик."""
    engine = sa.create_engine(f"sqlite:///{bot_db}")
    with engine.begin() as conn:
        found = conn.execute(
            sa.text("SELECT payload, state, conversation_id FROM outbox_message ORDER BY created_at")
        ).fetchall()
    engine.dispose()
    return [
        {"payload": json.loads(row[0]), "state": row[1], "conversation_id": row[2]}
        for row in found
    ]


# --------------------------------------------------------------------------- #
# Отправка
# --------------------------------------------------------------------------- #
def test_reply_lands_in_the_outbox(bot_db: Path) -> None:
    """Сообщение оператора попадает в очередь с признаками своего канала."""
    assert BotData(bot_db).reply_to_client(CONV["wa"], "Здравствуйте! Записали вас на среду.") is None

    queued = rows(bot_db)
    assert len(queued) == 1
    payload = queued[0]["payload"]
    assert queued[0]["state"] == "pending"
    assert payload["channel"] == "whatsapp"
    assert payload["chat_id"] == "77015550001"
    assert payload["kind"] == "operator_reply"
    assert payload["text"] == "Здравствуйте! Записали вас на среду."


async def test_the_sender_picks_up_what_crm_wrote(bot_db: Path) -> None:
    """Главная проверка: строку CRM забирает настоящая выборка отправщика.

    Если формат полезной нагрузки разойдётся с моделью исходящего хоть в одном
    поле, оператор будет писать в пустоту: строка ляжет в базу и не уйдёт
    никуда.
    """
    assert BotData(bot_db).reply_to_client(CONV["wa"], "Мы вас ждём в 17:30.") is None

    engine = build_engine(f"sqlite+aiosqlite:///{bot_db}")
    try:
        async with build_sessionmaker(engine)() as session:
            due = await repo_outbox.due(session, datetime.now(tz=UTC))
            assert len(due) == 1, "отправщик не увидел строку, созданную CRM"
            row = await repo_outbox.get(session, due[0])
    finally:
        await engine.dispose()

    message = OutboundMessage.model_validate(row.payload)
    assert message.text == "Мы вас ждём в 17:30."
    assert message.channel.value == "whatsapp"


def test_long_reply_is_split_into_messages(bot_db: Path) -> None:
    """Длинный ответ режется теми же правилами, что и ответ бота."""
    long_text = " ".join(["Одно предложение про тренировки." for _ in range(120)])
    assert BotData(bot_db).reply_to_client(CONV["wa"], long_text) is None

    queued = rows(bot_db)
    assert len(queued) > 1
    assert all(len(item["payload"]["text"]) <= 1000 for item in queued)
    # Части идут по порядку и с нарастающей задержкой — иначе клиент получит
    # их вперемешку.
    delays = [item["payload"]["delay_ms"] for item in queued]
    assert delays == sorted(delays) and delays[0] == 0


# --------------------------------------------------------------------------- #
# Куда писать нельзя
# --------------------------------------------------------------------------- #
def test_expired_instagram_window_is_refused_with_a_reason(bot_db: Path) -> None:
    """Молчаливый отказ хуже всего: оператор считал бы, что ответил."""
    denial = BotData(bot_db).reply_to_client(CONV["ig"], "Здравствуйте!")

    assert denial is not None
    assert "Instagram" in denial
    assert rows(bot_db) == []


def test_telegram_is_refused(bot_db: Path) -> None:
    """Telegram-бот очередь не читает — строка осталась бы в базе навсегда."""
    denial = BotData(bot_db).reply_to_client(CONV["tg"], "Здравствуйте!")

    assert denial is not None
    assert rows(bot_db) == []


def test_empty_text_is_refused(bot_db: Path) -> None:
    """Пустая отправка — промах по кнопке, а не сообщение."""
    assert BotData(bot_db).reply_to_client(CONV["wa"], "   ") is not None
    assert rows(bot_db) == []


def test_unknown_conversation_is_refused(bot_db: Path) -> None:
    """Чужой идентификатор в адресе не должен создавать строку в никуда."""
    assert BotData(bot_db).reply_to_client("нет-такого", "Привет") is not None
    assert rows(bot_db) == []


def test_reply_shows_up_in_the_dialogue(bot_db: Path) -> None:
    """Отправленное видно в переписке — иначе непонятно, ушло оно или нет."""
    bot = BotData(bot_db)
    assert bot.reply_to_client(CONV["wa"], "Ждём вас в среду.") is None

    texts = [item.text for item in bot.dialog(CONV["wa"])]
    assert "Ждём вас в среду." in texts
