# ФАЙЛ ЦЕЛИКОМ: app/logging_conf.py
"""Логирование проекта: structlog поверх stdlib-logging, одна строка = один JSON.

Почему именно так:

* Railway читает stdout построчно и распознаёт структурный лог, если строка — валидный
  JSON с полями ``message`` и ``level`` (``debug|info|warn|error``). Поэтому JSON-рендер
  пишет и ``event`` (наш контракт), и ``message`` (для Log Explorer), а уровень
  нормализуется: ``warning`` → ``warn``, ``critical`` → ``error``.
* Логи Railway смотрят и пересылают в чатах, поэтому ПДн в них не место: телефоны и имена
  маскируются двумя процессорами — :func:`_redact` (по имени ключа) и :func:`_mask_values`
  (регулярками по значению, включая произвольный текст и сообщения uvicorn).
* Путь вебхука содержит секрет (``/wazzup/webhook/<secret>``), а access-лог uvicorn пишет
  путь целиком — секрет вырезается регуляркой :data:`_WEBHOOK_PATH_RE`.

Обязательные поля записи (отсутствующие просто не пишутся, ``null`` не ставится):
``ts, level, event, correlation_id, conv_key_hash, lang, kb_hash, model, tool, latency_ms,
tokens_in, tokens_out, tokens_cached, cost_usd, outcome``.

Запрещено к логированию: текст сообщений клиента, ``text_raw``, значения ``GEMINI_API_KEY``,
``WAZZUP_API_KEY``, ``WAZZUP_WEBHOOK_SECRET``, ``ADMIN_TOKEN``, немаскированные телефоны и имена.

Модуль импортируется без сети и без ключей: настройки читаются лениво, внутри
:func:`configure_logging`, и только ради того, чтобы вырезать значения секретов из строк.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from typing import Any, Final

import structlog

from app.config import get_settings

# --------------------------------------------------------------------------- #
# Константы маскирования
# --------------------------------------------------------------------------- #
_MASK: Final[str] = "***"

#: Максимальная глубина обхода вложенных структур в процессорах.
_MAX_DEPTH: Final[int] = 4

#: Максимум элементов последовательности, которые обходятся поэлементно.
_MAX_ITEMS: Final[int] = 50

#: Телефон Казахстана в любом бытовом написании:
#: ``+77051234567``, ``77051234567``, ``8 777 123 45 67``, ``8-777-123-45-67``, ``8 (777) 123-45-67``.
_PHONE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)(?:\+?7|8)[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{2}[\s\-.]?\d{2}(?!\d)"
)

#: Секрет в пути вебхука Wazzup — в access-логе uvicorn он виден целиком.
_WEBHOOK_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(/wazzup/webhook/)[A-Za-z0-9_\-=]{4,}")

#: Заголовок авторизации, если он всё-таки попал в строку.
_BEARER_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{8,}")

#: Ключи, значения которых вырезаются целиком. Сравнение точное или по суффиксу
#: (``_api_key``/``_secret``/``_token``/``_password``), чтобы не задеть ``tokens_in``.
_SECRET_KEYS: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "token",
        "password",
        "passwd",
        "authorization",
        "auth",
        "credentials",
        "x-api-key",
    }
)
_SECRET_SUFFIXES: Final[tuple[str, ...]] = ("_api_key", "_secret", "_token", "_password", "_key")

#: Ключи, значения которых маскируются как телефон.
_PHONE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "phone",
        "phone_e164",
        "contact_phone",
        "client_phone",
        "parent_phone",
        "chat_id",
        "chatid",
        "channel_user",
        "manager_notify_target",
        "notify_target",
        "recipient",
    }
)

#: Ключи, значения которых маскируются как имя.
_NAME_KEYS: Final[frozenset[str]] = frozenset(
    {
        "author_name",
        "contact_name",
        "client_name",
        "parent_name",
        "child_name",
        "user_name",
        "full_name",
        "first_name",
        "last_name",
        "username",
        "contact_username",
        "instagram_username",
    }
)

#: Ключи, которые не логируются никогда: это тело переписки и промпты.
_DROP_KEYS: Final[frozenset[str]] = frozenset(
    {
        "text",
        "text_raw",
        "raw",
        "body",
        "payload",
        "content",
        "message_text",
        "user_text",
        "reply_text",
        "answer",
        "prompt",
        "system_instruction",
        "history",
        "caption",
    }
)

#: Порядок обязательных полей записи. Всё остальное дописывается следом, в порядке появления.
_FIELD_ORDER: Final[tuple[str, ...]] = (
    "ts",
    "level",
    "event",
    "message",
    "correlation_id",
    "conv_key_hash",
    "lang",
    "kb_hash",
    "model",
    "tool",
    "latency_ms",
    "tokens_in",
    "tokens_out",
    "tokens_cached",
    "cost_usd",
    "outcome",
)

#: Болтливые сторонние логгеры: их уровень поднимается независимо от общего.
_NOISY_LOGGERS: Final[dict[str, int]] = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "hpack": logging.WARNING,
    "asyncio": logging.WARNING,
    "sqlalchemy.engine": logging.WARNING,
    "sqlalchemy.pool": logging.WARNING,
    "aiosqlite": logging.WARNING,
    "multipart": logging.WARNING,
    "google_genai": logging.WARNING,
}

#: Значения секретов из настроек — вырезаются из строк по содержимому, а не по имени ключа.
_SECRET_VALUES: set[str] = set()

_configured: bool = False


# --------------------------------------------------------------------------- #
# Публичные маскировщики
# --------------------------------------------------------------------------- #
def mask_phone(value: str | None) -> str | None:
    """``+77051234567`` -> ``+7705***4567``. ``None`` и короткие строки — как есть.

    Понимает бытовые написания: ``8 (777) 123-45-67``, ``8-777-123-45-67``,
    ``+7 777 123 45 67``, ``77051234567``.
    """
    if value is None:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) != 10:
        # Не похоже на казахстанский номер: короткая строка или id — возвращаем как есть.
        return value
    return f"+7{digits[:3]}{_MASK}{digits[6:]}"


def mask_name(value: str | None) -> str | None:
    """``Айгуль`` -> ``А***``. Пустое значение -> ``None``.

    Составное имя маскируется по словам: ``Айгуль Нурлановна`` -> ``А*** Н***``.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    parts = [p for p in stripped.split() if p]
    return " ".join(f"{p[0]}{_MASK}" for p in parts[:3])


