"""Пути и параметры CRM. Ничего не угадывает молча — всё видно в интерфейсе."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

__all__ = ["CrmConfig"]

#: Кандидаты на базу диалогов, в порядке предпочтения. Telegram-бот пишет в
#: ``telegram.db``, продовый путь через Wazzup — в ``bot.db``.
_DB_CANDIDATES: tuple[str, ...] = ("telegram.db", "bot.db")


@dataclass(frozen=True, slots=True)
class CrmConfig:
    """Конфигурация одного запуска CRM."""

    root: Path
    kb_dir: Path
    media_dir: Path
    schema_version: int
    admin_db: Path
    state_db: Path
    bot_db: Path
    password: str
    timezone: str
    secret_key: str

    @property
    def backups_dir(self) -> Path:
        """Куда складываются резервные копии базы знаний перед каждой правкой."""
        return self.kb_dir / ".backups"

    @classmethod
    def from_env(cls, root: Path | None = None) -> CrmConfig:
        """Собирает конфигурацию из ``.env``/окружения и настроек бота.

        Пароль берётся тот же, что открывает ``/admin`` в Telegram: заводить для
        CRM второй пароль значило бы, что владелец школы держит в голове два.
        """
        base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
        from app.config import get_settings

        settings = get_settings()

        def _abs(value: Path) -> Path:
            return value if value.is_absolute() else base / value

        explicit = os.environ.get("CRM_BOT_DB", "").strip()
        if explicit:
            bot_db = _abs(Path(explicit))
        else:
            bot_db = next(
                (base / "data" / name for name in _DB_CANDIDATES if (base / "data" / name).is_file()),
                base / "data" / "bot.db",
            )

        # Ключ сессии по умолчанию — случайный на процесс: постоянный ключ в
        # исходниках означал бы, что чужую сессию можно подделать, зная репозиторий.
        secret = os.environ.get("CRM_SECRET_KEY", "").strip() or secrets.token_hex(32)

        return cls(
            root=base,
            kb_dir=_abs(settings.kb_dir),
            media_dir=_abs(settings.media_dir),
            schema_version=settings.kb_schema_version,
            admin_db=_abs(settings.admin_db_path),
            state_db=_abs(settings.state_sqlite_path),
            bot_db=bot_db,
            password=settings.admin_password,
            timezone=settings.timezone,
            secret_key=secret,
        )
