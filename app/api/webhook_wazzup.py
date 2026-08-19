"""Приём вебхуков Wazzup24.

Хендлер делает ровно четыре вещи (INTERFACES §13.3): сверяет секрет, отвечает на
регистрационный тестовый запрос, валидирует тело и кладёт payload в очередь.
Никакой обработки сообщения здесь нет — целевое время ответа < 200 мс.

Два правила, которые важнее аккуратности кодов ответа:

* несовпадение секрета → **404**, а не 403: подтверждать существование эндпоинта нельзя;
* всё остальное → **200 OK**, включая невалидное тело и внутренние сбои. Любой не-200
  заставляет Wazzup ретраить, а при систематических ошибках — отключить вебхук.
"""

from __future__ import annotations

import hmac
import json
from typing import Any, Final

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from app.channels.wazzup_schemas import WebhookPayload, parse_webhook
from app.config import Settings
from app.deps import get_settings_dep, metrics_module, runtime_or_none
from app.logging_conf import get_logger
from app.types import WebhookValidationError

router = APIRouter(tags=["wazzup"])

log = get_logger(__name__)

#: Потолок тела вебхука. Wazzup шлёт пачки сообщений, но не мегабайты.
MAX_WEBHOOK_BYTES: Final[int] = 1024 * 1024


def _ok(kind: str) -> Response:
    """Единственный успешный ответ вебхука: 200 и минимальное тело."""
    return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ok", "kind": kind})


def _count(kind: str) -> None:
    """Метрика приёма. Сбой метрик не имеет права влиять на ответ вебхука."""
    metrics = metrics_module()
    if metrics is None:
        return
    try:
        metrics.observe_webhook(kind)
    except Exception:  # pragma: no cover - метрика не критична
        pass


#: Списочные разделы тела вебхука, которые разбираются поэлементно.
_LIST_FIELDS: Final[tuple[str, ...]] = ("messages", "statuses", "channelsUpdates", "channels")


def _salvage(payload: Any) -> tuple[Any, WebhookPayload | None, int]:
    """Выбрасывает битые элементы пачки и разбирает остаток.

    Возвращает ``(очищенный payload, разобранный payload | None, сколько выброшено)``.
    ``None`` во втором элементе значит «спасать нечего»: тело не словарь, списков в
    нём нет, либо после отбраковки не осталось ни одного элемента.

    Схема валидирует тело целиком, поэтому один элемент с числовым ``chatId`` или
    без обязательного поля обнулял весь вебхук. Здесь каждый элемент проверяется
    отдельно — ровно тем же ``parse_webhook``, чтобы не завести вторую, расходящуюся
    со схемой проверку.
    """
    if not isinstance(payload, dict):
        return payload, None, 0

    cleaned: dict[str, Any] = {k: v for k, v in payload.items() if k not in _LIST_FIELDS}
    dropped = 0
    kept_total = 0
    for field in _LIST_FIELDS:
        items = payload.get(field)
        if not isinstance(items, list):
            if items is not None:
                dropped += 1  # не список там, где ожидался список
            continue
        kept: list[Any] = []
        for item in items:
            try:
                parse_webhook({field: [item]})
            except WebhookValidationError:
                dropped += 1
                continue
            kept.append(item)
        if kept:
            cleaned[field] = kept
            kept_total += len(kept)

    if not dropped or not kept_total:
        return payload, None, dropped
    try:
        return cleaned, parse_webhook(cleaned), dropped
    except WebhookValidationError:
        return payload, None, dropped


def _payload_kind(payload: WebhookPayload) -> str:
    """Что приехало: сообщения, статусы, обновления каналов."""
    if payload.message_list():
        return "message"
    if payload.status_list():
        return "status"
    if payload.channel_update_list():
        return "channel_update"
    return "other"


