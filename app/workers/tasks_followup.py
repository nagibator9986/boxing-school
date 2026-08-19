"""Напоминания клиенту (follow-up).

Правила здесь **продуктовые**, а не юридические: согласий проект не спрашивает
(SCOPE-OVERRIDE §1), но это не значит, что боту можно писать когда и сколько угодно.
Родитель, которому бот напомнил о себе третий раз в полночь, не приведёт ребёнка
в секцию — он заблокирует номер.

Четыре ограничения, снять которые нельзя:

1. **Жёсткий лимит попыток** — ``Settings.followup_max_attempts`` на диалог,
   плюс ``max_times`` на каждое правило из ``kb/policies.yaml``.
2. **Стоп-слова блокируют навсегда.** «Не пишите», «отстаньте», «спасибо, не надо» —
   ``conversation.followup_blocked = true``, и это состояние необратимо. Проверяются
   и флаги пайплайна, и последние реплики клиента: если стоп-слово проскочило мимо
   guard'ов, напоминание всё равно не уйдёт.
3. **Никаких отправок ночью.** Окно тишины — ``followup_quiet_hours_start`` …
   ``followup_quiet_hours_end`` по ``Asia/Almaty``. Оно действует на **все** виды
   напоминаний, включая ``only_work_hours: false``: правило из KB может разрешить
   отправку вне рабочих часов, но не ночью. Задача не отменяется, а переносится
   на утро.
4. **Только внутри окна мессенджера.** Instagram даёт 7 суток с последнего входящего
   и запрещает писать первым. Окно закрыто — напоминание отменяется, а не копится:
   Wazzup всё равно вернёт ошибку.

Ещё одно правило, из здравого смысла: если клиент ответил после того, как
напоминание было запланировано, мягкие «остались вопросы?» отменяются — разговор
и так идёт.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Sequence
from uuid import UUID
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.outbound import check_send_allowed
from app.config import get_settings
from app.kb.models import FollowupRule
from app.logging_conf import bind_correlation, clear_correlation, get_logger
from app.observability import metrics
from app.storage.models import Conversation, FollowupTask
from app.types import (
    ChannelKind,
    ConversationState,
    DecisionAction,
    FollowupKind,
    GuardFlag,
    Language,
    LeadStatus,
    OutboundKind,
    OutboundMessage,
    PipelineDecision,
)

log = get_logger(__name__)

TASK_NAME: Final[str] = "send_followup"

#: Состояния строки ``followup_task``.
STATE_PENDING: Final[str] = "pending"
STATE_SENT: Final[str] = "sent"
STATE_CANCELLED: Final[str] = "cancelled"
STATE_FAILED: Final[str] = "failed"

#: Состояния диалога, при которых напоминания бессмысленны: там уже человек.
_DEAD_STATES: Final[frozenset[str]] = frozenset(
    {
        ConversationState.PAUSED_OPERATOR.value,
        ConversationState.ESCALATED.value,
        ConversationState.CLOSED.value,
    }
)

#: Мягкие напоминания: отменяются, если клиент ответил сам.
_SOFT_KINDS: Final[frozenset[FollowupKind]] = frozenset(
    {FollowupKind.FU_SOFT, FollowupKind.FU_VALUE}
)

#: Напоминания, привязанные к времени пробного занятия, а не к «сейчас».
_TRIAL_ANCHORED: Final[frozenset[FollowupKind]] = frozenset(
    {FollowupKind.TRIAL_REMINDER_20H, FollowupKind.TRIAL_REMINDER_2H, FollowupKind.NO_SHOW}
)

#: Сколько задач за один проход подбирает сметка.
SWEEP_LIMIT: Final[int] = 100

#: Сколько последних реплик клиента проверяется на стоп-слова.
_STOP_WORD_LOOKBACK: Final[int] = 12


# --------------------------------------------------------------------------- #
# Задача отправки
# --------------------------------------------------------------------------- #
async def send_followup_job(ctx: dict[str, Any], task_id: str) -> None:
    """Продуктовые правила (не юридические, согласия не требуется):
    лимит ``followup_max_attempts``; стоп-слова блокируют навсегда; запрет ночью
    по ``Asia/Almaty``; отправка только внутри окна мессенджера.
    """
    from app.storage import repo_conversation

    deps = _deps(ctx)
    settings = deps.settings
    bind_correlation(str(task_id), task=TASK_NAME)
    started = time.perf_counter()

    try:
        try:
            tid = UUID(str(task_id))
        except (TypeError, ValueError):
            log.error("followup_bad_id", task_id=str(task_id))
            return

        if not settings.followup_enabled:
            async with deps.sessionmaker() as session:
                await _finish(session, tid, STATE_CANCELLED)
                await session.commit()
            return

        now = _now()
        async with deps.sessionmaker() as session:
            task = await session.get(FollowupTask, tid)
            if task is None or task.state != STATE_PENDING:
                log.debug("followup_not_pending", task_id=str(tid))
                return

            kind = _kind_of(task.kind)
            conv = await repo_conversation.get_by_id(session, task.conversation_id)
            if conv is None:
                await _finish(session, tid, STATE_CANCELLED)
                await session.commit()
                return

            skip = await _skip_reason(session, conv, task, kind=kind, settings=settings, now=now)
            if skip == "quiet_hours":
                run_at = next_allowed_time(
                    now,
                    start_hour=settings.followup_quiet_hours_start,
                    end_hour=settings.followup_quiet_hours_end,
                    tz=settings.timezone,
                )
                await session.execute(
                    sa.update(FollowupTask).where(FollowupTask.id == tid).values(run_at=run_at)
                )
                await session.commit()
                metrics.observe_followup_skipped(kind, "quiet_hours")
                log.info("followup_deferred_night", task_id=str(tid), run_at=run_at.isoformat())
                return
            if skip is not None:
                await _finish(session, tid, STATE_CANCELLED)
                await session.commit()
                metrics.observe_followup_skipped(kind, skip)
                log.info("followup_cancelled", task_id=str(tid), kind=kind.value, reason=skip)
                return

            message = await _build_message(deps, session, conv, kind=kind)
            if message is None:
                await _finish(session, tid, STATE_CANCELLED)
                await session.commit()
                metrics.observe_followup_skipped(kind, "no_template")
                return

            outbox_id = await _enqueue(session, message)
            # Терминальный переход — условный: `WHERE state = 'pending'`. Один и тот
            # же task_id приходит двумя путями (отложенная задача из schedule_followups
            # и followup_sweep_cron), и оба могли прочитать состояние pending до того,
            # как первый закоммитил sent. Проигравший здесь не обновит ни одной строки
            # и откатит транзакцию целиком — вместе со своей строкой outbox, поэтому
            # второе одинаковое напоминание клиенту не уходит.
            claimed_now = await _finish(
                session, tid, STATE_SENT, attempt=int(task.attempt) + 1, only_pending=True
            )
            if not claimed_now:
                await session.rollback()
                log.info("followup_lost_race", task_id=str(tid), kind=kind.value)
                return
            await repo_conversation.set_followup(
                session, conv.id, stage=int(conv.followup_stage) + 1
            )
            await session.commit()

        try:
            await deps.queue.enqueue_outbox(outbox_id)
        except Exception as exc:  # noqa: BLE001 - строку подберёт outbox_sweep_cron
            log.warning("followup_enqueue_failed", error=type(exc).__name__)

        metrics.observe_followup_sent(kind)
        log.info("followup_sent", task_id=str(tid), kind=kind.value, conv_id=str(conv.id))
    finally:
        metrics.observe_job(TASK_NAME, time.perf_counter() - started)
        clear_correlation()


async def followup_sweep_cron(ctx: dict[str, Any]) -> None:
    """Раз в 10 минут выбирает ``followup_task`` со сроком и ставит их в очередь."""
    deps = _deps(ctx)
    if not deps.settings.followup_enabled:
        return
    now = _now()
    async with deps.sessionmaker() as session:
        rows = (
            await session.execute(
                sa.select(FollowupTask.id)
                .where(FollowupTask.state == STATE_PENDING, FollowupTask.run_at <= now)
                .order_by(FollowupTask.run_at)
                .limit(SWEEP_LIMIT)
            )
        ).scalars().all()
        await session.commit()

    for task_id in rows:
        try:
            await deps.queue.enqueue_followup(task_id, run_at=now)
        except Exception as exc:  # noqa: BLE001
            log.warning("followup_sweep_enqueue_failed", task_id=str(task_id), error=type(exc).__name__)
    if rows:
        log.info("followup_sweep_done", due=len(rows))


# --------------------------------------------------------------------------- #
# Планирование и отмена
# --------------------------------------------------------------------------- #
async def schedule_followups(
    session: AsyncSession,
    conv: Conversation,
    *,
    decision: PipelineDecision,
    policy: Sequence[FollowupRule],
) -> list[UUID]:
    """Планирует напоминания по итогу хода. Возвращает id созданных задач.

    Ничего не отправляет и не коммитит: строки лягут в ту же транзакцию, что и
    остальной ход пайплайна, а разошлёт их сметка по расписанию.
    """
    from app.storage import repo_lead

    settings = get_settings()
    if not settings.followup_enabled or conv.followup_blocked:
        return []
    if decision.action in (DecisionAction.DROP, DecisionAction.DEFER):
        return []
    if GuardFlag.STOP_WORD in decision.guard_flags:
        # Клиент попросил не писать: блокируем диалог навсегда и снимаем всё запланированное.
        await block_followups(session, conv.id, reason="stop_word")
        return []
    if int(conv.followup_stage) >= int(settings.followup_max_attempts):
        return []

    lead = await repo_lead.get_by_conversation(session, conv.id)
    trial_slot = getattr(lead, "trial_slot", None)
    lead_status = getattr(lead, "status", None)
    booked = lead_status == LeadStatus.TRIAL_BOOKED.value
    now = _now()

    created: list[UUID] = []
    for rule in policy:
        if rule.max_times <= 0:
            continue
        if not _rule_applies(rule.event, booked=booked, trial_slot=trial_slot):
            continue
        run_at = _run_at(rule, now=now, trial_slot=trial_slot)
        if run_at is None or run_at <= now - timedelta(hours=1):
            continue
        if await _times_used(session, conv.id, rule.event) >= rule.max_times:
            continue

        # Одноимённая незапущенная задача заменяется: срок мог сдвинуться.
        await session.execute(
            sa.update(FollowupTask)
            .where(
                FollowupTask.conversation_id == conv.id,
                FollowupTask.kind == rule.event.value,
                FollowupTask.state == STATE_PENDING,
            )
            .values(state=STATE_CANCELLED)
        )
        task = FollowupTask(
            conversation_id=conv.id,
            kind=rule.event.value,
            run_at=run_at,
            state=STATE_PENDING,
            attempt=0,
        )
        session.add(task)
        await session.flush()
        created.append(task.id)

    if created:
        log.info("followups_scheduled", conv_id=str(conv.id), count=len(created))
    return created


async def cancel_followups(session: AsyncSession, conv_id: UUID, *, reason: str) -> int:
    """Снимает все незапущенные напоминания диалога. Возвращает число отменённых."""
    result = await session.execute(
        sa.update(FollowupTask)
        .where(FollowupTask.conversation_id == conv_id, FollowupTask.state == STATE_PENDING)
        .values(state=STATE_CANCELLED)
    )
    count = int(result.rowcount or 0)
    if count:
        log.info("followups_cancelled", conv_id=str(conv_id), count=count, reason=reason)
    return count


async def block_followups(session: AsyncSession, conv_id: UUID, *, reason: str) -> None:
    """Выключает напоминания диалога навсегда: стоп-слово клиента необратимо."""
    from app.storage import repo_conversation

    await repo_conversation.set_followup(session, conv_id, blocked=True)
    await cancel_followups(session, conv_id, reason=reason)
    log.info("followups_blocked", conv_id=str(conv_id), reason=reason)


# --------------------------------------------------------------------------- #
# Правила времени
# --------------------------------------------------------------------------- #
def is_quiet_hours(now: datetime, *, start_hour: int, end_hour: int, tz: str) -> bool:
    """Ночное окно тишины в зоне школы. Окно, переходящее через полночь, поддерживается.

    ``start_hour=21, end_hour=9`` — тишина с 21:00 до 09:00 следующего дня.
    Равные границы означают «тишины нет».
    """
    if start_hour == end_hour:
        return False
    hour = _to_zone(now, tz).hour
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def next_allowed_time(
    now: datetime, *, start_hour: int, end_hour: int, tz: str
) -> datetime:
    """Ближайший момент, когда писать уже можно. Вне тишины возвращает ``now``."""
    if not is_quiet_hours(now, start_hour=start_hour, end_hour=end_hour, tz=tz):
        return now
    local = _to_zone(now, tz)
    target = local.replace(hour=end_hour % 24, minute=0, second=0, microsecond=0)
    if target <= local:
        target = target + timedelta(days=1)
    return target.astimezone(timezone.utc)


def within_service_window(conv: Conversation, now: datetime) -> bool:
    """Можно ли писать в этот диалог прямо сейчас.

    Instagram: 7 суток с последнего входящего, писать первым нельзя.
    WhatsApp личный: окна нет. Неизвестный канал считается закрытым — молчание
    безопаснее, чем гарантированная ошибка отправки.
    """
    channel = _channel_of(conv)
    if channel is None:
        return False
    if conv.service_window_until is not None and _aware(conv.service_window_until) < now:
        return False
    return (
        check_send_allowed(
            channel=channel,
            last_inbound_at=_aware(conv.last_inbound_at),
            now=now,
            kind=OutboundKind.FOLLOWUP,
        )
        is None
    )


def has_stop_word(text: str | None, stop_words: Sequence[str]) -> str | None:
    """Найденное стоп-слово или ``None``. Сравнение регистронезависимое, по подстроке."""
    if not text:
        return None
    lowered = text.casefold()
    for word in stop_words:
        needle = (word or "").strip().casefold()
        if needle and needle in lowered:
            return needle
    return None


# --------------------------------------------------------------------------- #
# Внутреннее
# --------------------------------------------------------------------------- #
async def _skip_reason(
    session: AsyncSession,
    conv: Conversation,
    task: FollowupTask,
    *,
    kind: FollowupKind,
    settings: Any,
    now: datetime,
) -> str | None:
    """Почему это напоминание не уйдёт. ``None`` — уйдёт."""
    if conv.followup_blocked:
        return "blocked"
    if conv.state in _DEAD_STATES:
        return "operator"
    if int(task.attempt) >= int(settings.followup_max_attempts):
        return "attempts"
    if int(conv.followup_stage) >= int(settings.followup_max_attempts):
        return "attempts"

    stop_word = await _stop_word_in_dialog(session, conv)
    if stop_word is not None:
        await block_followups(session, conv.id, reason=f"stop_word:{stop_word}")
        return "stop_word"

    if kind in _SOFT_KINDS:
        last_in = _aware(conv.last_inbound_at)
        created = _aware(task.created_at)
        if last_in is not None and created is not None and last_in > created:
            return "replied"

    if is_quiet_hours(
        now,
        start_hour=settings.followup_quiet_hours_start,
        end_hour=settings.followup_quiet_hours_end,
        tz=settings.timezone,
    ):
        return "quiet_hours"

    if not within_service_window(conv, now):
        return "window"
    return None


async def _stop_word_in_dialog(session: AsyncSession, conv: Conversation) -> str | None:
    """Ищет стоп-слово в последних репликах клиента.

    Второй рубеж после guard'ов пайплайна: «не пишите» не должно теряться из-за
    того, что сообщение пришло вне хода бота (например, на паузе).
    """
    from app.kb.loader import get_snapshot
    from app.storage import repo_message
    from app.types import Author

    try:
        stop_words = list(get_snapshot().policies.followup_stop_words)
    except Exception:  # noqa: BLE001 - KB не загружена: проверять не по чему
        return None
    if not stop_words:
        return None
    try:
        transcript = await repo_message.load_transcript(
            session, conv.id, limit=_STOP_WORD_LOOKBACK
        )
    except Exception:  # noqa: BLE001
        return None
    for author, text in transcript:
        if author is Author.CLIENT:
            found = has_stop_word(text, stop_words)
            if found is not None:
                return found
    return None


async def _build_message(
    deps: Any, session: AsyncSession, conv: Conversation, *, kind: FollowupKind
) -> OutboundMessage | None:
    """Текст напоминания из KB. ``None`` — шаблона нет или не хватает данных для подстановки."""
    from app.storage import repo_lead

    kb = _kb_or_none(deps)
    if kb is None:
        return None
    rule = next((r for r in kb.policies.followup_policy if r.event is kind), None)
    if rule is None:
        return None

    channel = _channel_of(conv)
    if channel is None:
        return None
    lang = Language.KK if conv.lang == Language.KK.value else Language.RU

    lead = await repo_lead.get_by_conversation(session, conv.id)
    params: dict[str, Any] = {}
    child = getattr(lead, "child_name", None)
    if child:
        params["child"] = child
    gym_id = getattr(lead, "gym_id", None)
    if gym_id:
        gym = kb.gym(gym_id)
        title = gym.title.get(lang) if gym is not None else None
        address = gym.address.get(lang) if gym is not None else None
        if title:
            params["gym"] = f"{title}, {address}" if address else title

    try:
        text = kb.text(rule.template_id, lang, **params)
    except Exception as exc:  # noqa: BLE001 - нет ключа или не хватает плейсхолдера
        log.warning(
            "followup_template_failed",
            kind=kind.value,
            template=rule.template_id,
            error=type(exc).__name__,
        )
        return None
    if not text or not text.strip():
        return None

    return OutboundMessage(
        conversation_id=conv.id,
        channel_id=conv.channel_id,
        channel=channel,
        chat_id=conv.chat_id,
        lang=lang,
        kind=OutboundKind.FOLLOWUP,
        text=text.strip(),
    )


async def _enqueue(session: AsyncSession, message: OutboundMessage) -> UUID:
    """Кладёт напоминание в outbox текущей транзакцией."""
    from app.storage import repo_outbox

    return await repo_outbox.enqueue(session, message)


async def _finish(
    session: AsyncSession,
    task_id: UUID,
    state: str,
    *,
    attempt: int | None = None,
    only_pending: bool = False,
) -> bool:
    """Переводит задачу в терминальное состояние.

    ``only_pending=True`` добавляет условие ``state = 'pending'``: это захват
    задачи, а не просто запись. Возвращает ``False``, если строку успел забрать
    другой прогон (обновлено 0 строк) — вызывающий обязан откатить транзакцию.
    """
    values: dict[str, Any] = {"state": state}
    if attempt is not None:
        values["attempt"] = attempt
    stmt = sa.update(FollowupTask).where(FollowupTask.id == task_id)
    if only_pending:
        stmt = stmt.where(FollowupTask.state == STATE_PENDING)
    result = await session.execute(stmt.values(**values))
    return int(result.rowcount or 0) > 0


async def _times_used(session: AsyncSession, conv_id: UUID, kind: FollowupKind) -> int:
    """Сколько раз это напоминание уже уходило в диалоге."""
    stmt = (
        sa.select(sa.func.count())
        .select_from(FollowupTask)
        .where(
            FollowupTask.conversation_id == conv_id,
            FollowupTask.kind == kind.value,
            FollowupTask.state == STATE_SENT,
        )
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


def _rule_applies(
    kind: FollowupKind, *, booked: bool, trial_slot: datetime | None
) -> bool:
    """Уместно ли это напоминание для текущего состояния лида."""
    if kind in _TRIAL_ANCHORED:
        return trial_slot is not None
    # Мягкие напоминания нужны тем, кто ещё не записался.
    return not booked


def _run_at(
    rule: FollowupRule, *, now: datetime, trial_slot: datetime | None
) -> datetime | None:
    """Когда отправлять. Отрицательный ``delay_hours`` отсчитывается от пробного занятия."""
    if rule.delay_hours < 0:
        if trial_slot is None:
            return None
        return _aware(trial_slot) + timedelta(hours=rule.delay_hours)  # type: ignore[operator]
    if rule.event in _TRIAL_ANCHORED:
        if trial_slot is None:
            return None
        return _aware(trial_slot) + timedelta(hours=rule.delay_hours)  # type: ignore[operator]
    return now + timedelta(hours=rule.delay_hours)


def _kind_of(raw: str) -> FollowupKind:
    """Строка из БД → :class:`FollowupKind`; неизвестное считаем мягким напоминанием."""
    try:
        return FollowupKind(raw)
    except ValueError:
        return FollowupKind.FU_SOFT


def _channel_of(conv: Conversation) -> ChannelKind | None:
    """Канал диалога из ``conversation.chat_type``."""
    try:
        return ChannelKind(conv.chat_type)
    except ValueError:
        return None


def _kb_or_none(deps: Any) -> Any:
    try:
        return deps.kb()
    except Exception:  # noqa: BLE001
        return None


def _to_zone(value: datetime, tz: str) -> datetime:
    """Момент в указанной зоне; кривая зона откатывается на ``Asia/Almaty``."""
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("Asia/Almaty")
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(zone)


def _aware(value: datetime | None) -> datetime | None:
    """Наивное время из SQLite считаем UTC — иначе сравнения падают."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _deps(ctx: dict[str, Any]) -> Any:
    deps = ctx.get("deps")
    if deps is None:
        raise RuntimeError("В контексте воркера нет PipelineDeps: не отработал startup")
    return deps


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "STATE_CANCELLED",
    "STATE_FAILED",
    "STATE_PENDING",
    "STATE_SENT",
    "SWEEP_LIMIT",
    "TASK_NAME",
    "block_followups",
    "cancel_followups",
    "followup_sweep_cron",
    "has_stop_word",
    "is_quiet_hours",
    "next_allowed_time",
    "schedule_followups",
    "send_followup_job",
    "within_service_window",
]
