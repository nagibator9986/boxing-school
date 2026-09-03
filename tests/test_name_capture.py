"""Имя, названное клиентом, второй раз не спрашивают.

Живой прогон 20 диалогов 03.09.2026: на «как зовут ребёнка» клиент ответил
«Асель, 87015551122» — и получил в ответ «как зовут сына?». Телефон и возраст
из реплики разбирались, имя — нет, поэтому служебная заметка «клиент только что
назвал» про имя молчала.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.lexicon import extract_name
from app.core.pipeline import _just_said
from app.kb.models import KBSnapshot

UTC = timezone.utc
NOW = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Асель, 87015551122", "Асель"),
        ("Айгерим", "Айгерим"),
        ("Данияр 8 лет", "Данияр"),
        ("Зовут Мадина", "Мадина"),
    ],
)
def test_name_is_recognised(text: str, expected: str) -> None:
    assert extract_name(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Здравствуйте",
        "Мы в Тобыле",
        "Мы из Костаная",
        "Ему 8 лет",
        "Центр удобнее",
        "Каирбекова 24",
        "Хочу записать сына",
        "Здравствуйте, хочу записать ребёнка на пробное занятие в центре",
    ],
)
def test_not_a_name(text: str) -> None:
    """Город в падеже, местоимение и длинный рассказ именем не считаются."""
    assert extract_name(text) is None


def test_pipeline_note_lists_the_name_with_the_phone(kb: KBSnapshot) -> None:
    """Заметка модели говорит про имя и телефон разом — это одна реплика клиента."""
    said = _just_said("Асель, 87015551122", kb=kb, now=NOW)

    assert "телефон" in said
    assert any(item.startswith("имя — Асель") for item in said), said


# --------------------------------------------------------------------------- #
# Разговор с ребёнком не забывается на второй реплике
# --------------------------------------------------------------------------- #
CHILD_REPLY = (
    "Классно, что хочешь заниматься. Для записи нужно согласие родителей — "
    "покажи это сообщение маме или папе, пусть напишут сюда."
)


async def _conv_with_bot_reply(sessionmaker, reply: str):
    """Диалог, в котором бот уже отправил клиенту ``reply``."""
    import sqlalchemy as sa

    from app.storage import repo_message
    from app.storage.models import Conversation
    from app.types import ChannelKind, Language, OutboundMessage

    async with sessionmaker() as db:
        conv = Conversation(
            conv_key="conv:wa:child", channel_id="wa", chat_type="whatsapp", chat_id="7701",
        )
        db.add(conv)
        await db.flush()
        await repo_message.add_outbound(
            db,
            conv.id,
            OutboundMessage(
                conversation_id=conv.id,
                channel_id="wa",
                channel=ChannelKind.WHATSAPP,
                chat_id="7701",
                lang=Language.RU,
                text=reply,
            ),
            wazzup_message_id="wz-1",
        )
        await db.commit()
        return (await db.execute(sa.select(Conversation))).scalars().one()


async def test_child_talk_survives_the_next_message(sessionmaker) -> None:
    """«Родители не знают пока» — это по-прежнему ребёнок за клавиатурой.

    Живой прогон: девятилетний написал сам, бот попросил позвать родителей, а на
    следующей реплике забыл, с кем говорит, и продолжил собирать данные для
    записи. Просьбу отправляет guard, минуя модель, поэтому в её истории следа
    нет — признак приходится искать в отправленных сообщениях.
    """
    from app.core.pipeline import _child_talk_continues

    conv = await _conv_with_bot_reply(sessionmaker, CHILD_REPLY)
    async with sessionmaker() as db:
        assert await _child_talk_continues(db, conv, "Родители не знают пока")


async def test_parent_at_the_keyboard_returns_the_normal_talk(sessionmaker) -> None:
    """Взрослый подошёл — разговор снова обычный, иначе мы отказываем родителю."""
    from app.core.pipeline import _child_talk_continues

    conv = await _conv_with_bot_reply(sessionmaker, CHILD_REPLY)
    async with sessionmaker() as db:
        assert not await _child_talk_continues(db, conv, "Это мама, хочу записать сына")


async def test_ordinary_dialogue_is_not_treated_as_child(sessionmaker) -> None:
    """Без такой просьбы в переписке признака нет — взрослых не подозреваем."""
    from app.core.pipeline import _child_talk_continues

    conv = await _conv_with_bot_reply(sessionmaker, "Здравствуйте! Чем помочь?")
    async with sessionmaker() as db:
        assert not await _child_talk_continues(db, conv, "А во сколько занятия?")


def test_note_tells_the_model_a_child_is_typing() -> None:
    """Признак обязан дойти до модели: иначе правило существует только в тестах."""
    from datetime import datetime

    from app.llm.dynamic import build_dynamic_note
    from app.types import Language, LeadDraft

    note = build_dynamic_note(
        lang=Language.RU,
        now=datetime(2026, 9, 3, 18, 0, tzinfo=UTC),
        lead=LeadDraft(),
        intents=(),
        injection_suspected=False,
        child_at_keyboard=True,
    )

    assert "сам ребёнок" in note
    assert "не оформляй" in note


def test_note_is_silent_about_the_child_by_default() -> None:
    """Для взрослого этой строки в заметке быть не должно."""
    from datetime import datetime

    from app.llm.dynamic import build_dynamic_note
    from app.types import Language, LeadDraft

    note = build_dynamic_note(
        lang=Language.RU,
        now=datetime(2026, 9, 3, 18, 0, tzinfo=UTC),
        lead=LeadDraft(),
        intents=(),
        injection_suspected=False,
    )

    assert "сам ребёнок" not in note
