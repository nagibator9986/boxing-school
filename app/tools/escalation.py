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

import re
from typing import Final

from app.types import (
    TOOL_ESCALATION_REASONS,
    EscalationReason,
    IntentHint,
    Language,
    ManagerCard,
    ManagerCardKind,
    PauseReason,
    RenderHint,
    ToolContext,
    ToolResult,
    Urgency,
)
from app.kb.agreement import chosen_option, question_sentence, split_agreement
from app.tools.booking import dialog_link, format_local_dt, render_card_text

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

#: Пустое поле карточки: строку с ним карточка не печатает.
_NO_LINK: Final[str] = "—"

#: Запасная длительность паузы, если в policies.yaml значение не задано.
_DEFAULT_PAUSE_MINUTES: Final[int] = 60


def _client_asked_for_human(ctx: ToolContext) -> bool:
    """Просил ли клиент живого человека своими словами — сейчас или раньше в разговоре.

    Пометка согласия несёт текст бота: «Время подберёт администратор. Записать?» и
    ответ «Да» — не просьба позвать человека. Поэтому считаются только слова самого
    клиента, а интенты текущей реплики — только если это не согласие.
    """
    current_is_consent = bool(ctx.client_texts) and split_agreement(ctx.client_texts[-1])[1] is not None
    if IntentHint.MANAGER in ctx.intents and not current_is_consent:
        return True
    words = [word for word in ctx.kb.lexicon.intents.get(IntentHint.MANAGER, []) if word]
    own = [split_agreement(text)[0].lower() for text in ctx.client_texts]
    return any(word in text for text in own for word in words)


#: Вопрос бота о передаче разговора человеку.
_HANDOVER_QUESTION_MARKERS: Final[tuple[str, ...]] = (
    "переда", "администратор", "менеджер", "әкімші", "жеткіз",
)


def _agreed_to_a_handover(ctx: ToolContext) -> bool:
    """Согласился ли клиент именно на передачу вопроса администратору."""
    if not ctx.client_texts:
        return False
    _own, proposal = split_agreement(ctx.client_texts[-1])
    # Только сам вопрос: «…решает администратор. Передать ему?» — передача, а «Время
    # подберёт администратор. Записать?» — предложение записи.
    asked = question_sentence(proposal).lower() if proposal is not None else ""
    return any(marker in asked for marker in _HANDOVER_QUESTION_MARKERS)


#: Причины «не справился сам». На ответ клиента на вопрос бота они не годятся.
_STUCK_REASONS: Final[frozenset[EscalationReason]] = frozenset(
    {EscalationReason.NO_DATA, EscalationReason.REPEATED_MISS}
)
#: Голая цифра — ответ на список бота, даже если код этот список не узнал.
_BARE_REPLY_RE: Final[re.Pattern[str]] = re.compile(r"^\s*\d{1,2}\s*[.)]?\s*$")


def _answered_the_bot(ctx: ToolContext) -> bool:
    """Последняя реплика клиента — ответ на вопрос бота: «да», цифра варианта."""
    if not ctx.client_texts:
        return False
    latest = ctx.client_texts[-1]
    own, proposal = split_agreement(latest)
    return proposal is not None or chosen_option(latest) is not None or bool(_BARE_REPLY_RE.match(own))


#: Начало вопроса: «как», «сколько», «можно», «есть ли», «подскажите»; казахские
#: вопросительные слова и частицы.
_QUESTION_RE: Final[re.Pattern[str]] = re.compile(
    r"^\W*(?:а\s+|и\s+)?(?:как|сколько|где|когда|можно|что|чем|какой|какая|какие|какое|каких|почему|зачем"
    r"|есть\s+ли|нужн\w*\s+ли|подскажите|скажите|расскажите|объясните|у\s+вас)(?![а-яё])"
    r"|(?<![а-яәөүұқғңһі])(?:қанша|қайда|қашан|қалай|неге|ма|ме|ба|бе|па|пе)(?![а-яәөүұқғңһі])",
    re.IGNORECASE,
)


