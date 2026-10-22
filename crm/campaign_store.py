"""Список возврата в базе бота: импорт, сверка с перепиской бота и отметки исхода.

CRM пишет в базу бота только свою таблицу ``campaign_contact`` (её создаёт ``create_all``
при запуске службы, как и все таблицы бота). Таблицы самого бота здесь только читаются:
по ним видно, что номер уже записан, писал стоп-слово или с ним говорит администратор.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
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
from app.core.ignore_list import is_ignored, tail
from app.logging_conf import get_logger
from crm.botdb import _parse_ts
from crm.campaign_excel import ImportRow

__all__ = ["CampaignStore", "ContactRow", "ImportReport", "StoreBusy"]

_log = get_logger(__name__)

TABLE: Final[str] = "campaign_contact"

#: Паузы, которые ставит человек: в таком диалоге бот молчит, а клиенту пишет администратор.
_HUMAN_PAUSES: Final[frozenset[str]] = frozenset({"operator_reply", "manual"})

#: Поля из файла, которые обновляет повторный импорт.
_DESCRIPTIVE: Final[tuple[str, ...]] = (
    "wa_name", "parent_name", "child_name", "stage", "summary", "reason", "obstacle",
    "next_step", "check_note", "trial_info", "payment_info", "last_contact_at", "source_key",
)


class StoreBusy(Exception):
    """База бота не ответила (занята записью бота или повреждена). Повторить позже."""


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

    @contextmanager
    def _session(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Соединение на одну операцию: транзакция и закрытие в конце.

        ``with sqlite3.connect(...)`` только фиксирует транзакцию, а файл держит открытым до
        сборки мусора. Занятая база при записи — :class:`StoreBusy`, а не страница ошибки.
        """
        try:
            conn = self._connect(write=write)
        except sqlite3.Error as exc:
            if write:
                raise StoreBusy(str(exc)) from exc
            raise
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _write(self) -> Any:
        return self._session(write=True)

    @property
    def ready(self) -> bool:
        """Есть ли таблица. Её создаёт запуск службы бота; до него раздел пуст."""
        if not self._path.is_file():
            return False
        try:
            with self._session() as conn:
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
            with self._session() as conn:
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
        """Заводит новые контакты и обновляет описание прежних — и сразу сверяет их с ботом.

        Решение человека импорт не отменяет: у проверенного контакта и у того, кому уже
        писали, обновляется только описание из файла, статус остаётся.
        """
        report = ImportReport()
        if not self.ready:
            return report
        stamp = _stamp(now)
        try:
            with self._write() as conn:
                facts = self._bot_facts(conn)
                existing = {
                    row["chat_id"]: row
                    for row in conn.execute(
                        f"SELECT id, chat_id, status, segment, outcome, checked_at, not_before FROM {TABLE}"
                    )
                }
                for item in rows:
                    values = {name: getattr(item, name) for name in _DESCRIPTIVE}
                    values["last_contact_at"] = _stamp(item.last_contact_at)
                    values["check_required"] = 1 if item.check_required else 0
                    values["import_batch"] = batch
                    current = existing.get(item.chat_id)
                    known = self._facts_for(facts, item.chat_id)
                    if current is None:
                        decision = self._settle(
                            initial_decision(item.segment, check_required=item.check_required), known, now, None, report
                        )
                        self._insert(conn, item, values, decision, known.conversation_id, stamp)
                        report.added += 1
                        continue
                    # Без отметки человека статус — вывод из файла, и новый файл его пересчитывает:
                    # «текущий диалог» прошлого разбора в новом мог стать «интерес затих».
                    # Исключение по боту (сотрудник, «купил») сверка тут же вернёт.
                    fresh = current["outcome"] is None and current["status"] in (
                        CampaignStatus.NEEDS_CHECK.value,
                        CampaignStatus.READY.value,
                        CampaignStatus.EXCLUDED.value,
                    )
                    if fresh:
                        decision = self._settle(
                            initial_decision(item.segment, check_required=item.check_required), known, now, None, report
                        )
                        report.updated += 1
                    else:
                        decision = self._settle(
                            Decision(_status(current["status"]), _segment(current["segment"])),
                            known, now, _parse_ts(current["checked_at"]), report, keep_reason=True,
                        )
                        report.kept += 1
                    if decision is not None:
                        values.update(segment=decision.segment.value, status=decision.status.value)
                        if decision.reason is not None or fresh:
                            values["status_reason"] = decision.reason
                    if item.not_before is not None and current["outcome"] is None and not current["not_before"]:
                        values["not_before"] = _stamp(item.not_before)
                    values["conversation_id"] = known.conversation_id
                    assignments = ", ".join(f"{name} = ?" for name in values)
                    conn.execute(
                        f"UPDATE {TABLE} SET {assignments}, updated_at = ? WHERE id = ?",
                        [*values.values(), stamp, current["id"]],
                    )
        except sqlite3.Error as exc:
            _log.error("campaign_import_failed", error=str(exc))
            raise StoreBusy(str(exc)) from exc
        return report

    @staticmethod
    def _settle(
        decision: Decision,
        known: BotFacts,
        now: datetime,
        checked_at: datetime | None,
        report: ImportReport,
        *,
        keep_reason: bool = False,
    ) -> Decision | None:
        """Решение по файлу, поправленное перепиской бота. ``None`` — у прежнего контакта менять нечего."""
        adjusted = reconcile(decision.status, decision.segment, known, now=now, checked_at=checked_at)
        if adjusted is None:
            return None if keep_reason else decision
        if (adjusted.status, adjusted.segment) != (decision.status, decision.segment):
            report.reconciled += 1
        return adjusted

    def _insert(
        self,
        conn: sqlite3.Connection,
        item: ImportRow,
        values: dict[str, Any],
        decision: Decision,
        conversation_id: str | None,
        stamp: str | None,
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
            "conversation_id": conversation_id,
            "created_at": stamp,
            "updated_at": stamp,
        }
        names = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        conn.execute(f"INSERT INTO {TABLE} ({names}) VALUES ({marks})", list(columns.values()))

    def reconcile_all(self, *, now: datetime) -> int:
        """Сверяет все контакты с базой бота. Возвращает, у скольких поменялся статус."""
        if not self.ready:
            return 0
        try:
            with self._write() as conn:
                facts = self._bot_facts(conn)
                rows = conn.execute(f"SELECT * FROM {TABLE}").fetchall()
                return sum(self._reconcile_row(conn, row, facts, now) for row in rows)
        except sqlite3.Error as exc:
            _log.error("campaign_reconcile_failed", error=str(exc))
            raise StoreBusy(str(exc)) from exc

    def _reconcile_row(
        self, conn: sqlite3.Connection, row: sqlite3.Row, facts: dict[str, BotFacts], now: datetime
    ) -> int:
        """Сверка одной строки. ``1`` — статус или сегмент поменялись."""
        known = self._facts_for(facts, row["chat_id"])
        if known.conversation_id != row["conversation_id"]:
            conn.execute(f"UPDATE {TABLE} SET conversation_id = ? WHERE id = ?", (known.conversation_id, row["id"]))
        decision = reconcile(
            _status(row["status"]), _segment(row["segment"]), known, now=now, checked_at=_parse_ts(row["checked_at"])
        )
        if decision is None or (
            decision.status.value == row["status"]
            and decision.segment.value == row["segment"]
            and decision.reason == row["status_reason"]
        ):
            return 0
        conn.execute(
            f"UPDATE {TABLE} SET status = ?, segment = ?, status_reason = ?, updated_at = ? WHERE id = ?",
            (decision.status.value, decision.segment.value, decision.reason, _stamp(now), row["id"]),
        )
        return int((decision.status.value, decision.segment.value) != (row["status"], row["segment"]))

    def set_outcome(
        self,
        contact_id: str,
        outcome: CampaignOutcome,
        *,
        now: datetime,
        not_before: datetime | None = None,
        note: str | None = None,
    ) -> ContactRow | None:
        """Отметка исхода человеком. Возвращает контакт после отметки и сверки с ботом.

        ``None`` — контакта нет, ему уже писали или он сам попросил не писать.
        """
        try:
            with self._write() as conn:
                row = conn.execute(f"SELECT * FROM {TABLE} WHERE id = ?", (contact_id,)).fetchone()
                if row is None or _status(row["status"]) not in (
                    CampaignStatus.NEEDS_CHECK,
                    CampaignStatus.READY,
                    CampaignStatus.EXCLUDED,
                ):
                    return None
                decision = apply_outcome(outcome, _segment(row["segment"]), not_before=not_before)
                # Дата «не раньше» меняется только отметкой «позже»: прочие отметки сохраняют ту,
                # о которой клиент просил сам («напишите в ноябре»).
                later = outcome is CampaignOutcome.LATER
                conn.execute(
                    f"UPDATE {TABLE} SET status = ?, segment = ?, status_reason = ?, outcome = ?,"
                    " checked_at = ?, note = COALESCE(?, note),"
                    " not_before = CASE WHEN ? THEN ? ELSE not_before END, updated_at = ? WHERE id = ?",
                    (
                        decision.status.value, decision.segment.value, decision.reason, outcome.value,
                        _stamp(now), note, 1 if later else 0, _stamp(not_before) if later else None,
                        _stamp(now), contact_id,
                    ),
                )
                # Отметка «писать можно» не перекрывает жёсткие запреты бота: стоп-слово, запись впереди.
                fresh = conn.execute(f"SELECT * FROM {TABLE} WHERE id = ?", (contact_id,)).fetchone()
                self._reconcile_row(conn, fresh, self._bot_facts(conn), now)
        except sqlite3.Error as exc:
            _log.error("campaign_outcome_failed", error=str(exc))
            raise StoreBusy(str(exc)) from exc
        return self.contact(contact_id)

    def restore(self, contact_id: str, *, now: datetime) -> ContactRow | None:
        """Вернуть на проверку исключённый по ошибке контакт. Стоп-слово так не снимается.

        Возвращает контакт после сверки с ботом; ``None`` — вернуть нельзя.
        """
        try:
            with self._write() as conn:
                row = conn.execute(f"SELECT * FROM {TABLE} WHERE id = ?", (contact_id,)).fetchone()
                if row is None or _status(row["status"]) not in (CampaignStatus.EXCLUDED, CampaignStatus.READY):
                    return None
                conn.execute(
                    f"UPDATE {TABLE} SET status = ?, status_reason = ?, outcome = NULL, checked_at = NULL,"
                    " updated_at = ? WHERE id = ?",
                    (CampaignStatus.NEEDS_CHECK.value, "Возвращён на проверку", _stamp(now), contact_id),
                )
                fresh = conn.execute(f"SELECT * FROM {TABLE} WHERE id = ?", (contact_id,)).fetchone()
                self._reconcile_row(conn, fresh, self._bot_facts(conn), now)
        except sqlite3.Error as exc:
            _log.error("campaign_restore_failed", error=str(exc))
            raise StoreBusy(str(exc)) from exc
        return self.contact(contact_id)

    # -------------------------------------------------------------- база бота
    def _facts_for(self, facts: dict[str, BotFacts], chat_id: str) -> BotFacts:
        """Что бот знает о номере; сотрудники и рабочие номера — из настроек владельца."""
        known = facts.get(tail(chat_id), BotFacts())
        if is_ignored(self._silent, chat_id=chat_id, phone=None):
            known = replace(known, ignored=True)
        return known

    def _bot_facts(self, conn: sqlite3.Connection) -> dict[str, BotFacts]:
        """Что бот знает о номерах WhatsApp — по всем диалогам номера сразу.

        Диалогов у одного номера бывает несколько (сменился канал Wazzup): стоп-слово,
        сказанное в старом, действует и в новом, поэтому сведения складываются, а не
        берутся из последнего. Номер сравнивается по последним десяти цифрам.
        """
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
            ).fetchall()
        except sqlite3.Error as exc:
            # Таблиц бота ещё нет (бот ни разу не запускался) — сверять не с чем.
            _log.warning("campaign_bot_facts_unavailable", error=str(exc))
            return {}
        now = datetime.now(tz=UTC)
        facts: dict[str, BotFacts] = {}
        for row in rows:
            key = tail(str(row["chat_id"]))
            if not key:
                continue
            inbound, outbound = _parse_ts(row["last_inbound_at"]), _parse_ts(row["last_outbound_at"])
            activity = max((moment for moment in (inbound, outbound) if moment), default=None)
            until = _parse_ts(row["paused_until"])
            one = BotFacts(
                conversation_id=str(row["id"]),
                last_activity_at=activity,
                last_inbound_at=inbound,
                stop_word=bool(row["followup_blocked"]),
                lead_status=row["lead_status"],
                trial_at=_parse_ts(row["trial_slot"]),
                human_pause=bool(row["paused"])
                and row["pause_reason"] in _HUMAN_PAUSES
                and (until is None or until > now),
            )
            facts[key] = _merge(facts.get(key), one)
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


def _merge(before: BotFacts | None, one: BotFacts) -> BotFacts:
    """Сведения о номере из нескольких диалогов: запреты — из любого, запись и ссылка — из свежего."""
    if before is None:
        return one
    newer = one if (one.last_activity_at or datetime.min.replace(tzinfo=UTC)) >= (
        before.last_activity_at or datetime.min.replace(tzinfo=UTC)
    ) else before
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
