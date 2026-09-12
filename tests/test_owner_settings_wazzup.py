"""Настройки владельца из CRM в WhatsApp и Instagram.

Разбор 11.09.2026: «Номер для заявок», тексты автоответов и список номеров без
ответа читал только Telegram-бот. Сборка зависимостей для Wazzup их не
подключала, и в WhatsApp бот работал так, будто CRM пуста. Номера здесь
вымышленные: настоящий номер для заявок живёт только в настройках владельца.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app import deps as app_deps
from app.admin.admin_store import AdminStore
from app.admin.runtime_settings import RuntimeSettings
from app.channels import normalize
from app.channels.wazzup_schemas import parse_webhook
from app.core.pipeline import _owner_settings, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeLLMClient
from app.types import ChannelKind, EscalationReason, Language, OutboundKind, OutboundMessage, WazzupSpamError

from tests.conftest import RecordingQueue, webhook_payload

LEADS_NUMBER = "77000000077"
CHANNEL = "00000000-0000-4000-8000-000000000002"


def _store(path, **values: str) -> None:
    store = AdminStore(path)
    try:
        for key, value in values.items():
            store.set(key, value)
    finally:
        store.close()


def _production_deps(kb, state, sessionmaker, settings, admin_db):
    kb_loader.swap(kb)
    configured = settings.model_copy(update={"admin_db_path": str(admin_db), "wazzup_channel_id_whatsapp": CHANNEL})
    return app_deps._build_pipeline_deps(
        settings=configured, sessionmaker=sessionmaker, state=state, llm=FakeLLMClient([]), queue=RecordingQueue()
    )


async def test_production_pipeline_reads_the_number_for_leads_from_crm(kb, state, sessionmaker, settings, tmp_path) -> None:
    _store(tmp_path / "admin.db", lead_notify_target="+7 700 000 00 77")

    deps = _production_deps(kb, state, sessionmaker, settings, tmp_path / "admin.db")

    assert deps.runtime is not None
    assert deps.runtime() is deps.runtime(), "настройки владельца читаются через кеш, а не на каждый вызов"
    assert _owner_settings(deps).manager_notify_target == LEADS_NUMBER


async def test_auto_reply_text_saved_in_crm_works_in_whatsapp(kb, state, sessionmaker, settings, tmp_path) -> None:
    """Владелец вписал текст автоответа в CRM — эхо этого текста больше не «оператор»."""
    _store(tmp_path / "admin.db", auto_greeting_texts="Спасибо, что написали в школу")
    deps = _production_deps(kb, state, sessionmaker, settings, tmp_path / "admin.db")

    decisions = await process_inbound(
        deps, webhook_payload("crm-echo-1", "Спасибо, что написали в школу! Скоро ответим.", chat_id="77015556001", is_echo=True)
    )

    assert [d.reason for d in decisions] == ["auto_reply"]


async def test_background_escalation_card_goes_to_the_number_from_crm(settings, monkeypatch) -> None:
    from types import SimpleNamespace

    from app.workers import tasks_inbound

    sent: list[OutboundMessage] = []

    async def capture(deps, message):
        sent.append(message)

    monkeypatch.setattr(tasks_inbound, "_enqueue", capture)
    deps = SimpleNamespace(
        settings=settings.model_copy(update={"wazzup_channel_id_whatsapp": CHANNEL}),
        runtime=lambda: RuntimeSettings.from_values({"lead_notify_target": "+7 700 000 00 77"}),
    )
    now = datetime.now(UTC)
    payload = parse_webhook(webhook_payload("bg-1", "Сколько стоит?", chat_id="77015556002"))
    inbound = normalize.to_inbound_batch(payload, received_at=now)[0]

    await tasks_inbound._escalate(deps, inbound, lang=Language.RU, reason=EscalationReason.LLM_FAILURE, now=now)

    assert sent and sent[0].chat_id == LEADS_NUMBER


def test_wazzup_starts_when_the_number_for_leads_is_only_in_crm(settings, tmp_path) -> None:
    from app.asgi import wazzup_ready

    prod = settings.model_copy(update={
        "app_env": "prod", "wazzup_api_key": "key", "manager_notify_target": "",
        "admin_db_path": str(tmp_path / "admin.db"),
    })
    _, without = wazzup_ready(prod)
    assert any(reason.startswith("MANAGER_NOTIFY_TARGET") for reason in without)

    _store(tmp_path / "admin.db", lead_notify_target="+7 700 000 00 77")
    _, with_crm = wazzup_ready(prod)
    assert not any(reason.startswith("MANAGER_NOTIFY_TARGET") for reason in with_crm)


def test_number_with_eight_in_server_settings_is_the_same_number(settings) -> None:
    from app.notify.manager import manager_target

    configured = settings.model_copy(update={"manager_notify_target": "8 700 000 00 77", "wazzup_channel_id_whatsapp": CHANNEL})

    assert manager_target(configured)[2] == LEADS_NUMBER


async def test_undelivered_lead_card_does_not_stop_the_school_channel(monkeypatch) -> None:
    import app.workers.tasks_outbound as outbound

    stopped: list[str] = []
    alerts: list[str] = []

    async def stop(deps, channel_id):
        stopped.append(channel_id)

    async def alert(deps, text, *, code):
        alerts.append(code)

    monkeypatch.setattr(outbound, "_stop_channel", stop)
    monkeypatch.setattr(outbound, "_alert", alert)
    card = OutboundMessage(
        conversation_id=None, channel_id="school", channel=ChannelKind.WHATSAPP, chat_id=LEADS_NUMBER,
        lang=Language.RU, kind=OutboundKind.MANAGER_CARD, text="НОВАЯ ЗАПИСЬ",
    )

    await outbound._react_terminal(
        None, card, WazzupSpamError.__new__(WazzupSpamError), code="MESSAGES_IS_SPAM", reason="spam", conversation_id=None
    )

    assert stopped == [] and alerts == ["manager_card_undelivered"]


async def test_spam_on_a_client_reply_still_stops_the_channel(monkeypatch) -> None:
    """Защита канала для ответов клиентам остаётся прежней."""
    import app.workers.tasks_outbound as outbound

    stopped: list[str] = []

    async def stop(deps, channel_id):
        stopped.append(channel_id)

    async def alert(deps, text, *, code):
        return None

    monkeypatch.setattr(outbound, "_stop_channel", stop)
    monkeypatch.setattr(outbound, "_alert", alert)
    reply = OutboundMessage(
        conversation_id=None, channel_id="school", channel=ChannelKind.WHATSAPP, chat_id="77015556003",
        lang=Language.RU, kind=OutboundKind.BOT_REPLY, text="Здравствуйте",
    )

    await outbound._react_terminal(
        None, reply, WazzupSpamError.__new__(WazzupSpamError), code="MESSAGES_IS_SPAM", reason="spam", conversation_id=None
    )

    assert stopped == ["school"]
