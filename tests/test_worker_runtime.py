"""Регрессии группы D: старт отдельной службы воркера, деплой, HTTP-контур.

Каждый тест здесь падал бы до соответствующей правки. Сети нет: пул Redis
подменяется двойником, база — sqlite в памяти, время — параметрами функций,
а не ожиданием.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app import deps as app_deps
from app.storage import db as storage_db
from app.storage.models import Conversation, FollowupTask, OutboxMessage
from app.tools import content as content_tools
from app.types import ChannelKind, Language, OutboundKind, OutboundMessage
from app.workers import queue as queue_mod
from app.workers import tasks_followup, tasks_outbound

from tests.conftest import CHANNEL_ID


class _FakePool:
    """Двойник пула arq: помнит поставленное, в сеть не ходит."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    async def enqueue_job(self, name: str, *args: Any, **_kwargs: Any) -> Any:
        self.jobs.append((name, args))
        return SimpleNamespace(job_id=f"job-{len(self.jobs)}")

    async def aclose(self) -> None:
        self.closed = True


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# --------------------------------------------------------------------------- #
# blocker: отдельная служба воркера падала на старте
# --------------------------------------------------------------------------- #
async def test_worker_startup_builds_its_own_runtime(monkeypatch) -> None:
    """``arq app.workers.queue.WorkerSettings`` поднимается в пустом процессе.

    Раньше ``startup()`` читал контейнер HTTP-процесса (``get_pipeline_deps`` →
    ``get_runtime``), которого в процессе arq нет и быть не может: ``build_runtime``
    вызывался только из lifespan FastAPI. on_startup падал с 503, arq исключение не
    ловит — рекомендованный двухсервисный деплой уходил в crash-loop, и при
    ``INLINE_WORKER=false`` входящие сообщения не обрабатывал никто.
    """
    monkeypatch.setattr(queue_mod, "create_pool", lambda *_a, **_kw: _make_pool())
    saved_engine = storage_db._engine  # ставим свой движок и возвращаем чужой обратно
    app_deps.set_runtime(None)
    ctx: dict[str, Any] = {}
    try:
        await queue_mod.startup(ctx)

        assert ctx["deps"] is not None, "в контексте воркера нет PipelineDeps"
        assert ctx["deps"].sessionmaker is not None
        assert ctx["deps"].queue is ctx["queue"]
        assert app_deps.runtime_or_none() is not None
    finally:
        await queue_mod.shutdown(ctx)
        storage_db.set_engine(saved_engine) if saved_engine is not None else None
        app_deps.set_runtime(None)

    assert app_deps.runtime_or_none() is None, "shutdown обязан разобрать контейнер"


async def _make_pool() -> _FakePool:  # pragma: no cover - вспомогательная обёртка
    return _FakePool()


async def test_worker_startup_fails_loudly_only_on_real_resources(monkeypatch) -> None:
    """Единственная причина отказа старта воркера — недоступный ресурс, не 503 контейнера."""

    async def _boom(*_a: Any, **_kw: Any) -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(queue_mod, "create_pool", _boom)
    saved_engine = storage_db._engine
    app_deps.set_runtime(None)
    ctx: dict[str, Any] = {}
    try:
        with pytest.raises(ConnectionError):
            await queue_mod.startup(ctx)
    finally:
        await queue_mod.shutdown(ctx)
        if saved_engine is not None:
            storage_db.set_engine(saved_engine)
        app_deps.set_runtime(None)


# --------------------------------------------------------------------------- #
# medium: потеря входящего должна быть видна вебхуку
# --------------------------------------------------------------------------- #
async def test_enqueue_inbound_raises_when_redis_is_down(monkeypatch, settings) -> None:
    """Redis лёг: постановка входящего — исключение, исходящего — по-прежнему нет.

    У outbox есть страховка (``outbox_sweep_cron``), у входящего — никакой, поэтому
    молчаливый пустой job_id для него недопустим: вебхук обязан отличить приём от
    потери.
    """

    async def _boom(*_a: Any, **_kw: Any) -> Any:
        raise ConnectionError("redis down")

    monkeypatch.setattr(queue_mod, "create_pool", _boom)
    q = queue_mod.ArqJobQueue(settings)

    with pytest.raises(queue_mod.QueueUnavailableError):
        await q.enqueue_inbound({"messages": []})

    assert await q.enqueue_outbox(uuid4()) == ""


