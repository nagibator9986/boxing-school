"""Диалог и его память: строка ``conversation``, история модели, обрезка контекста.

Ключевое требование к истории Gemini — **сохранять ответ модели целиком**.
В ``candidate.content`` кроме текста лежат ``thought_signature`` и служебные
части; если сохранить только текст, следующий ход модели теряет собственные
рассуждения и начинает противоречить сам себе, а параллельные вызовы
инструментов вообще перестают собираться.

Второе требование — **резать историю только по границам целых витков**. Пара
``function_call`` (в ответе модели) и ``function_response`` (в следующем
сообщении роли ``user``) неразрывна: обрезка между ними делает запрос
невалидным, и Gemini отвечает ошибкой на весь диалог. Поэтому точками разреза
считаются только начала настоящих реплик клиента — контенты роли ``user``,
в которых нет ни одного ``function_response``.

Бюджет считается дважды: по количеству витков (``llm_history_turns``) и по
объёму символов (:data:`HISTORY_CHAR_BUDGET`) — длинная переписка с адресами и
прайсами упирается в токены раньше, чем в число реплик.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Sequence
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.outbound import window_expires_at
from app.logging_conf import get_logger
from app.storage import repo_conversation, repo_message
from app.storage.models import Conversation, Message
from app.types import (
    Author,
    ChannelKind,
    InboundMessage,
    Language,
    LLMRequest,
    ToolResult,
)

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from app.llm.client import LLMClient

__all__ = [
    "APPROX_CHARS_PER_TOKEN",
    "HISTORY_CHAR_BUDGET",
    "SUMMARY_MARKER",
    "ensure_conversation",
    "load_history",
    "maybe_summarize",
    "record_inbound",
    "save_turn",
    "service_window_until",
    "summary_content",
    "trim_history",
]

_log = get_logger(__name__)

#: Сколько символов кириллицы примерно приходится на один токен.
APPROX_CHARS_PER_TOKEN: Final[int] = 3
#: Потолок объёма истории в символах (~8 тысяч токенов).
HISTORY_CHAR_BUDGET: Final[int] = 24_000
#: Сколько элементов истории приходится на один виток в худшем случае:
#: реплика клиента, вызов инструмента, ответ инструмента, ответ модели.
_ELEMENTS_PER_TURN: Final[int] = 6

#: Пометка служебного элемента истории с резюме отрезанного хвоста.
SUMMARY_MARKER: Final[str] = "[Сводка предыдущей части переписки]"

#: Как часто пересчитывать резюме — каждое N-е входящее клиента. Резюме
#: описывает ХВОСТ, который меняется медленно, а вызов модели платный: без
#: этого шага каждое сообщение длинного диалога оплачивало бы лишний generate().
_SUMMARY_REFRESH_EVERY: Final[int] = 5

#: Инструкция «тихого» вызова резюме. Фактов о школе не содержит — только формат.
_SUMMARY_INSTRUCTION: Final[str] = (
    "Ты сжимаешь переписку администратора детской спортивной школы с родителем. "
    "Верни 3-5 коротких предложений на языке переписки: что спрашивал родитель, "
    "что уже сообщено, имя и возраст ребёнка, выбранный зал, договорённости. "
    "Не добавляй ничего, чего не было в переписке. Не выдумывай цены, адреса и время."
)


# --------------------------------------------------------------------------- #
# Диалог
# --------------------------------------------------------------------------- #
async def ensure_conversation(
    session: AsyncSession, inbound: InboundMessage, *, kb_hash: str
) -> Conversation:
    """Возвращает диалог входящего, создавая его при первом сообщении.

    Версия базы знаний фиксируется только у нового диалога: по ней потом видно,
    на каких данных бот отвечал, если родитель придёт с претензией.
    """
    conversation, created = await repo_conversation.get_or_create(session, inbound=inbound)
    if created and kb_hash:
        await repo_conversation.set_kb_hash_at_start(session, conversation.id, kb_hash)
    return conversation


async def record_inbound(
    session: AsyncSession, conv: Conversation, inbound: InboundMessage, *, author: Author
) -> UUID:
    """Сохраняет входящее и двигает счётчики диалога.

    ``author`` приходит снаружи: слой каналов знает про эхо, но не знает про наш
    outbox, поэтому окончательный вердикт «клиент это или оператор» ставит
    пайплайн. Сообщение записывается всегда — даже на паузе и даже если бот
    отвечать не будет.
    """
    message = inbound if inbound.author is author else inbound.model_copy(update={"author": author})
    message_id = await repo_message.add_inbound(session, conv.id, message)

    if author is Author.CLIENT:
        await repo_conversation.bump_counters(session, conv.id, msg_in=1)
        await repo_conversation.touch_inbound(
            session,
            conv.id,
            inbound.received_at,
            window_until=service_window_until(inbound.channel, inbound.received_at),
        )
    return message_id


def service_window_until(channel: ChannelKind, last_inbound_at: datetime) -> datetime | None:
    """Instagram ``+7`` дней; личный WhatsApp — ``None`` (окно не применяется)."""
    return window_expires_at(channel, last_inbound_at)


# --------------------------------------------------------------------------- #
# История модели
# --------------------------------------------------------------------------- #
async def load_history(
    session: AsyncSession, conv: Conversation, *, max_turns: int
) -> list[dict[str, Any]]:
    """История Gemini последних ``max_turns`` витков, обрезанная по границам витков.

    Первым элементом идёт резюме отрезанного хвоста (если оно есть): ради этого
    :func:`maybe_summarize` и платит за отдельный вызов модели. Без подмешивания
    резюме диалог терял бы всё, что было сказано до обрезки — имя ребёнка,
    выбранный зал, договорённости, — и резюме было бы деньгами в трубу.
    """
    if max_turns <= 0:
        return []
    raw = await repo_message.load_history(
        session, conv.id, max_turns=max_turns * _ELEMENTS_PER_TURN
    )
    history = trim_history(raw, max_turns=max_turns, max_chars=HISTORY_CHAR_BUDGET)
    summary = (conv.summary or "").strip()
    if summary:
        history.insert(0, summary_content(summary))
    return history


def summary_content(summary: str) -> dict[str, Any]:
    """Резюме как элемент истории: реплика ``user`` с явной пометкой служебности.

    Роль ``user`` выбрана намеренно: это законная точка разреза истории
    (``function_response`` внутри нет), поэтому последующая обрезка не разорвёт
    пару вызов/ответ инструмента.
    """
    return {"role": "user", "parts": [{"text": f"{SUMMARY_MARKER}\n{summary}"}]}


async def save_turn(
    session: AsyncSession, conv: Conversation, contents: Sequence[dict[str, Any]]
) -> None:
    """Дописывает ход в историю модели ЦЕЛИКОМ, включая thought signatures.

    Сюда идут дампы ``types.Content`` как есть: реплика клиента, ответ модели с
    ``function_call``, ответ инструмента, финальный текст. Ничего не
    выбрасывается — иначе следующий ход соберётся неверно.
    """
    if not contents:
        return
    await repo_message.add_llm_turn(session, conv.id, list(contents))


def trim_history(
    contents: Sequence[dict[str, Any]], *, max_turns: int, max_chars: int = HISTORY_CHAR_BUDGET
) -> list[dict[str, Any]]:
    """Режет историю ТОЛЬКО по границе завершённого витка.

    Точка разреза — начало настоящей реплики клиента (роль ``user`` без
    ``function_response``). Пара ``function_call`` / ``function_response`` при
    таком разрезе либо остаётся целиком, либо целиком отбрасывается.
    Дополнительно соблюдается бюджет символов: старые витки уходят первыми,
    последний виток остаётся всегда.
    """
    items = [item for item in contents if isinstance(item, dict)]
    if not items:
        return []

    starts = [index for index, item in enumerate(items) if _is_turn_start(item)]
    if not starts:
        # Истории без реплик клиента не бывает; безопаснее начать с чистого листа,
        # чем отправить в модель обрывок с непарным function_call.
        return []

    if max_turns > 0 and len(starts) > max_turns:
        starts = starts[-max_turns:]

    cut = starts[0]
    while len(starts) > 1 and _size(items[cut:]) > max_chars:
        starts = starts[1:]
        cut = starts[0]
    return items[cut:]


async def maybe_summarize(
    session: AsyncSession,
    conv: Conversation,
    llm: "LLMClient",
    *,
    max_turns: int,
) -> str | None:
    """Резюмирует хвост при обрезке истории. ``None`` — резюмировать нечего.

    Вызов «тихий»: без инструментов, без истории, результат клиенту не
    показывается. Любая ошибка модели гасится — потеря резюме не должна ронять
    ответ на текущее сообщение.

    Пересчёт идёт не каждое сообщение, а каждое ``_SUMMARY_REFRESH_EVERY``-е:
    готовое резюме уже подмешивается в контекст (:func:`load_history`), а
    отрезанный хвост между двумя соседними сообщениями почти не меняется —
    платить за generate() на каждую реплику не за что.
    """
    if max_turns <= 0:
        return None

    existing = (conv.summary or "").strip()
    if existing and int(conv.msg_in_count or 0) % _SUMMARY_REFRESH_EVERY != 0:
        return existing

    raw = await repo_message.load_history(
        session, conv.id, max_turns=max_turns * _ELEMENTS_PER_TURN * 3
    )
    items = [item for item in raw if isinstance(item, dict)]
    starts = [index for index, item in enumerate(items) if _is_turn_start(item)]
    if len(starts) <= max_turns:
        return None

    dropped = items[: starts[-max_turns]]
    transcript = _transcript(dropped)
    if not transcript.strip():
        return None

    request = LLMRequest(
        system_instruction=_SUMMARY_INSTRUCTION,
        history=[],
        user_text=transcript,
        dynamic_note="",
        tool_specs=(),
        tool_mode="NONE",
        lang=Language(conv.lang) if conv.lang else Language.RU,
        correlation_id=f"summary:{conv.id}",
    )
    try:
        response = await llm.generate(request, _no_tools)
    except Exception as exc:
        _log.warning("summary_failed", error=str(exc), conv_key=conv.conv_key)
        return None

    summary = (response.text or "").strip()
    if not summary:
        return None
    await repo_conversation.set_summary(session, conv.id, summary[:2000])
    _log.info("history_summarized", turns=len(starts), chars=len(summary))
    return summary


# --------------------------------------------------------------------------- #
# Внутреннее
# --------------------------------------------------------------------------- #
async def _no_tools(name: str, args: dict[str, Any]) -> ToolResult:
    """Исполнитель-заглушка: у «тихого» вызова инструментов нет."""
    return ToolResult.invalid_input(f"инструменты недоступны в служебном вызове ({name})")


def _is_turn_start(content: dict[str, Any]) -> bool:
    """Настоящая реплика клиента: роль ``user`` и ни одного ``function_response``."""
    if str(content.get("role") or "user") != "user":
        return False
    parts = content.get("parts")
    if not isinstance(parts, list):
        return True
    return not any(isinstance(part, dict) and part.get("function_response") for part in parts)


def _size(items: Sequence[dict[str, Any]]) -> int:
    """Объём куска истории в символах JSON-дампа."""
    try:
        return len(json.dumps(list(items), ensure_ascii=False))
    except (TypeError, ValueError):  # pragma: no cover - несериализуемая история
        return sum(len(str(item)) for item in items)


def _transcript(items: Sequence[dict[str, Any]]) -> str:
    """Текстовая выжимка куска истории для резюмирования."""
    lines: list[str] = []
    for item in items:
        role = str(item.get("role") or "user")
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        texts = [
            str(part.get("text")).strip()
            for part in parts
            if isinstance(part, dict) and part.get("text")
        ]
        if not texts:
            continue
        speaker = "Родитель" if role == "user" else "Бот"
        lines.append(f"{speaker}: {' '.join(texts)}")
    return "\n".join(lines[-80:])


async def bot_turns(db: AsyncSession, conv: Conversation) -> int:
    """Сколько раз бот уже отвечал в этом диалоге."""
    stmt = (
        sa.select(sa.func.count())
        .select_from(Message)
        .where(Message.conversation_id == conv.id, Message.author == Author.BOT.value)
    )
    result = await db.execute(stmt)
    return int(result.scalar_one() or 0)


async def is_first_turn(db: AsyncSession, conv: Conversation) -> bool:
    """Первое ли это сообщение диалога (бот ещё ничего не отвечал).

    Нужна, чтобы поздороваться шаблоном ровно один раз: на втором «здравствуйте»
    посреди разговора меню выглядело бы как сброс диалога.
    """
    stmt = (
        sa.select(sa.func.count())
        .select_from(Message)
        .where(Message.conversation_id == conv.id, Message.author == Author.BOT.value)
    )
    result = await db.execute(stmt)
    return int(result.scalar_one() or 0) == 0
