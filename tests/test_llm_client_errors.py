"""Классификация ошибок Gemini и бюджет повторов.

Цена ошибки классификации — не лишний лог, а живой человек: ``LLMQuotaError``
пробрасывается из ``_with_retry`` без единого повтора, пайплайн отвечает
заглушкой, шлёт карточку администратору и ставит бота на паузу. Поэтому обычный
лимит «запросов в минуту» обязан оставаться ретраебельным.

Сети здесь нет: используются настоящие объекты ошибок SDK с теми телами
ответов, которыми Gemini отдаёт 429.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from google.genai import errors as genai_errors

from app.config import get_settings
from app.llm import client as client_mod
from app.llm.client import GeminiClient, _classify
from app.llm.tool_runner import MALFORMED_CALL_CODE
from app.types import LLMError, LLMQuotaError, LLMRateLimitError


def error_429(message: str, *, details: list[dict] | None = None) -> genai_errors.ClientError:
    """Настоящая ошибка SDK с телом ответа Gemini."""
    body: dict = {"error": {"message": message, "status": "RESOURCE_EXHAUSTED"}}
    if details is not None:
        body["error"]["details"] = details
    return genai_errors.ClientError(429, body)


def quota_failure(quota_id: str) -> list[dict]:
    """``QuotaFailure`` со структурной метрикой квоты — как отдаёт Gemini."""
    return [
        {
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [
                {
                    "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                    "quotaId": quota_id,
                }
            ],
        }
    ]


# --------------------------------------------------------------------------- #
# Классификация 429
# --------------------------------------------------------------------------- #
def test_bare_resource_exhausted_is_retryable() -> None:
    """Голое ``Resource has been exhausted`` — это лимит в минуту, а не сутки.

    Подробностей в теле нет, значит, единственная безопасная сторона —
    повторить: лишняя пауза 0.6 с дешевле ложной эскалации на человека.
    """
    mapped = _classify(error_429("Resource has been exhausted (e.g. check quota)."))

    assert isinstance(mapped, LLMRateLimitError)
    assert mapped.retryable is True


def test_per_minute_quota_id_is_rate_limit_not_daily_quota() -> None:
    """``PerMinute`` в метрике квоты написан слитно — поиск по «per minute» его не видел."""
    mapped = _classify(
        error_429(
            "You exceeded your current quota, please check your plan and billing details.",
            details=quota_failure("GenerateRequestsPerMinutePerProjectPerModel-FreeTier"),
        )
    )

    assert isinstance(mapped, LLMRateLimitError)
    assert not isinstance(mapped, LLMQuotaError)
    assert mapped.retryable is True


def test_per_minute_quota_id_in_message_is_rate_limit() -> None:
    """Тот же признак, но выписанный прямо в текст сообщения."""
    mapped = _classify(
        error_429(
            "You exceeded your current quota. quota_id: "
            "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
        )
    )

    assert isinstance(mapped, LLMRateLimitError)


def test_per_day_quota_id_is_daily_quota() -> None:
    """Суточную квоту backoff не лечит — она обязана остаться неретраебельной."""
    mapped = _classify(
        error_429(
            "You exceeded your current quota, please check your plan and billing details.",
            details=quota_failure("GenerateRequestsPerDayPerProjectPerModel-FreeTier"),
        )
    )

    assert isinstance(mapped, LLMQuotaError)
    assert mapped.retryable is False


# --------------------------------------------------------------------------- #
# Повторы
# --------------------------------------------------------------------------- #
@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Паузы backoff в тестах не спят: проверяется факт повтора, а не таймер."""
    monkeypatch.setattr(client_mod, "_BACKOFF_BASE_S", 0.001)
    monkeypatch.setattr(client_mod, "_BACKOFF_MAX_S", 0.001)


async def test_per_minute_limit_is_retried_instead_of_escalated(fast_backoff: None) -> None:
    """Упёрлись в лимит в минуту — повторяем; клиент получает ответ, а не заглушку."""
    client = GeminiClient(api_key="", settings=get_settings())
    calls: list[str] = []

    async def run(model: str) -> str:
        calls.append(model)
        if len(calls) == 1:
            raise error_429(
                "You exceeded your current quota, please check your plan and billing details.",
                details=quota_failure("GenerateRequestsPerMinutePerProjectPerModel-FreeTier"),
            )
        return "ответ"

    result, model_used, fallback = await client._with_retry(run, what="test")

    assert result == "ответ"
    assert len(calls) == 2
    assert fallback is False
    assert model_used == calls[0]