async def test_enqueue_inbound_raises_on_empty_job_id(monkeypatch, settings) -> None:
    """Пул вернул задачу без id — это тоже потеря, а не успех."""

    class _NoIdPool(_FakePool):
        async def enqueue_job(self, name: str, *args: Any, **_kw: Any) -> Any:
            return None

    async def _pool(*_a: Any, **_kw: Any) -> Any:
        return _NoIdPool()

    monkeypatch.setattr(queue_mod, "create_pool", _pool)
    q = queue_mod.ArqJobQueue(settings)

    with pytest.raises(queue_mod.QueueUnavailableError):
        await q.enqueue_inbound({"messages": []})


# --------------------------------------------------------------------------- #
# high: двойное расписание в INLINE_WORKER
# --------------------------------------------------------------------------- #
def _run_inline_lifespan(monkeypatch, settings, *, owns_periodic: bool) -> list[str]:
    """Поднимает приложение с INLINE_WORKER=true и говорит, запустился ли второй цикл."""
    import asyncio

    from fastapi.testclient import TestClient

    from app import main as app_main

    started: list[str] = []
    inline_settings = settings.model_copy(update={"inline_worker": True})
    queue = SimpleNamespace(
        shutdown=lambda: None,
        startup=lambda: None,
        owns_periodic_jobs=owns_periodic,
    )
    if not owns_periodic:
        del queue.owns_periodic_jobs
    runtime = SimpleNamespace(
        settings=inline_settings,
        queue=queue,
        queue_kind="StubQueue",
        pipeline=None,
        state=None,
        sessionmaker=None,
        llm=None,
        wazzup=None,
    )

    async def _build(_settings: Any) -> Any:
        return runtime

    async def _shutdown() -> None:
        return None

    async def _cron(_runtime: Any) -> None:
        started.append("cron")
        await asyncio.Event().wait()

    monkeypatch.setattr(app_deps, "build_runtime", _build)
    monkeypatch.setattr(app_deps, "shutdown_runtime", _shutdown)
    monkeypatch.setattr(app_main, "_inline_cron_loop", _cron)

    with TestClient(app_main.create_app(inline_settings)):
        pass
    return started


def test_inline_cron_is_not_started_next_to_queue_schedule(monkeypatch, settings) -> None:
    """Владелец расписания ровно один — очередь.

    ``InlineJobQueue.startup`` уже крутит outbox_sweep/followup_sweep/refresh_channels;
    lifespan поднимал те же три задания вторым циклом, и каждая сметка выполнялась
    дважды за интервал: двойная нагрузка на БД и дубль напоминания клиенту, если
    первый прогон не успел закоммитить ``state=sent``.
    """
    assert queue_mod.InlineJobQueue.owns_periodic_jobs is True
    assert _run_inline_lifespan(monkeypatch, settings, owns_periodic=True) == []


def test_inline_cron_still_runs_for_queue_without_schedule(monkeypatch, settings) -> None:
    """Очередь расписанием не владеет — сметки обязан крутить кто-то другой."""
    assert _run_inline_lifespan(monkeypatch, settings, owns_periodic=False) == ["cron"]


# --------------------------------------------------------------------------- #
# blocker: собственное эхо после repeatedCrmMessageId
# --------------------------------------------------------------------------- #
def _message(text: str = "Записываю вас на пробное") -> OutboundMessage:
    return OutboundMessage(
        conversation_id=None,
        channel_id=CHANNEL_ID,
        channel=ChannelKind.WHATSAPP,
        chat_id="77010000001",
        lang=Language.RU,
        kind=OutboundKind.BOT_REPLY,
        text=text,
    )


async def test_duplicate_keeps_row_identifiable_as_our_own(sessionmaker) -> None:
    """Дубль по crmMessageId не имеет права превратить наше сообщение в «чужое».

    После ``repeatedCrmMessageId`` id сообщения Wazzup нам неизвестен, а в вебхуке
    ``crmMessageId`` не приходит. Единственный способ узнать собственное эхо —
    свежая строка ``sent`` с тем же текстом. Если её нет, бот принимает своё
    сообщение за реплику оператора и ставит себе паузу на 30 минут.
    """
    from app.storage import repo_outbox

    message = _message()
    async with sessionmaker() as session:
        outbox_id = await repo_outbox.enqueue(session, message)
        await session.commit()

    deps = SimpleNamespace(sessionmaker=sessionmaker)
    await tasks_outbound._mark_duplicate_sent(deps, outbox_id, message)

    async with sessionmaker() as session:
        row = await repo_outbox.get(session, outbox_id)
        assert row.state == "sent"
        assert await repo_outbox.exists_recent_sent_text(
            session,
            channel_id=CHANNEL_ID,
            chat_id="77010000001",
            text=message.text or "",
            since=_now() - timedelta(minutes=5),
        ), "эхо этой отправки не опознать — бот поставит себе паузу"


