"""DI-провайдеры HTTP-слоя и контейнер живых ресурсов процесса.

Здесь собирается всё, что живёт дольше одного запроса: движок БД, фабрика сессий,
хранилище состояний, клиент LLM, клиент Wazzup, очередь задач и ``PipelineDeps``.
Контейнер наполняется один раз в ``app.main.lifespan`` и обнуляется на shutdown.

Правила модуля:

* сети и БД при импорте нет — всё создаётся в :func:`build_runtime`;
* модули поздних волн (``app.core.pipeline``, ``app.llm.client``, ``app.workers.queue``,
  ``app.observability.metrics``) импортируются **лениво**, внутри функций: так HTTP-слой
  не тянет за собой половину приложения на этапе импорта и не образует циклов;
* FastAPI-зависимости не создают ресурсов, а только отдают уже созданные.
"""

from __future__ import annotations

import asyncio
import hmac
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Final

from fastapi import Header, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.kb import loader as kb_loader
from app.logging_conf import get_logger
from app.storage import db as storage_db
from app.storage.state import StateStore, build_state_store
from app.types import JobQueue, KBNotLoadedError, StorageError

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from app.channels.wazzup_client import WazzupClient
    from app.core.pipeline import PipelineDeps
    from app.kb.models import KBSnapshot
    from app.llm.client import LLMClient

log = get_logger(__name__)

#: Заголовок админки. Формат — ``Authorization: Bearer <admin_token>``.
_BEARER_PREFIX: Final[str] = "bearer "

#: Грубый rate limit админки: окно и потолок запросов с одного адреса.
ADMIN_RATE_LIMIT: Final[int] = 60
ADMIN_RATE_WINDOW_S: Final[int] = 60

#: Размер таблицы счётчиков, после которого чистим протухшие окна.
_RATE_TABLE_SOFT_LIMIT: Final[int] = 1024

#: Сколько ждать каждый шаг закрытия ресурсов. Railway по умолчанию даёт
#: ``drainingSeconds=0``, поэтому shutdown обязан быть быстрым и идемпотентным.
SHUTDOWN_STEP_TIMEOUT_S: Final[float] = 5.0


# --------------------------------------------------------------------------- #
# Контейнер процесса
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Runtime:
    """Живые ресурсы процесса. Создаётся в lifespan, один экземпляр на процесс."""

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    state: StateStore
    llm: "LLMClient"
    wazzup: "WazzupClient"
    queue: JobQueue
    pipeline: "PipelineDeps"
    queue_kind: str
    started_at: datetime


_runtime: Runtime | None = None


def set_runtime(runtime: Runtime | None) -> None:
    """Устанавливает (или сбрасывает) контейнер процесса. Зовётся из lifespan и тестов."""
    global _runtime
    _runtime = runtime


def get_runtime() -> Runtime:
    """Контейнер процесса. До инициализации — HTTP 503, а не падение."""
    runtime = _runtime
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="приложение ещё не инициализировано",
        )
    return runtime


def runtime_or_none() -> Runtime | None:
    """Контейнер или ``None`` — для путей, которые обязаны отвечать всегда."""
    return _runtime


# --------------------------------------------------------------------------- #
# Сборка и разборка ресурсов
# --------------------------------------------------------------------------- #
async def build_runtime(settings: Settings) -> Runtime:
    """Создаёт все ресурсы процесса в порядке из контракта (INTERFACES §13.1).

    Движок БД → фабрика сессий → хранилище состояний → LLM (+ warmup) → клиент Wazzup
    → ``PipelineDeps`` → очередь задач и её ``startup()``.

    Прогрев LLM блокером старта **не является**: нет сети — стартуем и пишем warning.
    Отсутствие очереди задач — блокер: без неё вебхуку некуда складывать входящие.
    """
    engine = storage_db.build_engine()
    storage_db.set_engine(engine)
    sessionmaker = storage_db.get_sessionmaker()

    state = build_state_store(settings)
    log.info(
        "state_store_ready",
        backend=type(state).__name__,
        configured=settings.state_backend,
    )

    llm = _build_llm(settings)
    try:
        await asyncio.wait_for(llm.warmup(), timeout=10.0)
    except Exception as exc:  # прогрев не обязателен: сеть могла быть недоступна
        log.warning("llm_warmup_failed", error=type(exc).__name__)

    wazzup = _build_wazzup(settings)

    pipeline = _build_pipeline_deps(
        settings=settings,
        sessionmaker=sessionmaker,
        state=state,
        llm=llm,
        queue=_QueueProxy(),
    )
    queue = _build_queue(settings, pipeline)
    # PipelineDeps.queue — обычное поле dataclass: подменяем прокси на реальную очередь,
    # чтобы разорвать курицу-яйцо (очередь строится вокруг deps, deps ссылаются на очередь).
    pipeline.queue = queue
    await queue.startup()

    runtime = Runtime(
        settings=settings,
        engine=engine,
        sessionmaker=sessionmaker,
        state=state,
        llm=llm,
        wazzup=wazzup,
        queue=queue,
        pipeline=pipeline,
        queue_kind=type(queue).__name__,
        started_at=datetime.now(timezone.utc),
    )
    set_runtime(runtime)
    log.info("runtime_ready", queue=runtime.queue_kind, inline_worker=settings.inline_worker)
    return runtime