async def test_malformed_function_call_is_retried(fast_backoff: None) -> None:
    """Осечка на вызове функции лечится повтором, а не передачей диалога человеку."""
    client = GeminiClient(api_key="", settings=get_settings())
    attempts: list[int] = []

    async def run(model: str) -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise LLMError("malformed", code=MALFORMED_CALL_CODE, retryable=True)
        return "расписание"

    result, _, _ = await client._with_retry(run, what="test")

    assert result == "расписание"
    assert len(attempts) == 2


async def test_daily_quota_is_not_retried(fast_backoff: None) -> None:
    """Суточная квота — повторять нечего, попытка ровно одна."""
    client = GeminiClient(api_key="", settings=get_settings())
    attempts: list[int] = []

    async def run(model: str) -> str:
        attempts.append(1)
        raise error_429(
            "You exceeded your current quota, please check your plan and billing details.",
            details=quota_failure("GenerateRequestsPerDayPerProjectPerModel-FreeTier"),
        )

    with pytest.raises(LLMQuotaError):
        await client._with_retry(run, what="test")

    assert len(attempts) == 1


async def test_retries_stop_at_the_turn_deadline(fast_backoff: None) -> None:
    """Повторы обязаны укладываться в бюджет хода, а не переживать его.

    Иначе ход длится дольше таймаута задачи воркера и TTL лока диалога: задачу
    убивают посреди хода, а лок протухает — и второе сообщение того же клиента
    запускает параллельный ход.
    """
    client = GeminiClient(api_key="", settings=get_settings())
    attempts: list[int] = []

    async def run(model: str) -> str:
        attempts.append(1)
        await asyncio.sleep(0.05)
        raise error_429("Resource has been exhausted (e.g. check quota).")

    started = time.monotonic()
    with pytest.raises(LLMError):
        await client._with_retry(run, what="test", deadline=time.monotonic() + 0.06)
    elapsed = time.monotonic() - started

    # Без дедлайна было бы 3 попытки × 2 модели = 6 попыток и ≥ 0.3 с.
    assert 1 <= len(attempts) <= 3
    assert elapsed < 0.3


# --------------------------------------------------------------------------- #
# Регрессия живого прогона: сужение списка инструментов не имеет права ронять ход
# --------------------------------------------------------------------------- #
def test_allowed_function_names_forces_validated_mode() -> None:
    """``allowed_function_names`` с режимом AUTO — гарантированная ошибка 400.

    Живой Gemini отвечает дословно: «Please set allowed_function_names only when
    function calling mode is ANY». Пайплайн сужает список инструментов при
    подозрении на инъекцию и на офтопике, оставляя режим AUTO, — и любое такое
    сообщение роняло ход: клиент получал «сбой на стороне сервиса», а бот уходил
    на паузу и замолкал до конца разговора.

    VALIDATED, а не ANY: ANY обязывает модель вызывать инструмент на каждом ходу,
    а она должна уметь просто ответить текстом.
    """
    from google.genai import types

    from app.llm.config import build_config

    fn = types.FunctionDeclaration(
        name="get_kb_fact",
        description="факт из базы знаний",
        parameters={"type": "object", "properties": {"topic": {"type": "string"}}},
    )
    config = build_config(
        system_instruction="инструкция",
        tools=[types.Tool(function_declarations=[fn])],
        allowed_function_names=["get_kb_fact"],
        mode="AUTO",
        max_output_tokens=256,
    )

    fcc = config.tool_config.function_calling_config
    assert fcc.mode is types.FunctionCallingConfigMode.VALIDATED
    assert fcc.allowed_function_names == ["get_kb_fact"]


def test_unrestricted_call_keeps_auto_mode() -> None:
    """Без сужения списка режим остаётся AUTO — модель сама решает, звать ли инструмент."""
    from google.genai import types

    from app.llm.config import build_config

    fn = types.FunctionDeclaration(name="get_gyms", description="залы", parameters={"type": "object"})
    config = build_config(
        system_instruction="инструкция",
        tools=[types.Tool(function_declarations=[fn])],
        allowed_function_names=None,
        mode="AUTO",
        max_output_tokens=256,
    )

    fcc = config.tool_config.function_calling_config
    assert fcc.mode is types.FunctionCallingConfigMode.AUTO
    assert not fcc.allowed_function_names
