# ФАЙЛ ЦЕЛИКОМ: app/config.py
"""Настройки приложения. Единственная точка чтения окружения.

``os.environ`` где-либо ещё в коде запрещён. Модуль обязан импортироваться при
полностью пустом окружении: обязательность значений проверяет
:meth:`Settings.startup_blockers`, а не валидатор поля.

Особенности Railway, ради которых написан этот модуль:

* порт приходит в переменной ``PORT`` — слушать нужно ``0.0.0.0:$PORT``;
* плагин Postgres выдаёт ``postgresql://…`` (иногда legacy ``postgres://…``),
  а SQLAlchemy async требует ``postgresql+asyncpg://…`` — нормализуем сами,
  чтобы заказчик вставлял переменную Railway без правки;
* плагин Redis выдаёт готовый ``REDIS_URL`` — используется как есть;
* ``INLINE_WORKER=true`` поднимает воркер в одном процессе с API, чтобы на старте
  хватило одной службы.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.types import ChannelKind, ConfigError

#: Префиксы, которые Railway/Neon/Heroku отдают вместо async-диалекта.
_PG_PREFIXES: Final[tuple[str, ...]] = ("postgres://", "postgresql://")

#: Параметры строки подключения, которые asyncpg не понимает и роняет соединение.
_PG_DROP_QUERY: Final[frozenset[str]] = frozenset(
    {"sslmode", "channel_binding", "target_session_attrs", "gssencmode"}
)


def normalize_database_url(raw: str) -> str:
    """Приводит ``DATABASE_URL`` к async-диалекту SQLAlchemy.

    ``postgres://`` и ``postgresql://`` → ``postgresql+asyncpg://``;
    ``sqlite://`` → ``sqlite+aiosqlite://``; уже нормализованные URL не трогает.
    Из postgres-URL удаляются query-параметры, которые asyncpg не принимает
    (``sslmode`` и другие из :data:`_PG_DROP_QUERY`).

    Пустая строка возвращается как есть — обязательность проверяется отдельно.
    """
    value = (raw or "").strip()
    if not value:
        return value

    # sqlite: приводим к async-драйверу, если он не задан явно.
    if value.startswith("sqlite+"):
        return value
    if value.startswith("sqlite:"):
        return "sqlite+aiosqlite:" + value[len("sqlite:"):]

    # postgres: работаем строго по префиксу, чтобы пароль с '@' и '/' пережил замену.
    if value.startswith("postgresql+"):
        return value
    for prefix in _PG_PREFIXES:
        if value.startswith(prefix):
            rest = value[len(prefix):]
            return _drop_query_params("postgresql+asyncpg://" + rest, _PG_DROP_QUERY)
    return value


def _drop_query_params(url: str, names: frozenset[str]) -> str:
    """Удаляет из query-строки перечисленные параметры, сохраняя порядок прочих."""
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in names]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


class Settings(BaseSettings):
    """Все настройки бота. Имена ENV совпадают с именами полей в верхнем регистре."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- окружение и HTTP ---------------------------------------------------
    app_env: Literal["local", "staging", "prod"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    host: str = "0.0.0.0"
    #: Railway передаёт порт только через PORT. Захардкоженный порт = провал деплоя.
    port: int = Field(default=8000, validation_alias=AliasChoices("PORT", "APP_PORT"))
    #: Внешний https-адрес службы, база для contentUri: https://<service>.up.railway.app
    public_base_url: str = "http://localhost:8000"

    # --- Wazzup24 -----------------------------------------------------------
    wazzup_api_key: str = ""
    wazzup_api_base: str = "https://api.wazzup24.com/v3"
    #: 32 байта base64url, попадает в путь вебхука. Сравнение — hmac.compare_digest.
    wazzup_webhook_secret: str = ""
    wazzup_channel_id_whatsapp: str | None = None
    wazzup_channel_id_instagram: str | None = None
    #: Всегда false: автоответ не должен гасить счётчик неотвеченных.
    wazzup_clear_unanswered: Literal[False] = False
    wazzup_timeout_ms: int = 15000
    wazzup_send_max_attempts: int = 5
    wazzup_send_backoff_base_ms: int = 1000
    wazzup_register_webhook_on_start: bool = False

    # --- Telegram (прямой Bot API, без агрегатора) --------------------------
    #: Токен от @BotFather. Пустой — канал Telegram просто не поднимается.
    #: Нужен, чтобы показывать бота с телефона до подключения Wazzup, и как
    #: единственный канал, куда видео уходит вложением, а не ссылкой.
    telegram_bot_token: str = ""
    #: Пауза между опросами getUpdates, секунды (long polling).
    telegram_poll_timeout_s: int = 25
    #: Telegram-id тех, кому разрешено править бота, через запятую. Список
    #: стартовый: дальше администраторы добавляются паролем прямо в чате,
    #: и выданные права живут в SQLite, а не в этой строке.
    telegram_admin_ids: str = ""
    #: Пароль входа в админку. Кто пришлёт его боту — становится администратором.
    #: Пустой пароль закрывает этот способ полностью.
    admin_password: str = "Azamat65"
    #: Файл со списком администраторов и настройками бота.
    admin_db_path: Path = Path("data/admin.db")

    @property
    def admin_ids(self) -> tuple[int, ...]:
        """Разобранный список Telegram-id администраторов."""
        out: list[int] = []
        for chunk in (self.telegram_admin_ids or "").replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit():
                out.append(int(chunk))
        return tuple(out)

    # --- Gemini -------------------------------------------------------------
    gemini_api_key: str = ""
    gemini_model_primary: str = "gemini-3.5-flash-lite"
    gemini_model_fallback: str = "gemini-3.1-flash-lite"
    gemini_thinking_level: Literal["minimal", "low", "medium", "high"] = "minimal"
    #: Потолок ОДНОГО обращения к модели.
    gemini_timeout_ms: int = 30000
    gemini_max_output_tokens: int = 1024
    llm_max_tool_loops: int = 5
    #: Потолок ВСЕГО хода модели: витки tool-loop, повторы и запасная модель
    #: вместе. Без него ход стоил бы (llm_max_tool_loops + 1) × gemini_timeout_ms
    #: и переживал бы и таймаут задачи, и TTL лока диалога.
    llm_turn_budget_s: int = 75
    llm_history_turns: int = 20
    llm_daily_budget_usd: float = 5.0
    #: Цена за 1M токенов, USD. Нужна только для оценки в llm_call.cost_usd.
    llm_price_in_per_mtok: float = 0.30
    llm_price_out_per_mtok: float = 2.50

    # --- хранилище ----------------------------------------------------------
    #: Всё хранилище — один файл SQLite. Postgres проекту не нужен:
    #: нагрузка школы — десятки диалогов в сутки, а один файл проще
    #: и в деплое, и в резервном копировании.
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_echo: bool = False
    #: Redis не нужен: состояние живёт в SQLite. Поле оставлено на случай
    #: масштабирования на несколько процессов — пустое оно ничего не включает.
    redis_url: str = ""
    #: sqlite — рабочий режим (состояние переживает перезапуск, внешних служб нет);
    #: memory — тесты; redis — только если однажды понадобится несколько процессов.
    state_backend: Literal["sqlite", "memory", "redis"] = "sqlite"
    #: Файл TTL-состояния: дедуп, пауза бота, локи диалогов.
    state_sqlite_path: Path = Path("data/state.db")

    # --- воркер -------------------------------------------------------------
    #: true — воркер живёт в процессе API (одна служба Railway).
    inline_worker: bool = False
    worker_max_jobs: int = 10
    #: Потолок задачи воркера. Обязан быть БОЛЬШЕ, чем ожидание лока плюс бюджет
    #: хода: иначе задачу убьют посреди хода, и клиент не получит даже заглушки.
    worker_job_timeout_s: int = 180

    # --- база знаний и медиа ------------------------------------------------
    kb_dir: Path = Path("kb")
    media_dir: Path = Path("media")
    kb_schema_version: int = 1
    kb_hot_reload: bool = False
    #: Как часто проверять каталог ``kb/`` на правки из CRM и админки, секунды.
    #: ``0`` — не проверять: тогда правки доедут только после перезапуска.
    kb_watch_interval_s: int = 5
    media_token_ttl_s: int = 600
    #: Секрет подписи ссылок /media/{token}. Пустой — берётся wazzup_webhook_secret.
    media_token_secret: str = ""

    # --- поведение диалога --------------------------------------------------
    timezone: str = "Asia/Almaty"
    debounce_seconds: int = 5
    debounce_max_seconds: int = 8
    #: TTL лока диалога. Обязан покрывать самый долгий ход целиком
    #: (``llm_turn_budget_s`` плюс работа с БД), иначе лок протухнет на середине
    #: хода и следующее сообщение запустит второй ход параллельно.
    conv_lock_ttl_seconds: int = 120
    #: Сколько ждать освобождения лока, прежде чем признать сообщение отложенным.
    #: Должно быть не меньше ``llm_turn_budget_s``: тогда сообщение, пришедшее
    #: во время хода, дожидается своей очереди, а не теряется.
    conv_lock_wait_seconds: int = 80
    dedup_ttl_seconds: int = 86400
    soft_message_chars: int = 600
    max_messages_per_turn: int = 2
    second_message_delay_ms: int = 2000
    pause_operator_minutes: int = 30
    pause_user_request_minutes: int = 60
    pause_escalation_minutes: int = 60
    pause_postcheck_minutes: int = 30
    pause_llm_failure_minutes: int = 15
    #: Служебная строка оператора, снимающая паузу. Приходит эхом, клиенту не пересылается.
    operator_resume_command: str = "#бот"
    bot_miss_limit: int = 2
    rate_limit_inbound_per_conv: int = 20
    rate_limit_window_seconds: int = 300

    # --- follow-up ----------------------------------------------------------
    followup_enabled: bool = True
    followup_max_attempts: int = 2
    followup_quiet_hours_start: int = 21
    followup_quiet_hours_end: int = 9

    # --- уведомления менеджеру ---------------------------------------------
    manager_notify_channel: ChannelKind = ChannelKind.WHATSAPP
    #: chatId менеджера в выбранном канале (для WhatsApp — только цифры, 77xxxxxxxxx).
    manager_notify_target: str | None = None
    manager_notify_channel_id: str | None = None
    work_hours: str = "10:00-20:00"
    sla_minutes: int = 30

    # --- админка и метрики --------------------------------------------------
    admin_token: str | None = None
    metrics_enabled: bool = True

    @field_validator("database_url", mode="after")
    @classmethod
    def _normalize_db_url(cls, value: str) -> str:
        """Автонормализация URL Railway. См. :func:`normalize_database_url`."""
        return normalize_database_url(value)

    @property
    def is_sqlite(self) -> bool:
        """Тесты и локальный пилот идут на sqlite+aiosqlite."""
        return self.database_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql+asyncpg")

    @property
    def media_signing_key(self) -> str:
        """Ключ подписи ссылок на медиа; фолбэк — секрет вебхука."""
        return self.media_token_secret or self.wazzup_webhook_secret

    def webhook_path(self) -> str:
        """Путь вебхука с секретом: ``/wazzup/webhook/{secret}`` (≤ 200 символов вместе с доменом)."""
        return f"/wazzup/webhook/{self.wazzup_webhook_secret}"

    def webhook_url(self) -> str:
        """Полный публичный URL вебхука для ``PATCH /v3/webhooks``."""
        return f"{self.public_base_url.rstrip('/')}{self.webhook_path()}"

    def startup_blockers(self) -> list[str]:
        """Технические блокеры старта. Юридических блокеров в проекте нет.

        Для ``app_env != "local"`` обязательны: ``wazzup_api_key``,
        ``wazzup_webhook_secret`` (≥ 24 символов), ``gemini_api_key``,
        ``database_url``, ``redis_url`` при ``state_backend="redis"``,
        ``public_base_url`` на https, ``manager_notify_target``.
        Возвращает список человекочитаемых причин; пустой список = можно стартовать.
        """
        blockers: list[str] = []
        if not self.database_url:
            blockers.append("DATABASE_URL пуст")
        if self.state_backend == "redis" and not self.redis_url:
            blockers.append("STATE_BACKEND=redis, но REDIS_URL пуст")
        if self.state_backend == "sqlite" and not str(self.state_sqlite_path).strip():
            blockers.append("STATE_SQLITE_PATH пуст")
        blockers.extend(self.timing_blockers())
        if self.app_env == "local":
            return blockers

        if not self.wazzup_api_key:
            blockers.append("WAZZUP_API_KEY пуст")
        if len(self.wazzup_webhook_secret) < 24:
            blockers.append("WAZZUP_WEBHOOK_SECRET короче 24 символов")
        if not self.gemini_api_key:
            blockers.append("GEMINI_API_KEY пуст")
        if not self.public_base_url.startswith("https://"):
            blockers.append("PUBLIC_BASE_URL обязан быть https")
        if not self.manager_notify_target:
            blockers.append("MANAGER_NOTIFY_TARGET пуст: некому слать лиды")
        if self.admin_token is not None and 0 < len(self.admin_token.strip()) < 24:
            # Пустой токен выключает админку целиком — это разрешено. А вот
            # короткий токен её открывает: он подбирается перебором.
            blockers.append("ADMIN_TOKEN короче 24 символов")
        return blockers

    def timing_blockers(self) -> list[str]:
        """Согласованность таймингов хода: лок, ожидание лока, потолок задачи.

        Эти четыре числа связаны жёстко, и рассогласование стоит клиенту ответа:

        * ``conv_lock_ttl_seconds > llm_turn_budget_s`` — лок обязан пережить
          самый долгий ход. Иначе ключ протухает на середине хода, следующее
          сообщение того же родителя спокойно берёт лок, и он получает два
          ответа, причём второй — на неполной истории;
        * ``conv_lock_wait_seconds >= llm_turn_budget_s`` — сообщение, пришедшее
          во время чужого хода, обязано этот ход пересидеть. Иначе оно уходит в
          отложенные, а клиент — в тишину;
        * ``worker_job_timeout_s > conv_lock_wait_seconds + llm_turn_budget_s`` —
          задачу нельзя убивать посреди ожидания и хода.
        """
        blockers: list[str] = []
        if self.conv_lock_ttl_seconds <= self.llm_turn_budget_s:
            blockers.append(
                f"CONV_LOCK_TTL_SECONDS={self.conv_lock_ttl_seconds} не переживает ход "
                f"LLM_TURN_BUDGET_S={self.llm_turn_budget_s}: лок протухнет под работающим "
                "ходом и клиент получит два ответа"
            )
        if self.conv_lock_wait_seconds < self.llm_turn_budget_s:
            blockers.append(
                f"CONV_LOCK_WAIT_SECONDS={self.conv_lock_wait_seconds} меньше "
                f"LLM_TURN_BUDGET_S={self.llm_turn_budget_s}: сообщение, пришедшее во время "
                "хода бота, не дождётся своей очереди"
            )
        budget = self.conv_lock_wait_seconds + self.llm_turn_budget_s
        if self.worker_job_timeout_s <= budget:
            blockers.append(
                f"WORKER_JOB_TIMEOUT_S={self.worker_job_timeout_s} не покрывает ожидание лока "
                f"и ход ({budget} с): задачу убьют посреди хода"
            )
        return blockers

    def require_startup(self) -> None:
        """Кидает :class:`app.types.ConfigError`, если ``startup_blockers`` не пуст."""
        blockers = self.startup_blockers()
        if blockers:
            raise ConfigError("Некорректная конфигурация: " + "; ".join(blockers))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кэшированный синглтон настроек. Единственный публичный доступ к конфигу."""
    return Settings()


def reset_settings_cache() -> None:
    """Сброс кэша. Только для тестов."""
    get_settings.cache_clear()
