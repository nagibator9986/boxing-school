"""Список возврата в базе бота: импорт, сверка с перепиской бота и отметки исхода.

CRM пишет в базу бота только свою таблицу ``campaign_contact`` (её создаёт ``create_all``
при запуске службы, как и все таблицы бота). Таблицы самого бота здесь только читаются:
по ним видно, что номер уже записан, писал стоп-слово или с ним говорит администратор.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.campaign.contacts import (
    BotFacts,
    CampaignOutcome,
    CampaignSegment,
    CampaignStatus,
    Decision,
    apply_outcome,
    initial_decision,
    reconcile,
)
from app.core.ignore_list import is_ignored
from app.logging_conf import get_logger
from crm.botdb import _parse_ts
from crm.campaign_excel import ImportRow

__all__ = ["CampaignStore", "ContactRow", "ImportReport"]

_log = get_logger(__name__)

TABLE: Final[str] = "campaign_contact"

#: Паузы, которые ставит человек: в таком диалоге бот молчит, а клиенту пишет администратор.
_HUMAN_PAUSES: Final[frozenset[str]] = frozenset({"operator_reply", "manual"})

#: Поля из файла, которые обновляет повторный импорт.
_DESCRIPTIVE: Final[tuple[str, ...]] = (
    "wa_name", "parent_name", "child_name", "stage", "summary", "reason", "obstacle",
    "next_step", "check_note", "trial_info", "payment_info", "last_contact_at", "source_key",
)


@dataclass(frozen=True, slots=True)
class ContactRow:
    """Строка списка возврата для страниц CRM."""

    id: str
    chat_id: str
    wa_name: str | None
    parent_name: str | None
    child_name: str | None
    stage: str | None
    segment: str
    summary: str | None
    reason: str | None
    obstacle: str | None
    next_step: str | None
    check_note: str | None
    trial_info: str | None
    payment_info: str | None
    last_contact_at: datetime | None
    not_before: datetime | None
    status: str
    status_reason: str | None
    outcome: str | None
    checked_at: datetime | None
    note: str | None
    conversation_id: str | None

    @property
    def phone(self) -> str:
        """«+7 701 123 45 67» — так номер читает администратор."""
        digits = self.chat_id
        if len(digits) == 11:
            return f"+{digits[0]} {digits[1:4]} {digits[4:7]} {digits[7:9]} {digits[9:]}"
        return digits


@dataclass(slots=True)
class ImportReport:
    """Что сделал импорт — показывается администратору одной строкой."""

    added: int = 0
    updated: int = 0
    kept: int = 0
    reconciled: int = 0


def _stamp(value: datetime | None) -> str | None:
    """Время в том виде, в каком его пишет в SQLite сам бот: UTC без пояса."""
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


class CampaignStore:
    """Доступ к списку возврата. Экземпляр на запрос."""

    def __init__(self, path: Path, *, tz: str = "Asia/Almaty", silent_numbers: Iterable[str] = ()) -> None:
        self._path = Path(path)
        self._silent = frozenset(silent_numbers)
        try:
            self._tz = ZoneInfo(tz)
        except Exception:  # noqa: BLE001 - кривое имя зоны не повод падать
            self._tz = ZoneInfo("Asia/Almaty")

    # ------------------------------------------------------------- соединение
    def _connect(self, *, write: bool = False) -> sqlite3.Connection:
        uri = f"file:{self._path}" + ("" if write else "?mode=ro")
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    @property
    def ready(self) -> bool:
        """Есть ли таблица. Её создаёт запуск службы бота; до него раздел пуст."""
        if not self._path.is_file():
            return False
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
                ).fetchone()
        except sqlite3.Error as exc:
            _log.error("campaign_table_check_failed", error=str(exc))
            return False
        return row is not None

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        if not self.ready:
            return []
        try:
            with self._connect() as conn:
                return list(conn.execute(sql, tuple(params)).fetchall())
        except sqlite3.Error as exc:
            _log.error("campaign_query_failed", error=str(exc), sql=sql.strip()[:120])
            return []

    # ----------------------------------------------------------------- чтение
    def counts(self) -> dict[str, int]:
        """Сколько контактов в каждом статусе."""
        return {
            row["status"]: int(row["n"])
            for row in self._query(f"SELECT status, COUNT(*) AS n FROM {TABLE} GROUP BY status")
        }

    def contacts(self, *, statuses: Sequence[str], segment: str = "", limit: int = 500) -> list[ContactRow]:
        """Контакты в этих статусах: свежие разговоры — первыми."""
        if not statuses:
            return []
        marks = ", ".join("?" for _ in statuses)
        params: list[Any] = list(statuses)
        clause = f"status IN ({marks})"
        if segment:
            clause += " AND segment = ?"
            params.append(segment)
        rows = self._query(
            f"SELECT * FROM {TABLE} WHERE {clause}"
            " ORDER BY last_contact_at IS NULL, last_contact_at DESC LIMIT ?",
            [*params, int(limit)],
        )
        return [self._row(row) for row in rows]

    def segment_counts(self, *, statuses: Sequence[str]) -> list[tuple[str, int]]:
        """Сегменты среди контактов в этих статусах — для фильтра."""
        if not statuses:
            return []
        marks = ", ".join("?" for _ in statuses)
        rows = self._query(
            f"SELECT segment, COUNT(*) AS n FROM {TABLE} WHERE status IN ({marks})"
            " GROUP BY segment ORDER BY n DESC",
            list(statuses),
        )
        return [(row["segment"], int(row["n"])) for row in rows]

    def contact(self, contact_id: str) -> ContactRow | None:
        rows = self._query(f"SELECT * FROM {TABLE} WHERE id = ?", (contact_id,))
        return self._row(rows[0]) if rows else None

    # ----------------------------------------------------------------- запись
    def import_rows(self, rows: Sequence[ImportRow], *, batch: str, now: datetime) -> ImportReport:
        """Заводит новые контакты и обновляет описание прежних.

        Решение человека импорт не отменяет: у проверенного контакта и у того, кому уже
        писали, обновляется только описание из файла, статус остаётся.
        """
        report = ImportReport()
        if not self.ready:
            return report
        stamp = _stamp(now)
        with self._connect(write=True) as conn:
            existing = {
                row["chat_id"]: row
                for row in conn.execute(f"SELECT id, chat_id, status, outcome, not_before FROM {TABLE}")
            }
            for item in rows:
                values = {name: getattr(item, name) for name in _DESCRIPTIVE}
                values["last_contact_at"] = _stamp(item.last_contact_at)
                values["check_required"] = 1 if item.check_required else 0
                values["import_batch"] = batch
                current = existing.get(item.chat_id)
                if current is None:
                    decision = initial_decision(item.segment, check_required=item.check_required)
                    self._insert(conn, item, values, decision, stamp)
                    report.added += 1
                    continue
                fresh = current["outcome"] is None and current["status"] in (
                    CampaignStatus.NEEDS_CHECK.value,
                    CampaignStatus.READY.value,
                )
                if fresh:
                    decision = initial_decision(item.segment, check_required=item.check_required)
                    values.update(segment=decision.segment.value, status=decision.status.value,
                                  status_reason=decision.reason)
                    report.updated += 1
                else:
                    report.kept += 1
                if item.not_before is not None and current["outcome"] is None and not current["not_before"]:
                    values["not_before"] = _stamp(item.not_before)
                assignments = ", ".join(f"{name} = ?" for name in values)
                conn.execute(
                    f"UPDATE {TABLE} SET {assignments}, updated_at = ? WHERE id = ?",
                    [*values.values(), stamp, current["id"]],
                )
        report.reconciled = self.reconcile_all(now=now)
        return report

    def _insert(
        self, conn: sqlite3.Connection, item: ImportRow, values: dict[str, Any], decision: Decision, stamp: str | None
    ) -> None:
        columns = {
            "id": uuid4().hex,
            "chat_id": item.chat_id,
            "channel": "whatsapp",
            **values,
            "not_before": _stamp(item.not_before),
            "segment": decision.segment.value,
            "status": decision.status.value,
            "status_reason": decision.reason,
            "created_at": stamp,
            "updated_at": stamp,
        }
        names = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        conn.execute(f"INSERT INTO {TABLE} ({names}) VALUES ({marks})", list(columns.values()))

    def reconcile_all(self, *, now: datetime) -> int:
        """Сверяет открытые контакты с базой бота. Возвращает, у скольких поменялся статус."""
        if not self.ready:
            return 0
        changed = 0
        stamp = _stamp(now)
        with self._connect(write=True) as conn:
            facts = self._bot_facts(conn)
            rows = conn.execute(
                f"SELECT id, chat_id, status, segment, outcome, status_reason, conversation_id FROM {TABLE}"
            ).fetchall()
            for row in rows:
                known = facts.get(row["chat_id"], BotFacts())
                if is_ignored(self._silent, chat_id=row["chat_id"], phone=None):
                    known = BotFacts(**{**_facts_dict(known), "ignored": True})
                if known.conversation_id != row["conversation_id"]:
                    conn.execute(
                        f"UPDATE {TABLE} SET conversation_id = ? WHERE id = ?", (known.conversation_id, row["id"])
                    )
                decision = reconcile(
                    _status(row["status"]),
                    _segment(row["segment"]),
                    known,
                    now=now,
                    checked=row["outcome"] is not None,
                )
                if decision is None or (
                    decision.status.value == row["status"]
                    and decision.segment.value == row["segment"]
                    and decision.reason == row["status_reason"]
                ):
                    continue
                conn.execute(
                    f"UPDATE {TABLE} SET status = ?, segment = ?, status_reason = ?, updated_at = ? WHERE id = ?",
                    (decision.status.value, decision.segment.value, decision.reason, stamp, row["id"]),
                )
                changed += 1
        return changed

    def set_outcome(
        self,
        contact_id: str,
        outcome: CampaignOutcome,
        *,
        now: datetime,
        not_before: datetime | None = None,
        note: str | None = None,
    ) -> bool:
        """Отметка исхода человеком. ``False`` — контакта нет или ему уже писали."""
        current = self.contact(contact_id)
        if current is None or _status(current.status) not in (
            CampaignStatus.NEEDS_CHECK,
            CampaignStatus.READY,
            CampaignStatus.EXCLUDED,
        ):
            return False
        decision = apply_outcome(outcome, _segment(current.segment), not_before=not_before)
        with self._connect(write=True) as conn:
            # Дата «не раньше» меняется только отметкой «позже»: прочие отметки сохраняют ту,
            # о которой клиент просил сам («напишите в ноябре»).
            later = outcome is CampaignOutcome.LATER
            conn.execute(
                f"UPDATE {TABLE} SET status = ?, segment = ?, status_reason = ?, outcome = ?, checked_at = ?,"
                " note = COALESCE(?, note), not_before = CASE WHEN ? THEN ? ELSE not_before END,"
                " updated_at = ? WHERE id = ?",
                (
                    decision.status.value,
                    decision.segment.value,
                    decision.reason,
                    outcome.value,
                    _stamp(now),
                    note,
                    1 if later else 0,
                    _stamp(not_before) if later else None,
                    _stamp(now),
                    contact_id,
                ),
            )
        # Отметка «писать можно» не перекрывает жёсткие запреты бота: стоп-слово, запись впереди.
        self.reconcile_all(now=now)
        return True

    def restore(self, contact_id: str, *, now: datetime) -> bool:
        """Вернуть на проверку исключённый по ошибке контакт. Стоп-слово так не снимается."""
        current = self.contact(contact_id)
        if current is None or _status(current.status) not in (CampaignStatus.EXCLUDED, CampaignStatus.READY):
            return False
        with self._connect(write=True) as conn:
            conn.execute(
                f"UPDATE {TABLE} SET status = ?, status_reason = ?, outcome = NULL, checked_at = NULL,"
                " updated_at = ? WHERE id = ?",
                (CampaignStatus.NEEDS_CHECK.value, "Возвращён на проверку", _stamp(now), contact_id),
            )
        self.reconcile_all(now=now)
        return True

    # -------------------------------------------------------------- база бота
    def _bot_facts(self, conn: sqlite3.Connection) -> dict[str, BotFacts]:
        """Что бот знает о номерах WhatsApp: последний диалог каждого номера."""
        try:
            rows = conn.execute(
                "SELECT c.id, c.chat_id, c.last_inbound_at, c.last_outbound_at, c.followup_blocked,"
                " e.paused, e.pause_reason, e.paused_until,"
                " (SELECT l.status FROM lead l WHERE l.conversation_id = c.id"
                "  ORDER BY l.updated_at DESC LIMIT 1) AS lead_status,"
                " (SELECT l.trial_slot FROM lead l WHERE l.conversation_id = c.id"
                "  ORDER BY l.updated_at DESC LIMIT 1) AS trial_slot"
                " FROM conversation c LEFT JOIN escalation_state e ON e.conversation_id = c.id"
                " WHERE c.chat_type = 'whatsapp'"
                " ORDER BY c.last_inbound_at"
            ).fetchall()
        except sqlite3.Error as exc:
            # Таблиц бота ещё нет (бот ни разу не запускался) — сверять не с чем.
            _log.warning("campaign_bot_facts_unavailable", error=str(exc))
            return {}
        now = datetime.now(tz=UTC)
        facts: dict[str, BotFacts] = {}
        for row in rows:
            inbound, outbound = _parse_ts(row["last_inbound_at"]), _parse_ts(row["last_outbound_at"])
            until = _parse_ts(row["paused_until"])
            facts[str(row["chat_id"])] = BotFacts(
                conversation_id=str(row["id"]),
                last_activity_at=max((moment for moment in (inbound, outbound) if moment), default=None),
                stop_word=bool(row["followup_blocked"]),
                lead_status=row["lead_status"],
                trial_at=_parse_ts(row["trial_slot"]),
                human_pause=bool(row["paused"])
                and row["pause_reason"] in _HUMAN_PAUSES
                and (until is None or until > now),
            )
        return facts

    # --------------------------------------------------------------- строки
    def _row(self, row: sqlite3.Row) -> ContactRow:
        def local(name: str) -> datetime | None:
            moment = _parse_ts(row[name])
            return moment.astimezone(self._tz) if moment else None

        return ContactRow(
            id=str(row["id"]),
            chat_id=str(row["chat_id"]),
            wa_name=row["wa_name"],
            parent_name=row["parent_name"],
            child_name=row["child_name"],
            stage=row["stage"],
            segment=row["segment"],
            summary=row["summary"],
            reason=row["reason"],
            obstacle=row["obstacle"],
            next_step=row["next_step"],
            check_note=row["check_note"],
            trial_info=row["trial_info"],
            payment_info=row["payment_info"],
            last_contact_at=local("last_contact_at"),
            not_before=local("not_before"),
            status=row["status"],
            status_reason=row["status_reason"],
            outcome=row["outcome"],
            checked_at=local("checked_at"),
            note=row["note"],
            conversation_id=row["conversation_id"],
        )


def _facts_dict(facts: BotFacts) -> dict[str, Any]:
    return {name: getattr(facts, name) for name in BotFacts.__slots__}


def _status(value: str) -> CampaignStatus:
    try:
        return CampaignStatus(value)
    except ValueError:
        return CampaignStatus.NEEDS_CHECK


def _segment(value: str) -> CampaignSegment:
    try:
        return CampaignSegment(value)
    except ValueError:
        return CampaignSegment.UNKNOWN
