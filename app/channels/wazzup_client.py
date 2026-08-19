"""HTTP-клиент Wazzup24 v3.

Сети ни в импорте модуля, ни в конструкторе: ``httpx.AsyncClient`` создаётся лениво
при первом запросе, поэтому модуль импортируется при пустом окружении и тестируется
через ``respx``.

Что важно помнить про этот API (research-wazzup24):

* успех ``POST /v3/message`` — **201**, не 200 (S4); реальные клиенты принимают оба;
* ``crmMessageId`` передаётся всегда: это единственная защита от дублей, и живёт она
  ровно 60 секунд — повтор с тем же id внутри минуты вернёт 400 ``repeatedCrmMessageId``,
  что для нас успех, а не ошибка;
* ``clearUnanswered=false`` на всех автоответах — иначе бот гасит счётчик неотвеченных
  и менеджеры перестают видеть ждущих клиентов;
* повторяются только 429, 5xx и сетевые сбои. На 4xx повтор запрещён: он либо не поможет,
  либо (без ``crmMessageId``) отправит клиенту второе сообщение.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Final
from uuid import uuid4

import httpx

from app.channels.errors import RETRYABLE_STATUSES, classify_response
from app.channels.outbound import next_backoff_ms
from app.channels.wazzup_schemas import (
    WEBHOOK_URI_MAX_LEN,
    SendMessageRequest,
    SendMessageResponse,
    SetWebhooksRequest,
    WebhookSubscriptions,
    WzChannel,
)
from app.config import Settings, get_settings
from app.types import ChannelState, WazzupError, WazzupServerError

#: Роуты API. База может уже содержать ``/v3`` — дубль вырезается в :meth:`WazzupClient._url`.
ROUTE_MESSAGE: Final[str] = "/v3/message"
ROUTE_CHANNELS: Final[str] = "/v3/channels"
ROUTE_WEBHOOKS: Final[str] = "/v3/webhooks"

#: Успешные коды отправки: по документации 201, по практике встречается и 200.
SEND_SUCCESS_STATUSES: Final[frozenset[int]] = frozenset({200, 201})

#: Значение ``state``, при котором каналом можно пользоваться. Сравнение регистронезависимо:
#: наборы и регистр значений в ``GET /v3/channels`` и в вебхуке ``channelsUpdates`` расходятся.
ACTIVE_CHANNEL_STATE: Final[str] = "active"

#: Потолок ожидания по заголовку ``Retry-After``, чтобы не уснуть на полчаса.
MAX_RETRY_AFTER_MS: Final[int] = 30_000


class WazzupClient:
    """Клиент Wazzup24 v3. Сети в конструкторе нет — клиент создаётся лениво.

    Дополнительные keyword-параметры (``max_attempts``, ``backoff_base_ms``) имеют
    значения по умолчанию, поэтому контрактный вызов
    ``WazzupClient(api_key=..., base_url=..., timeout_ms=...)`` работает без изменений.
    Внутренние повторы — короткие и только на транспортных сбоях; долгий повтор
    с большими паузами остаётся за воркером ``tasks_outbound``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_ms: int,
        max_attempts: int = 3,
        backoff_base_ms: int = 500,
        max_connections: int = 10,
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or "https://api.wazzup24.com/v3").rstrip("/")
        self._timeout_ms = max(1000, int(timeout_ms))
        self._max_attempts = max(1, int(max_attempts))
        self._backoff_base_ms = max(1, int(backoff_base_ms))
        self._max_connections = max(1, int(max_connections))
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    # ----------------------------------------------------------------- фабрики
    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "WazzupClient":
        """Собирает клиент из настроек. Настройки читаются в момент вызова, не при импорте."""
        cfg = settings or get_settings()
        return cls(
            api_key=cfg.wazzup_api_key,
            base_url=cfg.wazzup_api_base,
            timeout_ms=cfg.wazzup_timeout_ms,
            backoff_base_ms=cfg.wazzup_send_backoff_base_ms,
        )

    # ------------------------------------------------------------------ методы
    async def send_message(self, req: SendMessageRequest) -> SendMessageResponse:
        """``POST /v3/message``. Успех — 200 или 201. Ошибки → ``classify()``.

        ``crmMessageId`` проставляется автоматически, если его не передали:
        без него повтор превращается в дубль в чате клиента.
        ``clearUnanswered`` принудительно ``false``.
        """
        prepared = req.model_copy(
            update={
                "crmMessageId": req.crmMessageId or str(uuid4()),
                "clearUnanswered": False,
            }
        )
        response = await self._request("POST", ROUTE_MESSAGE, json=prepared.to_body())
        if response.status_code in SEND_SUCCESS_STATUSES:
            body = _json_or_none(response)
            return SendMessageResponse.model_validate(body if isinstance(body, dict) else {})
        raise self._error(response)

    async def get_channels(self) -> list[ChannelState]:
        """``GET /v3/channels``. Сравнение ``state`` регистронезависимо, неизвестное = не active."""
        response = await self._request("GET", ROUTE_CHANNELS)
        if response.status_code != 200:
            raise self._error(response)
        body = _json_or_none(response)
        rows = _extract_rows(body)
        states: list[ChannelState] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            channel = WzChannel.model_validate(row)
            state = (channel.state or "").strip()
            states.append(
                ChannelState(
                    channel_id=channel.channelId,
                    transport=(channel.transport or "unknown").strip().lower(),
                    plain_id=channel.plainId,
                    state=state,
                    is_active=state.lower() == ACTIVE_CHANNEL_STATE,
                )
            )
        return states

    async def get_webhooks(self) -> dict[str, Any]:
        """``GET /v3/webhooks``. Возвращает тело как есть — форма ответа в доке не зафиксирована."""
        response = await self._request("GET", ROUTE_WEBHOOKS)
        if response.status_code != 200:
            raise self._error(response)
        body = _json_or_none(response)
        if isinstance(body, dict):
            return body
        return {"raw": body}  # НП: проверить на проде, ответ описан в доке как «{ ok }»

    async def set_webhooks(
        self,
        uri: str,
        *,
        messages_and_statuses: bool = True,
        contacts_and_deals_creation: bool = False,
        channels_updates: bool = True,
        template_status: bool = False,
    ) -> None:
        """``PATCH /v3/webhooks``.

        ``contacts_and_deals_creation`` обязан быть ``False`` — мы не CRM и не умеем
        отвечать объектами контакта/сделки, а Wazzup их ждёт.
        Перед вызовом убедиться, что ``len(uri) <= 200``: это ограничение самого Wazzup.
        Регистрация проверяется тестовым POST на указанный адрес — если он не ответит
        200 OK, вернётся ``testPostNotPassed``.
        """
        if not uri or not uri.startswith("https://"):
            raise WazzupError(f"webhooksUri должен быть https-адресом, получено: {uri!r}")
        if len(uri) > WEBHOOK_URI_MAX_LEN:
            raise WazzupError(
                f"webhooksUri длиннее {WEBHOOK_URI_MAX_LEN} символов ({len(uri)}) — Wazzup его не примет"
            )
        if contacts_and_deals_creation:
            raise WazzupError("contactsAndDealsCreation обязан быть false: мы не CRM")
        payload = SetWebhooksRequest(
            webhooksUri=uri,
            subscriptions=WebhookSubscriptions(
                messagesAndStatuses=messages_and_statuses,
                contactsAndDealsCreation=False,
                channelsUpdates=channels_updates,
                templateStatus=template_status,
            ),
        )
        response = await self._request("PATCH", ROUTE_WEBHOOKS, json=payload.to_body())
        if response.status_code not in (200, 201, 204):
            raise self._error(response)

    async def aclose(self) -> None:
        """Закрывает пул соединений. Вызывается на shutdown приложения."""
        async with self._lock:
            client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def __aenter__(self) -> "WazzupClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------- внутреннее
    async def _get_client(self) -> httpx.AsyncClient:
        """Ленивое создание клиента: один пул соединений на весь процесс."""
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                seconds = self._timeout_ms / 1000
                self._client = httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        connect=min(5.0, seconds),
                        read=seconds,
                        write=seconds,
                        pool=seconds,
                    ),
                    limits=httpx.Limits(
                        max_connections=self._max_connections,
                        max_keepalive_connections=max(1, self._max_connections // 2),
                        keepalive_expiry=30.0,
                    ),
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": "ainazarov-bot/1.0 (+wazzup24-v3)",
                    },
                    follow_redirects=False,
                )
        return self._client

    def _url(self, route: str) -> str:
        """Склеивает базу и роут, не удваивая ``/v3``."""
        path = "/" + route.lstrip("/")
        if self._base_url.endswith("/v3") and path.startswith("/v3/"):
            path = path[len("/v3") :]
        return f"{self._base_url}{path}"

    async def _request(self, method: str, route: str, *, json: Any = None) -> httpx.Response:
        """Один вызов API с повторами на 429/5xx и сетевых сбоях.

        4xx возвращается вызывающему как есть — классифицировать и решать будет он.
        """
        url = self._url(route)
        client = await self._get_client()
        last_exc: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await client.request(method, url, json=json)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt >= self._max_attempts:
                    break
                await self._sleep(next_backoff_ms(attempt, base_ms=self._backoff_base_ms))
                continue
            if response.status_code in RETRYABLE_STATUSES and attempt < self._max_attempts:
                await self._sleep(self._delay_for(response, attempt))
                continue
            return response
        raise WazzupServerError(
            f"Wazzup недоступен: {type(last_exc).__name__ if last_exc else 'нет ответа'}",
            status=None,
            error_code="network_error",
            description=str(last_exc) if last_exc else None,
        )

    def _delay_for(self, response: httpx.Response, attempt: int) -> int:
        """Пауза перед повтором: уважаем ``Retry-After``, иначе экспонента с джиттером."""
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is not None:
            capped = min(retry_after, MAX_RETRY_AFTER_MS)
            return int(capped * (1 + random.uniform(0.0, 0.1)))
        return next_backoff_ms(attempt, base_ms=self._backoff_base_ms)

    @staticmethod
    async def _sleep(delay_ms: int) -> None:
        await asyncio.sleep(max(0, delay_ms) / 1000)

    @staticmethod
    def _error(response: httpx.Response) -> WazzupError:
        """Превращает неуспешный ответ в конкретный подкласс ``WazzupError``."""
        return classify_response(
            response.status_code,
            _json_or_none(response),
            request_id=response.headers.get("X-Request-Id"),
        )


def _json_or_none(response: httpx.Response) -> Any:
    """Разбирает тело ответа. Тело ошибки Wazzup бывает пустым — это норма (S7)."""
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _extract_rows(body: Any) -> list[Any]:
    """``GET /v3/channels`` документирован как массив; на всякий случай терпим и обёртку."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("data", "channels", "items"):  # НП: обёртка в доке не описана
            value = body.get(key)
            if isinstance(value, list):
                return value
    return []


def _parse_retry_after(value: str | None) -> int | None:
    """``Retry-After`` в секундах → миллисекунды. Формат-дату игнорируем."""
    if not value:
        return None
    try:
        return int(float(value.strip()) * 1000)
    except ValueError:
        return None


__all__ = [
    "ACTIVE_CHANNEL_STATE",
    "MAX_RETRY_AFTER_MS",
    "ROUTE_CHANNELS",
    "ROUTE_MESSAGE",
    "ROUTE_WEBHOOKS",
    "SEND_SUCCESS_STATUSES",
    "WazzupClient",
]
