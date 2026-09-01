# ФАЙЛ ЦЕЛИКОМ: app/types.py
"""Общие типы AINAZAROV TOP TEAM.

Единственный модуль, который импортируют все остальные слои. Правила модуля:

* в рантайме не импортирует ничего из ``app.*`` (только под ``TYPE_CHECKING``);
* не читает окружение, не ходит в сеть, не создаёт клиентов;
* всё, что пересекает границу между модулями, объявлено здесь и только здесь.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, Sequence
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from app.kb.models import KBSnapshot

TYPES_SCHEMA_VERSION: Final[int] = 1

#: Жёсткий предел длины одного исходящего сообщения (самый строгий канал — Instagram).
MAX_MESSAGE_CHARS: Final[int] = 1000

#: Телефон Казахстана в E.164. Валидация форматов — только этой регуляркой.
PHONE_E164_KZ_RE: Final[re.Pattern[str]] = re.compile(r"^\+7\d{10}$")

#: Стабильный slug: id зала, id артефакта, id FAQ-записи.
SLUG_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_]+$")

#: Границы возраста, в которых школа принимает детей без участия человека.
#: Живут здесь, а не в ``app.tools.booking``: те же границы проверяет извлечение
#: лида в ``app.llm``, а слою LLM импортировать ``app.tools`` запрещено (§1.1).
MIN_CHILD_AGE: Final[int] = 3
MAX_CHILD_AGE: Final[int] = 17

_PHONE_DIGITS_RE: Final[re.Pattern[str]] = re.compile(r"\D+")


def normalize_phone_kz(raw: str | None) -> str | None:
    """``8 705 123 45 67``, ``+7 705…``, ``7705…`` -> ``+77051234567``. Мусор -> None.

    Одна регулярка на весь проект: номер приходит из трёх мест (инструмент
    записи, извлечение лида, разбор текста клиента), и разойтись они не имеют
    права — иначе один и тот же родитель попадёт в базу дважды.
    """
    if not raw:
        return None
    digits = _PHONE_DIGITS_RE.sub("", str(raw))
    if not digits:
        return None
    if len(digits) == 11 and digits[0] in ("7", "8"):
        national = digits[1:]
    elif len(digits) == 10:
        national = digits
    else:
        return None
    if not national.startswith("7"):
        # Казахстанские номера — мобильные 7XX и городские 7XXX. Всё прочее
        # (российские 9XX, случайные цифры из текста) не наш номер.
        return None
    if len(set(national)) == 1:
        return None  # +77777777777 и подобные — почти всегда выдумка модели
    candidate = f"+7{national}"
    return candidate if PHONE_E164_KZ_RE.match(candidate) else None


# --------------------------------------------------------------------------- #
# Перечисления
# --------------------------------------------------------------------------- #
class Language(str, Enum):
    """Язык диалога. Третьего значения нет: всё остальное — эскалация."""

    RU = "ru"
    KK = "kk"

    @classmethod
    def parse(cls, value: str | None) -> "Language | None":
        """Разбирает строку в язык; неизвестное значение даёт ``None``."""
        if value is None:
            return None
        raw = value.strip().lower().replace("_", "-")
        if not raw:
            return None
        raw = raw.split("-", 1)[0]
        aliases = {"ru": cls.RU, "rus": cls.RU, "kk": cls.KK, "kz": cls.KK, "kaz": cls.KK}
        return aliases.get(raw)


class ChannelKind(str, Enum):
    """Канал общения. Значения совпадают с ``chatType`` Wazzup для этих каналов.

    ``TELEGRAM`` работает двумя путями и одним кодом: через Wazzup (там это тоже
    ``chatType: telegram``) и напрямую через Bot API — так канал поднимается за
    минуту, без агрегатора, и на нём удобно показывать бота заказчику.
    """

    WHATSAPP = "whatsapp"
    INSTAGRAM = "instagram"
    TELEGRAM = "telegram"

    @classmethod
    def from_chat_type(cls, chat_type: str) -> "ChannelKind | None":
        """Отображает ``chatType`` вебхука в канал; прочие значения дают ``None``."""
        raw = (chat_type or "").strip().lower()
        for member in cls:
            if raw == member.value:
                return member
        return None


class Direction(str, Enum):
    IN = "in"
    OUT = "out"


class Author(str, Enum):
    CLIENT = "client"
    BOT = "bot"
    OPERATOR = "operator"
    SYSTEM = "system"


class MsgType(str, Enum):
    """``messages[].type`` Wazzup, дословно; ``unknown`` — для всего непонятого."""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"
    VCARD = "vcard"
    GEO = "geo"
    WAPI_TEMPLATE = "wapi_template"
    UNSUPPORTED = "unsupported"
    MISSING_CALL = "missing_call"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class MessageStatus(str, Enum):
    """Объединение enum'ов ``messages.status`` и ``statuses.status`` + наш ``queued``."""

    QUEUED = "queued"
    INBOUND = "inbound"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    ERROR = "error"
    EDITED = "edited"


