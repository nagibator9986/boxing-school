"""Нормализация вебхука Wazzup во внутренние типы.

Ниже по потоку сырой payload Wazzup больше нигде не разбирается: пайплайн видит
только :class:`app.types.InboundMessage`.

Два критичных для проекта места.

**1. Эхо.** Все исходящие возвращаются вебхуком с ``isEcho=true`` — и наши,
и написанные оператором руками. Входящее клиента — это ``isEcho=false`` **и**
``status="inbound"`` (S5, §5.5). Не отфильтруешь — бот начинает отвечать сам себе,
и цикл не остановится: каждый его ответ снова придёт вебхуком.

**2. Вход живого оператора.** Отдельного события «оператор вошёл в диалог»
в API v3 **нет** (research §11). Собираем сигнал из двух признаков:

* ``sentFromApp=true`` — «отправлено из нативного чата Wazzup» (S5). Признак прямой,
  но **НЕ ПОДТВЕРЖДЕНО**, приходит ли ``false`` для сообщений, отправленных через API
  (открытый вопрос №12);
* эхо с ``messageId``, которого нет в нашем outbox. Признак косвенный, но надёжный:
  мы точно знаем, что отправляли сами.

Реализованы оба. Основной — второй. Расхождение сигналов не проглатывается,
а выносится наружу (:func:`echo_signal_mismatch`) и считается метрикой
``echo_signal_mismatch_total`` — по ней и закрывается открытый вопрос №12.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from app.channels.wazzup_schemas import WebhookPayload, WzChannelUpdate, WzMessage, WzStatus
from app.types import (
    Author,
    ChannelKind,
    ChannelState,
    Direction,
    InboundMessage,
    MessageStatus,
    MsgType,
    StatusUpdate,
)

#: Имя метрики расхождения признаков «оператор вмешался». Счётчик — суффикс ``_total``.
ECHO_SIGNAL_MISMATCH_METRIC: Final[str] = "echo_signal_mismatch_total"

#: Событие лога того же расхождения.
ECHO_SIGNAL_MISMATCH_EVENT: Final[str] = "echo_signal_mismatch"

#: ``state`` канала, при котором им можно пользоваться (сравнение регистронезависимо).
ACTIVE_CHANNEL_STATE: Final[str] = "active"

_DIGITS_RE: Final[re.Pattern[str]] = re.compile(r"\D+")
#: Дробная часть длиннее 6 знаков ломает ``datetime.fromisoformat``.
_FRACTION_RE: Final[re.Pattern[str]] = re.compile(r"(\.\d{6})\d+")
#: Длина номера в E.164: от 8 до 15 цифр вместе с кодом страны.
_MIN_PHONE_DIGITS: Final[int] = 8
_MAX_PHONE_DIGITS: Final[int] = 15


# --------------------------------------------------------------------------- #
# Разбор примитивов
# --------------------------------------------------------------------------- #
def parse_datetime(value: str) -> datetime:
    """``yyyy-mm-ddThh:mm:ss.msZ`` → aware datetime в UTC.

    Наивное время трактуется как UTC: Wazzup отдаёт время с ``Z`` (S5),
    а расчёты по Asia/Almaty делает уже пайплайн.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Пустая строка времени")
    normalized = _FRACTION_RE.sub(r"\1", raw)
    if normalized.endswith(("z", "Z")):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_datetime_safe(value: str | None, *, default: datetime) -> datetime:
    """То же, что :func:`parse_datetime`, но неразобранное время не роняет приём вебхука."""
    if not value:
        return default
    try:
        return parse_datetime(value)
    except (ValueError, TypeError):
        return default


def parse_timestamp_ms(value: int | None, *, default: datetime | None = None) -> datetime | None:
    """``timestamp`` в миллисекундах (``channelsUpdates``) → aware datetime в UTC."""
    if value is None:
        return default
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return default


def channel_from_chat_type(chat_type: str | None) -> ChannelKind | None:
    """``chatType`` → канал. ``None`` для остальных восьми значений enum Wazzup."""
    raw = (chat_type or "").strip().lower()
    if not raw:
        return None
    try:
        return ChannelKind.from_chat_type(raw)
    except NotImplementedError:
        # Тело метода дописывает волна 1a. Значения enum совпадают с chatType,
        # поэтому до тех пор работает прямое сопоставление по значению.
        pass
    try:
        return ChannelKind(raw)
    except ValueError:
        return None


def parse_msg_type(value: str | None) -> MsgType:
    """``messages[].type`` → :class:`app.types.MsgType`; всё непонятое — ``unknown``."""
    raw = (value or "").strip().lower()
    if not raw:
        return MsgType.TEXT
    try:
        return MsgType(raw)
    except ValueError:
        return MsgType.UNKNOWN


def parse_status(value: str | None, *, is_echo: bool) -> MessageStatus:
    """``status`` сообщения → :class:`app.types.MessageStatus`.

    Enum'ы ``messages.status`` и ``statuses.status`` не совпадают: в первом есть
    ``inbound``, но нет ``edited``, во втором наоборот. Неизвестное значение
    трактуем по направлению: эхо — уже отправлено, не эхо — входящее.
    """
    raw = (value or "").strip().lower()
    if raw:
        try:
            return MessageStatus(raw)
        except ValueError:
            pass
    return MessageStatus.SENT if is_echo else MessageStatus.INBOUND


