# INTERFACES.md — замороженный контракт AINAZAROV TOP TEAM

**Дата заморозки:** 2026-08-09. **Статус:** контракт. Восемь разработчиков собирают модули параллельно,
не видя реализации друг друга.

> **Приоритет документов:** `SCOPE-OVERRIDE.md` → **этот файл** → `ARCHITECTURE.md` → `KB-SPEC.md` → research-*.
> Там, где `ARCHITECTURE.md` недосказан или противоречит себе, решение принято здесь и помечено **[РЕШЕНО]**.
> Юридический слой (consent / pii / privacy / crypto / retention / audit) исключён целиком —
> ни одного упоминания ниже нет, и добавлять его нельзя.

## 0. Как пользоваться этим файлом

1. Блоки кода, помеченные `# ФАЙЛ ЦЕЛИКОМ`, копируются в репозиторий **байт-в-байт**. Дописывать в них
   можно только то, что явно разрешено в комментарии.
2. Все прочие блоки — **сигнатуры**. Тело реализует владелец файла. Менять имя, порядок и типы
   параметров, тип возврата — **нельзя**. Добавлять новые публичные функции — можно; удалять и
   переименовывать — нет.
3. Всё, что не объявлено публичным здесь, считается приватным (префикс `_`) и не может быть импортировано
   другим модулем.
4. Любой модуль обязан импортироваться без сети, без БД и без реальных ключей. Проверка:
   `python -c "import app.<module>"` при пустом окружении не падает.
5. Единственный источник настроек — `app.config.get_settings()`. `os.environ` в коде запрещён.
6. Единственный источник фактов о школе — `KBSnapshot`. Строковых констант с ценами, адресами,
   телефонами, расписанием в коде быть не может.

## 1. Карта владения файлами