class ConversationState(str, Enum):
    NEW = "new"
    ACTIVE = "active"
    PAUSED_OPERATOR = "paused_operator"
    ESCALATED = "escalated"
    CLOSED = "closed"


class PauseReason(str, Enum):
    OPERATOR_REPLY = "operator_reply"
    USER_REQUEST = "user_request"
    ESCALATION = "escalation"
    LLM_FAILURE = "llm_failure"
    POSTCHECK_FAIL = "postcheck_fail"
    SENSITIVE_TOPIC = "sensitive_topic"
    CHILD_DETECTED = "child_detected"
    MANUAL = "manual"
    BUDGET_GUARD = "budget_guard"


class ResumePolicy(str, Enum):
    TIMEOUT = "timeout"
    MANUAL_ONLY = "manual_only"


class LeadStatus(str, Enum):
    TRIAL_BOOKED = "trial_booked"
    THINKING = "thinking"
    NEEDS_CALL = "needs_call"
    ESCALATED = "escalated"
    NOT_TARGET = "not_target"
    NO_SHOW = "no_show"
    CONVERTED = "converted"


class PhoneSource(str, Enum):
    CHANNEL = "channel"
    TYPED = "typed"
    NONE = "none"


class Gender(str, Enum):
    M = "m"
    F = "f"
    UNKNOWN = "unknown"


class EscalationReason(str, Enum):
    """Полный внутренний набор причин.

    В JSON-схему инструмента ``escalate_to_manager`` попадают только значения из
    :data:`TOOL_ESCALATION_REASONS` — остальные ставит код, а не модель.
    """

    USER_REQUEST = "user_request"
    NO_DATA = "no_data"
    COMPLAINT = "complaint"
    MEDICAL = "medical"
    PRICE_OFF_LIST = "price_off_list"
    INSTALLMENTS = "installments"
    AGE_OUT_OF_RANGE = "age_out_of_range"
    FOREIGN_LANGUAGE = "foreign_language"
    REPEATED_MISS = "repeated_miss"
    POSTCHECK_FAIL = "postcheck_fail"
    LLM_FAILURE = "llm_failure"
    INJECTION = "injection"
    CHILD_WRITING = "child_writing"
    OFF_TOPIC = "off_topic"
    BUDGET_GUARD = "budget_guard"


#: Ровно то, что разрешено модели в enum схемы ``escalate_to_manager``.
TOOL_ESCALATION_REASONS: Final[tuple[str, ...]] = (
    "user_request",
    "no_data",
    "complaint",
    "medical",
    "price_off_list",
    "installments",
    "age_out_of_range",
    "foreign_language",
    "repeated_miss",
)


class Urgency(str, Enum):
    NORMAL = "normal"
    HIGH = "high"


class ToolStatus(str, Enum):
    """Статус конверта инструмента. ``error`` наружу в модель не уходит — см. ToolResult."""

    OK = "ok"
    NO_DATA = "no_data"
    NEEDS_OPERATOR = "needs_operator"
    INVALID_INPUT = "invalid_input"
    ERROR = "error"


class RenderHint(str, Enum):
    """Что модели разрешено сделать с результатом инструмента."""

    VERBATIM = "verbatim"          # произнести текст из data/caveats по смыслу дословно
    SUMMARIZE = "summarize"        # можно переформулировать своими словами
    NUMBERS_ONLY = "numbers_only"  # цифры брать только из data, ничего не добавлять
    SILENT = "silent"              # побочный эффект выполнен, упоминать содержимое не нужно
    FIXED_REPLY = "fixed_reply"    # ответ клиенту уже сформирован кодом, модель не переписывает


