"""Разовые правки данных в файле SQLite, которые ``create_all`` сделать не может.

Схему на Railway создаёт ``create_all`` (см. ``scripts/serve.py``), alembic там не
запускается — поэтому правка уже лежащих строк живёт здесь. Номер последней
выполненной правки хранится в ``PRAGMA user_version`` того же файла и меняется в той
же транзакции, что и сама правка: запуск, оборванный посередине, не повторит сдвиг
дважды, а выполненная правка не запустится снова.

На PostgreSQL правки не нужны: ``timestamptz`` хранит момент вместе с поясом.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from app.storage.models import FollowupTask, Lead
from app.types import FollowupKind

#: Номер последней правки. Новая правка — следующий номер и ветка в :func:`upgrade_sqlite_data`.
DATA_VERSION: Final[int] = 1

#: Напоминания, срок которых считался от времени пробного: «за 2 часа», «за сутки», «не пришёл».
_SHIFTED_WITH_SLOT: Final[frozenset[str]] = frozenset(
    {
        FollowupKind.TRIAL_REMINDER_2H.value,
        FollowupKind.TRIAL_REMINDER_20H.value,
        FollowupKind.NO_SHOW.value,
    }
)
#: Утреннее напоминание стоит на часы школы в день занятия, а не на сдвиг от него.
_MORNING: Final[str] = FollowupKind.TRIAL_REMINDER_MORNING.value


async def upgrade_sqlite_data(connection: AsyncConnection, *, timezone_name: str) -> list[str]:
    """Выполняет недостающие правки. Возвращает строки для журнала запуска."""
    if connection.dialect.name != "sqlite":
        return []
    version = int((await connection.exec_driver_sql("PRAGMA user_version")).scalar_one() or 0)
    done: list[str] = []
    if version < 1:
        moved = await _trial_slot_to_utc(connection, ZoneInfo(timezone_name))
        done.append(f"время пробного переведено из местного в UTC: {moved}")
    if version < DATA_VERSION:
        await connection.exec_driver_sql(f"PRAGMA user_version = {DATA_VERSION}")
    return done


async def _trial_slot_to_utc(connection: AsyncConnection, zone: ZoneInfo) -> int:
    """Правка 1: ``lead.trial_slot`` лежал в файле местным временем школы без пояса.

    Единственный, кто его писал, — запись на пробное (``session.starts_at``, время с
    поясом школы), а SQLite пояс отбрасывал. Читатели же считают наивное время UTC,
    отсюда напоминание «сегодня в 22:00» о занятии в 17:00. С этой версии момент
    приводится к UTC при записи (:class:`app.storage.models.UtcDateTime`), а уже
    лежащие строки сдвигаются здесь — один раз.
    """
    table = Lead.__table__
    rows = (
        await connection.execute(
            sa.select(table.c.id, table.c.conversation_id, table.c.trial_slot).where(
                table.c.trial_slot.is_not(None)
            )
        )
    ).all()
    for lead_id, conversation_id, local in rows:
        if local.tzinfo is not None:  # pragma: no cover - SQLite пояс не возвращает
            continue
        moment = local.replace(tzinfo=zone).astimezone(timezone.utc)
        await connection.execute(
            sa.update(table).where(table.c.id == lead_id).values(trial_slot=moment)
        )
        await _pending_reminders_follow(connection, conversation_id, local, moment, zone)
    return len(rows)


async def _pending_reminders_follow(
    connection: AsyncConnection, conversation_id: object, local: datetime, moment: datetime, zone: ZoneInfo
) -> None:
    """Незапущенные напоминания о пробном переезжают вместе со временем занятия.

    Их сроки считались от того же неверного времени: «за два часа» о занятии в 17:00
    стояло на 20:00. Без правки страховка «занятие уже началось» их бы просто сняла, и
    клиент, записанный на сегодня, не получил бы напоминания вовсе.
    """
    tasks = FollowupTask.__table__
    pending = (
        await connection.execute(
            sa.select(tasks.c.id, tasks.c.kind, tasks.c.run_at).where(
                tasks.c.conversation_id == conversation_id,
                tasks.c.state == "pending",
                tasks.c.kind.in_(_SHIFTED_WITH_SLOT | {_MORNING}),
            )
        )
    ).all()
    # Сколько прибавило чтение «местных» часов как UTC: на столько же уехал каждый срок.
    shift = local - moment.replace(tzinfo=None)
    starts = moment.astimezone(zone)
    for task_id, kind, run_at in pending:
        values: dict[str, object]
        if kind == _MORNING:
            # Тот же час школы, но в настоящий день занятия. Занятие раньше этого часа —
            # утреннее напоминание опоздало бы, его не ставили бы вовсе.
            clock = run_at.replace(tzinfo=timezone.utc).astimezone(zone)
            morning = starts.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
            values = {"run_at": morning} if morning < starts else {"state": "cancelled"}
        else:
            values = {"run_at": (run_at - shift).replace(tzinfo=timezone.utc)}
        await connection.execute(sa.update(tasks).where(tasks.c.id == task_id).values(**values))
