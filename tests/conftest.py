"""Общие фикстуры набора тестов.

Всё окружение выставляется ДО первого импорта ``app.config``: ``Settings``
читает переменные в момент создания, и переставить их потом уже нельзя.

Сети в тестах нет вообще: LLM — :class:`app.llm.client.FakeLLMClient`, состояние —
``MemoryStateStore``, база — sqlite в памяти, Wazzup — заглушка. Ни один тест не
имеет права требовать API-ключ.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Окружение — до импорта app.config.
os.environ.update(
    APP_ENV="local",
    DATABASE_URL="sqlite+aiosqlite:///:memory:",
    STATE_BACKEND="memory",
    REDIS_URL="",
    INLINE_WORKER="false",
    GEMINI_API_KEY="",
    WAZZUP_API_KEY="test-api-key",
    WAZZUP_WEBHOOK_SECRET="test-webhook-secret-0123456789ab",
    LOG_LEVEL="WARNING",
    LOG_JSON="false",
    KB_DIR=str(ROOT / "kb"),
    MEDIA_DIR=str(ROOT / "media"),
    METRICS_ENABLED="false",
    DEBOUNCE_SECONDS="0",
    DEBOUNCE_MAX_SECONDS="0",
)

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.kb.loader import load_sync  # noqa: E402
from app.kb.models import KBSnapshot  # noqa: E402
from app.storage import db as storage_db  # noqa: E402
from app.storage.models import Base  # noqa: E402
from app.storage.state import MemoryStateStore  # noqa: E402
from app.types import (  # noqa: E402
    ChannelKind,
    Language,
    LeadDraft,
    ManagerCard,
    OutboundMessage,
    PauseReason,
    ToolContext,
    ToolInvocation,
    ToolResult,
)

#: Канал и чат, общие для всех синтетических вебхуков.
CHANNEL_ID = "11111111-1111-1111-1111-111111111111"


# --------------------------------------------------------------------------- #
# База знаний
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def kb() -> KBSnapshot:
    """Настоящий снимок ``kb/``: тесты обязаны ловить расхождение кода и данных."""
    snapshot, _ = load_sync(ROOT / "kb", media_dir=ROOT / "media", schema_version=1)
    return snapshot


#: Залы, удалённые из базы 13.10.2026 (Рахат и «Кеме»), — записи как были, без видео.
CLOSED_GYMS_1210: Final[Path] = ROOT / "tests" / "fixtures" / "closed_gyms_1210.yaml"

#: Утренние занятия «Романтика» (Вт, Чт, Сб 09:00), снятые владельцем 13.10.2026.
_ROMANTIK_NO_MORNINGS: Final[str] = (
    "  # Утренних тренировок (Вт, Чт, Сб 09:00) здесь нет — владелец, 13.10.2026.\n"
)
_ROMANTIK_MORNINGS: Final[str] = "".join(
    f"  - discipline: {discipline}\n    age_from: null\n    age_to: null\n    days:\n"
    "    - tue\n    - thu\n    - sat\n    time_start: \"09:00\"\n    time_end: \"10:30\"\n"
    "    shift: second\n    note: null\n"
    for discipline in ("kickboxing", "boxing")
)


def _kb_variant(tmp_path_factory: pytest.TempPathFactory, name: str, edit) -> KBSnapshot:
    """Копия базы, в которой ``edit`` поправил ``gyms.yaml``. Рабочая база не трогается."""
    target = tmp_path_factory.mktemp(name) / "kb"
    shutil.copytree(ROOT / "kb", target, ignore=shutil.ignore_patterns(".*"))
    gyms = target / "gyms.yaml"
    before = gyms.read_text(encoding="utf-8")
    after = edit(before)
    assert after != before, f"{name}: правка базы ничего не изменила — устарел якорь"
    gyms.write_text(after, encoding="utf-8")
    snapshot, _ = load_sync(target, media_dir=ROOT / "media", schema_version=1)
    return snapshot


@pytest.fixture(scope="session")
def kb_with_closed_gyms(tmp_path_factory: pytest.TempPathFactory) -> KBSnapshot:
    """Копия базы, где залы, закрытые 12.10.2026 и удалённые 13.10.2026, снова работают.

    Для тестов логики записи, построенных на их расписании: у Рахата бокс и
    кикбоксинг Пн·Ср·Пт в 09:00 и 19:00, у «Кеме» — одно время. Такой формы
    расписания у действующих залов нет, а проверяют эти тесты согласие, выбор и
    время, а не состав залов — его проверяют тесты на настоящей базе (``kb``).
    """
    reopened = CLOSED_GYMS_1210.read_text(encoding="utf-8").replace(
        "active: false\n  status: closed\n", "active: true\n  status: open\n"
    )
    return _kb_variant(tmp_path_factory, "kb_reopened", lambda text: text + reopened)


@pytest.fixture(scope="session")
def kb_with_closed_gym_records(tmp_path_factory: pytest.TempPathFactory) -> KBSnapshot:
    """Копия базы, где Рахат и «Кеме» есть, но закрыты (``status: closed``).

    Ответ «этот зал больше не работает» остаётся на будущие закрытия: проверяется он
    здесь, а в настоящей базе закрытых записей с 13.10.2026 нет.
    """
    closed = CLOSED_GYMS_1210.read_text(encoding="utf-8")
    return _kb_variant(tmp_path_factory, "kb_closed_records", lambda text: text + closed)


#: Расписание Плазы до 13.10.2026 — на месте нового, от комментария владельца до видео зала.
PLAZA_SCHEDULE_1210: Final[Path] = ROOT / "tests" / "fixtures" / "plaza_schedule_1210.yaml"
_PLAZA_1310_HEAD: Final[str] = "  # Владелец 13.10.2026: «на Плазе Аймед тренировки только по графику"
_PLAZA_MEDIA: Final[str] = "  capacity_note: null\n  media:\n  - gym_location_plaza_szm_70\n"


def _schedule_1210(text: str) -> str:
    """Расписание залов, каким оно было 12.10.2026: утро «Романтика» и прежняя Плаза."""
    text = text.replace(_ROMANTIK_NO_MORNINGS, _ROMANTIK_MORNINGS)
    start, end = text.index(_PLAZA_1310_HEAD), text.index(_PLAZA_MEDIA)
    plaza = "".join(
        line for line in PLAZA_SCHEDULE_1210.read_text(encoding="utf-8").splitlines(keepends=True)
        if not line.startswith("#")
    )
    return text[:start] + plaza + text[end:]


@pytest.fixture(scope="session")
def kb_schedule_1210(tmp_path_factory: pytest.TempPathFactory) -> KBSnapshot:
    """Копия базы с расписанием до 13.10.2026: владелец снял утро «Романтика» и сократил Плазу.

    На прежнем расписании построены тесты выбора из нескольких вариантов: у секции два
    времени (Романтик 09:00 и 18:45), в 09:00 — и бокс, и кикбоксинг, а у Плазы один час
    (19:00) идёт и в Пн/Ср/Пт, и во Вт/Чт/Сб. Такой формы у нынешних залов нет, а
    проверяют эти тесты выбор, а не расписание — его проверяют тесты на настоящей базе.
    """
    return _kb_variant(tmp_path_factory, "kb_schedule_1210", _schedule_1210)


@pytest.fixture(scope="session")
def kb_dir() -> Path:
    """Путь к каталогу базы знаний."""
    return ROOT / "kb"


@pytest.fixture(scope="session")
def media_dir() -> Path:
    """Путь к каталогу медиа."""
    return ROOT / "media"


# --------------------------------------------------------------------------- #
# Заглушки побочных эффектов
# --------------------------------------------------------------------------- #
class RecordingServices:
    """Фейковая реализация ``app.types.ToolServices``: всё пишется в списки."""

    def __init__(self) -> None:
        self.outbound: list[OutboundMessage] = []
        self.leads: list[LeadDraft] = []
        self.cards: list[ManagerCard] = []
        self.pauses: list[tuple[str, int, PauseReason]] = []
        self.artifact_sends: dict[str, int] = {}
        self.escalated_recently = False

    async def enqueue_outbound(self, message: OutboundMessage) -> UUID:
        """Записывает исходящее и возвращает синтетический id."""
        self.outbound.append(message)
        return uuid4()

    async def upsert_lead(self, draft: LeadDraft) -> UUID:
        """Записывает черновик лида."""
        self.leads.append(draft)
        return uuid4()

    async def notify_manager(self, card: ManagerCard) -> None:
        """Записывает карточку менеджеру."""
        self.cards.append(card)

    async def set_pause(self, conv_key: str, *, minutes: int, reason: PauseReason) -> None:
        """Записывает паузу бота."""
        self.pauses.append((conv_key, minutes, reason))

    async def count_artifact_sends(self, conversation_id: UUID, artifact_id: str) -> int:
        """Сколько раз артефакт уже уходил в диалоге."""
        return self.artifact_sends.get(artifact_id, 0)

    async def escalation_open(self, *, within_minutes: int) -> bool:
        """Передан ли вопрос администратору недавно — задаётся тестом."""
        return self.escalated_recently


class RecordingQueue:
    """Очередь-регистратор вместо Redis/ARQ: в тестах сеть недопустима."""

    def __init__(self) -> None:
        self.inbound: list[dict[str, Any]] = []
        self.outbox: list[tuple[UUID, int]] = []
        self.followups: list[tuple[UUID, datetime]] = []
        self.fail_inbound: bool = False

    async def enqueue_inbound(self, payload: dict[str, Any]) -> str:
        """Регистрирует входящий payload; ``fail_inbound`` имитирует отказ очереди."""
        if self.fail_inbound:
            raise RuntimeError("очередь недоступна")
        self.inbound.append(payload)
        return str(uuid4())

    async def enqueue_outbox(self, outbox_id: UUID, *, delay_ms: int = 0) -> str:
        """Регистрирует отправку строки outbox."""
        self.outbox.append((outbox_id, delay_ms))
        return str(uuid4())

    async def enqueue_followup(self, task_id: UUID, *, run_at: datetime) -> str:
        """Регистрирует запланированный follow-up."""
        self.followups.append((task_id, run_at))
        return str(uuid4())

    async def startup(self) -> None:
        """Ничего не открывает."""

    async def shutdown(self) -> None:
        """Ничего не закрывает."""


# --------------------------------------------------------------------------- #
# Контекст инструментов
# --------------------------------------------------------------------------- #
@pytest.fixture
def services() -> RecordingServices:
    """Свежая заглушка побочных эффектов на каждый тест."""
    return RecordingServices()


@pytest.fixture
def make_ctx(kb: KBSnapshot, services: RecordingServices) -> Callable[..., ToolContext]:
    """Фабрика :class:`app.types.ToolContext` для тестов инструментов."""

    def _make(
        *,
        lang: Language = Language.RU,
        snapshot: KBSnapshot | None = None,
        conv_key: str = "conv:test",
        **extra: Any,
    ) -> ToolContext:
        chosen = snapshot or kb
        return ToolContext(
            conversation_id=uuid4(),
            conv_key=conv_key,
            channel=ChannelKind.WHATSAPP,
            channel_id=CHANNEL_ID,
            chat_id="77010000001",
            lang=lang,
            kb=chosen,
            kb_hash=chosen.kb_hash,
            now=datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc),
            correlation_id="test-correlation",
            services=services,
            **extra,
        )

    return _make


@pytest.fixture
def ctx(make_ctx: Callable[..., ToolContext]) -> ToolContext:
    """Готовый русскоязычный контекст."""
    return make_ctx()


# --------------------------------------------------------------------------- #
# Хранилище и состояние
# --------------------------------------------------------------------------- #
@pytest.fixture
def state() -> MemoryStateStore:
    """Чистое состояние на каждый тест."""
    return MemoryStateStore()


@pytest.fixture
async def sessionmaker():
    """sqlite в памяти со схемой, накатанной моделями (alembic здесь не нужен)."""
    engine = storage_db.build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = storage_db.build_sessionmaker(engine)
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest.fixture
def settings():
    """Настройки процесса (кэшированный синглтон)."""
    return get_settings()


# --------------------------------------------------------------------------- #
# Помощники для пост-фильтра
# --------------------------------------------------------------------------- #
def invocation(
    name: str,
    data: dict[str, Any] | None = None,
    *,
    result: ToolResult | None = None,
) -> ToolInvocation:
    """Синтетический факт вызова инструмента для :func:`app.core.postcheck.check`."""
    return ToolInvocation(
        call_id="call-1",
        name=name,
        args={},
        result=result if result is not None else ToolResult.success(data=data or {}),
        latency_ms=1,
        loop_index=0,
    )


def webhook_payload(
    message_id: str,
    text: str,
    *,
    is_echo: bool = False,
    chat_id: str = "77010000001",
    chat_type: str = "whatsapp",
    status: str | None = None,
    sent_from_app: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Синтетический вебхук Wazzup с одним сообщением."""
    message: dict[str, Any] = {
        "messageId": message_id,
        "channelId": CHANNEL_ID,
        "chatType": chat_type,
        "chatId": chat_id,
        "dateTime": "2026-08-10T09:00:00.000Z",
        "type": "text",
        "isEcho": is_echo,
        "status": status if status is not None else ("sent" if is_echo else "inbound"),
        "text": text,
        "contact": {"name": "Айгуль", "avatarUri": None},
    }
    if sent_from_app is not None:
        message["sentFromApp"] = sent_from_app
    if extra:
        message.update(extra)
    return {"messages": [message]}