class OutboundKind(str, Enum):
    BOT_REPLY = "bot_reply"
    ARTIFACT = "artifact"
    ESCALATION_NOTICE = "escalation_notice"
    #: Ответ живого человека, отправленный из CRM. Каналу он не отличается от
    #: ответа бота, а в переписке и метриках должен читаться как работа человека.
    OPERATOR_REPLY = "operator_reply"
    LEAD_CONFIRMATION = "lead_confirmation"
    FOLLOWUP = "followup"
    MANAGER_CARD = "manager_card"
    SYSTEM_NOTICE = "system_notice"


class OutboxState(str, Enum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class DecisionAction(str, Enum):
    """Чем закончилась обработка одного входящего."""

    REPLY = "reply"
    ESCALATE = "escalate"
    SILENT = "silent"
    DROP = "drop"
    DEFER = "defer"


class PostcheckFailKind(str, Enum):
    MONEY = "money"
    TIME = "time"
    WEEKDAY = "weekday"
    PHONE = "phone"
    ADDRESS = "address"
    MEDICAL_FORM = "medical_form"
    GYM_NAME = "gym_name"
    PROMPT_LEAK = "prompt_leak"
    FORBIDDEN_CLAIM = "forbidden_claim"
    TOO_LONG = "too_long"


class GuardFlag(str, Enum):
    INJECTION = "injection"
    OFF_TOPIC = "off_topic"
    CHILD_WRITING = "child_writing"
    STOP_WORD = "stop_word"
    MANAGER_REQUEST = "manager_request"
    ERASE_REQUEST = "erase_request"
    ABUSE = "abuse"


class IntentHint(str, Enum):
    PRICE = "price"
    SIGNUP = "signup"
    SCHEDULE = "schedule"
    LOCATION = "location"
    MANAGER = "manager"
    ERASE = "erase"
    STOP = "stop"
    SAFETY = "safety"
    AGE = "age"
    DOCS = "docs"
    GEAR = "gear"
    PAYMENT = "payment"
    COACHES = "coaches"
    OTHER = "other"


class Scope(str, Enum):
    """География. ``ALL`` — в ``get_gyms``; ``ANY`` — в ``get_kb_fact``."""

    CITY = "city"
    REGION = "region"
    ALL = "all"
    ANY = "any"


class Plan(str, Enum):
    STANDARD = "standard"
    FLEXIBLE = "flexible"
    SINGLE = "single"
    UNKNOWN = "unknown"


class ArtifactKind(str, Enum):
    TEXT_CARD = "text_card"
    IMAGE = "image"
    DOCUMENT = "document"
    LINK = "link"
    LOCATION_TEXT = "location_text"
    #: Видео уходит вложением только в Telegram. В WhatsApp его режет лимит API
    #: (10 МБ), в Instagram Direct видео не отправляется вовсе — там канал сам
    #: подменит артефакт ссылкой (``video_as_link_only``).
    VIDEO = "video"


class GymStatus(str, Enum):
    OPEN = "open"
    UNRESOLVED = "unresolved"
    CLOSED = "closed"


class FactSource(str, Enum):
    OWNER_CONFIRMED = "owner_confirmed"
    DERIVED = "derived"
    GENERIC = "generic"


class FollowupKind(str, Enum):
    FU_SOFT = "fu_soft"
    FU_VALUE = "fu_value"
    TRIAL_REMINDER_20H = "trial_reminder_20h"
    TRIAL_REMINDER_2H = "trial_reminder_2h"
    NO_SHOW = "no_show"


class ManagerCardKind(str, Enum):
    LEAD = "lead"
    ESCALATION = "escalation"
    ALERT = "alert"


class GapRef(str, Enum):
    """Реестр пробелов и конфликтов данных (KB-SPEC §9.2)."""

    G1 = "G-1"
    G2 = "G-2"
    G3 = "G-3"
    G4 = "G-4"
    G5 = "G-5"
    G6 = "G-6"
    G7 = "G-7"
    G8 = "G-8"
    G9 = "G-9"
    G10 = "G-10"
    G11 = "G-11"
    G12 = "G-12"
    G13 = "G-13"
    G14 = "G-14"
    G15 = "G-15"
    C3 = "C-3"
    C4 = "C-4"
    C5 = "C-5"


# --------------------------------------------------------------------------- #
# Ограничения каналов
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ChannelLimits:
    """Технические границы канала. Источник — research-wazzup24 §2.3, §2.4, §9."""

    max_text_chars: int
    soft_text_chars: int
    allows_media: bool
    allowed_mime: frozenset[str]
    max_file_bytes: int
    service_window_hours: int | None


CHANNEL_LIMITS: Final[dict[ChannelKind, ChannelLimits]] = {
    ChannelKind.WHATSAPP: ChannelLimits(
        max_text_chars=1000,
        soft_text_chars=600,
        allows_media=True,
        allowed_mime=frozenset({"image/jpeg", "image/png", "application/pdf"}),
        max_file_bytes=10 * 1024 * 1024,
        service_window_hours=None,  # НП: личный WhatsApp окна не имеет, WABA — 24 ч
    ),
    ChannelKind.INSTAGRAM: ChannelLimits(
        max_text_chars=1000,
        soft_text_chars=250,
        allows_media=True,
        allowed_mime=frozenset({"image/jpeg", "image/png", "image/bmp"}),
        max_file_bytes=8 * 1024 * 1024,
        service_window_hours=168,
    ),
    # Telegram Bot API: текст 4096 знаков, sendVideo до 50 МБ, окна обслуживания нет.
    # Единственный наш канал, куда видео уходит вложением, а не ссылкой.
    # Мягкий предел держим на уровне остальных: длинные простыни в мессенджере
    # одинаково плохи везде, а бот должен вести себя одинаково во всех каналах.
    ChannelKind.TELEGRAM: ChannelLimits(
        max_text_chars=4096,
        soft_text_chars=600,
        allows_media=True,
        allowed_mime=frozenset(
            {
                "image/jpeg",
                "image/png",
                "application/pdf",
                "video/mp4",
                "video/quicktime",
            }
        ),
        max_file_bytes=50 * 1024 * 1024,
        service_window_hours=None,
    ),
}


# --------------------------------------------------------------------------- #
# Сообщения
# --------------------------------------------------------------------------- #
class InboundMessage(BaseModel):
    """Нормализованное входящее сообщение — один элемент ``webhook.messages[]``.

    Создаётся только в ``app.channels.normalize``. Ниже по потоку сырой payload
    Wazzup больше нигде не разбирается.
    """

    model_config = ConfigDict(frozen=True)

    message_id: str
    channel_id: str
    channel: ChannelKind
    chat_type_raw: str
    chat_id: str
    conv_key: str
    direction: Direction
    author: Author
    msg_type: MsgType
    text: str | None = None
    content_uri: str | None = None
    status: MessageStatus
    is_echo: bool = False
    sent_from_app: bool | None = None  # НП: проверить на проде (ARCHITECTURE §15 п.1)
    author_name: str | None = None
    author_id: str | None = None
    contact_name: str | None = None
    contact_username: str | None = None
    contact_phone: str | None = None
    phone_e164: str | None = None
    channel_dt: datetime
    received_at: datetime
    error_code: str | None = None
    error_description: str | None = None
    is_edited: bool | None = None
    is_deleted: bool | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @staticmethod
    def make_conv_key(channel_id: str, chat_type: str, chat_id: str) -> str:
        """Естественный ключ диалога: ``f"{channel_id}:{chat_type}:{chat_id}"``."""
        return f"{channel_id}:{chat_type}:{chat_id}"


class OutboundMessage(BaseModel):
    """Одно исходящее сообщение. Ровно одно из ``text`` / ``content_uri``."""

    model_config = ConfigDict(frozen=True)

    crm_message_id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID | None = None
    channel_id: str
    channel: ChannelKind
    chat_id: str
    lang: Language
    kind: OutboundKind = OutboundKind.BOT_REPLY
    text: str | None = None
    content_uri: str | None = None
    artifact_id: str | None = None
    ref_message_id: str | None = None
    clear_unanswered: Literal[False] = False
    delay_ms: int = 0

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> "OutboundMessage":
        """Wazzup запрещает передавать ``text`` и ``contentUri`` одновременно."""
        has_text = bool(self.text and self.text.strip())
        has_uri = bool(self.content_uri and self.content_uri.strip())
        if has_text == has_uri:
            raise ValueError("OutboundMessage: нужен ровно один из text / content_uri")
        if has_text and len(self.text or "") > MAX_MESSAGE_CHARS:
            raise ValueError(
                f"OutboundMessage.text длиннее {MAX_MESSAGE_CHARS} знаков — требуется сплит"
            )
        return self


class StatusUpdate(BaseModel):
    """Элемент ``webhook.statuses[]`` после нормализации."""

    model_config = ConfigDict(frozen=True)

    message_id: str
    status: MessageStatus
    timestamp: datetime
    error_code: str | None = None
    error_description: str | None = None


class ChannelState(BaseModel):
    """Строка ответа ``GET /v3/channels`` или элемент ``channelsUpdates``."""

    model_config = ConfigDict(frozen=True)

    channel_id: str
    transport: str
    plain_id: str | None = None
    state: str
    is_active: bool


# --------------------------------------------------------------------------- #
# Лид
# --------------------------------------------------------------------------- #
class LeadDraft(BaseModel):
    """Накопительный черновик лида. Изменяемый: дополняется по ходу диалога."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID | None = None
    lead_id: UUID | None = None
    channel: ChannelKind | None = None
    channel_user: str | None = None
    instagram_username: str | None = None
    lang: Language | None = None
    parent_name: str | None = None
    parent_relation: str | None = None
    phone: str | None = None
    phone_source: PhoneSource = PhoneSource.NONE
    child_name: str | None = None
    child_age: int | None = None
    child_birth_year: int | None = None
    child_gender: Gender = Gender.UNKNOWN
    district: str | None = None
    gym_id: str | None = None
    trial_slot: datetime | None = None
    trial_slot_text: str | None = None
    motivation: str | None = None
    main_objection: str | None = None
    prior_experience: str | None = None
    health_notes: str | None = None
    status: LeadStatus = LeadStatus.THINKING
    escalation: bool = False
    dialog_url: str | None = None
    messages_count: int = 0

    def missing_required(self) -> tuple[str, ...]:
        """Каких обязательных полей не хватает для ``status=trial_booked``.

        Обязательны: ``child_name``, ``child_age``, ``gym_id``, ``lang``, а также
        любой способ связи — ``phone`` либо ``channel_user``.
        """
        missing: list[str] = []
        if not (self.child_name or "").strip():
            missing.append("child_name")
        if self.child_age is None:
            missing.append("child_age")
        if not (self.gym_id or "").strip():
            missing.append("gym_id")
        if self.lang is None:
            missing.append("lang")
        if not ((self.phone or "").strip() or (self.channel_user or "").strip()):
            missing.append("contact")
        return tuple(missing)

    def merge(self, other: "LeadDraft") -> "LeadDraft":
        """Возвращает новый черновик: непустые поля ``other`` перекрывают текущие."""
        merged = self.model_dump()
        defaults = {
            "phone_source": PhoneSource.NONE,
            "child_gender": Gender.UNKNOWN,
            "status": LeadStatus.THINKING,
            "escalation": False,
            "messages_count": 0,
        }
        for name in type(self).model_fields:
            value = getattr(other, name)
            if value is None:
                continue
            if name in defaults and value == defaults[name]:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            merged[name] = value
        return LeadDraft(**merged)


class ManagerCard(BaseModel):
    """Карточка, уходящая администратору в его канал."""

    model_config = ConfigDict(frozen=True)

    kind: ManagerCardKind
    text: str
    conversation_id: UUID | None = None
    lead_id: UUID | None = None
    lang: Language = Language.RU
    reason: EscalationReason | None = None
    urgency: Urgency = Urgency.NORMAL


# --------------------------------------------------------------------------- #
# Инструменты
# --------------------------------------------------------------------------- #
class ToolSpec(BaseModel):
    """Описание инструмента, пригодное для превращения в ``FunctionDeclaration``.

    Собирается из KB в ``app.tools.registry.build_tool_specs`` и передаётся в
    ``app.llm`` как данные — чтобы LLM-слой не зависел от слоя инструментов.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    parameters: dict[str, Any]
    deterministic: bool
    side_effect: bool = False


class ToolResult(BaseModel):
    """Единая форма ответа любого из восьми инструментов.

    ``ok`` — производное от ``status``: ``True`` только при ``ToolStatus.OK``.
    ``error`` заполняется исключительно при ``INVALID_INPUT`` и ``ERROR``.
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    status: ToolStatus
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    render_hint: RenderHint = RenderHint.SUMMARIZE
    caveats: tuple[str, ...] = ()
    say_if_no_data: dict[str, str] | None = None
    gap_ref: GapRef | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def success(
        cls,
        data: dict[str, Any],
        *,
        render_hint: RenderHint = RenderHint.SUMMARIZE,
        caveats: Sequence[str] = (),
        meta: dict[str, Any] | None = None,
    ) -> "ToolResult":
        """Успешный результат: ``ok=True``, ``status=ok``."""
        return cls(
            ok=True,
            status=ToolStatus.OK,
            data=data,
            render_hint=render_hint,
            caveats=tuple(caveats),
            meta=meta or {},
        )

    @classmethod
    def no_data(
        cls,
        say: dict[str, str],
        *,
        gap_ref: GapRef | None = None,
        data: dict[str, Any] | None = None,
    ) -> "ToolResult":
        """Данных нет. ``say`` обязан содержать ключи ``ru`` и ``kk``."""
        missing = {"ru", "kk"} - set(say)
        if missing:
            raise ValueError(f"say_if_no_data без ключей: {sorted(missing)}")
        return cls(
            ok=False,
            status=ToolStatus.NO_DATA,
            data=data or {},
            render_hint=RenderHint.VERBATIM,
            say_if_no_data=dict(say),
            gap_ref=gap_ref,
        )

    @classmethod
    def needs_operator(
        cls,
        say: dict[str, str],
        *,
        reason: EscalationReason,
        gap_ref: GapRef | None = None,
    ) -> "ToolResult":
        """Вопрос вне компетенции бота: ответ обязан привести к эскалации."""
        missing = {"ru", "kk"} - set(say)
        if missing:
            raise ValueError(f"say_if_no_data без ключей: {sorted(missing)}")
        return cls(
            ok=False,
            status=ToolStatus.NEEDS_OPERATOR,
            render_hint=RenderHint.VERBATIM,
            say_if_no_data=dict(say),
            gap_ref=gap_ref,
            meta={"escalation_reason": reason.value},
        )

    @classmethod
    def invalid_input(cls, error: str) -> "ToolResult":
        """Модель прислала невалидные аргументы. В диалог это не выносится."""
        return cls(
            ok=False,
            status=ToolStatus.INVALID_INPUT,
            error=error,
            render_hint=RenderHint.SILENT,
        )

    @classmethod
    def failure(cls, error: str) -> "ToolResult":
        """Внутренний сбой инструмента. Пайплайн трактует как промах бота."""
        return cls(
            ok=False,
            status=ToolStatus.ERROR,
            error=error,
            render_hint=RenderHint.SILENT,
        )

    def to_llm_payload(self) -> dict[str, Any]:
        """Конверт для ``FunctionResponse.response``.

        Форма: ``{"status": ..., "data": {...}, "caveats": [...],
        "say_if_no_data": {"ru": ..., "kk": ...}}``. Пустые ключи опускаются.
        ``ERROR`` отдаётся модели как ``needs_operator`` — модель не должна видеть трейсы.
        """
        status = self.status
        if status is ToolStatus.ERROR:
            status = ToolStatus.NEEDS_OPERATOR
        payload: dict[str, Any] = {"status": status.value}
        if self.data:
            payload["data"] = self.data
        if self.caveats:
            payload["caveats"] = list(self.caveats)
        if self.say_if_no_data:
            payload["say_if_no_data"] = dict(self.say_if_no_data)
        if status is ToolStatus.INVALID_INPUT and self.error:
            payload["error"] = self.error
        return payload


class ToolInvocation(BaseModel):
    """Факт вызова инструмента в текущем ходу — вход для пост-фильтра и логов."""

    model_config = ConfigDict(frozen=True)

    call_id: str | None
    name: str
    args: dict[str, Any]
    result: ToolResult
    latency_ms: int
    loop_index: int


class ToolServices(Protocol):
    """Побочные эффекты, доступные инструментам. Реализация — в ``app.core.pipeline``.

    Инструменты не открывают транзакции и не ходят в Wazzup напрямую: всё через
    этот протокол, поэтому в тестах он подменяется фейком.
    """

    async def enqueue_outbound(self, message: OutboundMessage) -> UUID:
        """Кладёт исходящее в outbox, возвращает ``outbox_message.id``."""
        ...

    async def upsert_lead(self, draft: LeadDraft) -> UUID:
        """Создаёт или обновляет лид диалога (идемпотентно), возвращает ``lead.id``."""
        ...

    async def notify_manager(self, card: ManagerCard) -> None:
        """Ставит карточку менеджеру в очередь отправки."""
        ...

    async def set_pause(
        self, conv_key: str, *, minutes: int, reason: PauseReason
    ) -> None:
        """Ставит или продлевает паузу бота."""
        ...

    async def count_artifact_sends(self, conversation_id: UUID, artifact_id: str) -> int:
        """Сколько раз артефакт уже уходил в этом диалоге."""
        ...


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Контекст одного хода, который получают все инструменты.

    Обычный ``dataclass``, а не pydantic-модель: несёт живые объекты (снимок KB,
    сервисы) и не должен их валидировать.
    """

    conversation_id: UUID
    conv_key: str
    channel: ChannelKind
    channel_id: str
    chat_id: str
    lang: Language
    kb: "KBSnapshot"
    kb_hash: str
    now: datetime
    correlation_id: str
    services: ToolServices
    lead_draft: LeadDraft = field(default_factory=LeadDraft)
    intents: tuple[IntentHint, ...] = ()
    injection_suspected: bool = False


class ToolExecutor(Protocol):
    """Исполнитель инструментов, который ``app.core`` передаёт в ``app.llm``."""

    async def __call__(self, name: str, args: dict[str, Any]) -> ToolResult:
        ...


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #
class LLMUsage(BaseModel):
    """Потребление одного вызова модели. Источник — ``response.usage_metadata``."""

    model_config = ConfigDict(frozen=True)

    model: str
    prompt_tokens: int = 0
    cached_tokens: int = 0
    candidates_tokens: int = 0
    thoughts_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    finish_reason: str | None = None


class LLMRequest(BaseModel):
    """Вход одного хода модели. ``history`` — JSON-дампы ``types.Content``."""

    model_config = ConfigDict(frozen=True)

    system_instruction: str
    history: list[dict[str, Any]]
    user_text: str
    dynamic_note: str
    tool_specs: tuple[ToolSpec, ...] = ()
    allowed_function_names: tuple[str, ...] | None = None
    tool_mode: Literal["AUTO", "ANY", "NONE", "VALIDATED"] = "AUTO"
    lang: Language = Language.RU
    correlation_id: str = ""
    max_output_tokens: int | None = None
    model: str | None = None


class LLMResponse(BaseModel):
    """Результат хода после отработки всего tool-loop."""

    model_config = ConfigDict(frozen=True)

    text: str | None
    blocked: bool = False
    block_reason: str | None = None
    finish_reason: str | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)
    invocations: tuple[ToolInvocation, ...] = ()
    usage: tuple[LLMUsage, ...] = ()
    model_used: str = ""
    loops: int = 0
    fallback_used: bool = False


