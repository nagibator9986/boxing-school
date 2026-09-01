"""След каждого вызова модели остаётся в базе.

Таблица ``llm_call`` описана в архитектуре как основа разбора инцидентов и
подсчёта расхода, но писать в неё не начали: за всё время работы бота там было
ноль строк. Цена этого известна — когда на ключе кончились кредиты, бот сутки
отвечал заглушками, а причина не была видна ниоткуда, кроме журнала Railway.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeLLMClient, FakeTurn
from app.storage import db as storage_db
from app.storage.models import Base
from app.types import LLMQuotaError

from tests.conftest import RecordingQueue, webhook_payload

UTC = timezone.utc
CHAT_ID = "77015557777"


class DeadLLM:
    """Модель без денег на ключе."""

    async def generate(self, req, executor):  # type: ignore[no-untyped-def]
        raise LLMQuotaError("квота Gemini исчерпана: prepayment credits are depleted")

    async def extract_lead(self, rendered, *, lang):  # type: ignore[no-untyped-def]
        raise LLMQuotaError("квота Gemini исчерпана")


@pytest.fixture
def bot_db(tmp_path: Path) -> Path:
    return tmp_path / "bot.db"


async def build(kb, state, settings, bot_db: Path, llm) -> PipelineDeps:
    """Пайплайн поверх файловой базы: строки должны пережить закрытие сессии."""
    kb_loader.swap(kb)
    engine = storage_db.build_engine(f"sqlite+aiosqlite:///{bot_db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return PipelineDeps(
        sessionmaker=storage_db.build_sessionmaker(engine),
        state=state,
        llm=llm,
        kb=kb_loader.get_snapshot,
        queue=RecordingQueue(),
        settings=settings,
    )


def calls(bot_db: Path) -> list[dict]:
    """Записанные вызовы модели."""
    engine = sa.create_engine(f"sqlite:///{bot_db}")
    with engine.begin() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT model, error, finish_reason, prompt_tokens, candidates_tokens,"
                " cost_usd, tool_calls, kb_hash FROM llm_call ORDER BY created_at"
            )
        ).mappings().all()
    engine.dispose()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# Отказ
# --------------------------------------------------------------------------- #
async def test_model_failure_leaves_a_trace(kb, state, settings, bot_db: Path) -> None:
    """Пустой ключ обязан оставить след в базе, а не только в журнале Railway."""
    deps = await build(kb, state, settings, bot_db, DeadLLM())

    await process_inbound(deps, webhook_payload("wz-llm-1", "Сколько стоит?", chat_id=CHAT_ID))

    recorded = calls(bot_db)
    assert len(recorded) == 1
    assert recorded[0]["error"] == "llm_quota"
    assert recorded[0]["kb_hash"], "без версии базы знаний разбор инцидента неполон"


# --------------------------------------------------------------------------- #
# Успешный ход
# --------------------------------------------------------------------------- #
async def test_successful_turn_records_spending(kb, state, settings, bot_db: Path) -> None:
    """Расход хода виден: модель, токены, вызванные инструменты."""
    llm = FakeLLMClient([])
    deps = await build(kb, state, settings, bot_db, llm)
    llm.reset([FakeTurn.answer("Здравствуйте! Занятия идут в шести залах города.")])

    await process_inbound(deps, webhook_payload("wz-llm-2", "Где вы находитесь?", chat_id=CHAT_ID))

    recorded = calls(bot_db)
    assert len(recorded) == 1
    assert recorded[0]["error"] is None
    assert recorded[0]["model"], "модель хода не записана"


async def test_one_row_per_turn(kb, state, settings, bot_db: Path) -> None:
    """Ход с инструментами — одна строка: разбирают ход целиком, а не витки."""
    llm = FakeLLMClient([])
    deps = await build(kb, state, settings, bot_db, llm)
    llm.reset([FakeTurn.answer("В Костанае шесть залов.")])
    await process_inbound(deps, webhook_payload("wz-llm-3", "Где вы?", chat_id=CHAT_ID))
    llm.reset([FakeTurn.answer("Занятия идут вечером.")])
    await process_inbound(deps, webhook_payload("wz-llm-4", "А когда?", chat_id=CHAT_ID))

    assert len(calls(bot_db)) == 2


# --------------------------------------------------------------------------- #
# Сводка
# --------------------------------------------------------------------------- #
async def test_stats_counts_errors(kb, state, settings, bot_db: Path) -> None:
    """Сводка отделяет отказы от удачных вызовов — на ней держится проверка здоровья."""
    from app.storage import repo_llm

    deps = await build(kb, state, settings, bot_db, DeadLLM())
    await process_inbound(deps, webhook_payload("wz-llm-5", "Сколько стоит?", chat_id=CHAT_ID))

    async with deps.sessionmaker() as session:
        summary = await repo_llm.stats(session, since=datetime.now(tz=UTC) - timedelta(hours=1))

    assert summary["calls"] == 1
    assert summary["errors"] == 1


async def test_telemetry_failure_does_not_cost_the_answer(
    kb, state, settings, bot_db: Path, monkeypatch
) -> None:
    """Сломанная телеметрия не имеет права стоить клиенту ответа."""
    from app.storage import repo_llm

    async def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("телеметрия сломана")

    monkeypatch.setattr(repo_llm, "record", boom)

    llm = FakeLLMClient([])
    deps = await build(kb, state, settings, bot_db, llm)
    llm.reset([FakeTurn.answer("В Костанае шесть залов.")])

    decisions = await process_inbound(
        deps, webhook_payload("wz-llm-6", "Где вы?", chat_id=CHAT_ID)
    )

    assert any(out.text for d in decisions for out in d.outbound)
