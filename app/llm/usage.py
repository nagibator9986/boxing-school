"""Учёт потребления модели и оценка стоимости.

У ``GenerateContentResponse`` НЕТ атрибута ``usage`` — есть только
``usage_metadata`` (проверено интроспекцией SDK 2.17). Кэш считается по
``cached_content_token_count``.
"""

from __future__ import annotations

from typing import Any, Final

from app.types import LLMUsage

#: Токенов в «миллионе токенов» прайс-листа.
_MTOK: Final[float] = 1_000_000.0


def _int(value: Any) -> int:
    """Безопасное приведение поля usage_metadata: там всё Optional."""
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):  # pragma: no cover - защита от мусора в SDK
        return 0


def usage_from_response(response: Any, *, model: str, latency_ms: int) -> LLMUsage:
    """Читает ``response.usage_metadata``.

    Отсутствие метаданных не ошибка: возвращаются нули, чтобы учёт стоимости
    не ронял ответ клиенту.
    """
    meta = getattr(response, "usage_metadata", None)
    finish_reason: str | None = None
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        raw_finish = getattr(candidates[0], "finish_reason", None)
        finish_reason = getattr(raw_finish, "value", None) or (
            str(raw_finish) if raw_finish is not None else None
        )

    if meta is None:
        return LLMUsage(model=model, latency_ms=latency_ms, finish_reason=finish_reason)

    return LLMUsage(
        model=model,
        prompt_tokens=_int(getattr(meta, "prompt_token_count", None)),
        cached_tokens=_int(getattr(meta, "cached_content_token_count", None)),
        candidates_tokens=_int(getattr(meta, "candidates_token_count", None)),
        thoughts_tokens=_int(getattr(meta, "thoughts_token_count", None)),
        total_tokens=_int(getattr(meta, "total_token_count", None)),
        latency_ms=latency_ms,
        finish_reason=finish_reason,
    )


def estimate_cost_usd(usage: LLMUsage, *, price_in: float, price_out: float) -> float:
    """Оценка стоимости вызова. thoughts-токены тарифицируются как выходные.

    Кэшированные входные токены дешевле, но коэффициент скидки зависит от модели
    и в настройках его нет — поэтому считаем консервативно, по полной цене входа.
    """
    tokens_in = max(usage.prompt_tokens, 0)
    tokens_out = max(usage.candidates_tokens, 0) + max(usage.thoughts_tokens, 0)
    return round((tokens_in * price_in + tokens_out * price_out) / _MTOK, 8)


def with_cost(usage: LLMUsage, *, price_in: float, price_out: float) -> LLMUsage:
    """Копия ``usage`` с заполненным ``cost_usd``."""
    return usage.model_copy(
        update={"cost_usd": estimate_cost_usd(usage, price_in=price_in, price_out=price_out)}
    )


__all__ = ["estimate_cost_usd", "usage_from_response", "with_cost"]