# --------------------------------------------------------------------------- #
# Пайплайн
# --------------------------------------------------------------------------- #
class PipelineDecision(BaseModel):
    """Итог обработки одного входящего. Возвращается ``app.core.pipeline.process``."""

    model_config = ConfigDict(frozen=True)

    action: DecisionAction
    reason: str
    conversation_id: UUID | None = None
    conv_key: str | None = None
    lang: Language | None = None
    outbound: tuple[OutboundMessage, ...] = ()
    manager_cards: tuple[ManagerCard, ...] = ()
    lead_id: UUID | None = None
    escalation_reason: EscalationReason | None = None
    guard_flags: tuple[GuardFlag, ...] = ()
    postcheck_fail: PostcheckFailKind | None = None
    invocations: tuple[ToolInvocation, ...] = ()
    usage: tuple[LLMUsage, ...] = ()
    kb_hash: str | None = None
    correlation_id: str = ""


class JobQueue(Protocol):
    """Очередь фоновых задач. Две реализации: ARQ и inline (``INLINE_WORKER=true``)."""

    async def enqueue_inbound(self, payload: dict[str, Any]) -> str:
        """Кладёт сырой payload вебхука на обработку. Возвращает job id."""
        ...

    async def enqueue_outbox(self, outbox_id: UUID, *, delay_ms: int = 0) -> str:
        """Ставит отправку строки outbox."""
        ...

    async def enqueue_followup(self, task_id: UUID, *, run_at: datetime) -> str:
        """Планирует follow-up на конкретное время."""
        ...

    async def startup(self) -> None:
        ...

    async def shutdown(self) -> None:
        ...


