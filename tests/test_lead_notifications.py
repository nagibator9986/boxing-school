"""Готовые записи уходят администратору и в рабочий чат школы.

Владелец 22.09.2026: «чтобы он отписался Зарине — администратору — и в группу…
в одностороннем, просто бот писал: „Записался на такое-то, такой-то зал“».
Номера здесь вымышленные: настоящие живут в настройках владельца.
"""

from __future__ import annotations

import sqlalchemy as sa

from app.admin.runtime_settings import RuntimeSettings, notify_targets
from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
from app.types import ChannelKind, Language, OutboundKind, OutboundMessage

from tests.conftest import RecordingQueue, webhook_payload

ADMIN = "77000000077"
ZARINA = "77000000078"
GROUP = "77000000077-1600000000@g.us"
CHANNEL = "00000000-0000-4000-8000-000000000002"
RAHAT = "center_kairbekova_24"


async def _deps(kb, state, sessionmaker, settings, llm, runtime: RuntimeSettings) -> PipelineDeps:
    kb_loader.swap(kb)
    return PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=llm, kb=kb_loader.get_snapshot,
        queue=RecordingQueue(),
        settings=settings.model_copy(update={"wazzup_channel_id_whatsapp": CHANNEL}),
        runtime=lambda: runtime,
    )


async def _sent(sessionmaker) -> dict[str, list[str]]:
    from app.storage.models import OutboxMessage

    async with sessionmaker() as db:
        payloads = (await db.execute(sa.select(OutboxMessage.payload))).scalars().all()
    result: dict[str, list[str]] = {}
    for payload in payloads:
        if payload and payload.get("kind") == OutboundKind.MANAGER_CARD.value:
            result.setdefault(str(payload.get("chat_id")), []).append(str(payload.get("text") or ""))
    return result


def _booking_llm() -> FakeLLMClient:
    return FakeLLMClient([
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Айназаров Али", "child_age": 8, "gym_id": RAHAT, "parent_agreed": True,
            "session_time": "19:00", "discipline": "boxing",
        })),
        FakeTurn.answer("Готово"),
    ])


def test_targets_are_read_as_numbers_and_group_chats() -> None:
    assert notify_targets("+7 700 000 00 77, 8 700 000 00 78") == (ADMIN, ZARINA)
    assert notify_targets(f"{GROUP}\n+7 700 000 00 77") == (GROUP, ADMIN)
    assert notify_targets("  ") == ()
    assert notify_targets("+7 700 000 00 77, 7 700 000 00 77") == (ADMIN,), "повтор не нужен"


async def test_booking_card_reaches_every_administrator(kb, state, sessionmaker, settings) -> None:
    runtime = RuntimeSettings.from_values({"lead_notify_target": "+7 700 000 00 77, +7 700 000 00 78"})
    deps = await _deps(kb, state, sessionmaker, settings, _booking_llm(), runtime)

    await process_inbound(deps, webhook_payload("ln-1", "Айназаров Али, 8 лет, на бокс в 19:00", chat_id="77015559201"))

    sent = await _sent(sessionmaker)
    assert "НОВАЯ ЗАПИСЬ" in "".join(sent.get(ADMIN, [])), sent
    assert "НОВАЯ ЗАПИСЬ" in "".join(sent.get(ZARINA, [])), sent


async def test_work_chat_gets_one_line_without_the_clients_phone(kb, state, sessionmaker, settings) -> None:
    runtime = RuntimeSettings.from_values(
        {"lead_notify_target": "+7 700 000 00 77", "lead_notify_chat": GROUP}
    )
    deps = await _deps(kb, state, sessionmaker, settings, _booking_llm(), runtime)
    chat = "77015559202"

    await process_inbound(deps, webhook_payload("ln-2", "Айназаров Али, 8 лет, на бокс в 19:00", chat_id=chat))

    sent = await _sent(sessionmaker)
    short = "".join(sent.get(GROUP, []))
    assert short.startswith("ЗАПИСАЛСЯ: Айназаров Али"), sent
    assert "Центр — у магазина «Рахат»" in short and "19:00" in short
    assert chat not in short and "Телефон" not in short, "телефон клиента в общий чат не уходит"
    assert "НОВАЯ ЗАПИСЬ" in "".join(sent.get(ADMIN, [])), "карточка администратору остаётся полной"


async def test_bot_stays_silent_in_the_work_chats(kb, state, sessionmaker, settings) -> None:
    runtime = RuntimeSettings.from_values(
        {"lead_notify_target": "+7 700 000 00 77, +7 700 000 00 78", "lead_notify_chat": GROUP}
    )
    deps = await _deps(kb, state, sessionmaker, settings, FakeLLMClient([]), runtime)

    for index, chat in enumerate((ADMIN, ZARINA, GROUP), 1):
        decisions = await process_inbound(deps, webhook_payload(f"ln-3-{index}", "принял, позвоню", chat_id=chat))
        assert [d.reason for d in decisions] == ["ignored_number"], (chat, decisions)


def test_a_client_is_not_silenced_by_the_group_chat_id() -> None:
    from app.core import ignore_list

    silent = ignore_list.parse(GROUP)

    assert ignore_list.is_ignored(silent, chat_id=GROUP, phone=None), "сама группа молчит"
    assert not ignore_list.is_ignored(silent, chat_id="71600000000", phone=None), (
        "у клиента совпали последние десять цифр с отметкой времени группы — это не группа"
    )


def test_group_chat_is_sent_with_its_own_chat_type() -> None:
    from app.channels.outbound import build_send_request

    group = OutboundMessage(
        conversation_id=None, channel_id="wa", channel=ChannelKind.WHATSAPP, chat_id=GROUP,
        lang=Language.RU, kind=OutboundKind.MANAGER_CARD, text="ЗАПИСАЛСЯ: Айназаров Али",
    )
    personal = group.model_copy(update={"chat_id": ADMIN})

    assert build_send_request(group).chatType == "whatsgroup"
    assert build_send_request(personal).chatType == "whatsapp"


def test_group_chat_id_is_not_turned_into_a_phone(settings) -> None:
    from app.notify.manager import manager_target

    configured = settings.model_copy(update={"wazzup_channel_id_whatsapp": CHANNEL})

    assert manager_target(configured, to=GROUP)[2] == GROUP
    assert manager_target(configured, to="8 700 000 00 77")[2] == ADMIN
