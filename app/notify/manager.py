"""Доставка карточек и тревог администратору школы.

Уведомление — это обычное исходящее сообщение в мессенджер сотрудника, поэтому
оно идёт тем же путём, что и ответы клиенту: через outbox и воркер отправки.
Прямых вызовов Wazzup здесь нет — иначе тревога «канал лежит» пыталась бы уйти
через лежащий канал в тот же миг и потерялась бы.

Канал и адрес администратора берутся из настроек:
``manager_notify_channel`` / ``manager_notify_target`` / ``manager_notify_channel_id``.
Адрес не задан — уведомление не отправляется, но и процесс не падает: молчащий бот
хуже, чем бот без уведомлений, а на Railway настройки правятся за минуту.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from app.config import Settings, get_settings
from app.logging_conf import get_logger
from app.notify.templates import render_alert, render_escalation_card, render_lead_card
from app.types import (
    ChannelKind,
    EscalationReason,
    Language,
    LeadDraft,
    ManagerCard,
    ManagerCardKind,
    OutboundKind,
    OutboundMessage,
    ToolServices,
    Urgency,
)

if TYPE_CHECKING:  # pragma: no cover - только аннотации, рантайм-зависимости нет
    from app.core.pipeline import PipelineDeps
    from app.kb.models import KBSnapshot

log = get_logger(__name__)

#: Одинаковую тревогу чаще, чем раз в 15 минут, слать бессмысленно: администратор
#: получит десять сообщений про один и тот же мёртвый канал и перестанет их читать.
ALERT_DEDUP_TTL_S: Final[int] = 900

#: Ключ подавления повторов в StateStore.
_ALERT_KEY: Final[str] = "alert:{code}"


async def notify(services: ToolServices, card: ManagerCard) -> None:
    """Ставит карточку в outbox на канал менеджера (``manager_notify_channel/target``).

    Ошибку наружу не выпускает: уведомление администратору не должно ронять
    обработку сообщения клиента.
    """
    message = build_manager_message(card)
    if message is None:
        return
    try:
        await services.enqueue_outbound(message)
    except Exception as exc:  # noqa: BLE001 - уведомление не важнее диалога
        log.warning(
            "manager_notify_failed",
            card_kind=card.kind.value,
            error=type(exc).__name__,
        )
        return
    log.info(
        "manager_notified",
        card_kind=card.kind.value,
        urgency=card.urgency.value,
        reason=card.reason.value if card.reason else None,
        conversation_id=str(card.conversation_id) if card.conversation_id else None,
    )


async def notify_alert(deps: "PipelineDeps", text: str, *, code: str) -> None:
    """Технические тревоги: канал неисправен, спам-блок, KB не загрузилась, бюджет исчерпан.

    Работает в обход пайплайна — у тревоги нет диалога, поэтому строка кладётся
    в outbox напрямую и сразу ставится в очередь отправки. Повторы с одним и тем же
    ``code`` подавляются на :data:`ALERT_DEDUP_TTL_S` секунд.
    """
    settings = getattr(deps, "settings", None) or get_settings()
    if not await _alert_allowed(deps, code):
        log.debug("alert_suppressed", code=code)
        return

    card = ManagerCard(
        kind=ManagerCardKind.ALERT,
        text=render_alert(text, code=code),
        urgency=Urgency.HIGH,
    )
    message = build_manager_message(card, settings=settings)
    if message is None:
        # Адреса нет — тревога всё равно обязана остаться в логах Railway.
        log.error("alert_undelivered", code=code, alert=text)
        return

    log.error("alert_raised", code=code, alert=text)

    outbox_id = await _enqueue_direct(deps, message)
    if outbox_id is None:
        return
    try:
        await deps.queue.enqueue_outbox(outbox_id)
    except Exception as exc:  # noqa: BLE001 - строка уже в outbox, её подберёт cron
        log.warning("alert_enqueue_failed", code=code, error=type(exc).__name__)


def build_manager_message(
    card: ManagerCard, *, settings: Settings | None = None
) -> OutboundMessage | None:
    """Карточка → исходящее сообщение администратору. ``None`` — адрес не настроен."""
    cfg = settings or get_settings()
    target = manager_target(cfg)
    if target is None:
        # ОШИБКА, а не предупреждение. Здесь теряется лид: бот довёл родителя до
        # записи, сказал «администратор свяжется» — и карточку никто не получил.
        # Предупреждение в логе такое не удержит, его никто не читает.
        log.error(
            "lead_lost_no_manager_target",
            card_kind=card.kind.value,
            conversation_id=str(card.conversation_id) if card.conversation_id else None,
            lead_id=str(card.lead_id) if card.lead_id else None,
            hint="задайте MANAGER_NOTIFY_TARGET и MANAGER_NOTIFY_CHANNEL_ID",
        )
        return None
    channel, channel_id, chat_id = target
    text = (card.text or "").strip()
    if not text:
        return None
    return OutboundMessage(
        conversation_id=None,
        channel_id=channel_id,
        channel=channel,
        chat_id=chat_id,
        lang=Language.RU,  # карточку читает сотрудник, она всегда по-русски
        kind=OutboundKind.MANAGER_CARD,
        text=text,
    )


def manager_target(settings: Settings | None = None) -> tuple[ChannelKind, str, str] | None:
    """``(канал, channelId, chatId)`` администратора. ``None`` — не настроено.

    Для WhatsApp ``chatId`` — только цифры (``77012345678``), для Instagram —
    igsid из вебхука; сконструировать его нельзя, поэтому значение берётся из настроек как есть.
    """
    cfg = settings or get_settings()
    raw_target = (cfg.manager_notify_target or "").strip()
    if not raw_target:
        return None

    channel = cfg.manager_notify_channel
    channel_id = (cfg.manager_notify_channel_id or "").strip() or _default_channel_id(cfg, channel)
    if not channel_id:
        return None

    chat_id = raw_target
    if channel is ChannelKind.WHATSAPP:
        digits = "".join(ch for ch in raw_target if ch.isdigit())
        if not digits:
            return None
        chat_id = digits
    return channel, channel_id, chat_id


def build_lead_card(
    lead: LeadDraft,
    *,
    kb: "KBSnapshot",
    gym_title: str | None = None,
    channel: ChannelKind,
    dialog_url: str | None = None,
    now: datetime | None = None,
    lead_id: UUID | None = None,
) -> ManagerCard:
    """Собирает :class:`ManagerCard` с лид-карточкой. Текст — ``templates.render_lead_card``."""
    moment = now or datetime.now(tz=timezone.utc)
    return ManagerCard(
        kind=ManagerCardKind.LEAD,
        text=render_lead_card(
            lead,
            kb=kb,
            gym_title=gym_title,
            channel=channel,
            dialog_url=dialog_url or lead.dialog_url,
            now=moment,
        ),
        conversation_id=lead.conversation_id,
        lead_id=lead_id or lead.lead_id,
        lang=lead.lang or Language.RU,
        urgency=Urgency.NORMAL,
    )


def build_escalation_card(
    *,
    reason: EscalationReason,
    question: str,
    lang: Language,
    phone: str | None,
    channel: ChannelKind,
    dialog_url: str | None = None,
    now: datetime | None = None,
    conversation_id: UUID | None = None,
    urgency: Urgency = Urgency.HIGH,
) -> ManagerCard:
    """Собирает :class:`ManagerCard` с карточкой эскалации."""
    moment = now or datetime.now(tz=timezone.utc)
    return ManagerCard(
        kind=ManagerCardKind.ESCALATION,
        text=render_escalation_card(
            reason=reason,
            question=question,
            lang=lang,
            phone=phone,
            channel=channel,
            dialog_url=dialog_url,
            now=moment,
        ),
        conversation_id=conversation_id,
        lang=lang,
        reason=reason,
        urgency=urgency,
    )


# --------------------------------------------------------------------------- #
# Внутреннее
# --------------------------------------------------------------------------- #
def _default_channel_id(settings: Settings, channel: ChannelKind) -> str:
    """Канал уведомлений по умолчанию — рабочий канал школы того же транспорта."""
    if channel is ChannelKind.WHATSAPP:
        return (settings.wazzup_channel_id_whatsapp or "").strip()
    return (settings.wazzup_channel_id_instagram or "").strip()


async def _alert_allowed(deps: Any, code: str) -> bool:
    """Не слали ли эту же тревогу только что. Без StateStore — всегда разрешено."""
    state = getattr(deps, "state", None)
    if state is None:
        return True
    try:
        return await state.set_if_absent(_ALERT_KEY.format(code=code), "1", ALERT_DEDUP_TTL_S)
    except Exception:  # noqa: BLE001 - Redis лёг: лучше лишняя тревога, чем ни одной
        return True


async def _enqueue_direct(deps: Any, message: OutboundMessage) -> UUID | None:
    """Кладёт сообщение в outbox собственной транзакцией. ``None`` — не удалось."""
    from app.storage import repo_outbox  # локальный импорт: модуль тяжелее, чем нужно на импорте

    sessionmaker = getattr(deps, "sessionmaker", None)
    if sessionmaker is None:
        log.error("alert_no_sessionmaker")
        return None
    try:
        async with sessionmaker() as session:
            outbox_id = await repo_outbox.enqueue(session, message)
            await session.commit()
            return outbox_id
    except Exception as exc:  # noqa: BLE001 - тревога уже в логах, БД может быть недоступна
        log.error("alert_persist_failed", error=type(exc).__name__)
        return None


__all__ = [
    "ALERT_DEDUP_TTL_S",
    "build_escalation_card",
    "build_lead_card",
    "build_manager_message",
    "manager_target",
    "notify",
    "notify_alert",
]