async def shutdown_runtime() -> None:
    """Закрывает ресурсы процесса. Каждый шаг ограничен по времени и не роняет остальные."""
    runtime = _runtime
    set_runtime(None)
    if runtime is None:
        await _safely("engine", storage_db.dispose_engine())
        return

    await _safely("queue", runtime.queue.shutdown())
    await _safely("llm", runtime.llm.aclose())
    await _safely("wazzup", runtime.wazzup.aclose())
    close = getattr(runtime.state, "close", None)
    if close is not None:
        await _safely("state", close())
    await _safely("engine", storage_db.dispose_engine())
    log.info("runtime_closed")


async def _safely(what: str, coro: Any) -> None:
    """Ждёт корутину закрытия не дольше :data:`SHUTDOWN_STEP_TIMEOUT_S`."""
    try:
        await asyncio.wait_for(coro, timeout=SHUTDOWN_STEP_TIMEOUT_S)
    except asyncio.TimeoutError:
        log.warning("shutdown_step_timeout", step=what)
    except Exception as exc:
        log.warning("shutdown_step_failed", step=what, error=type(exc).__name__)


class _QueueProxy:
    """Временная заглушка очереди на время сборки ``PipelineDeps``.

    Живёт микросекунды: сразу после создания настоящей очереди поле ``deps.queue``
    перезаписывается. Любой вызов до подмены — программная ошибка, поэтому падаем громко.
    """

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - защита от опечатки
        raise RuntimeError(f"очередь задач ещё не собрана (обращение к {name!r})")


def _build_llm(settings: Settings) -> "LLMClient":
    """Ленивый импорт слоя LLM (волна 2b)."""
    from app.llm.client import build_client

    return build_client(settings)


def _build_wazzup(settings: Settings) -> "WazzupClient":
    """Ленивый импорт клиента Wazzup: HTTP-пул создаётся при первом запросе."""
    from app.channels.wazzup_client import WazzupClient

    return WazzupClient.from_settings(settings)


def _build_pipeline_deps(
    *,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    state: StateStore,
    llm: "LLMClient",
    queue: Any,
) -> "PipelineDeps":
    """Ленивый импорт ядра (волна 3a)."""
    from app.core.pipeline import PipelineDeps

    return PipelineDeps(
        sessionmaker=sessionmaker,
        state=state,
        llm=llm,
        kb=kb_loader.get_snapshot,
        queue=queue,
        settings=settings,
    )


def _build_queue(settings: Settings, pipeline: "PipelineDeps") -> JobQueue:
    """Ленивый импорт очереди (волна 3c). ``INLINE_WORKER`` разбирается внутри ``build_queue``."""
    from app.workers.queue import build_queue

    return build_queue(settings, pipeline)


def metrics_module() -> Any | None:
    """Модуль метрик, если он уже есть в сборке. Метрики не должны ломать запрос."""
    try:
        from app.observability import metrics
    except Exception:  # pragma: no cover - метрики опциональны для HTTP-слоя
        return None
    return metrics


# --------------------------------------------------------------------------- #
# Зависимости FastAPI
# --------------------------------------------------------------------------- #
def get_settings_dep() -> Settings:
    """Настройки процесса. Внутри lifespan может быть переопределённый экземпляр."""
    runtime = _runtime
    return runtime.settings if runtime is not None else get_settings()


async def get_session() -> AsyncIterator[AsyncSession]:
    """Сессия БД на один запрос: commit на выходе, rollback на исключении."""
    runtime = _runtime
    maker = runtime.sessionmaker if runtime is not None else storage_db.get_sessionmaker()
    session = maker()
    try:
        yield session
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise StorageError(f"Сбой транзакции: {exc.__class__.__name__}") from exc
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def get_state() -> StateStore:
    """Хранилище TTL-состояний (Redis в проде, память в тестах)."""
    return get_runtime().state


