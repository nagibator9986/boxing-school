"""Ручной tool-loop поверх ``generate_content``.

Автоматический function calling SDK отключён специально: он теряет id
параллельных вызовов и thought signatures. Без id модель не сопоставит ответ с
запросом при параллельном вызове; без подписи Gemini 3 деградирует на СЛЕДУЮЩЕМ
ходу — а такую поломку по логам почти невозможно найти.

Сети здесь нет: клиент SDK подменён двойником, который отдаёт заранее собранные
``GenerateContentResponse``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Sequence

import pytest
from google.genai import types

from app.llm.tool_runner import (
    MALFORMED_CALL_CODE,
    content_to_dict,
    dict_to_content,
    run_tool_loop,
    safe_text,
    trim_history,
)
from app.types import (
    LLMBlockedError,
    LLMError,
    LLMTimeoutError,
    LLMToolLoopError,
    ToolResult,
    ToolStatus,
)


# --------------------------------------------------------------------------- #
# Двойник SDK
# --------------------------------------------------------------------------- #
def model_turn(*parts: types.Part, finish: str = "STOP") -> types.GenerateContentResponse:
    """Ответ модели из готовых частей."""
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=list(parts)), finish_reason=finish
            )
        ]
    )


def call_part(name: str, args: dict[str, Any], *, call_id: str, signature: bytes | None = None):
    """Часть с вызовом инструмента; подпись имитирует настоящего кандидата."""
    return types.Part(
        function_call=types.FunctionCall(id=call_id, name=name, args=args),
        thought_signature=signature,
    )


class FakeModels:
    """``client.aio.models`` с заранее заданной лентой ответов."""

    def __init__(self, responses: Sequence[types.GenerateContentResponse], delay: float = 0.0):
        self._responses = list(responses)
        self._delay = delay
        self.calls: list[list[types.Content]] = []

    async def generate_content(self, *, model, contents, config):
        """Отдаёт следующий ответ ленты, запомнив присланные ``contents``."""
        self.calls.append([item.model_copy(deep=True) for item in contents])
        if self._delay:
            await asyncio.sleep(self._delay)
        if not self._responses:
            raise AssertionError("двойник SDK исчерпан: тест ждёт лишний вызов модели")
        return self._responses.pop(0)


class FakeClient:
    """Минимальный двойник ``genai.Client``: только то, что зовёт tool-loop."""

    def __init__(self, responses: Sequence[types.GenerateContentResponse], delay: float = 0.0):
        self.models = FakeModels(responses, delay)
        self.aio = self

    @property
    def calls(self) -> list[list[types.Content]]:
        """Сохранённые ``contents`` каждого обращения к модели."""
        return self.models.calls


async def run(
    responses, executor, *, max_loops: int = 5, timeout_ms: int = 5000, contents=None
):
    """Запуск цикла с общими для тестов умолчаниями."""
    client = FakeClient(responses)
    result = await run_tool_loop(
        client=client,
        model="fake-model",
        contents=contents if contents is not None else [
            types.Content(role="user", parts=[types.Part(text="Сколько стоит?")])
        ],
        config=types.GenerateContentConfig(),
        executor=executor,
        max_loops=max_loops,
        timeout_ms=timeout_ms,
    )
    return result, client


async def ok_executor(name: str, args: dict[str, Any]) -> ToolResult:
    """Исполнитель-заглушка: всё успешно."""
    return ToolResult.success(data={"tool": name, "args": args})


# --------------------------------------------------------------------------- #
# Подписи мысли
# --------------------------------------------------------------------------- #
async def test_thought_signature_survives_in_history() -> None:
    """Ответ модели уходит в историю ЦЕЛИКОМ — вместе с ``thought_signature``.

    Потеря подписи не ломает текущий ответ, она ломает следующий ход.
    """
    responses = [
        model_turn(call_part("get_gyms", {"scope": "city"}, call_id="c1", signature=b"sig-tool")),
        model_turn(types.Part(text="Готово", thought_signature=b"sig-answer")),
    ]

    result, _ = await run(responses, ok_executor)

    signatures = [
        part.get("thought_signature")
        for item in result.history
        for part in item.get("parts", [])
        if part.get("thought_signature")
    ]
    assert len(signatures) == 2, f"подписи потеряны: {result.history}"


async def test_tool_results_are_sent_back_with_call_id() -> None:
    """``FunctionResponse`` собирается вручную: ``Part.from_function_response`` не берёт id."""
    responses = [
        model_turn(call_part("get_gyms", {"scope": "city"}, call_id="call-42")),
        model_turn(types.Part(text="Готово")),
    ]

    result, client = await run(responses, ok_executor)

    # Второй запрос к модели обязан нести ответ инструмента с тем же id.
    second_request = client.calls[1]
    responses_sent = [
        part.function_response
        for item in second_request
        for part in (item.parts or [])
        if part.function_response is not None
    ]
    assert len(responses_sent) == 1
    assert responses_sent[0].id == "call-42"
    assert responses_sent[0].name == "get_gyms"
    assert result.invocations[0].call_id == "call-42"


async def test_parallel_calls_keep_their_ids_and_run_together() -> None:
    """Параллельный виток: три вызова, три ответа, id не перепутаны."""
    started: list[str] = []

    async def slow_executor(name: str, args: dict[str, Any]) -> ToolResult:
        started.append(name)
        await asyncio.sleep(0.05)
        return ToolResult.success(data={"tool": name})

    responses = [
        model_turn(
            call_part("get_gyms", {"scope": "city"}, call_id="a"),
            call_part("calculate_price", {"plan": "standard"}, call_id="b"),
            call_part("get_kb_fact", {"topic": "trial"}, call_id="c"),
        ),
        model_turn(types.Part(text="Готово")),
    ]

    loop_started = asyncio.get_running_loop().time()
    result, client = await run(responses, slow_executor)
    elapsed = asyncio.get_running_loop().time() - loop_started

    assert [inv.call_id for inv in result.invocations] == ["a", "b", "c"]
    assert [inv.name for inv in result.invocations] == [
        "get_gyms",
        "calculate_price",
        "get_kb_fact",
    ]
    # Последовательное выполнение заняло бы 0.15 с — вызовы обязаны идти вместе.
    assert elapsed < 0.12, f"вызовы выполнены последовательно: {elapsed:.3f} с"

    # Результаты всех вызовов возвращаются ОДНИМ Content с role="user".
    tool_content = client.calls[1][-1]
    assert tool_content.role == "user"
    assert [part.function_response.id for part in tool_content.parts] == ["a", "b", "c"]


# --------------------------------------------------------------------------- #
# Отказы инструментов
# --------------------------------------------------------------------------- #
async def test_tool_exception_does_not_break_the_turn() -> None:
    """Упавший инструмент превращается в ошибку в ответе, а не роняет запрос."""

    async def exploding(name: str, args: dict[str, Any]) -> ToolResult:
        raise RuntimeError("инструмент упал")

    responses = [
        model_turn(call_part("get_schedule", {}, call_id="x")),
        model_turn(types.Part(text="Расписание уточнит администратор")),
    ]

    result, _ = await run(responses, exploding)

    assert result.text == "Расписание уточнит администратор"
    assert result.invocations[0].result.status is ToolStatus.ERROR
    assert result.invocations[0].result.ok is False


async def test_mixed_success_and_failure_in_one_loop() -> None:
    """Одна ошибка из трёх не отменяет остальные результаты витка."""

    async def flaky(name: str, args: dict[str, Any]) -> ToolResult:
        if name == "get_schedule":
            raise RuntimeError("нет расписания")
        return ToolResult.success(data={"tool": name})

    responses = [
        model_turn(
            call_part("get_gyms", {}, call_id="a"),
            call_part("get_schedule", {}, call_id="b"),
            call_part("calculate_price", {}, call_id="c"),
        ),
        model_turn(types.Part(text="Готово")),
    ]

    result, _ = await run(responses, flaky)

    statuses = {inv.name: inv.result.status for inv in result.invocations}
    assert statuses["get_gyms"] is ToolStatus.OK
    assert statuses["get_schedule"] is ToolStatus.ERROR
    assert statuses["calculate_price"] is ToolStatus.OK


# --------------------------------------------------------------------------- #
# Пределы цикла
# --------------------------------------------------------------------------- #
async def test_loop_limit_raises_instead_of_spinning() -> None:
    """Модель не остановилась за отведённые витки — цикл обязан оборваться."""
    responses = [
        model_turn(call_part("get_gyms", {}, call_id=f"c{i}")) for i in range(5)
    ]

    with pytest.raises(LLMToolLoopError):
        await run(responses, ok_executor, max_loops=2)


async def test_loop_limit_counts_calls_not_responses() -> None:
    """Ровно ``max_loops`` витков инструментов допустимы, следующий — отказ."""
    responses = [
        model_turn(call_part("get_gyms", {}, call_id="c1")),
        model_turn(call_part("calculate_price", {}, call_id="c2")),
        model_turn(types.Part(text="Готово")),
    ]

    result, _ = await run(responses, ok_executor, max_loops=2)

    assert result.text == "Готово"
    assert result.loops == 2
    assert len(result.invocations) == 2


async def test_timeout_is_reported_as_llm_timeout() -> None:
    """Модель не ответила за отведённое время — своё исключение, а не asyncio."""
    client = FakeClient([model_turn(types.Part(text="поздно"))], delay=0.2)

    with pytest.raises(LLMTimeoutError):
        await run_tool_loop(
            client=client,
            model="fake-model",
            contents=[types.Content(role="user", parts=[types.Part(text="привет")])],
            config=types.GenerateContentConfig(),
            executor=ok_executor,
            max_loops=2,
            timeout_ms=20,
        )


# --------------------------------------------------------------------------- #
# safe_text
# --------------------------------------------------------------------------- #
def test_safe_text_skips_thoughts() -> None:
    """Мысли модели клиенту не показываются, а ``response.text`` их приклеивает."""
    response = model_turn(
        types.Part(text="сначала подумаю", thought=True),
        types.Part(text="Здравствуйте!"),
    )

    assert safe_text(response) == "Здравствуйте!"


@pytest.mark.parametrize("finish", ["SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"])
def test_safe_text_raises_on_blocked_finish_reason(finish) -> None:
    """При таких ``finish_reason`` текста в ответе быть не может."""
    response = model_turn(types.Part(text="что-то"), finish=finish)

    with pytest.raises(LLMBlockedError):
        safe_text(response)


def test_malformed_function_call_is_retryable_not_blocked() -> None:
    """Испорченный вызов функции — техническая осечка, а не блокировка.

    В одном списке с SAFETY она стоила бы диалогу эскалации на человека и паузы
    бота: ``LLMBlockedError`` пробрасывается из ``_with_retry`` без единого
    повтора. Правильная реакция — повторить виток.
    """
    response = model_turn(types.Part(text="что-то"), finish="MALFORMED_FUNCTION_CALL")

    with pytest.raises(LLMError) as info:
        safe_text(response)

    assert not isinstance(info.value, LLMBlockedError)
    assert info.value.code == MALFORMED_CALL_CODE
    assert info.value.retryable is True


# --------------------------------------------------------------------------- #
# Бюджет хода
# --------------------------------------------------------------------------- #
async def test_call_timeout_is_capped_by_turn_deadline() -> None:
    """Потолок одного вызова не может пережить дедлайн всего хода.

    Иначе ход длится (витки + 1) × ``gemini_timeout_ms`` и умирает снаружи — по
    таймауту задачи воркера, посреди корутины, без заглушки клиенту.
    """
    client = FakeClient([model_turn(types.Part(text="поздно"))], delay=5.0)
    started = time.monotonic()

    with pytest.raises(LLMTimeoutError):
        await run_tool_loop(
            client=client,
            model="fake-model",
            contents=[types.Content(role="user", parts=[types.Part(text="привет")])],
            config=types.GenerateContentConfig(),
            executor=ok_executor,
            max_loops=5,
            timeout_ms=30_000,
            deadline=time.monotonic() + 0.05,
        )

    assert time.monotonic() - started < 1.0


async def test_turn_deadline_stops_the_loop_between_iterations() -> None:
    """Инструменты тоже тратят бюджет хода: витка после дедлайна не будет."""
    responses = [
        model_turn(call_part("get_gyms", {}, call_id="c1")),
        model_turn(types.Part(text="не должно дойти до этого ответа")),
    ]
    client = FakeClient(responses)

    async def slow_executor(name: str, args: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(0.08)
        return ToolResult.success(data={"tool": name})

    with pytest.raises(LLMTimeoutError):
        await run_tool_loop(
            client=client,
            model="fake-model",
            contents=[types.Content(role="user", parts=[types.Part(text="привет")])],
            config=types.GenerateContentConfig(),
            executor=slow_executor,
            max_loops=5,
            timeout_ms=30_000,
            deadline=time.monotonic() + 0.05,
        )

    assert len(client.calls) == 1


def test_safe_text_returns_none_without_candidates() -> None:
    """Пустой ответ — это ``None``, а не исключение."""
    assert safe_text(types.GenerateContentResponse(candidates=[])) is None


# --------------------------------------------------------------------------- #
# История
# --------------------------------------------------------------------------- #
def test_content_dict_roundtrip_keeps_signature() -> None:
    """История хранится как JSON и обязана переживать обратное преобразование."""
    content = types.Content(
        role="model", parts=[types.Part(text="привет", thought_signature=b"sig")]
    )

    restored = dict_to_content(content_to_dict(content))

    assert restored.parts[0].thought_signature == b"sig"
    assert restored.role == "model"


def test_trim_history_never_cuts_between_call_and_response() -> None:
    """``function_call`` без парного ``function_response`` — гарантированная ошибка запроса."""
    history = [
        {"role": "user", "parts": [{"text": "первый вопрос"}]},
        {"role": "model", "parts": [{"function_call": {"name": "get_gyms", "args": {}}}]},
        {"role": "user", "parts": [{"function_response": {"name": "get_gyms", "response": {}}}]},
        {"role": "model", "parts": [{"text": "ответ"}]},
        {"role": "user", "parts": [{"text": "второй вопрос"}]},
        {"role": "model", "parts": [{"text": "ответ 2"}]},
    ]

    trimmed = trim_history(history, max_turns=3)

    assert trimmed[0]["role"] == "user"
    assert "function_response" not in str(trimmed[0])
    assert trimmed[-1] == history[-1]


def test_trim_history_keeps_short_history_untouched() -> None:
    """Короткую историю резать нечего."""
    history = [{"role": "user", "parts": [{"text": "вопрос"}]}]

    assert trim_history(history, max_turns=10) == history
    assert trim_history(history, max_turns=0) == history
