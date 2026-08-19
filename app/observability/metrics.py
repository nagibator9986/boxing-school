"""Prometheus-метрики AINAZAROV TOP TEAM.

Модуль не ходит в сеть, не читает БД и импортируется при пустом окружении:
все коллекторы — обычные объекты в памяти, отдаются по ``GET /metrics``.

Метрики живут в **собственном** ``CollectorRegistry``. Причина простая: при
``INLINE_WORKER=true`` API и воркер работают в одном процессе, тесты создают
приложение по нескольку раз за сессию, и глобальный реестр ``prometheus_client``
рано или поздно даёт ``Duplicated timeseries``. Свой реестр этой проблемы не имеет.

Что считаем и зачем (вопросы владельца школы, на которые отвечают эти цифры):

* **сколько пришло и сколько ушло** — ``webhook_received_total``,
  ``inbound_processed_total``, ``outbound_sent_total``;
* **доля эскалаций** — ``escalations_total / inbound_processed_total``; цель 10–15 %:
  меньше — бот врёт вместо того, чтобы звать человека, больше — бот бесполезен;
* **конверсия в лид** — ``leads_created_total{status="trial_booked"} /
  conversations_started_total``;
* **латентность пайплайна** — ``pipeline_latency_seconds`` (весь путь от задачи
  воркера до постановки ответа в outbox) и ``llm_latency_seconds`` (только модель);
* **токены и деньги** — ``llm_tokens_total``, ``llm_cost_usd_total``,
  ``turn_cost_usd``, ``dialog_cost_usd``;
* **попадания в implicit-кэш Gemini** — ``llm_cache_hits_total`` и
  ``llm_cached_token_ratio``; кэш живёт стабильным префиксом промпта, падение
  доли = кто-то сломал префикс;
* **срабатывания постфильтра** — ``postcheck_fail_total``, главный индикатор
  галлюцинаций: ответ модели не прошёл сверку с KB и не ушёл клиенту;
* **echo_signal_mismatch_total** — расхождение признаков эха Wazzup
  (``sentFromApp`` против собственного outbox); рост означает, что детектор
  оператора работает на честном слове;
* **ошибки Wazzup по кодам** — ``wazzup_send_errors_total{code}``;
* **пробелы в базе знаний** — ``kb_gap_hits_total{topic}``: прямой бэклог
  владельцу школы, топ-10 вопросов без ответа.
"""

from __future__ import annotations

from typing import Any, Final, Iterable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from app.config import get_settings
from app.types import (
    ChannelKind,
    DecisionAction,
    GuardFlag,
    LLMUsage,
    OutboundKind,
    PipelineDecision,
    ToolInvocation,
)

#: Свой реестр: см. docstring модуля.
REGISTRY: Final[CollectorRegistry] = CollectorRegistry(auto_describe=True)

#: Секунды. Разложено под латентность мессенджер-бота, а не под микросервис:
#: всё, что дольше 10 с, для клиента одинаково долго.
_LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 30.0, 60.0,
)

#: Токены одного хода модели.
_TOKEN_BUCKETS: Final[tuple[float, ...]] = (
    256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536,
)

#: Доллары. Один ход дешёвый, диалог целиком — тоже, поэтому шкала мелкая.
_COST_BUCKETS: Final[tuple[float, ...]] = (
    0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.5,
)

#: Заглушка значения метки: пустых меток в Prometheus лучше не оставлять.
_UNKNOWN: Final[str] = "unknown"


# --------------------------------------------------------------------------- #
# Вход: вебхук и пайплайн
# --------------------------------------------------------------------------- #
webhook_received_total = Counter(
    "webhook_received_total",
    "Элементов вебхука Wazzup принято",
    labelnames=("kind",),  # message | status | channel_update | test | invalid
    registry=REGISTRY,
)

webhook_dedup_total = Counter(
    "webhook_dedup_total",
    "Повторных доставок вебхука отброшено дедупликацией",
    registry=REGISTRY,
)

inbound_processed_total = Counter(
    "inbound_processed_total",
    "Входящих сообщений обработано",
    labelnames=("chat_type", "action"),
    registry=REGISTRY,
)

conversations_started_total = Counter(
    "conversations_started_total",
    "Новых диалогов начато",
    labelnames=("lang",),
    registry=REGISTRY,
)

pipeline_latency_seconds = Histogram(
    "pipeline_latency_seconds",
    "Полный путь входящего: от задачи воркера до решения пайплайна",
    labelnames=("action",),
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)

