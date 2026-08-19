"""Передача диалога человеку — инструмент ``escalate_to_manager``.

Эскалация — не аварийный выход, а нормальный сценарий: половина пробелов в базе
знаний (расписание, условия пробного, справка, оплата) закрывается только живым
администратором. Инструмент делает три вещи и ровно в этом порядке:

1. **ставит паузу бота** — чтобы автоответ не перебивал человека, который уже
   набирает ответ;
2. **уведомляет администратора** карточкой «НУЖЕН ЖИВОЙ ОТВЕТ»;
3. **возвращает готовый текст клиенту** из ``kb/i18n.yaml``
   (``render_hint=FIXED_REPLY`` — модель этот текст не переписывает).

Чего инструмент **не** делает: не гасит счётчик неотвеченных сообщений в
Wazzup. ``clearUnanswered`` в проекте всегда ``false``
(:class:`app.types.OutboundMessage` объявляет это типом), потому что диалог,
переданный человеку, обязан оставаться в списке «требует ответа», пока человек
не ответил. Автоответ, снимающий отметку, — это способ потерять клиента тихо.
"""

from __future__ import annotations

from typing import Final

from app.types import (
    TOOL_ESCALATION_REASONS,
    EscalationReason,
    Language,
    ManagerCard,
    ManagerCardKind,
    PauseReason,
    RenderHint,
    ToolContext,
    ToolResult,
    Urgency,
)
from app.tools.booking import format_local_dt, render_card_text

#: Причины, для которых в базе знаний есть отдельная, более точная фраза клиенту.
_REPLY_KEYS: Final[dict[str, str]] = {
    EscalationReason.MEDICAL.value: "escalation.medical",
    EscalationReason.COMPLAINT.value: "escalation.complaint",
    EscalationReason.FOREIGN_LANGUAGE.value: "escalation.foreign_language",
}

#: Причины, по которым администратора дёргаем срочно независимо от того, что
#: сказала модель: здоровье ребёнка и жалоба ждать не могут.
_ALWAYS_HIGH: Final[frozenset[str]] = frozenset(
    {EscalationReason.MEDICAL.value, EscalationReason.COMPLAINT.value}
)

#: Запасная длительность паузы, если в policies.yaml значение не задано.
_DEFAULT_PAUSE_MINUTES: Final[int] = 60


def _reply_key(reason: str) -> str:
    """Ключ i18n с текстом, который увидит клиент."""
    return _REPLY_KEYS.get(reason, "escalation.handoff")


async def escalate_to_manager(
    ctx: ToolContext,
    *,
    reason: str,
    question_summary: str,
    urgency: str = "normal",
) -> ToolResult:
    """Ставит паузу, шлёт карточку «НУЖЕН ЖИВОЙ ОТВЕТ», возвращает готовый текст клиенту
    из i18n `escalation.handoff`. render_hint=FIXED_REPLY — модель этот текст не переписывает."""
    kb = ctx.kb
    lang: Language = ctx.lang

    reason_key = str(reason or "").strip().lower()
    if reason_key not in TOOL_ESCALATION_REASONS:
        return ToolResult.invalid_input(f"недопустимая причина эскалации '{reason}'")
    reason_enum = EscalationReason(reason_key)

    summary = " ".join(str(question_summary or "").split())[:200].strip()
    if not summary:
        return ToolResult.invalid_input("question_summary пуст: администратору нечего передать")

    try:
        level = Urgency(str(urgency or "normal").strip().lower())
    except ValueError:
        level = Urgency.NORMAL
    if reason_key in _ALWAYS_HIGH:
        level = Urgency.HIGH

    pause_minutes = int(getattr(kb.policies, "escalation_pause_minutes", _DEFAULT_PAUSE_MINUTES) or _DEFAULT_PAUSE_MINUTES)

    # 1. Пауза. Ставится первой: если следом упадёт уведомление, бот всё равно
    #    замолчит и не будет отвечать поверх человека.
    paused = False
    try:
        await ctx.services.set_pause(ctx.conv_key, minutes=pause_minutes, reason=PauseReason.ESCALATION)
        paused = True
    except Exception as exc:  # pragma: no cover - зависит от инфраструктуры
        _ = exc

    # 2. Карточка администратору.
    draft = ctx.lead_draft
    card_text = render_card_text(
        kb,
        "lead_card.escalation",
        {
            "phone": draft.phone or draft.channel_user or ctx.chat_id or "—",
            "question": summary,
            "lang": lang.value,
            "reason": reason_enum.value,
            "channel": ctx.channel.value,
            "dt": format_local_dt(ctx.now),
        },
    )
    card = ManagerCard(
        kind=ManagerCardKind.ESCALATION,
        text=card_text,
        conversation_id=ctx.conversation_id,
        lead_id=draft.lead_id,
        lang=lang,
        reason=reason_enum,
        urgency=level,
    )
    admin_notified = False
    try:
        await ctx.services.notify_manager(card)
        admin_notified = True
    except Exception as exc:  # pragma: no cover - зависит от инфраструктуры
        _ = exc

    # 3. Готовый ответ клиенту. Инструмент его НЕ отправляет сам: пайплайн берёт
    #    текст из data и ставит в outbox одним сообщением, с clearUnanswered=false.
    reply_key = _reply_key(reason_key)
    reply_text = kb.text(reply_key, lang)

    caveats = [
        "Текст ответа уже готов (data.reply_text) — отдай его как есть, ничего не добавляя.",
        "Не обещай срок ответа администратора и не называй его телефон.",
    ]
    return ToolResult.success(
        data={
            "escalated": True,
            "reason": reason_enum.value,
            "urgency": level.value,
            "question_summary": summary,
            "reply_text": reply_text,
            "reply_i18n_key": reply_key,
            "paused": paused,
            "pause_minutes": pause_minutes,
            "pause_reason": PauseReason.ESCALATION.value,
            "admin_notified": admin_notified,
            "clear_unanswered": False,
        },
        render_hint=RenderHint.FIXED_REPLY,
        caveats=caveats,
        meta={"escalation_reason": reason_enum.value, "urgency": level.value},
    )


__all__ = ["escalate_to_manager"]
