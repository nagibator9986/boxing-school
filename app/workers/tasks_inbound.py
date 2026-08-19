"""Задача обработки входящего вебхука.

Тонкая обёртка над ``core.pipeline.process_inbound``: корреляция, метрики, лог,
классификация ошибок. Вся логика диалога живёт в пайплайне, здесь её нет.

Два правила, ради которых эта обёртка вообще существует.

**Ретраи — только на транзиентных ошибках.** Redis моргнул, БД переподключается,
модель ответила 429 — это повод повторить. Логическая ошибка в разборе payload
повторится ровно так же и через минуту, поэтому такая задача не ретраится: её
место в логе, а не в очереди.

**Деградация вместо молчания.** Если пайплайн всё-таки упал (контракт §15 это
запрещает, но код пишут люди), клиент получает честную фразу «сбой, передаю
администратору» из ``kb/i18n.yaml``, а администратор — карточку эскалации.
Молчащий бот хуже честного «сейчас передам менеджеру»: клиент, которому никто не
ответил, уходит в другую секцию и не возвращается.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Final
from uuid import UUID, uuid4

from arq.worker import Retry

from app.logging_conf import bind_correlation, clear_correlation, get_logger
from app.observability import metrics
from app.types import (
    BotError,
    EscalationReason,
    InboundMessage,
    Language,
    LLMQuotaError,
    LLMRateLimitError,
    LLMTimeoutError,
    OutboundKind,
    OutboundMessage,
    PipelineDecision,
    StorageError,
)

log = get_logger(__name__)

#: Имя задачи в метриках и логах.
TASK_NAME: Final[str] = "process_inbound"

#: Ключ i18n с честной фразой-заглушкой при сбое.
FALLBACK_TEXT_KEY: Final[str] = "error.generic"

#: Задержки повторов задачи, секунды. Дальше — отказ и лог.
RETRY_DELAYS_S: Final[tuple[int, ...]] = (5, 15, 45)

#: Исключения, при которых повтор осмыслен.
_TRANSIENT: Final[tuple[type[BaseException], ...]] = (
    StorageError,
    LLMTimeoutError,
    LLMRateLimitError,
    ConnectionError,
    TimeoutError,
    OSError,
)


async def process_inbound_job(ctx: dict[str, Any], payload: dict[str, Any]) -> None:
    """Обёртка над ``core.pipeline.process_inbound``: correlation_id, метрики, лог, clear_correlation.

    Исключений наружу не выпускает, кроме :class:`arq.worker.Retry` — это сигнал
    очереди повторить задачу позже.
    """
    correlation_id = _correlation_id(ctx)
    bind_correlation(correlation_id, task=TASK_NAME)
    started = time.perf_counter()
    deps = _deps(ctx)

    try:
        from app.core import pipeline  # локальный импорт: разрывает цикл core ↔ workers

        decisions: list[PipelineDecision] = await pipeline.process_inbound(deps, payload)
    except _TRANSIENT as exc:
        metrics.observe_job_failure(TASK_NAME, _code(exc))
        _raise_retry(ctx, exc)
        return
    except LLMQuotaError as exc:
        # Квота исчерпана: повтор не поможет, но клиенту всё равно нужно ответить.
        metrics.observe_job_failure(TASK_NAME, _code(exc))
        await _degrade(deps, payload, exc, reason=EscalationReason.BUDGET_GUARD)
        return
    except Exception as exc:  # noqa: BLE001 - воркер обязан пережить любую ошибку
        metrics.observe_job_failure(TASK_NAME, _code(exc))
        log.exception("inbound_job_failed", error=type(exc).__name__)
        await _degrade(deps, payload, exc, reason=EscalationReason.LLM_FAILURE)
        return
    finally:
        elapsed = time.perf_counter() - started
        metrics.observe_job(TASK_NAME, elapsed)
        clear_correlation()

    for decision in decisions:
        metrics.observe_decision(decision)
        metrics.observe_pipeline_latency(decision.action, elapsed)
        log.info(
            "inbound_processed",
            action=decision.action.value,
            reason=decision.reason,
            conversation_id=str(decision.conversation_id) if decision.conversation_id else None,
            escalation=decision.escalation_reason.value if decision.escalation_reason else None,
            outbound=len(decision.outbound),
            elapsed_ms=int(elapsed * 1000),
        )


# --------------------------------------------------------------------------- #
# Деградация
# --------------------------------------------------------------------------- #
async def _degrade(
    deps: Any,
    payload: dict[str, Any],
    exc: BaseException,
    *,
    reason: EscalationReason,
) -> None:
    """Пайплайн упал: сказать клиенту правду и позвать администратора.

    Всё внутри защищено: если и деградация не сработала, наружу это не выходит —
    задача уже провалена, а исключение из обработчика ошибки ничего не улучшит.
    """
    try:
        inbounds = _client_messages(payload)
    except Exception:  # noqa: BLE001 - payload может быть каким угодно
        inbounds = []

    if not inbounds:
        await _alert(deps, f"Пайплайн упал, разобрать вебхук не удалось: {type(exc).__name__}")
        return

    kb = _kb_or_none(deps)
    now = datetime.now(tz=timezone.utc)

    for inbound in inbounds:
        lang = await _language_of(deps, inbound)
        text = None
        if kb is not None:
            try:
                text = kb.text(FALLBACK_TEXT_KEY, lang)
            except Exception:  # noqa: BLE001 - ключа нет: молчим клиенту, зовём человека
                text = None

        if text:
            await _enqueue(
                deps,
                OutboundMessage(
                    channel_id=inbound.channel_id,
                    channel=inbound.channel,
                    chat_id=inbound.chat_id,
                    lang=lang,
                    kind=OutboundKind.ESCALATION_NOTICE,
                    text=text,
                ),
            )

        await _escalate(deps, inbound, lang=lang, reason=reason, now=now)

    await _alert(deps, f"Пайплайн упал на входящем: {type(exc).__name__}")


async def _escalate(
    deps: Any,
    inbound: InboundMessage,
    *,
    lang: Language,
    reason: EscalationReason,
    now: datetime,
) -> None:
    """Ставит администратору карточку «нужен живой ответ» по конкретному сообщению."""
    try:
        from app.notify.manager import build_escalation_card, build_manager_message

        card = build_escalation_card(
            reason=reason,
            question=inbound.text or "(сообщение без текста)",
            lang=lang,
            phone=inbound.phone_e164 or inbound.contact_phone,
            channel=inbound.channel,
            now=now,
        )
        message = build_manager_message(card)
        if message is not None:
            await _enqueue(deps, message)
            metrics.observe_manager_notification(card.kind)
        metrics.escalations_total.labels(reason=reason.value).inc()
    except Exception as exc:  # noqa: BLE001
        log.warning("degrade_escalation_failed", error=type(exc).__name__)


async def _alert(deps: Any, text: str) -> None:
    """Техническая тревога с подавлением повторов."""
    try:
        from app.notify.manager import notify_alert

        await notify_alert(deps, text, code="pipeline_failure")
    except Exception as exc:  # noqa: BLE001
        log.warning("degrade_alert_failed", error=type(exc).__name__)


async def _enqueue(deps: Any, message: OutboundMessage) -> UUID | None:
    """Кладёт исходящее в outbox и сразу ставит в очередь отправки."""
    from app.storage import repo_outbox

    try:
        async with deps.sessionmaker() as session:
            outbox_id = await repo_outbox.enqueue(session, message)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - БД недоступна: остаётся только лог
        log.error("degrade_enqueue_failed", error=type(exc).__name__)
        return None
    try:
        await deps.queue.enqueue_outbox(outbox_id)
    except Exception as exc:  # noqa: BLE001 - строку подберёт outbox_sweep_cron
        log.warning("degrade_enqueue_job_failed", error=type(exc).__name__)
    return outbox_id


def _client_messages(payload: dict[str, Any]) -> list[InboundMessage]:
    """Клиентские сообщения из сырого payload. Эхо и статусы отбрасываются."""
    from app.channels.normalize import is_client_inbound, to_inbound_batch
    from app.channels.wazzup_schemas import parse_webhook

    parsed = parse_webhook(payload)
    now = datetime.now(tz=timezone.utc)
    return [msg for msg in to_inbound_batch(parsed, received_at=now) if is_client_inbound(msg)]


async def _language_of(deps: Any, inbound: InboundMessage) -> Language:
    """Язык диалога из БД; неизвестно — русский (город русскоязычный по умолчанию)."""
    from app.storage import repo_conversation

    try:
        async with deps.sessionmaker() as session:
            conv = await repo_conversation.get_by_conv_key(session, inbound.conv_key)
    except Exception:  # noqa: BLE001
        return Language.RU
    if conv is None or not conv.lang:
        return Language.RU
    return Language.KK if conv.lang == Language.KK.value else Language.RU


def _kb_or_none(deps: Any) -> Any:
    """Снимок KB или ``None``, если он ещё не загружен."""
    try:
        return deps.kb()
    except Exception:  # noqa: BLE001 - KBNotLoadedError и всё, что мог отдать провайдер
        return None


# --------------------------------------------------------------------------- #
# Внутреннее
# --------------------------------------------------------------------------- #
def _deps(ctx: dict[str, Any]) -> Any:
    """``PipelineDeps`` из контекста воркера. Кладётся в ``queue.startup``."""
    deps = ctx.get("deps")
    if deps is None:
        raise RuntimeError("В контексте воркера нет PipelineDeps: не отработал startup")
    return deps


def _correlation_id(ctx: dict[str, Any]) -> str:
    """Корреляция задачи: id задачи arq либо свежий uuid для inline-режима."""
    raw = ctx.get("job_id") or ctx.get("correlation_id")
    return str(raw) if raw else uuid4().hex


def _code(exc: BaseException) -> str:
    """Код ошибки для метрики: свой ``BotError.code`` либо имя класса."""
    return exc.code if isinstance(exc, BotError) else type(exc).__name__


def _raise_retry(ctx: dict[str, Any], exc: BaseException) -> None:
    """Просит очередь повторить задачу; попытки исчерпаны — просто лог."""
    attempt = int(ctx.get("job_try") or 1)
    if attempt > len(RETRY_DELAYS_S):
        log.error("inbound_job_gave_up", attempt=attempt, error=type(exc).__name__)
        return
    delay = RETRY_DELAYS_S[attempt - 1]
    metrics.observe_job_retry(TASK_NAME)
    log.warning(
        "inbound_job_retry", attempt=attempt, delay_s=delay, error=type(exc).__name__
    )
    raise Retry(defer=delay)


__all__ = ["FALLBACK_TEXT_KEY", "RETRY_DELAYS_S", "TASK_NAME", "process_inbound_job"]