async def test_duplicate_does_not_erase_known_message_id(sessionmaker) -> None:
    """Уже известный ``wazzup_message_id`` затирать нельзя: по нему узнают эхо."""
    from app.storage import repo_outbox

    message = _message("Привет!")
    async with sessionmaker() as session:
        outbox_id = await repo_outbox.enqueue(session, message)
        await repo_outbox.mark_sent(session, outbox_id, wazzup_message_id="wz-777")
        await session.commit()

    deps = SimpleNamespace(sessionmaker=sessionmaker)
    await tasks_outbound._mark_duplicate_sent(deps, outbox_id, message)

    async with sessionmaker() as session:
        row = await repo_outbox.get(session, outbox_id)
        assert row.wazzup_message_id == "wz-777"
        assert await repo_outbox.exists_by_wazzup_message_id(session, "wz-777")


# --------------------------------------------------------------------------- #
# low: ссылка на медиа протухает между решением и отправкой
# --------------------------------------------------------------------------- #
def test_media_link_is_signed_again_at_send_time(settings) -> None:
    """Строка outbox может пролежать час — токен живёт 10 минут.

    Отправка откладывается штатно: спам-блок канала возвращает строку в pending на
    ``CHANNEL_RETRY_DELAY_S`` и повторяет это до ``CHANNEL_STOP_TTL_S``; после
    рестарта строку подбирает сметка. Уходил тот же протухший токен, и Wazzup
    получал 404 вместо файла.
    """
    secret = settings.media_signing_key
    stale_token = content_tools.make_media_token("price/list.pdf", ttl_s=1, secret=secret)
    stale_url = f"{settings.public_base_url.rstrip('/')}/media/{stale_token}"
    later = _now() + timedelta(hours=1)

    with pytest.raises(ValueError):
        content_tools.parse_media_token(stale_token, secret=secret, now=later)

    message = OutboundMessage(
        channel_id=CHANNEL_ID,
        channel=ChannelKind.WHATSAPP,
        chat_id="77010000001",
        lang=Language.RU,
        kind=OutboundKind.ARTIFACT,
        content_uri=stale_url,
    )
    fresh = tasks_outbound._with_fresh_media(message)

    assert fresh.content_uri != stale_url
    fresh_token = (fresh.content_uri or "").rsplit("/media/", 1)[-1]
    assert content_tools.parse_media_token(fresh_token, secret=secret, now=_now()) == (
        "price/list.pdf"
    )


def test_foreign_link_is_left_alone(settings) -> None:
    """Ссылка не наша (подпись не сходится) — не трогаем и не ломаем отправку."""
    foreign = "https://cdn.example.com/media/not-our-token.sig"
    message = OutboundMessage(
        channel_id=CHANNEL_ID,
        channel=ChannelKind.WHATSAPP,
        chat_id="77010000001",
        lang=Language.RU,
        kind=OutboundKind.ARTIFACT,
        content_uri=foreign,
    )

    assert tasks_outbound._with_fresh_media(message).content_uri == foreign


# --------------------------------------------------------------------------- #
# low: два одинаковых напоминания при параллельных прогонах
# --------------------------------------------------------------------------- #
async def _followup_row(sessionmaker) -> tuple[Any, Any]:
    conv_id = uuid4()
    task_id = uuid4()
    async with sessionmaker() as session:
        session.add(
            Conversation(
                id=conv_id,
                conv_key=f"whatsapp:{CHANNEL_ID}:77010000001",
                chat_type=ChannelKind.WHATSAPP.value,
                channel_id=CHANNEL_ID,
                chat_id="77010000001",
                lang=Language.RU.value,
            )
        )
        session.add(
            FollowupTask(
                id=task_id,
                conversation_id=conv_id,
                kind="no_reply_2h",
                run_at=_now() - timedelta(minutes=1),
                state=tasks_followup.STATE_PENDING,
                attempt=0,
            )
        )
        await session.commit()
    return conv_id, task_id


async def test_second_runner_cannot_finish_the_same_followup(sessionmaker) -> None:
    """Захват задачи условный: второй прогон не переведёт её в sent повторно.

    Один task_id приходит двумя путями — отложенной задачей и сметкой раз в 10
    минут. Оба могли прочитать ``state=pending`` до коммита первого; без условия
    ``WHERE state = 'pending'`` оба доводили дело до конца, и родитель получал
    одно и то же напоминание дважды.
    """
    _, task_id = await _followup_row(sessionmaker)

    async with sessionmaker() as session:
        won = await tasks_followup._finish(
            session, task_id, tasks_followup.STATE_SENT, attempt=1, only_pending=True
        )
        await session.commit()
    assert won is True

    async with sessionmaker() as session:
        again = await tasks_followup._finish(
            session, task_id, tasks_followup.STATE_SENT, attempt=1, only_pending=True
        )
        await session.rollback()
    assert again is False, "проигравший прогон обязан узнать, что задачу уже забрали"


