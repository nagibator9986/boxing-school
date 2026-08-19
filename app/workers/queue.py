"""Очередь фоновых задач: ARQ в проде, честный inline-пул на старте.

Две реализации протокола :class:`app.types.JobQueue`:

* :class:`ArqJobQueue` — Redis, отдельная служба Railway (``arq app.workers.queue.WorkerSettings``);
* :class:`InlineJobQueue` — ``INLINE_WORKER=true``: задачи выполняются в процессе API.

Inline-режим **не заглушка**. Внутри живёт ``asyncio.Queue``, пул из
``worker_max_jobs`` корутин, планировщик отложенных задач, свои повторы и
периодические задания. Он честно выполняет ту же работу, что и arq, просто без
Redis: одна служба на Railway вместо двух, пока нагрузка маленькая. Разделить
службы потом — вопрос одной переменной окружения, код при этом не меняется.

Разница, о которой нужно помнить: inline-очередь живёт в памяти процесса и не
переживает рестарт. Ничего не теряется насовсем — незавершённые строки outbox
и напоминания лежат в БД, их подбирают сметки по расписанию.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Final, Sequence
from uuid import UUID, uuid4

from arq import cron
from arq.connections import RedisSettings, create_pool
from arq.worker import Retry

from app.config import Settings, get_settings
from app.logging_conf import get_logger
from app.observability import metrics
from app.types import JobQueue
from app.workers.tasks_followup import followup_sweep_cron, send_followup_job
from app.workers.tasks_inbound import process_inbound_job
from app.workers.tasks_outbound import (
    outbox_sweep_cron,
    refresh_channels_cron,
    send_outbox_job,
)

if TYPE_CHECKING:  # pragma: no cover - только аннотации
    from app.core.pipeline import PipelineDeps

log = get_logger(__name__)

#: Имена задач. Строки, а не функции: arq ищет задачу по имени в реестре воркера.
JOB_INBOUND: Final[str] = "process_inbound_job"
JOB_OUTBOX: Final[str] = "send_outbox_job"
JOB_FOLLOWUP: Final[str] = "send_followup_job"

class QueueUnavailableError(RuntimeError):
    """Задачу поставить не удалось, и подобрать её потом нечем.

    Поднимается только там, где у задачи нет страховки в БД (входящее сообщение
    вебхука). Для исходящих строк и напоминаний провал постановки не фатален:
    строка остаётся в таблице, её подберёт сметка по расписанию.
    """


#: Проверка канала: раз в 15 минут. Чаще незачем, реже — слишком поздно узнаем,
#: что бот молчит всем клиентам сразу.
REFRESH_CHANNELS_MINUTES: Final[set[int]] = {0, 15, 30, 45}

#: Сметка напоминаний: раз в 10 минут. Точность выше не нужна — напоминания не
#: привязаны к секундам, а лишние проходы бьют по БД.
FOLLOWUP_SWEEP_MINUTES: Final[set[int]] = {0, 10, 20, 30, 40, 50}

#: Сколько раз arq повторит упавшую задачу (первая попытка + три повтора).
MAX_TRIES: Final[int] = 4

#: Сколько держать результат задачи в Redis, секунды.
KEEP_RESULT_S: Final[int] = 600

#: Шаг планировщика отложенных задач в inline-режиме, секунды.
_INLINE_TICK_S: Final[float] = 0.2

#: Сколько ждать завершения текущих задач при остановке inline-воркера, секунды.
INLINE_DRAIN_TIMEOUT_S: Final[float] = 20.0

#: Периодические задания inline-режима: (функция, интервал в секундах).
_INLINE_PERIODIC: Final[tuple[tuple[str, float], ...]] = (
    ("outbox_sweep_cron", 60.0),
    ("followup_sweep_cron", 600.0),
    ("refresh_channels_cron", 900.0),
)


# --------------------------------------------------------------------------- #
# Реестр задач
# --------------------------------------------------------------------------- #
#: Всё, что умеет исполнять воркер. Обе реализации очереди берут функции отсюда,
#: поэтому inline и arq не могут разъехаться по списку задач.
TASKS: Final[dict[str, Callable[..., Any]]] = {
    JOB_INBOUND: process_inbound_job,
    JOB_OUTBOX: send_outbox_job,
    JOB_FOLLOWUP: send_followup_job,
    "outbox_sweep_cron": outbox_sweep_cron,
    "followup_sweep_cron": followup_sweep_cron,
    "refresh_channels_cron": refresh_channels_cron,
}


# --------------------------------------------------------------------------- #
# ARQ
# --------------------------------------------------------------------------- #
class ArqJobQueue(JobQueue):
    """Прод-реализация поверх Redis.

    Пул создаётся лениво, в :meth:`startup`: конструктор в этом проекте не имеет
    права ходить в сеть. Если Redis недоступен, постановка задачи логируется как
    ошибка и возвращает пустой id — вызывающий (пайплайн, API) от этого не падает,
    а строка в БД остаётся и будет подобрана сметкой.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._pool: Any | None = None
        self._lock = asyncio.Lock()

    async def startup(self) -> None:
        """Поднимает пул Redis."""
        await self._get_pool()

    async def shutdown(self) -> None:
        """Закрывает пул."""
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                await pool.aclose()
            except AttributeError:  # pragma: no cover - старые версии redis-py
                await pool.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("arq_pool_close_failed", error=type(exc).__name__)

    async def enqueue_inbound(self, payload: dict[str, Any]) -> str:
        """Кладёт сырой payload вебхука на обработку. Возвращает job id.

        В отличие от исходящих, у входящего сообщения нет страховки: в БД оно
        попадает уже внутри задачи, сметки для него не существует. Поэтому провал
        постановки здесь — исключение, а не пустой id: вебхук обязан отличить
        приём от потери и сказать об этом в лог и в метрику.
        """
        return await self._enqueue(JOB_INBOUND, payload, required=True)

    async def enqueue_outbox(self, outbox_id: UUID, *, delay_ms: int = 0) -> str:
        """Ставит отправку строки outbox."""
        defer = timedelta(milliseconds=max(0, delay_ms)) if delay_ms else None
        return await self._enqueue(JOB_OUTBOX, str(outbox_id), defer_by=defer)

    async def enqueue_followup(self, task_id: UUID, *, run_at: datetime) -> str:
        """Планирует follow-up на конкретное время."""
        return await self._enqueue(JOB_FOLLOWUP, str(task_id), defer_until=run_at)

    async def _get_pool(self) -> Any:
        async with self._lock:
            if self._pool is None:
                self._pool = await create_pool(redis_settings(self._settings))
            return self._pool

    async def _enqueue(
        self,
        name: str,
        *args: Any,
        defer_by: timedelta | None = None,
        defer_until: datetime | None = None,
        required: bool = False,
    ) -> str:
        try:
            pool = await self._get_pool()
            job = await pool.enqueue_job(
                name, *args, _defer_by=defer_by, _defer_until=defer_until
            )
        except Exception as exc:  # noqa: BLE001 - Redis лёг: задачу подберёт сметка
            log.error("enqueue_failed", job=name, error=type(exc).__name__)
            if required:
                raise QueueUnavailableError(
                    f"задача {name} не поставлена в очередь: {type(exc).__name__}"
                ) from exc
            return ""
        job_id = getattr(job, "job_id", "") or ""
        if not job_id and required:
            # ``enqueue_job`` вернул None: задача с таким id уже в очереди либо
            # пул отдал пустой результат. Для входящего это тоже потеря.
            log.error("enqueue_failed", job=name, error="no_job_id")
            raise QueueUnavailableError(f"задача {name} не поставлена в очередь: пустой id")
        return job_id


