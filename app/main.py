"""Точка входа HTTP-службы: фабрика приложения, lifespan, middleware.

Railway-специфика, зашитая в этот модуль:

* слушаем ``0.0.0.0:$PORT`` — порт приходит переменной ``PORT`` (``Settings.port``);
* путь healthcheck — ``/healthz``, и ``TrustedHostMiddleware`` обязан пропускать
  ``healthcheck.railway.app``, иначе деплой падает с «service unavailable»;
* ``drainingSeconds`` по умолчанию **0**: между SIGTERM и SIGKILL времени нет,
  поэтому shutdown короткий, пошагово ограниченный по времени и идемпотентный;
* ``INLINE_WORKER=true`` поднимает воркер и периодические задачи в этом же процессе,
  чтобы бот жил одной службой.

Блокеры старта — только технические: невалидная KB и отсутствие обязательных
настроек подключения (``Settings.require_startup``). Юридических блокеров в проекте нет.
"""

from __future__ import annotations

import asyncio
import importlib
import time
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator, Awaitable, Callable, Final
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import deps
from app.api import admin as admin_api
from app.api import health as health_api
from app.api import media as media_api
from app.api import webhook_wazzup as webhook_api
from app.config import Settings, get_settings
from app.kb import loader as kb_loader
from app.logging_conf import bind_correlation, clear_correlation, configure_logging, get_logger
from app.types import (
    BotError,
    ConfigError,
    KBNotLoadedError,
    KBValidationError,
    StorageError,
)

log = get_logger(__name__)

#: Заголовок сквозного идентификатора запроса.
CORRELATION_HEADER: Final[str] = "x-correlation-id"

#: Хост, с которого Railway дёргает healthcheck. Забыть его в TrustedHost = провал деплоя.
RAILWAY_HEALTHCHECK_HOST: Final[str] = "healthcheck.railway.app"

#: Пути, которые не пишутся в лог: healthcheck и метрики создают шум,
#: а на Railway есть предел 500 строк логов в секунду на реплику.
QUIET_PATHS: Final[frozenset[str]] = frozenset({"/healthz", "/metrics"})

#: Префикс вебхука. Ответ на этом пути обязан быть 200 даже при внутренней аварии.
WEBHOOK_PREFIX: Final[str] = "/wazzup/webhook/"

#: Пауза перед регистрацией вебхука: Wazzup в ответ на PATCH шлёт тестовый POST,
#: и сервер к этому моменту уже должен принимать соединения.
WEBHOOK_REGISTER_DELAY_S: Final[float] = 3.0

#: Периодические задачи, которые в режиме INLINE_WORKER некому запустить (arq-крон не работает).
#: Интервалы — из контракта INTERFACES §14.1; ``outbox_sweep_cron`` разгребает застрявшие отправки.
_CRON_JOBS: Final[tuple[tuple[str, int], ...]] = (
    ("outbox_sweep_cron", 60),
    ("followup_sweep_cron", 10 * 60),
    ("refresh_channels_cron", 15 * 60),
)

#: Где искать эти функции. Владелец модулей — волна 3c; имя модуля контрактом не задано.
_CRON_MODULES: Final[tuple[str, ...]] = (
    "app.workers.queue",
    "app.workers.tasks_outbound",
    "app.workers.tasks_followup",
)