def mask_text(value: str | None, *, limit: int = 64) -> str | None:
    """Обрезает и маскирует телефоны внутри произвольного текста.

    Переводы строк схлопываются в пробел (одна запись лога = одна строка),
    хвост длиннее ``limit`` отбрасывается с многоточием.
    """
    if value is None:
        return None
    text = _scrub_string(value)
    text = " ".join(text.split())
    if limit >= 0 and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def conv_key_hash(conv_key: str) -> str:
    """sha256(conv_key)[:12] — в логи уходит хеш, а не номер клиента."""
    return hashlib.sha256(conv_key.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Контекст (correlation_id)
# --------------------------------------------------------------------------- #
def bind_correlation(correlation_id: str, **fields: object) -> None:
    """Кладёт correlation_id (= wazzup messageId) и поля в contextvars structlog.

    ``conv_key`` в полях автоматически превращается в ``conv_key_hash`` — сырой ключ
    диалога содержит номер клиента и в логах не появляется.
    """
    payload: dict[str, Any] = {"correlation_id": correlation_id}
    for key, value in fields.items():
        if key == "conv_key" and isinstance(value, str):
            payload["conv_key_hash"] = conv_key_hash(value)
            continue
        payload[key] = value
    structlog.contextvars.bind_contextvars(**payload)


def clear_correlation() -> None:
    """Очищает контекст. Обязательно в ``finally`` каждой задачи воркера."""
    structlog.contextvars.clear_contextvars()


# --------------------------------------------------------------------------- #
# Процессоры
# --------------------------------------------------------------------------- #
def _scrub_string(value: str) -> str:
    """Вырезает из строки секреты, телефоны и секретную часть пути вебхука."""
    result = value
    for secret in tuple(_SECRET_VALUES):
        if secret and secret in result:
            result = result.replace(secret, _MASK)
    result = _WEBHOOK_PATH_RE.sub(rf"\1{_MASK}", result)
    result = _BEARER_RE.sub(rf"\1{_MASK}", result)
    return _PHONE_RE.sub(_phone_sub, result)


def _phone_sub(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if len(digits) == 11:
        digits = digits[1:]
    if len(digits) != 10:
        return match.group(0)
    return f"+7{digits[:3]}{_MASK}{digits[6:]}"


def _is_secret_key(key: str) -> bool:
    return key in _SECRET_KEYS or key.endswith(_SECRET_SUFFIXES)


def _redact(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Вырезает запрещённые значения по имени ключа.

    Секреты заменяются на ``***``, телефоны и имена маскируются, тело переписки
    (``text``, ``text_raw``, ``history``, промпты) не логируется вовсе.
    """
    return _redact_mapping(event_dict, depth=0)


def _redact_mapping(mapping: dict[str, Any], *, depth: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw_key, value in mapping.items():
        key = str(raw_key)
        lowered = key.lower()
        if lowered == "conv_key" and isinstance(value, str):
            out["conv_key_hash"] = conv_key_hash(value)
            continue
        if _is_secret_key(lowered) or lowered in _DROP_KEYS:
            out[key] = _MASK
            continue
        if lowered in _PHONE_KEYS:
            out[key] = mask_phone(value) if isinstance(value, str) else _MASK
            continue
        if lowered in _NAME_KEYS:
            out[key] = mask_name(value) if isinstance(value, str) else _MASK
            continue
        if isinstance(value, dict) and depth < _MAX_DEPTH:
            out[key] = _redact_mapping(value, depth=depth + 1)
            continue
        if isinstance(value, (list, tuple)) and depth < _MAX_DEPTH:
            out[key] = [
                _redact_mapping(item, depth=depth + 1) if isinstance(item, dict) else item
                for item in list(value)[:_MAX_ITEMS]
            ]
            continue
        out[key] = value
    return out


def _mask_values(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Второй рубеж: маскирует телефоны и секреты внутри любых строковых значений.

    Нужен для чужих логов (uvicorn, sqlalchemy, сторонние библиотеки), где имя ключа
    ни о чём не говорит, а номер клиента может оказаться прямо в тексте сообщения.
    """
    return _mask_any(event_dict, depth=0)


def _mask_any(value: Any, *, depth: int) -> Any:
    if isinstance(value, str):
        return _scrub_string(value)
    if depth >= _MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {k: _mask_any(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_any(v, depth=depth + 1) for v in list(value)[:_MAX_ITEMS]]
    return value


def _railway_level(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Приводит запись к тому виду, который понимает Log Explorer Railway."""
    level = event_dict.get("level")
    if level == "warning":
        event_dict["level"] = "warn"
    elif level in {"critical", "exception", "fatal"}:
        event_dict["level"] = "error"
    event = event_dict.get("event")
    if isinstance(event, str) and "message" not in event_dict:
        event_dict["message"] = event
    return event_dict


def _order_keys(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Ставит обязательные поля в фиксированном порядке; отсутствующие не добавляет."""
    ordered: dict[str, Any] = {}
    for key in _FIELD_ORDER:
        if key in event_dict:
            ordered[key] = event_dict[key]
    for key, value in event_dict.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def _json_dumps(obj: Any, **kwargs: Any) -> str:
    """Однострочный JSON; кириллица остаётся читаемой, неизвестные типы — через ``str``."""
    kwargs.pop("default", None)
    kwargs.pop("ensure_ascii", None)
    return json.dumps(obj, ensure_ascii=False, default=str, **kwargs)


def _load_secret_values() -> None:
    """Забирает значения секретов из настроек, чтобы вырезать их из произвольных строк.

    Настройки читаются лениво и под ``try``: логирование не имеет права уронить старт,
    даже если конфигурация битая — блокеры старта проверяет ``Settings.require_startup``.
    """
    try:
        settings = get_settings()
    except Exception:  # noqa: BLE001 - конфиг проверяется отдельно, здесь важна живучесть
        return
    candidates = (
        settings.wazzup_api_key,
        settings.wazzup_webhook_secret,
        settings.gemini_api_key,
        settings.media_token_secret,
        settings.admin_token or "",
    )
    for value in candidates:
        if value and len(value) >= 8:
            _SECRET_VALUES.add(value)


# --------------------------------------------------------------------------- #
# Настройка
# --------------------------------------------------------------------------- #
def configure_logging(*, level: str, json_output: bool = True) -> None:
    """Настраивает structlog и stdlib-logging. Вызывается один раз в lifespan.

    ``json_output=True`` — одна строка JSON на запись (прод, Railway);
    ``False`` — человекочитаемый вывод для локальной разработки.
    Логи uvicorn, sqlalchemy и прочих сторонних библиотек проходят через те же
    процессоры маскирования.
    """
    global _configured

    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    _load_secret_values()

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.processors.StackInfoRenderer(),
        _redact,
        _mask_values,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    if json_output:
        final_processors: list[Any] = [
            _railway_level,
            _order_keys,
            structlog.processors.JSONRenderer(serializer=_json_dumps),
        ]
    else:
        final_processors = [_order_keys, structlog.dev.ConsoleRenderer(colors=False)]

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            *final_processors,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # uvicorn ставит свои хендлеры — снимаем их, иначе строка печатается дважды.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "arq", "arq.worker", "alembic"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    for name, lvl in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(lvl)

    logging.captureWarnings(True)
    _configured = True


def get_logger(name: str) -> "structlog.stdlib.BoundLogger":
    """Логгер модуля. Единственный разрешённый способ получить логгер."""
    return structlog.stdlib.get_logger(name)


__all__ = [
    "bind_correlation",
    "clear_correlation",
    "configure_logging",
    "conv_key_hash",
    "get_logger",
    "mask_name",
    "mask_phone",
    "mask_text",
]
