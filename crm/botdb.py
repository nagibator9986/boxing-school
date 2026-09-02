"""Чтение базы диалогов бота и две операторские команды поверх неё.

Модуль намеренно ходит в SQLite напрямую, а не через модели SQLAlchemy: CRM —
синхронное Flask-приложение, а слой хранения бота асинхронный. Тянуть в веб
event loop ради пяти выборок значит получить два способа открыть одну базу и
разъезжающиеся сессии.

Читает CRM в режиме **только для чтения** (``mode=ro``): случайная запись в базу
живого бота — это испорченная история диалога, которую не вернуть. Исключение
одно: снять или поставить паузу бота. Пауза хранится в двух местах — строка
``escalation_state`` (истина) и ключ в ``state.db`` (быстрый путь), — и снимать
её нужно в обоих, иначе бот промолчит ещё до конца окна.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.logging_conf import get_logger

__all__ = ["CHANNEL_TITLES", "BotData", "Client", "Dialog", "LeadRow"]

_log = get_logger(__name__)

#: Как называется канал на языке владельца школы.
CHANNEL_TITLES: Final[dict[str, str]] = {
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "instagram": "Instagram",
    "vk": "ВКонтакте",
    "avito": "Авито",
}

#: Значок канала. В списке из сотни строк глаз ищет иконку, а не слово.
CHANNEL_ICONS: Final[dict[str, str]] = {
    "telegram": "✈️",
    "whatsapp": "🟢",
    "instagram": "📸",
    "vk": "🔷",
    "avito": "🟩",
}

#: Каналы, в которые CRM может писать: их очередь читает отправщик Wazzup.
_REPLYABLE_CHANNELS: Final[frozenset[str]] = frozenset({"whatsapp", "instagram"})

#: Отказы отправщика человеческим языком — оператору нужен не код, а причина.
_DENIAL_TEXTS: Final[dict[str, str]] = {
    "channel_cannot_initiate": (
        "В Instagram нельзя писать первым: клиент ещё ни разу не написал сюда."
    ),
    "service_window_expired": (
        "Окно ответа в Instagram закрыто — с последнего сообщения клиента прошло "
        "больше семи суток. Ответить можно, когда он напишет снова."
    ),
}

_UTC: Final[timezone] = UTC


def _rulower(value: Any) -> str:
    """Приведение регистра, знающее про кириллицу. Для SQL-функции ``rulower``."""
    return str(value or "").lower()


def _parse_ts(value: Any) -> datetime | None:
    """Отметка времени из SQLite. Наивное значение считается UTC.

    SQLAlchemy пишет ``TIMESTAMPTZ`` в SQLite строкой без зоны, но всегда в UTC.
    Прочитать её как локальную значило бы сдвинуть всю историю на пять часов.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=_UTC)
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=_UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_UTC)


@dataclass(frozen=True, slots=True)
class Client:
    """Строка списка клиентов."""

    id: str
    conv_key: str
    channel: str
    chat_id: str
    name: str
    username: str | None
    phone: str | None
    lang: str | None
    state: str
    msg_in: int
    msg_out: int
    first_at: datetime | None
    last_at: datetime | None
    last_text: str | None
    has_lead: bool
    paused: bool
    paused_until: datetime | None

    @property
    def channel_title(self) -> str:
        """Название канала для интерфейса."""
        return CHANNEL_TITLES.get(self.channel, self.channel or "—")

    @property
    def channel_icon(self) -> str:
        """Значок канала."""
        return CHANNEL_ICONS.get(self.channel, "💬")


@dataclass(frozen=True, slots=True)
class Dialog:
    """Одно сообщение переписки — то, что видно человеку."""

    direction: str
    author: str
    author_name: str | None
    text: str
    msg_type: str
    status: str
    created_at: datetime | None

    @property
    def is_client(self) -> bool:
        """Реплика клиента (а не бота и не оператора)."""
        return self.direction == "in"


@dataclass(frozen=True, slots=True)
class LeadRow:
    """Лид-карточка в списке заявок."""

    id: str
    conversation_id: str
    created_at: datetime | None
    channel: str | None
    parent_name: str | None
    phone: str | None
    child_name: str
    child_age: int
    child_gender: str
    district: str | None
    gym_id: str | None
    trial_slot_text: str | None
    motivation: str | None
    main_objection: str | None
    status: str
    escalation: bool
    lang: str | None

    @property
    def channel_title(self) -> str:
        """Канал, из которого пришла заявка."""
        return CHANNEL_TITLES.get(self.channel or "", self.channel or "—")