# --------------------------------------------------------------------------- #
# Middleware
# --------------------------------------------------------------------------- #
class CorrelationMiddleware:
    """Чистое ASGI-middleware: correlation id, тайминг запроса, строка лога.

    Не наследует ``BaseHTTPMiddleware`` сознательно: тот оборачивает ответ в очередь
    и мешает потоковой отдаче файлов из ``/media/{token}``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        correlation_id = (
            deps.safe_correlation_id(headers.get(CORRELATION_HEADER))
            or deps.safe_correlation_id(headers.get("x-request-id"))
            or uuid4().hex
        )
        path: str = scope.get("path", "")
        method: str = scope.get("method", "")
        started = time.perf_counter()
        seen: dict[str, int] = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                seen["status"] = int(message["status"])
                MutableHeaders(scope=message)[CORRELATION_HEADER] = correlation_id
            await send(message)

        bind_correlation(correlation_id, path=path, method=method)
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if path not in QUIET_PATHS:
                log.info(
                    "http_request",
                    path=path,
                    method=method,
                    http_status=seen["status"],
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            clear_correlation()


def _allowed_hosts(settings: Settings) -> list[str]:
    """Белый список Host для прод-окружения. ``healthcheck.railway.app`` — обязателен."""
    hosts = {
        RAILWAY_HEALTHCHECK_HOST,
        "*.up.railway.app",
        "*.railway.app",
        "*.railway.internal",
        "localhost",
        "127.0.0.1",
    }
    hostname = urlsplit(settings.public_base_url).hostname
    if hostname:
        hosts.add(hostname)
    return sorted(hosts)


# --------------------------------------------------------------------------- #
# Обработчики исключений
# --------------------------------------------------------------------------- #
def _is_webhook(request: Request) -> bool:
    """Путь вебхука: ему нельзя отдавать не-200, иначе Wazzup отключит доставку."""
    return request.url.path.startswith(WEBHOOK_PREFIX)


async def _bot_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Свои исключения: наружу только код, стек — в лог."""
    error = exc if isinstance(exc, BotError) else BotError(str(exc))
    http_status = 500
    if isinstance(error, (KBNotLoadedError, StorageError)):
        http_status = 503
    elif isinstance(error, KBValidationError):
        http_status = 422
    log.warning("request_failed", code=error.code, http_status=http_status)
    if _is_webhook(request):
        return JSONResponse(status_code=200, content={"status": "ok", "kind": "error"})
    return JSONResponse(status_code=http_status, content={"error": error.code})


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    """422 без эха входных данных: в теле запроса бывают телефоны и имена."""
    log.info("request_validation_failed", path=request.url.path)
    if _is_webhook(request):
        return JSONResponse(status_code=200, content={"status": "ok", "kind": "invalid"})
    return JSONResponse(status_code=422, content={"error": "validation_error"})


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    """Последний рубеж. Клиент не получает ни стека, ни текста исключения."""
    log.error(
        "unhandled_exception",
        path=request.url.path,
        error=type(exc).__name__,
        exc_info=True,
    )
    if _is_webhook(request):
        return JSONResponse(status_code=200, content={"status": "ok", "kind": "error"})
    return JSONResponse(status_code=500, content={"error": "internal_error"})


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Старт: configure_logging -> settings.require_startup() -> kb.load() (падение = отказ старта)
    -> build_engine -> state store -> LLM warmup -> JobQueue.startup()
    -> при INLINE_WORKER=true поднять inline-воркер
    -> при wazzup_register_webhook_on_start=true PATCH /v3/webhooks.
    Стоп: очередь, LLM, БД, http-клиенты."""
    settings: Settings = getattr(app.state, "settings", None) or get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    log.info(
        "startup_begin",
        app_env=settings.app_env,
        port=settings.port,
        inline_worker=settings.inline_worker,
        state_backend=settings.state_backend,
    )

    # 1. Технические блокеры: обязательные настройки подключения.
    try:
        settings.require_startup()
    except ConfigError as exc:
        log.error("startup_blocked", reason="config_error", message=exc.message)
        raise

    # 2. База знаний. Невалидная KB — отказ старта: бот без фактов бесполезен и опасен.
    try:
        snapshot = await kb_loader.load(
            settings.kb_dir,
            media_dir=settings.media_dir,
            schema_version=settings.kb_schema_version,
        )
    except KBValidationError as exc:
        log.error("startup_blocked", reason="kb_validation_error", errors=list(exc.errors)[:20])
        raise
    kb_loader.swap(snapshot)
    log.info("kb_loaded", kb_hash=snapshot.kb_hash, gyms=len(snapshot.gyms.gyms))

    # 3. Пулы, клиенты, очередь. Очередь недоступна -> старта нет: вебхуку некуда писать.
    runtime = await deps.build_runtime(settings)
    app.state.runtime = runtime

    # 4. INLINE_WORKER: пул задач поднимает InlineJobQueue.startup(). Расписание
    #    периодических заданий у него своё (_INLINE_PERIODIC), и владелец расписания
    #    обязан быть ровно один: раньше те же outbox_sweep/followup_sweep/refresh_channels
    #    крутились ещё и здесь, то есть выполнялись дважды за интервал — двойная
    #    нагрузка на БД и, что хуже, второй sweep успевал поставить дубль напоминания,
    #    пока первый не закоммитил state=sent. Свой цикл поднимаем только если очередь
    #    расписанием не владеет (другая реализация JobQueue).
    # 3a. Наблюдатель за базой знаний: CRM и Telegram-админка правят YAML в других
    #     процессах. Без него правка цены доехала бы до клиентов только после
    #     перезапуска сервиса, а интерфейс всё это время показывал бы «сохранено».
    watch_task: asyncio.Task[None] | None = None
    if settings.kb_watch_interval_s > 0:
        watch_task = asyncio.create_task(_kb_watch_loop(settings), name="kb-watch")

    cron_task: asyncio.Task[None] | None = None
    if settings.inline_worker and not getattr(runtime.queue, "owns_periodic_jobs", False):
        cron_task = asyncio.create_task(_inline_cron_loop(runtime), name="inline-cron")
        log.info("inline_worker_started", queue=runtime.queue_kind, max_jobs=settings.worker_max_jobs)

    # 5. Регистрация вебхука — отложенной задачей: PATCH /v3/webhooks вызывает тестовый
    #    POST на наш адрес, а сервер начнёт принимать соединения только после lifespan.
    register_task: asyncio.Task[None] | None = None
    if settings.wazzup_register_webhook_on_start:
        register_task = asyncio.create_task(_register_webhook(settings, runtime), name="wz-webhook")

    log.info("startup_done", kb_hash=snapshot.kb_hash, queue=runtime.queue_kind)
    try:
        yield
    finally:
        log.info("shutdown_begin")
        for task in (watch_task, cron_task, register_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        await deps.shutdown_runtime()
        app.state.runtime = None
        log.info("shutdown_done")


async def _kb_watch_loop(settings: Settings) -> None:
    """Периодически перечитывает базу знаний, если файлы изменились.

    Сломанная база не применяется и не роняет сервис: бот продолжает отвечать по
    прежнему снимку, а причина уходит в лог. Правило то же, что и на старте, —
    половинчатая база хуже отсутствующей.
    """
    from app.core.kb_watch import KBWatcher

    watcher = KBWatcher(
        settings.kb_dir,
        media_dir=settings.media_dir,
        schema_version=settings.kb_schema_version,
        min_interval_s=0,
    )
    while True:
        await asyncio.sleep(settings.kb_watch_interval_s)
        try:
            result = await watcher.maybe_reload()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - наблюдатель не имеет права ронять сервис
            log.error("kb_watch_failed", error=type(exc).__name__, message=str(exc))
            continue
        if result is not None and result.changed:
            log.info("kb_hot_reloaded", old=result.old_hash[:12], new=result.new_hash[:12])


async def _register_webhook(settings: Settings, runtime: deps.Runtime) -> None:
    """Регистрирует адрес вебхука в Wazzup. Неуспех старт не ломает."""
    await asyncio.sleep(WEBHOOK_REGISTER_DELAY_S)
    url = settings.webhook_url()
    if not url.startswith("https://"):
        log.warning("webhook_register_skipped", reason="public_base_url не https", url=url)
        return
    try:
        await runtime.wazzup.set_webhooks(url)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error("webhook_register_failed", error=type(exc).__name__, url=url)
        return
    log.info("webhook_registered", url=url)


def _load_cron_jobs() -> list[tuple[str, Callable[[dict[str, Any]], Awaitable[None]], int]]:
    """Находит cron-функции волны 3c. Модуль контрактом не зафиксирован — пробуем кандидатов."""
    found: list[tuple[str, Callable[[dict[str, Any]], Awaitable[None]], int]] = []
    for name, interval in _CRON_JOBS:
        for module_name in _CRON_MODULES:
            try:
                module = importlib.import_module(module_name)
            except Exception:  # модуль ещё не собран или тянет недоступную зависимость
                continue
            func = getattr(module, name, None)
            if callable(func):
                found.append((name, func, interval))
                break
        else:
            log.warning("inline_cron_missing", job=name)
    return found


async def _inline_cron_loop(runtime: deps.Runtime) -> None:
    """Периодические задачи в процессе API при ``INLINE_WORKER=true``.

    Контекст задач — обычный arq-подобный ``dict``: кладём в него всё, что может
    понадобиться, чтобы не зависеть от того, какой ключ читает конкретная задача.
    """
    jobs = _load_cron_jobs()
    if not jobs:
        log.warning("inline_cron_disabled", reason="ни одна cron-функция не найдена")
        return

    ctx: dict[str, Any] = {
        "settings": runtime.settings,
        "deps": runtime.pipeline,
        "state": runtime.state,
        "sessionmaker": runtime.sessionmaker,
        "llm": runtime.llm,
        "queue": runtime.queue,
        "wazzup": runtime.wazzup,
        "kb": kb_loader.get_snapshot,
    }
    # Первый прогон — не сразу: даём приложению принять трафик.
    next_run = {name: time.monotonic() + 30.0 for name, _, _ in jobs}
    log.info("inline_cron_started", jobs=[name for name, _, _ in jobs])

    while True:
        await asyncio.sleep(5.0)
        now = time.monotonic()
        for name, func, interval in jobs:
            if now < next_run[name]:
                continue
            next_run[name] = now + interval
            try:
                await func(ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("inline_cron_failed", job=name, error=type(exc).__name__)


# --------------------------------------------------------------------------- #
# Фабрика приложения
# --------------------------------------------------------------------------- #
def create_app(settings: Settings | None = None) -> FastAPI:
    """Фабрика приложения. Роутеры, middleware, lifespan. Сети при вызове нет."""
    resolved = settings or get_settings()
    is_public = resolved.app_env in ("prod", "staging")

    app = FastAPI(
        title="AINAZAROV TOP TEAM — AI-консультант",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None if is_public else "/docs",
        redoc_url=None,
        openapi_url=None if is_public else "/openapi.json",
    )
    app.state.settings = resolved
    app.state.runtime = None

    # Порядок важен: TrustedHost стоит снаружи, но обязан пропускать healthcheck Railway.
    if is_public:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts(resolved))
    app.add_middleware(CorrelationMiddleware)

    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(BotError, _bot_error_handler)
    app.add_exception_handler(Exception, _unhandled_handler)

    app.include_router(health_api.router)
    app.include_router(webhook_api.router)
    app.include_router(media_api.router)
    app.include_router(admin_api.router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        """Корень — только признак жизни, без единой подробности о конфигурации."""
        return {"service": "ainazarov-bot", "status": "ok"}

    return app


#: Модульный экземпляр для ``uvicorn app.main:app``.
app: FastAPI = create_app()


def main() -> None:
    """Локальный запуск: ``python -m app.main``. На Railway команду задаёт start command."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
