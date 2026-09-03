"""Контракт HTTP-клиента Wazzup24 — единственного пути сообщения к клиенту.

До появления этого файла модуль не был покрыт ничем: пайплайн проверялся до
``outbox``, а что происходит дальше — не проверял никто. При этом ошибка здесь
не видна ни в логах пайплайна, ни в тестах: бот считает, что ответил, а родитель
не получает ничего.

Сеть заглушена ``respx``: проверяется форма запроса и реакция на ответы,
а не доступность Wazzup.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.channels.errors import ErrorDisposition, disposition, is_duplicate, is_retriable
from app.channels.wazzup_client import (
    ROUTE_CHANNELS,
    ROUTE_MESSAGE,
    ROUTE_WEBHOOKS,
    WazzupClient,
)
from app.channels.wazzup_schemas import SendMessageRequest
from app.types import WazzupError, WazzupServerError

BASE = "https://api.wazzup24.com/v3"
API_KEY = "test-wazzup-key"
CHANNEL_ID = "11111111-1111-1111-1111-111111111111"

# Клиент не удваивает ``/v3``: база уже кончается на нём, поэтому боевой адрес —
# https://api.wazzup24.com/v3/message, а не .../v3/v3/message.
URL_MESSAGE = f"{BASE}{ROUTE_MESSAGE.removeprefix('/v3')}"
URL_CHANNELS = f"{BASE}{ROUTE_CHANNELS.removeprefix('/v3')}"
URL_WEBHOOKS = f"{BASE}{ROUTE_WEBHOOKS.removeprefix('/v3')}"


@pytest.fixture
async def client():
    """Клиент с короткими паузами: тесты не имеют права спать секундами."""
    c = WazzupClient(
        api_key=API_KEY,
        base_url=BASE,
        timeout_ms=2000,
        max_attempts=3,
        backoff_base_ms=1,
    )
    try:
        yield c
    finally:
        await c.aclose()


def message(text: str = "Здравствуйте", crm_id: str = "crm-1") -> SendMessageRequest:
    """Типовое исходящее сообщение бота."""
    return SendMessageRequest(
        channelId=CHANNEL_ID,
        chatType="whatsapp",
        chatId="77015550101",
        text=text,
        crmMessageId=crm_id,
    )


# --------------------------------------------------------------------------- #
# Успешная отправка
# --------------------------------------------------------------------------- #
@respx.mock
async def test_send_message_accepts_201_and_returns_message_id(client) -> None:
    """Успех отправки — 201, а не 200: так описано в документации Wazzup."""
    route = respx.post(URL_MESSAGE).mock(
        return_value=httpx.Response(201, json={"messageId": "wz-1", "chatId": "77015550101"})
    )

    result = await client.send_message(message())

    assert route.called
    assert result.messageId == "wz-1"


@respx.mock
async def test_send_message_also_accepts_200(client) -> None:
    """Часть каналов отвечает 200 — это тоже успех, а не повод для ретрая."""
    respx.post(URL_MESSAGE).mock(
        return_value=httpx.Response(200, json={"messageId": "wz-2"})
    )

    assert (await client.send_message(message())).messageId == "wz-2"


@respx.mock
async def test_request_carries_bearer_token_and_never_clears_unanswered(client) -> None:
    """Два инварианта запроса, которые ломают продукт молча.

    ``clearUnanswered`` обязан быть ``false``: иначе автоответ бота гасит счётчик
    неотвеченных, и менеджеры перестают видеть, что клиент ждёт человека.
    ``crmMessageId`` — единственная защита от дубля при повторной отправке.
    """
    route = respx.post(URL_MESSAGE).mock(
        return_value=httpx.Response(201, json={"messageId": "wz-3"})
    )

    await client.send_message(message(crm_id="crm-42"))

    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {API_KEY}"

    import json

    body = json.loads(request.content)
    assert body["clearUnanswered"] is False, "бот погасил счётчик неотвеченных"
    assert body["crmMessageId"] == "crm-42", "потеряна защита от дубля"
    assert body["chatType"] == "whatsapp"
    assert "contentUri" not in body, "пустые поля не должны уезжать в запрос"


# --------------------------------------------------------------------------- #
# Ошибки: что ретраить, а что нет
# --------------------------------------------------------------------------- #
@respx.mock
async def test_client_error_is_not_retried(client) -> None:
    """4xx — вина запроса. Повтор её не исправит и только сожжёт лимит."""
    route = respx.post(URL_MESSAGE).mock(
        return_value=httpx.Response(
            400, json={"error": "MESSAGE_ONLY_TEXT_OR_CONTENT", "description": "bad"}
        )
    )

    with pytest.raises(WazzupError):
        await client.send_message(message())

    assert route.call_count == 1, f"4xx повторили {route.call_count} раз"


@respx.mock
async def test_server_error_is_retried_and_then_succeeds(client) -> None:
    """5xx — временная беда на той стороне: повторяем, а не теряем сообщение."""
    route = respx.post(URL_MESSAGE).mock(
        side_effect=[
            httpx.Response(502, text="bad gateway"),
            httpx.Response(201, json={"messageId": "wz-after-retry"}),
        ]
    )

    result = await client.send_message(message())

    assert route.call_count == 2
    assert result.messageId == "wz-after-retry"


@respx.mock
async def test_server_error_exhausts_attempts_and_raises(client) -> None:
    """Когда попытки кончились — честная ошибка, чтобы воркер вернул задачу в очередь."""
    route = respx.post(URL_MESSAGE).mock(
        return_value=httpx.Response(503, text="unavailable")
    )

    with pytest.raises(WazzupServerError):
        await client.send_message(message())

    assert route.call_count == 3, "клиент обязан израсходовать ровно max_attempts"


@respx.mock
async def test_network_failure_is_retried(client) -> None:
    """Обрыв соединения — не повод терять сообщение клиента."""
    route = respx.post(URL_MESSAGE).mock(
        side_effect=[
            httpx.ConnectError("connection reset"),
            httpx.Response(201, json={"messageId": "wz-net"}),
        ]
    )

    result = await client.send_message(message())

    assert route.call_count == 2
    assert result.messageId == "wz-net"


@respx.mock
async def test_duplicate_crm_message_id_is_recognised_not_retried(client) -> None:
    """Повтор ``crmMessageId`` — признак, что сообщение уже ушло.

    Ретрай здесь дал бы клиенту второе одинаковое сообщение, поэтому ошибка
    классифицируется как дубль и обрабатывается отдельно от обычной 4xx.
    """
    route = respx.post(URL_MESSAGE).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": "repeatedCrmMessageId",
                "description": "You have already sent message with same crmMessageId",
            },
        )
    )

    with pytest.raises(WazzupError) as exc:
        await client.send_message(message())

    assert route.call_count == 1
    assert is_duplicate(exc.value), "повтор crmMessageId не распознан как дубль"
    assert not is_retriable(exc.value)


@respx.mock
async def test_error_code_case_and_separators_do_not_matter(client) -> None:
    """Один и тот же код Wazzup пишет и в camelCase, и в SNAKE_CASE.

    Классификация обязана давать один вердикт на оба написания, иначе поведение
    зависит от того, какую страницу документации повторил сервер.
    """
    respx.post(URL_MESSAGE).mock(
        return_value=httpx.Response(400, json={"error": "REPEATED_CRM_MESSAGE_ID"})
    )

    with pytest.raises(WazzupError) as exc:
        await client.send_message(message())

    assert is_duplicate(exc.value)


# --------------------------------------------------------------------------- #
# Каналы и вебхуки
# --------------------------------------------------------------------------- #
@respx.mock
async def test_get_channels_parses_bare_list(client) -> None:
    """Форма ответа в документации не зафиксирована — принимаем голый список."""
    respx.get(URL_CHANNELS).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"channelId": CHANNEL_ID, "transport": "whatsapp", "state": "active"},
            ],
        )
    )

    channels = await client.get_channels()

    assert len(channels) == 1
    assert channels[0].channel_id == CHANNEL_ID
    assert channels[0].is_active, "активный канал распознан как неактивный"


@respx.mock
async def test_get_channels_parses_wrapped_list(client) -> None:
    """…и список, завёрнутый в объект: обёртка в доке тоже не описана."""
    respx.get(URL_CHANNELS).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"channelId": CHANNEL_ID, "transport": "whatsapp", "state": "active"}]},
        )
    )

    assert len(await client.get_channels()) == 1


@respx.mock
async def test_set_webhooks_uses_patch(client) -> None:
    """Регистрация вебхука — ``PATCH``, а не ``POST``. Ошибка метода = бот глухой."""
    route = respx.patch(URL_WEBHOOKS).mock(return_value=httpx.Response(200, json={}))

    await client.set_webhooks("https://bot.example.com/wazzup/webhook/secret-secret-secret-12")

    assert route.called
    import json

    body = json.loads(route.calls.last.request.content)
    assert body["webhooksUri"].startswith("https://")


async def test_set_webhooks_rejects_plain_http(client) -> None:
    """http-адрес Wazzup не примет — ловим это у себя, а не после молчания вебхука."""
    with pytest.raises(WazzupError):
        await client.set_webhooks("http://bot.example.com/wazzup/webhook/secret")


async def test_set_webhooks_rejects_too_long_uri(client) -> None:
    """Лимит длины URI — 200 символов; длинный секрет молча обрежется на той стороне."""
    long_uri = "https://bot.example.com/wazzup/webhook/" + "s" * 220

    with pytest.raises(WazzupError):
        await client.set_webhooks(long_uri)


# --------------------------------------------------------------------------- #
# Классификация для воркера отправки
# --------------------------------------------------------------------------- #
@respx.mock
async def test_expired_instagram_window_needs_human_not_retry(client) -> None:
    """Истёкшее окно Instagram ретраем не лечится — нужен человек.

    Окно открывается только входящим сообщением клиента; сколько ни повторяй
    отправку, оно не откроется, а попытки будут жечь лимиты.
    """
    respx.post(URL_MESSAGE).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": "MESSAGE_WINDOW_EXPIRED",
                "description": "24 hours window expired",
            },
        )
    )

    with pytest.raises(WazzupError) as exc:
        await client.send_message(
            SendMessageRequest(
                channelId=CHANNEL_ID,
                chatType="instagram",
                chatId="ig-user",
                text="Здравствуйте",
                crmMessageId="crm-ig-1",
            )
        )

    assert not is_retriable(exc.value)
    assert disposition(exc.value) is not ErrorDisposition.RETRIABLE


# --------------------------------------------------------------------------- #
# Значки списка не режутся
# --------------------------------------------------------------------------- #
def test_list_markers_survive_sanitize() -> None:
    """Значок в начале строки — разметка, а не украшение.

    Правило «не больше двух эмодзи на сообщение» писалось против фейерверка, но
    список залов попадал под него целиком: 03.09.2026 из семи пунктов значок
    остался у двух, и список превратился в лесенку.
    """
    from app.channels.outbound import sanitize

    text = "\n".join(f"📍 Зал номер {n}" for n in range(1, 8))

    assert sanitize(text).count("📍") == 7


def test_emoji_fireworks_inside_a_line_are_still_trimmed() -> None:
    """Опора: правило про фейерверк осталось — просто считается по строке."""
    from app.channels.outbound import sanitize

    assert sanitize("Ура!!! 🥊🥊🥊🔥🔥😀 приходите").count("🥊") == 2
