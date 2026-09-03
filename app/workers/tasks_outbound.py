"""Отправка исходящих: outbox → Wazzup.

Единственное место в проекте, где вызывается ``WazzupClient.send_message``.
Пайплайн только кладёт строки в ``outbox_message``; отправляет их этот воркер.

Идемпотентность — жёсткая. ``crmMessageId`` берётся из строки outbox и не
меняется между попытками, поэтому повтор после сетевого таймаута не даёт клиенту
второе одинаковое сообщение: Wazzup 60 секунд помнит этот идентификатор и
отвечает ``repeatedCrmMessageId``, что для нас **успех**, а не ошибка.

Классификация ошибок целиком делегирована ``app.channels.errors.disposition``:

* ``retriable`` — 429 и 5xx: экспоненциальный backoff 1/2/4/8 с + джиттер,
  до ``wazzup_send_max_attempts`` попыток, затем ``state=failed`` и эскалация;
* ``duplicate`` — сообщение уже ушло: ``state=sent``, метрика ошибок не растёт;
* ``needs_human`` — спам-блок, мёртвый канал, закрытое окно Instagram: повтор
  бесполезен, нужен живой человек;
* ``fatal`` — 400/401/403: чинится кодом, а не повтором.

Сообщение в закрытое окно мессенджера не отправляется вовсе: Instagram даёт
7 суток с последнего входящего и не разрешает писать первым. Это техническое
ограничение канала, а не наше решение.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Final
from uuid import UUID

from app.channels.errors import ErrorDisposition, disposition, normalize_code
from app.core import pause
from app.channels.outbound import build_send_request, check_send_allowed, next_backoff_ms
from app.logging_conf import bind_correlation, clear_correlation, get_logger
from app.observability import metrics
from app.types import (
    LeadStatus,
    OutboundKind,
    OutboundMessage,
    WazzupBadContactError,
    WazzupChannelError,
    WazzupDuplicateError,
    WazzupError,
    WazzupSpamError,
)

log = get_logger(__name__)

TASK_NAME: Final[str] = "send_outbox"

#: Канал остановлен (спам-блок или авария) — исходящие ждут столько секунд.
CHANNEL_STOP_TTL_S: Final[int] = 3600

#: Через столько секунд остановленный канал пробуется снова.
CHANNEL_RETRY_DELAY_S: Final[int] = 300

#: Ключ «канал остановлен» в StateStore.
_CHANNEL_STOP_KEY: Final[str] = "channel:stop:{channel_id}"

#: Сколько строк за один проход разгребает cron-сметка.
SWEEP_LIMIT: Final[int] = 50


async def send_outbox_job(ctx: dict[str, Any], outbox_id: str) -> None:
    """Отправка одной строки outbox с тем же ``crmMessageId`` при каждом ретрае.

    429 и 5xx → backoff 1/2/4/8 с + jitter, до ``wazzup_send_max_attempts``, затем
    ``state=failed`` и эскалация. 400/401/403 не ретраятся. ``WazzupDuplicateError``
    → ``state=sent`` (сообщение уже ушло). ``MESSAGES_IS_SPAM`` → стоп канала +
    тревога менеджеру. ``BAD_CONTACT`` → пометить лид. ``CHANNEL_*`` → общий алерт,
    бот молчит, входящие копятся.
    """
    from app.storage import repo_conversation, repo_outbox

    deps = _deps(ctx)
    settings = deps.settings
    bind_correlation(str(outbox_id), task=TASK_NAME)
    started = time.perf_counter()

    try:
        try:
            oid = UUID(str(outbox_id))
        except (TypeError, ValueError):
            log.error("outbox_bad_id", outbox_id=str(outbox_id))
            return

        now = _now()
        async with deps.sessionmaker() as session:
            row = await repo_outbox.get(session, oid)
            if row is None:
                log.warning("outbox_row_missing", outbox_id=str(oid))
                return
            channel_id = str(row.payload.get("channel_id") or "")
            if await _channel_stopped(deps, channel_id):
                # Канал остановлен: не жжём попытку, просто отложим строку.
                await repo_outbox.mark_failed(
                    session,
                    oid,
                    error="channel_stopped",
                    next_attempt_at=now + timedelta(seconds=CHANNEL_RETRY_DELAY_S),
                )
                await session.commit()
                log.info("outbox_deferred_channel_stopped", outbox_id=str(oid))
                return

            claimed = await repo_outbox.claim(session, oid)
            if claimed is None:
                await session.commit()
                log.debug("outbox_already_claimed", outbox_id=str(oid))
                return

            attempt = int(claimed.attempts)
            conversation_id = claimed.conversation_id
            message = _message_of(claimed)
            if message is not None:
                message = _with_fresh_media(message)
            if message is None:
                await repo_outbox.mark_skipped(session, oid, error="bad_payload")
                await session.commit()
                log.error("outbox_bad_payload", outbox_id=str(oid))
                return

            conv = (
                await repo_conversation.get_by_id(session, conversation_id)
                if conversation_id is not None
                else None
            )
            if await _stale_after_takeover(session, conv, claimed, message, now=now):
                await repo_outbox.mark_skipped(session, oid, error="operator_took_over")
                await session.commit()
                log.info(
                    "outbox_skipped_operator",
                    outbox_id=str(oid),
                    kind=message.kind.value,
                )
                return

            deny = check_send_allowed(
                channel=message.channel,
                last_inbound_at=_aware(getattr(conv, "last_inbound_at", None)),
                now=now,
                kind=message.kind,
            )
            if deny is not None:
                await repo_outbox.mark_skipped(session, oid, error=deny)
                await session.commit()
                metrics.observe_outbound_failed(message.channel, "window")
                log.warning(
                    "outbox_skipped_window",
                    outbox_id=str(oid),
                    channel=message.channel.value,
                    reason=deny,
                )
                await _alert(deps, f"Не отправлено: {deny}", code="send_window_closed")
                return

            await session.commit()

        await _deliver(
            ctx,
            deps,
            oid,
            message,
            attempt=attempt,
            max_attempts=int(settings.wazzup_send_max_attempts),
            base_ms=int(settings.wazzup_send_backoff_base_ms),
            conversation_id=conversation_id,
        )
    finally:
        metrics.observe_job(TASK_NAME, time.perf_counter() - started)
        clear_correlation()


async def send_outbox_batch(ctx: dict[str, Any], limit: int = SWEEP_LIMIT) -> int:
    """Пачка из outbox: всё, чему пора уходить. Возвращает число обработанных строк.

    Используется сметкой по расписанию — она подбирает строки, которые не попали
    в очередь (перезапуск процесса, потерянная задача, наступивший срок ретрая).
    """
    from app.storage import repo_outbox

    deps = _deps(ctx)
    async with deps.sessionmaker() as session:
        ids = await repo_outbox.due(session, _now(), limit=limit)
        await session.commit()

    for outbox_id in ids:
        try:
            await send_outbox_job(ctx, str(outbox_id))
        except Exception as exc:  # noqa: BLE001 - одна плохая строка не рушит пачку
            log.warning("outbox_batch_item_failed", outbox_id=str(outbox_id), error=type(exc).__name__)
    return len(ids)


async def outbox_sweep_cron(ctx: dict[str, Any]) -> None:
    """Разгребает outbox и обновляет метрики очереди. Раз в минуту.

    Нужна на случай, когда строка легла в БД, а задача в очередь не попала:
    процесс перезапустили, Redis моргнул, задачу потеряли. Без сметки такое
    сообщение не ушло бы никогда.
    """
    from app.storage import repo_outbox

    deps = _deps(ctx)
    processed = await send_outbox_batch(ctx, SWEEP_LIMIT)
    try:
        async with deps.sessionmaker() as session:
            metrics.set_outbox_pending(await repo_outbox.pending_count(session))
    except Exception as exc:  # noqa: BLE001 - метрика не повод падать
        log.debug("outbox_gauge_failed", error=type(exc).__name__)
    if processed:
        log.info("outbox_sweep_done", processed=processed)


async def refresh_channels_cron(ctx: dict[str, Any]) -> None:
    """``GET /v3/channels`` раз в 15 минут: жив ли канал.

    Неактивный канал означает, что бот молча перестал отвечать всем клиентам —
    это тревога, а не запись в логе. Ожившему каналу снимается стоп-флаг.
    """
    deps = _deps(ctx)
    settings = deps.settings
    try:
        client = await _client(ctx, deps)
        channels = await client.get_channels()
    except Exception as exc:  # noqa: BLE001 - недоступность Wazzup сама по себе тревога
        log.warning("channels_refresh_failed", error=type(exc).__name__)
        return

    wanted = {
        cid
        for cid in (settings.wazzup_channel_id_whatsapp, settings.wazzup_channel_id_instagram)
        if cid
    }
    broken: list[str] = []
    for channel in channels:
        metrics.observe_channel_state(channel.channel_id, channel.transport, channel.is_active)
        if channel.is_active:
            await _clear_channel_stop(deps, channel.channel_id)
        elif not wanted or channel.channel_id in wanted:
            broken.append(f"{channel.transport}:{channel.channel_id} = {channel.state}")

    if broken:
        await _alert(
            deps,
            "Канал Wazzup не в рабочем состоянии: " + "; ".join(broken),
            code="channel_inactive",
        )
    log.info("channels_refreshed", total=len(channels), broken=len(broken))


# --------------------------------------------------------------------------- #
# Отправка
# --------------------------------------------------------------------------- #
async def _deliver(
    ctx: dict[str, Any],
    deps: Any,
    outbox_id: UUID,
    message: OutboundMessage,
    *,
    attempt: int,
    max_attempts: int,
    base_ms: int,
    conversation_id: UUID | None,
) -> None:
    """Сетевой вызов и разбор исхода. Транзакция БД на время запроса не держится."""
    from app.storage import repo_conversation, repo_outbox

    try:
        client = await _client(ctx, deps)
        response = await client.send_message(build_send_request(message))
    except WazzupDuplicateError:
        # Сообщение уже ушло минуту назад: идемпотентность отработала как задумано.
        await _mark_duplicate_sent(deps, outbox_id, message)
        log.info("outbox_duplicate", outbox_id=str(outbox_id), kind=message.kind.value)
        return
    except WazzupError as exc:
        await _handle_error(
            deps,
            outbox_id,
            message,
            exc,
            attempt=attempt,
            max_attempts=max_attempts,
            base_ms=base_ms,
            conversation_id=conversation_id,
        )
        return
    except Exception as exc:  # noqa: BLE001 - сеть, DNS, таймаут httpx
        wrapped = WazzupError(f"Сетевая ошибка отправки: {type(exc).__name__}", status=None)
        wrapped.retryable = True
        await _handle_error(
            deps,
            outbox_id,
            message,
            wrapped,
            attempt=attempt,
            max_attempts=max_attempts,
            base_ms=base_ms,
            conversation_id=conversation_id,
        )
        return

    wazzup_message_id = getattr(response, "messageId", None)
    now = _now()
    async with deps.sessionmaker() as session:
        await repo_outbox.mark_sent(session, outbox_id, wazzup_message_id=wazzup_message_id)
        if conversation_id is not None:
            await repo_conversation.touch_outbound(session, conversation_id, now)
        await session.commit()

    metrics.observe_outbound_sent(message.channel, message.kind)
    log.info(
        "outbox_sent",
        outbox_id=str(outbox_id),
        kind=message.kind.value,
        channel=message.channel.value,
        attempt=attempt,
        wazzup_message_id=wazzup_message_id,
    )


#: Что уходит клиенту даже в диалоге, который ведёт человек. Ответ самого
#: человека из CRM — очевидно; карточка администратору идёт не клиенту, а ему.
_SENT_DESPITE_TAKEOVER: Final[frozenset[OutboundKind]] = frozenset(
    {OutboundKind.OPERATOR_REPLY, OutboundKind.MANAGER_CARD}
)


async def _stale_after_takeover(
    session: Any,
    conv: Any,
    row: Any,
    message: OutboundMessage,
    *,
    now: datetime,
) -> bool:
    """Устарела ли строка: человек вошёл в разговор уже после её постановки.

    Проверка окна канала здесь была, а этой не было. Строка ответа лежит в
    очереди до минуты, и за эту минуту в диалог мог войти живой человек: бот
    писал поверх него — ровно то, на что жаловался владелец.

    Сравнивается не «стоит ли пауза», а что было раньше: реплика человека или
    постановка строки. Иначе «передаю администратору» не ушло бы никогда — эту
    строку ставит тот же ход, который и объявляет паузу.
    """
    if message.kind in _SENT_DESPITE_TAKEOVER or conv is None:
        return False
    try:
        seen = await pause.operator_last_seen(session, conv.id)
    except Exception as exc:  # noqa: BLE001 - отправку не роняем из-за проверки
        log.warning("takeover_check_failed", error=type(exc).__name__)
        return False
    if seen is None:
        return False
    queued = _aware(getattr(row, "created_at", None)) or now
    return seen > queued


async def _handle_error(
    deps: Any,
    outbox_id: UUID,
    message: OutboundMessage,
    exc: WazzupError,
    *,
    attempt: int,
    max_attempts: int,
    base_ms: int,
    conversation_id: UUID | None,
) -> None:
    """Раскладывает ошибку отправки по трём исходам: повтор, человек, отказ."""
    from app.storage import repo_outbox

    code = normalize_code(exc.error_code) or exc.code
    metrics.observe_wazzup_error(code)
    kind = disposition(exc)

    if kind is ErrorDisposition.DUPLICATE:
        await _mark_duplicate_sent(deps, outbox_id, message)
        return

    if kind is ErrorDisposition.RETRIABLE and attempt < max_attempts:
        delay_ms = next_backoff_ms(attempt, base_ms=base_ms)
        next_at = _now() + timedelta(milliseconds=delay_ms)
        async with deps.sessionmaker() as session:
            await repo_outbox.mark_failed(
                session, outbox_id, error=f"{code}: {exc.message}", next_attempt_at=next_at
            )
            await session.commit()
        metrics.observe_job_retry(TASK_NAME)
        log.warning(
            "outbox_retry",
            outbox_id=str(outbox_id),
            attempt=attempt,
            delay_ms=delay_ms,
            code=code,
        )
        try:
            await deps.queue.enqueue_outbox(outbox_id, delay_ms=delay_ms)
        except Exception as queue_exc:  # noqa: BLE001 - строку подберёт outbox_sweep_cron
            log.warning("outbox_requeue_failed", error=type(queue_exc).__name__)
        return

    # Дальше — терминальные исходы: повторять нечего.
    reason = "exhausted" if kind is ErrorDisposition.RETRIABLE else kind.value
    async with deps.sessionmaker() as session:
        await repo_outbox.mark_failed(
            session, outbox_id, error=f"{code}: {exc.message}", next_attempt_at=None
        )
        await session.commit()
    metrics.observe_outbound_failed(message.channel, reason)
    log.error(
        "outbox_failed",
        outbox_id=str(outbox_id),
        attempt=attempt,
        code=code,
        disposition=reason,
        channel=message.channel.value,
    )

    await _react_terminal(
        deps, message, exc, code=code, reason=reason, conversation_id=conversation_id
    )


async def _react_terminal(
    deps: Any,
    message: OutboundMessage,
    exc: WazzupError,
    *,
    code: str,
    reason: str,
    conversation_id: UUID | None,
) -> None:
    """Что делать с каналом, лидом и администратором после терминальной ошибки."""
    if isinstance(exc, WazzupSpamError):
        await _stop_channel(deps, message.channel_id)
        await _alert(
            deps,
            f"WhatsApp пометил отправку как спам, канал {message.channel_id} остановлен. "
            "Лид не потерян, ответьте клиенту вручную.",
            code="wazzup_spam",
        )
        return

    if isinstance(exc, WazzupChannelError):
        await _stop_channel(deps, message.channel_id)
        await _alert(
            deps,
            f"Канал {message.channel_id} не работает ({code}). Бот молчит, входящие копятся.",
            code="wazzup_channel",
        )
        return

    if isinstance(exc, WazzupBadContactError):
        await _flag_lead(deps, conversation_id)
        await _alert(
            deps,
            f"Не доставлено клиенту ({code}): номера нет в мессенджере либо окно диалога закрыто. "
            "Нужен звонок.",
            code="wazzup_bad_contact",
        )
        return

    await _alert(
        deps,
        f"Сообщение клиенту не отправлено ({code}, {reason}). Ответьте вручную.",
        code="wazzup_send_failed",
    )


# --------------------------------------------------------------------------- #
# Побочные эффекты
# --------------------------------------------------------------------------- #
async def _flag_lead(deps: Any, conversation_id: UUID | None) -> None:
    """Помечает лид как требующий звонка: писать в мессенджер бесполезно."""
    if conversation_id is None:
        return
    from app.storage import repo_lead

    try:
        async with deps.sessionmaker() as session:
            lead = await repo_lead.get_by_conversation(session, conversation_id)
            if lead is None:
                return
            lead.status = LeadStatus.NEEDS_CALL.value
            lead.escalation = True
            await session.commit()
            metrics.observe_lead(LeadStatus.NEEDS_CALL)
    except Exception as exc:  # noqa: BLE001
        log.warning("lead_flag_failed", error=type(exc).__name__)


async def _stop_channel(deps: Any, channel_id: str | None) -> None:
    """Ставит стоп-флаг канала: исходящие откладываются, а не теряются."""
    if not channel_id:
        return
    state = getattr(deps, "state", None)
    if state is None:
        return
    try:
        await state.set(_CHANNEL_STOP_KEY.format(channel_id=channel_id), "1", CHANNEL_STOP_TTL_S)
    except Exception as exc:  # noqa: BLE001
        log.debug("channel_stop_failed", error=type(exc).__name__)


async def _clear_channel_stop(deps: Any, channel_id: str) -> None:
    """Канал ожил — снимаем стоп."""
    state = getattr(deps, "state", None)
    if state is None:
        return
    try:
        await state.delete(_CHANNEL_STOP_KEY.format(channel_id=channel_id))
    except Exception as exc:  # noqa: BLE001
        log.debug("channel_resume_failed", error=type(exc).__name__)


async def _channel_stopped(deps: Any, channel_id: str) -> bool:
    """Остановлен ли канал прямо сейчас."""
    if not channel_id:
        return False
    state = getattr(deps, "state", None)
    if state is None:
        return False
    try:
        return await state.get(_CHANNEL_STOP_KEY.format(channel_id=channel_id)) is not None
    except Exception:  # noqa: BLE001 - Redis лёг: пробуем отправить, хуже не будет
        return False


async def _alert(deps: Any, text: str, *, code: str) -> None:
    """Тревога администратору с подавлением повторов."""
    try:
        from app.notify.manager import notify_alert

        await notify_alert(deps, text, code=code)
    except Exception as exc:  # noqa: BLE001
        log.warning("outbound_alert_failed", code=code, error=type(exc).__name__)


# --------------------------------------------------------------------------- #
# Внутреннее
# --------------------------------------------------------------------------- #
def _message_of(row: Any) -> OutboundMessage | None:
    """Строка outbox → ``OutboundMessage``.

    ``crm_message_id`` принудительно берётся из колонки: это и есть ключ
    идемпотентности, он обязан пережить любое количество повторов.
    """
    payload = dict(row.payload or {})
    payload["crm_message_id"] = str(row.crm_message_id)
    if row.conversation_id is not None:
        payload["conversation_id"] = str(row.conversation_id)
    try:
        return OutboundMessage.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - битый payload чинится кодом, не повтором
        log.error("outbox_payload_invalid", error=type(exc).__name__)
        return None


async def _mark_duplicate_sent(
    deps: Any, outbox_id: UUID, message: OutboundMessage
) -> None:
    """``repeatedCrmMessageId`` — это успех: строка становится ``sent``.

    Два требования, из-за которых это отдельная функция.

    1. Уже известный ``wazzup_message_id`` НЕ затирается. Именно по нему пайплайн
       узнаёт собственное эхо (``repo_outbox.exists_by_wazzup_message_id``);
       записать вместо него ``None`` значит своими руками сделать эхо «чужим».
    2. Когда id так и остался неизвестным (штатный случай дубля: ответ Wazzup на
       первую попытку до нас не доехал, а в вебхуке ``crmMessageId`` не приходит),
       пишем предупреждение. Опознать такое эхо можно только по тексту
       (``repo_outbox.exists_recent_sent_text``), и это работает лишь потому, что
       строка помечается ``sent`` вот прямо сейчас, с текстом в ``payload``.
       Иначе бот принимает собственное сообщение за реплику оператора и ставит
       себе паузу на 30 минут — клиент остаётся без ответа.
    """
    from app.storage import repo_outbox

    async with deps.sessionmaker() as session:
        row = await repo_outbox.get(session, outbox_id)
        known = getattr(row, "wazzup_message_id", None)
        await repo_outbox.mark_sent(session, outbox_id, wazzup_message_id=known)
        await session.commit()
    if not known:
        log.warning(
            "outbox_sent_unidentified",
            outbox_id=str(outbox_id),
            kind=message.kind.value,
            reason="duplicate_without_message_id",
        )


def _with_fresh_media(message: OutboundMessage) -> OutboundMessage:
    """Пере-подписывает ссылку на медиа в момент фактической отправки.

    Токен выпускается пайплайном (``content.build_media_url``) и живёт
    ``MEDIA_TOKEN_TTL_S`` = 10 минут, а строка outbox может пролежать час:
    спам-блок канала откладывает её на ``CHANNEL_RETRY_DELAY_S`` раз за разом,
    после рестарта процесса её подбирает сметка. Из ``payload`` восстанавливается
    ТОТ ЖЕ токен, и Wazzup, скачивая ``contentUri``, получает 404 — файл клиенту
    не приходит, а попытки жгутся впустую. Поэтому непосредственно перед
    отправкой ссылка выпускается заново.

    Чужой (не наш) ``content_uri`` не трогаем: подпись не сойдётся, вернём как есть.
    """
    uri = message.content_uri
    if not uri:
        return message
    try:
        from app.tools.content import refresh_media_url

        fresh = refresh_media_url(uri)
    except Exception as exc:  # noqa: BLE001 - протухшая ссылка лучше, чем несостоявшаяся отправка
        log.warning("outbox_media_refresh_failed", error=type(exc).__name__)
        return message
    if fresh == uri:
        return message
    return message.model_copy(update={"content_uri": fresh})


async def _client(ctx: dict[str, Any], deps: Any) -> Any:
    """Ленивый ``WazzupClient``, живущий столько же, сколько воркер."""
    client = ctx.get("wazzup")
    if client is None:
        from app.channels.wazzup_client import WazzupClient

        client = WazzupClient.from_settings(deps.settings)
        ctx["wazzup"] = client
    return client


def _deps(ctx: dict[str, Any]) -> Any:
    """``PipelineDeps`` из контекста воркера."""
    deps = ctx.get("deps")
    if deps is None:
        raise RuntimeError("В контексте воркера нет PipelineDeps: не отработал startup")
    return deps


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """Наивное время из SQLite считаем UTC: иначе сравнение с ``now`` падает."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


__all__ = [
    "CHANNEL_RETRY_DELAY_S",
    "CHANNEL_STOP_TTL_S",
    "SWEEP_LIMIT",
    "TASK_NAME",
    "outbox_sweep_cron",
    "refresh_channels_cron",
    "send_outbox_batch",
    "send_outbox_job",
]
