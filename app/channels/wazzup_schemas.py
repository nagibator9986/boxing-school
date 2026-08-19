"""Pydantic-модели HTTP-контракта Wazzup24 v3.

Единственное место в проекте, где описан сырой JSON Wazzup. Имена полей — как в
JSON (camelCase), потому что этот модуль и есть граница с чужим API; во внутренние
модели (``app.types``) их переводит :mod:`app.channels.normalize`.

Три правила модуля:

* **ничего не выдумываем** — все поля взяты из ``docs/research-wazzup24.md`` §2, §4, §5;
  недокументированные объекты типизированы как ``dict[str, Any] | None`` с пометкой ``НП``;
* **``extra="allow"`` везде** — Wazzup вправе прислать новое поле, и приём вебхука
  от этого падать не должен (payload с неизвестным полем всё равно нужно принять и ответить 200);
* спорные поля — ``Optional`` со значением по умолчанию ``None``: в реальных примерах S5
  отсутствует половина полей структуры.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.types import WebhookValidationError

#: Значения ``chatType`` из документации (S4/S5, §3.1 research). Полный enum — 10 значений.
CHAT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "whatsapp",
        "whatsgroup",
        "viber",
        "instagram",
        "telegram",
        "telegroup",
        "vk",
        "avito",
        "max",
        "maxgroup",
    }
)

#: ``messages[].status`` (S5): есть ``inbound``, нет ``edited``.
MESSAGE_STATUSES: Final[frozenset[str]] = frozenset({"inbound", "sent", "delivered", "read", "error"})

#: ``statuses[].status`` (S5): есть ``edited``, нет ``inbound``.
STATUS_UPDATE_STATUSES: Final[frozenset[str]] = frozenset({"sent", "delivered", "read", "error", "edited"})

#: Максимальная длина ``webhooksUri`` (S5, §4 research).
WEBHOOK_URI_MAX_LEN: Final[int] = 200


class _WzModel(BaseModel):
    """База для всех схем Wazzup: неизвестные поля сохраняются, а не роняют разбор."""

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=False)


# --------------------------------------------------------------------------- #
# Вебхук: вложенные объекты
# --------------------------------------------------------------------------- #
class WzContact(_WzModel):
    """``messages[].contact``. Для WhatsApp ``phone`` не документирован — номер лежит в ``chatId``."""

    name: str | None = None
    avatarUri: str | None = None
    username: str | None = None  # только Telegram (S5)
    phone: str | None = None  # только Telegram и MAX (S5)


class WzError(_WzModel):
    """Объект ошибки: и внутри ``messages[]``/``statuses[]``, и в теле HTTP-ответа 4xx."""

    error: str | None = None
    description: str | None = None
    data: dict[str, Any] | None = None


class WzMessage(_WzModel):
    """Элемент ``messages[]`` вебхука (S5 §5.2).

    ``isEcho=false`` + ``status="inbound"`` — входящее от клиента.
    ``isEcho=true`` — эхо любого исходящего: и нашего собственного, и написанного
    оператором руками из чатов Wazzup.
    """

    messageId: str
    channelId: str
    chatType: str
    chatId: str
    dateTime: str | None = None
    type: str | None = None
    isEcho: bool | None = None
    contact: WzContact | None = None
    text: str | None = None
    contentUri: str | None = None
    status: str | None = None
    error: WzError | None = None
    authorName: str | None = None  # приходит только при isEcho == true (S5)
    authorId: str | None = None
    instPost: dict[str, Any] | None = None  # НП: полный состав не документирован
    interactive: list[Any] | None = None  # НП: структура не раскрыта в доке
    quotedMessage: dict[str, Any] | None = None  # НП: структура не раскрыта в доке
    sentFromApp: bool | None = None  # НП: проверить на проде, приходит ли false для API-сообщений
    isEdited: bool | None = None
    isDeleted: bool | None = None
    oldInfo: dict[str, Any] | None = None
    avitoProfileId: str | None = None
    advert: dict[str, Any] | None = None  # НП: структура не раскрыта в доке


class WzStatus(_WzModel):
    """Элемент ``statuses[]`` — обновление статуса нашего исходящего (S5 §5.4)."""

    messageId: str
    timestamp: str | None = None
    status: str | None = None
    error: WzError | None = None


class WzChannelUpdate(_WzModel):
    """Элемент ``channelsUpdates[]`` (S5 §5.7).

    Набор и регистр значений ``state`` здесь и в ``GET /v3/channels`` не совпадают —
    сравнивать только регистронезависимо, неизвестное значение считать «не active».
    """

    channelId: str
    state: str | None = None
    timestamp: int | None = None  # мс
    tier: str | None = None  # только WABA: TIER_0, TIER_1K, ...
    qr: str | None = None  # base64, только при state == "qr"
    qridle: bool | None = None


class WzTemplateStatus(_WzModel):
    """``templateStatus`` — модерация шаблона WABA (S5 §5.8). Нам не нужен, но приходить может."""

    templateGuid: str | None = None
    name: str | None = None
    status: str | None = None


class WebhookPayload(_WzModel):
    """Тело вебхука. ``messages`` и ``statuses`` могут прийти одновременно (S5 §5.1).

    ``extra="forbid"`` здесь запрещён сознательно: новые поля Wazzup не должны ронять приём.
    Регистрационный тестовый запрос приходит как ``{"test": true}`` либо как пустое тело —
    оба случая обязаны дать 200 OK, иначе Wazzup вернёт ``testPostNotPassed``.
    """

    test: bool | None = None
    messages: list[WzMessage] | None = None
    statuses: list[WzStatus] | None = None
    channelsUpdates: list[WzChannelUpdate] | None = None
    templateStatus: WzTemplateStatus | None = None
    createContact: dict[str, Any] | None = None  # мы не CRM, игнорируем
    createDeal: dict[str, Any] | None = None  # мы не CRM, игнорируем

    @property
    def is_test(self) -> bool:
        """Тестовый запрос при регистрации вебхука: ``{"test": true}`` или пустое тело."""
        if self.test:
            return True
        return not any(
            (
                self.messages,
                self.statuses,
                self.channelsUpdates,
                self.templateStatus,
                self.createContact,
                self.createDeal,
            )
        )

    def message_list(self) -> list[WzMessage]:
        """``messages`` без ``None`` — чтобы вызывающий не проверял на пустоту."""
        return list(self.messages or ())

    def status_list(self) -> list[WzStatus]:
        """``statuses`` без ``None``."""
        return list(self.statuses or ())

    def channel_update_list(self) -> list[WzChannelUpdate]:
        """``channelsUpdates`` без ``None``."""
        return list(self.channelsUpdates or ())


def parse_webhook(payload: Any) -> WebhookPayload:
    """Разбирает тело вебхука. Кидает :class:`app.types.WebhookValidationError`.

    Хендлер вебхука обязан поймать это исключение и всё равно ответить **200 OK**:
    4xx спровоцировал бы ретраи Wazzup на заведомо неразбираемом теле.
    """
    if payload is None:
        return WebhookPayload()
    if isinstance(payload, (bytes, str)):
        raise WebhookValidationError("Тело вебхука не разобрано как JSON-объект")
    if not isinstance(payload, dict):
        raise WebhookValidationError(f"Ожидался JSON-объект, получено {type(payload).__name__}")
    try:
        return WebhookPayload.model_validate(payload)
    except ValidationError as exc:  # pragma: no cover - текст ошибки не контрактный
        raise WebhookValidationError(f"Невалидное тело вебхука: {exc.error_count()} ошибок") from exc


# --------------------------------------------------------------------------- #
# Исходящие запросы
# --------------------------------------------------------------------------- #
class SendMessageRequest(_WzModel):
    """Тело ``POST /v3/message``.

    ``text`` и ``contentUri`` одновременно передавать нельзя (``MESSAGE_ONLY_TEXT_OR_CONTENT``).
    ``crmMessageId`` передаётся всегда — это единственная защита от дублей, живёт 60 секунд.
    ``clearUnanswered`` всегда ``False``: автоответ не должен гасить счётчик неотвеченных.
    """

    channelId: str
    chatType: str
    chatId: str
    text: str | None = None
    contentUri: str | None = None
    crmMessageId: str | None = None
    refMessageId: str | None = None
    clearUnanswered: bool = False

    def to_body(self) -> dict[str, Any]:
        """JSON-тело запроса: пустые поля выброшены, ``clearUnanswered`` принудительно ``False``."""
        body = self.model_dump(exclude_none=True)
        body["clearUnanswered"] = False
        return body


class SendMessageResponse(_WzModel):
    """Ответ ``POST /v3/message``. Успех — 201 (S4); реальные клиенты принимают и 200."""

    messageId: str | None = None
    chatId: str | None = None


class WebhookSubscriptions(_WzModel):
    """``subscriptions`` для ``PATCH /v3/webhooks``.

    ``contactsAndDealsCreation`` обязан быть ``False``: мы не CRM и не умеем отвечать
    корректными объектами контакта/сделки.
    """

    messagesAndStatuses: bool = True
    contactsAndDealsCreation: bool = False
    channelsUpdates: bool = True
    templateStatus: bool = False


class SetWebhooksRequest(_WzModel):
    """Тело ``PATCH /v3/webhooks``. ``webhooksUri`` — не более 200 символов (S5)."""

    webhooksUri: str
    subscriptions: WebhookSubscriptions = Field(default_factory=WebhookSubscriptions)

    def to_body(self) -> dict[str, Any]:
        """JSON-тело запроса."""
        return self.model_dump()


class WzChannel(_WzModel):
    """Строка ответа ``GET /v3/channels`` (S6 §10)."""

    channelId: str
    transport: str | None = None
    plainId: str | None = None
    state: str | None = None


class WzErrorResponse(_WzModel):
    """Тело ошибки Wazzup.

    Общий формат (S7): ``error``/``description``/``data``. Расширенный формат отправки (S4)
    добавляет ``status`` и ``requestId``. Тело может быть и вовсе пустым.
    """

    status: int | None = None
    requestId: str | None = None
    error: str | None = None
    description: str | None = None
    data: dict[str, Any] | None = None


__all__ = [
    "CHAT_TYPES",
    "MESSAGE_STATUSES",
    "STATUS_UPDATE_STATUSES",
    "WEBHOOK_URI_MAX_LEN",
    "SendMessageRequest",
    "SendMessageResponse",
    "SetWebhooksRequest",
    "WebhookPayload",
    "WebhookSubscriptions",
    "WzChannel",
    "WzChannelUpdate",
    "WzContact",
    "WzError",
    "WzErrorResponse",
    "WzMessage",
    "WzStatus",
    "WzTemplateStatus",
    "parse_webhook",
]
