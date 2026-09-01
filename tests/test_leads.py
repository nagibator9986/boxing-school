"""Заявка появляется без модели — и доходит до вкладки «Заявки» в CRM.

Владелец: «во вкладку заявки ничего не приходит». Так и было: заявку создавали
только два пути, и оба идут через Gemini — инструмент ``create_trial_lead`` и
фоновый разбор переписки ``extract_lead``. Пока модель отвечает, этого хватало.
Когда она молчит, вкладка оставалась пустой, хотя клиенты писали и оставляли
телефоны прямо в переписке.

Здесь проверяется весь путь целиком: настоящий ``process_inbound`` с мёртвой
моделью пишет строку в базу, а читает её тот же код, которым читает CRM
(:class:`crm.botdb.BotData`), — не запрос, переписанный в тесте.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from app.core import pause
from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.storage import db as storage_db
from app.storage.models import Base, Conversation
from app.types import LLMQuotaError
from crm.botdb import BotData
from tests.conftest import RecordingQueue, webhook_payload

CHAT_ID = "77015551234"


class DeadLLM:
    """Модель, которой нет: ровно та ошибка, что пришла с боевого ключа."""

    _ERROR = "квота Gemini исчерпана: Your prepayment credits are depleted."

    async def generate(self, req, executor):  # type: ignore[no-untyped-def]
        raise LLMQuotaError(self._ERROR)

    async def extract_lead(self, rendered, *, lang):  # type: ignore[no-untyped-def]
        raise LLMQuotaError(self._ERROR)


@pytest.fixture
async def bot_db(tmp_path: Path) -> Path:
    """База диалогов файлом: CRM читает её тем же путём, что и в бою."""
    return tmp_path / "bot.db"


@pytest.fixture
async def deps(kb, state, settings, bot_db: Path) -> PipelineDeps:
    """Пайплайн с мёртвой моделью поверх файловой базы."""
    kb_loader.swap(kb)
    engine = storage_db.build_engine(f"sqlite+aiosqlite:///{bot_db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = storage_db.build_sessionmaker(engine)
    try:
        yield PipelineDeps(
            sessionmaker=maker,
            state=state,
            llm=DeadLLM(),
            kb=kb_loader.get_snapshot,
            queue=RecordingQueue(),
            settings=settings,
        )
    finally:
        await engine.dispose()


def crm(bot_db: Path) -> BotData:
    """Тот самый объект, которым CRM читает базу бота."""
    return BotData(bot_db)


# --------------------------------------------------------------------------- #
# Заявка без модели
# --------------------------------------------------------------------------- #
async def test_escalation_creates_a_lead_visible_in_crm(deps, bot_db: Path) -> None:
    """Клиент написал, модель молчит — заявка всё равно есть, с телефоном."""
    await process_inbound(
        deps,
        webhook_payload("wz-lead-1", "Хочу записать сына на бокс", chat_id=CHAT_ID),
    )

    leads = crm(bot_db).leads()
    assert len(leads) == 1
    assert leads[0].phone == f"+{CHAT_ID}"
    assert leads[0].status == "escalated"
    assert leads[0].channel == "whatsapp"


async def test_lead_carries_age_read_from_the_message(deps, bot_db: Path) -> None:
    """Возраст ребёнка разбирается регулярками — модель для этого не нужна."""
    await process_inbound(
        deps,
        webhook_payload("wz-lead-2", "Сыну 9 лет, хотим на бокс", chat_id=CHAT_ID),
    )

    leads = crm(bot_db).leads()
    assert len(leads) == 1
    assert leads[0].child_age == 9


async def test_second_message_does_not_duplicate_the_lead(deps, bot_db: Path) -> None:
    """Одна заявка на диалог: ``upsert`` обновляет, а не плодит строки."""
    for i, text in enumerate(("Здравствуйте", "Сыну 9 лет"), start=1):
        await process_inbound(deps, webhook_payload(f"wz-dup-{i}", text, chat_id=CHAT_ID))

    leads = crm(bot_db).leads()
    assert len(leads) == 1
    assert leads[0].child_age == 9


async def test_escalation_does_not_undo_a_booked_trial(deps, bot_db: Path) -> None:
    """«Записан на пробное» — результат работы, эскалация его не отменяет.

    Иначе один сбой модели после записи превращал бы оформленную заявку в
    «передан администратору», и владелец терял бы из вида пробное занятие.
    """
    # Голое приветствие отвечается шаблоном без модели и заявки не создаёт —
    # нужен вопрос, который доходит до модели и потому эскалируется.
    await process_inbound(deps, webhook_payload("wz-book-1", "Сколько стоит?", chat_id=CHAT_ID))

    engine = sa.create_engine(f"sqlite:///{bot_db}")
    with engine.begin() as conn:
        booked = conn.execute(
            sa.text("UPDATE lead SET status = 'trial_booked', parent_name = 'Айгуль'")
        ).rowcount
    engine.dispose()
    assert booked == 1, "первый ход не создал заявку — проверять нечего"

    # После эскалации бот на паузе, и второй ход молчал бы, не дойдя до заявки.
    # Пауза снимается так же, как её снимает таймаут в бою.
    async with deps.sessionmaker() as db:
        conv = (await db.execute(sa.select(Conversation))).scalars().one()
        await pause.resume(deps.state, db, conv.id, conv.conv_key, by="timeout")
        await db.commit()

    second = await process_inbound(
        deps, webhook_payload("wz-book-2", "А во сколько?", chat_id=CHAT_ID)
    )
    assert [d.action.value for d in second] == ["escalate"], "ход не дошёл до эскалации"

    leads = crm(bot_db).leads()
    assert len(leads) == 1
    assert leads[0].status == "trial_booked"
    assert leads[0].parent_name == "Айгуль", "имя из разбора модели затёрто именем профиля"


async def test_no_lead_without_anything_to_call_back(deps, bot_db: Path) -> None:
    """Ни телефона, ни возраста, ни имени — пустая карточка в списке не нужна.

    Telegram-чат без телефона и без имени профиля: перезвонить по такой заявке
    нельзя, а строка в списке создаёт видимость работы.
    """
    payload = webhook_payload("tg-1", "Здравствуйте", chat_id="tg-777", chat_type="telegram")
    for message in payload["messages"]:
        message["contact"] = {}
        message.pop("contactName", None)
        message.pop("contactPhone", None)

    await process_inbound(deps, payload)

    assert crm(bot_db).leads() == []


async def test_broken_lead_write_does_not_lose_the_answer(deps, bot_db: Path, monkeypatch) -> None:
    """Заявка не важнее ответа клиенту.

    Сохранение лида идёт в той же транзакции, что и весь ход. Упавший запрос
    оставляет сессию SQLAlchemy в состоянии «нужен откат», и следующий коммит
    срывает ход целиком: клиент не получает ни карточки из базы знаний, ни
    честной заглушки, а администратор — карточки эскалации.
    """
    from app.storage import repo_lead

    called: list[str] = []

    async def boom(session, draft):  # type: ignore[no-untyped-def]
        called.append("да")
        await session.execute(sa.text("INSERT INTO lead (id) VALUES ('битая-строка')"))
        raise AssertionError("сюда не дойдём: запрос уже упал")

    monkeypatch.setattr(repo_lead, "upsert", boom)

    decisions = await process_inbound(
        deps, webhook_payload("wz-boom-1", "Сколько стоит?", chat_id=CHAT_ID)
    )

    assert called, "подмена не сработала — тест ничего не проверил"
    assert [d.action.value for d in decisions] == ["escalate"], "ход сорвался из-за заявки"

    # Решение в памяти ещё ничего не доказывает: важно, что коммит хода уцелел
    # и строка ответа действительно легла в очередь отправки.
    engine = sa.create_engine(f"sqlite:///{bot_db}")
    with engine.begin() as conn:
        queued = conn.execute(sa.text("SELECT COUNT(*) FROM outbox_message")).scalar_one()
    engine.dispose()
    assert queued >= 1, "ответ клиенту не сохранился: транзакция хода не закоммитилась"
