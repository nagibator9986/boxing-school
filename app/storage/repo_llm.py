"""Запись вызовов модели: расход, задержки, отказы.

Таблица ``llm_call`` описана в архитектуре как след каждого обращения к
модели — по ней разбирают инциденты, считают стоимость диалога и видят, что
модель отвечает с ошибками. Схема была создана с самого начала, а писал в неё
никто: за всё время в таблице ноль строк.

Стоило это дорого. Когда на ключе кончились кредиты, бот сутки отвечал
заглушками, и ни в интерфейсе, ни в базе не было ни следа причины — владелец
узнал её, только спросив. Метрики Prometheus живут в памяти процесса и на
Railway никем не читаются; журнал он не открывает. Строка в базе переживает
перезапуск и видна из CRM.

Запись не имеет права ронять ход: ошибка здесь гасится вызывающим.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import LLMCall
from app.types import LLMUsage

__all__ = ["record", "stats"]


async def record(
    session: AsyncSession,
    *,
    conversation_id: UUID | None,
    usage: Sequence[LLMUsage] = (),
    model: str | None = None,
    tool_calls: Sequence[str] = (),
    finish_reason: str | None = None,
    error: str | None = None,
    kb_hash: str | None = None,
) -> None:
    """Одна строка на ход: сумма расхода витков плюс исход.

    Витков в ходе может быть несколько (модель зовёт инструменты и продолжает),
    и каждый из них — отдельный вызов. Для разбора инцидента важен ход целиком,
    поэтому токены и задержки суммируются, а модель берётся из последнего
    витка: именно она отдала ответ клиенту.
    """
    totals = _totals(usage)
    session.add(
        LLMCall(
            id=uuid4(),
            conversation_id=conversation_id,
            model=model or (usage[-1].model if usage else None),
            prompt_tokens=totals["prompt_tokens"],
            cached_tokens=totals["cached_tokens"],
            candidates_tokens=totals["candidates_tokens"],
            thoughts_tokens=totals["thoughts_tokens"],
            latency_ms=totals["latency_ms"],
            cost_usd=totals["cost_usd"],
            tool_calls=json.dumps(list(tool_calls), ensure_ascii=False),
            finish_reason=finish_reason or (usage[-1].finish_reason if usage else None),
            error=error,
            kb_hash=kb_hash,
            created_at=datetime.now(tz=timezone.utc),
        )
    )
    await session.flush()


def _totals(usage: Sequence[LLMUsage]) -> dict[str, Any]:
    """Суммы по виткам хода. Пустой расход — нули, а не ``None``: колонки NOT NULL."""
    return {
        "prompt_tokens": sum(int(item.prompt_tokens or 0) for item in usage),
        "cached_tokens": sum(int(item.cached_tokens or 0) for item in usage),
        "candidates_tokens": sum(int(item.candidates_tokens or 0) for item in usage),
        "thoughts_tokens": sum(int(item.thoughts_tokens or 0) for item in usage),
        "latency_ms": sum(int(item.latency_ms or 0) for item in usage),
        "cost_usd": float(sum(float(item.cost_usd or 0.0) for item in usage)),
    }


async def stats(session: AsyncSession, *, since: datetime) -> dict[str, Any]:
    """Сводка вызовов за период: сколько, сколько с ошибкой, во что обошлись."""
    row = (
        await session.execute(
            sa.select(
                sa.func.count(LLMCall.id),
                sa.func.sum(sa.case((LLMCall.error.is_not(None), 1), else_=0)),
                sa.func.coalesce(sa.func.sum(LLMCall.cost_usd), 0.0),
            ).where(LLMCall.created_at >= since)
        )
    ).one()
    return {"calls": int(row[0] or 0), "errors": int(row[1] or 0), "cost_usd": float(row[2] or 0.0)}