async def test_lost_race_rolls_back_the_outbox_row(sessionmaker, monkeypatch, settings) -> None:
    """Проигравший откатывает транзакцию целиком — вместе со своей строкой outbox."""
    conv_id, task_id = await _followup_row(sessionmaker)

    class _Queue:
        def __init__(self) -> None:
            self.outbox: list[Any] = []

        async def enqueue_outbox(self, outbox_id: Any, *, delay_ms: int = 0) -> str:
            self.outbox.append(outbox_id)
            return "job"

    queue = _Queue()
    deps = SimpleNamespace(sessionmaker=sessionmaker, settings=settings, queue=queue, state=None)

    async def _message_for(*_a: Any, **_kw: Any) -> OutboundMessage:
        return _message("Не дозвонились — напоминаем про пробное")

    async def _no_skip(*_a: Any, **_kw: Any) -> None:
        return None

    async def _lost(*_a: Any, **kw: Any) -> bool:
        # Победителя изображаем честно: задача уже переведена в sent другим прогоном.
        return False if kw.get("only_pending") else True

    monkeypatch.setattr(tasks_followup, "_build_message", _message_for)
    monkeypatch.setattr(tasks_followup, "_skip_reason", _no_skip)
    monkeypatch.setattr(tasks_followup, "_finish", _lost)

    await tasks_followup.send_followup_job({"deps": deps}, str(task_id))

    assert queue.outbox == [], "проигравший поставил дубль в очередь отправки"
    async with sessionmaker() as session:
        rows = (await session.execute(sa.select(OutboxMessage.id))).scalars().all()
    assert rows == [], "строка outbox проигравшего осталась в базе — уйдёт второе напоминание"
    assert conv_id is not None


# --------------------------------------------------------------------------- #
# medium: rate limit админки обходился заголовком
# --------------------------------------------------------------------------- #
def _request_with_xff(value: str) -> Any:
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/admin/stats",
        "headers": [(b"x-forwarded-for", value.encode("ascii"))],
        "client": (value.split(",")[0].strip(), 1234),
        "query_string": b"",
    }
    return Request(scope)


async def test_rate_limit_key_ignores_client_supplied_forwarded_for() -> None:
    """Подделка X-Forwarded-For больше не даёт каждому запросу свой счётчик.

    Процесс стартует с ``--forwarded-allow-ips='*'``, uvicorn берёт САМОЕ ЛЕВОЕ
    значение заголовка и кладёт его в ``scope['client']`` — счёт по нему означал
    неограниченный перебор ADMIN_TOKEN.
    """
    app_deps.reset_admin_rate_limit()
    try:
        for i in range(app_deps.ADMIN_RATE_LIMIT):
            await app_deps.admin_rate_limit(_request_with_xff(f"10.0.0.{i % 250}, 203.0.113.7"))

        with pytest.raises(Exception) as exc:
            await app_deps.admin_rate_limit(_request_with_xff("10.9.9.9, 203.0.113.7"))
        assert getattr(exc.value, "status_code", None) == 429
    finally:
        app_deps.reset_admin_rate_limit()


# --------------------------------------------------------------------------- #
# low: /metrics и /readyz на публичном домене
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "header,expected",
    [
        (None, False),
        ("Bearer wrong-token-wrong-token-000", False),
        ("Basic a-b-c", False),
        ("Bearer " + "s" * 32, True),
    ],
)
def test_ops_endpoints_require_admin_bearer_outside_local(header, expected) -> None:
    """На публичном домене Railway дамп Prometheus и разбивка /readyz — по токену."""
    from app.api import health

    prod = SimpleNamespace(app_env="production", admin_token="s" * 32)
    assert health._ops_authorized(header, prod) is expected


def test_ops_endpoints_stay_open_locally() -> None:
    """Локальная разработка не должна требовать токен."""
    from app.api import health

    local = SimpleNamespace(app_env="local", admin_token=None)
    assert health._ops_authorized(None, local) is True


def test_metrics_closed_when_prod_has_no_admin_token() -> None:
    """Токена нет — метрики закрыты, а не открыты всему интернету."""
    from app.api import health

    prod = SimpleNamespace(app_env="production", admin_token=None)
    assert health._ops_authorized("Bearer whatever", prod) is False


