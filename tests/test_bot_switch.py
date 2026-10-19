"""Выключатель бота — владелец 19.10.2026: «пока что отключи бота».

Галочка «Бот отвечает клиентам» в настройках CRM. В базе настроек она по умолчанию
выключена: выпуск сам гасит бота, а включают его из CRM, без нового выпуска.
Выключенный бот молчит и не шлёт напоминаний, но реплики клиентов сохраняет.
"""

from __future__ import annotations

from pathlib import Path

from app.admin.admin_store import AdminStore
from app.admin.runtime_settings import RuntimeSettings, load_runtime_settings
from app.config import Settings
from app.core import session as conv_session
from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeLLMClient, FakeTurn
from app.storage import repo_conversation
from app.types import DecisionAction

from tests.conftest import RecordingQueue, webhook_payload

OFF = RuntimeSettings.from_values({"bot_enabled": "off"})
ON = RuntimeSettings.from_values({"bot_enabled": "on"})


def test_the_switch_is_off_until_someone_turns_it_on(tmp_path: Path) -> None:
    path = tmp_path / "admin.db"

    assert load_runtime_settings(path).bot_enabled is False, "выпуск гасит бота сам"
    store = AdminStore(path)
    store.set("bot_enabled", "on")
    store.close()
    assert load_runtime_settings(path).bot_enabled is True


def test_settings_built_without_the_switch_keep_the_bot_on() -> None:
    assert RuntimeSettings.from_values({}).bot_enabled is True
    assert RuntimeSettings().bot_enabled is True


def test_a_switched_off_bot_sends_no_reminders() -> None:
    base = Settings(wazzup_api_key="x", wazzup_webhook_secret="y" * 24, gemini_api_key="z")

    assert OFF.apply_to(base).followup_enabled is False
    assert ON.apply_to(base).followup_enabled is True
    assert RuntimeSettings.from_values({"bot_enabled": "on", "followup_enabled": "off"}).apply_to(
        base
    ).followup_enabled is False


def _deps(kb, state, sessionmaker, settings, owner: dict[str, RuntimeSettings]) -> PipelineDeps:
    kb_loader.swap(kb)
    return PipelineDeps(
        sessionmaker=sessionmaker, state=state, kb=kb_loader.get_snapshot, queue=RecordingQueue(),
        settings=settings, llm=FakeLLMClient([]), runtime=lambda: owner["now"],
    )


async def test_a_switched_off_bot_is_silent_but_keeps_the_message(kb, state, sessionmaker, settings) -> None:
    owner = {"now": OFF}
    deps = _deps(kb, state, sessionmaker, settings, owner)
    chat = "77015551900"

    decisions = await process_inbound(deps, webhook_payload(f"{chat}-1", "Сколько стоит абонемент?", chat_id=chat))

    assert [(d.action, d.reason) for d in decisions] == [(DecisionAction.SILENT, "bot_switched_off")]
    assert not any(d.outbound for d in decisions)
    async with sessionmaker() as db:
        conv = await repo_conversation.get_by_id(db, decisions[0].conversation_id)
        history = await conv_session.load_history(db, conv, max_turns=10)
    assert "Сколько стоит абонемент?" in str(history), "реплика клиента сохраняется для CRM и модели"

    owner["now"] = ON
    deps.llm.reset([FakeTurn.answer("Абонемент стоит 25 000 ₸. Сколько лет ребёнку?")])
    decisions = await process_inbound(deps, webhook_payload(f"{chat}-2", "Здравствуйте, сколько стоит?", chat_id=chat))

    assert decisions[0].action is not DecisionAction.SILENT, "включили — бот снова отвечает"
    assert any(d.outbound for d in decisions)
