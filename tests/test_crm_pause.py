"""Пауза, поставленная из CRM, обязана глушить бота целиком.

Владелец: «если я уже отвечаю клиенту, чтобы бот не писал параллельно». Один
путь остался открытым: напоминания. Они смотрят на состояние диалога, а CRM
писала только строку паузы — и запланированное напоминание уходило клиенту
поверх начатого человеком разговора.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.config import get_settings
from app.storage.db import build_engine, build_sessionmaker
from app.storage.models import Base, Conversation, FollowupTask
from app.types import FollowupKind
from app.workers.tasks_followup import _skip_reason
from crm.botdb import BotData

UTC = timezone.utc

#: Полдень в Костанае: тихие часы напоминаний — с 21:00 до 9:00, и при
#: настоящем «сейчас» тест зависел бы от времени суток на машине.
NOON = datetime(2026, 9, 2, 7, 0, tzinfo=UTC)

CONV_ID = uuid4().hex
CONV_KEY = "ch-1:whatsapp:77015550001"


@pytest.fixture
def bot_db(tmp_path: Path) -> Path:
    """Диалог с назначенным напоминанием, которому пора уходить."""
    path = tmp_path / "bot.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    now = (NOON - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversation (id, conv_key, channel_id, chat_type, chat_id,"
                " contact_name, lang, lang_locked, state, msg_in_count, msg_out_count,"
                " bot_miss_count, followup_stage, followup_blocked, created_at, updated_at,"
                " first_inbound_at, last_inbound_at)"
                " VALUES (:id, :key, 'ch-1', 'whatsapp', '77015550001', 'Гульнара', 'ru', 0,"
                " 'active', 2, 2, 0, 0, 0, :now, :now, :now, :now)"
            ),
            {"id": CONV_ID, "key": CONV_KEY, "now": now},
        )
        conn.execute(
            sa.text(
                "INSERT INTO followup_task (id, conversation_id, kind, run_at, state, attempt,"
                " created_at) VALUES (:id, :conv, 'fu_value', :now, 'pending', 0, :now)"
            ),
            {"id": uuid4().hex, "conv": CONV_ID, "now": now},
        )
    engine.dispose()
    return path


async def skip_reason(bot_db: Path) -> str | None:
    """Уйдёт ли напоминание клиенту прямо сейчас. ``None`` — уйдёт."""
    engine = build_engine(f"sqlite+aiosqlite:///{bot_db}")
    try:
        async with build_sessionmaker(engine)() as session:
            conv = (await session.execute(sa.select(Conversation))).scalars().one()
            task = (await session.execute(sa.select(FollowupTask))).scalars().one()
            return await _skip_reason(
                session,
                conv,
                task,
                kind=FollowupKind.FU_VALUE,
                settings=get_settings(),
                now=NOON,
            )
    finally:
        await engine.dispose()


async def test_followup_would_be_sent_without_a_pause(bot_db: Path) -> None:
    """Опора теста: без паузы напоминание действительно уходит."""
    assert await skip_reason(bot_db) is None


async def test_pause_from_crm_silences_followups(bot_db: Path) -> None:
    """Человек взял диалог на себя — напоминание бота уходить не имеет права.

    CRM писала только строку паузы, а напоминания смотрят на состояние диалога.
    Клиент, которому отвечал администратор, получал поверх этого «ещё думаете?»
    от бота.
    """
    assert BotData(bot_db).pause_bot(CONV_ID, CONV_KEY, minutes=120) is True

    assert await skip_reason(bot_db) == "operator"


async def test_resume_from_crm_returns_the_bot(bot_db: Path) -> None:
    """Сняли паузу — бот снова работает в этом диалоге, включая напоминания."""
    bot = BotData(bot_db)
    bot.pause_bot(CONV_ID, CONV_KEY, minutes=120)

    assert bot.resume_bot(CONV_ID, CONV_KEY) is True
    assert await skip_reason(bot_db) is None


async def test_reply_from_crm_silences_followups(bot_db: Path) -> None:
    """То же для ответа клиенту: он тоже означает, что дальше отвечает человек."""
    bot = BotData(bot_db)
    assert bot.reply_to_client(CONV_ID, "Здравствуйте! Ждём вас в среду.") is None
    bot.pause_bot(CONV_ID, CONV_KEY, minutes=120)

    assert await skip_reason(bot_db) == "operator"


async def test_resume_returns_an_escalated_dialog_to_work(bot_db: Path) -> None:
    """Снятие паузы возвращает в работу и диалог, который эскалировал сам бот.

    Сам бот при снятии паузы ставит состояние «в работе» всегда, а CRM
    возвращала только из «отвечает человек». Диалог, переданный администратору
    ботом, оставался «эскалированным» навсегда — и напоминания по нему не
    уходили уже никогда.
    """
    engine = sa.create_engine(f"sqlite:///{bot_db}")
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE conversation SET state = 'escalated'"))
    engine.dispose()

    assert BotData(bot_db).resume_bot(CONV_ID, CONV_KEY) is True

    assert await skip_reason(bot_db) is None
