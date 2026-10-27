"""Что знает о номере сам бот — одно правило для CRM и для отправки.

Сверку делают двое: CRM (импорт, кнопка «Сверить», отметки исхода; синхронный ``sqlite3``)
и воркер рассылки (перед каждым сообщением; асинхронный SQLAlchemy). Запрос и правила
сложения здесь одни на обоих: разъехавшись, они дали бы «в CRM контакт на проверке, а
сообщение ему ушло».
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Final

from app.campaign.contacts import BotFacts
from app.core.ignore_list import is_ignored, tail

__all__ = ["FACTS_SQL", "facts_for", "facts_from_rows", "merge", "parse_ts"]

#: Паузы, которые ставит человек: в таком диалоге бот молчит, а клиенту пишет администратор.
HUMAN_PAUSES: Final[frozenset[str]] = frozenset({"operator_reply", "manual"})

#: Диалоги WhatsApp со всем, что важно для рассылки. Заявка — последняя по времени правки.
FACTS_SQL: Final[str] = (
    "SELECT c.id, c.chat_id, c.last_inbound_at, c.last_outbound_at, c.followup_blocked,"
    " e.paused, e.pause_reason, e.paused_until,"
    " (SELECT l.status FROM lead l WHERE l.conversation_id = c.id"
    "  ORDER BY l.updated_at DESC LIMIT 1) AS lead_status,"
    " (SELECT l.trial_slot FROM lead l WHERE l.conversation_id = c.id"
    "  ORDER BY l.updated_at DESC LIMIT 1) AS trial_slot"
    " FROM conversation c LEFT JOIN escalation_state e ON e.conversation_id = c.id"
    " WHERE c.chat_type = 'whatsapp'"
)


def parse_ts(value: Any) -> datetime | None:
    """Отметка времени из SQLite. Наивное значение — UTC: так его пишет SQLAlchemy."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def facts_from_rows(rows: Iterable[Mapping[str, Any]], *, now: datetime) -> dict[str, BotFacts]:
    """Строки :data:`FACTS_SQL` → сведения по номеру (ключ — последние десять цифр).

    Диалогов у одного номера бывает несколько (сменился канал Wazzup): стоп-слово,
    сказанное в старом, действует и в новом, поэтому сведения складываются, а не
    берутся из последнего.
    """
    facts: dict[str, BotFacts] = {}
    for row in rows:
        key = tail(str(row["chat_id"]))
        if not key:
            continue
        inbound, outbound = parse_ts(row["last_inbound_at"]), parse_ts(row["last_outbound_at"])
        activity = max((moment for moment in (inbound, outbound) if moment), default=None)
        until = parse_ts(row["paused_until"])
        one = BotFacts(
            conversation_id=str(row["id"]),
            last_activity_at=activity,
            last_inbound_at=inbound,
            stop_word=bool(row["followup_blocked"]),
            lead_status=row["lead_status"],
            trial_at=parse_ts(row["trial_slot"]),
            human_pause=bool(row["paused"])
            and row["pause_reason"] in HUMAN_PAUSES
            and (until is None or until > now),
        )
        facts[key] = merge(facts.get(key), one)
    return facts


def facts_for(facts: Mapping[str, BotFacts], chat_id: str, *, silent: frozenset[str]) -> BotFacts:
    """Что бот знает о номере; сотрудники и рабочие номера — из настроек владельца."""
    known = facts.get(tail(chat_id), BotFacts())
    if is_ignored(silent, chat_id=chat_id, phone=None):
        known = replace(known, ignored=True)
    return known


def merge(before: BotFacts | None, one: BotFacts) -> BotFacts:
    """Сведения о номере из нескольких диалогов: запреты — из любого, запись и ссылка — из свежего."""
    if before is None:
        return one
    floor = datetime.min.replace(tzinfo=UTC)
    newer = one if (one.last_activity_at or floor) >= (before.last_activity_at or floor) else before
    # Пробное впереди в любом из диалогов важнее записи в последнем.
    trial_source = max(
        (facts for facts in (before, one) if facts.lead_status == "trial_booked" and facts.trial_at),
        key=lambda facts: facts.trial_at,
        default=newer,
    )
    converted = before.lead_status == "converted" or one.lead_status == "converted"
    return BotFacts(
        conversation_id=newer.conversation_id,
        last_activity_at=newer.last_activity_at,
        last_inbound_at=max((m for m in (before.last_inbound_at, one.last_inbound_at) if m), default=None),
        stop_word=before.stop_word or one.stop_word,
        lead_status="converted" if converted else trial_source.lead_status,
        trial_at=trial_source.trial_at,
        human_pause=before.human_pause or one.human_pause,
        ignored=before.ignored or one.ignored,
    )