def phone_from_chat_id(chat_id: str, channel: ChannelKind) -> str | None:
    """WhatsApp: ``77012345678`` → ``+77012345678``. Instagram → ``None``.

    В Instagram ``chatId`` — это аккаунт или IGSID, номера там нет и быть не может.
    """
    if channel is not ChannelKind.WHATSAPP:
        return None
    digits = _DIGITS_RE.sub("", chat_id or "")
    if _MIN_PHONE_DIGITS <= len(digits) <= _MAX_PHONE_DIGITS:
        return f"+{digits}"
    return None


def chat_id_from_phone(phone_e164: str) -> str:
    """``+77012345678`` → ``77012345678`` (только цифры, без ``+``).

    Для Instagram ``chatId`` никогда не конструируется — он берётся только из вебхука.
    """
    return _DIGITS_RE.sub("", phone_e164 or "")


# --------------------------------------------------------------------------- #
# Вебхук → внутренние модели
# --------------------------------------------------------------------------- #
def to_inbound(msg: WzMessage, *, received_at: datetime) -> InboundMessage | None:
    """Вебхук → внутренняя модель. ``None``, если ``chatType`` не whatsapp/instagram.

    Возвращает и эхо тоже: решение «это наш ответ / это оператор / это клиент»
    принимает пайплайн, у него есть outbox. Здесь только нормализация.
    """
    channel = channel_from_chat_type(msg.chatType)
    if channel is None:
        return None

    is_echo = bool(msg.isEcho)
    status = parse_status(msg.status, is_echo=is_echo)
    contact = msg.contact
    chat_id = msg.chatId or ""
    phone = phone_from_chat_id(chat_id, channel)
    if phone is None and contact is not None and contact.phone:
        digits = _DIGITS_RE.sub("", contact.phone)
        if _MIN_PHONE_DIGITS <= len(digits) <= _MAX_PHONE_DIGITS:
            phone = f"+{digits}"

    return InboundMessage(
        message_id=msg.messageId,
        channel_id=msg.channelId,
        channel=channel,
        chat_type_raw=msg.chatType,
        chat_id=chat_id,
        conv_key=InboundMessage.make_conv_key(msg.channelId, msg.chatType, chat_id),
        direction=Direction.OUT if is_echo else Direction.IN,
        author=_author_of(msg, is_echo=is_echo),
        msg_type=parse_msg_type(msg.type),
        text=msg.text,
        content_uri=msg.contentUri,
        status=status,
        is_echo=is_echo,
        sent_from_app=msg.sentFromApp,  # НП: проверить на проде (открытый вопрос №12)
        author_name=msg.authorName,
        author_id=msg.authorId,
        contact_name=contact.name if contact else None,
        contact_username=contact.username if contact else None,
        contact_phone=contact.phone if contact else None,
        phone_e164=phone,
        channel_dt=parse_datetime_safe(msg.dateTime, default=received_at),
        received_at=received_at,
        error_code=msg.error.error if msg.error else None,
        error_description=msg.error.description if msg.error else None,
        is_edited=msg.isEdited,
        is_deleted=msg.isDeleted,
        raw=msg.model_dump(mode="json", exclude_none=True),
    )


def to_inbound_batch(payload: WebhookPayload, *, received_at: datetime) -> list[InboundMessage]:
    """Все сообщения вебхука, которые относятся к нашим двум каналам."""
    result: list[InboundMessage] = []
    for msg in payload.message_list():
        normalized = to_inbound(msg, received_at=received_at)
        if normalized is not None:
            result.append(normalized)
    return result


def to_status(update: WzStatus) -> StatusUpdate:
    """Элемент ``statuses[]`` → :class:`app.types.StatusUpdate`.

    Вебхук статусов приходит только на исходящие, поэтому неизвестное значение
    статуса безопаснее трактовать как ``sent``, а не как ошибку.
    """
    return StatusUpdate(
        message_id=update.messageId,
        status=parse_status(update.status, is_echo=True),
        timestamp=parse_datetime_safe(update.timestamp, default=_utc_now()),
        error_code=update.error.error if update.error else None,
        error_description=update.error.description if update.error else None,
    )


def to_channel_state(update: WzChannelUpdate) -> ChannelState:
    """Элемент ``channelsUpdates[]`` → :class:`app.types.ChannelState`.

    ``transport`` в этом вебхуке не приходит (его отдаёт только ``GET /v3/channels``),
    поэтому ставим ``unknown``. Значение ``state`` сравнивается регистронезависимо:
    наборы и регистр в вебхуке и в REST не совпадают (``qr`` против ``qridle``,
    ``openElsewhere`` против ``openelsewhere``).
    """
    state = (update.state or "").strip()
    return ChannelState(
        channel_id=update.channelId,
        transport="unknown",
        plain_id=None,
        state=state,
        is_active=is_active_state(state),
    )