| Волна | Владелец | Файлы |
|---|---|---|
| **1a** | ядро-конфиг | `app/types.py`, `app/config.py`, `app/logging_conf.py`, `requirements.txt`, `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `railway.json`, `.env.example`, `Makefile` |
| **1b** | KB | `app/kb/__init__.py`, `app/kb/models.py`, `app/kb/loader.py`, `app/kb/render.py`, `app/kb/gaps.py`, `kb/*.yaml` |
| **1c** | storage | `app/storage/__init__.py`, `app/storage/db.py`, `app/storage/models.py`, `app/storage/state.py`, `app/storage/repo_conversation.py`, `app/storage/repo_message.py`, `app/storage/repo_lead.py`, `app/storage/repo_outbox.py`, `migrations/*` |
| **2a** | каналы | `app/channels/*.py` |
| **2b** | LLM | `app/llm/*.py` |
| **2c** | tools | `app/tools/*.py` |
| **3a** | ядро-пайплайн | `app/core/*.py` |
| **3b** | API | `app/api/*.py`, `app/main.py`, `app/deps.py` |
| **3c** | воркеры и уведомления | `app/workers/*.py`, `app/notify/*.py`, `app/observability/metrics.py` |
| **4** | тесты | `tests/*` |

**Чужие файлы можно читать, редактировать — нельзя.** Нужна правка в чужом файле — правка идёт в этот
контракт, а не в код.

### 1.1 Правило зависимостей (проверяется тестом `tests/test_layering.py`)

```
app.types          ← не импортирует ничего из app.*
app.config         ← только app.types
app.logging_conf   ← только app.types, app.config
app.kb             ← app.types, app.config
app.storage        ← app.types, app.config
app.channels       ← app.types, app.config              (НЕ знает про Gemini, KB и core)
app.llm            ← app.types, app.config              (НЕ знает про Wazzup, KB и tools)
app.tools          ← app.types, app.config, app.kb, app.storage   (НЕ знает про LLM и core)
app.core           ← всё вышеперечисленное
app.notify         ← app.types, app.config, app.kb, app.storage
app.workers        ← всё
app.api            ← app.types, app.config, app.core, app.storage, app.kb, app.deps
```

**[РЕШЕНО]** `app.llm` не импортирует `app.tools`. Схемы инструментов приходят в LLM-слой как
`Sequence[ToolSpec]` (тип из `app.types`), исполнение — как объект протокола `ToolExecutor`.
Собирает и то и другое `app.core.pipeline`.

**[РЕШЕНО]** Отдельного `app/errors.py` **нет**. Вся иерархия исключений живёт в `app/types.py` —
чтобы не плодить файл, не попавший в карту владения. См. §15.

**[РЕШЕНО]** История диалога вне `app.llm` — это `list[dict[str, Any]]` (JSON-дампы `types.Content`,
полученные через `model_dump(mode="json", exclude_none=True)`). Типы `google.genai` не пересекают границу
`app.llm`. Ни один модуль, кроме `app/llm/*.py`, не имеет права импортировать `google.genai`.

## 2. Глобальные константы контракта

| Имя | Значение | Где живёт |
|---|---|---|
| Максимум витков tool-loop | 5 | `Settings.llm_max_tool_loops` |
| Жёсткий предел одного исходящего | 1000 знаков | `app.types.MAX_MESSAGE_CHARS` |
| Мягкий предел (после которого сплит) | 600 знаков | `Settings.soft_message_chars` |
| Сообщений подряд от бота | 2 | `Settings.max_messages_per_turn` |
| Дебаунс | 5 с, продление до 8 с | `Settings.debounce_seconds` / `debounce_max_seconds` |
| TTL дедупликации входящих | 86400 с | `Settings.dedup_ttl_seconds` |
| TTL лока диалога | 60 с | `Settings.conv_lock_ttl_seconds` |
| `clearUnanswered` | всегда `false` | `OutboundMessage.clear_unanswered: Literal[False]` |
| Таймзона расчётов | `Asia/Almaty` | `Settings.timezone` |
| Формат телефона | `^\+7\d{10}$` | `app.types.PHONE_E164_KZ_RE` |
## 3. `app/types.py` — ФАЙЛ ЦЕЛИКОМ

Копируется как есть. Тела с `raise NotImplementedError` дописывает владелец волны 1a; сигнатуры,
имена полей и значения перечислений менять запрещено — на них завязаны все восемь волн.

```python
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
        raise NotImplementedError


class ChannelKind(str, Enum):
    """Канал общения. Значения совпадают с ``chatType`` Wazzup для этих двух каналов."""

    WHATSAPP = "whatsapp"
    INSTAGRAM = "instagram"

    @classmethod
    def from_chat_type(cls, chat_type: str) -> "ChannelKind | None":
        """Отображает ``chatType`` вебхука в канал; прочие 8 значений дают ``None``."""
        raise NotImplementedError


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
        raise NotImplementedError


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
        raise NotImplementedError

    def merge(self, other: "LeadDraft") -> "LeadDraft":
        """Возвращает новый черновик: непустые поля ``other`` перекрывают текущие."""
        raise NotImplementedError


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
        raise NotImplementedError

    @classmethod
    def no_data(
        cls,
        say: dict[str, str],
        *,
        gap_ref: GapRef | None = None,
        data: dict[str, Any] | None = None,
    ) -> "ToolResult":
        """Данных нет. ``say`` обязан содержать ключи ``ru`` и ``kk``."""
        raise NotImplementedError

    @classmethod
    def needs_operator(
        cls,
        say: dict[str, str],
        *,
        reason: EscalationReason,
        gap_ref: GapRef | None = None,
    ) -> "ToolResult":
        """Вопрос вне компетенции бота: ответ обязан привести к эскалации."""
        raise NotImplementedError

    @classmethod
    def invalid_input(cls, error: str) -> "ToolResult":
        """Модель прислала невалидные аргументы. В диалог это не выносится."""
        raise NotImplementedError

    @classmethod
    def failure(cls, error: str) -> "ToolResult":
        """Внутренний сбой инструмента. Пайплайн трактует как промах бота."""
        raise NotImplementedError

    def to_llm_payload(self) -> dict[str, Any]:
        """Конверт для ``FunctionResponse.response``.

        Форма: ``{"status": ..., "data": {...}, "caveats": [...],
        "say_if_no_data": {"ru": ..., "kk": ...}}``. Пустые ключи опускаются.
        ``ERROR`` отдаётся модели как ``needs_operator`` — модель не должна видеть трейсы.
        """
        raise NotImplementedError


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
        raise NotImplementedError


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
```

### 3.1 Что обязана делать реализация тел (волна 1a)

| Метод | Поведение, на которое рассчитывают потребители |
|---|---|
| `Language.parse` | `"ru"/"RU"/"рус"` → `RU`; `"kk"/"kz"/"каз"` → `KK`; иначе `None`. Регистр не важен |
| `ChannelKind.from_chat_type` | `"whatsapp"` → `WHATSAPP`, `"instagram"` → `INSTAGRAM`, остальные 8 значений `chatType` → `None` |
| `OutboundMessage._exactly_one_payload` | ровно одно из `text` / `content_uri` заполнено, иначе `ValueError`; `len(text) <= MAX_MESSAGE_CHARS`, иначе `ValueError` |
| `LeadDraft.missing_required` | кортеж имён недостающих полей; пустой кортеж = можно ставить `trial_booked` |
| `LeadDraft.merge` | новый объект; `None`, `""`, `Gender.UNKNOWN`, `PhoneSource.NONE` в `other` **не** затирают заполненное |
| `ToolResult.success` | `ok=True`, `status=OK`, `error=None` |
| `ToolResult.no_data` | `ok=False`, `status=NO_DATA`, `render_hint=VERBATIM`, `say_if_no_data=say`; `KeyError`, если в `say` нет `ru` и `kk` |
| `ToolResult.needs_operator` | `ok=False`, `status=NEEDS_OPERATOR`, `render_hint=VERBATIM`, `meta["escalation_reason"] = reason.value` |
| `ToolResult.invalid_input` | `ok=False`, `status=INVALID_INPUT`, `render_hint=SILENT`, `error` заполнен |
| `ToolResult.failure` | `ok=False`, `status=ERROR`, `render_hint=SILENT`, `error` заполнен |
| `ToolResult.to_llm_payload` | `{"status": ...}` + непустые `data` / `caveats` / `say_if_no_data`; при `status in (ERROR, INVALID_INPUT)` наружу уходит `{"status": "needs_operator"}` без `error` — трейсы модели не показываем |
| `WazzupError.normalized_code` | `(self.error_code or "").replace("_", "").lower()` |

**Инвариант, проверяемый тестом:** `ToolResult.ok is (result.status is ToolStatus.OK)`.
## 4. `app/config.py` — ФАЙЛ ЦЕЛИКОМ

```python
# ФАЙЛ ЦЕЛИКОМ: app/config.py
"""Настройки приложения. Единственная точка чтения окружения.

``os.environ`` где-либо ещё в коде запрещён. Модуль обязан импортироваться при
полностью пустом окружении: обязательность значений проверяет
:meth:`Settings.startup_blockers`, а не валидатор поля.

Особенности Railway, ради которых написан этот модуль:

* порт приходит в переменной ``PORT`` — слушать нужно ``0.0.0.0:$PORT``;
* плагин Postgres выдаёт ``postgresql://…`` (иногда legacy ``postgres://…``),
  а SQLAlchemy async требует ``postgresql+asyncpg://…`` — нормализуем сами,
  чтобы заказчик вставлял переменную Railway без правки;
* плагин Redis выдаёт готовый ``REDIS_URL`` — используется как есть;
* ``INLINE_WORKER=true`` поднимает воркер в одном процессе с API, чтобы на старте
  хватило одной службы.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.types import ChannelKind, ConfigError

#: Префиксы, которые Railway/Neon/Heroku отдают вместо async-диалекта.
_PG_PREFIXES: Final[tuple[str, ...]] = ("postgres://", "postgresql://")

#: Параметры строки подключения, которые asyncpg не понимает и роняет соединение.
_PG_DROP_QUERY: Final[frozenset[str]] = frozenset(
    {"sslmode", "channel_binding", "target_session_attrs", "gssencmode"}
)


def normalize_database_url(raw: str) -> str:
    """Приводит ``DATABASE_URL`` к async-диалекту SQLAlchemy.

    ``postgres://`` и ``postgresql://`` → ``postgresql+asyncpg://``;
    ``sqlite://`` → ``sqlite+aiosqlite://``; уже нормализованные URL не трогает.
    Из postgres-URL удаляются query-параметры, которые asyncpg не принимает
    (``sslmode`` и другие из :data:`_PG_DROP_QUERY`).

    Пустая строка возвращается как есть — обязательность проверяется отдельно.
    """
    raise NotImplementedError


def _drop_query_params(url: str, names: frozenset[str]) -> str:
    """Удаляет из query-строки перечисленные параметры, сохраняя порядок прочих."""
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in names]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


class Settings(BaseSettings):
    """Все настройки бота. Имена ENV совпадают с именами полей в верхнем регистре."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- окружение и HTTP ---------------------------------------------------
    app_env: Literal["local", "staging", "prod"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    host: str = "0.0.0.0"
    #: Railway передаёт порт только через PORT. Захардкоженный порт = провал деплоя.
    port: int = Field(default=8000, validation_alias=AliasChoices("PORT", "APP_PORT"))
    #: Внешний https-адрес службы, база для contentUri: https://<service>.up.railway.app
    public_base_url: str = "http://localhost:8000"

    # --- Wazzup24 -----------------------------------------------------------
    wazzup_api_key: str = ""
    wazzup_api_base: str = "https://api.wazzup24.com/v3"
    #: 32 байта base64url, попадает в путь вебхука. Сравнение — hmac.compare_digest.
    wazzup_webhook_secret: str = ""
    wazzup_channel_id_whatsapp: str | None = None
    wazzup_channel_id_instagram: str | None = None
    #: Всегда false: автоответ не должен гасить счётчик неотвеченных.
    wazzup_clear_unanswered: Literal[False] = False
    wazzup_timeout_ms: int = 15000
    wazzup_send_max_attempts: int = 5
    wazzup_send_backoff_base_ms: int = 1000
    wazzup_register_webhook_on_start: bool = False

    # --- Gemini -------------------------------------------------------------
    gemini_api_key: str = ""
    gemini_model_primary: str = "gemini-3.5-flash-lite"
    gemini_model_fallback: str = "gemini-3.1-flash-lite"
    gemini_thinking_level: Literal["minimal", "low", "medium", "high"] = "minimal"
    gemini_timeout_ms: int = 30000
    gemini_max_output_tokens: int = 1024
    llm_max_tool_loops: int = 5
    llm_history_turns: int = 20
    llm_daily_budget_usd: float = 5.0
    #: Цена за 1M токенов, USD. Нужна только для оценки в llm_call.cost_usd.
    llm_price_in_per_mtok: float = 0.30
    llm_price_out_per_mtok: float = 2.50

    # --- хранилище ----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_echo: bool = False
    redis_url: str = "redis://localhost:6379/0"
    #: redis — прод; memory — только тесты и локальный запуск без Redis.
    state_backend: Literal["redis", "memory"] = "redis"

    # --- воркер -------------------------------------------------------------
    #: true — воркер живёт в процессе API (одна служба Railway).
    inline_worker: bool = False
    worker_max_jobs: int = 10
    worker_job_timeout_s: int = 120

    # --- база знаний и медиа ------------------------------------------------
    kb_dir: Path = Path("kb")
    media_dir: Path = Path("media")
    kb_schema_version: int = 1
    kb_hot_reload: bool = False
    media_token_ttl_s: int = 600
    #: Секрет подписи ссылок /media/{token}. Пустой — берётся wazzup_webhook_secret.
    media_token_secret: str = ""

    # --- поведение диалога --------------------------------------------------
    timezone: str = "Asia/Almaty"
    debounce_seconds: int = 5
    debounce_max_seconds: int = 8
    conv_lock_ttl_seconds: int = 60
    dedup_ttl_seconds: int = 86400
    soft_message_chars: int = 600
    max_messages_per_turn: int = 2
    second_message_delay_ms: int = 2000
    pause_operator_minutes: int = 30
    pause_user_request_minutes: int = 60
    pause_escalation_minutes: int = 60
    pause_postcheck_minutes: int = 30
    pause_llm_failure_minutes: int = 15
    #: Служебная строка оператора, снимающая паузу. Приходит эхом, клиенту не пересылается.
    operator_resume_command: str = "#бот"
    bot_miss_limit: int = 2
    rate_limit_inbound_per_conv: int = 20
    rate_limit_window_seconds: int = 300

    # --- follow-up ----------------------------------------------------------
    followup_enabled: bool = True
    followup_max_attempts: int = 2
    followup_quiet_hours_start: int = 21
    followup_quiet_hours_end: int = 9

    # --- уведомления менеджеру ---------------------------------------------
    manager_notify_channel: ChannelKind = ChannelKind.WHATSAPP
    #: chatId менеджера в выбранном канале (для WhatsApp — только цифры, 77xxxxxxxxx).
    manager_notify_target: str | None = None
    manager_notify_channel_id: str | None = None
    work_hours: str = "10:00-20:00"
    sla_minutes: int = 30

    # --- админка и метрики --------------------------------------------------
    admin_token: str | None = None
    metrics_enabled: bool = True

    @field_validator("database_url", mode="after")
    @classmethod
    def _normalize_db_url(cls, value: str) -> str:
        """Автонормализация URL Railway. См. :func:`normalize_database_url`."""
        return normalize_database_url(value)

    @property
    def is_sqlite(self) -> bool:
        """Тесты и локальный пилот идут на sqlite+aiosqlite."""
        return self.database_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql+asyncpg")

    @property
    def media_signing_key(self) -> str:
        """Ключ подписи ссылок на медиа; фолбэк — секрет вебхука."""
        return self.media_token_secret or self.wazzup_webhook_secret

    def webhook_path(self) -> str:
        """Путь вебхука с секретом: ``/wazzup/webhook/{secret}`` (≤ 200 символов вместе с доменом)."""
        return f"/wazzup/webhook/{self.wazzup_webhook_secret}"

    def webhook_url(self) -> str:
        """Полный публичный URL вебхука для ``PATCH /v3/webhooks``."""
        return f"{self.public_base_url.rstrip('/')}{self.webhook_path()}"

    def startup_blockers(self) -> list[str]:
        """Технические блокеры старта. Юридических блокеров в проекте нет.

        Для ``app_env != "local"`` обязательны: ``wazzup_api_key``,
        ``wazzup_webhook_secret`` (≥ 24 символов), ``gemini_api_key``,
        ``database_url``, ``redis_url`` при ``state_backend="redis"``,
        ``public_base_url`` на https, ``manager_notify_target``.
        Возвращает список человекочитаемых причин; пустой список = можно стартовать.
        """
        raise NotImplementedError

    def require_startup(self) -> None:
        """Кидает :class:`app.types.ConfigError`, если ``startup_blockers`` не пуст."""
        blockers = self.startup_blockers()
        if blockers:
            raise ConfigError("Некорректная конфигурация: " + "; ".join(blockers))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кэшированный синглтон настроек. Единственный публичный доступ к конфигу."""
    return Settings()


def reset_settings_cache() -> None:
    """Сброс кэша. Только для тестов."""
    get_settings.cache_clear()
```

### 4.1 `normalize_database_url` — таблица истинности (тест `tests/test_config.py` обязателен)

| Вход | Выход |
|---|---|
| `postgres://u:p@h:5432/db` | `postgresql+asyncpg://u:p@h:5432/db` |
| `postgresql://u:p@h:5432/db` | `postgresql+asyncpg://u:p@h:5432/db` |
| `postgresql://u:p@h/db?sslmode=require` | `postgresql+asyncpg://u:p@h/db` |
| `postgresql+asyncpg://u:p@h/db` | без изменений |
| `sqlite:///./data/bot.db` | `sqlite+aiosqlite:///./data/bot.db` |
| `sqlite+aiosqlite:///:memory:` | без изменений |
| `""` | `""` |

Пароль с символами `@` и `/` обязан пережить нормализацию — работать со строкой префиксом, а не
регуляркой по всему URL.

### 4.2 Артефакты деплоя (владелец — волна 1a)

**`railway.json`** в корне репозитория:

```json
{
  "$schema": "https://railway.com/railway.schema.json",
  "build": { "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" },
  "deploy": {
    "startCommand": "uvicorn app.main:app --host 0.0.0.0 --port $PORT",
    "healthcheckPath": "/healthz",
    "healthcheckTimeout": 30,
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10
  }
}
```

**Две службы из одного репозитория** (вторая создаётся в UI Railway с переопределением команды):

| Служба | Start command |
|---|---|
| `api` | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| `worker` | `arq app.workers.queue.WorkerSettings` |

При `INLINE_WORKER=true` служба `worker` не нужна: воркер поднимается в lifespan API.

**`Dockerfile`** — multi-stage, non-root, без `EXPOSE` фиксированного порта в качестве истины:
слушаем `$PORT`. `HEALTHCHECK` не задаётся (его делает Railway по `healthcheckPath`).

**`docker-compose.yml`** — **только локальная разработка**, обязательный комментарий в шапке:
`# локальная разработка; в проде используется Railway`. Сервисы: `db` (postgres:16), `redis` (redis:7).
Никаких `caddy`, `backup`, `pgaudit`.

**`Makefile`** — цели: `dev` (compose up db+redis), `run` (uvicorn), `worker` (arq), `migrate`
(`alembic upgrade head`), `kb-check` (`python -m app.kb.loader --check`), `test`, `lint`, `fmt`.

**`.env.example`** — имена без значений, ровно поля `Settings` в верхнем регистре. Переменных про
согласия, `POLICY_URL`, БИН и шифрование в нём **нет**.

## 5. `requirements.txt` — ФАЙЛ ЦЕЛИКОМ

Диапазоны, а не точные пины: верхняя граница по мажору защищает от breaking changes, нижняя —
гарантирует наличие используемых API. Ничего сверх списка добавлять нельзя.

```text
# ФАЙЛ ЦЕЛИКОМ: requirements.txt
fastapi>=0.115,<1.0
uvicorn[standard]>=0.30,<1.0
pydantic>=2.9,<3.0
pydantic-settings>=2.5,<3.0
httpx>=0.27,<1.0
sqlalchemy[asyncio]>=2.0.36,<2.1
asyncpg>=0.30,<1.0
aiosqlite>=0.20,<1.0
alembic>=1.13,<2.0
redis>=5.0,<6.0
arq>=0.26,<1.0
pyyaml>=6.0,<7.0
structlog>=24.4,<26.0
prometheus-client>=0.21,<1.0
google-genai>=2.17.0,<3.0.0
pytest>=8.3,<9.0
pytest-asyncio>=0.24,<2.0
respx>=0.21,<1.0
```

Пояснения к границам:

* `google-genai>=2.17.0` — на этой версии проверены `types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT`,
  отсутствие `id` у `Part.from_function_response` и наличие режима `VALIDATED`;
* `redis<6.0` — верхняя граница выбрана из-за зависимости `arq`, которая тянет `redis>=4.2,<6`;
  поднимать `redis` до 6/7 нельзя, пока не обновится `arq`;
* `psycopg2` в списке **нет** — Alembic работает через async-движок (см. §7.5).

**`pyproject.toml`** содержит только конфигурацию инструментов (не метаданные пакета):

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 110
target-version = "py311"
```
## 6. `app/logging_conf.py` (волна 1a)

```python
def configure_logging(*, level: str, json_output: bool = True) -> None:
    """Настраивает structlog и stdlib-logging. Вызывается один раз в lifespan."""

def get_logger(name: str) -> "structlog.stdlib.BoundLogger":
    """Логгер модуля. Единственный разрешённый способ получить логгер."""

def mask_phone(value: str | None) -> str | None:
    """`+77051234567` -> `+7705***4567`. `None` и короткие строки — как есть."""

def mask_name(value: str | None) -> str | None:
    """`Айгуль` -> `А***`. Пустое значение -> `None`."""

def mask_text(value: str | None, *, limit: int = 64) -> str | None:
    """Обрезает и маскирует телефоны внутри произвольного текста."""

def bind_correlation(correlation_id: str, **fields: object) -> None:
    """Кладёт correlation_id (= wazzup messageId) и поля в contextvars structlog."""

def clear_correlation() -> None:
    """Очищает контекст. Обязательно в `finally` каждой задачи воркера."""

def conv_key_hash(conv_key: str) -> str:
    """sha256(conv_key)[:12] — в логи уходит хеш, а не номер клиента."""
```

**Обязательные поля каждой записи:** `ts, level, event, correlation_id, conv_key_hash, lang, kb_hash,
model, tool, latency_ms, tokens_in, tokens_out, tokens_cached, cost_usd, outcome`.
Отсутствующие поля не пишутся (не `null`).

**Запрещено к логированию:** текст сообщений клиента, `text_raw`, значения `GEMINI_API_KEY`,
`WAZZUP_API_KEY`, `WAZZUP_WEBHOOK_SECRET`, `ADMIN_TOKEN`, немаскированные телефоны и имена.
Процессор `_redact` вырезает их по имени ключа — тест `tests/test_logging_mask.py` это проверяет.

## 7. `app/storage/*` (волна 1c)

### 7.1 Точные имена таблиц и колонок

Диалект — PostgreSQL, `ts` = `TIMESTAMPTZ`, `id` = `UUID` (`server_default` не используем, id
генерирует приложение). **Все имена таблиц в единственном числе.** Персональные данные хранятся
**открытым текстом** — `TypeDecorator`-ов, шифрования и колонок `delete_after` в проекте нет.
Таблицы `consent_record` и `audit_event` **не создаются**.

**`conversation`**

| Колонка | Тип | Примечание |
|---|---|---|
| `id` | UUID PK | |
| `conv_key` | Text UNIQUE NOT NULL | `{channel_id}:{chat_type}:{chat_id}` |
| `channel_id` | Text NOT NULL | |
| `chat_type` | Text NOT NULL | значение `ChannelKind` |
| `chat_id` | Text NOT NULL | |
| `contact_name` | Text NULL | |
| `instagram_username` | Text NULL | |
| `phone_e164` | Text NULL | |
| `lang` | Text NULL | `ru` / `kk` |
| `lang_locked` | Boolean NOT NULL DEFAULT false | |
| `state` | Text NOT NULL DEFAULT `'new'` | `ConversationState`, значения `consent_pending` нет |
| `summary` | Text NULL | |
| `kb_hash_at_start` | Text NULL | |
| `msg_in_count` / `msg_out_count` | Integer NOT NULL DEFAULT 0 | |
| `bot_miss_count` | Integer NOT NULL DEFAULT 0 | ≥ `bot_miss_limit` → эскалация |
| `first_inbound_at` / `last_inbound_at` / `last_outbound_at` | ts NULL | |
| `service_window_until` | ts NULL | Instagram +7 дней |
| `followup_stage` | SmallInteger NOT NULL DEFAULT 0 | |
| `followup_blocked` | Boolean NOT NULL DEFAULT false | |
| `created_at` / `updated_at` | ts NOT NULL | |

**`message`**

| Колонка | Тип | Примечание |
|---|---|---|
| `id` | UUID PK | |
| `conversation_id` | UUID FK → `conversation.id` NOT NULL | |
| `direction` | Text NOT NULL | `in` / `out` |
| `author` | Text NOT NULL | `client` / `bot` / `operator` / `system` |
| `wazzup_message_id` | Text UNIQUE NULL | ключ дедупликации |
| `crm_message_id` | UUID UNIQUE NULL | наш идемпотентный ключ |
| `gemini_role` | Text NULL | `user` / `model` |
| `gemini_content` | JSONB NULL | полный дамп `types.Content` |
| `msg_type` | Text NOT NULL | значение `MsgType` |
| `text_raw` | Text NULL | текст как прислал клиент |
| `content_uri` | Text NULL | |
| `status` | Text NOT NULL | значение `MessageStatus` |
| `error_code` / `error_description` | Text NULL | нормализованный код |
| `is_echo` / `sent_from_app` | Boolean NULL | сырые флаги вебхука |
| `author_name` / `author_id` | Text NULL | только при `is_echo=true` |
| `channel_dt` | ts NULL | |
| `created_at` | ts NOT NULL | |

Индексы: `ix_message_conv_created (conversation_id, created_at)`,
`ux_message_wazzup_id (wazzup_message_id)`, частичный `ix_message_error` по `status='error'`.

**`lead`**

`id` UUID PK, `conversation_id` UUID FK NOT NULL, `created_at` ts, `updated_at` ts,
`channel` Text, `channel_user` Text, `instagram_username` Text NULL, `lang` Text,
`parent_name` Text NULL, `parent_relation` Text NULL, `phone` Text NULL,
`phone_source` Text NOT NULL DEFAULT `'none'`, `child_name` Text NOT NULL, `child_age` SmallInteger NOT NULL,
`child_birth_year` SmallInteger NULL, `child_gender` Text NOT NULL DEFAULT `'unknown'`,
`district` Text NULL, `gym_id` Text NULL, `trial_slot` ts NULL, `trial_slot_text` Text NULL,
`motivation` Text NULL, `main_objection` Text NULL, `prior_experience` Text NULL,
`health_notes` Text NULL, `status` Text NOT NULL, `escalation` Boolean NOT NULL DEFAULT false,
`dialog_url` Text NULL, `messages_count` Integer NOT NULL DEFAULT 0, `notified_at` ts NULL.
Уникальность: `ux_lead_conversation (conversation_id)` — один активный лид на диалог,
повторный `create_trial_lead` обновляет строку.
Колонок `consent_to_contact` и `delete_after` **нет**.

**`escalation_state`** — PK `conversation_id` UUID FK; `paused` Boolean NOT NULL DEFAULT false,
`paused_until` ts NULL, `pause_reason` Text NULL, `operator_last_seen_at` ts NULL,
`operator_author_id` / `operator_author_name` Text NULL, `escalation_count` Integer NOT NULL DEFAULT 0,
`last_escalated_at` ts NULL, `manager_notified_at` ts NULL,
`resume_policy` Text NOT NULL DEFAULT `'timeout'`, `resumed_at` ts NULL.

**`gym`** — read-model, зеркало `kb/gyms.yaml`, перезаписывается целиком в транзакции только загрузчиком:
`id` Text PK, `scope`, `settlement`, `is_head` Boolean, `active` Boolean, `status` Text,
`title_ru`/`title_kk`, `address_ru`/`address_kk`, `landmark_ru`/`landmark_kk`,
`district_ru`/`district_kk`, `district_aliases` JSONB, `lat`/`lon` Numeric NULL, `map_url` Text NULL,
`phone` Text NULL, `has_schedule` Boolean, `kb_hash` Text.
**[РЕШЕНО]** таблица называется `gym` (в `ARCHITECTURE.md` §3.4 стояло `gyms`), `district_aliases` —
`JSONB`, а не `text[]`, чтобы модель работала и на SQLite.

**`processed_webhook`** — `message_id` Text PK, `kind` Text (`message`/`status`), `first_seen_at` ts.
Для статусов ключ составной строкой: `f"{message_id}:{status}"`.

**`outbox_message`** — `id` UUID PK, `conversation_id` UUID FK NULL, `crm_message_id` UUID UNIQUE NOT NULL,
`payload` JSONB NOT NULL (дамп `OutboundMessage`), `state` Text NOT NULL DEFAULT `'pending'`,
`attempts` Integer NOT NULL DEFAULT 0, `next_attempt_at` ts NULL, `wazzup_message_id` Text NULL,
`last_error` Text NULL, `created_at`/`updated_at` ts. Индекс `ix_outbox_pending (state, next_attempt_at)`.

**`llm_call`** — `id` UUID PK, `conversation_id` UUID FK NULL, `model` Text, `prompt_tokens`,
`cached_tokens`, `candidates_tokens`, `thoughts_tokens` Integer, `latency_ms` Integer,
`cost_usd` Numeric(10, 6), `tool_calls` JSONB, `finish_reason` Text NULL, `error` Text NULL,
`kb_hash` Text NULL, `created_at` ts.

**`followup_task`** — `id` UUID PK, `conversation_id` UUID FK, `kind` Text (`FollowupKind`),
`run_at` ts, `state` Text (`pending|sent|cancelled|failed`), `attempt` SmallInteger, `created_at` ts.

**`kb_version`** — `hash` Text PK, `loaded_at` ts, `files` JSONB, `valid` Boolean.

### 7.2 `app/storage/db.py`

```python
def build_engine(url: str | None = None) -> AsyncEngine:
    """Создаёт async-движок. Сеть не трогает: подключение ленивое."""

def build_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """`expire_on_commit=False` — объекты живут после коммита."""

@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Транзакция: commit на выходе, rollback на исключении."""

async def ping(session: AsyncSession) -> bool:
    """`SELECT 1` для /readyz."""

async def dispose_engine() -> None:
    """Закрывает пул. Вызывается в shutdown lifespan."""

Base: type[DeclarativeBase]
```

### 7.3 Репозитории

Все методы принимают `session: AsyncSession` **первым** параметром; репозитории не открывают
транзакции сами и не коммитят.

```python
# app/storage/repo_conversation.py
async def get_by_conv_key(session, conv_key: str) -> Conversation | None: ...
async def get_or_create(session, *, inbound: InboundMessage) -> tuple[Conversation, bool]: ...
async def update_language(session, conv_id: UUID, *, lang: Language, locked: bool) -> None: ...
async def set_state(session, conv_id: UUID, state: ConversationState) -> None: ...
async def bump_counters(session, conv_id: UUID, *, msg_in: int = 0, msg_out: int = 0) -> None: ...
async def set_bot_miss(session, conv_id: UUID, value: int) -> None: ...
async def touch_inbound(session, conv_id: UUID, at: datetime, *, window_until: datetime | None) -> None: ...
async def set_summary(session, conv_id: UUID, summary: str) -> None: ...
async def set_followup(session, conv_id: UUID, *, stage: int | None = None, blocked: bool | None = None) -> None: ...

# app/storage/repo_message.py
async def exists_wazzup_id(session, message_id: str) -> bool: ...
async def add_inbound(session, conv_id: UUID, msg: InboundMessage) -> UUID: ...
async def add_outbound(session, conv_id: UUID, msg: OutboundMessage, *, wazzup_message_id: str | None) -> UUID: ...
async def add_llm_turn(session, conv_id: UUID, contents: Sequence[dict[str, Any]]) -> None: ...
async def load_history(session, conv_id: UUID, *, max_turns: int) -> list[dict[str, Any]]: ...
async def load_transcript(session, conv_id: UUID, *, limit: int = 40) -> list[tuple[Author, str]]: ...
async def update_status(session, wazzup_message_id: str, update: StatusUpdate) -> bool: ...
async def count_artifact_sends(session, conv_id: UUID, artifact_id: str) -> int: ...

# app/storage/repo_lead.py
async def upsert(session, draft: LeadDraft) -> UUID: ...
async def get_by_conversation(session, conv_id: UUID) -> Lead | None: ...
async def mark_notified(session, lead_id: UUID, at: datetime) -> None: ...
async def list_recent(session, *, limit: int = 50, status: LeadStatus | None = None) -> list[Lead]: ...

# app/storage/repo_outbox.py
async def enqueue(session, msg: OutboundMessage) -> UUID: ...
async def claim(session, outbox_id: UUID) -> OutboxMessage | None: ...
async def mark_sent(session, outbox_id: UUID, *, wazzup_message_id: str | None) -> None: ...
async def mark_failed(session, outbox_id: UUID, *, error: str, next_attempt_at: datetime | None) -> None: ...
async def mark_skipped(session, outbox_id: UUID, *, error: str) -> None: ...
async def exists_by_wazzup_message_id(session, message_id: str) -> bool: ...
async def pending_count(session) -> int: ...
async def due(session, now: datetime, *, limit: int = 50) -> list[UUID]: ...
```

`exists_by_wazzup_message_id` — **основной** признак «эхо не наше» в детекторе оператора (§12.5).

### 7.4 `app/storage/state.py` — TTL-состояния

```python
class StateStore(Protocol):
    async def set_if_absent(self, key: str, value: str, ttl_s: int) -> bool: ...
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl_s: int) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def incr(self, key: str, ttl_s: int) -> int: ...
    async def ttl(self, key: str) -> int: ...
    @asynccontextmanager
    def lock(self, key: str, ttl_s: int) -> AsyncIterator[bool]: ...

class RedisStateStore(StateStore): ...
class MemoryStateStore(StateStore):
    """Только тесты и локальный запуск. Не переживает рестарт, не работает на 2 процесса."""

def build_state_store(settings: Settings) -> StateStore: ...
```

**Пространства ключей заморожены:**

| Ключ | TTL | Смысл |
|---|---|---|
| `wz:msg:{message_id}` | `dedup_ttl_seconds` | дедуп входящих |
| `wz:st:{message_id}:{status}` | `dedup_ttl_seconds` | дедуп статусов |
| `lock:conv:{conv_key}` | `conv_lock_ttl_seconds` | одновременность 1 на диалог |
| `deb:conv:{conv_key}` | `debounce_max_seconds` | окно склейки |
| `pause:{conv_key}` | до `paused_until` | быстрый путь проверки паузы |
| `rate:conv:{conv_key}` | `rate_limit_window_seconds` | счётчик входящих |
| `budget:llm:{YYYY-MM-DD}` | 172800 | суточный расход в USD (микроцентами, int) |

### 7.5 Alembic

`migrations/env.py` работает через **async**-движок (`connection.run_sync(context.run_migrations)`),
потому что `psycopg2` в зависимости не входит. `alembic.ini` берёт URL не из файла, а из
`get_settings().database_url`. Первая ревизия — `0001_initial`, создаёт все девять таблиц из §7.1.
## 8. `app/kb/*` (волна 1b)

### 8.1 `app/kb/models.py` — pydantic-схемы YAML

Все модели `frozen=True, extra="forbid"`. Пустая строка `""` в любом текстовом поле — **ошибка
валидации**, «нет данных» кодируется `null` или `[]` (KB-SPEC §9.1).

```python
class Bilingual(BaseModel):
    """Пара RU/KK. Обе локали обязательны, если поле вообще заполнено."""
    ru: str | None
    kk: str | None

class Coach(BaseModel):
    name: str
    credentials: str | None
    groups: list[str]
    speaks: list[Language]

class ScheduleSlot(BaseModel):
    age_from: int
    age_to: int
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]]
    time_start: str          # "HH:MM"
    time_end: str            # "HH:MM"
    shift: Literal["first", "second", "any"]
    note: Bilingual | None

class Gym(BaseModel):
    id: str                  # SLUG_RE
    scope: Scope             # только CITY | REGION
    settlement: str
    is_head: bool
    active: bool
    status: GymStatus
    title: Bilingual
    address: Bilingual
    landmark: Bilingual
    district: Bilingual
    district_aliases: list[str]
    geo_lat: float | None
    geo_lon: float | None
    map_url: str | None
    phone: str | None
    coaches: list[Coach]
    schedule: list[ScheduleSlot]
    capacity_note: Bilingual | None
    media: list[str]
    gap_refs: list[GapRef]
    internal_note: str | None    # НИКОГДА не попадает в промпт и клиенту

class GymsFile(BaseModel):
    schema_version: int
    updated_at: date
    updated_by: Literal["owner", "admin", "dev"]
    timezone: str
    city_settlement: str
    gyms: list[Gym]

class PlanPrice(BaseModel):
    price: int
    recalculation: bool
    label: Bilingual
    note: Bilingual | None

class FamilyDiscount(BaseModel):
    type: Literal["percent_by_order", "fixed_per_child"]
    rules: list[tuple[int, int]]          # (child_index, percent)
    applies_to: list[str]
    applies_to_status: Literal["confirmed", "unconfirmed"]
    base_rule: Literal["enrollment_order", "cheapest_first"]
    base_rule_status: Literal["confirmed", "unconfirmed"]
    max_children: int | None
    label: Bilingual

class PricingFile(BaseModel):
    schema_version: int
    currency: Literal["KZT"]
    rounding_mode: Literal["half_up", "floor", "none"]
    rounding_to: int
    city_settlement: str
    city_sessions: int
    city_validity_days: int
    city_validity_note: Bilingual
    city_plans: dict[str, PlanPrice | None]      # ключи: standard | flexible
    city_single: PlanPrice | None
    city_family_discount: FamilyDiscount
    region_settlements: list[str]
    region_plans: dict[str, PlanPrice | None]
    region_single: PlanPrice | None
    region_family_price_per_child: int | None
    region_family_min_children: int
    region_family_label: Bilingual
    derived_enabled: bool
    derived_facts: list["DerivedFact"]
    payment_methods: list[str]
    payment_details: Bilingual | None
    freeze_policy: Bilingual | None

class DerivedFact(BaseModel):
    """Производный аргумент («выгода абонемента»). Произносится, только если derived_enabled."""
    id: str
    ru: str
    kk: str

class FollowupRule(BaseModel):
    """Строка политики follow-up. Продуктовое правило, согласия не требует."""
    event: FollowupKind
    delay_hours: int
    only_work_hours: bool
    template_id: str              # ключ i18n группы `followup`
    max_times: int

class FaqEntry(BaseModel):
    id: str
    topic: str                    # значение из FAQ_TOPICS
    scope: Scope                  # CITY | REGION | ANY
    question_variants: dict[Language, list[str]]
    answer: Bilingual
    source: FactSource
    gap_ref: GapRef | None
    escalate_if_empty: bool
    requires_tool: str | None
    forbidden_claims: list[str]

class Artifact(BaseModel):
    id: str
    kind: ArtifactKind
    enabled: bool
    scope: Scope
    gym_id: str | None
    title: Bilingual
    when_to_send_ru: str
    body: Bilingual | None
    file_path: str | None
    file_mime: str | None
    file_bytes: int | None
    file_sha256: str | None
    channels: dict[ChannelKind, Literal["allow", "deny"]]
    max_send_per_dialog: int
    gap_ref: GapRef | None
    render_from: Literal["gyms", "pricing"] | None

class PoliciesFile(BaseModel):
    """Организационный слой. Юридических полей НЕТ — см. SCOPE-OVERRIDE §1."""
    schema_version: int
    org_brand: str
    org_city: str
    audience_adults_only: bool
    audience_child_detected_action: Literal["escalate", "stop"]
    work_hours: Bilingual | None
    sla_reply_minutes: int | None
    escalation_triggers: list[EscalationReason]
    escalation_pause_minutes: int
    followup_policy: list["FollowupRule"]
    followup_stop_words: list[str]
    forbidden_behaviour: list[str]

class I18nFile(BaseModel):
    schema_version: int
    strings: dict[str, Bilingual]

class LexiconFile(BaseModel):
    schema_version: int
    intents: dict[IntentHint, list[str]]
    kk_graphemes: list[str]
    kk_words: list[str]
    kk_translit: list[str]
    ru_translit: list[str]
    age_patterns: list[str]
    gender_markers: dict[Gender, list[str]]
    districts_extra: list[str]

class KBSnapshot(BaseModel):
    """Иммутабельный снимок базы знаний. Живёт в памяти, меняется атомарным swap."""
    kb_hash: str
    loaded_at: datetime
    gyms: GymsFile
    pricing: PricingFile
    faq: list[FaqEntry]
    media: list[Artifact]
    policies: PoliciesFile
    i18n: I18nFile
    lexicon: LexiconFile

    def gym(self, gym_id: str) -> Gym | None: ...
    def active_gyms(self, scope: Scope = Scope.ALL) -> tuple[Gym, ...]:
        """Только `active=True` и `status=open`, отсортированы по `id`."""
    def gym_ids(self) -> tuple[str, ...]:
        """Enum для схем инструментов: id активных и открытых залов, отсортированы."""
    def artifact(self, artifact_id: str) -> Artifact | None: ...
    def artifact_ids(self) -> tuple[str, ...]:
        """Только `enabled=True`, отсортированы. Из них строится enum `artifact_id`."""
    def faq_topics(self) -> tuple[str, ...]:
        """Отсортированное множество тем FAQ — enum `topic` в `get_kb_fact`."""
    def faq_entry(self, topic: str, scope: Scope) -> FaqEntry | None: ...
    def text(self, key: str, lang: Language, **params: object) -> str:
        """Строка i18n с подстановкой плейсхолдеров. Нет ключа -> KBValidationError."""
    def gaps(self) -> tuple[GapRef, ...]: ...
```

**Заморожен полный enum тем FAQ** (KB-SPEC §3.1 + §5.5 `ARCHITECTURE.md`), 18 значений:

```python
FAQ_TOPICS: Final[tuple[str, ...]] = (
    "trial", "docs", "gear", "safety", "age_groups", "payment", "freeze", "coaches",
    "results", "girls", "adults", "instagram", "contacts", "offer",
    "sessions_count", "group_size", "competitions", "summer",
)
```

### 8.2 `app/kb/loader.py`

```python
async def load(kb_dir: Path, *, media_dir: Path, schema_version: int) -> KBSnapshot:
    """Читает 7 YAML, валидирует, проверяет медиа-файлы, считает kb_hash.

    Кидает KBValidationError со списком всех найденных ошибок (не первой).
    Сети не касается; вызывается в lifespan и в /admin/kb/reload.
    """

def compute_kb_hash(files: Mapping[str, bytes]) -> str:
    """sha256 от детерминированной сериализации: сортировка имён файлов, \n, ensure_ascii=False."""

def get_snapshot() -> KBSnapshot:
    """Текущий снимок. До первой загрузки — KBNotLoadedError."""

def swap(snapshot: KBSnapshot) -> str:
    """Атомарная подмена ссылки. Возвращает предыдущий kb_hash (или '')."""

async def reload(kb_dir: Path, *, media_dir: Path, schema_version: int) -> tuple[str, str]:
    """Загрузка + swap. Возвращает (old_hash, new_hash). При ошибке старый снимок остаётся."""

async def mirror_to_db(session: AsyncSession, snapshot: KBSnapshot) -> int:
    """Полная перезапись таблицы `gym` в транзакции. Возвращает число строк."""

def main(argv: Sequence[str] | None = None) -> int:
    """CLI `python -m app.kb.loader --check`: 0 — валидна, 1 — нет (используется в Makefile)."""
```

**Правила валидации, обязательные к реализации:** уникальность `gyms[].id`;
`scope == city` ⟺ `settlement == city_settlement`; непересечение `district_aliases` между залами,
кроме алиасов, ведущих на `status=unresolved`; `geo_lat` и `geo_lon` заданы оба или ни одного;
`media[].gym_id` ссылается на существующий зал; `artifact.render_from is None` ⟹ для
`text_card/link/location_text` обязателен `body`; для `image/document` обязательны все четыре поля
`file_*`, файл существует и sha256 совпадает — иначе артефакт принудительно `enabled=False` и пишется
`kb_load_failures_total`; в `i18n` совпадает множество плейсхолдеров в `ru` и `kk`, длина ≤ 900;
`faq[].topic ∈ FAQ_TOPICS`.

**Блокеры старта — только технические:** нечитаемый или невалидный YAML, несовпадение
`schema_version`, отсутствие каталога `kb/`. Проверок БИН, текстов согласия и `policy_url` **нет**.

### 8.3 `app/kb/render.py`

```python
def render_system_prompt(snapshot: KBSnapshot) -> str:
    """Статический префикс system_instruction. Байт-в-байт стабилен при одном kb_hash.

    Порядок блоков зафиксирован (KB-SPEC §8.2): роль и тон -> реестр залов ->
    витрина цен -> дайджест FAQ -> каталог артефактов -> манифест пробелов ->
    правила эскалации -> few-shot. Динамики (дата, язык, имя) здесь нет.
    """

def render_gyms_block(snapshot: KBSnapshot) -> str: ...
def render_pricing_showcase(snapshot: KBSnapshot) -> str: ...
def render_faq_digest(snapshot: KBSnapshot) -> str: ...
def render_artifacts_catalog(snapshot: KBSnapshot) -> str: ...
def render_gaps_manifest(snapshot: KBSnapshot) -> str: ...
def render_gyms_list_card(snapshot: KBSnapshot, *, scope: Scope, lang: Language) -> str:
    """Тело артефакта `gyms_list_*` (render_from: gyms)."""
def render_price_card(snapshot: KBSnapshot, *, scope: Scope, lang: Language) -> str:
    """Тело артефакта `price_card_*` (render_from: pricing)."""
def render_gym_location(snapshot: KBSnapshot, *, gym_id: str, lang: Language) -> str:
    """Тело артефакта `gym_location_<gym_id>`: адрес + ориентир + ссылка, если есть."""
```

**Инвариант, проверяемый тестом:** `render_system_prompt(s) == render_system_prompt(s)` побайтно
и не содержит ни одного `internal_note`, ни одного телефона, ни одной строки расписания.

### 8.4 `app/kb/gaps.py`

```python
GAP_TO_I18N: Final[dict[GapRef, str]] = {...}
"""G-1 -> 'gap.schedule', G-2 -> 'gap.contacts', G-3 -> 'gap.region_address',
G-4 -> 'gap.trial_conditions', G-5 -> 'gap.gear', G-6 -> 'gap.docs',
G-8 -> 'gap.coaches', G-9 -> 'gap.payment', C-3 -> 'gap.kzhbi'."""

def say_no_data(snapshot: KBSnapshot, gap: GapRef) -> dict[str, str]:
    """{'ru': ..., 'kk': ...} — готовая фраза-заглушка для say_if_no_data."""

def gap_for_topic(topic: str) -> GapRef | None: ...
def is_blocking(gap: GapRef) -> bool:
    """Всегда False: юридических блокеров запуска в проекте нет."""
```
## 9. `app/channels/*` (волна 2a)

Слой не знает ни про Gemini, ни про KB, ни про core. Единственный, кто разбирает и собирает
HTTP Wazzup24.

### 9.1 `app/channels/wazzup_schemas.py`

Pydantic-модели строго по `research-wazzup24.md` §5. Имена полей — **как в JSON** (camelCase),
через `alias`; выдумывать поля запрещено, недокументированные объекты типизированы как
`dict[str, Any] | None` с комментарием `# НП: структура не раскрыта в доке`.

```python
class WzContact(BaseModel):
    name: str | None; avatarUri: str | None; username: str | None; phone: str | None

class WzError(BaseModel):
    error: str | None; description: str | None; data: dict[str, Any] | None

class WzMessage(BaseModel):
    messageId: str; channelId: str; chatType: str; chatId: str
    dateTime: str; type: str | None; isEcho: bool | None
    contact: WzContact | None; text: str | None; contentUri: str | None
    status: str | None; error: WzError | None
    authorName: str | None; authorId: str | None
    instPost: dict[str, Any] | None      # НП: полный состав не документирован
    interactive: list[Any] | None        # НП
    quotedMessage: dict[str, Any] | None # НП
    sentFromApp: bool | None             # НП: приходит ли false для API-сообщений
    isEdited: bool | None; isDeleted: bool | None
    oldInfo: dict[str, Any] | None
    avitoProfileId: str | None; advert: dict[str, Any] | None

class WzStatus(BaseModel):
    messageId: str; timestamp: str; status: str; error: WzError | None

class WzChannelUpdate(BaseModel):
    channelId: str; state: str; timestamp: int | None
    tier: str | None; qr: str | None; qridle: bool | None

class WzTemplateStatus(BaseModel):
    templateGuid: str; name: str; status: str

class WebhookPayload(BaseModel):
    """Тело вебхука. messages и statuses могут прийти одновременно."""
    test: bool | None = None
    messages: list[WzMessage] | None = None
    statuses: list[WzStatus] | None = None
    channelsUpdates: list[WzChannelUpdate] | None = None
    templateStatus: WzTemplateStatus | None = None
    createContact: dict[str, Any] | None = None   # мы не CRM, игнорируем
    createDeal: dict[str, Any] | None = None      # мы не CRM, игнорируем

class SendMessageRequest(BaseModel):
    channelId: str; chatType: str; chatId: str
    text: str | None = None; contentUri: str | None = None
    crmMessageId: str | None = None; refMessageId: str | None = None
    clearUnanswered: bool = False

class SendMessageResponse(BaseModel):
    messageId: str | None = None; chatId: str | None = None
```

`WebhookPayload` **не** ставит `extra="forbid"`: новые поля Wazzup не должны ронять приём.

### 9.2 `app/channels/errors.py`

```python
def normalize_code(code: str | None) -> str:
    """`code.replace("_", "").lower()`. Регистр в доке Wazzup непоследователен."""

def classify(status: int, code: str | None, description: str | None,
             data: dict[str, Any] | None = None, request_id: str | None = None) -> WazzupError:
    """Отображает HTTP-ответ в конкретный подкласс WazzupError."""

RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})
DUPLICATE_CODES: Final[frozenset[str]] = frozenset({"repeatedcrmmessageid"})
CHANNEL_CODES: Final[frozenset[str]]     # channelnotfound, channelblocked, channelnomoney,
                                         # channelwapirejected, channellimitexceeded,
                                         # messagechannelunavailable
SPAM_CODES: Final[frozenset[str]] = frozenset({"messagesisspam"})
BAD_CONTACT_CODES: Final[frozenset[str]] = frozenset({"badcontact", "chatidigsidmismatch"})
```

**Правило классификации:** `repeatedcrmmessageid` → `WazzupDuplicateError` (вызывающий трактует как
успех); 429 → `WazzupRateLimitError`; 5xx → `WazzupServerError`; 400/401/403 — не ретраить.

### 9.3 `app/channels/wazzup_client.py`

```python
class WazzupClient:
    """httpx-клиент Wazzup24 v3. Сети в конструкторе нет — клиент создаётся лениво."""

    def __init__(self, *, api_key: str, base_url: str, timeout_ms: int) -> None: ...
    async def send_message(self, req: SendMessageRequest) -> SendMessageResponse:
        """POST /v3/message. Успех — 200 или 201. Ошибки -> classify()."""
    async def get_channels(self) -> list[ChannelState]:
        """GET /v3/channels. Сравнение state регистронезависимо, неизвестное = не active."""
    async def get_webhooks(self) -> dict[str, Any]:
        """GET /v3/webhooks."""
    async def set_webhooks(self, uri: str, *, messages_and_statuses: bool = True,
                           contacts_and_deals_creation: bool = False,
                           channels_updates: bool = True,
                           template_status: bool = False) -> None:
        """PATCH /v3/webhooks. contacts_and_deals_creation обязан быть False — мы не CRM.
        Перед вызовом убедиться, что len(uri) <= 200."""
    async def aclose(self) -> None: ...
```

### 9.4 `app/channels/normalize.py`

```python
def to_inbound(msg: WzMessage, *, received_at: datetime) -> InboundMessage | None:
    """Вебхук -> внутренняя модель. None, если chatType не whatsapp/instagram."""

def to_status(update: WzStatus) -> StatusUpdate: ...
def to_channel_state(update: WzChannelUpdate) -> ChannelState: ...

def parse_datetime(value: str) -> datetime:
    """`yyyy-mm-ddThh:mm:ss.msZ` -> aware datetime в UTC."""

def phone_from_chat_id(chat_id: str, channel: ChannelKind) -> str | None:
    """WhatsApp: `77012345678` -> `+77012345678`. Instagram -> None."""

def chat_id_from_phone(phone_e164: str) -> str:
    """`+77012345678` -> `77012345678` (только цифры, без `+`)."""

def is_operator_echo(msg: InboundMessage, *, known_outbox: bool) -> bool:
    """Эхо оператора: is_echo и (sent_from_app is True или not known_outbox).

    known_outbox — результат repo_outbox.exists_by_wazzup_message_id.
    Второй признак основной: приходит ли sentFromApp=false для API-сообщений, НЕ ПОДТВЕРЖДЕНО.
    """
```

**[РЕШЕНО]** `chatId` для Instagram никогда не конструируется — берётся только из вебхука.

### 9.5 `app/channels/outbound.py`

```python
def split_text(text: str, *, channel: ChannelKind, soft_limit: int, hard_limit: int) -> list[str]:
    """Режет по абзацам, затем по предложениям. Не рвёт слова, не рвёт URL.
    Не более `max_messages_per_turn` кусков: остаток обрезается по границе предложения."""

def sanitize(text: str) -> str:
    """Убирает markdown-заголовки, `**`, нумерованные списки, лишние эмодзи.
    Списки приводит к тире `—`. WhatsApp/Instagram markdown не рендерят."""

def check_artifact_deliverable(*, channel: ChannelKind, kind: ArtifactKind,
                               mime: str | None, size_bytes: int | None,
                               allowed: bool) -> str | None:
    """None — можно слать; иначе причина отказа (mime, размер, channels: deny).

    Принимает примитивы, а не kb-модель: слой channels про KB не знает.
    Разворачивает Artifact в аргументы вызывающий — app/tools/content.py.
    """

def build_send_request(msg: OutboundMessage) -> SendMessageRequest:
    """OutboundMessage -> тело POST /v3/message. clearUnanswered всегда False."""

def next_backoff_ms(attempt: int, *, base_ms: int) -> int:
    """Экспонента 1/2/4/8 с + jitter до 25%. attempt начинается с 1."""
```

## 10. `app/llm/*` (волна 2b)

Единственное место, где импортируется `google.genai`. Наружу отдаёт только типы из `app.types`
и `list[dict]` истории.

### 10.1 `app/llm/config.py`

```python
MODEL_PRIMARY: str            # из настроек, не из литерала
MODEL_FALLBACK: str
SAFETY: Final[list[types.SafetySetting]]
"""DANGEROUS_CONTENT=OFF (бокс — контактный спорт), HARASSMENT=OFF,
HATE_SPEECH=BLOCK_ONLY_HIGH, SEXUALLY_EXPLICIT=BLOCK_MEDIUM_AND_ABOVE.
Категория называется HARM_CATEGORY_DANGEROUS_CONTENT — HARM_CATEGORY_DANGEROUS не существует."""

def build_config(*, system_instruction: str, tools: list[types.Tool] | None,
                 allowed_function_names: Sequence[str] | None,
                 mode: str, max_output_tokens: int) -> types.GenerateContentConfig:
    """temperature/top_p/top_k НЕ задаются. automatic_function_calling отключён.
    thinking_config задаётся явно из настроек."""
```

### 10.2 `app/llm/client.py`

```python
class LLMClient(Protocol):
    async def generate(self, req: LLMRequest, executor: ToolExecutor) -> LLMResponse: ...
    async def extract_lead(self, transcript: str, *, lang: Language) -> tuple[LeadDraft, LLMUsage]: ...
    async def warmup(self) -> None: ...
    async def aclose(self) -> None: ...

class GeminiClient(LLMClient):
    def __init__(self, *, api_key: str, settings: Settings) -> None:
        """Клиент google-genai создаётся лениво при первом вызове; ключ не проверяется."""

class NullLLMClient(LLMClient):
    """Заглушка для тестов и для режима «только KB»: всегда LLMResponse(text=None, blocked=True)."""

def safe_text(response: "types.GenerateContentResponse") -> str | None:
    """Единственный разрешённый способ достать текст.
    Проверяет prompt_feedback.block_reason, наличие candidates и finish_reason.
    response.text напрямую не читается нигде."""

def build_client(settings: Settings) -> LLMClient: ...
```

**Контракт `generate`:** все вызовы через `client.aio.models.generate_content`; ответ модели кладётся
в историю **целиком** (`candidate.content`), с thought signatures; при `429 rate_limit_exceeded` —
встроенный retry SDK; при исчерпании — фолбэк-модель (`fallback_used=True`); при
`quota_exceeded` — `LLMQuotaError` без ретраев.

### 10.3 `app/llm/prompt.py` и `app/llm/dynamic.py`

```python
# prompt.py
def build_system_instruction(kb_prompt: str) -> str:
    """Оборачивает отрендеренный KB-префикс в правила безопасности.
    Пользовательский текст сюда не попадает НИКОГДА."""

def prompt_ngrams(system_instruction: str, n: int = 8) -> frozenset[str]:
    """N-граммы для детектора утечки промпта (используется постфильтром)."""

# dynamic.py
def build_dynamic_note(*, lang: Language, now: datetime, lead: LeadDraft,
                       intents: Sequence[IntentHint], injection_suspected: bool,
                       gym_id: str | None = None) -> str:
    """Служебная заметка ПОСЛЕДНИМ элементом contents. Любая динамика в начале убивает кэш."""

def wrap_user_message(text: str) -> str:
    """`<user_message>…</user_message>`. Содержимое — данные, а не инструкции."""
```

### 10.4 `app/llm/tools_schema.py`

```python
def to_function_declarations(specs: Sequence[ToolSpec]) -> list[types.Tool]:
    """ToolSpec -> types.Tool. Схемы плоские, только поддержанное подмножество OpenAPI."""

def allowed_names(specs: Sequence[ToolSpec], subset: Sequence[str] | None) -> list[str]: ...
```

### 10.5 `app/llm/tool_runner.py`

```python
async def run_tool_loop(*, client: "genai.Client", model: str,
                        contents: list["types.Content"],
                        config: "types.GenerateContentConfig",
                        executor: ToolExecutor, max_loops: int,
                        timeout_ms: int) -> LLMResponse:
    """Ручной цикл. Ключевые инварианты:

    * candidate.content целиком уходит в contents;
    * результаты — ОДНИМ Content с role="user";
    * Part собирается вручную:
      types.Part(function_response=types.FunctionResponse(id=call.id, name=call.name, response=result));
      Part.from_function_response() использовать нельзя — он не принимает id;
    * не более max_loops витков, иначе LLMToolLoopError.
    """

def content_to_dict(content: "types.Content") -> dict[str, Any]:
    """model_dump(mode="json", exclude_none=True)."""

def dict_to_content(raw: dict[str, Any]) -> "types.Content": ...

def trim_history(contents: list[dict[str, Any]], *, max_turns: int) -> list[dict[str, Any]]:
    """Режет ТОЛЬКО по границе завершённого tool-цикла: function_call без парного
    function_response в истории ломает запрос."""
```

### 10.6 `app/llm/extract_lead.py` и `app/llm/usage.py`

```python
# extract_lead.py
class LeadExtraction(BaseModel):
    """Схема structured output. Плоская, без вложенности глубже 2 уровней."""
    parent_name: str | None; parent_relation: str | None; phone: str | None
    child_name: str | None; child_age: int | None; child_birth_year: int | None
    child_gender: Literal["m", "f", "unknown"]; district: str | None; gym_id: str | None
    trial_slot_text: str | None; motivation: str | None; main_objection: str | None
    prior_experience: str | None; health_notes: str | None
    language: Literal["ru", "kk"]; ready_to_book: bool

async def extract(client: "genai.Client", *, transcript: str, lang: Language,
                  model: str, gym_ids: Sequence[str]) -> tuple[LeadDraft, LLMUsage]:
    """Отдельный «тихий» вызов: response_mime_type="application/json" + response_schema.
    Structured output и function calling в одном вызове не смешиваются.
    Синтаксис JSON гарантирован, смысл — нет: телефон и возраст перепроверяются кодом."""

# usage.py
def usage_from_response(response: Any, *, model: str, latency_ms: int) -> LLMUsage:
    """Читает response.usage_metadata. Кэш — cached_content_token_count.
    Атрибута `usage` у GenerateContentResponse НЕТ (это Interactions API)."""

def estimate_cost_usd(usage: LLMUsage, *, price_in: float, price_out: float) -> float:
    """thoughts-токены тарифицируются как выходные."""
```
## 11. `app/tools/*` — восемь инструментов (волна 2c)

Инструментов ровно **восемь**, новых добавлять нельзя. Пять детерминированных, один — выбор из
реестра, два — побочные эффекты. Ни один инструмент не бросает исключение ради бизнес-исхода:
всё через `ToolResult`. Исключение `ToolExecutionError` допустимо только при внутреннем сбое и
ловится раннером.

### 11.1 `app/tools/registry.py`

```python
TOOL_NAMES: Final[tuple[str, ...]] = (
    "get_gyms", "find_gym_by_district", "calculate_price", "get_schedule",
    "get_kb_fact", "send_content", "create_trial_lead", "escalate_to_manager",
)

ENUM_GYM_ID: Final[str] = "{{ENUM:gym_id}}"
ENUM_ARTIFACT_ID: Final[str] = "{{ENUM:artifact_id}}"
ENUM_FAQ_TOPIC: Final[str] = "{{ENUM:topic}}"

RAW_TOOL_SPECS: Final[tuple[ToolSpec, ...]]
"""Схемы с плейсхолдерами вместо enum'ов. Единственный источник JSON-схем в проекте:
app/llm/tools_schema.py их только преобразует, но не хранит."""

def build_tool_specs(kb: KBSnapshot) -> tuple[ToolSpec, ...]:
    """Подставляет enum'ы из KB: gym_id из kb.gym_ids(), artifact_id из kb.artifact_ids(),
    topic из kb.faq_topics(). Модель физически не может назвать несуществующий id."""

def get_impl(name: str) -> "ToolFn":
    """Имя -> реализация. Неизвестное имя -> KeyError."""

def is_deterministic(name: str) -> bool: ...

async def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Валидирует args по схеме (лишние ключи отбрасываются, недостающие обязательные ->
    ToolResult.invalid_input) и вызывает реализацию. Никогда не пробрасывает исключение:
    любое падение превращается в ToolResult.failure."""
```

Тип реализации: `ToolFn = Callable[..., Awaitable[ToolResult]]`, первый позиционный параметр —
`ctx: ToolContext`, остальные — только ключевые, имена совпадают с ключами JSON-схемы.

### 11.2 `calculate_price` — детерминированный (`app/tools/pricing.py`)

*Description для модели (дословно в декларации):* «Рассчитать стоимость. Использовать **ВСЕГДА**,
когда речь о деньгах, скидках, "сколько выйдет", сравнении абонемента с разовыми. **Никогда не
называть и не складывать цены самостоятельно.** Перед вызовом обязательно определить, город это или
райцентр.»

```json
{
  "type": "object",
  "properties": {
    "scope": {"type": "string", "enum": ["city", "region"],
              "description": "city — Костанай; region — райцентр области"},
    "plan": {"type": "string", "enum": ["standard", "flexible", "single", "unknown"],
             "description": "standard — без перерасчёта; flexible — с перерасчётом; single — разовая"},
    "children_count": {"type": "integer", "minimum": 1, "maximum": 5,
                       "description": "Сколько детей из ОДНОЙ семьи будут заниматься"},
    "single_sessions": {"type": "integer", "minimum": 1, "maximum": 60,
                        "description": "Только для plan=single: сколько разовых тренировок посчитать"}
  },
  "required": ["scope", "plan", "children_count"]
}
```

```python
async def calculate_price(ctx: ToolContext, *, scope: str, plan: str,
                          children_count: int, single_sessions: int | None = None) -> ToolResult:
    """Чистая функция поверх pricing.yaml. render_hint=NUMBERS_ONLY. 100% покрытие тестами."""
```

`data` при `status=ok`: `currency, scope, plan, sessions_included, validity_days, recalculation,
per_child:[{index, discount_pct, price}], total, price_per_session,
compare_single:{single_price, twelve_singles, saving_vs_single}`.

Обязательные исходы: `city` — 1-й ребёнок 100%, 2-й −10%, 3-й −15%, свыше `max_children` →
`needs_operator`; округление `half_up` до 10 ₸; `region` — 1 ребёнок 10 000 ₸, ≥ `min_children` →
8 000 ₸ **за каждого**; `region` + `flexible|single` → `no_data` (G-10); `plan=unknown` → обе витрины
без выбора за клиента; `applies_to_status=unconfirmed` и `plan=flexible` → обязательный caveat (C-4);
дети на разных тарифах → `needs_operator` (C-5).

### 11.3 `get_gyms` — детерминированный (`app/tools/gyms.py`)

*Description:* «Использовать всегда при вопросах об адресах, районах, "где вы", "сколько у вас залов".
Никогда не перечислять залы по памяти.»

```json
{"type": "object",
 "properties": {
   "scope": {"type": "string", "enum": ["city", "region", "all"]},
   "settlement": {"type": "string", "description": "Название населённого пункта, если клиент назвал"},
   "limit": {"type": "integer", "minimum": 1, "maximum": 12}},
 "required": ["scope"]}
```

```python
async def get_gyms(ctx: ToolContext, *, scope: str, settlement: str | None = None,
                   limit: int = 12) -> ToolResult:
    """Только active=True и status=open, тексты на языке диалога. render_hint=VERBATIM."""
```

`data.gyms[]`: `id, title, address, landmark, district, scope, has_schedule, phone, map_url`.
`phone` и `map_url` сегодня `null` (G-2, G-15) → в `caveats` уходит фраза-заглушка.

### 11.4 `find_gym_by_district` — детерминированный (`app/tools/gyms.py`)

```json
{"type": "object",
 "properties": {
   "district_text": {"type": "string", "description": "Слова клиента о районе как есть, без правки"},
   "settlement": {"type": "string"}},
 "required": ["district_text"]}
```

```python
async def find_gym_by_district(ctx: ToolContext, *, district_text: str,
                               settlement: str | None = None) -> ToolResult:
    """Матчинг по district_aliases + лексикону, БЕЗ модели. До 3 залов, отсортированы по силе
    совпадения. Каждый элемент несёт match: "alias" | "settlement" | "fallback"."""
```

**Особый случай КЖБИ (C-3):** попадание в запись со `status=unresolved` → `needs_operator` и фраза
`i18n.gap.kzhbi`. Бот не имеет права утверждать ни что зал на КЖБИ есть, ни что его нет.

### 11.5 `get_schedule` — детерминированный, сегодня всегда `no_data` (`app/tools/schedule.py`)

```json
{"type": "object",
 "properties": {
   "gym_id": {"type": "string", "enum": ["<из kb/gyms.yaml>"]},
   "child_age": {"type": "integer", "minimum": 3, "maximum": 60},
   "shift": {"type": "string", "enum": ["first", "second", "unknown"],
             "description": "Смена в школе: first — учится в первую, second — во вторую"}},
 "required": ["gym_id"]}
```

```python
async def get_schedule(ctx: ToolContext, *, gym_id: str, child_age: int | None = None,
                       shift: str = "unknown") -> ToolResult:
    """Пока gyms[].schedule == [] возвращает no_data с gap_ref=G-1 и готовой фразой на RU/KK.
    Существует именно для того, чтобы модель не выдумывала время."""
```

### 11.6 `get_kb_fact` — детерминированный поиск (`app/tools/facts.py`)

```json
{"type": "object",
 "properties": {
   "topic": {"type": "string", "enum": ["trial", "docs", "gear", "safety", "age_groups", "payment",
                                        "freeze", "coaches", "results", "girls", "adults",
                                        "instagram", "contacts", "offer", "sessions_count",
                                        "group_size", "competitions", "summer"]},
   "scope": {"type": "string", "enum": ["city", "region", "any"]}},
 "required": ["topic"]}
```

```python
async def get_kb_fact(ctx: ToolContext, *, topic: str, scope: str = "any") -> ToolResult:
    """data: {answer_ru, answer_kk, source, gap_ref}. Пустой answer -> no_data + фраза-заглушка,
    при escalate_if_empty=True -> needs_operator. render_hint=VERBATIM."""
```

Темы из этого enum модель **не имеет права** закрывать без вызова инструмента — это прописано и в
системном промпте, и проверяется постфильтром.

### 11.7 `send_content` — доставка из реестра (`app/tools/content.py`)

```json
{"type": "object",
 "properties": {
   "artifact_id": {"type": "string", "enum": ["<из kb/media.yaml, только enabled=true>"]},
   "gym_id": {"type": "string", "description": "Если артефакт привязан к конкретному залу"}},
 "required": ["artifact_id"]}
```

```python
async def send_content(ctx: ToolContext, *, artifact_id: str, gym_id: str | None = None) -> ToolResult:
    """Ставит артефакт в outbox САМ, до того как модель сформулировала текст.
    render_hint=SILENT: модель узнаёт факт постановки и пишет одно короткое сопроводительное
    сообщение, содержимое артефакта не пересказывает.
    Проверки: доставляемость в канале, max_send_per_dialog, наличие файла и sha256."""

def make_media_token(rel_path: str, *, ttl_s: int, secret: str) -> str:
    """HMAC-подписанный токен для /media/{token}. Живёт ttl_s (по умолчанию 10 минут)."""

def parse_media_token(token: str, *, secret: str, now: datetime) -> str:
    """Токен -> относительный путь. Просрочка/подделка -> ValueError."""

def build_media_url(rel_path: str) -> str:
    """`{public_base_url}/media/{token}`. Ссылка отдаёт файл напрямую, без редиректов."""
```

**[РЕШЕНО]** Кодек токена живёт здесь, а не в `app/api/media.py`: `api` может импортировать `tools`,
обратное направление запрещено.

`data` при `ok`: `{"queued": [{"kind": ..., "artifact_id": ...}], "note": ...}`.

### 11.8 `create_trial_lead` — побочный эффект (`app/tools/booking.py`)

```json
{"type": "object",
 "properties": {
   "child_name": {"type": "string", "maxLength": 60},
   "child_age": {"type": "integer", "minimum": 3, "maximum": 17},
   "child_gender": {"type": "string", "enum": ["m", "f", "unknown"]},
   "gym_id": {"type": "string", "enum": ["<из kb/gyms.yaml>"]},
   "preferred_time_text": {"type": "string", "description": "Как сказал родитель: «среда вечером»"},
   "parent_name": {"type": "string"},
   "phone": {"type": "string", "description": "Только если родитель назвал номер сам. Для WhatsApp не спрашивать — номер уже известен"},
   "motivation": {"type": "string", "maxLength": 120},
   "main_objection": {"type": "string", "maxLength": 120},
   "health_notes": {"type": "string", "maxLength": 200}},
 "required": ["child_name", "child_age", "gym_id"]}
```

```python
async def create_trial_lead(ctx: ToolContext, *, child_name: str, child_age: int, gym_id: str,
                            child_gender: str = "unknown", preferred_time_text: str | None = None,
                            parent_name: str | None = None, phone: str | None = None,
                            motivation: str | None = None, main_objection: str | None = None,
                            health_notes: str | None = None) -> ToolResult:
    """Фиксирует запись и отдаёт карточку администратору.

    Проверок согласия и кода need_consent НЕТ (SCOPE-OVERRIDE §1): телефон принимается сразу.
    Остаются как защита от галлюцинаций модели: валидация телефона PHONE_E164_KZ_RE,
    диапазон возраста 3..17, gym_id из KB.
    Идемпотентность: повторный вызов в пределах диалога ОБНОВЛЯЕТ лид, а не плодит новый.
    Транзакция: lead + outbox(карточка) атомарно. render_hint=SUMMARIZE.
    data: {lead_id, status: "trial_booked" | "needs_call", admin_notified: true}
    """

def normalize_phone_kz(raw: str | None) -> str | None:
    """`8 705 123 45 67`, `+7 705…`, `7705…` -> `+77051234567`. Мусор -> None.
    Публичная функция: используется также core/lexicon.extract_phone."""
```

Возраст вне 3..17 → `needs_operator` с `reason=age_out_of_range` (лид не создаётся).
Невалидный телефон → лид создаётся **без** телефона, `phone_source=none`, в `caveats` — просьба
уточнить номер у администратора.

### 11.9 `escalate_to_manager` — побочный эффект (`app/tools/escalation.py`)

```json
{"type": "object",
 "properties": {
   "reason": {"type": "string", "enum": ["user_request", "no_data", "complaint", "medical",
                                         "price_off_list", "installments", "age_out_of_range",
                                         "foreign_language", "repeated_miss"]},
   "question_summary": {"type": "string", "maxLength": 200},
   "urgency": {"type": "string", "enum": ["normal", "high"]}},
 "required": ["reason", "question_summary"]}
```

```python
async def escalate_to_manager(ctx: ToolContext, *, reason: str, question_summary: str,
                              urgency: str = "normal") -> ToolResult:
    """Ставит паузу, шлёт карточку «НУЖЕН ЖИВОЙ ОТВЕТ», возвращает готовый текст клиенту
    из i18n `escalation.handoff`. render_hint=FIXED_REPLY — модель этот текст не переписывает."""
```

Enum `reason` в схеме — ровно `TOOL_ESCALATION_REASONS` (9 значений). Остальные значения
`EscalationReason` ставит код и в схему не попадают.

### 11.10 Сводка

| Инструмент | Детерминированный | Побочный эффект | `render_hint` по умолчанию |
|---|---|---|---|
| `calculate_price` | да, полностью | нет | `NUMBERS_ONLY` |
| `get_gyms` | да | нет | `VERBATIM` |
| `find_gym_by_district` | да | нет | `VERBATIM` |
| `get_schedule` | да (сейчас всегда `no_data`) | нет | `VERBATIM` |
| `get_kb_fact` | да (поиск по KB) | нет | `VERBATIM` |
| `send_content` | да (выбор из реестра) | да (outbox) | `SILENT` |
| `create_trial_lead` | нет | да (lead + карточка) | `SUMMARIZE` |
| `escalate_to_manager` | нет | да (пауза + карточка) | `FIXED_REPLY` |

Модель отвечает только за выбор инструмента, формулировку, тон, язык, порядок вопросов и эмпатию.
Ни одна цифра, дата, адрес и имя в исходящем сообщении не может появиться иначе, чем из `data`.
## 12. `app/core/*` (волна 3a)

Модулей `consent.py` и `pii.py` **нет** — они отменены целиком. Порядок шагов пайплайна:
**дедуп → эхо/оператор → пауза → debounce → сессия → язык → guards → LLM+tools → postcheck → outbox.**

### 12.1 `app/core/pipeline.py`

```python
@dataclass(slots=True)
class PipelineDeps:
    """Всё, что пайплайну нужно снаружи. Собирается в app/deps.py."""
    sessionmaker: async_sessionmaker[AsyncSession]
    state: StateStore
    llm: LLMClient
    kb: Callable[[], KBSnapshot]
    queue: JobQueue
    settings: Settings

async def process_inbound(deps: PipelineDeps, payload: dict[str, Any]) -> list[PipelineDecision]:
    """Обрабатывает один webhook-payload целиком: messages, statuses, channelsUpdates.
    Возвращает по решению на каждое обработанное сообщение. Исключения наружу не выпускает:
    любая ошибка превращается в решение с action=ESCALATE или DROP."""

async def process_message(deps: PipelineDeps, inbound: InboundMessage) -> PipelineDecision:
    """Один шаг сценария §1 ARCHITECTURE. Точка входа для e2e-тестов."""

async def build_tool_services(deps: PipelineDeps, session: AsyncSession,
                              conv: Conversation) -> ToolServices:
    """Реализация протокола ToolServices поверх репозиториев текущей транзакции."""

async def build_tool_executor(deps: PipelineDeps, ctx: ToolContext) -> ToolExecutor:
    """Замыкание вокруг tools.registry.dispatch: собирает ToolInvocation и метрики."""
```

**Правила, обязательные к реализации:**

* решение `DEFER` — сообщение попало в окно дебаунса, ответа сейчас нет;
* при паузе бот **пишет** входящее в БД и в историю Gemini, но не отвечает и не шлёт follow-up;
* `bot_miss_count >= bot_miss_limit` → эскалация;
* при `injection_suspected` список инструментов сужается до
  `("get_kb_fact", "escalate_to_manager")`, строгость постфильтра повышается;
* лид извлекается «тихим» вызовом (`llm.extract_lead`) **после** ответа, не в основном ходу;
* все исходящие идут через outbox, прямых вызовов `WazzupClient` в пайплайне нет.

### 12.2 `app/core/dedup.py`

```python
async def seen_message(state: StateStore, session: AsyncSession, message_id: str) -> bool:
    """Двойной барьер: SETNX в Redis (быстрый путь) + INSERT ... ON CONFLICT DO NOTHING
    в processed_webhook (надёжный). True — уже обрабатывали, задачу нужно бросить."""

async def seen_status(state: StateStore, session: AsyncSession,
                      message_id: str, status: MessageStatus) -> bool:
    """Статусы дедуплицируются по паре (messageId, status)."""
```

### 12.3 `app/core/debounce.py`

```python
async def collect(state: StateStore, conv_key: str, text: str, *,
                  window_s: int, max_window_s: int) -> str | None:
    """Кладёт текст в буфер серии. None — окно ещё открыто, обрабатывать рано.
    Строка — окно закрылось, вернулась склейка сообщений в порядке поступления."""

@asynccontextmanager
async def conversation_lock(state: StateStore, conv_key: str, *, ttl_s: int) -> AsyncIterator[bool]:
    """Одновременность на диалог — ровно 1. False — лок занят, задача откладывается."""

def join_messages(parts: Sequence[str]) -> str:
    """Склейка серии: перевод строки между частями, дубли подряд схлопываются."""
```

### 12.4 `app/core/session.py`

```python
async def ensure_conversation(session: AsyncSession, inbound: InboundMessage,
                              *, kb_hash: str) -> Conversation: ...
async def record_inbound(session: AsyncSession, conv: Conversation,
                         inbound: InboundMessage, *, author: Author) -> UUID: ...
async def load_history(session: AsyncSession, conv: Conversation,
                       *, max_turns: int) -> list[dict[str, Any]]: ...
async def save_turn(session: AsyncSession, conv: Conversation,
                    contents: Sequence[dict[str, Any]]) -> None: ...
async def maybe_summarize(session: AsyncSession, conv: Conversation,
                          llm: LLMClient, *, max_turns: int) -> str | None:
    """Резюмирует хвост при обрезке истории. None — резюмировать нечего."""
def service_window_until(channel: ChannelKind, last_inbound_at: datetime) -> datetime | None:
    """Instagram +7 дней; личный WhatsApp — None (окно не применяется)."""
```

### 12.5 `app/core/pause.py`

```python
async def is_paused(state: StateStore, session: AsyncSession, conv_id: UUID,
                    conv_key: str, now: datetime) -> bool: ...
async def set_pause(state: StateStore, session: AsyncSession, conv_id: UUID, conv_key: str, *,
                    minutes: int, reason: PauseReason, now: datetime,
                    author_id: str | None = None, author_name: str | None = None) -> None:
    """Ставит или ПРОДЛЕВАЕТ окно. Каждое новое сообщение оператора продлевает паузу."""
async def resume(state: StateStore, session: AsyncSession, conv_id: UUID, conv_key: str, *,
                 by: Literal["timeout", "operator_command", "admin"]) -> None: ...
def detect_operator(inbound: InboundMessage, *, known_outbox: bool) -> bool:
    """См. channels.normalize.is_operator_echo. Расхождение признаков пишется
    в метрику echo_signal_mismatch_total — по ней закрывается открытый вопрос №12."""
def is_resume_command(text: str | None, *, command: str) -> bool:
    """Служебная строка оператора (по умолчанию `#бот`) снимает паузу.
    Сама строка клиенту не пересылается — она уже ушла от оператора."""
def pause_minutes_for(reason: PauseReason, settings: Settings) -> int: ...
```

**Никогда** не снимать паузу по инициативе клиента: новое сообщение клиента паузу только продлевает
диалог, но не возвращает бота. После возврата бот молчит до следующего входящего.

### 12.6 `app/core/language.py`

```python
class LanguageDecision(BaseModel):
    lang: Language
    locked: bool
    needs_bridge: bool          # добавить мостик `bridge.kk_offer` в конец первого ответа
    confidence: float
    source: Literal["graphemes", "words", "translit", "previous", "default", "switch"]

def detect(text: str, *, lexicon: LexiconFile, previous: Language | None = None,
           locked: bool = False) -> LanguageDecision:
    """Правила §6 ARCHITECTURE: язык по ПОСЛЕДНЕМУ сообщению; смешанная реплика —
    по смысловой части, не по приветствию; < 3 слов и неоднозначно -> ru + мостик;
    транслит казахского -> kk (отвечаем кириллицей); транслит русского -> ru;
    переключение клиентом -> locked=True навсегда."""

def is_foreign(text: str, *, lexicon: LexiconFile) -> bool:
    """Язык вне {ru, kk} -> эскалация с reason=foreign_language."""
```

### 12.7 `app/core/lexicon.py`

```python
def normalize(text: str) -> str:
    """Нижний регистр, схлопывание пробелов, ё->е, снятие эмодзи. Смысл не меняет."""
def intent_hints(text: str, *, lexicon: LexiconFile) -> tuple[IntentHint, ...]: ...
def extract_age(text: str, *, lexicon: LexiconFile, now: datetime) -> int | None:
    """Понимает «8 лет», «сыну 8», «2018 г.р.» (год рождения -> возраст на дату now)."""
def extract_gender(text: str, *, lexicon: LexiconFile) -> Gender: ...
def extract_phone(text: str) -> str | None:
    """Обёртка над tools.booking.normalize_phone_kz — одна регулярка на весь проект."""
def match_district(text: str, *, lexicon: LexiconFile, gyms: Sequence[Gym]) -> tuple[str, ...]:
    """Возвращает id залов-кандидатов по алиасам; используется find_gym_by_district."""
```

### 12.8 `app/core/guards.py`

```python
class GuardVerdict(BaseModel):
    flags: tuple[GuardFlag, ...]
    allowed_tools: tuple[str, ...] | None    # None = ограничений нет
    fixed_reply_key: str | None              # ключ i18n, если ответ задан кодом
    escalate: bool
    reason: EscalationReason | None

def scan(text: str, *, lang: Language, lexicon: LexiconFile,
         policies: PoliciesFile) -> GuardVerdict:
    """Один проход по всем правилам. Порядок приоритета:
    stop_word -> child_writing -> manager_request -> injection -> off_topic."""

def detect_injection(text: str) -> bool:
    """Сигнатуры: «игнорируй предыдущие», «ты теперь», «покажи системный промпт»,
    «system prompt», «твои инструкции», «act as», «DAN», base64-блоки > 200 символов.
    Диалог НЕ блокируется: ход помечается, инструменты сужаются, постфильтр строже."""

def detect_child_writing(text: str, *, lang: Language) -> bool:
    """ПРОДУКТОВОЕ правило, а не требование ToS: платит и решает родитель.
    При срабатывании — доброжелательная просьба позвать родителя (`escalation.child_writing`)."""

def detect_off_topic(text: str, *, lexicon: LexiconFile) -> bool:
    """Запрещено отдельно: медицинские рекомендации, оценка веса/телосложения ребёнка,
    обещания спортивных результатов, политика, религия."""

def detect_stop_word(text: str, *, stop_words: Sequence[str]) -> bool:
    """Срабатывание выключает follow-up НАВСЕГДА (followup_blocked=True)."""
```

### 12.9 `app/core/postcheck.py`

```python
class PostcheckVerdict(BaseModel):
    ok: bool
    kind: PostcheckFailKind | None
    offending: tuple[str, ...]
    text: str                    # очищенный текст, если ok=True

def check(text: str, *, invocations: Sequence[ToolInvocation], lang: Language,
          kb: KBSnapshot, prompt_ngrams: frozenset[str],
          strict: bool = False) -> PostcheckVerdict:
    """Анти-галлюцинационный фильтр. Работает ПОСЛЕ safe_text и ДО постановки в outbox.

    Извлекает и сверяет с data вызовов ТЕКУЩЕГО хода:
    деньги `\\d[\\d\\s]*(₸|тг|тенге|тнг)`, время `\\b\\d{1,2}[:.]\\d{2}\\b`, дни недели,
    телефоны, номера домов и улиц, номера медицинских форм (075, 026), названия залов.
    Любое неподтверждённое значение -> ok=False: ответ НЕ отправляется, пишется
    postcheck_fail_total{kind}, клиенту уходит нейтральный текст из i18n, диалог эскалируется.

    strict=True (после guard-тревоги) дополнительно запрещает любые числа, кроме пришедших из data.
    """

def extract_numbers(text: str) -> tuple[str, ...]: ...
def extract_times(text: str) -> tuple[str, ...]: ...
def has_prompt_leak(text: str, ngrams: frozenset[str], *, n: int = 8) -> bool:
    """Пересечение >= 8 слов подряд с системным промптом -> блок ответа."""
def normalize_number(value: str) -> str:
    """`25 000 ₸`, `25000`, `25.000` -> `25000` для сравнения с data."""
```

**Особое правило G-1:** если `get_schedule` вернул `no_data`, любой ответ, содержащий время вида
`HH:MM`, блокируется независимо от прочих проверок.
## 13. `app/api/*`, `app/main.py`, `app/deps.py` (волна 3b)

Модуля `app/api/privacy.py` **нет**.

### 13.1 `app/main.py`

```python
def create_app(settings: Settings | None = None) -> FastAPI:
    """Фабрика приложения. Роутеры, middleware, lifespan. Сети при вызове нет."""

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Старт: configure_logging -> settings.require_startup() -> kb.load() (падение = отказ старта)
    -> build_engine -> state store -> LLM warmup -> JobQueue.startup()
    -> при INLINE_WORKER=true поднять inline-воркер
    -> при wazzup_register_webhook_on_start=true PATCH /v3/webhooks.
    Стоп: очередь, LLM, БД, http-клиенты."""

app: FastAPI    # module-level, для `uvicorn app.main:app`
```

**Блокеры старта — только технические:** невалидная KB и отсутствие обязательных настроек
подключения. Проверок текстов согласия, `policy_url` и БИН нет.

### 13.2 `app/deps.py`

```python
def get_settings_dep() -> Settings: ...
async def get_session() -> AsyncIterator[AsyncSession]: ...
def get_state() -> StateStore: ...
def get_queue() -> JobQueue: ...
def get_llm() -> LLMClient: ...
def get_kb() -> KBSnapshot: ...
def get_pipeline_deps() -> PipelineDeps: ...
async def require_admin(authorization: str | None = Header(default=None)) -> None:
    """Bearer admin_token. Не задан в настройках -> 404 на всю админку."""
```

### 13.3 `app/api/webhook_wazzup.py`

```python
router: APIRouter

@router.post("/wazzup/webhook/{secret}")
async def receive(secret: str, request: Request, ...) -> Response:
    """Делает ровно четыре вещи и ничего больше:

    1. hmac.compare_digest(secret, settings.wazzup_webhook_secret); несовпадение -> 404
       (не 403 — чтобы не подтверждать существование эндпоинта) + security-лог;
    2. `{"test": true}` -> 200 OK немедленно (иначе PATCH /v3/webhooks вернёт testPostNotPassed);
    3. Pydantic-валидация тела; невалидное -> 200 OK + drop + security-лог
       (ошибку отдавать нельзя: спровоцируем ретраи);
    4. queue.enqueue_inbound(payload) -> 200 OK.

    Целевое время ответа < 200 мс, потолок Wazzup — 30 с. Тяжёлой работы здесь нет.
    """
```

### 13.4 `app/api/health.py`

```python
@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Живость процесса. Ничего не проверяет, всегда 200. Это путь healthcheck Railway."""

@router.get("/readyz")
async def readyz(...) -> JSONResponse:
    """Готовность: БД, Redis (если state_backend=redis), загруженная KB, состояние канала.
    Не готово -> 503 с телом {"db": bool, "redis": bool, "kb": bool, "channels": bool}."""

@router.get("/metrics")
async def metrics() -> Response:
    """Prometheus. Отдаётся, только если metrics_enabled."""
```

### 13.5 `app/api/media.py`

```python
@router.get("/media/{token}")
async def get_media(token: str) -> FileResponse:
    """Отдаёт файл НАПРЯМУЮ: 200 + Content-Type, без единого редиректа — Wazzup скачивает
    контент сразу после запроса и редиректы не поддерживает.
    Просроченный или поддельный токен -> 404. Путь вне MEDIA_DIR -> 404."""
```

### 13.6 `app/api/admin.py`

Все ручки под `require_admin`.

```python
@router.post("/admin/kb/reload")
async def reload_kb(...) -> dict[str, str]:
    """Перечитывает KB. Успех -> {"old_hash", "new_hash"}; ошибка валидации -> 422
    с перечнем полей, старый снимок продолжает работать."""

@router.get("/admin/leads")
async def list_leads(limit: int = 50, status: LeadStatus | None = None, ...) -> list[dict[str, Any]]: ...

@router.post("/admin/pause")
async def pause(conv_key: str, minutes: int = 60, ...) -> dict[str, str]: ...

@router.post("/admin/resume")
async def resume(conv_key: str, ...) -> dict[str, str]: ...

@router.get("/admin/health/channels")
async def channels(...) -> list[ChannelState]: ...
```

## 14. `app/workers/*`, `app/notify/*`, `app/observability/metrics.py` (волна 3c)

Модулей `tasks_retention.py` и `observability/audit.py` **нет**.

### 14.1 `app/workers/queue.py`

```python
class ArqJobQueue(JobQueue):
    """Прод-реализация поверх Redis."""

class InlineJobQueue(JobQueue):
    """INLINE_WORKER=true: задачи исполняются в том же процессе, честно и до конца.

    Реализация: внутренняя asyncio.Queue + пул из worker_max_jobs воркеров, поднятый в lifespan.
    Заглушкой быть не имеет права: задачи реально выполняются, ошибки логируются,
    graceful shutdown дожидается текущих задач.
    """

def build_queue(settings: Settings, deps: PipelineDeps) -> JobQueue: ...

async def startup(ctx: dict[str, Any]) -> None: ...
async def shutdown(ctx: dict[str, Any]) -> None: ...

class WorkerSettings:
    """Точка входа arq: `arq app.workers.queue.WorkerSettings`."""
    functions = [process_inbound_job, send_outbox_job, send_followup_job]
    cron_jobs = [refresh_channels_cron, followup_sweep_cron]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs: int
    job_timeout: int
```

Cron-задачи: `refresh_channels_cron` (раз в 15 минут, `GET /v3/channels`, контроль `state == active`)
и `followup_sweep_cron` (раз в 10 минут, выбирает `followup_task` со сроком). Задачи `retention_sweep`
**нет**.

### 14.2 Задачи

```python
# app/workers/tasks_inbound.py
async def process_inbound_job(ctx: dict[str, Any], payload: dict[str, Any]) -> None:
    """Обёртка над core.pipeline.process_inbound: correlation_id, метрики, лог, clear_correlation."""

# app/workers/tasks_outbound.py
async def send_outbox_job(ctx: dict[str, Any], outbox_id: str) -> None:
    """Отправка одной строки outbox с тем же crmMessageId при каждом ретрае.

    429 и 5xx -> backoff 1/2/4/8 с + jitter, до wazzup_send_max_attempts, затем state=failed
    и эскалация. 400/401/403 не ретраятся. WazzupDuplicateError -> state=sent (сообщение уже ушло).
    MESSAGES_IS_SPAM -> стоп канала + тревога менеджеру. BAD_CONTACT -> пометить лид.
    CHANNEL_* -> общий алерт, бот молчит, входящие копятся.
    """

# app/workers/tasks_followup.py
async def send_followup_job(ctx: dict[str, Any], task_id: str) -> None:
    """Продуктовые правила (не юридические, согласия не требуется):
    лимит followup_max_attempts; стоп-слова блокируют навсегда; запрет ночью по Asia/Almaty;
    отправка только внутри окна мессенджера."""

async def schedule_followups(session: AsyncSession, conv: Conversation,
                             *, decision: PipelineDecision, policy: Sequence[FollowupRule]) -> list[UUID]: ...
async def cancel_followups(session: AsyncSession, conv_id: UUID, *, reason: str) -> int: ...
def is_quiet_hours(now: datetime, *, start_hour: int, end_hour: int, tz: str) -> bool: ...
def within_service_window(conv: Conversation, now: datetime) -> bool: ...
```

### 14.3 `app/notify/*`

```python
# app/notify/templates.py
def render_lead_card(lead: LeadDraft, *, kb: KBSnapshot, gym_title: str | None,
                     channel: ChannelKind, dialog_url: str | None, now: datetime) -> str:
    """Заголовок капсом одной строкой, максимум 12 строк, без markdown.
    Пустые поля НЕ печатаются вовсе. Телефон — отдельной строкой, начинается с `+7`,
    без скобок и дефисов, чтобы распознавался как tel:.
    Карточка всегда по-русски: её читает сотрудник; поле `Язык:` говорит, на каком языке звонить."""

def render_escalation_card(*, reason: EscalationReason, question: str, lang: Language,
                           phone: str | None, channel: ChannelKind,
                           dialog_url: str | None, now: datetime) -> str: ...
def render_alert(text: str, *, code: str) -> str: ...

# app/notify/manager.py
async def notify(services: ToolServices, card: ManagerCard) -> None:
    """Ставит карточку в outbox на канал менеджера (manager_notify_channel/target)."""
async def notify_alert(deps: PipelineDeps, text: str, *, code: str) -> None:
    """Технические тревоги: канал неисправен, спам-блок, KB не загрузилась, бюджет исчерпан."""
```

### 14.4 `app/observability/metrics.py`

```python
webhook_received_total: Counter          # labels: kind
webhook_dedup_total: Counter
inbound_processed_total: Counter         # labels: chat_type, action
llm_latency_seconds: Histogram           # labels: model
llm_tokens_total: Counter                # labels: kind (in|out|cached|thoughts)
llm_cost_usd_total: Counter              # labels: model
llm_errors_total: Counter                # labels: code
tool_calls_total: Counter                # labels: name, status
postcheck_fail_total: Counter            # labels: kind
kb_gap_hits_total: Counter               # labels: topic
kb_load_failures_total: Counter
wazzup_send_errors_total: Counter        # labels: code
outbox_pending: Gauge
pause_active_conversations: Gauge
echo_signal_mismatch_total: Counter
leads_created_total: Counter             # labels: status
escalations_total: Counter               # labels: reason
conversations_started_total: Counter     # labels: lang
followups_sent_total: Counter            # labels: kind

def observe_llm(usage: LLMUsage) -> None: ...
def observe_tool(invocation: ToolInvocation) -> None: ...
def observe_decision(decision: PipelineDecision) -> None: ...
def render_latest() -> tuple[bytes, str]:
    """(payload, content_type) для GET /metrics."""
```

Метрика `kb_gap_hits_total{topic}` — прямой бэклог владельцу школы: топ-10 вопросов без ответа.
## 15. Контракт исключений

Все классы объявлены в `app/types.py` (§3). Отдельного `app/errors.py` нет.

| Исключение | Кто кидает | Кто ловит | Что делает ловящий |
|---|---|---|---|
| `ConfigError` | `Settings.require_startup` | `app/main.py` lifespan | процесс не стартует, лог `startup_blocked` |
| `KBValidationError` | `app/kb/loader.load` | lifespan (старт) / `api/admin.reload_kb` | старт: отказ; reload: HTTP 422, старый снимок жив, `kb_load_failures_total` |
| `KBNotLoadedError` | `app/kb/loader.get_snapshot` | `app/deps.get_kb` | 503 в `/readyz`, задача воркера откладывается |
| `WebhookValidationError` | `app/channels/wazzup_schemas` через `api/webhook_wazzup` | сам хендлер вебхука | **200 OK** + drop + security-лог (ошибка спровоцировала бы ретраи) |
| `WazzupRateLimitError`, `WazzupServerError` | `WazzupClient` | `tasks_outbound.send_outbox_job` | backoff 1/2/4/8 с + jitter, до `wazzup_send_max_attempts` |
| `WazzupDuplicateError` | `WazzupClient` | `tasks_outbound.send_outbox_job` | **успех**: `state=sent`, метрика не растёт |
| `WazzupSpamError` | `WazzupClient` | `tasks_outbound` | стоп канала, `notify_alert`, лид не теряется |
| `WazzupBadContactError` | `WazzupClient` | `tasks_outbound` | `state=failed`, пометка лида, без ретраев |
| `WazzupChannelError` | `WazzupClient` | `tasks_outbound`, `refresh_channels_cron` | общий алерт, бот молчит, входящие копятся |
| `WazzupError` (базовый) | `WazzupClient` | `tasks_outbound` | `state=failed`, `last_error`, эскалация |
| `LLMRateLimitError`, `LLMTimeoutError` | `GeminiClient` | `GeminiClient` (внутренний retry SDK), затем `core/pipeline` | фолбэк-модель; при повторе — эскалация `llm_failure` |
| `LLMQuotaError` | `GeminiClient` | `core/pipeline` | режим «только KB + эскалация» до сброса квоты, алерт |
| `LLMBlockedError` | `safe_text` / `GeminiClient` | `core/pipeline` | нейтральный текст из i18n + `escalate_to_manager(llm_failure)` + пауза 15 мин |
| `LLMToolLoopError` | `tool_runner.run_tool_loop` | `core/pipeline` | эскалация `repeated_miss`, ответ-заглушка |
| `BudgetExceededError` | `llm/usage` бюджет-гард | `core/pipeline` | пауза `budget_guard` до конца суток, алерт менеджеру |
| `ToolExecutionError` | реализация инструмента | `tools/registry.dispatch` | превращается в `ToolResult.failure`, наружу не выходит |
| `PostcheckFailedError` | `core/postcheck` (или возврат `PostcheckVerdict`) | `core/pipeline` | ответ **не отправляется**, эскалация, пауза 30 мин, `postcheck_fail_total` |
| `StorageError` | репозитории | `core/pipeline`, воркеры | rollback, задача помечается неуспешной, ретрай очереди |

**Три правила без исключений:**

1. Ни одно исключение не долетает до хендлера вебхука — он всегда отвечает `200 OK`
   (кроме несовпадения секрета: `404`).
2. `process_inbound` не выпускает исключений наружу: любая ошибка становится
   `PipelineDecision(action=ESCALATE|DROP)`.
3. Инструменты не кидают исключений ради бизнес-исхода. `no_data`, `needs_operator`,
   `invalid_input` — это `ToolResult`, а не `raise`.

## 16. Именование

### 16.1 Ключи `kb/i18n.yaml`

Формат: `<группа>.<slug>`, `^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$`. Разрешённые группы — закрытый список:

| Группа | Назначение | Примеры |
|---|---|---|
| `gap` | заглушка «данных нет»; ключ соответствует `GapRef` через `GAP_TO_I18N` | `gap.schedule`, `gap.contacts`, `gap.region_address`, `gap.trial_conditions`, `gap.docs`, `gap.gear`, `gap.payment`, `gap.coaches`, `gap.kzhbi` |
| `escalation` | передача администратору | `escalation.handoff`, `escalation.child_writing` |
| `error` | технические сбои | `error.generic`, `error.voice_message`, `error.too_many_messages` |
| `bridge` | языковой мостик | `bridge.kk_offer` |
| `followup` | тексты напоминаний | `followup.fu_soft`, `followup.fu_value` |
| `lead_card` | карточки администратору | `lead_card.trial_booked`, `lead_card.escalation`, `lead_card.thinking` |
| `greeting` | приветствия | `greeting.first`, `greeting.salam` |
| `system` | служебные подтверждения клиенту | `system.artifact_sent`, `system.lead_saved` |

Группы `consent` **нет** — блок текстов согласия из `i18n.yaml` удалён.

Правила: плейсхолдеры `{snake_case}` в фигурных скобках, множество плейсхолдеров в `ru` и `kk`
обязано совпадать; длина ≤ 900 знаков; markdown и эмодзи запрещены; пустая строка — ошибка валидации.
Ключ, используемый кодом, обязан существовать в KB — иначе `KBValidationError` на загрузке, а не в рантайме.

### 16.2 Идентификаторы контента

| Сущность | Правило | Примеры |
|---|---|---|
| `gyms[].id` | `^[a-z0-9_]+$`, транслитерация улицы/ориентира, **никогда не меняется** | `ksk_kairbekova_334` |
| `artifacts[].id` | `^[a-z0-9_]+$`, префикс по назначению | см. ниже |
| `faq[].id` | `^[a-z0-9_]+$`, `<topic>_<уточнение>` | `trial_conditions`, `docs_medical` |
| `derived.facts[].id` | `^[a-z0-9_]+$` | `subscription_vs_single` |

Префиксы артефактов зафиксированы: `price_card_{city|region}`, `price_photo_{city|region}`,
`gyms_list_{city|region}`, `gym_location_<gym_id>` (суффикс обязан совпадать с существующим
`gyms[].id`), `schedule_<gym_id>`, `payment_details`, `instagram_link`, `offer_and_policy`.
Артефакты `schedule_*` и `payment_details` поставляются с `enabled: false` (G-1, G-9);
`offer_and_policy` допустим, но обязательным больше не является — consent gate отменён.

Переименование любого `id` = потеря ссылок из `Lead.gym_id`, `media.yaml` и метрик. Запрещено:
запись помечается `active: false` / `enabled: false`, но `id` живёт вечно.

### 16.3 Имена метрик и логов

Метрики — `snake_case`, счётчики оканчиваются на `_total`, гистограммы — на `_seconds`.
События логов — `snake_case` глагол в прошедшем времени или существительное состояния:
`webhook_received`, `dedup_hit`, `operator_detected`, `llm_call_done`, `postcheck_fail`,
`outbox_sent`, `kb_switch`, `startup_blocked`.

## 17. `tests/*` (волна 4)

```
tests/conftest.py           фикстуры: settings_override, sqlite+aiosqlite in-memory,
                            MemoryStateStore, FakeLLMClient, FakeToolServices, kb_snapshot
tests/test_config.py        нормализация DATABASE_URL (таблица §4.1), PORT, INLINE_WORKER
tests/test_types.py         инварианты ToolResult, LeadDraft.merge, OutboundMessage-валидатор
tests/test_logging_mask.py  маскирование телефонов и имён, отсутствие секретов в логах
tests/test_kb_loader.py     валидация, kb_hash детерминирован, отказ при пустой строке
tests/test_kb_render.py     префикс байт-в-байт стабилен, нет internal_note и телефонов
tests/test_pricing.py       100% ветвей calculate_price (город/район, 1..5 детей, C-4, C-5, G-10)
tests/test_language.py      правила §6 ARCHITECTURE, включая транслит и code-switching
tests/test_guards.py        injection, off-topic, ребёнок за клавиатурой, стоп-слова
tests/test_postcheck.py     деньги, время, телефоны, утечка промпта, правило G-1
tests/test_wazzup_schemas.py  контрактные: примеры payload из research-wazzup24 §5.3 парсятся
tests/test_wazzup_client.py   respx: 201, 400 repeatedCrmMessageId в двух регистрах, 429, 5xx
tests/test_outbound.py      сплит, sanitize, лимиты канала, backoff
tests/test_tool_runner.py   параллельный tool-call: function_response.id == function_call.id
tests/test_pipeline.py      e2e на фейках: дедуп, эхо оператора, пауза, дебаунс, эскалация
tests/test_layering.py      статическая проверка правила зависимостей §1.1
tests/test_imports.py       каждый модуль импортируется при пустом окружении
```

Обязательные к покрытию инварианты: `ToolResult.ok is (status is OK)`; `clearUnanswered` всегда
`false`; повторный `create_trial_lead` не плодит лидов; `postcheck` блокирует `HH:MM` при
`get_schedule = no_data`; нормализованное сравнение кодов ошибок Wazzup.

## 18. Что решено этим документом (сводка)

| # | Вопрос, недосказанный в `ARCHITECTURE.md` | Решение |
|---|---|---|
| 1 | Где живут исключения | `app/types.py`; `app/errors.py` не создаётся |
| 2 | Как LLM-слой узнаёт об инструментах | `Sequence[ToolSpec]` + `ToolExecutor`; `app.llm` не импортирует `app.tools` |
| 3 | Формат истории вне LLM-слоя | `list[dict]` — JSON-дампы `types.Content`; `google.genai` не выходит за `app/llm` |
| 4 | Где хранятся JSON-схемы инструментов | `app/tools/registry.py::RAW_TOOL_SPECS`, enum'ы подставляются из KB |
| 5 | Имя таблицы залов | `gym` (единственное число), `district_aliases` — JSONB ради SQLite |
| 6 | Кодек токена `/media/{token}` | `app/tools/content.py`; `api` импортирует `tools`, не наоборот |
| 7 | Нормализация телефона | одна функция `tools/booking.normalize_phone_kz`, её переиспользует `core/lexicon` |
| 8 | Enum причин эскалации | внутренний `EscalationReason` шире; в схему инструмента идут только `TOOL_ESCALATION_REASONS` (9 значений) |
| 9 | `INLINE_WORKER` | честная реализация `InlineJobQueue` с пулом задач и graceful shutdown, не заглушка |
| 10 | Alembic без `psycopg2` | `migrations/env.py` на async-движке |
| 11 | Ретеншн, согласия, шифрование, аудит | отсутствуют полностью: нет таблиц, колонок, настроек, модулей и ключей i18n |
| 12 | `sslmode` в `DATABASE_URL` | вырезается автоматически — asyncpg этот параметр не понимает |
