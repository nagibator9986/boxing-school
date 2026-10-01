"""Напоминания уходят по настройкам владельца из CRM, а не по конфигурации процесса.

01.10.2026 администратор написала боту с номера для заявок раньше, чем его вписали
в CRM: бот ответил ей как клиенту и поставил напоминания. Вписанный потом номер
должен их погасить, как и выключатель «Напоминания» в CRM.
Номера здесь вымышленные: настоящие живут в настройках владельца.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.admin.runtime_settings import RuntimeSettings
from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeLLMClient, FakeTurn
from app.storage.models import Conversation, FollowupTask, OutboxMessage
from app.types import FollowupKind

from tests.conftest import RecordingQueue, webhook_payload

ADMIN = "77000000077"


@pytest.fixture
def owner() -> dict[str, RuntimeSettings]:
    return {"now": RuntimeSettings.from_values({})}


@pytest.fixture
async def deps(kb, state, sessionmaker, settings, owner, monkeypatch) -> PipelineDeps:
    from app.workers import tasks_followup

    # Тихие часы проверяются своими тестами; здесь они сделали бы итог зависимым
    # от времени суток на машине.
    monkeypatch.setattr(tasks_followup, "is_quiet_hours", lambda *a, **k: False)
    kb_loader.swap(kb)
    return PipelineDeps(
        sessionmaker=sessionmaker, state=state,
        llm=FakeLLMClient([FakeTurn.answer("Здравствуйте! Чем помочь?")]),
        kb=kb_loader.get_snapshot, queue=RecordingQueue(), settings=settings,
        runtime=lambda: owner["now"],
    )


async def _remind(deps: PipelineDeps) -> tuple[str, int]:
    """Ставит напоминание в диалог и запускает воркер. ``(состояние задачи, новых исходящих)``."""
    from app.workers import tasks_followup

    async with deps.sessionmaker() as db:
        conv = (await db.execute(sa.select(Conversation))).scalars().one()
        before = len((await db.execute(sa.select(OutboxMessage.id))).all())
        task = FollowupTask(
            conversation_id=conv.id, kind=FollowupKind.FU_VALUE.value,
            run_at=datetime.now(tz=timezone.utc), state="pending", attempt=0,
        )
        db.add(task)
        await db.commit()
        task_id = task.id

    await tasks_followup.send_followup_job({"deps": deps, "correlation_id": "test"}, str(task_id))

    async with deps.sessionmaker() as db:
        state = (await db.get(FollowupTask, task_id)).state
        after = len((await db.execute(sa.select(OutboxMessage.id))).all())
    return state, after - before


async def test_reminder_goes_out_when_nothing_forbids_it(deps) -> None:
    """Контроль: без запретов напоминание уходит — иначе тесты ниже проверяли бы пустоту."""
    await process_inbound(deps, webhook_payload("fo-1", "Здравствуйте", chat_id=ADMIN))

    state, sent = await _remind(deps)

    assert (state, sent) == ("sent", 1)


async def test_no_reminder_to_a_number_added_to_the_leads_setting_later(deps, owner) -> None:
    await process_inbound(deps, webhook_payload("fo-2", "Здравствуйте", chat_id=ADMIN))
    # Номер вторым в списке: первый попадает в запрет ещё и как адрес карточек,
    # и тест с одним номером не заметил бы, что остальные адресаты выпали.
    owner["now"] = RuntimeSettings.from_values(
        {"lead_notify_target": "+7 700 000 00 01, +7 700 000 00 77"}
    )

    state, sent = await _remind(deps)

    assert (state, sent) == ("cancelled", 0), "номер для заявок не получает напоминаний клиенту"


async def test_reminders_switched_off_in_crm_are_not_sent(deps, owner) -> None:
    await process_inbound(deps, webhook_payload("fo-3", "Здравствуйте", chat_id=ADMIN))
    owner["now"] = RuntimeSettings.from_values({"followup_enabled": "off"})

    state, sent = await _remind(deps)

    assert (state, sent) == ("cancelled", 0), "выключатель в CRM обязан действовать на рассылку"
