"""Клиент модели: боевой :class:`GeminiClient` и его тестовые двойники.

Слой ``app.llm`` — единственное место, где живёт ``google.genai``; наружу
отдаются только типы из :mod:`app.types` и история в виде ``list[dict]``.
Ни один импорт этого модуля не делает сетевых вызовов: SDK-клиент создаётся
лениво, ключ не проверяется.

Главный инвариант модуля — **сохранение thought signatures**. Ответ модели
уходит в историю ЦЕЛИКОМ (``candidate.content`` со всеми частями и подписями),
и это сделано невозможным нарушить по конструкции:

* история собирается только классом :class:`_Contents`, который принимает
  ``types.Content``; метода «добавить ход модели из строки» у него нет;
* ход модели представлен типом :class:`ModelTurn`, чей конструктор не принимает
  ``str`` — положить в историю «только текст» нельзя даже по ошибке;
* после каждого хода :func:`_assert_signatures_kept` сверяет число подписей во
  входящей и исходящей истории: молчаливая потеря подписи превращается в
  громкую :class:`app.types.LLMError`.

Учёт: клиент сам досчитывает ``cost_usd`` и вызывает
``metrics.observe_llm`` — пайплайну повторять это не нужно.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, Awaitable, Callable, Final, Protocol, Sequence, TypeVar, final, runtime_checkable

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app.config import Settings, get_settings
from app.llm.config import build_config, model_fallback, model_primary
from app.llm.dynamic import wrap_user_message
from app.llm.tool_runner import (
    content_to_dict,
    dict_to_content,
    run_tool_loop,
    safe_text,
)
from app.llm.tools_schema import allowed_names, to_function_declarations
from app.llm.usage import with_cost
from app.logging_conf import get_logger
from app.observability import metrics
from app.types import (
    Language,
    LeadDraft,
    LLMBlockedError,
    LLMError,
    LLMQuotaError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMTimeoutError,
    LLMToolLoopError,
    LLMUsage,
    ToolExecutor,
    ToolInvocation,
    ToolResult,
)

log = get_logger(__name__)

T = TypeVar("T")

#: Сколько раз пробуем одну модель, прежде чем уйти на запасную.
_ATTEMPTS_PER_MODEL: Final[int] = 3

#: Базовая пауза backoff, секунды. Реальная пауза — base * 2**(попытка-1) * джиттер.
_BACKOFF_BASE_S: Final[float] = 0.6
_BACKOFF_MAX_S: Final[float] = 8.0

#: HTTP-коды, на которых имеет смысл повторить запрос (retry живёт и внутри SDK).
_RETRY_STATUS: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})

#: Ретраи внутри самого SDK — первая линия обороны, до нашего цикла.
_SDK_RETRY_ATTEMPTS: Final[int] = 2

#: Насколько внутренний дедлайн хода короче ``llm_turn_budget_s``: свой таймаут
#: обязан сработать раньше внешнего, иначе ход отменят снаружи.
_TURN_BUDGET_RESERVE_S: Final[float] = 2.0

#: Признаки СУТОЧНОЙ (или иной длинной) квоты в 429: её backoff не лечит.
#: Ищутся в «сплющенной» строке — без пробелов, дефисов и подчёркиваний,
#: поэтому ``PerDay``, ``per-day`` и ``per_day`` совпадают одинаково.
#:
#: Сюда же — деньги на проекте. «Your prepayment credits are depleted» Gemini
#: отдаёт тем же 429 RESOURCE_EXHAUSTED, что и лимит в минуту, но повторять
#: такое бессмысленно: до пополнения счёта не поможет ни backoff, ни запасная
#: модель — ключ-то один. Без этих признаков каждое сообщение клиента стоило
#: шести отказавших вызовов и нескольких секунд его ожидания впустую.
_QUOTA_MARKERS: Final[tuple[str, ...]] = (
    "perday",
    "daily",
    "billingnotenabled",
    "billingaccount",
    "quotaexceededforquotametricperday",
    "creditsaredepleted",
    "prepaymentcredits",
    "insufficientcredits",
    "outofcredits",
    "billingisdisabled",
)

#: Признаки лимита В МИНУТУ (или в секунду): ровно то, ради чего есть backoff.
_RATE_MARKERS: Final[tuple[str, ...]] = (
    "perminute",
    "persecond",
    "ratelimit",
    "requestsperminute",
    "tokensperminute",
)


# --------------------------------------------------------------------------- #
# Контракт
# --------------------------------------------------------------------------- #
@runtime_checkable
class LLMClient(Protocol):
    """Единственный интерфейс модели, который видит остальной проект."""

    async def generate(self, req: LLMRequest, executor: ToolExecutor) -> LLMResponse:
        """Один ход диалога: запрос → инструменты → ответ."""
        ...

    async def extract_lead(self, transcript: str, *, lang: Language) -> tuple[LeadDraft, LLMUsage]:
        """«Тихий» вызов: вытащить поля лида из расшифровки диалога."""
        ...

    async def warmup(self) -> None:
        """Прогрев на старте: создать клиента SDK. Сетевых вызовов нет."""
        ...

    async def aclose(self) -> None:
        """Закрыть HTTP-пул. Повторный вызов безопасен."""
        ...


# --------------------------------------------------------------------------- #
# Ход модели: тип, который нельзя собрать из одного текста
# --------------------------------------------------------------------------- #
@final
class ModelTurn:
    """Ход модели в истории — ТОЛЬКО целиком, вместе с thought signatures.

    Тип существует ради одного инварианта. Соблазн положить в историю
    ``{"role": "model", "parts": [{"text": ответ}]}`` велик, а последствия
    незаметны: подпись рассуждения теряется, и на следующем витке
    многоходового function calling модель «забывает», зачем звала инструмент.
    Поэтому конструктора из ``str`` здесь нет и добавлять его нельзя —
    принимается только ``types.Content`` (или ``types.Candidate``) целиком.
    """

    __slots__ = ("_content",)

    def __init__(self, content: "types.Content") -> None:
        if not isinstance(content, types.Content):
            raise TypeError(
                "ModelTurn собирается только из types.Content целиком: "
                "текст ответа без thought signature в историю не кладётся"
            )
        if content.role and content.role != "model":
            raise ValueError(f"ModelTurn ожидает role='model', получено {content.role!r}")
        self._content = content

    @classmethod
    def from_candidate(cls, candidate: "types.Candidate") -> "ModelTurn":
        """Ход из кандидата ответа. ``candidate.content`` берётся как есть."""
        content = getattr(candidate, "content", None)
        if content is None:
            raise ValueError("кандидат без content: класть в историю нечего")
        return cls(content)

    @property
    def content(self) -> "types.Content":
        """Исходный ``Content`` без изменений."""
        return self._content

    @property
    def signature_count(self) -> int:
        """Сколько частей хода несут ``thought_signature``."""
        return sum(1 for part in (self._content.parts or []) if part.thought_signature)

    def to_history(self) -> dict[str, Any]:
        """JSON-форма для хранения. Подписи переживают round-trip (base64)."""
        return content_to_dict(self._content)

    def __repr__(self) -> str:  # pragma: no cover - диагностика
        parts = len(self._content.parts or [])
        return f"ModelTurn(parts={parts}, signatures={self.signature_count})"


@final
class _Contents:
    """Сборщик ``contents`` запроса.

    Публичных способов добавить ход модели «из текста» у класса нет: единственная
    дверь для роли ``model`` — :meth:`add_model_turn`, принимающий
    :class:`ModelTurn`. Всё остальное — пользовательские элементы.
    """

    __slots__ = ("_items",)

    def __init__(self) -> None:
        self._items: list[types.Content] = []

    @classmethod
    def from_request(cls, req: LLMRequest) -> "_Contents":
        """История + сообщение клиента + служебная заметка последним элементом."""
        built = cls()
        built.extend_history(req.history)
        built.add_user_text(_user_block(req.user_text))
        if req.dynamic_note.strip():
            built.add_user_text(req.dynamic_note)
        return built

    def extend_history(self, history: Sequence[Mapping[str, Any]]) -> None:
        """Восстанавливает сохранённую историю. Битая запись — ошибка валидации."""
        for raw in history:
            self._items.append(dict_to_content(dict(raw)))

    def add_user_text(self, text: str) -> None:
        """Текстовый элемент от лица клиента."""
        self._items.append(types.Content(role="user", parts=[types.Part(text=text)]))

    def add_model_turn(self, turn: ModelTurn) -> None:
        """Ход модели целиком. Другого способа добавить роль ``model`` нет."""
        self._items.append(turn.content)

    def add_tool_results(self, parts: Sequence["types.Part"]) -> None:
        """Ответы инструментов одного витка — ОДНИМ ``Content`` с ``role='user'``."""
        self._items.append(types.Content(role="user", parts=list(parts)))

    def as_list(self) -> list["types.Content"]:
        """Свежая копия списка: ``run_tool_loop`` дописывает в него ходы."""
        return list(self._items)

    def to_history(self) -> list[dict[str, Any]]:
        """JSON-форма всей истории."""
        return [content_to_dict(item) for item in self._items]


def _user_block(text: str) -> str:
    """Сообщение клиента в контейнере ``<user_message>`` (повторно не оборачиваем)."""
    stripped = (text or "").lstrip()
    if stripped.startswith("<user_message>"):
        return text
    return wrap_user_message(text)


def _count_signatures(history: Sequence[Mapping[str, Any]]) -> int:
    """Сколько частей истории несут ``thought_signature`` (история в JSON-форме)."""
    total = 0
    for item in history:
        for part in item.get("parts", []) or []:
            if isinstance(part, Mapping) and part.get("thought_signature"):
                total += 1
    return total


def _assert_signatures_kept(
    before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]
) -> None:
    """Тревога, если подписи из входящей истории не доехали до исходящей.

    Потеря подписи не ломает текущий ответ — она ломает СЛЕДУЮЩИЙ ход, и найти
    это по логам почти невозможно. Поэтому падаем сразу и громко.
    """
    was = _count_signatures(before)
    now = _count_signatures(after)
    if now < was:
        log.error("history_signature_lost", before=was, after=now)
        raise LLMError(
            f"история потеряла thought signatures: было {was}, стало {now}",
            code="llm_history_corrupt",
        )


# --------------------------------------------------------------------------- #
# Классификация ошибок SDK
# --------------------------------------------------------------------------- #
def _status_of(exc: BaseException) -> int | None:
    """HTTP-код ошибки SDK, если он есть."""
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def _429_haystack(exc: BaseException, message: str) -> str:
    """Текст ошибки вместе со структурными деталями, без разделителей.

    Gemini пишет метрику квоты в ``details`` (``QuotaFailure.violations`` →
    ``quota_id``/``quota_metric``), причём слитным CamelCase:
    ``GenerateRequestsPerMinutePerProjectPerModel-FreeTier``. Поиск по
    «per minute» с пробелом такое не находит, поэтому склеиваем всё в одну
    строку и убираем всё, кроме букв и цифр.
    """
    details = getattr(exc, "details", None)
    raw = f"{message} {details if details is not None else ''}"
    return re.sub(r"[^a-z0-9]", "", raw.lower())


def _classify(exc: BaseException) -> LLMError:
    """Ошибка SDK → своё исключение из :mod:`app.types`.

    ``quota_exceeded`` отделяется от ``rate_limit_exceeded`` намеренно: первое
    backoff не лечит, второе лечит. Разделение идёт по метрике квоты
    (``PerDay``/``PerMinute``), а не по вольному тексту сообщения: Gemini отдаёт
    и посекундный лимит, и суточный одним и тем же «You exceeded your current
    quota», а «Resource has been exhausted» вообще без подробностей.

    При неоднозначности 429 считается ретраебельным: лишний повтор через
    секунду дешевле, чем ложная эскалация на человека и пауза бота.
    """
    status = _status_of(exc)
    message = str(getattr(exc, "message", "") or exc)
    lowered = message.lower()
    if status == 429 or "resource_exhausted" in lowered:
        squashed = _429_haystack(exc, message)
        minute = any(marker in squashed for marker in _RATE_MARKERS)
        daily = any(marker in squashed for marker in _QUOTA_MARKERS)
        if daily and not minute:
            return LLMQuotaError(f"квота Gemini исчерпана: {message[:300]}")
        return LLMRateLimitError(f"Gemini rate limit: {message[:300]}")
    if status in _RETRY_STATUS:
        return LLMError(
            f"Gemini временно недоступен ({status}): {message[:300]}",
            code="llm_unavailable",
            retryable=True,
        )
    if status is not None and 400 <= status < 500:
        return LLMError(f"Gemini отклонил запрос ({status}): {message[:300]}", code="llm_bad_request")
    return LLMError(f"Сбой вызова Gemini: {message[:300]}", code="llm_error", retryable=True)


def _backoff_delay(attempt: int) -> float:
    """Экспоненциальная пауза с джиттером: ``base * 2**(attempt-1) * U(0.5, 1.5)``."""
    raw = _BACKOFF_BASE_S * (2 ** max(attempt - 1, 0))
    return min(raw, _BACKOFF_MAX_S) * random.uniform(0.5, 1.5)


# --------------------------------------------------------------------------- #
# Боевой клиент
# --------------------------------------------------------------------------- #
class GeminiClient(LLMClient):
    """Клиент Gemini поверх ``google-genai``.

    Клиент SDK создаётся лениво при первом вызове; ключ не проверяется — модуль
    обязан импортироваться и в окружении без ключей.
    """

    def __init__(self, *, api_key: str, settings: Settings) -> None:
        self._api_key = api_key
        self._settings = settings
        self._sdk_client: "genai.Client | None" = None

    # -- инфраструктура ---------------------------------------------------- #
    def _sdk(self) -> "genai.Client":
        """Ленивая сборка клиента SDK. Сети здесь нет — только объект и пул."""
        if self._sdk_client is None:
            self._sdk_client = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(
                    timeout=self._settings.gemini_timeout_ms,
                    retry_options=types.HttpRetryOptions(
                        attempts=_SDK_RETRY_ATTEMPTS,
                        initial_delay=1.0,
                        max_delay=_BACKOFF_MAX_S,
                        exp_base=2.0,
                        jitter=1.0,
                        http_status_codes=sorted(_RETRY_STATUS),
                    ),
                ),
            )
        return self._sdk_client

    async def warmup(self) -> None:
        """Создаёт клиента SDK заранее, чтобы первый диалог не платил за это."""
        self._sdk()
        log.info("llm_warmed_up", model=model_primary(self._settings))

    async def aclose(self) -> None:
        """Закрывает HTTP-пул SDK. Повторный вызов безопасен."""
        client = self._sdk_client
        self._sdk_client = None
        if client is None:
            return
        try:
            await client.aio.aclose()
        except Exception as exc:  # pragma: no cover - закрытие не должно ронять shutdown
            log.warning("llm_close_failed", error=type(exc).__name__, detail=str(exc)[:200])

    def _models(self, requested: str | None) -> list[str]:
        """Цепочка моделей: основная, затем запасная (если она другая)."""
        primary = requested or model_primary(self._settings)
        fallback = model_fallback(self._settings)
        return [primary] if fallback == primary else [primary, fallback]

    async def _with_retry(
        self,
        run: Callable[[str], Awaitable[T]],
        *,
        what: str,
        requested_model: str | None = None,
        deadline: float | None = None,
    ) -> tuple[T, str, bool]:
        """Выполняет ``run(model)`` с backoff и фолбэком на запасную модель.

        Возвращает ``(результат, использованная модель, был ли фолбэк)``.
        ``LLMQuotaError`` и ошибки запроса пробрасываются без повторов.

        ``deadline`` (шкала :func:`time.monotonic`) — общий потолок хода:
        повторы и паузы backoff обязаны укладываться в него, иначе ход
        переживёт таймаут задачи воркера и TTL лока диалога.
        """
        chain = self._models(requested_model)
        last: LLMError | None = None
        for model_index, model in enumerate(chain):
            for attempt in range(1, _ATTEMPTS_PER_MODEL + 1):
                if deadline is not None and time.monotonic() >= deadline:
                    raise last or LLMTimeoutError(f"Бюджет хода исчерпан ({what})")
                try:
                    return await run(model), model, model_index > 0
                except (LLMQuotaError, LLMToolLoopError, LLMBlockedError):
                    raise
                except LLMTimeoutError as exc:
                    # Таймаут повторять нечем: бюджет ожидания клиента уже потрачен.
                    metrics.observe_llm_error(exc.code)
                    raise
                except genai_errors.APIError as exc:
                    mapped = _classify(exc)
                except LLMError as exc:
                    mapped = exc
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # неизвестный сбой транспорта
                    mapped = LLMError(
                        f"Непредвиденный сбой вызова модели: {type(exc).__name__}",
                        code="llm_error",
                        retryable=True,
                    )

                metrics.observe_llm_error(mapped.code)
                last = mapped
                if isinstance(mapped, LLMQuotaError) or not mapped.retryable:
                    raise mapped
                log.warning(
                    "llm_attempt_failed",
                    what=what,
                    model=model,
                    attempt=attempt,
                    code=mapped.code,
                    error=mapped.message[:200],
                )
                if attempt < _ATTEMPTS_PER_MODEL:
                    pause = _backoff_delay(attempt)
                    if deadline is not None:
                        # Спать дольше остатка бюджета бессмысленно: проснёмся
                        # уже за дедлайном, и попытка всё равно не состоится.
                        pause = min(pause, max(0.0, deadline - time.monotonic()))
                    await asyncio.sleep(pause)
            if model_index + 1 < len(chain):
                log.warning("llm_fallback_model", what=what, frm=model, to=chain[model_index + 1])
        raise last or LLMError("Модель не ответила и не сообщила причину", code="llm_error")

    # -- основной ход ------------------------------------------------------ #
    async def generate(self, req: LLMRequest, executor: ToolExecutor) -> LLMResponse:
        """Один ход диалога: собрать запрос, отработать tool-loop, вернуть ответ.

        Ответ модели попадает в историю целиком (см. :class:`ModelTurn`),
        стоимость каждого вызова досчитывается здесь же и уходит в метрики.
        """
        config = build_config(
            system_instruction=req.system_instruction,
            tools=to_function_declarations(req.tool_specs) or None,
            allowed_function_names=(
                allowed_names(req.tool_specs, list(req.allowed_function_names))
                if req.allowed_function_names is not None
                else None
            ),
            mode=req.tool_mode,
            max_output_tokens=req.max_output_tokens or self._settings.gemini_max_output_tokens,
            settings=self._settings,
        )
        contents = _Contents.from_request(req)

        # Общий дедлайн хода: витки tool-loop, повторы и запасная модель считают
        # ОДИН бюджет. Без него ход стоил бы (витки + 1) × таймаут вызова × 3
        # попытки × 2 модели и умирал бы снаружи — по таймауту задачи воркера,
        # посреди хода, без заглушки клиенту и с протухшим локом диалога.
        budget_s = max(1, int(self._settings.llm_turn_budget_s))
        # Запас, чтобы внутренний дедлайн сработал РАНЬШЕ внешнего wait_for
        # пайплайна: тогда ход обрывается своим LLMTimeoutError и успевает
        # вернуть заглушку, а не отменяется посреди корутины.
        deadline = time.monotonic() + max(1.0, budget_s - _TURN_BUDGET_RESERVE_S)

        async def once(model: str) -> LLMResponse:
            return await run_tool_loop(
                client=self._sdk(),
                model=model,
                contents=contents.as_list(),
                config=config,
                executor=executor,
                max_loops=self._settings.llm_max_tool_loops,
                timeout_ms=self._settings.gemini_timeout_ms,
                deadline=deadline,
            )

        try:
            response, model_used, fallback_used = await self._with_retry(
                once, what="generate", requested_model=req.model, deadline=deadline
            )
        except LLMBlockedError as exc:
            # Блокировка — не сбой инфраструктуры: пайплайн отвечает по KB и эскалирует.
            metrics.observe_llm_error(exc.code)
            log.warning("llm_blocked", correlation_id=req.correlation_id, reason=exc.message[:200])
            return LLMResponse(
                text=None,
                blocked=True,
                block_reason=exc.message[:300],
                history=list(req.history),
                model_used=model_primary(self._settings),
            )

        priced = self._priced(response.usage)
        _assert_signatures_kept(req.history, response.history)
        return response.model_copy(
            update={"usage": priced, "model_used": model_used, "fallback_used": fallback_used}
        )

    # -- тихий ход --------------------------------------------------------- #
    async def extract_lead(self, transcript: str, *, lang: Language) -> tuple[LeadDraft, LLMUsage]:
        """Отдельный вызов со structured output. Инструменты не подключаются."""
        from app.llm.extract_lead import extract  # локальный импорт: избегаем цикла

        gym_ids = self._gym_ids()

        async def once(model: str) -> tuple[LeadDraft, LLMUsage]:
            return await extract(
                self._sdk(),
                transcript=transcript,
                lang=lang,
                model=model,
                gym_ids=gym_ids,
            )

        (draft, usage), model_used, _ = await self._with_retry(once, what="extract_lead")
        priced = self._priced((usage,))
        return draft, priced[0].model_copy(update={"model": model_used})

    def _gym_ids(self) -> tuple[str, ...]:
        """Идентификаторы залов из снимка KB. Без KB — пустой кортеж."""
        try:
            from app.kb.loader import get_snapshot

            return tuple(get_snapshot().gym_ids)
        except Exception:  # KB ещё не загружена — извлечение обойдётся без списка
            return ()

    def _priced(self, usages: Sequence[LLMUsage]) -> tuple[LLMUsage, ...]:
        """Досчитывает стоимость каждого вызова и отправляет его в метрики."""
        priced: list[LLMUsage] = []
        for item in usages:
            enriched = with_cost(
                item,
                price_in=self._settings.llm_price_in_per_mtok,
                price_out=self._settings.llm_price_out_per_mtok,
            )
            metrics.observe_llm(enriched)
            priced.append(enriched)
        return tuple(priced)


# --------------------------------------------------------------------------- #
# Заглушка
# --------------------------------------------------------------------------- #
class NullLLMClient(LLMClient):
    """Заглушка для тестов и режима «только KB»: модель всегда молчит."""

    def __init__(self, *, model: str = "null") -> None:
        self._model = model

    async def generate(self, req: LLMRequest, executor: ToolExecutor) -> LLMResponse:
        """Всегда ``LLMResponse(text=None, blocked=True)``. Историю не трогает."""
        return LLMResponse(
            text=None,
            blocked=True,
            block_reason="llm_disabled",
            history=list(req.history),
            model_used=self._model,
        )

    async def extract_lead(self, transcript: str, *, lang: Language) -> tuple[LeadDraft, LLMUsage]:
        """Пустой черновик: извлекать нечем."""
        return LeadDraft(lang=lang), LLMUsage(model=self._model)

    async def warmup(self) -> None:
        """Ничего не делает."""
        return None

    async def aclose(self) -> None:
        """Ничего не делает."""
        return None


# --------------------------------------------------------------------------- #
# Скриптуемый двойник для тестов
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FakeCall:
    """Запланированный вызов инструмента в скрипте :class:`FakeLLMClient`."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str | None = None


