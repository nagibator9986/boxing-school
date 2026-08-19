"""Классификация ошибок Wazzup24.

Документация Wazzup пишет один и тот же код и в camelCase, и в UPPER_SNAKE
(``repeatedCrmMessageId`` в прозе S4 против ``REPEATED_CRM_MESSAGE_ID`` в таблице
кодов и в примере JSON на той же странице; тот же класс расхождения в S5:
``uriNotValid``, ``testPostNotPassed``). Какой вариант приходит на практике —
**НЕ ПОДТВЕРЖДЕНО**. Поэтому коды сравниваются только нормализованно:
регистр и разделители отбрасываются.

Классификация — три исхода:

* **retriable** — 429 и 5xx, сетевые сбои: повторить с backoff;
* **needs_human** — канал/окно/спам: повтор не поможет, нужен живой человек
  (истёкшее 7-дневное окно Instagram — именно сюда, а не в ретраи);
* **fatal** — 4xx по нашей вине: чинится кодом, а не повтором.

Отдельно стоит **duplicate**: ``repeatedCrmMessageId`` — это успех отправки,
а не ошибка (сообщение уже ушло минуту назад, идемпотентность отработала).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Final

from app.types import (
    WazzupBadContactError,
    WazzupChannelError,
    WazzupDuplicateError,
    WazzupError,
    WazzupRateLimitError,
    WazzupServerError,
    WazzupSpamError,
)

#: Всё, что не буква и не цифра, при сравнении кодов выбрасывается.
_NON_ALNUM_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: HTTP-статусы, при которых повтор осмыслен (S7: лимит 500 запросов / 5 секунд).
RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

#: Повторный ``crmMessageId`` в течение 60 секунд. Вызывающий трактует как успех.
DUPLICATE_CODES: Final[frozenset[str]] = frozenset({"repeatedcrmmessageid"})

#: Канал неисправен/выключен/не оплачен: бот молчит, менеджеру уходит общий алерт.
CHANNEL_CODES: Final[frozenset[str]] = frozenset(
    {
        "channelnotfound",
        "channelblocked",
        "channelnomoney",
        "channelwapirejected",
        "channellimitexceeded",
        "messagechannelunavailable",
        "balanceisempty",
        "channelinvalidtransportforediting",
        "channelinvalidtransportforcontentediting",
        "channelinvalidtransportfordeletion",
    }
)

#: WhatsApp счёл сообщение спамом. Стоп канала + тревога менеджеру, лид не теряется.
SPAM_CODES: Final[frozenset[str]] = frozenset({"messagesisspam", "spam"})

#: Контакт недостижим: номера нет в WhatsApp, либо chatId не тот. Ретраить бессмысленно.
BAD_CONTACT_CODES: Final[frozenset[str]] = frozenset(
    {"badcontact", "chatidigsidmismatch", "chatnoaccess", "messagesnotfound"}
)

#: Писать первым нельзя / окно диалога закрыто / шаблон отклонён — нужен живой человек.
#: НП: отдельного кода «истекло 7-дневное окно Instagram» в доке нет (§9.1, открытый
#: вопрос №10: полный список кодов вебхука не опубликован). Пока опираемся на эти четыре
#: и на предварительную проверку окна в ``outbound.check_send_allowed``.
NEEDS_HUMAN_CODES: Final[frozenset[str]] = frozenset(
    {
        "messagesnottextfirst",
        "messagesabnormalsend",
        "messagesinvalidcontacttype",
        "templaterejected",
    }
)

#: Ошибки формата запроса: чинятся кодом, а не повтором.
FATAL_CODES: Final[frozenset[str]] = frozenset(
    {
        "invalidmessagedata",
        "wrongtransport",
        "validationerror",
        "messagewrongcontenttype",
        "messageonlytextorcontent",
        "messagesonlytextorcontent",
        "messagenothingtosend",
        "messagetexttoolong",
        "messagestoolonginstagram",
        "messagestoolongtelegram",
        "messagestoolongwaba",
        "messagestoolongvk",
        "messagestoolongavito",
        "messagestoolongviber",
        "messagestoolongwabaheader",
        "messagestoolongwabatemplate",
        "messagesunsupportedcontenttypeinstapi",
        "messagescontentcannotbeblank",
        "messagestextcannotbeblank",
        "messagescontentsizeexceeded",
        "messagescontentsizeexceededwaba",
        "messagescontentsizeexceededtelegram",
        "messagedownloadcontenterror",
        "referencemessagenotfound",
        "messagescontainbuttons",
        "messageseditingtimeexpired",
        "messagesdeletiontimeexpired",
    }
)


class ErrorDisposition(str, Enum):
    """Что делать с ошибкой. Единственный вход для воркера отправки."""

    RETRIABLE = "retriable"
    DUPLICATE = "duplicate"
    NEEDS_HUMAN = "needs_human"
    FATAL = "fatal"


def normalize_code(code: str | None) -> str:
    """Нормализует код ошибки: регистр и разделители не влияют на сравнение.

    ``REPEATED_CRM_MESSAGE_ID``, ``repeatedCrmMessageId`` и ``repeated-crm-message-id``
    дают одну и ту же строку ``repeatedcrmmessageid``. Реализация шире, чем
    ``code.replace("_", "").lower()`` из контракта (выбрасываются любые не-буквенно-цифровые
    символы), но на всех документированных кодах результат совпадает байт-в-байт.
    """
    if not code:
        return ""
    return _NON_ALNUM_RE.sub("", code.strip().lower())


def classify(
    status: int,
    code: str | None,
    description: str | None,
    data: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> WazzupError:
    """Отображает HTTP-ответ Wazzup в конкретный подкласс :class:`app.types.WazzupError`.

    Код важнее статуса: ``CHANNEL_BLOCKED`` приезжает с 400, но это не наша ошибка формата.
    Порядок разбора: дубль → спам → контакт → канал → «нужен человек» → 429 → 5xx → прочее.
    """
    normalized = normalize_code(code)
    message = _message(status, code, description)
    kwargs: dict[str, Any] = {
        "status": status,
        "error_code": code,
        "description": description,
        "data": data,
        "request_id": request_id,
    }

    if normalized in DUPLICATE_CODES:
        return WazzupDuplicateError(message, **kwargs)
    if normalized in SPAM_CODES:
        return WazzupSpamError(message, **kwargs)
    if normalized in BAD_CONTACT_CODES:
        return WazzupBadContactError(message, **kwargs)
    if normalized in CHANNEL_CODES:
        return WazzupChannelError(message, **kwargs)
    if normalized in NEEDS_HUMAN_CODES:
        # Окно закрыто или писать первым нельзя: не ретраить, звать человека.
        return WazzupBadContactError(message, **kwargs)
    if status == 429:
        return WazzupRateLimitError(message, **kwargs)
    if status in RETRYABLE_STATUSES or status >= 500:
        return WazzupServerError(message, **kwargs)
    return WazzupError(message, **kwargs)


def classify_response(status: int, body: Any, *, request_id: str | None = None) -> WazzupError:
    """То же, что :func:`classify`, но принимает разобранное тело ответа целиком.

    Тело ошибки Wazzup бывает пустым (S7), поэтому ``body`` может быть чем угодно.
    """
    code: str | None = None
    description: str | None = None
    data: dict[str, Any] | None = None
    rid = request_id
    if isinstance(body, dict):
        raw_code = body.get("error")
        code = raw_code if isinstance(raw_code, str) else None
        raw_description = body.get("description")
        description = raw_description if isinstance(raw_description, str) else None
        raw_data = body.get("data")
        data = raw_data if isinstance(raw_data, dict) else None
        raw_rid = body.get("requestId")
        if rid is None and isinstance(raw_rid, str):
            rid = raw_rid
    return classify(status, code, description, data, rid)


def disposition(exc: WazzupError) -> ErrorDisposition:
    """Что делать с уже классифицированной ошибкой.

    Воркер отправки не разбирает коды сам: он спрашивает здесь. ``DUPLICATE`` —
    успех (сообщение уже доставлено), ``NEEDS_HUMAN`` — эскалация без ретраев.
    """
    normalized = normalize_code(exc.error_code)
    if isinstance(exc, WazzupDuplicateError) or normalized in DUPLICATE_CODES:
        return ErrorDisposition.DUPLICATE
    if isinstance(exc, (WazzupSpamError, WazzupChannelError, WazzupBadContactError)):
        return ErrorDisposition.NEEDS_HUMAN
    if normalized in NEEDS_HUMAN_CODES:
        return ErrorDisposition.NEEDS_HUMAN
    if isinstance(exc, (WazzupRateLimitError, WazzupServerError)) or exc.retryable:
        return ErrorDisposition.RETRIABLE
    if exc.status is not None and exc.status in RETRYABLE_STATUSES:
        return ErrorDisposition.RETRIABLE
    return ErrorDisposition.FATAL


def is_retriable(exc: WazzupError) -> bool:
    """Повторять ли отправку. 4xx (кроме 429) не повторяются никогда."""
    return disposition(exc) is ErrorDisposition.RETRIABLE


def is_duplicate(exc: WazzupError) -> bool:
    """``repeatedCrmMessageId`` — сообщение уже ушло, это успех."""
    return disposition(exc) is ErrorDisposition.DUPLICATE


def needs_human(exc: WazzupError) -> bool:
    """Нужен живой человек: спам-блок, мёртвый канал, закрытое окно Instagram."""
    return disposition(exc) is ErrorDisposition.NEEDS_HUMAN


def is_channel_down(exc: WazzupError) -> bool:
    """Проблема самого канала (а не конкретного сообщения) — общий алерт менеджеру."""
    return isinstance(exc, (WazzupChannelError, WazzupSpamError))


def webhook_error_code(error: Any) -> str:
    """Нормализованный код из ``messages[].error`` / ``statuses[].error``.

    Полный список кодов вебхука не опубликован (открытый вопрос №10), известны
    ``BAD_CONTACT``, ``CHATID_IGSID_MISMATCH``, ``TOO_LONG_TEXT``, ``SPAM``.
    """
    if error is None:
        return ""
    raw = getattr(error, "error", None)
    if raw is None and isinstance(error, dict):
        raw = error.get("error")
    return normalize_code(raw if isinstance(raw, str) else None)


def _message(status: int, code: str | None, description: str | None) -> str:
    """Человекочитаемое сообщение исключения; в логи уходит именно оно."""
    parts = [f"Wazzup {status}"]
    if code:
        parts.append(str(code))
    if description:
        parts.append(str(description))
    return ": ".join(parts)


__all__ = [
    "BAD_CONTACT_CODES",
    "CHANNEL_CODES",
    "DUPLICATE_CODES",
    "ErrorDisposition",
    "FATAL_CODES",
    "NEEDS_HUMAN_CODES",
    "RETRYABLE_STATUSES",
    "SPAM_CODES",
    "classify",
    "classify_response",
    "disposition",
    "is_channel_down",
    "is_duplicate",
    "is_retriable",
    "needs_human",
    "normalize_code",
    "webhook_error_code",
]