def get_queue() -> JobQueue:
    """Очередь фоновых задач."""
    return get_runtime().queue


def get_llm() -> "LLMClient":
    """Клиент модели."""
    return get_runtime().llm


def get_wazzup() -> "WazzupClient":
    """Клиент Wazzup24 (общий пул соединений процесса)."""
    return get_runtime().wazzup


def get_kb() -> "KBSnapshot":
    """Текущий снимок базы знаний. Снимка нет — 503, а не 500."""
    try:
        return kb_loader.get_snapshot()
    except KBNotLoadedError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="база знаний не загружена",
        ) from exc


def get_pipeline_deps() -> "PipelineDeps":
    """Зависимости пайплайна (для ручных прогонов и отладочных ручек)."""
    return get_runtime().pipeline


# --------------------------------------------------------------------------- #
# Админка: аутентификация и rate limit
# --------------------------------------------------------------------------- #
_admin_hits: dict[str, tuple[float, int]] = {}
_admin_lock: Final[asyncio.Lock] = asyncio.Lock()


def _rate_limit_key(request: Request) -> str:
    """Ключ счётчика запросов — тот, который клиент подделать не может.

    Процесс запускается с ``--forwarded-allow-ips='*'`` (Dockerfile, railway.api.json),
    и uvicorn кладёт в ``scope['client']`` САМОЕ ЛЕВОЕ значение ``X-Forwarded-For``,
    то есть то, что прислал сам клиент. Считая лимит по нему, мы получали лимит,
    который снимается одной строкой заголовка: ``X-Forwarded-For: 1.2.3.<случайное>``
    даёт каждому запросу свой счётчик, и ADMIN_TOKEN перебирается без ограничений.

    Поэтому берём ПРАВЫЙ элемент цепочки — его дописывает ближайший к нам прокси
    (edge Railway) поверх всего, что прислал клиент. Худшее, что может сделать
    атакующий, — попасть в общий счётчик с другими клиентами того же прокси: лимит
    при этом становится строже, а не слабее.
    """
    forwarded = request.headers.get("x-forwarded-for") or ""
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    if hops:
        return hops[-1]
    return request.client.host if request.client is not None else "unknown"


async def admin_rate_limit(request: Request) -> None:
    """Грубый лимит запросов админки по адресу клиента.

    Считается в памяти процесса: админка низкочастотная, а зависимость от Redis
    означала бы, что при его недоступности админ теряет доступ к ручкам ремонта.
    """
    client = _rate_limit_key(request)
    now = time.monotonic()
    async with _admin_lock:
        if len(_admin_hits) > _RATE_TABLE_SOFT_LIMIT:
            stale = [k for k, (started, _) in _admin_hits.items() if now - started > ADMIN_RATE_WINDOW_S]
            for key in stale:
                _admin_hits.pop(key, None)
        started, count = _admin_hits.get(client, (now, 0))
        if now - started > ADMIN_RATE_WINDOW_S:
            started, count = now, 0
        count += 1
        _admin_hits[client] = (started, count)
        over_limit = count > ADMIN_RATE_LIMIT
        retry_after = max(1, int(ADMIN_RATE_WINDOW_S - (now - started)))
    if over_limit:
        log.warning("admin_rate_limited", hits=count)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="слишком много запросов",
            headers={"Retry-After": str(retry_after)},
        )


def reset_admin_rate_limit() -> None:
    """Сброс счётчиков. Только для тестов."""
    _admin_hits.clear()


async def require_admin(authorization: str | None = Header(default=None)) -> None:
    """Bearer admin_token. Не задан в настройках -> 404 на всю админку."""
    settings = get_settings_dep()
    token = (settings.admin_token or "").strip()
    if not token:
        # Админка не сконфигурирована — её как будто не существует.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    header = authorization or ""
    presented = header[len(_BEARER_PREFIX):] if header.lower().startswith(_BEARER_PREFIX) else ""
    if not hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8")):
        log.warning("admin_auth_failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="требуется админ-токен",
            headers={"WWW-Authenticate": "Bearer"},
        )


# --------------------------------------------------------------------------- #
# Мелкие утилиты HTTP-слоя
# --------------------------------------------------------------------------- #
_CORRELATION_SAFE_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


def safe_correlation_id(raw: str | None) -> str | None:
    """Пропускает только безопасный внешний correlation id (защита от инъекции в лог)."""
    if raw and _CORRELATION_SAFE_RE.match(raw):
        return raw
    return None