def _canonical_uuid(value: str) -> str | None:
    """``UUID`` из того, как он лежит в SQLite. ``None`` — это не идентификатор.

    В базе они хранятся без дефисов, а модель исходящего ждёт канонический вид.
    """
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


class BotData:
    """Доступ к базе диалогов. Экземпляр создаётся на запрос — соединение дешёвое."""

    def __init__(self, path: Path, *, state_db: Path | None = None, tz: str = "Asia/Almaty") -> None:
        self._path = Path(path)
        self._state_db = Path(state_db) if state_db else None
        try:
            self._tz = ZoneInfo(tz)
        except Exception:  # noqa: BLE001 - кривое имя зоны не повод падать
            self._tz = _UTC

    # ------------------------------------------------------------- соединение
    @property
    def exists(self) -> bool:
        """Есть ли база. Пока бот ни разу не запускался — её нет, и это нормально."""
        return self._path.is_file()

    def _connect(self, *, write: bool = False) -> sqlite3.Connection:
        uri = f"file:{self._path}" + ("" if write else "?mode=ro")
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        # Родная LOWER() в SQLite умеет только латиницу: «Зарина» так и остаётся
        # «Зарина», и поиск по имени клиента не находит ничего. Отдаём приведение
        # регистра Python — он знает про кириллицу и казахские буквы.
        conn.create_function("rulower", 1, _rulower, deterministic=True)
        return conn

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """Выборка. Отсутствие базы или таблицы — пустой результат, не исключение.

        Пустой результат вместо падения нужен ровно для одного случая: бот ещё ни
        разу не запускался, базы диалогов нет, а CRM уже открыли. Показать в этом
        случае нули правильнее, чем страницу с ошибкой. Но молчать нельзя: любая
        другая ошибка SQLite уходит в лог, иначе сломанный запрос выглядел бы как
        «данных нет» и жил бы годами.
        """
        if not self.exists:
            return []
        try:
            with self._connect() as conn:
                return list(conn.execute(sql, tuple(params)).fetchall())
        except sqlite3.Error as exc:
            _log.error("crm_query_failed", error=str(exc), sql=sql.strip()[:120])
            return []

    def _scalar(self, sql: str, params: Sequence[Any] = (), *, default: int = 0) -> int:
        """Одно число из выборки. Нет базы или строк — ``default``."""
        rows = self._query(sql, params)
        if not rows or rows[0][0] is None:
            return default
        return int(rows[0][0])

    def last_message_at(self) -> datetime | None:
        """Когда бот последний раз что-то писал или получал.

        Отдельного пульса у бота нет, и выдумывать «онлайн» по факту запущенной
        CRM нельзя: это разные процессы, один вполне работает без другого.
        """
        rows = self._query("SELECT MAX(created_at) FROM message")
        return self.local(_parse_ts(rows[0][0])) if rows and rows[0][0] else None

    def _utc_offset_sql(self) -> str:
        """Сдвиг местного времени для SQL ``date(..., '+5 hours')``.

        Считается по текущему моменту: зона школы фиксированная (UTC+5, без
        перехода на летнее время), но брать смещение из самой зоны надёжнее,
        чем зашивать пятёрку в запрос.
        """
        offset = datetime.now(tz=self._tz).utcoffset() or timedelta(0)
        hours = int(offset.total_seconds() // 3600)
        return f"{hours:+d} hours"

    def local(self, value: datetime | None) -> datetime | None:
        """Время в часовом поясе школы — в нём владелец и читает отчёты."""
        return value.astimezone(self._tz) if value is not None else None

    # ---------------------------------------------------------------- сводка
    def overview(self, *, days: int = 7) -> dict[str, Any]:
        """Числа для главной страницы."""
        since = (datetime.now(tz=_UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        channels = {
            row["chat_type"]: row["n"]
            for row in self._query(
                "SELECT chat_type, COUNT(*) AS n FROM conversation GROUP BY chat_type"
            )
        }
        cost_row = self._query(
            "SELECT COALESCE(SUM(cost_usd), 0), COALESCE(SUM(prompt_tokens + candidates_tokens), 0),"
            " COUNT(*) FROM llm_call WHERE created_at >= ?",
            (since,),
        )
        cost, tokens, calls = (cost_row[0] if cost_row else (0, 0, 0))

        return {
            "clients_total": self._scalar("SELECT COUNT(*) FROM conversation"),
            "clients_new": self._scalar(
                "SELECT COUNT(*) FROM conversation WHERE created_at >= ?", (since,)
            ),
            "messages_in": self._scalar(
                "SELECT COUNT(*) FROM message WHERE direction='in' AND text_raw IS NOT NULL"
            ),
            "messages_week": self._scalar(
                "SELECT COUNT(*) FROM message WHERE created_at >= ? AND text_raw IS NOT NULL",
                (since,),
            ),
            "leads_total": self._scalar("SELECT COUNT(*) FROM lead"),
            "leads_week": self._scalar("SELECT COUNT(*) FROM lead WHERE created_at >= ?", (since,)),
            "escalations": self._scalar("SELECT COUNT(*) FROM lead WHERE escalation = 1"),
            "paused_now": self._scalar(
                "SELECT COUNT(*) FROM escalation_state WHERE paused = 1 AND"
                " (paused_until IS NULL OR paused_until > ?)",
                (datetime.now(tz=_UTC).strftime("%Y-%m-%d %H:%M:%S"),),
            ),
            "channels": channels,
            "llm_cost_usd": float(cost or 0),
            "llm_tokens": int(tokens or 0),
            "llm_calls": int(calls or 0),
            "days": days,
        }

    def daily_counts(self, *, days: int = 14) -> list[tuple[str, int, int]]:
        """По дням: сколько диалогов начато и сколько заявок. Для графика.

        Сутки считаются местные, костанайские. В базе время хранится в UTC, а
        Костанай — UTC+5: без сдвига вечерние обращения попадали бы в следующий
        день, и владелец видел бы всплеск активности в ночь на понедельник.
        """
        shift = self._utc_offset_sql()
        since = (datetime.now(tz=_UTC) - timedelta(days=days + 1)).strftime("%Y-%m-%d")
        convs = {
            row[0]: row[1]
            for row in self._query(
                f"SELECT date(created_at, '{shift}'), COUNT(*) FROM conversation"
                f" WHERE created_at >= ? GROUP BY date(created_at, '{shift}')",
                (since,),
            )
        }
        leads = {
            row[0]: row[1]
            for row in self._query(
                f"SELECT date(created_at, '{shift}'), COUNT(*) FROM lead"
                f" WHERE created_at >= ? GROUP BY date(created_at, '{shift}')",
                (since,),
            )
        }
        out: list[tuple[str, int, int]] = []
        today = datetime.now(tz=self._tz).date()
        for offset in range(days - 1, -1, -1):
            day = (today - timedelta(days=offset)).isoformat()
            out.append((day, convs.get(day, 0), leads.get(day, 0)))
        return out

    # --------------------------------------------------------------- клиенты
    def _client_filter(
        self, *, channel: str, search: str, only_leads: bool
    ) -> tuple[str, list[Any]]:
        """Условие выборки клиентов. Общее для списка и для подсчёта.

        Одно место на оба запроса не ради краткости: разъехавшись, фильтр и
        счётчик дают «показано 50 из 3», и постраничная навигация врёт.
        """
        where: list[str] = []
        params: list[Any] = []
        if channel:
            where.append("c.chat_type = ?")
            params.append(channel)
        if search:
            where.append(
                "(rulower(COALESCE(c.contact_name,'')) LIKE ? OR c.chat_id LIKE ?"
                " OR rulower(COALESCE(c.instagram_username,'')) LIKE ?"
                " OR COALESCE(c.phone_e164,'') LIKE ?)"
            )
            needle = f"%{search.lower()}%"
            params.extend([needle, needle, needle, needle])
        if only_leads:
            where.append("EXISTS (SELECT 1 FROM lead l WHERE l.conversation_id = c.id)")
        return ("WHERE " + " AND ".join(where)) if where else "", params

    def clients(
        self,
        *,
        channel: str = "",
        search: str = "",
        only_leads: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Client]:
        """Список клиентов с последним сообщением и признаком заявки."""
        clause, params = self._client_filter(channel=channel, search=search, only_leads=only_leads)

        rows = self._query(
            f"""
            SELECT c.id, c.conv_key, c.chat_type, c.chat_id, c.contact_name, c.instagram_username,
                   c.phone_e164, c.lang, c.state, c.msg_in_count, c.msg_out_count,
                   c.first_inbound_at, c.last_inbound_at,
                   (SELECT m.text_raw FROM message m
                     WHERE m.conversation_id = c.id AND m.text_raw IS NOT NULL AND m.text_raw <> ''
                     ORDER BY m.created_at DESC LIMIT 1) AS last_text,
                   EXISTS (SELECT 1 FROM lead l WHERE l.conversation_id = c.id) AS has_lead,
                   COALESCE(e.paused, 0) AS paused, e.paused_until
              FROM conversation c
              LEFT JOIN escalation_state e ON e.conversation_id = c.id
              {clause}
             ORDER BY COALESCE(c.last_inbound_at, c.created_at) DESC
             LIMIT ? OFFSET ?
            """,
            [*params, int(limit), int(offset)],
        )
        return [self._client(row) for row in rows]

    def clients_count(self, *, channel: str = "", search: str = "", only_leads: bool = False) -> int:
        """Сколько всего клиентов подходит под фильтр — для постраничной навигации.

        Считает база. Раньше здесь выбирались все строки и мерилась длина списка:
        на сотне диалогов незаметно, на десяти тысячах — секунды и мегабайты
        памяти на каждое открытие страницы.
        """
        clause, params = self._client_filter(channel=channel, search=search, only_leads=only_leads)
        return self._scalar(f"SELECT COUNT(*) FROM conversation c {clause}", params)

    def client(self, conv_id: str) -> Client | None:
        """Один клиент по идентификатору диалога."""
        rows = self._query(
            """
            SELECT c.id, c.conv_key, c.chat_type, c.chat_id, c.contact_name, c.instagram_username,
                   c.phone_e164, c.lang, c.state, c.msg_in_count, c.msg_out_count,
                   c.first_inbound_at, c.last_inbound_at,
                   NULL AS last_text,
                   EXISTS (SELECT 1 FROM lead l WHERE l.conversation_id = c.id) AS has_lead,
                   COALESCE(e.paused, 0) AS paused, e.paused_until
              FROM conversation c
              LEFT JOIN escalation_state e ON e.conversation_id = c.id
             WHERE c.id = ?
            """,
            (conv_id,),
        )
        return self._client(rows[0]) if rows else None

    def dialog(self, conv_id: str, *, limit: int = 500) -> list[Dialog]:
        """Переписка целиком: и что писал клиент, и что отвечал бот.

        Реплики собираются из **двух** таблиц, и иначе никак: входящие лежат в
        ``message``, а ответы бота — в ``outbox_message``, потому что отправкой
        занимается воркер, а не пайплайн. В ``message`` для исходящего остаётся
        только служебная строка без текста. Показывать одну таблицу значило бы
        отдать оператору половину разговора — реплики клиента без ответов,
        по которым невозможно понять, что вообще произошло.

        Служебные строки истории модели отбрасываются: в них вызовы
        инструментов, человеку они говорят меньше, чем занимают места.
        """
        rows = self._query(
            """
            SELECT direction, author, author_name, text_raw, msg_type, status, created_at
              FROM message
             WHERE conversation_id = ? AND text_raw IS NOT NULL AND TRIM(text_raw) <> ''
             ORDER BY created_at
             LIMIT ?
            """,
            (conv_id, int(limit)),
        )
        dialog = [
            Dialog(
                direction=row["direction"],
                author=row["author"],
                author_name=row["author_name"],
                text=row["text_raw"] or "",
                msg_type=row["msg_type"],
                status=row["status"],
                created_at=_parse_ts(row["created_at"]),
            )
            for row in rows
        ]
        dialog.extend(self._bot_replies(conv_id, limit=limit))
        # Ключ сортировки — время; строки без времени уводим в начало, чтобы
        # сравнение не падало на None.
        dialog.sort(key=lambda item: item.created_at or datetime.min.replace(tzinfo=_UTC))
        return [
            Dialog(
                direction=item.direction,
                author=item.author,
                author_name=item.author_name,
                text=item.text,
                msg_type=item.msg_type,
                status=item.status,
                created_at=self.local(item.created_at),
            )
            for item in dialog
        ]

    def _bot_replies(self, conv_id: str, *, limit: int) -> list[Dialog]:
        """Ответы бота из очереди отправки."""
        rows = self._query(
            """
            SELECT payload, state, created_at
              FROM outbox_message
             WHERE conversation_id = ?
             ORDER BY created_at
             LIMIT ?
            """,
            (conv_id, int(limit)),
        )
        out: list[Dialog] = []
        for row in rows:
            raw = row["payload"]
            try:
                payload = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw or {})
            except (ValueError, TypeError):
                continue
            text = str(payload.get("text") or "").strip()
            artifact = payload.get("artifact_id")
            if not text and artifact:
                # Материал без подписи: показываем, что именно ушло клиенту.
                text = f"[отправлен материал: {artifact}]"
            if not text:
                continue
            out.append(
                Dialog(
                    direction="out",
                    author="bot",
                    author_name=None,
                    text=text,
                    msg_type=str(payload.get("kind") or "bot_reply"),
                    status=str(row["state"] or ""),
                    created_at=_parse_ts(row["created_at"]),
                )
            )
        return out

    # ----------------------------------------------------------------- лиды
    def leads(self, *, status: str = "", limit: int = 200) -> list[LeadRow]:
        """Заявки, свежие сверху."""
        clause, params = ("WHERE status = ?", [status]) if status else ("", [])
        return self._lead_rows(clause, params, limit=limit)

    def _lead_rows(self, clause: str, params: list[Any], *, limit: int) -> list[LeadRow]:
        """Заявки по готовому условию. Одно место разбора строки на все выборки."""
        rows = self._query(
            f"""
            SELECT id, conversation_id, created_at, channel, parent_name, phone, child_name,
                   child_age, child_gender, district, gym_id, trial_slot_text, motivation,
                   main_objection, status, escalation, lang
              FROM lead {clause}
             ORDER BY created_at DESC LIMIT ?
            """,
            [*params, int(limit)],
        )
        return [
            LeadRow(
                id=row["id"],
                conversation_id=row["conversation_id"],
                created_at=self.local(_parse_ts(row["created_at"])),
                channel=row["channel"],
                parent_name=row["parent_name"],
                phone=row["phone"],
                child_name=row["child_name"],
                child_age=row["child_age"],
                child_gender=row["child_gender"],
                district=row["district"],
                gym_id=row["gym_id"],
                trial_slot_text=row["trial_slot_text"],
                motivation=row["motivation"],
                main_objection=row["main_objection"],
                status=row["status"],
                escalation=bool(row["escalation"]),
                lang=row["lang"],
            )
            for row in rows
        ]

    def lead_for(self, conv_id: str) -> LeadRow | None:
        """Заявка по диалогу, если она есть."""
        rows = self._lead_rows("WHERE conversation_id = ?", [conv_id], limit=1)
        return rows[0] if rows else None

    def lead_statuses(self) -> list[tuple[str, int]]:
        """Сколько заявок в каждом статусе — для фильтра."""
        return [(row[0], row[1]) for row in self._query(
            "SELECT status, COUNT(*) FROM lead GROUP BY status ORDER BY COUNT(*) DESC"
        )]

    # -------------------------------------------------------------- операции
    def resume_bot(self, conv_id: str, conv_key: str) -> bool:
        """Возвращает бота в диалог: снимает паузу в обоих хранилищах.

        Пауза живёт в строке ``escalation_state`` и в ключе быстрого пути. Снять
        только строку значит оставить бота молчащим до конца окна, снять только
        ключ — получить возврат паузы при следующем чтении из базы.
        """
        if not self.exists:
            return False
        now = datetime.now(tz=_UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
        try:
            with self._connect(write=True) as conn:
                conn.execute(
                    "UPDATE escalation_state SET paused = 0, paused_until = NULL,"
                    " pause_reason = NULL, resumed_at = ? WHERE conversation_id = ?",
                    (now, conv_id),
                )
                # Тот же путь, которым снимает паузу сам бот: строка и состояние
                # диалога. Иначе напоминания остались бы выключенными навсегда.
                # Возвращаются оба нерабочих состояния — и «отвечает человек», и
                # «передан администратору»: бот при снятии паузы не различает их
                # тоже. «Завершён» не трогаем: это осознанный конец разговора.
                conn.execute(
                    "UPDATE conversation SET state = 'active' WHERE id = ?"
                    " AND state IN ('paused_operator', 'escalated')",
                    (conv_id,),
                )
        except sqlite3.Error as exc:
            _log.error("crm_resume_failed", error=str(exc), conversation_id=conv_id)
            return False
        # Ключ снимаем в любом случае: строки в базе может не быть вовсе (бота
        # ставили на паузу только быстрым путём), и тогда снять нужно именно его.
        self._drop_state_key(f"pause:{conv_key}")
        return True

    def pause_bot(self, conv_id: str, conv_key: str, *, minutes: int = 60) -> bool:
        """Ставит бота на паузу вручную: дальше в диалоге отвечает человек."""
        if not self.exists:
            return False
        until = datetime.now(tz=_UTC) + timedelta(minutes=max(1, int(minutes)))
        stamp = until.strftime("%Y-%m-%d %H:%M:%S.%f")
        try:
            with self._connect(write=True) as conn:
                changed = conn.execute(
                    "UPDATE escalation_state SET paused = 1, paused_until = ?,"
                    " pause_reason = 'manual' WHERE conversation_id = ?",
                    (stamp, conv_id),
                ).rowcount
                if not changed:
                    conn.execute(
                        "INSERT INTO escalation_state (conversation_id, paused, paused_until,"
                        " pause_reason, escalation_count, resume_policy)"
                        " VALUES (?, 1, ?, 'manual', 0, 'manual')",
                        (conv_id, stamp),
                    )
                # Состояние диалога — вторая половина паузы, и без неё бот
                # замолкает лишь наполовину: напоминания смотрят именно сюда и
                # уходили клиенту поверх разговора, начатого человеком.
                conn.execute(
                    "UPDATE conversation SET state = 'paused_operator' WHERE id = ?",
                    (conv_id,),
                )
        except sqlite3.Error as exc:
            _log.error("crm_pause_failed", error=str(exc), conversation_id=conv_id)
            return False
        self._set_state_key(f"pause:{conv_key}", until)
        return True

    def close_dialog(self, conv_id: str) -> bool:
        """Завершает диалог: следующее сообщение клиента начнётся с меню.

        Меню из четырёх пунктов показывается один раз на разговор — на втором
        «здравствуйте» подряд оно выглядело бы как сброс. Но разговор когда-то
        заканчивается, и решает это человек: после завершения клиент,
        написавший снова, начинает как новый.
        """
        if not self.exists:
            return False
        try:
            with self._connect(write=True) as conn:
                changed = conn.execute(
                    "UPDATE conversation SET state = 'closed' WHERE id = ?", (conv_id,)
                ).rowcount
        except sqlite3.Error as exc:
            _log.error("crm_close_failed", error=str(exc), conversation_id=conv_id)
            return False
        return bool(changed)

    # ------------------------------------------------------- здоровье модели
    def llm_health(self, *, hours: int = 1) -> dict[str, Any]:
        """Сколько вызовов модели за период, сколько с ошибкой и какой.

        Ключ может быть задан и при этом не работать — кончились кредиты,
        отозван, исчерпана квота. Настройки об этом не скажут ничего, а доля
        отказов скажет сразу.
        """
        edge = (datetime.now(tz=_UTC) - timedelta(hours=max(1, int(hours)))).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )
        rows = self._query(
            "SELECT COUNT(*) AS calls,"
            " SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors"
            "  FROM llm_call WHERE created_at >= ?",
            (edge,),
        )
        calls = int(rows[0]["calls"] or 0) if rows else 0
        errors = int(rows[0]["errors"] or 0) if rows else 0
        last = self._query(
            "SELECT error FROM llm_call WHERE error IS NOT NULL AND created_at >= ?"
            " ORDER BY created_at DESC LIMIT 1",
            (edge,),
        )
        return {
            "calls": calls,
            "errors": errors,
            "last_error": str(last[0]["error"]) if last else "",
        }

    # ---------------------------------------------------- здоровье отправки
    def stuck_outbox(self, *, minutes: int = 10) -> int:
        """Сколько сообщений ждут отправки дольше положенного.

        Очередь подметается раз в минуту, поэтому всё, что старше нескольких
        минут, — признак неисправности: не работает отправщик, недействителен
        ключ канала, не тот номер. Клиент этих сообщений не получил.
        """
        edge = (datetime.now(tz=_UTC) - timedelta(minutes=max(1, int(minutes)))).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )
        rows = self._query(
            "SELECT COUNT(*) AS n FROM outbox_message"
            " WHERE state IN ('pending', 'sending') AND created_at < ?",
            (edge,),
        )
        return int(rows[0]["n"]) if rows else 0

    def failed_outbox(self, *, hours: int = 24) -> tuple[int, str] | None:
        """Сколько сообщений не доставлено за период и последняя причина."""
        edge = (datetime.now(tz=_UTC) - timedelta(hours=max(1, int(hours)))).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )
        rows = self._query(
            "SELECT COUNT(*) AS n, MAX(updated_at) AS last FROM outbox_message"
            " WHERE state IN ('failed', 'skipped') AND updated_at >= ?",
            (edge,),
        )
        count = int(rows[0]["n"]) if rows else 0
        if not count:
            return None
        reason_rows = self._query(
            "SELECT last_error FROM outbox_message"
            " WHERE state IN ('failed', 'skipped') AND updated_at >= ?"
            " ORDER BY updated_at DESC LIMIT 1",
            (edge,),
        )
        reason = str(reason_rows[0]["last_error"] or "причина не записана") if reason_rows else ""
        return count, reason or "причина не записана"

    # ------------------------------------------------- ответ клиенту из CRM
    def reply_to_client(self, conv_id: str, text: str, *, soft_limit: int = 900) -> str | None:
        """Кладёт сообщение оператора в очередь отправки. ``None`` — успех.

        Иначе возвращается человекочитаемая причина отказа.

        До этого ответить клиенту из CRM было нельзя вовсе: владелец видел
        переписку и эскалацию, но писать шёл в WhatsApp с телефона. Строка в
        ``outbox_message`` — тот же путь, которым отправляет сам бот, поэтому
        отправщик подберёт её своим циклом и правила канала соблюдёт сам.

        Проверки здесь не дублируют отправщик, а спасают оператора от
        молчаливого отказа: строку с закрытым окном Instagram он бы пропустил,
        а оператор считал бы, что ответил.
        """
        from app.channels.outbound import check_send_allowed, split_text, text_limits
        from app.types import ChannelKind, OutboundKind

        body = (text or "").strip()
        if not body:
            return "Пустое сообщение отправлять нечего."
        if not self.exists:
            return "База диалогов недоступна."

        conv = self._conversation_row(conv_id)
        if conv is None:
            return "Диалог не найден."

        try:
            channel = ChannelKind(str(conv["chat_type"] or "").strip().lower())
        except ValueError:
            return f"Неизвестный канал диалога: {conv['chat_type']!r}."
        if channel not in _REPLYABLE_CHANNELS:
            # Telegram-бот отправляет из своего цикла и очередь не читает:
            # строка осталась бы в базе навсегда, а оператор ждал бы доставки.
            return "Отсюда можно писать только в WhatsApp и Instagram."

        canonical = _canonical_uuid(conv_id)
        if canonical is None:
            # Без идентификатора отправщик не узнает, когда клиент писал, а для
            # Instagram «неизвестно» означает «мы пишем первыми» — строка будет
            # молча пропущена, и оператор об этом не узнает.
            return "У диалога испорченный идентификатор — отправка невозможна."

        now = datetime.now(tz=_UTC)
        denial = check_send_allowed(
            channel=channel,
            last_inbound_at=_parse_ts(conv["last_inbound_at"]),
            now=now,
            kind=OutboundKind.OPERATOR_REPLY,
        )
        if denial is not None:
            return _DENIAL_TEXTS.get(denial, f"Канал не принимает сообщение: {denial}.")

        soft, hard = text_limits(channel, soft_limit=soft_limit)
        parts = split_text(body, channel=channel, soft_limit=soft, hard_limit=hard, max_parts=10)
        if not parts:
            return "Пустое сообщение отправлять нечего."

        stamp = now.strftime("%Y-%m-%d %H:%M:%S.%f")
        try:
            with self._connect(write=True) as conn:
                for index, part in enumerate(parts):
                    # SQLAlchemy хранит UUID в SQLite как 32 шестнадцатеричных
                    # знака без дефисов. Строка с дефисами находилась выборкой
                    # отправщика, но `session.get` по ней ничего не возвращал:
                    # сообщение легло бы в базу и не ушло никуда.
                    crm_message_id = uuid4().hex
                    payload = {
                        # В полезной нагрузке — канонический вид с дефисами:
                        # её разбирает pydantic, а не SQLite.
                        "crm_message_id": str(UUID(crm_message_id)),
                        "conversation_id": canonical,
                        "channel_id": conv["channel_id"],
                        "channel": channel.value,
                        "chat_id": conv["chat_id"],
                        "lang": conv["lang"] or "ru",
                        "kind": OutboundKind.OPERATOR_REPLY.value,
                        "text": part,
                        "content_uri": None,
                        "artifact_id": None,
                        "ref_message_id": None,
                        # В проекте это поле всегда false: счётчик неотвеченных
                        # в Wazzup ведут операторы, и гасить его отправкой из
                        # CRM значит прятать от них диалог.
                        "clear_unanswered": False,
                        # Части одного ответа не должны прийти вперемешку.
                        "delay_ms": index * 400,
                    }
                    conn.execute(
                        "INSERT INTO outbox_message (id, conversation_id, crm_message_id,"
                        " payload, state, attempts, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)",
                        (uuid4().hex, conv_id, crm_message_id, json.dumps(payload), stamp, stamp),
                    )
        except sqlite3.Error as exc:
            _log.error("crm_reply_failed", error=str(exc), conversation_id=conv_id)
            return "Не удалось записать сообщение в очередь отправки."

        _log.info("crm_reply_queued", conversation_id=conv_id, parts=len(parts))
        return None

    def _conversation_row(self, conv_id: str) -> sqlite3.Row | None:
        """Строка диалога с полями, нужными для отправки."""
        rows = self._query(
            "SELECT id, conv_key, chat_type, chat_id, channel_id, lang, last_inbound_at"
            "  FROM conversation WHERE id = ? LIMIT 1",
            (conv_id,),
        )
        return rows[0] if rows else None

    # --------------------------------------------------- быстрый путь паузы
    def _drop_state_key(self, key: str) -> None:
        """Удаляет ключ из ``state.db``. Отсутствие базы — не ошибка."""
        if self._state_db is None or not self._state_db.is_file():
            return
        try:
            with sqlite3.connect(str(self._state_db), timeout=5.0) as conn:
                conn.execute("DELETE FROM kv_state WHERE key = ?", (key,))
        except sqlite3.Error:
            return

    def _set_state_key(self, key: str, until: datetime) -> None:
        """Ставит ключ паузы с тем же сроком, что и строка в базе."""
        if self._state_db is None or not self._state_db.is_file():
            return
        try:
            with sqlite3.connect(str(self._state_db), timeout=5.0) as conn:
                conn.execute(
                    "INSERT INTO kv_state(key, value, expires_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                    " expires_at = excluded.expires_at",
                    (key, "1", until.timestamp()),
                )
        except sqlite3.Error:
            return

    # ------------------------------------------------------------ внутреннее
    def _client(self, row: sqlite3.Row) -> Client:
        paused_until = _parse_ts(row["paused_until"])
        active_pause = bool(row["paused"]) and (
            paused_until is None or paused_until > datetime.now(tz=_UTC)
        )
        return Client(
            id=row["id"],
            conv_key=row["conv_key"],
            channel=row["chat_type"] or "",
            chat_id=row["chat_id"],
            name=row["contact_name"] or "Без имени",
            username=row["instagram_username"],
            phone=row["phone_e164"],
            lang=row["lang"],
            state=row["state"],
            msg_in=row["msg_in_count"] or 0,
            msg_out=row["msg_out_count"] or 0,
            first_at=self.local(_parse_ts(row["first_inbound_at"])),
            last_at=self.local(_parse_ts(row["last_inbound_at"])),
            # sqlite3.Row: «in row» проверяет значения, имена колонок только в .keys()
            last_text=row["last_text"] if "last_text" in row.keys() else None,  # noqa: SIM118
            has_lead=bool(row["has_lead"]),
            paused=active_pause,
            paused_until=self.local(paused_until),
        )