guard_flags_total = Counter(
    "guard_flags_total",
    "Срабатываний защитных фильтров на входящем",
    labelnames=("flag",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Модель
# --------------------------------------------------------------------------- #
llm_latency_seconds = Histogram(
    "llm_latency_seconds",
    "Латентность одного вызова Gemini",
    labelnames=("model",),
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)

llm_tokens_total = Counter(
    "llm_tokens_total",
    "Токены Gemini",
    labelnames=("kind",),  # in | out | cached | thoughts
    registry=REGISTRY,
)

llm_cost_usd_total = Counter(
    "llm_cost_usd_total",
    "Оценочная стоимость вызовов Gemini, USD",
    labelnames=("model",),
    registry=REGISTRY,
)

llm_calls_total = Counter(
    "llm_calls_total",
    "Вызовов Gemini",
    labelnames=("model", "finish_reason"),
    registry=REGISTRY,
)

llm_errors_total = Counter(
    "llm_errors_total",
    "Ошибки Gemini по коду исключения",
    labelnames=("code",),
    registry=REGISTRY,
)

llm_cache_hits_total = Counter(
    "llm_cache_hits_total",
    "Вызовов, где implicit-кэш Gemini сработал (cached_tokens > 0)",
    labelnames=("model",),
    registry=REGISTRY,
)

llm_cached_token_ratio = Histogram(
    "llm_cached_token_ratio",
    "Доля кэшированных токенов от промпта: показатель стабильности префикса",
    buckets=(0.0, 0.1, 0.25, 0.5, 0.7, 0.85, 0.95, 1.0),
    registry=REGISTRY,
)

turn_tokens = Histogram(
    "turn_tokens",
    "Всего токенов на один ход бота (сумма по вызовам хода)",
    buckets=_TOKEN_BUCKETS,
    registry=REGISTRY,
)

turn_cost_usd = Histogram(
    "turn_cost_usd",
    "Стоимость одного хода бота, USD",
    buckets=_COST_BUCKETS,
    registry=REGISTRY,
)

dialog_tokens = Histogram(
    "dialog_tokens",
    "Токены на диалог целиком",
    buckets=_TOKEN_BUCKETS,
    registry=REGISTRY,
)

dialog_cost_usd = Histogram(
    "dialog_cost_usd",
    "Стоимость диалога целиком, USD",
    buckets=_COST_BUCKETS,
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Инструменты, база знаний, постфильтр
# --------------------------------------------------------------------------- #
tool_calls_total = Counter(
    "tool_calls_total",
    "Вызовов инструментов",
    labelnames=("name", "status"),
    registry=REGISTRY,
)

tool_latency_seconds = Histogram(
    "tool_latency_seconds",
    "Латентность одного инструмента",
    labelnames=("name",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 3.0),
    registry=REGISTRY,
)

postcheck_fail_total = Counter(
    "postcheck_fail_total",
    "Ответов модели заблокировано анти-галлюцинационным постфильтром",
    labelnames=("kind",),
    registry=REGISTRY,
)

kb_gap_hits_total = Counter(
    "kb_gap_hits_total",
    "Обращений к теме, по которой в базе знаний нет данных",
    labelnames=("topic",),
    registry=REGISTRY,
)

kb_load_failures_total = Counter(
    "kb_load_failures_total",
    "Неудачных загрузок базы знаний",
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Исходящие и Wazzup
# --------------------------------------------------------------------------- #
outbound_sent_total = Counter(
    "outbound_sent_total",
    "Исходящих сообщений доставлено в Wazzup",
    labelnames=("channel", "kind"),
    registry=REGISTRY,
)

outbound_failed_total = Counter(
    "outbound_failed_total",
    "Исходящих сообщений не отправлено (терминально)",
    labelnames=("channel", "disposition"),
    registry=REGISTRY,
)

wazzup_send_errors_total = Counter(
    "wazzup_send_errors_total",
    "Ошибки отправки Wazzup по нормализованному коду",
    labelnames=("code",),
    registry=REGISTRY,
)

wazzup_channel_active = Gauge(
    "wazzup_channel_active",
    "Состояние канала Wazzup: 1 — active, 0 — нет (обновляет cron refresh_channels)",
    labelnames=("channel_id", "transport"),
    registry=REGISTRY,
)

echo_signal_mismatch_total = Counter(
    "echo_signal_mismatch_total",
    "Расхождение признаков эха: sentFromApp против собственного outbox",
    registry=REGISTRY,
)

outbox_pending = Gauge(
    "outbox_pending",
    "Строк в outbox, ждущих отправки",
    registry=REGISTRY,
)

pause_active_conversations = Gauge(
    "pause_active_conversations",
    "Диалогов на паузе прямо сейчас",
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Лиды, эскалации, follow-up, уведомления
# --------------------------------------------------------------------------- #
leads_created_total = Counter(
    "leads_created_total",
    "Лидов создано или переведено в статус",
    labelnames=("status",),
    registry=REGISTRY,
)

escalations_total = Counter(
    "escalations_total",
    "Передач диалога живому администратору",
    labelnames=("reason",),
    registry=REGISTRY,
)

followups_sent_total = Counter(
    "followups_sent_total",
    "Отправленных напоминаний",
    labelnames=("kind",),
    registry=REGISTRY,
)

followups_skipped_total = Counter(
    "followups_skipped_total",
    "Напоминаний не отправлено: причина продуктовая или техническая",
    labelnames=("kind", "reason"),
    registry=REGISTRY,
)

manager_notifications_total = Counter(
    "manager_notifications_total",
    "Карточек и тревог, поставленных администратору",
    labelnames=("kind",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Воркер
# --------------------------------------------------------------------------- #
worker_job_seconds = Histogram(
    "worker_job_seconds",
    "Длительность задачи воркера",
    labelnames=("task",),
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)

worker_job_failures_total = Counter(
    "worker_job_failures_total",
    "Задач воркера завершилось ошибкой",
    labelnames=("task", "code"),
    registry=REGISTRY,
)

worker_job_retries_total = Counter(
    "worker_job_retries_total",
    "Повторных попыток задач воркера",
    labelnames=("task",),
    registry=REGISTRY,
)

worker_queue_depth = Gauge(
    "worker_queue_depth",
    "Глубина внутренней очереди InlineJobQueue (0 при отдельном arq-воркере)",
    registry=REGISTRY,
)


# --------------------------------------------------------------------------- #
# Наблюдатели
# --------------------------------------------------------------------------- #
def observe_llm(usage: LLMUsage) -> None:
    """Учитывает один вызов модели: латентность, токены, деньги, кэш."""
    model = _label(usage.model)
    llm_latency_seconds.labels(model=model).observe(max(0, usage.latency_ms) / 1000.0)
    llm_calls_total.labels(model=model, finish_reason=_label(usage.finish_reason)).inc()

    if usage.prompt_tokens:
        llm_tokens_total.labels(kind="in").inc(usage.prompt_tokens)
    if usage.candidates_tokens:
        llm_tokens_total.labels(kind="out").inc(usage.candidates_tokens)
    if usage.cached_tokens:
        llm_tokens_total.labels(kind="cached").inc(usage.cached_tokens)
        llm_cache_hits_total.labels(model=model).inc()
    if usage.thoughts_tokens:
        llm_tokens_total.labels(kind="thoughts").inc(usage.thoughts_tokens)
    if usage.prompt_tokens > 0:
        ratio = min(1.0, max(0.0, usage.cached_tokens / usage.prompt_tokens))
        llm_cached_token_ratio.observe(ratio)
    if usage.cost_usd:
        llm_cost_usd_total.labels(model=model).inc(max(0.0, usage.cost_usd))


def observe_llm_error(code: str) -> None:
    """Ошибка модели по коду исключения (``llm_timeout``, ``llm_quota``, …)."""
    llm_errors_total.labels(code=_label(code)).inc()


def observe_tool(invocation: ToolInvocation) -> None:
    """Учитывает один вызов инструмента и, если он упёрся в пробел KB, — тему пробела."""
    name = _label(invocation.name)
    tool_calls_total.labels(name=name, status=_label(invocation.result.status)).inc()
    tool_latency_seconds.labels(name=name).observe(max(0, invocation.latency_ms) / 1000.0)
    if invocation.result.gap_ref is not None:
        topic = invocation.args.get("topic")
        kb_gap_hits_total.labels(
            topic=_label(topic if isinstance(topic, str) and topic else invocation.result.gap_ref)
        ).inc()


def observe_decision(decision: PipelineDecision) -> None:
    """Учитывает итог обработки одного входящего.

    Одна точка на весь пайплайн: действие, флаги guard'ов, эскалация, постфильтр,
    инструменты, токены и стоимость хода. Пайплайн зовёт её ровно один раз на решение,
    иначе счётчики задвоятся.
    """
    action = _label(decision.action)
    chat_type = _chat_type(decision.conv_key)
    inbound_processed_total.labels(chat_type=chat_type, action=action).inc()

    for flag in decision.guard_flags:
        guard_flags_total.labels(flag=_label(flag)).inc()

    if decision.escalation_reason is not None or decision.action is DecisionAction.ESCALATE:
        escalations_total.labels(reason=_label(decision.escalation_reason or "unspecified")).inc()

    if decision.postcheck_fail is not None:
        postcheck_fail_total.labels(kind=_label(decision.postcheck_fail)).inc()

    for invocation in decision.invocations:
        observe_tool(invocation)

    tokens = 0
    cost = 0.0
    for usage in decision.usage:
        observe_llm(usage)
        tokens += usage.total_tokens or (
            usage.prompt_tokens + usage.candidates_tokens + usage.thoughts_tokens
        )
        cost += usage.cost_usd
    if decision.usage:
        turn_tokens.observe(tokens)
        turn_cost_usd.observe(cost)

    for card in decision.manager_cards:
        manager_notifications_total.labels(kind=_label(card.kind)).inc()


def observe_pipeline_latency(action: DecisionAction | str, seconds: float) -> None:
    """Латентность полного прохода пайплайна по одному входящему."""
    pipeline_latency_seconds.labels(action=_label(action)).observe(max(0.0, seconds))


def observe_dialog(*, tokens: int, cost_usd: float) -> None:
    """Итог по диалогу целиком. Зовётся при закрытии диалога или сводкой по расписанию."""
    dialog_tokens.observe(max(0, tokens))
    dialog_cost_usd.observe(max(0.0, cost_usd))


def observe_outbound_sent(channel: ChannelKind | str, kind: OutboundKind | str) -> None:
    """Исходящее реально ушло в Wazzup."""
    outbound_sent_total.labels(channel=_label(channel), kind=_label(kind)).inc()


def observe_outbound_failed(channel: ChannelKind | str, disposition: str) -> None:
    """Исходящее терминально не ушло: ``fatal``, ``needs_human``, ``exhausted``, ``window``."""
    outbound_failed_total.labels(channel=_label(channel), disposition=_label(disposition)).inc()


def observe_wazzup_error(code: str | None) -> None:
    """Ошибка Wazzup по нормализованному коду (регистр и разделители уже отброшены)."""
    wazzup_send_errors_total.labels(code=_label(code or "unknown")).inc()


def observe_guard_flags(flags: Iterable[GuardFlag]) -> None:
    """Флаги guard'ов вне контекста решения (например, на дропнутом сообщении)."""
    for flag in flags:
        guard_flags_total.labels(flag=_label(flag)).inc()


def observe_lead(status: Any) -> None:
    """Лид создан или сменил статус."""
    leads_created_total.labels(status=_label(status)).inc()


def observe_conversation_started(lang: Any) -> None:
    """Новый диалог. Доля казахских диалогов управляет приоритетом KK-контента."""
    conversations_started_total.labels(lang=_label(lang)).inc()


def observe_followup_sent(kind: Any) -> None:
    """Напоминание ушло клиенту."""
    followups_sent_total.labels(kind=_label(kind)).inc()


def observe_followup_skipped(kind: Any, reason: str) -> None:
    """Напоминание не ушло: ``quiet_hours``, ``stop_word``, ``window``, ``replied``, …"""
    followups_skipped_total.labels(kind=_label(kind), reason=_label(reason)).inc()


def observe_manager_notification(kind: Any) -> None:
    """Карточка администратору поставлена в очередь."""
    manager_notifications_total.labels(kind=_label(kind)).inc()


def observe_echo_mismatch() -> None:
    """Признаки эха разошлись — детектор оператора работает на одном сигнале."""
    echo_signal_mismatch_total.inc()


def observe_webhook(kind: str, count: int = 1) -> None:
    """Принят элемент вебхука: ``message``/``status``/``channel_update``/``test``/``invalid``."""
    if count > 0:
        webhook_received_total.labels(kind=_label(kind)).inc(count)


def observe_dedup_hit(count: int = 1) -> None:
    """Повторная доставка отброшена."""
    if count > 0:
        webhook_dedup_total.inc(count)


def observe_kb_load_failure() -> None:
    """KB не загрузилась или не прошла валидацию."""
    kb_load_failures_total.inc()


def observe_channel_state(channel_id: str, transport: str, is_active: bool) -> None:
    """Состояние канала Wazzup по данным ``GET /v3/channels``."""
    wazzup_channel_active.labels(
        channel_id=_label(channel_id), transport=_label(transport)
    ).set(1 if is_active else 0)


def set_outbox_pending(value: int) -> None:
    """Глубина исходящей очереди. Обновляется cron-задачей, а не на каждом сообщении."""
    outbox_pending.set(max(0, value))


def set_pause_active(value: int) -> None:
    """Сколько диалогов сейчас на паузе."""
    pause_active_conversations.set(max(0, value))


def set_worker_queue_depth(value: int) -> None:
    """Глубина внутренней очереди ``InlineJobQueue``."""
    worker_queue_depth.set(max(0, value))


def observe_job(task: str, seconds: float) -> None:
    """Длительность задачи воркера."""
    worker_job_seconds.labels(task=_label(task)).observe(max(0.0, seconds))


def observe_job_failure(task: str, code: str) -> None:
    """Задача воркера упала."""
    worker_job_failures_total.labels(task=_label(task), code=_label(code)).inc()


def observe_job_retry(task: str) -> None:
    """Задача воркера пошла на повтор."""
    worker_job_retries_total.labels(task=_label(task)).inc()


def render_latest() -> tuple[bytes, str]:
    """``(payload, content_type)`` для ``GET /metrics``.

    При ``metrics_enabled=false`` отдаётся пустое тело: ручка остаётся, данных нет.
    Сбой чтения настроек метрики не отменяет: снимать показания надо именно тогда,
    когда с системой что-то не так.
    """
    try:
        enabled = bool(get_settings().metrics_enabled)
    except Exception:  # noqa: BLE001 - настройки сломаны, а метрики нужны как никогда
        enabled = True
    if not enabled:
        return b"", CONTENT_TYPE_LATEST
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


# --------------------------------------------------------------------------- #
# Внутреннее
# --------------------------------------------------------------------------- #
def _label(value: Any) -> str:
    """Значение метки: enum → ``.value``, ``None`` → ``unknown``, всё прочее → ``str``."""
    if value is None:
        return _UNKNOWN
    raw = getattr(value, "value", value)
    text = str(raw).strip()
    return text or _UNKNOWN


def _chat_type(conv_key: str | None) -> str:
    """Канал из ``conv_key`` вида ``{channel_id}:{chat_type}:{chat_id}``."""
    if not conv_key:
        return _UNKNOWN
    parts = conv_key.split(":")
    return parts[1] if len(parts) >= 3 and parts[1] else _UNKNOWN


__all__ = [
    "CONTENT_TYPE_LATEST",
    "REGISTRY",
    "conversations_started_total",
    "dialog_cost_usd",
    "dialog_tokens",
    "echo_signal_mismatch_total",
    "escalations_total",
    "followups_sent_total",
    "followups_skipped_total",
    "guard_flags_total",
    "inbound_processed_total",
    "kb_gap_hits_total",
    "kb_load_failures_total",
    "leads_created_total",
    "llm_cache_hits_total",
    "llm_cached_token_ratio",
    "llm_calls_total",
    "llm_cost_usd_total",
    "llm_errors_total",
    "llm_latency_seconds",
    "llm_tokens_total",
    "manager_notifications_total",
    "observe_channel_state",
    "observe_conversation_started",
    "observe_decision",
    "observe_dedup_hit",
    "observe_dialog",
    "observe_echo_mismatch",
    "observe_followup_sent",
    "observe_followup_skipped",
    "observe_guard_flags",
    "observe_job",
    "observe_job_failure",
    "observe_job_retry",
    "observe_kb_load_failure",
    "observe_lead",
    "observe_llm",
    "observe_llm_error",
    "observe_manager_notification",
    "observe_outbound_failed",
    "observe_outbound_sent",
    "observe_pipeline_latency",
    "observe_tool",
    "observe_wazzup_error",
    "observe_webhook",
    "outbound_failed_total",
    "outbound_sent_total",
    "outbox_pending",
    "pause_active_conversations",
    "pipeline_latency_seconds",
    "postcheck_fail_total",
    "render_latest",
    "set_outbox_pending",
    "set_pause_active",
    "set_worker_queue_depth",
    "tool_calls_total",
    "tool_latency_seconds",
    "turn_cost_usd",
    "turn_tokens",
    "wazzup_channel_active",
    "wazzup_send_errors_total",
    "webhook_dedup_total",
    "webhook_received_total",
    "worker_job_failures_total",
    "worker_job_retries_total",
    "worker_job_seconds",
    "worker_queue_depth",
]
