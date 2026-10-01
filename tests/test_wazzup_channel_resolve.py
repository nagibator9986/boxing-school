"""Канал школы для карточек — такой, какой он есть в кабинете Wazzup.

01.10.2026, журнал Railway: ``channels_refreshed total=3 broken=0`` и тут же
``readyz … channels=false``. Wazzup работал, клиентам бот отвечал (канал берётся
из входящего сообщения), но ID из ``WAZZUP_CHANNEL_ID_WHATSAPP`` не совпал ни с
одним из трёх каналов кабинета — карточки администратору ушли бы в никуда.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.notify.manager as manager
from app.notify.manager import build_manager_message, resolve_channel_id
from app.types import ChannelKind, ChannelState, ManagerCard, ManagerCardKind

WA = "a1b2c3d4-1111-4111-8111-abcdef111111"
IG = "22222222-2222-4222-8222-222222222222"
TG = "33333333-3333-4333-8333-333333333333"
WRONG = "00000000-0000-4000-8000-000000000999"

ACCOUNT = (
    ChannelState(channel_id=WA, transport="whatsapp", plain_id="77000000050", state="active", is_active=True),
    ChannelState(channel_id=IG, transport="instagram", plain_id="school", state="active", is_active=True),
    ChannelState(channel_id=TG, transport="telegram", plain_id="bot", state="active", is_active=True),
)


@pytest.fixture(autouse=True)
def _forget_account():
    from app.workers import tasks_outbound

    yield
    manager.remember_account_channels(())
    tasks_outbound._REPORTED_CHANNELS.clear()


@pytest.mark.parametrize(
    ("configured", "expected", "how"),
    [
        (WA, WA, "configured"),
        (f"  {WA.upper()} ", WA, "configured"),          # пробелы и регистр при копировании
        ("+7 700 000 00 50", WA, "by_phone"),             # вписали номер WhatsApp школы
        (WRONG, WA, "only_active"),                       # чужой ID, а WhatsApp в кабинете один
        ("", WA, "only_active"),                          # переменную не задали вовсе
    ],
)
def test_school_whatsapp_channel_is_found_in_the_account(configured, expected, how) -> None:
    assert resolve_channel_id(configured, ChannelKind.WHATSAPP, ACCOUNT) == (expected, how)


def test_two_whatsapp_channels_are_not_guessed() -> None:
    """Рабочих WhatsApp два — выбирать за владельца нельзя: остаётся настройка."""
    second = ChannelState(channel_id=TG, transport="whatsapp", plain_id="77000000051", state="active", is_active=True)

    assert resolve_channel_id(WRONG, ChannelKind.WHATSAPP, (ACCOUNT[0], second)) == (WRONG, "missing")


def test_without_the_account_list_the_setting_is_trusted() -> None:
    assert resolve_channel_id(WRONG, ChannelKind.WHATSAPP, ()) == (WRONG, "unchecked")


def test_manager_card_goes_through_the_real_channel(settings) -> None:
    configured = settings.model_copy(update={
        "wazzup_channel_id_whatsapp": WRONG, "manager_notify_target": "+7 700 000 00 77",
    })
    card = ManagerCard(kind=ManagerCardKind.LEAD, text="НОВАЯ ЗАПИСЬ НА ПРОБНОЕ")

    manager.remember_account_channels(ACCOUNT)
    message = build_manager_message(card, settings=configured)

    assert message is not None and message.channel_id == WA


class _Wazzup:
    async def get_channels(self) -> list[ChannelState]:
        return list(ACCOUNT)


async def test_channel_check_remembers_the_account_and_reports_the_right_id(settings, state, monkeypatch) -> None:
    from app.workers import tasks_outbound

    warnings: list[dict] = []
    monkeypatch.setattr(tasks_outbound.log, "warning", lambda event, **kw: warnings.append({"event": event, **kw}))
    configured = settings.model_copy(update={"wazzup_channel_id_whatsapp": WRONG})
    deps = SimpleNamespace(settings=configured, state=state)

    await tasks_outbound.refresh_channels_cron({"deps": deps, "wazzup": _Wazzup()})
    await tasks_outbound.refresh_channels_cron({"deps": deps, "wazzup": _Wazzup()})

    assert manager._ACCOUNT_CHANNELS == ACCOUNT, "карточки должны знать каналы кабинета"
    mismatch = [w for w in warnings if w["event"] == "wazzup_channel_id_mismatch"]
    assert len(mismatch) == 1, "раз на процесс, а не раз в 15 минут"
    assert mismatch[0]["using"] == WA and any(WA in line for line in mismatch[0]["account_channels"])


async def test_readiness_accepts_the_school_channel_found_in_the_account(settings, monkeypatch) -> None:
    from app.api import health

    monkeypatch.setattr(health, "runtime_or_none", lambda: SimpleNamespace(wazzup=_Wazzup()))
    monkeypatch.setattr(health, "_channels_cache", (0.0, False))
    configured = settings.model_copy(update={"wazzup_api_key": "x", "wazzup_channel_id_whatsapp": WRONG})

    assert await health._check_channels(configured) is True
