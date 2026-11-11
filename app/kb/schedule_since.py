"""Отметка ``Gym.schedule_since``: когда у зала поменялись занятия.

Ставят её все, кто пишет расписание в ``kb/gyms.yaml``: CRM (форма зала, вставка текстом,
откат копии) и админка бота в Telegram. По ней бот понимает, что карточка расписания,
ушедшая клиенту раньше, устарела (``app.tools.freshness``).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.kb.models import ScheduleSlot

#: Пояс по умолчанию — тот же, что у ``GymsFile.timezone``.
DEFAULT_TZ = "Asia/Almaty"


def schedule_key(schedule: Any) -> str:
    """Занятия зала одной строкой для сравнения «было — стало».

    Через модель занятия: форма CRM собирает занятия заново, и пустое поле в ней может
    прийти явным ``null`` там, где в файле его не было, — без нормализации каждое
    сохранение зала «меняло» бы расписание.
    """
    slots: list[Any] = []
    for raw in schedule or ():
        try:
            slots.append(ScheduleSlot.model_validate(dict(raw)).model_dump(mode="json"))
        except Exception:  # noqa: BLE001 - кривую строку отклонит проверка базы, сравним как есть
            slots.append(raw)
    return json.dumps(slots, sort_keys=True, ensure_ascii=False, default=str)


def schedules_differ(before: Any, after: Any) -> bool:
    """Поменялись ли занятия по существу."""
    return schedule_key(before) != schedule_key(after)


def stamp_now(tz_name: str | None = None) -> str:
    """Момент смены для записи в YAML: по часам школы, с поясом, без долей секунды."""
    try:
        tz = ZoneInfo(tz_name or DEFAULT_TZ)
    except Exception:  # noqa: BLE001 - кривой пояс в файле не должен ронять правку
        tz = ZoneInfo(DEFAULT_TZ)
    return datetime.now(tz=tz).replace(microsecond=0).isoformat()


__all__ = ["schedule_key", "schedules_differ", "stamp_now"]
