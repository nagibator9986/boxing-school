"""Ручной tool-loop поверх ``generate_content``.

Автоматический function calling SDK отключён специально: он теряет id
параллельных вызовов и thought signatures, а без них Gemini 3 деградирует
на следующем ходу.

Инварианты цикла:

* ``candidate.content`` уходит в ``contents`` ЦЕЛИКОМ — вместе с
  ``thought_signature`` частей;
* результаты всех вызовов возвращаются ОДНИМ ``Content`` с ``role="user"``;
* ``Part`` собирается вручную через ``types.FunctionResponse(id=...)`` —
  ``Part.from_function_response()`` не принимает ``id`` (проверено
  интроспекцией: сигнатура ``(*, name, response, parts)``);
* больше ``max_loops`` витков — :class:`app.types.LLMToolLoopError`.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Final, Sequence

from google.genai import types

from app.logging_conf import get_logger
from app.types import (
    LLMBlockedError,
    LLMError,
    LLMResponse,
    LLMTimeoutError,
    LLMToolLoopError,
    LLMUsage,
    ToolExecutor,
    ToolInvocation,
    ToolResult,
    ToolStatus,
)

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from google import genai

log = get_logger(__name__)

#: finish_reason, при которых текста в ответе быть не может.
_BAD_FINISH: frozenset[str] = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "PROHIBITED_CONTENT",
        "SPII",
        "BLOCKLIST",
        "IMAGE_SAFETY",
    }
)

#: Модель не смогла собрать вызов функции. Это НЕ блокировка: рядом с SAFETY
#: такая осечка стоила бы диалогу эскалации на человека и паузы бота, хотя
#: лечится она обычным повтором — при другом сэмплинге вызов собирается.
#: Поэтому отдельный retryable-код, который ``_with_retry`` повторяет.
_MALFORMED_FINISH: Final[str] = "MALFORMED_FUNCTION_CALL"
MALFORMED_CALL_CODE: Final[str] = "llm_malformed_call"


def _enum_value(raw: Any) -> str | None:
    """Значение enum SDK как строка. В разных версиях это str-enum или обычный enum."""
    if raw is None:
        return None
    return str(getattr(raw, "value", raw))


def content_to_dict(content: "types.Content") -> dict[str, Any]:
    """``Content`` → JSON-совместимый dict (формат истории вне ``app.llm``)."""
    return content.model_dump(mode="json", exclude_none=True)


def dict_to_content(raw: dict[str, Any]) -> "types.Content":
    """Обратное преобразование. Битые записи истории отбрасывать нельзя молча —
    ``ValidationError`` поднимается наверх, чтобы диалог не «поехал»."""
    return types.Content.model_validate(raw)


def trim_history(contents: list[dict[str, Any]], *, max_turns: int) -> list[dict[str, Any]]:
    """Режет историю ТОЛЬКО по границе завершённого tool-цикла.

    ``function_call`` без парного ``function_response`` в истории — гарантированная
    ошибка запроса, поэтому обрезка ищет ближайшую снизу безопасную границу.
    """
    if max_turns <= 0 or len(contents) <= max_turns:
        return list(contents)

    start = len(contents) - max_turns
    while start < len(contents):
        item = contents[start]
        if item.get("role") == "user" and not _has_function_response(item):
            break
        start += 1
    if start >= len(contents):
        # Безопасной границы не нашлось — лучше отдать хвост целиком, чем битый запрос.
        return list(contents)
    return list(contents[start:])


def _has_function_response(item: dict[str, Any]) -> bool:
    """Есть ли в записи истории ответ инструмента."""
    return any("function_response" in part for part in item.get("parts", []) or [])


def _function_calls(content: "types.Content | None") -> list["types.FunctionCall"]:
    """Все ``function_call`` кандидата в порядке появления."""
    if content is None or not content.parts:
        return []
    return [part.function_call for part in content.parts if part.function_call is not None]


def safe_text(response: "types.GenerateContentResponse") -> str | None:
    """Единственный разрешённый способ достать текст из ответа.

    ``response.text`` напрямую не читается нигде: он бросает исключение на
    заблокированном ответе и молча склеивает мысли модели с ответом.
    """
    feedback = getattr(response, "prompt_feedback", None)
    block_reason = _enum_value(getattr(feedback, "block_reason", None)) if feedback else None
    if block_reason and block_reason != "BLOCKED_REASON_UNSPECIFIED":
        raise LLMBlockedError(f"Запрос заблокирован моделью: {block_reason}")

    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None

    candidate = candidates[0]
    finish = _enum_value(getattr(candidate, "finish_reason", None))
    if finish == _MALFORMED_FINISH:
        raise LLMError(
            "Модель испортила вызов функции: finish_reason=MALFORMED_FUNCTION_CALL",
            code=MALFORMED_CALL_CODE,
            retryable=True,
        )
    if finish in _BAD_FINISH:
        raise LLMBlockedError(f"Ответ заблокирован моделью: finish_reason={finish}")

    content = getattr(candidate, "content", None)
    if content is None or not content.parts:
        return None

    chunks = [
        part.text
        for part in content.parts
        # thought=True — «мысли» модели, клиенту они не показываются.
        if part.text and not getattr(part, "thought", False)
    ]
    text = "".join(chunks).strip()
    return text or None


async def run_tool_loop(
    *,
    client: "genai.Client",
    model: str,
    contents: list["types.Content"],
    config: "types.GenerateContentConfig",
    executor: ToolExecutor,
    max_loops: int,
    timeout_ms: int,
    deadline: float | None = None,
) -> LLMResponse:
    """Ручной цикл «модель → инструменты → модель».

    ``timeout_ms`` — потолок ОДНОГО обращения к модели, ``deadline`` — момент
    (шкала :func:`time.monotonic`), после которого хода больше нет. Без второго
    бюджет витков складывался бы: ``(max_loops + 1) × timeout_ms`` переживает и
    таймаут задачи воркера, и TTL лока диалога, поэтому ход обрывали бы снаружи
    — посреди вызова, без заглушки клиенту и с откатом транзакции.

    Возвращает :class:`app.types.LLMResponse` с полной историей хода в
    JSON-форме, вызовами инструментов и потреблением по каждому обращению.
    """
    usages: list[LLMUsage] = []
    invocations: list[ToolInvocation] = []
    timeout_s = max(timeout_ms, 1) / 1000.0

    for loop_index in range(max_loops + 1):
        call_timeout_s = timeout_s
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise LLMTimeoutError(
                    f"Бюджет хода исчерпан на витке {loop_index}: модель не успела ответить"
                )
            # Инструменты уже съели часть бюджета — оставшееся и есть потолок вызова.
            call_timeout_s = min(timeout_s, left)

        started = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model, contents=contents, config=config
                ),
                timeout=call_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise LLMTimeoutError(
                f"Модель не ответила за {int(call_timeout_s * 1000)} мс"
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        from app.llm.usage import usage_from_response  # локальный импорт: избегаем цикла

        usages.append(usage_from_response(response, model=model, latency_ms=latency_ms))

        candidates = getattr(response, "candidates", None) or []
        candidate_content = getattr(candidates[0], "content", None) if candidates else None
        calls = _function_calls(candidate_content)

        if not calls:
            text = safe_text(response)
            if candidate_content is not None:
                contents.append(candidate_content)
            finish = _enum_value(getattr(candidates[0], "finish_reason", None)) if candidates else None
            return LLMResponse(
                text=text,
                finish_reason=finish,
                history=[content_to_dict(item) for item in contents],
                invocations=tuple(invocations),
                usage=tuple(usages),
                model_used=model,
                loops=loop_index,
            )

        if loop_index >= max_loops:
            raise LLMToolLoopError(
                f"Модель не остановилась за {max_loops} витков tool-loop"
            )

        # Ответ модели уходит в историю ЦЕЛИКОМ: без thought signatures
        # следующий ход теряет контекст рассуждения.
        contents.append(candidate_content)  # type: ignore[arg-type]

        results = await _execute_calls(calls, executor, loop_index, invocations)
        contents.append(types.Content(role="user", parts=results))

    raise LLMToolLoopError("Недостижимая ветка tool-loop")  # pragma: no cover


async def _execute_calls(
    calls: Sequence["types.FunctionCall"],
    executor: ToolExecutor,
    loop_index: int,
    sink: list[ToolInvocation],
) -> list["types.Part"]:
    """Выполняет вызовы одного витка параллельно и собирает ``Part`` с ответами."""
    async def one(call: "types.FunctionCall") -> tuple["types.FunctionCall", ToolResult, int]:
        started = time.perf_counter()
        name = call.name or ""
        args = dict(call.args or {})
        try:
            result = await executor(name, args)
        except Exception as exc:  # исполнитель обязан не падать, но подстрахуемся
            log.warning("tool_executor_failed", tool=name, error=type(exc).__name__)
            result = ToolResult.failure(f"{name}: {type(exc).__name__}")
        return call, result, int((time.perf_counter() - started) * 1000)

    done = await asyncio.gather(*(one(call) for call in calls))

    parts: list[types.Part] = []
    for call, result, latency_ms in done:
        sink.append(
            ToolInvocation(
                call_id=call.id,
                name=call.name or "",
                args=dict(call.args or {}),
                result=result,
                latency_ms=latency_ms,
                loop_index=loop_index,
            )
        )
        # Part.from_function_response() не принимает id — при параллельных вызовах
        # модель не сможет сопоставить ответ с запросом. Собираем вручную.
        parts.append(
            types.Part(
                function_response=types.FunctionResponse(
                    id=call.id,
                    name=call.name,
                    response=result.to_llm_payload(),
                )
            )
        )
        if result.status is ToolStatus.ERROR:
            log.warning("tool_failed", tool=call.name, error=result.error)
    return parts


__all__ = [
    "MALFORMED_CALL_CODE",
    "content_to_dict",
    "dict_to_content",
    "run_tool_loop",
    "safe_text",
    "trim_history",
]