@router.post("/wazzup/webhook/{secret}", include_in_schema=False)
async def receive(
    secret: str,
    request: Request,
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    """Приём вебхука Wazzup24: сверка секрета, тест-запрос, валидация, очередь."""
    # 1. Секрет в пути. Сравнение constant-time; пустой секрет в настройках = эндпоинта нет.
    expected = settings.wazzup_webhook_secret or ""
    if not expected or not hmac.compare_digest(secret.encode("utf-8"), expected.encode("utf-8")):
        log.warning("webhook_secret_mismatch", path_len=len(secret))
        _count("forbidden")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    # 2. Тело. Слишком большое не читаем вовсе.
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_WEBHOOK_BYTES:
        log.warning("webhook_payload_too_large", size=int(declared))
        _count("invalid")
        return _ok("too_large")

    try:
        raw = await request.body()
    except Exception as exc:  # обрыв соединения на чтении тела
        log.warning("webhook_body_read_failed", error=type(exc).__name__)
        _count("invalid")
        return _ok("unread")

    if len(raw) > MAX_WEBHOOK_BYTES:
        log.warning("webhook_payload_too_large", size=len(raw))
        _count("invalid")
        return _ok("too_large")

    payload: Any = None
    if raw.strip():
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            # Битый JSON — это не наша проблема и не повод для 4xx: 4xx = ретраи Wazzup.
            log.warning("webhook_invalid_payload", reason="json", error=type(exc).__name__)
            _count("invalid")
            return _ok("invalid")

    try:
        parsed = parse_webhook(payload)
    except WebhookValidationError as exc:
        # Пачка целиком не разобралась. Прежде чем выбросить её, спасаем годные
        # элементы: Wazzup складывает несколько сообщений в один вебхук и,
        # получив 200, эту пачку не повторит — иначе вместе с одним незнакомым
        # системным сообщением молча пропадал бы вопрос живого клиента.
        payload, salvaged, dropped = _salvage(payload)
        if salvaged is None:
            log.warning("webhook_invalid_payload", reason="schema", error=exc.message)
            _count("invalid")
            return _ok("invalid")
        parsed = salvaged
        log.warning(
            "webhook_partially_invalid",
            reason="schema",
            error=exc.message,
            dropped=dropped,
        )
        _count("partial")

    # 3. Регистрационный тест Wazzup: `{"test": true}` либо пустое тело.
    #    Не ответим 200 — PATCH /v3/webhooks вернёт testPostNotPassed и вебхук не зарегистрируется.
    if parsed.is_test:
        log.info("webhook_test_received")
        _count("test")
        return _ok("test")

    kind = _payload_kind(parsed)
    _count(kind)

    # 4. Постановка в очередь. Обработки в хендлере нет — только enqueue.
    runtime = runtime_or_none()
    if runtime is None:
        log.error("webhook_dropped", reason="runtime_not_ready", kind=kind)
        _count("dropped")
        return _ok("dropped")

    try:
        job_id = await runtime.queue.enqueue_inbound(
            payload if isinstance(payload, dict) else {}
        )
    except Exception as exc:
        # Очередь недоступна. Ответить не-200 нельзя (Wazzup отключит вебхук),
        # поэтому громко логируем: это событие для алерта в Railway Log Explorer.
        log.error(
            "webhook_enqueue_failed",
            kind=kind,
            error=type(exc).__name__,
            messages=len(parsed.message_list()),
            statuses=len(parsed.status_list()),
        )
        _count("dropped")
        return _ok("dropped")

    if not job_id:
        # Очередь вернула пустой id вместо исключения: сообщение никуда не встало,
        # а страховки для входящих (в отличие от outbox_sweep_cron) не существует.
        # Молчаливого `webhook_received` с пустым job_id здесь быть не должно —
        # иначе потеря лида выглядит в мониторинге как штатный приём.
        log.error(
            "webhook_enqueue_failed",
            kind=kind,
            error="empty_job_id",
            messages=len(parsed.message_list()),
            statuses=len(parsed.status_list()),
        )
        _count("dropped")
        return _ok("dropped")

    log.info(
        "webhook_received",
        kind=kind,
        job_id=job_id,
        messages=len(parsed.message_list()),
        statuses=len(parsed.status_list()),
        channel_updates=len(parsed.channel_update_list()),
    )
    return _ok(kind)