@dataclass(frozen=True)
class FakeTurn:
    """Один запланированный ответ модели.

    Ход либо завершает диалог (есть ``text``, нет ``calls``), либо зовёт
    инструменты (``calls``) — тогда за ним в скрипте должен идти следующий ход.
    """

    text: str | None = None
    calls: tuple[FakeCall, ...] = ()
    thought_signature: bytes | None = b"fake-thought-signature"
    finish_reason: str = "STOP"
    blocked: bool = False
    block_reason: str | None = None
    usage: LLMUsage | None = None

    @classmethod
    def answer(cls, text: str, **kwargs: Any) -> "FakeTurn":
        """Финальный ход: модель отвечает текстом."""
        return cls(text=text, **kwargs)

    @classmethod
    def tool(cls, *calls: FakeCall, text: str | None = None, **kwargs: Any) -> "FakeTurn":
        """Ход с вызовами инструментов (можно несколько — параллельный вызов)."""
        return cls(text=text, calls=tuple(calls), **kwargs)

    @classmethod
    def block(cls, reason: str = "SAFETY") -> "FakeTurn":
        """Ход, на котором модель заблокировала ответ."""
        return cls(text=None, blocked=True, block_reason=reason, finish_reason=reason)


class FakeLLMClient(LLMClient):
    """Детерминированный двойник модели: скрипт вместо сети.

    Отдаёт заранее заданную последовательность ходов, реально прогоняя через
    ``executor`` вызовы инструментов, — поэтому многоходовой function calling
    воспроизводится в тестах один в один. История собирается тем же кодом, что
    и в бою (``types.Content`` → :func:`content_to_dict`), включая
    ``thought_signature``: тест на потерю подписи имеет смысл только так.
    """

    def __init__(
        self,
        script: Sequence[FakeTurn] | None = None,
        *,
        lead: LeadDraft | None = None,
        model: str = "fake-model",
        max_loops: int = 5,
    ) -> None:
        self._script: list[FakeTurn] = list(script or ())
        self._lead = lead
        self._model = model
        self._max_loops = max_loops
        self.requests: list[LLMRequest] = []
        self.invocations: list[ToolInvocation] = []
        self.generate_calls: int = 0
        self.model_calls: int = 0
        self.extract_calls: int = 0
        self.closed: bool = False
        self.warmed: bool = False

    # -- управление скриптом ------------------------------------------------ #
    def push(self, *turns: FakeTurn) -> "FakeLLMClient":
        """Дописывает ходы в конец скрипта. Возвращает себя — удобно в тестах."""
        self._script.extend(turns)
        return self

    def reset(self, script: Sequence[FakeTurn] | None = None) -> None:
        """Сбрасывает счётчики и подменяет скрипт."""
        self._script = list(script or ())
        self.requests.clear()
        self.invocations.clear()
        self.generate_calls = 0
        self.model_calls = 0
        self.extract_calls = 0

    @property
    def pending(self) -> int:
        """Сколько ходов скрипта ещё не израсходовано."""
        return len(self._script)

    def _next(self) -> FakeTurn:
        """Следующий ход скрипта. Пустой скрипт — ошибка теста, а не молчание."""
        if not self._script:
            raise LLMError("скрипт FakeLLMClient исчерпан", code="fake_script_exhausted")
        return self._script.pop(0)

    # -- контракт ----------------------------------------------------------- #
    async def generate(self, req: LLMRequest, executor: ToolExecutor) -> LLMResponse:
        """Проигрывает скрипт, выполняя инструменты настоящим исполнителем."""
        self.requests.append(req)
        self.generate_calls += 1

        contents = _Contents.from_request(req)
        usages: list[LLMUsage] = []
        invocations: list[ToolInvocation] = []

        for loop_index in range(self._max_loops + 1):
            turn = self._next()
            self.model_calls += 1
            usages.append(
                turn.usage
                or LLMUsage(model=self._model, latency_ms=0, finish_reason=turn.finish_reason)
            )
            if turn.blocked:
                return LLMResponse(
                    text=None,
                    blocked=True,
                    block_reason=turn.block_reason or turn.finish_reason,
                    history=list(req.history),
                    usage=tuple(usages),
                    model_used=self._model,
                    loops=loop_index,
                )

            contents.add_model_turn(self._turn_content(turn, loop_index))
            if not turn.calls:
                history = contents.to_history()
                _assert_signatures_kept(req.history, history)
                return LLMResponse(
                    text=turn.text,
                    finish_reason=turn.finish_reason,
                    history=history,
                    invocations=tuple(invocations),
                    usage=tuple(usages),
                    model_used=self._model,
                    loops=loop_index,
                )

            if loop_index >= self._max_loops:
                raise LLMToolLoopError(
                    f"Модель не остановилась за {self._max_loops} витков tool-loop"
                )
            parts = await self._run_calls(turn, executor, loop_index, invocations)
            contents.add_tool_results(parts)

        raise LLMToolLoopError("Недостижимая ветка tool-loop")  # pragma: no cover

    async def extract_lead(self, transcript: str, *, lang: Language) -> tuple[LeadDraft, LLMUsage]:
        """Возвращает заранее заданный черновик лида."""
        self.extract_calls += 1
        draft = self._lead.model_copy() if self._lead is not None else LeadDraft(lang=lang)
        return draft, LLMUsage(model=self._model)

    async def warmup(self) -> None:
        """Отмечает факт прогрева."""
        self.warmed = True

    async def aclose(self) -> None:
        """Отмечает факт закрытия."""
        self.closed = True

    # -- внутреннее --------------------------------------------------------- #
    def _turn_content(self, turn: FakeTurn, loop_index: int) -> ModelTurn:
        """Собирает ``Content`` хода модели — с подписью, как настоящий кандидат."""
        parts: list[types.Part] = []
        if turn.text:
            parts.append(types.Part(text=turn.text, thought_signature=turn.thought_signature))
        for index, call in enumerate(turn.calls):
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(
                        id=call.id or f"fake-{loop_index}-{index}",
                        name=call.name,
                        args=dict(call.args),
                    ),
                    thought_signature=None if turn.text else turn.thought_signature,
                )
            )
        if not parts:
            parts.append(types.Part(text="", thought_signature=turn.thought_signature))
        return ModelTurn(types.Content(role="model", parts=parts))

    async def _run_calls(
        self,
        turn: FakeTurn,
        executor: ToolExecutor,
        loop_index: int,
        sink: list[ToolInvocation],
    ) -> list["types.Part"]:
        """Выполняет инструменты витка и собирает ``function_response``-части."""
        parts: list[types.Part] = []
        for index, call in enumerate(turn.calls):
            call_id = call.id or f"fake-{loop_index}-{index}"
            started = time.perf_counter()
            try:
                result = await executor(call.name, dict(call.args))
            except Exception as exc:  # исполнитель обязан не падать, но подстрахуемся
                result = ToolResult.failure(f"{call.name}: {type(exc).__name__}")
            invocation = ToolInvocation(
                call_id=call_id,
                name=call.name,
                args=dict(call.args),
                result=result,
                latency_ms=int((time.perf_counter() - started) * 1000),
                loop_index=loop_index,
            )
            sink.append(invocation)
            self.invocations.append(invocation)
            parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        id=call_id,
                        name=call.name,
                        response=result.to_llm_payload(),
                    )
                )
            )
        return parts


# --------------------------------------------------------------------------- #
# Сборка
# --------------------------------------------------------------------------- #
def build_client(settings: Settings | None = None) -> LLMClient:
    """Клиент по настройкам. Без ключа — :class:`NullLLMClient` (режим «только KB»)."""
    cfg = settings or get_settings()
    if not cfg.gemini_api_key:
        log.warning("llm_disabled_no_api_key")
        return NullLLMClient()
    return GeminiClient(api_key=cfg.gemini_api_key, settings=cfg)


__all__ = [
    "FakeCall",
    "FakeLLMClient",
    "FakeTurn",
    "GeminiClient",
    "LLMClient",
    "ModelTurn",
    "NullLLMClient",
    "build_client",
    "safe_text",
]
