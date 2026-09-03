"""Пауза бота: пока в диалоге работает живой человек, автоответчик молчит.

Главная причина существования модуля — эффект «бот перебивает оператора».
Администратор набирает ответ, клиент в это время дописывает уточнение, бот
получает вебхук и отвечает первым. Дальше в чате два ответа, один из которых
неверный, и клиент не понимает, с кем разговаривает.

Правила:

* вход оператора определяется по эху исходящего, которого нет в нашем outbox
  (см. :func:`app.channels.normalize.is_operator_echo`), и ставит паузу на
  ``pause_operator_minutes``;
* **каждое** следующее сообщение оператора паузу продлевает, а не начинает
  заново — окно всегда «сейчас плюс N минут», но никогда не укорачивается;
* пауза снимается по таймауту, служебной строкой оператора (``#бот``) или
  из админки — и **НИКОГДА** сообщением клиента. Сообщение клиента при паузе
  сохраняется в БД и в историю модели, но ответа не порождает;
* после возврата бот молчит до следующего входящего: догонять диалог
  собственной репликой он не должен.

Состояние живёт в двух местах: строка ``escalation_state`` (истина, переживает
рестарт) и ключ ``key_pause`` в Redis (быстрый путь, TTL до конца окна).
Отдельного репозитория для ``escalation_state`` в контракте нет, поэтому запросы
к этой таблице собраны здесь.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Final, Literal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.normalize import ECHO_SIGNAL_MISMATCH_EVENT, echo_signals, is_operator_echo
from app.config import Settings
from app.logging_conf import get_logger
from app.storage import repo_conversation
from app.storage.models import EscalationState
from app.storage.state import StateStore, key_pause
from app.types import (
    ConversationState,
    InboundMessage,
    PauseReason,
    ResumePolicy,
)

__all__ = [
    "detect_operator",
    "is_paused",
    "is_resume_command",
    "pause_minutes_for",
    "resume",
    "operator_last_seen",
    "set_pause",
]

_log = get_logger(__name__)

#: Причины, после которых бот сам не возвращается: снимает только человек.
_MANUAL_ONLY: Final[frozenset[PauseReason]] = frozenset({PauseReason.MANUAL})

#: Паузы, которые бот ставит себе сам. Клиент за них не отвечает: если он пишет
#: снова, а человек так и не подключился, молчать не за кем. Сбой самой модели
#: (``LLM_FAILURE``) сюда не входит — там пауза защищает не диалог, а сервис.
_BOTS_OWN_PAUSES: Final[frozenset[str]] = frozenset(
    {PauseReason.ESCALATION.value, PauseReason.POSTCHECK_FAIL.value}
)

#: Запасная длительность, если в настройках значения нет.
_DEFAULT_MINUTES: Final[int] = 30


# --------------------------------------------------------------------------- #
# Чтение
# --------------------------------------------------------------------------- #
async def is_paused(
    state: StateStore, session: AsyncSession, conv_id: UUID, conv_key: str, now: datetime
) -> bool:
    """На паузе ли диалог. Быстрый путь — Redis, истина — таблица ``escalation_state``.

    Истёкшее окно снимается здесь же: строка помечается ``paused=False``, диалог
    возвращается в ``active``. Исключение — ``resume_policy='manual_only'``:
    такую паузу время не снимает.
    """
    try:
        if await state.get(key_pause(conv_key)):
            return True
    except Exception as exc:  # Redis недоступен — спрашиваем базу
        _log.warning("pause_state_unavailable", error=str(exc))

    row = await _row(session, conv_id)
    if row is None or not row.paused:
        return False

    deadline = _aware(row.paused_until)
    manual_only = row.resume_policy == ResumePolicy.MANUAL_ONLY.value
    if deadline is not None and deadline <= now and not manual_only:
        await _clear(state, session, row, conv_key, by="timeout", now=now)
        return False

    # База знает про паузу, а Redis её потерял — восстанавливаем быстрый путь.
    await _mark(state, conv_key, deadline, now)
    return True


# --------------------------------------------------------------------------- #
# Запись
# --------------------------------------------------------------------------- #
async def set_pause(
    state: StateStore,
    session: AsyncSession,
    conv_id: UUID,
    conv_key: str,
    *,
    minutes: int,
    reason: PauseReason,
    now: datetime,
    author_id: str | None = None,
    author_name: str | None = None,
) -> None:
    """Ставит или ПРОДЛЕВАЕТ окно. Каждое новое сообщение оператора продлевает паузу.

    Новое окно — ``now + minutes``; если действующее окно длиннее, оно
    сохраняется: продление никогда не укорачивает паузу.
    """
    # Ноль минут — «молчать, пока бота не вернут вручную». Так владелец задаёт
    # паузу после ответа человека: срок здесь означал, что бот сам возвращается
    # в разговор, который человек ведёт до сих пор, — а он просил обратного:
    # «после того как кто-то пишет с самого аккаунта, бот должен резко затихать».
    forever = int(minutes) <= 0
    deadline = None if forever else now + timedelta(minutes=max(1, int(minutes)))

    row = await _row(session, conv_id)
    if row is None:
        row = EscalationState(conversation_id=conv_id)
        session.add(row)

    current = _aware(row.paused_until)
    if not forever and row.paused and current is not None and current > deadline:
        deadline = current
    if row.paused and row.resume_policy == ResumePolicy.MANUAL_ONLY.value:
        # Паузу без срока продление сроком не укорачивает.
        deadline = None
        forever = True

    row.paused = True
    row.paused_until = deadline
    row.pause_reason = reason.value
    row.resume_policy = (
        ResumePolicy.MANUAL_ONLY.value
        if forever or reason in _MANUAL_ONLY
        else ResumePolicy.TIMEOUT.value
    )
    row.resumed_at = None
    if reason is PauseReason.OPERATOR_REPLY:
        row.operator_last_seen_at = now
        if author_id:
            row.operator_author_id = author_id
        if author_name:
            row.operator_author_name = author_name
    if reason in (PauseReason.ESCALATION, PauseReason.SENSITIVE_TOPIC, PauseReason.POSTCHECK_FAIL):
        row.escalation_count = int(row.escalation_count or 0) + 1
        row.last_escalated_at = now

    await session.flush()
    await _mark(state, conv_key, deadline, now)

    state_value = (
        ConversationState.PAUSED_OPERATOR
        if reason is PauseReason.OPERATOR_REPLY
        else ConversationState.ESCALATED
    )
    await repo_conversation.set_state(session, conv_id, state_value)
    _log.info(
        "bot_paused",
        conv_key=conv_key,
        reason=reason.value,
        minutes=minutes,
        until=deadline.isoformat() if deadline is not None else "до возврата вручную",
    )


async def resume(
    state: StateStore,
    session: AsyncSession,
    conv_id: UUID,
    conv_key: str,
    *,
    by: Literal["timeout", "operator_command", "admin", "client_wrote_again"],
) -> None:
    """Снимает паузу: таймаут, команда оператора, админка или новый вопрос клиента.

    Последний случай — только для паузы, которую бот поставил себе сам, передавая
    вопрос администратору (см. :func:`escalation_pause_lifted`). Паузу, которую
    поставил человек, сообщение клиента не снимает никогда.
    """
    row = await _row(session, conv_id)
    if row is None:
        await _forget(state, conv_key)
        return
    await _clear(state, session, row, conv_key, by=by, now=datetime.now(tz=timezone.utc))


# --------------------------------------------------------------------------- #
# Признаки оператора
# --------------------------------------------------------------------------- #
async def operator_last_seen(session: AsyncSession, conv_id: UUID) -> datetime | None:
    """Когда человек последний раз писал в этот диалог. ``None`` — не писал.

    Нужна отправщику: строка ответа могла пролежать в очереди минуту, и за эту
    минуту в разговор мог войти человек. Отправить её после этого — то самое
    «бот продолжает вмешиваться», на которое жаловался владелец.
    """
    row = await _row(session, conv_id)
    return _aware(row.operator_last_seen_at) if row is not None else None


async def escalation_pause_lifted(
    state: StateStore, session: AsyncSession, conv_id: UUID, conv_key: str
) -> bool:
    """Снять ли паузу, потому что клиент написал снова, а человек не подключился.

    Передав вопрос администратору — или сняв собственный ответ постфильтром, —
    бот замолкал на час — и на всё, что клиент
    писал дальше, тоже. В живом прогоне 20 диалогов это дважды оставляло клиента
    без ответа: «а справка нужна какая-то?» и «давайте тогда в субботу на пробную
    прийдём» уходили в тишину.

    Час молчания задумывался как «не мешать администратору», но администратор
    ещё не написал ни слова: как только он напишет, диалог замолчит по другой,
    более сильной причине — ``PauseReason.OPERATOR_REPLY``, и её сообщение
    клиента не снимает. Поэтому здесь снимается только пауза самого бота.
    """
    row = await _row(session, conv_id)
    if row is None or not row.paused:
        return False
    if row.pause_reason not in _BOTS_OWN_PAUSES:
        return False
    if row.resume_policy == ResumePolicy.MANUAL_ONLY.value:
        return False
    seen = _aware(row.operator_last_seen_at)
    started = _aware(row.last_escalated_at)
    if seen is not None and (started is None or seen >= started):
        # Человек написал уже после передачи — дальше диалог ведёт он.
        # Реплика, оставшаяся от прошлого захода (его вернули через CRM,
        # и бот снова работает), молчания не оправдывает.
        return False
    await _clear(
        state, session, row, conv_key, by="client_wrote_again",
        now=datetime.now(tz=timezone.utc),
    )
    return True


def detect_operator(inbound: InboundMessage, *, known_outbox: bool) -> bool:
    """См. :func:`app.channels.normalize.is_operator_echo`.

    Расхождение признаков (``sentFromApp`` против «нет в нашем outbox») пишется
    событием ``echo_signal_mismatch`` — по нему закрывается открытый вопрос №12
    о поведении поля ``sentFromApp`` на проде.
    """
    signals = echo_signals(inbound, known_outbox=known_outbox)
    if signals.mismatch:
        _log.warning(ECHO_SIGNAL_MISMATCH_EVENT, **signals.as_log_fields())
    return is_operator_echo(inbound, known_outbox=known_outbox)


def is_resume_command(text: str | None, *, command: str) -> bool:
    """Служебная строка оператора (по умолчанию ``#бот``) снимает паузу.

    Сама строка клиенту не пересылается — она уже ушла от оператора в чат,
    наше дело только распознать её в эхе.
    """
    if not text or not command:
        return False
    cleaned = " ".join(text.split()).casefold()
    needle = command.strip().casefold()
    return bool(needle) and (cleaned == needle or cleaned.startswith(needle + " "))


def pause_minutes_for(reason: PauseReason, settings: Settings) -> int:
    """Длительность паузы для причины — из настроек, без литералов по коду."""
    mapping: dict[PauseReason, int] = {
        PauseReason.OPERATOR_REPLY: settings.pause_operator_minutes,
        PauseReason.USER_REQUEST: settings.pause_user_request_minutes,
        PauseReason.ESCALATION: settings.pause_escalation_minutes,
        PauseReason.SENSITIVE_TOPIC: settings.pause_escalation_minutes,
        PauseReason.CHILD_DETECTED: settings.pause_escalation_minutes,
        PauseReason.LLM_FAILURE: settings.pause_llm_failure_minutes,
        PauseReason.POSTCHECK_FAIL: settings.pause_postcheck_minutes,
        PauseReason.MANUAL: settings.pause_user_request_minutes,
        PauseReason.BUDGET_GUARD: _minutes_till_midnight(settings),
    }
    # Ноль сохраняется: это осмысленное значение — «до возврата вручную».
    minutes = int(mapping.get(reason, _DEFAULT_MINUTES))
    return minutes if minutes <= 0 else max(1, minutes)


# --------------------------------------------------------------------------- #
# Внутреннее
# --------------------------------------------------------------------------- #
async def _row(session: AsyncSession, conv_id: UUID) -> EscalationState | None:
    """Строка состояния диалога или ``None``."""
    result = await session.execute(
        sa.select(EscalationState).where(EscalationState.conversation_id == conv_id).limit(1)
    )
    return result.scalar_one_or_none()


async def _clear(
    state: StateStore,
    session: AsyncSession,
    row: EscalationState,
    conv_key: str,
    *,
    by: str,
    now: datetime,
) -> None:
    """Снимает паузу в базе и в быстром пути."""
    row.paused = False
    row.paused_until = None
    row.resumed_at = now
    await session.flush()
    await _forget(state, conv_key)
    await repo_conversation.set_state(session, row.conversation_id, ConversationState.ACTIVE)
    _log.info("bot_resumed", conv_key=conv_key, by=by)


#: TTL быстрого ключа для паузы без срока. Истина всё равно в базе: когда ключ
#: истечёт, ``is_paused`` прочитает строку и поставит его заново. Сутки — чтобы
#: не держать в Redis запись, которую снимут кнопкой через минуту.
_ENDLESS_PAUSE_TTL_S: Final[int] = 24 * 60 * 60


async def _mark(
    state: StateStore, conv_key: str, deadline: datetime | None, now: datetime
) -> None:
    """Ставит быстрый ключ паузы с TTL до конца окна.

    ``deadline is None`` — пауза без срока: ключ живёт сутки и переставляется
    при следующем чтении, пока человек не вернёт бота.
    """
    if deadline is None:
        try:
            await state.set(key_pause(conv_key), "manual", _ENDLESS_PAUSE_TTL_S)
        except Exception as exc:  # pragma: no cover - истина всё равно в базе
            _log.warning("pause_mark_failed", error=str(exc))
        return
    ttl = int((deadline - now).total_seconds())
    if ttl <= 0:
        return
    try:
        await state.set(key_pause(conv_key), deadline.isoformat(), ttl)
    except Exception as exc:  # pragma: no cover - истина всё равно в базе
        _log.warning("pause_mark_failed", error=str(exc))


async def _forget(state: StateStore, conv_key: str) -> None:
    """Убирает быстрый ключ паузы."""
    try:
        await state.delete(key_pause(conv_key))
    except Exception as exc:  # pragma: no cover
        _log.warning("pause_forget_failed", error=str(exc))


def _aware(value: datetime | None) -> datetime | None:
    """Приводит время из БД к aware-UTC: SQLite отдаёт naive."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _minutes_till_midnight(settings: Settings) -> int:
    """Сколько минут осталось до конца суток школы — для паузы по бюджету."""
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(tz=ZoneInfo(settings.timezone))
    except Exception:  # pragma: no cover - неизвестная таймзона
        now = datetime.now(tz=timezone.utc)
    minutes = (23 - now.hour) * 60 + (60 - now.minute)
    return max(15, minutes)