def _just_telling(ctx: ToolContext) -> bool:
    """Клиент рассказывает о себе, а не спрашивает: «Он ходил в том году», «Вчера был у вас»."""
    if not ctx.client_texts:
        return False
    own = split_agreement(ctx.client_texts[-1])[0].strip()
    return bool(own) and "?" not in own and not _QUESTION_RE.search(own)


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

    # «Клиент просит человека» — это слова клиента, а не вывод модели. Живое
    # воспроизведение 11.09.2026: на «Да» после предложения записи модель звала
    # администратора «по просьбе клиента», и запись обрывалась на полпути.
    if reason_enum is EscalationReason.USER_REQUEST and not (
        _client_asked_for_human(ctx) or _agreed_to_a_handover(ctx)
    ):
        return ToolResult.invalid_input(
            "клиент не просил живого человека: не передавай разговор администратору. "
            "Продолжай сам — если ответ клиента непонятен, коротко переспроси; если в базе "
            "нет нужных данных, используй reason=no_data"
        )
    # «Да» на предложение записи — не вопрос без данных. Модель, не найдя, как
    # продолжить, передавала такой разговор администратору с reason=no_data.
    # Скриншот владельца 12.09.2026: клиент дважды ответил «2» на список секций, и модель
    # сдалась с reason=repeated_miss. «Не справился» — не причина отдавать запись.
    if (
        reason_enum in _STUCK_REASONS
        and _answered_the_bot(ctx)
        and not _agreed_to_a_handover(ctx)
        and not _client_asked_for_human(ctx)
    ):
        return ToolResult.invalid_input(
            "клиент ответил на твой же вопрос — согласием или номером варианта. Это не вопрос "
            "без данных: продолжай запись с того, что он выбрал, или коротко переспроси, "
            "перечислив варианты нумерованным списком"
        )

    # Живой прогон 19.10.2026: на «Он ходил в том году» модель передала разговор
    # администратору «за отсутствием данных» — а клиент ничего не спрашивал. Владелец
    # 15.10.2026: «через каждый диалог пишется: здесь лучше ответит администратор».
    if (
        reason_enum in _STUCK_REASONS
        and _just_telling(ctx)
        and not _agreed_to_a_handover(ctx)
        and not _client_asked_for_human(ctx)
    ):
        return ToolResult.invalid_input(
            "клиент ничего не спросил — он рассказывает о себе («ходил в том году», «вчера был "
            "у вас»). Не передавай разговор администратору: ответь по-человечески одной фразой и "
            "задай один вопрос, который ведёт дальше — к записи или абонементу. Если клиент о "
            "чём-то просит, а данных нет, спроси, передать ли вопрос администратору"
        )

    summary = " ".join(str(question_summary or "").split())[:200].strip()
    if not summary:
        return ToolResult.invalid_input("question_summary пуст: администратору нечего передать")

    pause_minutes = int(getattr(kb.policies, "escalation_pause_minutes", _DEFAULT_PAUSE_MINUTES) or _DEFAULT_PAUSE_MINUTES)
    # Администратор уже получил вопрос этого диалога и ещё не ответил: вторая карточка
    # и второе «Здесь лучше ответит администратор…» ничего не ускоряют. Живой диалог
    # 13.10.2026: «Вчера на 9.00 договорились», «Или 18.00», «Не понятно» — три передачи
    # подряд. Жалоба и здоровье уходят всегда: это новая, срочная причина.
    if reason_key not in _ALWAYS_HIGH and await ctx.services.escalation_open(within_minutes=pause_minutes):
        # Диалог 16.10.2026: на «Да, конечно» о рассказе про абонементы модель дописала
        # к прайсу «Администратор уже подключается…» — согласие не вопрос для человека.
        return ToolResult.invalid_input(
            "администратор уже получил вопрос из этого диалога и ещё не ответил — повторно не "
            "передавай. Ответь клиенту сам по данным инструментов (расписание, залы, цены, запись). "
            "Об администраторе пиши, только если клиент спросил то, чего в данных нет: тогда одной "
            "фразой — вопрос уже у администратора, он ответит здесь, без обещания сроков"
        )

    try:
        level = Urgency(str(urgency or "normal").strip().lower())
    except ValueError:
        level = Urgency.NORMAL
    if reason_key in _ALWAYS_HIGH:
        level = Urgency.HIGH

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
            "dialog": dialog_link(ctx) or _NO_LINK,
        },
    )
    # Без адреса сервиса ссылки нет — и строки «Переписка: —» тоже.
    card_text = "\n".join(line for line in card_text.splitlines() if not line.endswith(f": {_NO_LINK}"))
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