# --------------------------------------------------------------------------- #
# Исключения
# --------------------------------------------------------------------------- #
class BotError(Exception):
    """База всех своих исключений. Ловится на границе воркера и API."""

    code: str = "bot_error"
    retryable: bool = False

    def __init__(self, message: str, *, code: str | None = None, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable


class ConfigError(BotError):
    """Настройки не позволяют работать. Кидается только на старте."""

    code = "config_error"


class KBValidationError(BotError):
    """KB не прошла валидацию. ``errors`` — список ``"файл: поле: сообщение"``."""

    code = "kb_validation_error"

    def __init__(self, message: str, *, errors: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.errors: tuple[str, ...] = tuple(errors)


class KBNotLoadedError(BotError):
    """Снимок KB запрошен до загрузки."""

    code = "kb_not_loaded"


class StorageError(BotError):
    code = "storage_error"


class WazzupError(BotError):
    """Ошибка HTTP-вызова Wazzup. Коды сравниваются нормализованно."""

    code = "wazzup_error"

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error_code: str | None = None,
        description: str | None = None,
        data: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.description = description
        self.data = data or {}
        self.request_id = request_id

    @property
    def normalized_code(self) -> str:
        """``code.replace("_", "").lower()`` — регистр в доке Wazzup непоследователен."""
        return (self.error_code or "").replace("_", "").lower()


class WazzupRateLimitError(WazzupError):
    """429. Ретраить с backoff."""

    code = "wazzup_rate_limit"
    retryable = True


class WazzupServerError(WazzupError):
    """5xx. Ретраить с backoff."""

    code = "wazzup_server_error"
    retryable = True


class WazzupDuplicateError(WazzupError):
    """``repeatedcrmmessageid``. Трактуется вызывающим как успех отправки."""

    code = "wazzup_duplicate"


class WazzupChannelError(WazzupError):
    """``CHANNEL_*``, ``MESSAGE_CHANNEL_UNAVAILABLE``. Канал неисправен — общий алерт."""

    code = "wazzup_channel_error"


class WazzupSpamError(WazzupError):
    """``MESSAGES_IS_SPAM``. Стоп канала и тревога менеджеру."""

    code = "wazzup_spam"


class WazzupBadContactError(WazzupError):
    """``BAD_CONTACT``, ``CHATID_IGSID_MISMATCH``. Пометить лид, не ретраить."""

    code = "wazzup_bad_contact"


class LLMError(BotError):
    code = "llm_error"


class LLMTimeoutError(LLMError):
    code = "llm_timeout"
    retryable = True


class LLMRateLimitError(LLMError):
    """``rate_limit_exceeded`` — лечится backoff."""

    code = "llm_rate_limit"
    retryable = True


class LLMQuotaError(LLMError):
    """``quota_exceeded`` — backoff бесполезен, переходим в режим «только KB»."""

    code = "llm_quota"


class LLMBlockedError(LLMError):
    """``prompt_feedback.block_reason`` или недопустимый ``finish_reason``."""

    code = "llm_blocked"


class LLMToolLoopError(LLMError):
    """Превышен лимит витков tool-loop."""

    code = "llm_tool_loop"


class BudgetExceededError(BotError):
    """Суточный лимит расходов на модель исчерпан."""

    code = "budget_exceeded"


class ToolExecutionError(BotError):
    """Инструмент упал непредвиденно. Бизнес-исходы возвращаются через ToolResult."""

    code = "tool_execution_error"


class PostcheckFailedError(BotError):
    """Ответ модели не прошёл анти-галлюцинационный фильтр."""

    code = "postcheck_failed"

    def __init__(self, message: str, *, kind: PostcheckFailKind, offending: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.kind = kind
        self.offending: tuple[str, ...] = tuple(offending)


class WebhookValidationError(BotError):
    """Тело вебхука не прошло валидацию. Наружу всё равно отдаём 200 OK."""

    code = "webhook_validation_error"
