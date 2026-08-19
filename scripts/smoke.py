#!/usr/bin/env python
"""Дымовой прогон пайплайна: FakeLLMClient + sqlite, без единого сетевого вызова.

Скрипт поднимает настоящий :func:`app.core.pipeline.process_inbound` на временной
sqlite-базе (схема накатывается моделями, alembic здесь не нужен), скармливает ему
синтетический вебхук Wazzup и печатает, что бот собрался ответить и какие
инструменты дёрнул.

Проверки, которые он прогоняет:

1. обычная реплика «Здравствуйте, сколько стоит?» → ответ + tool-вызовы;
2. эхо-фильтр: ``isEcho=true`` не порождает ответа;
3. дедуп: один ``messageId`` дважды → ровно один ответ.

Запуск::

    .venv/bin/python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_DB_PATH = Path(tempfile.mkdtemp(prefix="ainazarov-smoke-")) / "smoke.db"

# Окружение выставляем ДО импорта app.config: Settings читает его на создании.
os.environ.update(
    APP_ENV="local",
    DATABASE_URL=f"sqlite+aiosqlite:///{_DB_PATH}",
    STATE_BACKEND="memory",
    REDIS_URL="",
    INLINE_WORKER="false",
    GEMINI_API_KEY="",
    WAZZUP_API_KEY="smoke-key",
    WAZZUP_WEBHOOK_SECRET="smoke-secret-smoke-secret-1234",
    LOG_LEVEL="WARNING",
)

from app.config import get_settings  # noqa: E402
from app.core.pipeline import PipelineDeps, process_inbound  # noqa: E402
from app.kb import loader as kb_loader  # noqa: E402
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn  # noqa: E402
from app.storage import db as storage_db  # noqa: E402
from app.storage import repo_outbox  # noqa: E402
from app.storage.models import Base  # noqa: E402
from app.storage.state import build_state_store  # noqa: E402
from app.types import PipelineDecision  # noqa: E402

CHANNEL_ID = "11111111-1111-1111-1111-111111111111"
CHAT_ID = "77012345678"


class RecordingQueue:
    """Очередь-регистратор: складывает задачи в список вместо Redis.

    Дымовой прогон проверяет решения пайплайна, а не доставку в Wazzup, поэтому
    настоящая ARQ-очередь здесь только мешала бы: она полезла бы в сеть.
    """

    def __init__(self) -> None:
        self.inbound: list[dict[str, Any]] = []
        self.outbox: list[tuple[UUID, int]] = []
        self.followups: list[tuple[UUID, datetime]] = []

    async def enqueue_inbound(self, payload: dict[str, Any]) -> str:
        """Регистрирует входящий payload."""
        self.inbound.append(payload)
        return str(uuid4())

    async def enqueue_outbox(self, outbox_id: UUID, *, delay_ms: int = 0) -> str:
        """Регистрирует задачу отправки строки outbox."""
        self.outbox.append((outbox_id, delay_ms))
        return str(uuid4())

    async def enqueue_followup(self, task_id: UUID, *, run_at: datetime) -> str:
        """Регистрирует запланированный follow-up."""
        self.followups.append((task_id, run_at))
        return str(uuid4())

    async def startup(self) -> None:
        """Ничего не открывает."""

    async def shutdown(self) -> None:
        """Ничего не закрывает."""


def webhook(
    message_id: str, text: str, *, is_echo: bool = False, chat_id: str = CHAT_ID
) -> dict[str, Any]:
    """Синтетический payload вебхука Wazzup с одним сообщением.

    Каждый сценарий берёт свой ``chat_id``: эхо-тест ставит паузу «пришёл оператор»,
    и в общем чате она погасила бы следующие сценарии.
    """
    return {
        "messages": [
            {
                "messageId": message_id,
                "channelId": CHANNEL_ID,
                "chatType": "whatsapp",
                "chatId": chat_id,
                "dateTime": "2026-08-10T09:00:00.000Z",
                "type": "text",
                "isEcho": is_echo,
                "status": "inbound" if not is_echo else "sent",
                "text": text,
                "contact": {"name": "Айгуль", "avatarUri": None},
            }
        ]
    }


def script() -> Sequence[FakeTurn]:
    """Скрипт модели: сперва детерминированный расчёт и список залов, потом ответ.

    Имена и аргументы инструментов — из реального реестра ``app.tools.registry``;
    цифры в ответе обязаны совпадать с выдачей ``calculate_price``, иначе
    пост-фильтр (postcheck) справедливо снимет ответ как «деньги от себя».
    """
    return [
        FakeTurn.tool(
            FakeCall(
                "calculate_price",
                {"scope": "city", "plan": "standard", "children_count": 1},
            ),
            FakeCall("get_gyms", {"scope": "city"}),
        ),
        FakeTurn.answer(
            "Здравствуйте! Стандартный абонемент в Костанае — 25 000 ₸ за 12 занятий на месяц. "
            "Первое занятие бесплатное. В каком районе вам удобнее заниматься?"
        ),
    ]


async def build_deps(llm: FakeLLMClient) -> PipelineDeps:
    """Собирает PipelineDeps на временной sqlite и памяти вместо Redis."""
    settings = get_settings()
    engine = storage_db.build_engine()
    storage_db.set_engine(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    snapshot = await kb_loader.load(
        settings.kb_dir,
        media_dir=settings.media_dir,
        schema_version=settings.kb_schema_version,
    )
    kb_loader.swap(snapshot)

    queue = RecordingQueue()
    deps = PipelineDeps(
        sessionmaker=storage_db.get_sessionmaker(),
        state=build_state_store(settings),
        llm=llm,
        kb=kb_loader.get_snapshot,
        queue=queue,
        settings=settings,
    )
    await queue.startup()
    return deps


def show(title: str, decisions: Sequence[PipelineDecision], llm: FakeLLMClient) -> None:
    """Печатает разбор решений пайплайна по одному вебхуку."""
    print(f"\n=== {title} ===")
    print(f"решений: {len(decisions)}")
    for d in decisions:
        print(f"  action        : {d.action}")
        print(f"  reason        : {d.reason}")
        print(f"  lang          : {d.lang}")
        print(f"  conv_key      : {d.conv_key}")
        print(f"  guard_flags   : {[f for f in d.guard_flags] or '—'}")
        print(f"  postcheck     : {d.postcheck_fail or 'ok'}")
        for inv in d.invocations:
            status = "ok" if inv.result.ok else f"ERR {inv.result.error}"
            print(f"  tool          : {inv.name}({inv.args}) -> {status}")
        for out in d.outbound:
            print(f"  ОТВЕТ КЛИЕНТУ : {out.text!r}")
        if not d.outbound:
            print("  ОТВЕТ КЛИЕНТУ : (нет)")
    print(f"  вызовов LLM   : {llm.generate_calls}")


async def main() -> int:
    """Прогоняет три сценария и возвращает код выхода."""
    llm = FakeLLMClient(script())
    deps = await build_deps(llm)
    failures: list[str] = []

    # 1. Обычная реплика.
    decisions = await process_inbound(deps, webhook("wz-msg-1", "Здравствуйте, сколько стоит?"))
    show("1. Обычная реплика «Здравствуйте, сколько стоит?»", decisions, llm)
    replies = [o for d in decisions for o in d.outbound]
    tools = [i.name for d in decisions for i in d.invocations]
    if not replies:
        failures.append("сценарий 1: бот не собрал ни одного ответа")
    if "calculate_price" not in tools:
        failures.append(f"сценарий 1: calculate_price не вызван, вызваны {tools}")
    bad = [i for d in decisions for i in d.invocations if not i.result.ok]
    if bad:
        failures.append(f"сценарий 1: инструменты с ошибкой: {[(i.name, i.result.error) for i in bad]}")
    if any(d.postcheck_fail for d in decisions):
        failures.append("сценарий 1: пост-фильтр снял ответ")

    # 2. Эхо-фильтр.
    llm.reset(script())
    decisions = await process_inbound(
        deps,
        webhook("wz-echo-1", "Это написал оператор руками", is_echo=True, chat_id="77010000002"),
    )
    show("2. Эхо-фильтр (isEcho=true)", decisions, llm)
    if any(d.outbound for d in decisions):
        failures.append("сценарий 2: эхо породило ответ клиенту")
    if llm.generate_calls:
        failures.append("сценарий 2: эхо дошло до LLM")

    # 3. Дедуп: один и тот же messageId дважды.
    llm.reset(script())
    dup = webhook("wz-dup-1", "Сколько стоит абонемент?", chat_id="77010000003")
    first = await process_inbound(deps, dup)
    second = await process_inbound(deps, dup)
    show("3a. Дедуп, первая доставка", first, llm)
    show("3b. Дедуп, повторная доставка того же messageId", second, llm)
    total = sum(len(d.outbound) for d in first) + sum(len(d.outbound) for d in second)
    if total != 1:
        failures.append(f"сценарий 3: ответов {total}, ожидался ровно 1")

    # 4. Собственное эхо бота: пришло с messageId нашей же строки outbox.
    #    Оно обязано быть отброшено как echo_own, а НЕ принято за оператора —
    #    иначе бот ставил бы себе паузу после каждого своего ответа.
    queue: RecordingQueue = deps.queue  # type: ignore[assignment]
    own_id = "wz-own-echo-1"
    outbox_id, _ = queue.outbox[0]
    async with deps.sessionmaker() as db:
        await repo_outbox.claim(db, outbox_id)
        await repo_outbox.mark_sent(db, outbox_id, wazzup_message_id=own_id)
        await db.commit()

    llm.reset(script())
    decisions = await process_inbound(
        deps, webhook(own_id, "Здравствуйте! Стандартный абонемент...", is_echo=True)
    )
    show("4. Собственное эхо бота (messageId из нашего outbox)", decisions, llm)
    if any(d.reason != "echo_own" for d in decisions):
        failures.append(
            f"сценарий 4: своё эхо не распознано, reason={[d.reason for d in decisions]} "
            "(бот поставил бы себе паузу после каждого ответа)"
        )

    await deps.queue.shutdown()
    await storage_db.dispose_engine()

    print("\n" + "=" * 60)
    if failures:
        print("SMOKE: ПРОВАЛ")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(
        "SMOKE: OK — ответ собран, эхо оператора молчит, дедуп держит один ответ, "
        "своё эхо отброшено"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
