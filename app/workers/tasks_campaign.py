"""Рассылка «Возврат клиентов»: черновики, отправка и статусы по переписке.

Одно периодическое задание раз в минуту, три шага, каждый со своей страховкой — сбой
одного не останавливает другие и никогда не доходит до ответов клиентам:

1. **Статусы** тех, кому уже написали: ответил, записался, купил, стоп, не доставлено,
   пора напоминание, не ответил. Читаются из базы самого бота — ответ клиента ведёт
   обычный разговор, отдельной отметки он не ждёт.
2. **Черновики** для «можно писать»: модель делает шаблон базы знаний личным по итогу
   прошлой переписки и проходит проверку фактов; не прошла или недоступна — шаблон.
3. **Отправка** — не больше одного сообщения за проход и только одобренного человеком:
   при включённых боте и рассылке, в часы рассылки, в пределах дневного лимита, с
   паузой после предыдущего, и после последней сверки с базой бота.

Сообщение уходит той же очередью, что и ответы бота (outbox → Wazzup), поэтому эхо не
принимается за администратора, а текст виден в переписке CRM. В историю модели оно
ложится со служебной заметкой: клиент ответит на сообщение, которого модель не писала.
Конвейер ответов клиенту этот модуль не трогает.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Final
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.campaign.contacts import (
    IN_PLAY,
    BotFacts,
    CampaignSegment,
    CampaignStatus,
    Decision,
    progress,
    send_check,
)
from app.campaign.facts import FACTS_SQL, facts_for, facts_from_rows
from app.campaign.messages import (
    DRAFT_INSTRUCTION,
    Brief,
    address_name,
    compose,
    draft_prompt,
    first_message_id,
    history_note,
    offers_beyond_template,
    other_sport,
    reminder_message_id,
    reminder_text,
    template_body,
    validate_draft,
)
from app.channels.errors import BAD_CONTACT_CODES, CHANNEL_CODES, SPAM_CODES, normalize_code
from app.core import ignore_list
from app.logging_conf import get_logger
from app.observability import metrics
from app.storage.models import CampaignContact, Conversation, OutboxMessage, utcnow
from app.types import (
    ChannelKind,
    InboundMessage,
    Language,
    LLMRequest,
    OutboundKind,
    OutboundMessage,
    OutboxState,
    ToolResult,
)

log = get_logger(__name__)

TASK_NAME: Final[str] = "campaign_cron"

#: Сколько черновиков пишет один проход. Проход раз в минуту, черновик — вызов модели:
#: больше за раз — дольше держим задание, а очередь в 200 контактов и так уходит за час.
DRAFTS_PER_PASS: Final[int] = 3

#: Сколько одобренных контактов перебирает один проход в поисках того, кому можно писать.
_CANDIDATES: Final[int] = 5

#: Потолок одного вызова модели для черновика, секунды.
_DRAFT_TIMEOUT_S: Final[float] = 30.0

#: Причины недоставки человеческим языком — их читает администратор в CRM.
_UNDELIVERED_TEXTS: Final[tuple[tuple[frozenset[str], str], ...]] = (
    (BAD_CONTACT_CODES, "Не доставлено: номера нет в WhatsApp или он недоступен"),
    (SPAM_CODES, "Не доставлено: WhatsApp счёл сообщение спамом — рассылка остановлена"),
    (CHANNEL_CODES, "Не доставлено: канал WhatsApp школы не работает"),
)
_SKIPPED_TEXTS: Final[dict[str, str]] = {
    "operator_took_over": "Не отправлено: в диалог вошёл администратор",
}

#: Насколько свежий сигнал «спам» останавливает рассылку. Свежий — значит, его ещё не
#: видели: старая строка outbox иначе выключала бы рассылку после каждого включения.
_SPAM_WINDOW: Final[timedelta] = timedelta(minutes=15)

#: Статусы, которые ещё может сдвинуть переписка.
_TRACKED: Final[tuple[str, ...]] = tuple(sorted(status.value for status in IN_PLAY))

#: Пометка черновика: откуда он.
SOURCE_MODEL: Final[str] = "model"
SOURCE_TEMPLATE: Final[str] = "template"


# --------------------------------------------------------------------------- #
# Задание
# --------------------------------------------------------------------------- #
async def campaign_cron(ctx: dict[str, Any]) -> None:
    """Раз в минуту: статусы, черновики, одно сообщение."""
    deps = _deps(ctx)
    started = time.perf_counter()
    now = _now()
    try:
        if not await _has_work(deps):
            return
        runtime = _owner_runtime(deps)
        settings = runtime.apply_to(deps.settings) if runtime is not None else deps.settings
        silent = ignore_list.silent_numbers(settings, runtime)
        kb = _kb_or_none(deps)
        policy = kb.policies.campaign if kb is not None else None

        due: list[str] = []
        if policy is not None:
            try:
                due = await track(deps, now=now, policy=policy, silent=silent)
            except Exception as exc:  # noqa: BLE001 - статусы подождут следующего прохода
                log.warning("campaign_track_failed", error=type(exc).__name__)
        if kb is not None:
            try:
                await recheck(deps, kb=kb)
                await draft(deps, kb=kb, settings=settings)
            except Exception as exc:  # noqa: BLE001 - черновики подождут следующего прохода
                log.warning("campaign_draft_failed", error=type(exc).__name__)
        if kb is not None and policy is not None and runtime is not None:
            try:
                await send(deps, now=now, kb=kb, runtime=runtime, settings=settings, silent=silent, due=due)
            except Exception as exc:  # noqa: BLE001 - отправка подождёт следующего прохода
                log.warning("campaign_send_failed", error=type(exc).__name__)
    finally:
        metrics.observe_job(TASK_NAME, time.perf_counter() - started)


async def _has_work(deps: Any) -> bool:
    """Есть ли кому писать черновик, отправлять или вести статус. Пустой список — ноль запросов дальше."""
    async with deps.sessionmaker() as session:
        try:
            found = (
                await session.execute(
                    sa.select(CampaignContact.id)
                    .where(
                        CampaignContact.status.in_(
                            (CampaignStatus.READY.value, CampaignStatus.DRAFT.value, CampaignStatus.APPROVED.value, *_TRACKED)
                        )
                    )
                    .limit(1)
                )
            ).first()
        except Exception as exc:  # noqa: BLE001 - таблицы ещё нет: запуск без create_all
            log.debug("campaign_table_unavailable", error=type(exc).__name__)
            return False
    return found is not None


# --------------------------------------------------------------------------- #
# 1. Статусы
# --------------------------------------------------------------------------- #
async def track(deps: Any, *, now: datetime, policy: Any, silent: frozenset[str]) -> list[str]:
    """Двигает статусы тех, кому уже написали. Возвращает id тех, кому пора напоминание."""
    due: list[str] = []
    async with deps.sessionmaker() as session:
        rows = (
            await session.execute(
                sa.select(CampaignContact)
                .where(CampaignContact.status.in_(_TRACKED))
                .order_by(CampaignContact.sent_at)
            )
        ).scalars().all()
        if not rows:
            return []
        facts = await _bot_facts(session, now)
        ids = [first_message_id(row.id.hex) for row in rows]
        ids += [reminder_message_id(row.id.hex) for row in rows if row.reminder_sent_at is not None]
        deliveries = await _deliveries(session, ids)
        # Спам — по любому сообщению рассылки, и первому, и напоминанию, но только свежий.
        spam = any(
            code in SPAM_CODES and updated is not None and updated >= now - _SPAM_WINDOW
            for _, _, code, updated in deliveries.values()
        )
        streak = await _failure_streak(session, now=now, size=int(policy.stop_after_failures))
        for row in rows:
            known = facts_for(facts, row.chat_id, silent=silent)
            state, reason, _, _ = deliveries.get(first_message_id(row.id.hex), (None, None, "", None))
            step = progress(
                _status(row.status),
                _segment(row.segment),
                known,
                now=now,
                sent_at=_aware(row.sent_at),
                reminder_sent_at=_aware(row.reminder_sent_at),
                delivered=state == OutboxState.SENT.value,
                undelivered=reason,
                remind_after=timedelta(days=float(policy.remind_after_days)),
                give_up_after=timedelta(days=float(policy.give_up_after_days)),
            )
            values: dict[str, Any] = {}
            if step.replied_at is not None and row.replied_at is None:
                values["replied_at"] = step.replied_at
            if step.decision is not None:
                values.update(status=step.decision.status.value, status_reason=step.decision.reason)
            if values:
                values["updated_at"] = now
                await session.execute(
                    sa.update(CampaignContact)
                    .where(CampaignContact.id == row.id, CampaignContact.status == row.status)
                    .values(**values)
                )
            if step.remind:
                due.append(row.id.hex)
        await session.commit()
    if spam:
        await _stop_campaign(
            deps,
            "Рассылка «Возврат клиентов» остановлена: WhatsApp пометил её сообщение как спам. "
            "Ответы клиентам идут как обычно. Прежде чем включать рассылку снова в настройках "
            "CRM, уменьшите число сообщений в день.",
            code="campaign_spam",
        )
    elif streak is not None:
        await _stop_campaign(
            deps,
            f"Рассылка «Возврат клиентов» остановлена: {int(policy.stop_after_failures)} сообщения подряд "
            f"не доставлены, ответ Wazzup — {streak}. Ответы клиентам идут как обычно. Подробности — "
            "в CRM, вкладка «Отправлены»; включите рассылку, когда причина устранена.",
            code="campaign_failures",
        )
    return due


async def _deliveries(
    session: AsyncSession, ids: list[UUID]
) -> dict[UUID, tuple[str, str | None, str, datetime | None]]:
    """Судьба сообщений рассылки: состояние строки outbox, причина недоставки словами
    (``None`` — доставлено или ещё в пути), код ошибки Wazzup и когда строка менялась."""
    if not ids:
        return {}
    rows = (
        await session.execute(
            sa.select(
                OutboxMessage.crm_message_id,
                OutboxMessage.state,
                OutboxMessage.last_error,
                OutboxMessage.next_attempt_at,
                OutboxMessage.updated_at,
            ).where(OutboxMessage.crm_message_id.in_(ids))
        )
    ).all()
    found: dict[UUID, tuple[str, str | None, str, datetime | None]] = {}
    for crm_id, state, error, next_attempt, updated in rows:
        # ``last_error`` — «код: сообщение» (tasks_outbound) или голый код пропуска.
        code = normalize_code(str(error or "").split(":", 1)[0])
        reason: str | None = None
        if state == OutboxState.SKIPPED.value:
            reason = _SKIPPED_TEXTS.get(str(error or ""), f"Не отправлено ({error or 'причина неизвестна'})")
        elif state == OutboxState.FAILED.value and next_attempt is None:
            reason = _undelivered_text(code, error)
        found[crm_id] = (str(state), reason, code, _aware(updated))
    return found


async def _failure_streak(session: AsyncSession, *, now: datetime, size: int) -> str | None:
    """Последние ``size`` первых сообщений не доставлены — код отказа последнего, иначе ``None``.

    Живая рассылка 27.10.2026: из пяти сообщений четыре подряд вернулись с кодом Wazzup,
    которого нет в документации, а рассылка шла дальше. Серия отказов — признак беды с
    каналом или номером, а не с клиентами; номера без WhatsApp (``BAD_CONTACT``) в старом
    списке ожидаемы и серию не составляют. Серия должна быть свежей: старые отказы после
    повторного включения рассылку снова не остановят.
    """
    if size <= 0:
        return None
    contacts = (
        await session.execute(
            sa.select(CampaignContact.id)
            .where(CampaignContact.sent_at.is_not(None))
            .order_by(CampaignContact.sent_at.desc())
            .limit(size)
        )
    ).scalars().all()
    if len(contacts) < size:
        return None
    found = await _deliveries(session, [first_message_id(contact.hex) for contact in contacts])
    failed = [
        (code, updated)
        for _, reason, code, updated in found.values()
        if reason is not None and code not in BAD_CONTACT_CODES
    ]
    if len(failed) < size:
        return None
    newest = max((updated for _, updated in failed if updated is not None), default=None)
    if newest is None or newest < now - _SPAM_WINDOW:
        return None
    return max(failed, key=lambda item: item[1] or now)[0] or "без кода"


def _undelivered_text(code: str, error: str | None = None) -> str:
    """Причина терминальной ошибки отправки словами; неизвестная — с ответом Wazzup как есть."""
    for codes, text in _UNDELIVERED_TEXTS:
        if code in codes:
            return text
    detail = str(error or "").split(":", 1)[-1].strip()
    return f"Не доставлено ({code or 'причина неизвестна'}){': ' + detail[:200] if detail else ''}"


async def _stop_campaign(deps: Any, text: str, *, code: str) -> None:
    """Сигнал беды с отправкой (спам, серия недоставок): рассылка выключается сама.

    После спама отправка с номера школы уже стоит час (``tasks_outbound``), но через час
    рассылка продолжила бы — и следующий такой сигнал мог стоить номера целиком.
    Включить снова может только человек, увидев причину. Тревога — только когда
    рассылка была включена: иначе тот же сигнал повторял бы её каждые четверть часа.
    """
    try:
        from app.admin.admin_store import AdminStore

        store = AdminStore(deps.settings.admin_db_path)
        try:
            if store.get("campaign_enabled") == "off":
                return
            store.set("campaign_enabled", "off")
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 - тревога ниже важнее
        log.error("campaign_disable_failed", error=type(exc).__name__)
    try:
        from app.notify.manager import notify_alert

        await notify_alert(deps, text, code=code)
    except Exception as exc:  # noqa: BLE001
        log.warning("campaign_alert_failed", error=type(exc).__name__)


# --------------------------------------------------------------------------- #
# 2. Черновики
# --------------------------------------------------------------------------- #
async def recheck(deps: Any, *, kb: Any) -> int:
    """Готовые тексты, которые предлагают лишнее, — на переписку. Возвращает их число.

    Правила черновика ужесточались после живой рассылки 27.10.2026, а написанные раньше
    тексты уже лежали в «Черновиках» и «Одобренных». Они проверяются тем же правилом и,
    если предлагают индивидуальные занятия, тарифы или чужой спорт, пишутся заново.
    Текст, который поправил человек (``edited``), остаётся: это его решение.
    """
    changed = 0
    async with deps.sessionmaker() as session:
        rows = (
            await session.execute(
                sa.select(CampaignContact).where(
                    CampaignContact.status.in_((CampaignStatus.DRAFT.value, CampaignStatus.APPROVED.value)),
                    sa.or_(CampaignContact.draft_source.is_(None), CampaignContact.draft_source != "edited"),
                )
            )
        ).scalars().all()
        for row in rows:
            approved = row.status == CampaignStatus.APPROVED.value
            text = row.approved_text if approved else row.draft_text
            template = template_body(kb, _segment(row.segment), Language.RU)
            if not offers_beyond_template(text, template=template):
                continue
            result = await session.execute(
                sa.update(CampaignContact)
                .where(CampaignContact.id == row.id, CampaignContact.status == row.status)
                .values(
                    status=CampaignStatus.READY.value,
                    status_reason=(
                        "Одобренный текст предлагал то, чего нет в приглашении, — пишется заново, одобрите снова"
                        if approved
                        else "Текст предлагал то, чего нет в приглашении, — пишется заново"
                    ),
                    draft_text=None,
                    draft_source=None,
                    approved_text=None,
                    approved_at=None,
                    updated_at=utcnow(),
                )
            )
            changed += int(result.rowcount or 0)
        await session.commit()
    if changed:
        log.info("campaign_texts_rewritten", count=changed)
    return changed


async def draft(deps: Any, *, kb: Any, settings: Any) -> int:
    """Пишет черновики тем, кого человек пустил в работу. Возвращает, сколько написано.

    Черновик пишется каждому в «можно писать», даже если текст у него уже есть: туда
    возвращаются и после новой проверки, и после новой загрузки файла — прежний текст
    мог быть написан под другой исход.
    """
    async with deps.sessionmaker() as session:
        rows = (
            await session.execute(
                sa.select(CampaignContact)
                .where(CampaignContact.status == CampaignStatus.READY.value)
                .order_by(CampaignContact.updated_at)
                .limit(DRAFTS_PER_PASS)
            )
        ).scalars().all()
        briefs = [(row.id, _brief(row), row.outcome) for row in rows]
        await session.commit()

    written = 0
    for contact_id, brief, outcome in briefs:
        sport = other_sport(brief) if outcome is None else None
        if sport is not None:
            # Человек этот контакт ещё не смотрел: отметка «писать можно» снимет вопрос.
            async with deps.sessionmaker() as session:
                await session.execute(
                    sa.update(CampaignContact)
                    .where(CampaignContact.id == contact_id, CampaignContact.status == CampaignStatus.READY.value)
                    .values(
                        status=CampaignStatus.NEEDS_CHECK.value,
                        status_reason=f"Спрашивал про другой спорт («{sport}…») — в школе его нет. Писать ли?",
                        updated_at=utcnow(),
                    )
                )
                await session.commit()
            continue
        text, source = await write_draft(deps, kb=kb, brief=brief, tz_name=settings.timezone, key=contact_id.hex)
        if text is None:
            log.warning("campaign_no_template", segment=brief.segment.value)
            continue
        reason = (
            "Черновик написан по итогу прошлой переписки — прочитайте и одобрите"
            if source == SOURCE_MODEL
            else "Черновик по шаблону: модель сейчас недоступна или её текст не прошёл проверку"
        )
        async with deps.sessionmaker() as session:
            result = await session.execute(
                sa.update(CampaignContact)
                .where(CampaignContact.id == contact_id, CampaignContact.status == CampaignStatus.READY.value)
                .values(
                    status=CampaignStatus.DRAFT.value,
                    draft_text=text,
                    draft_source=source,
                    status_reason=reason,
                    updated_at=utcnow(),
                )
            )
            await session.commit()
        written += int(result.rowcount or 0)
    if written:
        log.info("campaign_drafts_written", count=written)
    return written


async def write_draft(deps: Any, *, kb: Any, brief: Brief, tz_name: str, key: str) -> tuple[str | None, str | None]:
    """Текст первого сообщения и откуда он. ``(None, None)`` — в базе знаний нет текстов возврата."""
    lang = Language.RU
    body = template_body(kb, brief.segment, lang)
    if body is None:
        return None, None
    name = address_name(brief.parent_name)
    fallback = compose(kb, body, name=name, lang=lang)
    if fallback is None:
        return None, None
    request = LLMRequest(
        system_instruction=DRAFT_INSTRUCTION,
        history=[],
        user_text=draft_prompt(brief, body, tz_name=tz_name),
        dynamic_note="",
        tool_specs=(),
        tool_mode="NONE",
        lang=lang,
        correlation_id=f"campaign:{key}",
        max_output_tokens=getattr(deps.settings, "gemini_max_output_tokens", None),
    )
    try:
        response = await asyncio.wait_for(deps.llm.generate(request, _no_tools), timeout=_DRAFT_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - модель недоступна: шаблон и есть запасной путь
        log.info("campaign_draft_by_template", reason=type(exc).__name__)
        return fallback, SOURCE_TEMPLATE
    text = validate_draft(
        getattr(response, "text", None),
        kb=kb,
        name=name,
        known_names=[brief.child_name or ""],
        lang=lang,
        segment=brief.segment,
        facts=" ".join(value for value in (brief.summary, brief.obstacle, brief.note) if value),
    )
    if text is None:
        log.info("campaign_draft_by_template", reason="validation")
        return fallback, SOURCE_TEMPLATE
    return text, SOURCE_MODEL


async def _no_tools(name: str, args: dict[str, Any]) -> ToolResult:
    """У черновика инструментов нет."""
    return ToolResult.invalid_input(f"инструменты недоступны в черновике рассылки ({name})")


# --------------------------------------------------------------------------- #
# 3. Отправка
# --------------------------------------------------------------------------- #
async def send(
    deps: Any,
    *,
    now: datetime,
    kb: Any,
    runtime: Any,
    settings: Any,
    silent: frozenset[str],
    due: list[str],
) -> bool:
    """Одно сообщение рассылки, если сейчас можно. ``True`` — сообщение поставлено в очередь."""
    if not (runtime.bot_enabled and runtime.campaign_enabled):
        return False
    if not within_hours(now, runtime.campaign_hours, tz_name=settings.timezone):
        return False
    policy = kb.policies.campaign
    async with deps.sessionmaker() as session:
        today = await _sent_since(session, day_start(now, tz_name=settings.timezone))
        last = await _last_send(session)
    if today >= runtime.campaign_daily_limit:
        return False
    if last is not None and now < last + pause_after(last, policy.min_gap_minutes, policy.max_gap_minutes):
        return False
    if await _channel_stopped(deps, settings):
        return False

    for contact_id in due:
        if await send_reminder(deps, contact_id, now=now, kb=kb, silent=silent):
            return True
    async with deps.sessionmaker() as session:
        candidates = (
            await session.execute(
                sa.select(CampaignContact.id)
                .where(
                    CampaignContact.status == CampaignStatus.APPROVED.value,
                    sa.or_(CampaignContact.not_before.is_(None), CampaignContact.not_before <= now),
                )
                .order_by(CampaignContact.approved_at, CampaignContact.id)
                .limit(_CANDIDATES)
            )
        ).scalars().all()
    for contact_id in candidates:
        if await send_first(deps, contact_id.hex, now=now, kb=kb, settings=settings, silent=silent):
            return True
    return False


async def send_first(
    deps: Any, contact_id: str, *, now: datetime, kb: Any, settings: Any, silent: frozenset[str]
) -> bool:
    """Первое сообщение контакту — после последней сверки. ``True`` — поставлено в очередь."""
    from app.core import session as conv_session
    from app.storage import repo_outbox

    cid = UUID(contact_id)
    async with deps.sessionmaker() as session:
        row = await session.get(CampaignContact, cid)
        if row is None or row.status != CampaignStatus.APPROVED.value:
            return False
        facts = await _bot_facts(session, now)
        known = facts_for(facts, row.chat_id, silent=silent)
        blocked = send_check(
            _status(row.status),
            _segment(row.segment),
            known,
            now=now,
            checked_at=_aware(row.checked_at),
            approved_at=_aware(row.approved_at),
        )
        text = (row.approved_text or "").strip()
        if blocked is None and not text:
            blocked = Decision(CampaignStatus.DRAFT, _segment(row.segment), "Одобрен пустой текст — одобрите заново")
        if (
            blocked is None
            and row.draft_source != "edited"
            and offers_beyond_template(text, template=template_body(kb, _segment(row.segment), Language.RU))
        ):
            # Вторая страховка после recheck(): лишнее предложение не уходит ни при каком сбое.
            blocked = Decision(
                CampaignStatus.READY, _segment(row.segment), "Текст предлагал то, чего нет в приглашении, — пишется заново"
            )
        if blocked is not None:
            await session.execute(
                sa.update(CampaignContact)
                .where(CampaignContact.id == cid, CampaignContact.status == CampaignStatus.APPROVED.value)
                .values(
                    status=blocked.status.value,
                    segment=blocked.segment.value,
                    status_reason=blocked.reason,
                    conversation_id=known.conversation_id,
                    updated_at=now,
                )
            )
            await session.commit()
            log.info("campaign_send_blocked", status=blocked.status.value)
            return False

        conv = await _conversation(session, row, known, settings=settings, kb=kb)
        if conv is None:
            await session.rollback()
            log.warning("campaign_no_whatsapp_channel")
            return False
        message = OutboundMessage(
            crm_message_id=first_message_id(contact_id),
            conversation_id=conv.id,
            channel_id=conv.channel_id,
            channel=ChannelKind.WHATSAPP,
            chat_id=conv.chat_id,
            lang=Language.RU,
            kind=OutboundKind.CAMPAIGN,
            text=text,
        )
        outbox_id = await repo_outbox.enqueue(session, message)
        note = history_note(_brief(row), tz_name=settings.timezone)
        await conv_session.save_turn(
            session,
            conv,
            [{"role": "user", "parts": [{"text": note}]}, {"role": "model", "parts": [{"text": text}]}],
        )
        claimed = await session.execute(
            sa.update(CampaignContact)
            .where(CampaignContact.id == cid, CampaignContact.status == CampaignStatus.APPROVED.value)
            .values(
                status=CampaignStatus.SENT.value,
                status_reason="Отправлено — ждём ответа",
                sent_at=now,
                conversation_id=conv.id.hex,
                updated_at=now,
            )
        )
        if not claimed.rowcount:
            # Человек в эту же секунду вернул черновик на правку: сообщение не уходит.
            await session.rollback()
            return False
        await session.commit()

    await _enqueue(deps, outbox_id)
    log.info("campaign_message_queued", kind="first")
    return True


async def send_reminder(deps: Any, contact_id: str, *, now: datetime, kb: Any, silent: frozenset[str]) -> bool:
    """Одно напоминание молчащему клиенту. ``True`` — поставлено в очередь."""
    from app.core import session as conv_session
    from app.storage import repo_conversation, repo_outbox

    cid = UUID(contact_id)
    async with deps.sessionmaker() as session:
        row = await session.get(CampaignContact, cid)
        if row is None or row.status != CampaignStatus.SENT.value or row.reminder_sent_at is not None:
            return False
        known = facts_for(await _bot_facts(session, now), row.chat_id, silent=silent)
        sent_at = _aware(row.sent_at)
        if known.stop_word or known.human_pause or known.ignored or sent_at is None:
            return False
        if known.last_inbound_at is not None and known.last_inbound_at > sent_at:
            return False
        conv = await repo_conversation.get_by_id(session, UUID(row.conversation_id)) if row.conversation_id else None
        if conv is None:
            return False
        lang = Language.KK if conv.lang == Language.KK.value else Language.RU
        text = reminder_text(kb, lang)
        if text is None:
            return False
        message = OutboundMessage(
            crm_message_id=reminder_message_id(contact_id),
            conversation_id=conv.id,
            channel_id=conv.channel_id,
            channel=ChannelKind.WHATSAPP,
            chat_id=conv.chat_id,
            lang=lang,
            kind=OutboundKind.CAMPAIGN,
            text=text,
        )
        outbox_id = await repo_outbox.enqueue(session, message)
        await conv_session.save_turn(session, conv, [{"role": "model", "parts": [{"text": text}]}])
        claimed = await session.execute(
            sa.update(CampaignContact)
            .where(
                CampaignContact.id == cid,
                CampaignContact.status == CampaignStatus.SENT.value,
                CampaignContact.reminder_sent_at.is_(None),
            )
            .values(reminder_sent_at=now, status_reason="Напомнили — ждём ответа", updated_at=now)
        )
        if not claimed.rowcount:
            await session.rollback()
            return False
        await session.commit()

    await _enqueue(deps, outbox_id)
    log.info("campaign_message_queued", kind="reminder")
    return True


async def _conversation(session: AsyncSession, row: CampaignContact, known: BotFacts, *, settings: Any, kb: Any) -> Conversation | None:
    """Диалог бота с этим номером; нет — заводится, как завёл бы его первый ответ клиента.

    Ключ диалога тот же, что посчитает приём сообщения (``канал:whatsapp:номер``), поэтому
    ответ клиента ляжет в этот же диалог — вместе с нашим сообщением в истории модели.
    """
    from app.storage import repo_conversation
    from app.storage.db import insert_or_ignore

    channel_id = await _whatsapp_channel(session, settings)
    if known.conversation_id:
        try:
            found = await repo_conversation.get_by_id(session, UUID(known.conversation_id))
        except ValueError:
            found = None
        # Диалог через прежний канал Wazzup не годится: отправка через него не дойдёт, а
        # ответ клиента придёт уже нынешним каналом — в другой диалог.
        if found is not None and (not channel_id or found.channel_id == channel_id):
            return found
    if not channel_id:
        return None
    conv_key = InboundMessage.make_conv_key(channel_id, ChannelKind.WHATSAPP.value, row.chat_id)
    existing = await repo_conversation.get_by_conv_key(session, conv_key)
    if existing is not None:
        return existing
    stamp = utcnow()
    await session.execute(
        insert_or_ignore(
            session,
            Conversation,
            {
                "id": uuid4(),
                "conv_key": conv_key,
                "channel_id": channel_id,
                "chat_type": ChannelKind.WHATSAPP.value,
                "chat_id": row.chat_id,
                "contact_name": row.wa_name,
                "phone_e164": f"+{row.chat_id}",
                "state": "new",
                "kb_hash_at_start": getattr(kb, "kb_hash", None),
                "created_at": stamp,
                "updated_at": stamp,
            },
            index_elements=["conv_key"],
        )
    )
    return await repo_conversation.get_by_conv_key(session, conv_key)


async def _whatsapp_channel(session: AsyncSession, settings: Any) -> str:
    """Канал WhatsApp школы — тот, через который приходят ответы клиентов.

    Надёжнее всего — канал последнего живого диалога: с ним посчитает ключ приём ответа.
    Диалогов ещё нет — канал из настроек, как его видит кабинет Wazzup.
    """
    latest = (
        await session.execute(
            sa.select(Conversation.channel_id)
            .where(Conversation.chat_type == ChannelKind.WHATSAPP.value, Conversation.last_inbound_at.is_not(None))
            .order_by(Conversation.last_inbound_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest:
        return str(latest)
    from app.notify.manager import resolve_channel_id

    configured, _ = resolve_channel_id(getattr(settings, "wazzup_channel_id_whatsapp", "") or "", ChannelKind.WHATSAPP)
    return configured


async def _channel_stopped(deps: Any, settings: Any) -> bool:
    """Остановлен ли канал WhatsApp школы после спам-блока или аварии."""
    from app.workers.tasks_outbound import _channel_stopped as stopped

    async with deps.sessionmaker() as session:
        channel_id = await _whatsapp_channel(session, settings)
    return bool(channel_id) and await stopped(deps, channel_id)


# --------------------------------------------------------------------------- #
# Темп
# --------------------------------------------------------------------------- #
def within_hours(now: datetime, hours: tuple[int, int], *, tz_name: str) -> bool:
    """Сейчас — часы рассылки? ``hours`` — минуты от полуночи по часам школы, конец не входит."""
    local = now.astimezone(_zone(tz_name))
    minute = local.hour * 60 + local.minute
    start, end = hours
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def day_start(now: datetime, *, tz_name: str) -> datetime:
    """Полночь сегодняшнего дня по часам школы — в UTC: с неё считается дневной лимит."""
    local = now.astimezone(_zone(tz_name))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def pause_after(last: datetime, min_minutes: int, max_minutes: int) -> timedelta:
    """Пауза после предыдущего сообщения: от и до, разная от раза к разу.

    Одинаковый шаг между сообщениями — признак автомата. Длина выводится из момента
    прошлой отправки, а не из случайного числа: проход раз в минуту иначе бросал бы
    кости заново и пауза выходила бы короче задуманной.
    """
    low, high = sorted((max(1, int(min_minutes)), max(1, int(max_minutes))))
    return timedelta(minutes=low + int(last.timestamp()) % (high - low + 1))


async def _sent_since(session: AsyncSession, since: datetime) -> int:
    """Сколько сообщений рассылки ушло с этого момента: первые и напоминания."""
    first = (
        await session.execute(sa.select(sa.func.count()).select_from(CampaignContact).where(CampaignContact.sent_at >= since))
    ).scalar_one()
    reminders = (
        await session.execute(
            sa.select(sa.func.count()).select_from(CampaignContact).where(CampaignContact.reminder_sent_at >= since)
        )
    ).scalar_one()
    return int(first or 0) + int(reminders or 0)


async def _last_send(session: AsyncSession) -> datetime | None:
    """Когда ушло последнее сообщение рассылки."""
    first, reminder = (
        await session.execute(sa.select(sa.func.max(CampaignContact.sent_at), sa.func.max(CampaignContact.reminder_sent_at)))
    ).one()
    moments = [_aware(_ts(value)) for value in (first, reminder) if value is not None]
    return max((moment for moment in moments if moment is not None), default=None)


# --------------------------------------------------------------------------- #
# Внутреннее
# --------------------------------------------------------------------------- #
async def _bot_facts(session: AsyncSession, now: datetime) -> dict[str, BotFacts]:
    rows = (await session.execute(sa.text(FACTS_SQL))).mappings().all()
    return facts_from_rows(rows, now=now)


def _brief(row: CampaignContact) -> Brief:
    return Brief(
        segment=_segment(row.segment),
        parent_name=row.parent_name,
        child_name=row.child_name,
        summary=row.summary,
        obstacle=row.obstacle,
        next_step=row.next_step,
        note=row.note,
        last_contact_at=_aware(row.last_contact_at),
    )


async def _enqueue(deps: Any, outbox_id: UUID) -> None:
    try:
        await deps.queue.enqueue_outbox(outbox_id)
    except Exception as exc:  # noqa: BLE001 - строку подберёт outbox_sweep_cron
        log.warning("campaign_enqueue_failed", error=type(exc).__name__)


def _status(value: str) -> CampaignStatus:
    try:
        return CampaignStatus(value)
    except ValueError:
        return CampaignStatus.NEEDS_CHECK


def _segment(value: str) -> CampaignSegment:
    try:
        return CampaignSegment(value)
    except ValueError:
        return CampaignSegment.UNKNOWN


def _ts(value: Any) -> datetime | None:
    """Агрегат по колонке времени SQLite отдаёт строку, а не ``datetime``."""
    if value is None or isinstance(value, datetime):
        return value
    from app.campaign.facts import parse_ts

    return parse_ts(value)


def _aware(value: datetime | None) -> datetime | None:
    """Наивное время из SQLite считаем UTC — иначе сравнения падают."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("Asia/Almaty")


def _kb_or_none(deps: Any) -> Any:
    try:
        return deps.kb()
    except Exception:  # noqa: BLE001
        return None


def _owner_runtime(deps: Any) -> Any | None:
    """Настройки владельца. ``None`` — не читаются: тогда рассылка не идёт вовсе."""
    factory = getattr(deps, "runtime", None)
    if factory is None:
        return None
    try:
        return factory()
    except Exception as exc:  # noqa: BLE001
        log.warning("runtime_settings_failed", error=type(exc).__name__)
        return None


def _deps(ctx: dict[str, Any]) -> Any:
    deps = ctx.get("deps")
    if deps is None:
        raise RuntimeError("В контексте воркера нет PipelineDeps: не отработал startup")
    return deps


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "DRAFTS_PER_PASS",
    "TASK_NAME",
    "campaign_cron",
    "day_start",
    "draft",
    "first_message_id",
    "pause_after",
    "recheck",
    "reminder_message_id",
    "send",
    "send_first",
    "send_reminder",
    "track",
    "within_hours",
    "write_draft",
]
