"""Администраторы и настройки бота в SQLite.

Отдельный маленький файл рядом с основной базой. Почему не переменные окружения:
чтобы добавить администратора или выключить напоминания, владельцу пришлось бы
править ``.env`` и перезапускать бота — то есть звать программиста. Здесь всё
меняется прямо в чате и переживает перезапуск.

Пароль в базе не хранится: сверяется хеш, а сам пароль живёт в настройках
процесса. Выданные права хранятся по Telegram-id — их и проверяет бот.
"""

from __future__ import annotations

import hmac
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Final

from app.logging_conf import get_logger

__all__ = ["AdminStore", "AdminUser", "SETTING_SPECS", "SettingSpec"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AdminUser:
    """Кому разрешено управлять ботом."""

    telegram_id: int
    title: str
    added_at: str


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """Настройка, которую владелец может менять из чата."""

    key: str
    title: str
    kind: str  # bool | time_range | text
    default: str
    hint: str


#: Закрытый список настраиваемого. Всё остальное — код и база знаний, их из чата
#: менять нельзя: там правки требуют валидации, а не свободного ввода.
SETTING_SPECS: Final[tuple[SettingSpec, ...]] = (
    SettingSpec(
        key="followup_enabled",
        title="Напоминания клиентам",
        kind="bool",
        default="on",
        hint="Бот один раз напоминает о себе тем, кто не ответил. Стоп-слова отключают навсегда. Работает на канале WhatsApp/Instagram; в Telegram напоминания пока не отправляются — очередь напоминаний живёт в фоновом воркере.",
    ),
    SettingSpec(
        key="quiet_hours",
        title="Тихие часы",
        kind="time_range",
        default="21:00-09:00",
        hint="В это время бот не пишет первым. Часовой пояс Костаная. Действует на напоминания, то есть вместе с предыдущей настройкой.",
    ),
    SettingSpec(
        key="work_hours",
        title="Часы работы администратора",
        kind="time_range",
        default="10:00-20:00",
        hint="Бот говорит клиенту, когда с ним свяжутся.",
    ),
    SettingSpec(
        key="lead_notify",
        title="Уведомления о лидах",
        kind="bool",
        default="on",
        hint="Карточка новой заявки приходит всем администраторам.",
    ),
    SettingSpec(
        key="trial_free",
        title="Первое занятие бесплатное",
        kind="bool",
        default="on",
        hint="Выключите, если пробное станет платным — бот перестанет его обещать.",
    ),
)

_SPEC_BY_KEY: Final[dict[str, SettingSpec]] = {s.key: s for s in SETTING_SPECS}


class AdminStore:
    """Список администраторов и настройки бота. Один файл SQLite."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS admin_user ("
            " telegram_id INTEGER PRIMARY KEY,"
            " title TEXT NOT NULL DEFAULT '',"
            " added_at TEXT NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS bot_setting ("
            " key TEXT PRIMARY KEY,"
            " value TEXT NOT NULL,"
            " updated_at TEXT NOT NULL)"
        )
        self._conn.commit()

    # ------------------------------------------------------------ админы
    def is_admin(self, telegram_id: int | str | None) -> bool:
        """Есть ли у пользователя права управления ботом."""
        try:
            uid = int(telegram_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        row = self._conn.execute(
            "SELECT 1 FROM admin_user WHERE telegram_id = ?", (uid,)
        ).fetchone()
        return row is not None

    def grant(self, telegram_id: int, title: str = "") -> bool:
        """Выдаёт права. ``False`` — они уже были."""
        if self.is_admin(telegram_id):
            return False
        self._conn.execute(
            "INSERT INTO admin_user(telegram_id, title, added_at) VALUES (?, ?, ?)",
            (int(telegram_id), (title or "").strip()[:80],
             datetime.now(tz=timezone.utc).isoformat(timespec="seconds")),
        )
        self._conn.commit()
        _log.info("admin_granted", telegram_id=int(telegram_id))
        return True

    def revoke(self, telegram_id: int) -> bool:
        """Отзывает права. ``False`` — такого администратора не было.

        Последнего администратора убрать нельзя: иначе управление ботом
        потеряется, и вернуть его можно будет только паролем заново.
        """
        if len(self.admins()) <= 1 and self.is_admin(telegram_id):
            return False
        cur = self._conn.execute(
            "DELETE FROM admin_user WHERE telegram_id = ?", (int(telegram_id),)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def admins(self) -> list[AdminUser]:
        """Все администраторы в порядке добавления."""
        rows = self._conn.execute(
            "SELECT telegram_id, title, added_at FROM admin_user ORDER BY added_at"
        ).fetchall()
        return [AdminUser(telegram_id=r[0], title=r[1], added_at=r[2]) for r in rows]

    @staticmethod
    def password_matches(candidate: str, expected: str) -> bool:
        """Сравнение пароля постоянным временем.

        Сравнивать строки обычным ``==`` нельзя: время сравнения зависит от того,
        сколько символов совпало, и по нему пароль подбирается посимвольно.
        """
        if not expected:
            return False
        left = sha256((candidate or "").strip().encode()).digest()
        right = sha256(expected.strip().encode()).digest()
        return hmac.compare_digest(left, right)

    # ---------------------------------------------------------- настройки
    def get(self, key: str) -> str:
        """Значение настройки; если не задано — значение по умолчанию."""
        spec = _SPEC_BY_KEY.get(key)
        row = self._conn.execute(
            "SELECT value FROM bot_setting WHERE key = ?", (key,)
        ).fetchone()
        if row:
            return str(row[0])
        return spec.default if spec else ""

    def set(self, key: str, value: str) -> None:
        """Сохраняет настройку. Неизвестный ключ не принимается."""
        if key not in _SPEC_BY_KEY:
            raise KeyError(f"неизвестная настройка: {key}")
        self._conn.execute(
            "INSERT INTO bot_setting(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, datetime.now(tz=timezone.utc).isoformat(timespec="seconds")),
        )
        self._conn.commit()
        _log.info("setting_changed", key=key, value=value)

    def all_settings(self) -> list[tuple[SettingSpec, str]]:
        """Все настройки с текущими значениями — для показа в чате."""
        return [(spec, self.get(spec.key)) for spec in SETTING_SPECS]

    def close(self) -> None:
        """Закрывает соединение."""
        self._conn.close()