# --------------------------------------------------------------------------- #
# low: пул БД уже, чем число параллельных задач
# --------------------------------------------------------------------------- #
def test_pool_is_not_narrower_than_worker_concurrency() -> None:
    """5+5 при worker_max_jobs=10 — очередь за соединением ровно на пике.

    Сессия держится весь ход, включая вызовы Gemini, поэтому отправка готовых
    ответов вставала на pool_timeout и уходила в ретраи.
    """
    cfg = SimpleNamespace(worker_max_jobs=10)
    size, overflow = storage_db._fit_pool_to_workers(5, 5, cfg)

    assert size + overflow >= 10 + storage_db.POOL_HEADROOM
    # Заданный вручную запас не урезаем.
    assert storage_db._fit_pool_to_workers(20, 10, cfg) == (20, 10)


async def test_sweep_ignores_telegram_rows(sessionmaker) -> None:
    """Воркер Wazzup не забирает сообщения Telegram-бота.

    Строки в очередь пишет общий пайплайн, а отправляют их разные транспорты:
    Wazzup — воркером, Telegram — собственным циклом опроса. Без фильтра воркер
    пытался бы отправить telegram-адрес через чужой канал, и каждая такая
    строка уходила бы в бесконечный ретрай.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    import sqlalchemy as sa

    from app.storage import repo_outbox
    from app.storage.models import Base, Conversation
    from app.types import ChannelKind, Language, OutboundKind, OutboundMessage

    async with sessionmaker() as session:
        conv_id = uuid4()
        session.add(
            Conversation(
                id=conv_id,
                conv_key=f"k-{conv_id}",
                channel_id="ch",
                chat_type="whatsapp",
                chat_id="77010000001",
                state="new",
            )
        )
        await session.flush()

        for channel, chat in (
            (ChannelKind.WHATSAPP, "77010000001"),
            (ChannelKind.TELEGRAM, "647787396"),
        ):
            await repo_outbox.enqueue(
                session,
                OutboundMessage(
                    conversation_id=conv_id,
                    channel_id="ch",
                    channel=channel,
                    chat_id=chat,
                    lang=Language.RU,
                    kind=OutboundKind.BOT_REPLY,
                    text=f"ответ для {channel.value}",
                ),
            )
        await session.commit()

        due = await repo_outbox.due(session, datetime.now(tz=UTC))
        assert len(due) == 1, "воркер забрал не только свои сообщения"

        payload = (
            await session.execute(
                sa.text("SELECT json_extract(payload, '$.channel') FROM outbox_message WHERE id = :i"),
                {"i": str(due[0]).replace("-", "")},
            )
        ).scalar()
        assert payload == "whatsapp"


async def test_mark_sent_by_crm_id_closes_the_row(sessionmaker) -> None:
    """Канал, который отправляет сам, закрывает свою строку очереди.

    Telegram-бот доставляет сообщения из решения пайплайна, а не воркером.
    Без отметки строка навсегда осталась бы «в очереди», и счётчик
    неотправленного рос бы с каждым ответом бота.
    """
    from uuid import uuid4

    import sqlalchemy as sa

    from app.storage import repo_outbox
    from app.storage.models import Conversation
    from app.types import ChannelKind, Language, OutboundKind, OutboundMessage

    async with sessionmaker() as session:
        conv_id = uuid4()
        session.add(
            Conversation(
                id=conv_id,
                conv_key=f"tg-{conv_id}",
                channel_id="telegram-bot-api",
                chat_type="telegram",
                chat_id="647787396",
                state="new",
            )
        )
        await session.flush()

        message = OutboundMessage(
            conversation_id=conv_id,
            channel_id="telegram-bot-api",
            channel=ChannelKind.TELEGRAM,
            chat_id="647787396",
            lang=Language.RU,
            kind=OutboundKind.BOT_REPLY,
            text="ответ",
        )
        await repo_outbox.enqueue(session, message)
        await session.commit()

        assert await repo_outbox.pending_count(session) == 1
        assert await repo_outbox.mark_sent_by_crm_id(session, message.crm_message_id) is True
        await session.commit()
        assert await repo_outbox.pending_count(session) == 0

        # Повторная отметка ничего не меняет: доставка идемпотентна.
        assert await repo_outbox.mark_sent_by_crm_id(session, message.crm_message_id) is False
        state = (
            await session.execute(
                sa.text("SELECT state FROM outbox_message WHERE conversation_id = :c"),
                {"c": str(conv_id).replace("-", "")},
            )
        ).scalar()
        assert state == "sent"