# --------------------------------------------------------------------------- #
# Inline
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _InlineJob:
    """Одна задача внутренней очереди."""

    name: str
    args: tuple[Any, ...]
    run_at: float  # монотонное время цикла событий
    attempt: int = 1
    job_id: str = field(default_factory=lambda: uuid4().hex)


class InlineJobQueue(JobQueue):
    """``INLINE_WORKER=true``: задачи исполняются в том же процессе, честно и до конца.

    Реализация: внутренняя ``asyncio.Queue`` + пул из ``worker_max_jobs`` воркеров,
    поднятый в lifespan. Заглушкой быть не имеет права: задачи реально выполняются,
    ошибки логируются, graceful shutdown дожидается текущих задач.

    Отложенные задачи (backoff отправки, напоминания) держит отдельный планировщик,
    чтобы ожидание не занимало слот воркера. Периодические задания повторяют
    cron-расписание arq: ``outbox_sweep`` раз в минуту, ``followup_sweep`` раз в
    10 минут, ``refresh_channels`` раз в 15 минут.

    Расписанием владеет очередь и только она — признак ``owns_periodic_jobs``.
    По нему ``app.main`` понимает, что свой цикл периодических задач поднимать не
    нужно: два владельца расписания давали двойной прогон каждой сметки.
    """

    #: Периодические задания уже внутри: внешнему планировщику здесь делать нечего.
    owns_periodic_jobs: bool = True

    def __init__(self, settings: Settings, deps: "PipelineDeps") -> None:
        self._settings = settings
        self._deps = deps
        self._ctx: dict[str, Any] = {"deps": deps, "settings": settings, "inline": True}
        self._ready: asyncio.Queue[_InlineJob] = asyncio.Queue()
        self._delayed: list[_InlineJob] = []
        self._workers: list[asyncio.Task[None]] = []
        self._periodic: list[asyncio.Task[None]] = []
        self._scheduler: asyncio.Task[None] | None = None
        self._running = False

    # -- жизненный цикл --------------------------------------------------- #
    async def startup(self) -> None:
        """Поднимает пул воркеров, планировщик и периодические задания."""
        if self._running:
            return
        self._running = True
        size = max(1, int(self._settings.worker_max_jobs))
        self._workers = [
            asyncio.create_task(self._worker_loop(i), name=f"inline-worker-{i}")
            for i in range(size)
        ]
        self._scheduler = asyncio.create_task(self._scheduler_loop(), name="inline-scheduler")
        self._periodic = [
            asyncio.create_task(self._periodic_loop(name, interval), name=f"inline-cron-{name}")
            for name, interval in _INLINE_PERIODIC
        ]
        log.info("inline_worker_started", workers=size, periodic=len(self._periodic))

    async def shutdown(self) -> None:
        """Останавливает приём задач и дожидается текущих. Задачи не бросаются посреди работы."""
        if not self._running:
            return
        self._running = False

        # Сначала перестаём порождать новую работу, потом дожидаемся текущей.
        background = [*self._periodic]
        if self._scheduler is not None:
            background.append(self._scheduler)
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        self._periodic = []
        self._scheduler = None

        try:
            await asyncio.wait_for(self._ready.join(), timeout=INLINE_DRAIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("inline_worker_drain_timeout", pending=self._ready.qsize())

        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

        client = self._ctx.pop("wazzup", None)
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001
                log.debug("inline_wazzup_close_failed", error=type(exc).__name__)
        log.info("inline_worker_stopped")

    # -- постановка задач -------------------------------------------------- #
    async def enqueue_inbound(self, payload: dict[str, Any]) -> str:
        """Кладёт сырой payload вебхука на обработку. Возвращает job id."""
        return self._put(_InlineJob(JOB_INBOUND, (payload,), run_at=self._clock()))

    async def enqueue_outbox(self, outbox_id: UUID, *, delay_ms: int = 0) -> str:
        """Ставит отправку строки outbox."""
        return self._put(
            _InlineJob(
                JOB_OUTBOX, (str(outbox_id),), run_at=self._clock() + max(0, delay_ms) / 1000.0
            )
        )

    async def enqueue_followup(self, task_id: UUID, *, run_at: datetime) -> str:
        """Планирует follow-up на конкретное время."""
        delay = max(0.0, (_aware(run_at) - _now()).total_seconds())
        return self._put(_InlineJob(JOB_FOLLOWUP, (str(task_id),), run_at=self._clock() + delay))

    # -- внутреннее -------------------------------------------------------- #
    def _put(self, job: _InlineJob) -> str:
        if not self._running:
            # Очередь не поднята (тесты, ранний вызов) — выполняем сразу же в фоне,
            # иначе задача потеряется молча.
            log.warning("inline_queue_not_running", job=job.name)
            asyncio.get_running_loop().create_task(self._run(job))
            return job.job_id
        if job.run_at > self._clock():
            self._delayed.append(job)
        else:
            self._ready.put_nowait(job)
        metrics.set_worker_queue_depth(self._ready.qsize() + len(self._delayed))
        return job.job_id

    def _clock(self) -> float:
        return asyncio.get_running_loop().time()

    async def _scheduler_loop(self) -> None:
        """Переносит отложенные задачи в очередь готовых, когда наступает срок."""
        while True:
            await asyncio.sleep(_INLINE_TICK_S)
            if not self._delayed:
                continue
            now = self._clock()
            due = [job for job in self._delayed if job.run_at <= now]
            if not due:
                continue
            self._delayed = [job for job in self._delayed if job.run_at > now]
            for job in due:
                self._ready.put_nowait(job)
            metrics.set_worker_queue_depth(self._ready.qsize() + len(self._delayed))

    async def _worker_loop(self, index: int) -> None:
        """Один воркер пула: берёт задачу и доводит её до конца."""
        log.debug("inline_worker_ready", index=index)
        while True:
            job = await self._ready.get()
            try:
                await self._run(job)
            finally:
                self._ready.task_done()
                metrics.set_worker_queue_depth(self._ready.qsize() + len(self._delayed))

    async def _periodic_loop(self, name: str, interval: float) -> None:
        """Периодическое задание inline-режима — тот же смысл, что у cron в arq.

        Первый запуск отложен: на старте процесса важнее поднять API, а не
        разгребать очередь. Ошибка задания не останавливает цикл.
        """
        func = TASKS.get(name)
        if func is None:  # pragma: no cover - список задач статичен
            return
        await asyncio.sleep(min(15.0, interval))
        timeout = max(1, int(self._settings.worker_job_timeout_s))
        while True:
            ctx = dict(self._ctx)
            ctx["job_id"] = f"cron:{name}"
            ctx["job_try"] = 1
            try:
                await asyncio.wait_for(func(ctx), timeout=timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - цикл обязан пережить сбой задания
                metrics.observe_job_failure(name, type(exc).__name__)
                log.warning("inline_cron_failed", job=name, error=type(exc).__name__)
            finally:
                if "wazzup" in ctx and "wazzup" not in self._ctx:
                    self._ctx["wazzup"] = ctx["wazzup"]
            await asyncio.sleep(interval)

    async def _run(self, job: _InlineJob) -> None:
        """Выполняет задачу с таймаутом и обрабатывает запрос на повтор."""
        func = TASKS.get(job.name)
        if func is None:
            log.error("inline_unknown_job", job=job.name)
            return
        ctx = dict(self._ctx)
        ctx["job_id"] = job.job_id
        ctx["job_try"] = job.attempt
        timeout = max(1, int(self._settings.worker_job_timeout_s))
        try:
            await asyncio.wait_for(func(ctx, *job.args), timeout=timeout)
        except Retry as retry:
            self._defer(job, _retry_delay_s(retry))
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            metrics.observe_job_failure(job.name, "timeout")
            log.error("inline_job_timeout", job=job.name, timeout_s=timeout)
        except Exception as exc:  # noqa: BLE001 - воркер не имеет права умирать
            metrics.observe_job_failure(job.name, type(exc).__name__)
            log.exception("inline_job_failed", job=job.name, error=type(exc).__name__)
        finally:
            # Клиент Wazzup, созданный задачей, переиспользуется остальными.
            if "wazzup" in ctx and "wazzup" not in self._ctx:
                self._ctx["wazzup"] = ctx["wazzup"]

    def _defer(self, job: _InlineJob, delay_s: float) -> None:
        """Повтор задачи по её собственному запросу (``Retry``)."""
        if job.attempt >= MAX_TRIES:
            metrics.observe_job_failure(job.name, "max_tries")
            log.error("inline_job_max_tries", job=job.name, attempt=job.attempt)
            return
        metrics.observe_job_retry(job.name)
        self._put(
            _InlineJob(
                job.name,
                job.args,
                run_at=self._clock() + max(0.0, delay_s),
                attempt=job.attempt + 1,
                job_id=job.job_id,
            )
        )


# --------------------------------------------------------------------------- #
# Сборка
# --------------------------------------------------------------------------- #
def build_queue(settings: Settings, deps: "PipelineDeps") -> JobQueue:
    """Очередь по конфигурации: inline-пул при ``INLINE_WORKER=true``, иначе ARQ."""
    if settings.inline_worker:
        return InlineJobQueue(settings, deps)
    return ArqJobQueue(settings)


def redis_settings(settings: Settings | None = None) -> RedisSettings:
    """``RedisSettings`` из ``REDIS_URL``. Сети не касается — только разбор строки."""
    url = settings.redis_url if settings is not None else _cfg("redis_url", _REDIS_FALLBACK)
    try:
        return RedisSettings.from_dsn(url)
    except Exception:  # noqa: BLE001 - кривой URL не должен ломать импорт модуля
        log.error("redis_url_invalid")
        return RedisSettings.from_dsn(_REDIS_FALLBACK)


# --------------------------------------------------------------------------- #
# Жизненный цикл отдельного arq-процесса
# --------------------------------------------------------------------------- #
async def startup(ctx: dict[str, Any]) -> None:
    """Готовит контекст воркера: логи, настройки, KB, зависимости пайплайна.

    Отдельный процесс ``arq`` не проходит через lifespan FastAPI, поэтому всё,
    что там делает ``app/main.py``, нужно сделать здесь: настроить логирование,
    загрузить базу знаний, собрать ресурсы процесса.

    Ресурсы строятся ЗДЕСЬ ЖЕ (:func:`app.deps.build_runtime`), а не берутся из
    контейнера HTTP-процесса: в процессе ``arq`` этот контейнер пуст, и попытка
    прочитать его (``get_pipeline_deps`` → ``get_runtime`` → 503) роняла
    ``on_startup``. arq исключение из ``on_startup`` не ловит, поэтому отдельная
    служба воркера уходила в crash-loop, а входящие сообщения не обрабатывал
    никто: при ``INLINE_WORKER=false`` API кладёт задачи в Redis, и потребителя
    просто не было.
    """
    from app.logging_conf import configure_logging

    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    ctx["settings"] = settings

    await _ensure_kb(settings)

    from app import deps as deps_module

    runtime = deps_module.runtime_or_none()
    if runtime is None:
        # Обычный путь отдельной службы: контейнера в процессе ещё нет.
        # build_runtime сам поднимает движок БД, состояние, LLM, Wazzup и очередь.
        runtime = await deps_module.build_runtime(settings)
    else:
        # Воркер поднят внутри уже собранного процесса (тесты, INLINE-гибрид):
        # второй раз ресурсы не строим.
        try:
            await runtime.queue.startup()
        except Exception as exc:  # noqa: BLE001 - без пула воркер всё равно работает
            log.warning("worker_queue_startup_failed", error=type(exc).__name__)

    ctx["runtime"] = runtime
    ctx["deps"] = runtime.pipeline
    ctx["settings"] = runtime.settings
    ctx["state"] = runtime.state
    ctx["queue"] = runtime.queue
    ctx["sessionmaker"] = runtime.sessionmaker
    ctx["wazzup"] = runtime.wazzup
    log.info("worker_started", inline=False, max_jobs=settings.worker_max_jobs)


async def shutdown(ctx: dict[str, Any]) -> None:
    """Закрывает http-клиенты, очередь и пул БД."""
    runtime = ctx.pop("runtime", None)
    client = ctx.pop("wazzup", None)
    if client is not None and client is not getattr(runtime, "wazzup", None):
        # Клиент, созданный задачей, а не контейнером: закрыть его больше некому.
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001
            log.debug("worker_wazzup_close_failed", error=type(exc).__name__)

    if runtime is not None:
        from app import deps as deps_module

        # Закрывает очередь, LLM, Wazzup, состояние и движок БД — по одному,
        # с логом на каждый сбой.
        await deps_module.shutdown_runtime()
        ctx.pop("deps", None)
        log.info("worker_stopped")
        return

    deps = ctx.get("deps")
    queue = getattr(deps, "queue", None)
    if queue is not None:
        try:
            await queue.shutdown()
        except Exception as exc:  # noqa: BLE001
            log.debug("worker_queue_close_failed", error=type(exc).__name__)

    try:
        from app.storage.db import dispose_engine

        await dispose_engine()
    except Exception as exc:  # noqa: BLE001
        log.debug("worker_db_close_failed", error=type(exc).__name__)
    log.info("worker_stopped")


async def _ensure_kb(settings: Settings) -> None:
    """Загружает базу знаний, если снимка ещё нет.

    Без KB воркер не сможет ни ответить клиенту, ни отправить напоминание —
    поэтому провал загрузки это ошибка и метрика, а не молчание.
    """
    from app.kb import loader

    try:
        loader.get_snapshot()
        return
    except Exception:  # noqa: BLE001 - KBNotLoadedError: грузим сами
        pass
    try:
        snapshot = await loader.load(
            settings.kb_dir,
            media_dir=settings.media_dir,
            schema_version=settings.kb_schema_version,
        )
        loader.swap(snapshot)
        log.info("worker_kb_loaded", kb_hash=snapshot.kb_hash)
    except Exception as exc:  # noqa: BLE001
        metrics.observe_kb_load_failure()
        log.error("worker_kb_load_failed", error=type(exc).__name__)


# --------------------------------------------------------------------------- #
# Точка входа arq
# --------------------------------------------------------------------------- #
#: Локальный Redis: значение по умолчанию Settings.redis_url. Нужен только как
#: страховка, чтобы импорт модуля не зависел от настроек.
_REDIS_FALLBACK: Final[str] = "redis://localhost:6379/0"


def _cfg(name: str, default: Any) -> Any:
    """Настройка для тела класса ``WorkerSettings``.

    Ленивыми эти атрибуты сделать нельзя: arq читает их из ``settings_cls.__dict__``
    при создании воркера, а тело класса вычисляется на импорте. Поэтому значение
    берётся из настроек, а при любой проблеме — дефолт: модуль обязан
    импортироваться при пустом окружении.
    """
    try:
        return getattr(get_settings(), name)
    except Exception:  # noqa: BLE001
        return default


class WorkerSettings:
    """Точка входа arq: ``arq app.workers.queue.WorkerSettings``.

    ``max_tries`` = 4: первая попытка и три повтора. Больше не нужно — задача,
    которая не прошла четыре раза, не пройдёт и на пятый, а строка outbox всё
    равно останется в БД с ``state=failed`` и попадёт в тревогу администратору.

    ``retry_jobs=True`` означает, что задача, прерванная остановкой процесса
    (деплой на Railway), вернётся в очередь, а не потеряется.
    """

    functions: Sequence[Any] = [process_inbound_job, send_outbox_job, send_followup_job]
    cron_jobs: Sequence[Any] = [
        # Раз в 15 минут: жив ли канал Wazzup.
        cron(refresh_channels_cron, minute=REFRESH_CHANNELS_MINUTES, second=0, run_at_startup=True),
        # Раз в 10 минут: напоминания, которым настал срок.
        cron(followup_sweep_cron, minute=FOLLOWUP_SWEEP_MINUTES, second=0),
        # Раз в минуту: строки outbox, не попавшие в очередь, и метрика её глубины.
        cron(outbox_sweep_cron, second=0),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings: RedisSettings = redis_settings()
    max_jobs: int = _cfg("worker_max_jobs", 10)
    job_timeout: int = _cfg("worker_job_timeout_s", 120)
    max_tries: int = MAX_TRIES
    keep_result: int = KEEP_RESULT_S
    retry_jobs: bool = True
    poll_delay: float = 0.5
    health_check_interval: int = 60


# --------------------------------------------------------------------------- #
# Внутреннее
# --------------------------------------------------------------------------- #
def _retry_delay_s(retry: Retry) -> float:
    """Задержка из ``arq.worker.Retry`` в секундах."""
    score = getattr(retry, "defer_score", None)
    if score:
        return float(score) / 1000.0
    return 5.0


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


__all__ = [
    "ArqJobQueue",
    "FOLLOWUP_SWEEP_MINUTES",
    "INLINE_DRAIN_TIMEOUT_S",
    "InlineJobQueue",
    "JOB_FOLLOWUP",
    "JOB_INBOUND",
    "JOB_OUTBOX",
    "KEEP_RESULT_S",
    "MAX_TRIES",
    "QueueUnavailableError",
    "REFRESH_CHANNELS_MINUTES",
    "TASKS",
    "WorkerSettings",
    "build_queue",
    "redis_settings",
    "shutdown",
    "startup",
]