def is_active_state(state: str | None) -> bool:
    """``state == "active"`` без учёта регистра; всё неизвестное — не active."""
    return (state or "").strip().lower() == ACTIVE_CHANNEL_STATE


# --------------------------------------------------------------------------- #
# Эхо и вход оператора
# --------------------------------------------------------------------------- #
def is_client_inbound(msg: InboundMessage) -> bool:
    """Настоящее входящее от клиента: ``isEcho=false`` **и** ``status="inbound"``.

    Двойная проверка намеренная: ``isEcho`` — основной признак, ``status`` — подстраховка
    на случай, если поле не пришло. Всё остальное отвечать не нужно.
    """
    return not msg.is_echo and msg.status is MessageStatus.INBOUND


def is_operator_echo(msg: InboundMessage, *, known_outbox: bool) -> bool:
    """Эхо оператора: ``is_echo`` и (``sent_from_app is True`` или ``not known_outbox``).

    ``known_outbox`` — результат ``repo_outbox.exists_by_wazzup_message_id``.
    Второй признак основной: приходит ли ``sentFromApp=false`` для API-сообщений,
    НЕ ПОДТВЕРЖДЕНО.
    """
    if not msg.is_echo:
        return False
    return msg.sent_from_app is True or not known_outbox


@dataclass(frozen=True, slots=True)
class EchoSignals:
    """Результат обоих признаков «в диалог вошёл живой оператор».

    ``mismatch=True`` означает, что признаки разошлись: либо ``sentFromApp`` для
    API-сообщений ведёт себя не так, как мы предполагали, либо в outbox дыра.
    Обе причины требуют разбора, поэтому расхождение считается метрикой
    :data:`ECHO_SIGNAL_MISMATCH_METRIC`, а не молча игнорируется.
    """

    is_echo: bool
    #: Прямой признак: ``sentFromApp``. ``None`` — поле не пришло.
    by_sent_from_app: bool | None
    #: Основной признак: ``messageId`` отсутствует в нашем outbox.
    by_outbox: bool
    #: Итог, совпадает с :func:`is_operator_echo`.
    is_operator: bool
    #: Признаки дали разный ответ.
    mismatch: bool

    def as_log_fields(self) -> dict[str, Any]:
        """Поля для структурного лога события ``echo_signal_mismatch``."""
        return {
            "is_echo": self.is_echo,
            "sent_from_app": self.by_sent_from_app,
            "unknown_outbox": self.by_outbox,
            "is_operator": self.is_operator,
        }


def echo_signals(msg: InboundMessage, *, known_outbox: bool) -> EchoSignals:
    """Считает оба признака входа оператора и их расхождение.

    Расхождением считается ситуация, когда поле ``sentFromApp`` пришло (не ``None``)
    и противоречит выводу по outbox: например, эхо помечено ``sentFromApp=false``,
    но такого ``messageId`` мы не отправляли (значит, оператор всё-таки писал),
    либо ``sentFromApp=true`` на сообщении, которое лежит в нашем outbox.
    """
    if not msg.is_echo:
        return EchoSignals(
            is_echo=False,
            by_sent_from_app=msg.sent_from_app,
            by_outbox=False,
            is_operator=False,
            mismatch=False,
        )
    by_app = msg.sent_from_app
    by_outbox = not known_outbox
    operator = by_app is True or by_outbox
    mismatch = by_app is not None and by_app is not by_outbox
    return EchoSignals(
        is_echo=True,
        by_sent_from_app=by_app,
        by_outbox=by_outbox,
        is_operator=operator,
        mismatch=mismatch,
    )


def echo_signal_mismatch(msg: InboundMessage, *, known_outbox: bool) -> bool:
    """``True``, если два признака входа оператора противоречат друг другу.

    Вызывающий инкрементит метрику :data:`ECHO_SIGNAL_MISMATCH_METRIC`. Слой каналов
    сам метрики не пишет — он не знает про ``app.observability`` по правилу зависимостей.
    """
    return echo_signals(msg, known_outbox=known_outbox).mismatch


# --------------------------------------------------------------------------- #
# Внутреннее
# --------------------------------------------------------------------------- #
def _author_of(msg: WzMessage, *, is_echo: bool) -> Author:
    """Предварительный автор. Точный вердикт по эху даёт :func:`is_operator_echo` с outbox."""
    if not is_echo:
        return Author.CLIENT
    if msg.sentFromApp is True:
        return Author.OPERATOR
    return Author.BOT


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


__all__ = [
    "ACTIVE_CHANNEL_STATE",
    "ECHO_SIGNAL_MISMATCH_EVENT",
    "ECHO_SIGNAL_MISMATCH_METRIC",
    "EchoSignals",
    "channel_from_chat_type",
    "chat_id_from_phone",
    "echo_signal_mismatch",
    "echo_signals",
    "is_active_state",
    "is_client_inbound",
    "is_operator_echo",
    "parse_datetime",
    "parse_datetime_safe",
    "parse_msg_type",
    "parse_status",
    "parse_timestamp_ms",
    "phone_from_chat_id",
    "to_channel_state",
    "to_inbound",
    "to_inbound_batch",
    "to_status",
]
