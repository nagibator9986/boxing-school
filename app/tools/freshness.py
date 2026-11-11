"""Свежесть расписания в уже отправленном: что клиент видел до того, как расписание поменялось.

Владелец 11.11.2026, через сутки после выкатки нового расписания Плазы и Тобыла: «вы не
исправили расписание». База знаний на сервере была верной, а в переписках, начатых раньше,
бот опирался на собственную прежнюю карточку: заметка «клиент уже получил расписание — не
присылай повторно, ссылайся на список выше», лимит повторной отправки и анти-галлюцинационный
фильтр, который доверяет любому времени, однажды сказанному в этом диалоге.

Момент смены — ``Gym.schedule_since`` из базы знаний. Строка очереди, ушедшая раньше него и
несущая расписание этого зала, устарела.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Final, Iterable, Mapping
from zoneinfo import ZoneInfo

from app.kb.models import Gym, KBSnapshot
from app.types import OutboundKind

#: Карточка расписания зала в очереди: ``schedule_<gym_id>`` (см. ``app.tools.schedule``).
SCHEDULE_CARD_PREFIX: Final[str] = "schedule_"

#: Виды строк, текст которых пишет модель или человек: зал в них узнаётся по названию, а
#: устаревшим такое сообщение бывает, только если в нём есть время.
_FREE_TEXT_KINDS: Final[frozenset[str]] = frozenset({
    OutboundKind.BOT_REPLY.value,
    OutboundKind.FOLLOWUP.value,
    OutboundKind.CAMPAIGN.value,
    OutboundKind.OPERATOR_REPLY.value,
    OutboundKind.ESCALATION_NOTICE.value,
})

_TIME_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3])[:.][0-5]\d(?!\d)")


@dataclass(frozen=True, slots=True)
class SentRow:
    """Строка очереди исходящих, как её видит проверка свежести."""

    created_at: datetime
    text: str
    kind: str | None
    artifact_ids: tuple[str, ...]

    @classmethod
    def from_payload(cls, created_at: datetime, payload: Mapping[str, Any] | None) -> "SentRow":
        data = dict(payload or {})
        own = (str(data["artifact_id"]),) if data.get("artifact_id") else ()
        merged = tuple(str(item) for item in (data.get("merged_artifact_ids") or ()) if item)
        return cls(
            created_at=created_at,
            text=str(data.get("text") or ""),
            kind=str(data["kind"]) if data.get("kind") else None,
            artifact_ids=tuple(dict.fromkeys(own + merged)),
        )


def since_of(snapshot: KBSnapshot, gym: Gym) -> datetime | None:
    """С какого момента действует расписание зала, с поясом. ``None`` — не отмечено."""
    value = gym.schedule_since
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(snapshot.gyms.timezone))
    return value


def schedule_gyms(snapshot: KBSnapshot, artifact_id: str) -> tuple[Gym, ...]:
    """Залы, чьё расписание несёт этот материал: карточка зала, подпись к видео дороги, список."""
    if artifact_id.startswith(SCHEDULE_CARD_PREFIX):
        gym = snapshot.gym(artifact_id[len(SCHEDULE_CARD_PREFIX):])
        if gym is not None:
            return (gym,)
    artifact = snapshot.artifact(artifact_id)
    if artifact is None:
        return ()
    if artifact.render_from == "route" and artifact.gym_id:
        gym = snapshot.gym(artifact.gym_id)
        return (gym,) if gym is not None else ()
    if artifact.render_from == "schedules":
        return snapshot.active_gyms(artifact.scope)
    return ()


def artifact_since(snapshot: KBSnapshot, artifact_id: str) -> datetime | None:
    """С какого момента материал показывает нынешнее расписание.

    ``None`` — расписания в материале нет или момент смены не отмечен: тогда любая прежняя
    отправка считается, как и раньше.
    """
    moments = [moment for gym in schedule_gyms(snapshot, artifact_id) if (moment := since_of(snapshot, gym))]
    return max(moments, default=None)


def outdated_gyms(
    snapshot: KBSnapshot,
    row: SentRow,
    *,
    named_in: Callable[[KBSnapshot, str], Iterable[str]],
) -> tuple[Gym, ...]:
    """Залы, расписание которых в этой строке устарело. Пусто — строка свежая.

    Карточки и подписи узнаются по материалам строки. Текст модели или человека — по
    названному залу, и только если в тексте есть время: «Ждём вас на Плазе» не устаревает.
    """
    sent_at = _aware(row.created_at)
    gyms: dict[str, Gym] = {}
    for artifact_id in row.artifact_ids:
        for gym in schedule_gyms(snapshot, artifact_id):
            gyms[gym.id] = gym
    if row.kind in _FREE_TEXT_KINDS and _TIME_RE.search(row.text):
        for gym_id in named_in(snapshot, row.text):
            gym = snapshot.gym(gym_id)
            if gym is not None:
                gyms[gym.id] = gym
    return tuple(
        gym for gym in gyms.values()
        if (since := since_of(snapshot, gym)) is not None and sent_at < since
    )


def _aware(moment: datetime) -> datetime:
    """Наивное время из базы — UTC (соглашение ``app.storage.models.UtcDateTime``)."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


__all__ = [
    "SCHEDULE_CARD_PREFIX",
    "SentRow",
    "artifact_since",
    "outdated_gyms",
    "schedule_gyms",
    "since_of",
]
