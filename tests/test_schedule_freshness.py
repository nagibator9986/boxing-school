"""Расписание, которое клиент видел до смены, не выдаётся за действующее.

Владелец 11.11.2026, через сутки после выкатки нового расписания Плазы и Тобыла: «вы не
исправили расписание». База знаний на сервере была верной, но в переписках, начатых раньше,
бот опирался на свою прежнюю карточку: заметка «клиент уже получил расписание — ссылайся на
список выше», лимит повторной отправки и фильтр, доверявший любому времени, однажды
сказанному в диалоге. Момент смены — ``Gym.schedule_since``.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
import yaml

from app.admin.schedule_store import apply_schedule, read_schedule
from app.core import pipeline
from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.kb.schedule_since import schedules_differ
from app.llm.client import FakeLLMClient, FakeTurn
from app.llm.dynamic import build_dynamic_note
from app.storage import repo_message, repo_outbox
from app.storage.models import Conversation, OutboxMessage
from app.tools import freshness
from app.tools.gyms import gyms_named_in
from app.tools.schedule import get_schedule
from app.types import ChannelKind, Language, LeadDraft, OutboundKind, OutboundMessage, ToolContext
from crm.kbio import KBEditor
from tests.conftest import CHANNEL_ID, RecordingQueue, webhook_payload

TOBYL = "region_tobyl"
PLAZA = "plaza_szm_70"
KSK = "ksk_kairbekova_334"
CHAT = "77015550301"
OLD_TOBYL_CARD = "🗓 Расписание — Тобыл\n\n🥊 Бокс и кикбоксинг\n🕒 Пн, Ср, Пт · 20:00–21:00"


def _since(kb, gym_id: str) -> datetime:
    since = freshness.since_of(kb, kb.gym(gym_id))
    assert since is not None, f"у {gym_id} не отмечен момент смены расписания"
    return since


def _row(created_at, *, text="", kind=OutboundKind.ARTIFACT, artifact_id=None, merged=()):
    payload = {"text": text, "kind": kind.value, "artifact_id": artifact_id, "merged_artifact_ids": list(merged)}
    return freshness.SentRow.from_payload(created_at, payload)


# --------------------------------------------------------------------------- #
# Данные: момент смены у Плазы и Тобыла отмечен
# --------------------------------------------------------------------------- #
def test_plaza_and_tobyl_carry_the_moment_their_schedule_changed(kb) -> None:
    for gym_id in (PLAZA, TOBYL):
        assert _since(kb, gym_id) == datetime(2026, 11, 10, 10, 32, tzinfo=timezone.utc)
    assert freshness.since_of(kb, kb.gym(KSK)) is None


# --------------------------------------------------------------------------- #
# Какая строка устарела
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("artifact_id", "kind", "text", "stale"),
    [
        (f"schedule_{TOBYL}", OutboundKind.ARTIFACT, OLD_TOBYL_CARD, True),
        (f"route_{TOBYL}", OutboundKind.ARTIFACT, "Тобыл, улица Тәуелсіздік, 51", True),
        ("schedules_city", OutboundKind.ARTIFACT, "6. Тобыл\n🕒 Пн, Ср, Пт · 20:00–21:00", True),
        (None, OutboundKind.BOT_REPLY, "В Тобыле занятия в 20:00.", True),
        (None, OutboundKind.BOT_REPLY, "Ждём вас в Тобыле!", False),
        (f"schedule_{KSK}", OutboundKind.ARTIFACT, "🗓 Расписание — КСК", False),
        ("trial_confirmation", OutboundKind.LEAD_CONFIRMATION, "✅ Тобыл, понедельник в 20:00", False),
    ],
)
def test_a_row_sent_before_the_change_is_outdated(kb, artifact_id, kind, text, stale) -> None:
    before = _since(kb, TOBYL) - timedelta(hours=1)
    row = _row(before, text=text, kind=kind, artifact_id=artifact_id)
    outdated = freshness.outdated_gyms(kb, row, named_in=gyms_named_in)
    assert bool(outdated) is stale, outdated


def test_the_same_card_after_the_change_is_current(kb) -> None:
    after = _since(kb, TOBYL) + timedelta(minutes=1)
    row = _row(after, text="🗓 Расписание — Тобыл", artifact_id=f"schedule_{TOBYL}")
    assert freshness.outdated_gyms(kb, row, named_in=gyms_named_in) == ()


def test_a_card_merged_into_a_reply_is_still_recognised(kb) -> None:
    before = _since(kb, PLAZA) - timedelta(days=1)
    row = _row(before, text="Вот расписание", kind=OutboundKind.BOT_REPLY, merged=(f"schedule_{PLAZA}",))
    assert [gym.id for gym in freshness.outdated_gyms(kb, row, named_in=gyms_named_in)] == [PLAZA]


def test_naive_database_time_is_utc(kb) -> None:
    """SQLite отдаёт время без пояса — по соглашению это UTC, а не время школы."""
    since = _since(kb, TOBYL)
    naive_after = (since + timedelta(minutes=1)).astimezone(timezone.utc).replace(tzinfo=None)
    row = _row(naive_after, artifact_id=f"schedule_{TOBYL}")
    assert freshness.outdated_gyms(kb, row, named_in=gyms_named_in) == ()


# --------------------------------------------------------------------------- #
# Хранилище и инструмент расписания
# --------------------------------------------------------------------------- #
async def _conversation(db, chat=CHAT):
    conv = Conversation(conv_key=f"{CHANNEL_ID}:whatsapp:{chat}", channel_id=CHANNEL_ID, chat_type="whatsapp", chat_id=chat)
    db.add(conv)
    await db.flush()
    return conv


async def _sent(db, conv, created_at, **fields):
    message = OutboundMessage(
        conversation_id=conv.id, channel_id=CHANNEL_ID, channel=ChannelKind.WHATSAPP,
        chat_id=conv.chat_id, lang=Language.RU, **fields,
    )
    outbox_id = await repo_outbox.enqueue(db, message)
    await db.execute(sa.update(OutboxMessage).where(OutboxMessage.id == outbox_id).values(created_at=created_at))
    return outbox_id


async def test_sends_are_counted_from_the_moment_given(kb, sessionmaker) -> None:
    since = _since(kb, TOBYL)
    async with sessionmaker() as db:
        conv = await _conversation(db)
        await _sent(db, conv, since - timedelta(days=1), kind=OutboundKind.ARTIFACT,
                    text=OLD_TOBYL_CARD, artifact_id=f"schedule_{TOBYL}")
        artifact = f"schedule_{TOBYL}"
        assert await repo_message.count_artifact_sends(db, conv.id, artifact) == 1
        assert await repo_message.count_artifact_sends(db, conv.id, artifact, since=since) == 0


def _tool_ctx(kb, services, conv) -> ToolContext:
    return ToolContext(
        conversation_id=conv.id, conv_key=conv.conv_key, channel=ChannelKind.WHATSAPP,
        channel_id=CHANNEL_ID, chat_id=conv.chat_id, lang=Language.RU, kb=kb, kb_hash=kb.kb_hash,
        now=datetime(2026, 11, 11, 11, 0, tzinfo=timezone.utc), correlation_id="test", services=services,
    )


async def _schedule_after(kb, state, sessionmaker, settings, sent_at):
    deps = PipelineDeps(sessionmaker=sessionmaker, state=state, kb=lambda: kb, queue=RecordingQueue(),
                        settings=settings, llm=None)
    async with sessionmaker() as db:
        conv = await _conversation(db)
        await _sent(db, conv, sent_at, kind=OutboundKind.ARTIFACT, text=OLD_TOBYL_CARD,
                    artifact_id=f"schedule_{TOBYL}")
        services = pipeline._Services(deps=deps, session=db, conv=conv)
        result = await get_schedule(_tool_ctx(kb, services, conv), gym_id=TOBYL)
        return result, services


async def test_a_card_seen_before_the_change_is_sent_again_with_a_note(kb, state, sessionmaker, settings) -> None:
    result, services = await _schedule_after(kb, state, sessionmaker, settings, _since(kb, TOBYL) - timedelta(days=1))

    sent = [message.text or "" for message in services.messages]
    assert any("19:30–21:00" in text for text in sent), sent
    assert not result.data.get("already_shown"), result.data
    assert any("расписание обновилось" in caveat for caveat in result.caveats), result.caveats


async def test_a_card_seen_after_the_change_is_not_repeated(kb, state, sessionmaker, settings) -> None:
    result, services = await _schedule_after(kb, state, sessionmaker, settings, _since(kb, TOBYL) + timedelta(hours=1))

    assert services.messages == []
    assert result.data.get("already_shown") is True
    assert not any("расписание обновилось" in caveat for caveat in result.caveats)


# --------------------------------------------------------------------------- #
# Заметка модели: «уже получил карточки» и «расписание изменилось»
# --------------------------------------------------------------------------- #
async def test_the_note_stops_pointing_at_an_outdated_card(kb, sessionmaker) -> None:
    since = _since(kb, TOBYL)
    async with sessionmaker() as db:
        conv = await _conversation(db)
        await _sent(db, conv, since - timedelta(days=1), kind=OutboundKind.ARTIFACT,
                    text=OLD_TOBYL_CARD, artifact_id=f"schedule_{TOBYL}")
        cards, outdated = await pipeline._cards_already_sent(db, conv, kb=kb)

    assert "расписание зала" not in cards
    assert outdated == ("Тобыл",)
    note = build_dynamic_note(
        lang=Language.RU, now=datetime(2026, 11, 11, 16, 0), lead=LeadDraft(), intents=(),
        injection_suspected=False, cards_sent=cards, outdated_schedules=outdated,
    )
    assert "Расписание этих залов изменилось" in note and "Тобыл" in note and "get_schedule" in note


async def test_once_the_new_card_is_sent_the_gym_is_no_longer_outdated(kb, sessionmaker) -> None:
    since = _since(kb, TOBYL)
    async with sessionmaker() as db:
        conv = await _conversation(db)
        await _sent(db, conv, since - timedelta(days=1), kind=OutboundKind.ARTIFACT,
                    text=OLD_TOBYL_CARD, artifact_id=f"schedule_{TOBYL}")
        await _sent(db, conv, since + timedelta(hours=1), kind=OutboundKind.ARTIFACT,
                    text="🗓 Расписание — Тобыл\n🕒 Пн, Ср, Пт · 19:30–21:00", artifact_id=f"schedule_{TOBYL}")
        cards, outdated = await pipeline._cards_already_sent(db, conv, kb=kb)

    assert cards == ("расписание зала",)
    assert outdated == ()


async def test_postcheck_trusts_times_only_from_current_schedules(kb, sessionmaker) -> None:
    since = _since(kb, TOBYL)
    async with sessionmaker() as db:
        conv = await _conversation(db)
        await _sent(db, conv, since - timedelta(days=1), kind=OutboundKind.ARTIFACT,
                    text=OLD_TOBYL_CARD + "\n💳 12 000 ₸", artifact_id=f"schedule_{TOBYL}")
        sent = await pipeline._sent_for_postcheck(db, conv, kb=kb)

    assert "20:00" in pipeline._times_in(sent.texts)
    assert "20:00" not in sent.times, "часа 20:00 у Тобыла больше нет"
    assert "21:00" in sent.times, "конец занятия остался прежним — его подтверждает и старая карточка"
    assert set(sent.weekdays) == {"mon", "wed", "fri"}, "дни Тобыла не менялись"
    assert "12000" in pipeline._numbers_in(sent.texts), "цены из старой карточки по-прежнему подтверждены"


async def test_days_that_are_gone_are_not_confirmed_by_an_old_card(kb, sessionmaker) -> None:
    async with sessionmaker() as db:
        conv = await _conversation(db)
        await _sent(db, conv, _since(kb, TOBYL) - timedelta(days=1), kind=OutboundKind.ARTIFACT,
                    text="🗓 Расписание — Тобыл\n🕒 Вт, Чт · 20:00–21:00", artifact_id=f"schedule_{TOBYL}")
        await _sent(db, conv, _since(kb, TOBYL) - timedelta(days=1), kind=OutboundKind.BOT_REPLY,
                    text="В Тобыле ждём вас во вторник в 20:00")
        sent = await pipeline._sent_for_postcheck(db, conv, kb=kb)

    assert not {"tue", "thu"} & set(sent.weekdays) and "20:00" not in sent.times


async def test_a_booking_merged_into_an_outdated_card_keeps_its_time(kb, sessionmaker) -> None:
    """Подтверждение записи, склеенное с прежней карточкой: устарели строки занятий, не запись."""
    since = _since(kb, TOBYL)
    async with sessionmaker() as db:
        conv = await _conversation(db)
        await _sent(db, conv, since - timedelta(days=1), kind=OutboundKind.ARTIFACT,
                    text=OLD_TOBYL_CARD + "\n\n✅ Записали: во вторник в 18:45\n💳 12 000 ₸",
                    artifact_id=f"schedule_{TOBYL}", merged_artifact_ids=("trial_confirmation",))
        sent = await pipeline._sent_for_postcheck(db, conv, kb=kb)
        cards, outdated = await pipeline._cards_already_sent(db, conv, kb=kb)

    assert "18:45" in sent.times and "20:00" not in sent.times
    assert "tue" in sent.weekdays, "день записи подтверждён, хотя занятий во вторник в Тобыле нет"
    assert "прайс" in cards and "расписание зала" not in cards
    assert outdated == ("Тобыл",)


@pytest.mark.parametrize(("shift", "shown"), [(timedelta(days=-1), False), (timedelta(hours=1), True)])
@pytest.mark.parametrize(
    ("question", "reply", "said"),
    [
        # Только час: его подтверждало бы одно лишь «время уже звучало в диалоге».
        ("Во сколько в Тобыле?", "В Тобыле занятия начинаются в 20:00.", "занятия начинаются в 20:00"),
        # Час и день: проверяются оба.
        ("Во сколько в Тобыле в понедельник?", "В Тобыле занятия в понедельник в 20:00.", "занятия в понедельник в 20:00"),
        # Только дни, которых в расписании больше нет: старая карточка их не подтверждает.
        ("В какие дни в Тобыле?", "В Тобыле занятия по вторникам и четвергам.",
         "по вторникам и четвергам"),
    ],
)
async def test_the_bot_cannot_repeat_an_old_hour_from_the_dialog(
    kb, state, sessionmaker, settings, shift, shown, question, reply, said
) -> None:
    """Сквозной ход: модель отвечает по памяти, не вызывая инструмент."""
    async with sessionmaker() as db:
        conv = await _conversation(db)
        await _sent(db, conv, _since(kb, TOBYL) + shift, kind=OutboundKind.ARTIFACT,
                    text=OLD_TOBYL_CARD + "\n🕒 Вт, Чт · 20:00–21:00", artifact_id=f"schedule_{TOBYL}")
        await db.commit()
    kb_loader.swap(kb)
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, kb=kb_loader.get_snapshot, queue=RecordingQueue(),
        settings=settings, llm=FakeLLMClient([FakeTurn.answer(reply), FakeTurn.answer(reply)]),
    )

    decisions = await process_inbound(deps, webhook_payload("m-fresh-1", question, chat_id=CHAT))

    text = "\n".join(out.text or "" for d in decisions for out in d.outbound)
    # Заблокированный ответ заменяет аварийная карточка со всеми залами — там «20:00» есть
    # у КЖБИ (19:00–20:00), поэтому проверяется сама фраза модели.
    assert (said in text) is shown, text
    if not shown:
        assert "Пн, Ср, Пт · 19:30–21:00" in text, "вместо старого часа клиент видит новое расписание"


# --------------------------------------------------------------------------- #
# Кто ставит отметку: CRM и админка в Telegram
# --------------------------------------------------------------------------- #
@pytest.fixture
def kb_copy(tmp_path, kb_dir, media_dir):
    target = tmp_path / "kb"
    shutil.copytree(kb_dir, target)
    return target, media_dir


def _gym_raw(kb_dir, gym_id):
    data = yaml.safe_load((kb_dir / "gyms.yaml").read_text(encoding="utf-8"))
    return next(gym for gym in data["gyms"] if gym["id"] == gym_id)


def _since_value(raw) -> datetime:
    value = raw["schedule_since"]
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def test_crm_stamps_a_changed_schedule_and_keeps_an_unchanged_one(kb_copy) -> None:
    kb_dir, media_dir = kb_copy
    editor = KBEditor(kb_dir, media_dir=media_dir, schema_version=1)
    started = datetime.now(timezone.utc).replace(microsecond=0)

    document = editor.load("gyms.yaml")
    ksk = next(gym for gym in document["gyms"] if gym["id"] == KSK)
    ksk["schedule"][0]["time_end"] = "10:45" if ksk["schedule"][0]["time_end"] != "10:45" else "10:40"
    tobyl = next(gym for gym in document["gyms"] if gym["id"] == TOBYL)
    # Форма CRM собирает зал заново и про отметку не знает: merge_into её бы стёр.
    del tobyl["schedule_since"]
    editor.save({"gyms.yaml": document})

    assert _since_value(_gym_raw(kb_dir, KSK)) >= started
    assert _since_value(_gym_raw(kb_dir, TOBYL)) == datetime(2026, 11, 10, 15, 32, tzinfo=_since_value(_gym_raw(kb_dir, TOBYL)).tzinfo)
    assert "schedule_since" not in _gym_raw(kb_dir, "center_kasymkhanova_10")


def test_the_telegram_admin_stamps_a_changed_schedule(kb_copy) -> None:
    kb_dir, media_dir = kb_copy
    started = datetime.now(timezone.utc).replace(microsecond=0)
    slots = read_schedule(kb_dir, KSK)
    slots[0] = {**slots[0], "time_end": "10:45" if slots[0]["time_end"] != "10:45" else "10:40"}

    apply_schedule(kb_dir, KSK, slots, media_dir=media_dir, schema_version=1)

    assert _since_value(_gym_raw(kb_dir, KSK)) >= started


def test_the_same_slots_written_differently_are_not_a_change(kb) -> None:
    slot = kb.gym(TOBYL).schedule[0].model_dump(mode="json")
    sparse = {key: value for key, value in slot.items() if value is not None}
    assert not schedules_differ([slot], [sparse])
    assert schedules_differ([slot], [{**slot, "time_start": "20:00"}])


# --------------------------------------------------------------------------- #
# Материалы: подпись к видео дороги, список расписаний города
# --------------------------------------------------------------------------- #
async def _with_services(kb, state, sessionmaker, settings, db, rows):
    deps = PipelineDeps(sessionmaker=sessionmaker, state=state, kb=lambda: kb, queue=RecordingQueue(),
                        settings=settings, llm=None)
    conv = await _conversation(db)
    for created_at, fields in rows:
        await _sent(db, conv, created_at, **fields)
    services = pipeline._Services(deps=deps, session=db, conv=conv)
    return _tool_ctx(kb, services, conv), services


async def test_route_caption_carries_the_new_schedule_after_an_outdated_card(kb, state, sessionmaker, settings) -> None:
    from app.tools.content import send_content

    old = (_since(kb, TOBYL) - timedelta(days=1),
           dict(kind=OutboundKind.ARTIFACT, text=OLD_TOBYL_CARD, artifact_id=f"schedule_{TOBYL}"))
    async with sessionmaker() as db:
        ctx, services = await _with_services(kb, state, sessionmaker, settings, db, [old])
        await send_content(ctx, artifact_id=f"route_{TOBYL}")

    sent = "\n".join(message.text or "" for message in services.messages)
    assert "Пн, Ср, Пт · 19:30–21:00" in sent, sent


@pytest.mark.parametrize(("shift", "resent"), [(timedelta(days=-1), True), (timedelta(hours=1), False)])
async def test_city_schedules_list_is_resent_only_when_outdated(kb, state, sessionmaker, settings, shift, resent) -> None:
    from app.tools.content import send_content

    limit = kb.artifact("schedules_city").max_send_per_dialog
    rows = [(_since(kb, TOBYL) + shift, dict(kind=OutboundKind.ARTIFACT, text="6. Тобыл\n🕒 Пн, Ср, Пт · 20:00–21:00",
                                             artifact_id="schedules_city"))] * limit
    async with sessionmaker() as db:
        ctx, services = await _with_services(kb, state, sessionmaker, settings, db, rows)
        result = await send_content(ctx, artifact_id="schedules_city")

    assert (result.data.get("status") != "already_sent") is resent, result.data
    assert bool(services.messages) is resent


@pytest.mark.parametrize(("shift", "by_code"), [(timedelta(days=-1), True), (timedelta(hours=1), False)])
async def test_time_question_gets_the_city_card_again_when_it_was_outdated(kb, state, sessionmaker, settings, shift, by_code) -> None:
    limit = kb.artifact("schedules_city").max_send_per_dialog
    async with sessionmaker() as db:
        conv = await _conversation(db)
        for _ in range(limit):
            await _sent(db, conv, _since(kb, TOBYL) + shift, kind=OutboundKind.ARTIFACT,
                        text="6. Тобыл\n🕒 Пн, Ср, Пт · 20:00–21:00", artifact_id="schedules_city")
        await db.commit()
    kb_loader.swap(kb)
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, kb=kb_loader.get_snapshot, queue=RecordingQueue(),
        settings=settings, llm=FakeLLMClient([FakeTurn.answer("Подскажите, какой зал вам удобнее?")] * 2),
    )

    decisions = await process_inbound(deps, webhook_payload("m-city-1", "Во сколько у вас тренировки?", chat_id=CHAT))

    text = "\n".join(out.text or "" for d in decisions for out in d.outbound)
    assert ("Пн, Ср, Пт · 19:30–21:00" in text) is by_code, text


def test_restoring_a_backup_with_another_schedule_stamps_it(kb_copy) -> None:
    kb_dir, media_dir = kb_copy
    editor = KBEditor(kb_dir, media_dir=media_dir, schema_version=1)
    document = editor.load("gyms.yaml")
    ksk = next(gym for gym in document["gyms"] if gym["id"] == KSK)
    ksk["schedule"][0]["time_end"] = "10:45" if ksk["schedule"][0]["time_end"] != "10:45" else "10:40"
    saved = editor.save({"gyms.yaml": document})
    stamped_by_edit = _since_value(_gym_raw(kb_dir, KSK))

    editor.restore(saved.backup)

    # В копии прежнее расписание и отметки нет — после отката она есть, и она не раньше правки.
    assert _since_value(_gym_raw(kb_dir, KSK)) >= stamped_by_edit


async def test_an_old_card_still_confirms_days_that_did_not_change(kb, state, sessionmaker, settings) -> None:
    """Дни Тобыла не менялись (Пн, Ср, Пт): «ждём в понедельник» по старой карточке — не выдумка.

    Прод 11.11.2026, 19:22: ответ с «понедельник» сняли как выдумку, разговор ушёл
    администратору, бот встал на паузу.
    """
    async with sessionmaker() as db:
        conv = await _conversation(db)
        await _sent(db, conv, _since(kb, TOBYL) - timedelta(days=1), kind=OutboundKind.ARTIFACT,
                    text=OLD_TOBYL_CARD, artifact_id=f"schedule_{TOBYL}")
        await db.commit()
    kb_loader.swap(kb)
    reply = "Хорошо, ждём вас в Тобыле в понедельник!"
    deps = PipelineDeps(
        sessionmaker=sessionmaker, state=state, kb=kb_loader.get_snapshot, queue=RecordingQueue(),
        settings=settings, llm=FakeLLMClient([FakeTurn.answer(reply), FakeTurn.answer(reply)]),
    )

    decisions = await process_inbound(deps, webhook_payload("m-days-1", "Спасибо, придём", chat_id=CHAT))

    text = "\n".join(out.text or "" for d in decisions for out in d.outbound)
    assert "в понедельник" in text, text
