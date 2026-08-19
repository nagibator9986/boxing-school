"""Нормализация вебхука Wazzup: эхо-фильтр и детекция входа оператора.

Два отказа, которые здесь ловятся, стоят дороже всех остальных в проекте:

* **эхо не отфильтровано** — бот отвечает сам себе, каждый его ответ приходит
  обратно вебхуком, и цикл не останавливается никогда;
* **вход оператора не распознан** — живой человек пишет клиенту, бот пишет
  поверх него, клиент получает два разных ответа на один вопрос.

Отдельного события «оператор вошёл в диалог» в API v3 нет, признака два, и они
могут разойтись. Расхождение обязано быть видимым, а не проглоченным.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.channels.normalize import (
    channel_from_chat_type,
    echo_signal_mismatch,
    echo_signals,
    is_active_state,
    is_client_inbound,
    is_operator_echo,
    parse_datetime,
    parse_datetime_safe,
    parse_msg_type,
    parse_status,
    phone_from_chat_id,
    to_inbound,
    to_inbound_batch,
)
from app.channels.wazzup_schemas import WzMessage, parse_webhook
from app.types import Author, ChannelKind, Direction, MessageStatus, MsgType

RECEIVED_AT = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
CHANNEL_ID = "11111111-1111-1111-1111-111111111111"


def message(**over) -> WzMessage:
    """Сообщение вебхука с разумными умолчаниями."""
    payload = {
        "messageId": "wz-1",
        "channelId": CHANNEL_ID,
        "chatType": "whatsapp",
        "chatId": "77010000001",
        "dateTime": "2026-08-10T09:00:00.000Z",
        "type": "text",
        "isEcho": False,
        "status": "inbound",
        "text": "Здравствуйте",
    }
    payload.update(over)
    return WzMessage.model_validate(payload)


def inbound(**over):
    """Нормализованное входящее из сообщения вебхука."""
    return to_inbound(message(**over), received_at=RECEIVED_AT)


# --------------------------------------------------------------------------- #
# Эхо-фильтр
# --------------------------------------------------------------------------- #
def test_client_message_is_inbound() -> None:
    """Настоящее входящее: ``isEcho=false`` и ``status=inbound``."""
    msg = inbound()

    assert is_client_inbound(msg) is True
    assert msg.direction is Direction.IN
    assert msg.author is Author.CLIENT


@pytest.mark.parametrize(
    ("is_echo", "status"),
    [
        (True, "sent"),        # наш собственный ответ вернулся эхом
        (True, "delivered"),
        (True, "inbound"),     # эхо важнее статуса
        (False, "sent"),       # не эхо, но и не входящее
        (False, "delivered"),
        (False, "read"),
    ],
)
def test_non_client_messages_are_filtered(is_echo, status) -> None:
    """Всё, что не «эхо=false И статус=inbound», отвечать не нужно.

    Двойная проверка намеренная: не отфильтруешь — бот отвечает сам себе, и
    остановить этот цикл будет нечем.
    """
    assert is_client_inbound(inbound(isEcho=is_echo, status=status)) is False


def test_echo_keeps_direction_out() -> None:
    """Эхо — это исходящее, даже если пришло вебхуком входящих."""
    msg = inbound(isEcho=True, status="sent")

    assert msg.is_echo is True
    assert msg.direction is Direction.OUT


# --------------------------------------------------------------------------- #
# Вход оператора: два признака
# --------------------------------------------------------------------------- #
def test_operator_echo_by_sent_from_app() -> None:
    """Прямой признак: сообщение отправлено из нативного чата Wazzup."""
    msg = inbound(isEcho=True, status="sent", sentFromApp=True)

    assert is_operator_echo(msg, known_outbox=True) is True
    assert msg.author is Author.OPERATOR


def test_operator_echo_by_unknown_outbox() -> None:
    """Основной признак: ``messageId``, которого мы не отправляли."""
    msg = inbound(isEcho=True, status="sent")

    assert is_operator_echo(msg, known_outbox=False) is True


def test_own_echo_is_not_operator() -> None:
    """Своё эхо оператором не считается, иначе бот ставил бы себе паузу всегда."""
    msg = inbound(isEcho=True, status="sent", sentFromApp=False)

    assert is_operator_echo(msg, known_outbox=True) is False
    signals = echo_signals(msg, known_outbox=True)
    assert signals.is_operator is False
    assert signals.mismatch is False


def test_client_message_is_never_operator_echo() -> None:
    """Не эхо — не эхо оператора, каким бы ни было ``sentFromApp``."""
    msg = inbound(sentFromApp=True)

    assert is_operator_echo(msg, known_outbox=False) is False
    assert echo_signals(msg, known_outbox=False).mismatch is False


@pytest.mark.parametrize(
    ("sent_from_app", "known_outbox", "expected_operator", "expected_mismatch"),
    [
        (None, True, False, False),   # поля нет, строка наша — это наш ответ
        (None, False, True, False),   # поля нет, строки нет — писал оператор
        (True, True, True, True),     # «из приложения», но строка наша — расхождение
        (False, False, True, True),   # «не из приложения», но строки нет — расхождение
        (True, False, True, False),
        (False, True, False, False),
    ],
)
def test_echo_signal_mismatch_truth_table(
    sent_from_app, known_outbox, expected_operator, expected_mismatch
) -> None:
    """Расхождение признаков не проглатывается, а выносится наружу метрикой.

    По этому счётчику и закрывается открытый вопрос №12: приходит ли
    ``sentFromApp=false`` для сообщений, отправленных через API.
    """
    msg = inbound(isEcho=True, status="sent", sentFromApp=sent_from_app)
    signals = echo_signals(msg, known_outbox=known_outbox)

    assert signals.is_operator is expected_operator
    assert signals.mismatch is expected_mismatch
    assert echo_signal_mismatch(msg, known_outbox=known_outbox) is expected_mismatch
    # Итог обоих признаков обязан совпадать с публичной функцией.
    assert signals.is_operator is is_operator_echo(msg, known_outbox=known_outbox)
    assert set(signals.as_log_fields()) == {
        "is_echo",
        "sent_from_app",
        "unknown_outbox",
        "is_operator",
    }


# --------------------------------------------------------------------------- #
# Устойчивость к неизвестным полям
# --------------------------------------------------------------------------- #
def test_unknown_fields_in_payload_do_not_break_parsing() -> None:
    """Wazzup добавит поле — вебхук обязан продолжать разбираться."""
    msg = inbound(
        someBrandNewField={"nested": [1, 2, 3]},
        anotherFlag=True,
        futureStatusCode=42,
    )

    assert msg is not None
    assert msg.message_id == "wz-1"
    assert msg.text == "Здравствуйте"


def test_unknown_chat_type_is_skipped_not_crashed() -> None:
    """Каналов в enum Wazzup десять, наших три: остальные молча пропускаются."""
    assert to_inbound(message(chatType="viber"), received_at=RECEIVED_AT) is None
    assert channel_from_chat_type("vk") is None
    assert channel_from_chat_type("avito") is None
    assert channel_from_chat_type(None) is None
    assert channel_from_chat_type("whatsapp") is ChannelKind.WHATSAPP
    # Telegram поддержан: и через Wazzup, и напрямую через Bot API.
    assert channel_from_chat_type("telegram") is ChannelKind.TELEGRAM


def test_batch_keeps_only_supported_channels() -> None:
    """Пачка сообщений: чужие каналы отбрасываются, наши остаются."""
    payload = parse_webhook(
        {
            "messages": [
                message().model_dump(mode="json", exclude_none=True),
                message(messageId="wz-2", chatType="viber").model_dump(
                    mode="json", exclude_none=True
                ),
                message(messageId="wz-3", chatType="instagram", chatId="igsid-1").model_dump(
                    mode="json", exclude_none=True
                ),
            ]
        }
    )
    result = to_inbound_batch(payload, received_at=RECEIVED_AT)

    assert [m.message_id for m in result] == ["wz-1", "wz-3"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("text", MsgType.TEXT),
        ("image", MsgType.IMAGE),
        ("", MsgType.TEXT),
        (None, MsgType.TEXT),
        ("hologram", MsgType.UNKNOWN),
    ],
)
def test_parse_msg_type(raw, expected) -> None:
    """Неизвестный тип сообщения не роняет приём — он становится ``unknown``."""
    assert parse_msg_type(raw) is expected


@pytest.mark.parametrize(
    ("raw", "is_echo", "expected"),
    [
        ("inbound", False, MessageStatus.INBOUND),
        ("sent", True, MessageStatus.SENT),
        ("", False, MessageStatus.INBOUND),
        ("", True, MessageStatus.SENT),
        ("edited", False, MessageStatus.EDITED),   # есть только в statuses[]
        (None, True, MessageStatus.SENT),
        ("совершенно новый статус", False, MessageStatus.INBOUND),
        ("совершенно новый статус", True, MessageStatus.SENT),
    ],
)
def test_parse_status_falls_back_to_direction(raw, is_echo, expected) -> None:
    """Enum'ы ``messages.status`` и ``statuses.status`` разные — гадать нельзя.

    Неизвестное значение трактуется по направлению: эхо уже отправлено, не эхо —
    входящее. Иначе новый статус в API молча превратил бы клиента в бота.
    """
    assert parse_status(raw, is_echo=is_echo) is expected


# --------------------------------------------------------------------------- #
# Примитивы
# --------------------------------------------------------------------------- #
def test_parse_datetime_normalizes_to_utc() -> None:
    """Wazzup отдаёт время с ``Z``; наивное трактуется как UTC."""
    assert parse_datetime("2026-08-10T09:00:00.000Z") == RECEIVED_AT
    assert parse_datetime("2026-08-10T14:00:00+05:00") == RECEIVED_AT
    assert parse_datetime("2026-08-10T09:00:00").tzinfo is timezone.utc
    # Дробная часть длиннее шести знаков ломает fromisoformat — хвост обязан
    # срезаться, а не ронять разбор всего вебхука.
    assert parse_datetime("2026-08-10T09:00:00.1234567Z") == RECEIVED_AT.replace(
        microsecond=123456
    )


@pytest.mark.parametrize("raw", ["", None, "вчера", "2026-13-45T99:99:99Z"])
def test_parse_datetime_safe_never_breaks_webhook(raw) -> None:
    """Неразобранное время не имеет права уронить приём вебхука."""
    assert parse_datetime_safe(raw, default=RECEIVED_AT) == RECEIVED_AT


def test_phone_from_chat_id() -> None:
    """WhatsApp: chatId — это номер. Instagram: номера там нет и быть не может."""
    assert phone_from_chat_id("77012345678", ChannelKind.WHATSAPP) == "+77012345678"
    assert phone_from_chat_id("igsid-123", ChannelKind.WHATSAPP) is None
    assert phone_from_chat_id("77012345678", ChannelKind.INSTAGRAM) is None


def test_instagram_phone_taken_from_contact_only_if_valid() -> None:
    """Для Instagram телефон может прийти только из карточки контакта."""
    msg = inbound(
        chatType="instagram",
        chatId="igsid-1",
        contact={"name": "Айгуль", "phone": "+7 701 234 56 78"},
    )

    assert msg.channel is ChannelKind.INSTAGRAM
    assert msg.phone_e164 == "+77012345678"


@pytest.mark.parametrize(
    ("state", "expected"),
    [("active", True), ("ACTIVE", True), (" Active ", True), ("qridle", False), (None, False)],
)
def test_is_active_state_is_case_insensitive(state, expected) -> None:
    """Регистр ``state`` в вебхуке и в REST не совпадает — сравнение без регистра."""
    assert is_active_state(state) is expected


def test_conv_key_is_stable_for_same_chat() -> None:
    """Ключ диалога собирается из канала, типа чата и chatId — и только из них."""
    first = inbound(messageId="a")
    second = inbound(messageId="b")

    assert first.conv_key == second.conv_key
    assert first.conv_key != inbound(chatId="77010000002").conv_key
