"""Возврат клиентов, этапы 2–3: черновики, отправка, статусы по переписке.

Номера и имена вымышленные: репозиторий публичный.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from app.admin.admin_store import AdminStore
from app.admin.runtime_settings import RuntimeSettings
from app.campaign.contacts import (
    BotFacts,
    CampaignResult,
    CampaignSegment,
    CampaignStatus,
    apply_result,
    progress,
    send_check,
)
from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeLLMClient, FakeTurn
from app.storage.models import CampaignContact, Conversation, EscalationState, Lead, Message, OutboxMessage
from app.workers import tasks_campaign as campaign
from tests.conftest import CHANNEL_ID, RecordingQueue, webhook_payload

#: 11:00 по Алматы — внутри часов рассылки по умолчанию (10:00–20:00).
NOW = datetime(2026, 10, 27, 6, 0, tzinfo=UTC)
PHONE = "77015550901"
OTHER = "77015550999"
WEEK = timedelta(days=7)

MODEL_DRAFT = "Вы спрашивали про бокс для сына — первая тренировка у нас бесплатная. Подобрать зал рядом с домом?"


@pytest.fixture
def owner() -> dict[str, RuntimeSettings]:
    return {
        "now": RuntimeSettings.from_values(
            {"bot_enabled": "on", "campaign_enabled": "on", "campaign_daily_limit": "12", "campaign_hours": "10:00-20:00"}
        )
    }


@pytest.fixture
def admin_db(tmp_path: Path) -> Path:
    return tmp_path / "admin.db"


@pytest.fixture
async def deps(kb, state, sessionmaker, settings, owner, admin_db) -> PipelineDeps:
    kb_loader.swap(kb)
    built = PipelineDeps(
        sessionmaker=sessionmaker,
        state=state,
        llm=FakeLLMClient([]),
        kb=kb_loader.get_snapshot,
        queue=RecordingQueue(),
        settings=settings.model_copy(update={"admin_db_path": admin_db}),
        runtime=lambda: owner["now"],
    )
    # Канал WhatsApp школы — по живому диалогу другого клиента, как на сервере.
    await _conversation(built, "77015550000", last_inbound=NOW - timedelta(days=30))
    return built


async def _contact(deps: PipelineDeps, *, chat_id: str = PHONE, status: CampaignStatus = CampaignStatus.APPROVED,
                   segment: CampaignSegment = CampaignSegment.FADED, text: str | None = "Здравствуйте! Подобрать зал?",
                   **extra) -> UUID:
    values = {
        "chat_id": chat_id,
        "segment": segment.value,
        "status": status.value,
        "parent_name": "Петрова Анна",
        "summary": "Спрашивала про бокс для сына 7 лет, пропала после цены",
        "last_contact_at": NOW - timedelta(days=20),
        "draft_text": text,
        "approved_text": text if status is not CampaignStatus.READY else None,
        "approved_at": NOW - timedelta(days=1) if status is CampaignStatus.APPROVED else None,
        **extra,
    }
    async with deps.sessionmaker() as session:
        row = CampaignContact(**values)
        session.add(row)
        await session.commit()
        return row.id


async def _conversation(deps: PipelineDeps, chat_id: str, *, last_inbound: datetime | None, blocked: bool = False,
                        channel: str = CHANNEL_ID) -> UUID:
    async with deps.sessionmaker() as session:
        conv = Conversation(
            conv_key=f"{channel}:whatsapp:{chat_id}", channel_id=channel, chat_type="whatsapp", chat_id=chat_id,
            last_inbound_at=last_inbound, followup_blocked=blocked, lang="ru",
        )
        session.add(conv)
        await session.commit()
        return conv.id


async def _row(deps: PipelineDeps, contact_id: UUID) -> CampaignContact:
    async with deps.sessionmaker() as session:
        return await session.get(CampaignContact, contact_id)


async def _send(deps: PipelineDeps, owner, *, now: datetime = NOW, due: list[str] | None = None) -> bool:
    kb = deps.kb()
    runtime = owner["now"]
    return await campaign.send(
        deps, now=now, kb=kb, runtime=runtime, settings=deps.settings, silent=frozenset(), due=due or []
    )


async def _track(deps: PipelineDeps, *, now: datetime) -> list[str]:
    return await campaign.track(deps, now=now, policy=deps.kb().policies.campaign, silent=frozenset())


async def _outbox(deps: PipelineDeps) -> list[OutboxMessage]:
    async with deps.sessionmaker() as session:
        return list((await session.execute(sa.select(OutboxMessage).order_by(OutboxMessage.created_at))).scalars())


# --------------------------------------------------------------------------- #
# Черновики
# --------------------------------------------------------------------------- #
async def test_the_model_makes_the_template_personal(deps) -> None:
    deps.llm.push(FakeTurn.answer(MODEL_DRAFT))
    contact = await _contact(deps, status=CampaignStatus.READY, text=None)

    assert await campaign.draft(deps, kb=deps.kb(), settings=deps.settings) == 1

    row = await _row(deps, contact)
    assert row.status == "draft" and row.draft_source == "model"
    # Обращение — по имени, а не «Петрова Анна»; приветствие добавляет код, а не модель.
    assert row.draft_text == f"Здравствуйте, Анна! {MODEL_DRAFT}"
    request = deps.llm.requests[0]
    assert "пропала после цены" in request.user_text and "AINAZAROV TOP TEAM" in request.user_text
    assert request.tool_mode == "NONE"


@pytest.mark.parametrize(
    "answer",
    [
        "Абонемент у нас 25 000 тенге. Записать сына?",               # цена — выдумка
        "Приходите в субботу в 17:00 на пробное. Удобно?",            # время и день — выдумка
        "Здравствуйте! Первая тренировка бесплатная.",                # без вопроса
        "Рады видеть Тимура! Записать его на бесплатное пробное?",    # чужое имя
    ],
)
async def test_a_draft_that_fails_the_fact_check_falls_back_to_the_template(deps, answer: str) -> None:
    deps.llm.push(FakeTurn.answer(answer))
    contact = await _contact(deps, status=CampaignStatus.READY, text=None)

    await campaign.draft(deps, kb=deps.kb(), settings=deps.settings)

    row = await _row(deps, contact)
    assert row.status == "draft" and row.draft_source == "template"
    assert row.draft_text == deps.kb().text("campaign.hello", "ru", name="Анна") + " " + deps.kb().text(
        "campaign.first_faded", "ru"
    )
    assert "шаблону" in row.status_reason


async def test_details_the_conversation_never_mentioned_are_not_invented(deps) -> None:
    """Живой прогон 27.10.2026: итог «записались, не пришли», а модель — «с сыном на кикбоксинг»."""
    deps.llm.push(
        FakeTurn.answer("Вы планировали прийти с сыном на кикбоксинг, но не получилось. Подобрать другой день?"),
        FakeTurn.answer("вы планировали прийти с ребёнком на пробное, но не получилось. Подобрать другой день?"),
    )
    invented = await _contact(deps, status=CampaignStatus.READY, text=None, segment=CampaignSegment.NO_SHOW,
                              summary="Записались на пробное, не пришли")
    await campaign.draft(deps, kb=deps.kb(), settings=deps.settings)
    assert (await _row(deps, invented)).draft_source == "template"

    honest = await _contact(deps, chat_id=OTHER, status=CampaignStatus.READY, text=None,
                            segment=CampaignSegment.NO_SHOW, summary="Записались на пробное, не пришли")
    await campaign.draft(deps, kb=deps.kb(), settings=deps.settings)
    row = await _row(deps, honest)
    # Строчная буква в начале исправлена, а не стоила черновика.
    assert row.draft_source == "model" and "! Вы планировали прийти с ребёнком" in row.draft_text


async def test_details_from_the_conversation_are_welcome(deps) -> None:
    deps.llm.push(FakeTurn.answer("Вы спрашивали про кикбоксинг для дочки — первая тренировка бесплатная. Подобрать зал?"))
    contact = await _contact(deps, status=CampaignStatus.READY, text=None,
                             summary="Спрашивала про кикбоксинг для дочери 9 лет")
    await campaign.draft(deps, kb=deps.kb(), settings=deps.settings)
    assert (await _row(deps, contact)).draft_source == "model"


async def test_without_the_model_the_draft_is_the_template(deps) -> None:
    """Кредиты Gemini кончились — черновик всё равно есть, по шаблону сегмента."""
    contact = await _contact(deps, status=CampaignStatus.READY, text=None, segment=CampaignSegment.NO_SHOW,
                             parent_name="мама Тимура")

    await campaign.draft(deps, kb=deps.kb(), settings=deps.settings)

    row = await _row(deps, contact)
    assert row.draft_source == "template"
    assert row.draft_text.startswith(deps.kb().text("campaign.hello_anon", "ru"))
    assert deps.kb().text("campaign.first_no_show", "ru") in row.draft_text


async def test_a_stale_text_of_a_contact_cleared_again_is_rewritten(deps) -> None:
    """В «можно писать» возвращаются и со старым текстом — он мог быть под другой исход."""
    deps.llm.push(FakeTurn.answer(MODEL_DRAFT))
    contact = await _contact(deps, status=CampaignStatus.READY, text="Старый текст под прежний исход?")

    assert await campaign.draft(deps, kb=deps.kb(), settings=deps.settings) == 1

    row = await _row(deps, contact)
    assert row.status == "draft" and row.draft_text == f"Здравствуйте, Анна! {MODEL_DRAFT}"


async def test_drafts_are_written_only_for_contacts_cleared_to_write(deps) -> None:
    waiting = await _contact(deps, status=CampaignStatus.NEEDS_CHECK, text=None)
    done = await _contact(deps, chat_id=OTHER, status=CampaignStatus.DRAFT, text="Свой текст")

    assert await campaign.draft(deps, kb=deps.kb(), settings=deps.settings) == 0
    assert (await _row(deps, waiting)).draft_text is None
    assert (await _row(deps, done)).draft_text == "Свой текст"
    assert deps.llm.generate_calls == 0


# --------------------------------------------------------------------------- #
# Отправка
# --------------------------------------------------------------------------- #
async def test_an_approved_message_goes_through_the_bot_queue_into_a_new_dialog(deps, owner) -> None:
    contact = await _contact(deps, text="Здравствуйте, Анна! Подобрать зал рядом с домом?")

    assert await _send(deps, owner) is True

    row = await _row(deps, contact)
    assert row.status == "sent" and row.sent_at is not None
    async with deps.sessionmaker() as session:
        conv = (await session.execute(sa.select(Conversation).where(Conversation.chat_id == PHONE))).scalars().one()
        history = [
            item.gemini_content
            for item in (
                await session.execute(
                    sa.select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
                )
            ).scalars()
        ]
    # Ключ диалога тот, что посчитает приём ответа: ответ ляжет в этот же диалог.
    assert conv.conv_key == f"{CHANNEL_ID}:whatsapp:{PHONE}" and row.conversation_id == conv.id.hex
    [message] = await _outbox(deps)
    assert message.payload["kind"] == "campaign" and message.payload["text"] == "Здравствуйте, Анна! Подобрать зал рядом с домом?"
    assert message.crm_message_id == campaign.first_message_id(contact.hex)
    assert deps.queue.outbox == [(message.id, 0)]
    # В истории модели — заметка (не слова клиента) и само сообщение.
    assert history[0]["role"] == "user" and history[0]["parts"][0]["text"].startswith("[служебная заметка системы]")
    assert "пропала после цены" in history[0]["parts"][0]["text"]
    assert history[1] == {"role": "model", "parts": [{"text": "Здравствуйте, Анна! Подобрать зал рядом с домом?"}]}


async def test_the_clients_reply_continues_the_same_dialog_without_a_greeting_menu(deps) -> None:
    """Ответ на рассылку — обычный разговор: тот же диалог, модель видит, что написала школа."""
    now = datetime.now(tz=UTC) - timedelta(minutes=30)
    contact = await _contact(deps, text="Здравствуйте, Анна! Подобрать зал рядом с домом?")
    assert await campaign.send_first(deps, contact.hex, now=now, kb=deps.kb(), settings=deps.settings,
                                     silent=frozenset()) is True

    deps.llm.push(FakeTurn.answer("Подскажите, в каком районе вам удобнее?"))
    await process_inbound(deps, webhook_payload("cmp-1", "Здравствуйте", chat_id=PHONE))

    async with deps.sessionmaker() as session:
        convs = (await session.execute(sa.select(Conversation).where(Conversation.chat_id == PHONE))).scalars().all()
    assert len(convs) == 1
    request = deps.llm.requests[-1]
    said = [part.get("text", "") for item in request.history for part in item.get("parts", [])]
    assert "Здравствуйте, Анна! Подобрать зал рядом с домом?" in said
    texts = [row.payload.get("text") for row in await _outbox(deps)]
    assert texts[-1].startswith("Подскажите, в каком районе вам удобнее?")
    assert not any(deps.kb().text("greeting.first", "ru")[:40] in (text or "") for text in texts)

    await _track(deps, now=datetime.now(tz=UTC))
    row = await _row(deps, contact)
    assert row.status == "replied" and row.replied_at is not None


async def test_an_existing_dialog_of_the_number_is_reused(deps, owner) -> None:
    conv_id = await _conversation(deps, PHONE, last_inbound=NOW - timedelta(days=40))
    contact = await _contact(deps)

    assert await _send(deps, owner) is True

    assert (await _row(deps, contact)).conversation_id == conv_id.hex
    [message] = await _outbox(deps)
    assert message.conversation_id == conv_id and message.payload["channel_id"] == CHANNEL_ID


async def test_a_dialog_through_an_old_channel_is_not_used(deps, owner) -> None:
    """Школа сменила канал Wazzup: через старый сообщение не дойдёт, а ответ придёт новым."""
    old = await _conversation(deps, PHONE, last_inbound=NOW - timedelta(days=60), channel="old-channel")
    contact = await _contact(deps)

    assert await _send(deps, owner) is True

    [message] = await _outbox(deps)
    assert message.payload["channel_id"] == CHANNEL_ID and message.conversation_id != old
    assert (await _row(deps, contact)).conversation_id == message.conversation_id.hex


@pytest.mark.parametrize(
    ("setup", "status", "reason"),
    [
        ("stop", "opted_out", "стоп"),
        ("pause", "needs_check", "администратор"),
        ("wrote_after_approval", "needs_check", "после одобрения"),
        ("recent", "needs_check", "свежий"),
    ],
)
async def test_the_last_check_before_sending_stops_the_message(deps, owner, setup: str, status: str, reason: str) -> None:
    contact = await _contact(deps)
    if setup == "stop":
        await _conversation(deps, PHONE, last_inbound=NOW - timedelta(days=30), blocked=True)
    elif setup == "pause":
        conv_id = await _conversation(deps, PHONE, last_inbound=NOW - timedelta(days=30))
        async with deps.sessionmaker() as session:
            session.add(EscalationState(conversation_id=conv_id, paused=True, pause_reason="manual",
                                        escalation_count=0, resume_policy="manual_only"))
            await session.commit()
    elif setup == "wrote_after_approval":
        # Одобрен давно, клиент писал боту после одобрения, но уже не на этой неделе.
        await _conversation(deps, PHONE, last_inbound=NOW - timedelta(days=8))
        async with deps.sessionmaker() as session:
            await session.execute(sa.update(CampaignContact).values(approved_at=NOW - timedelta(days=10)))
            await session.commit()
    else:
        await _conversation(deps, PHONE, last_inbound=NOW - timedelta(days=2))

    assert await _send(deps, owner) is False

    row = await _row(deps, contact)
    assert row.status == status and reason in row.status_reason.lower(), row.status_reason
    assert await _outbox(deps) == []


async def test_a_checked_contact_is_not_written_to_while_a_human_answers(deps, owner) -> None:
    """Проверенный контакт: мягкие сигналы сверки молчат, но администратор в диалоге прямо сейчас — нет."""
    contact = await _contact(deps, checked_at=NOW - timedelta(days=2))
    conv_id = await _conversation(deps, PHONE, last_inbound=NOW - timedelta(days=30))
    async with deps.sessionmaker() as session:
        session.add(EscalationState(conversation_id=conv_id, paused=True, pause_reason="operator_reply",
                                    escalation_count=0, resume_policy="manual_only"))
        await session.commit()

    assert await _send(deps, owner) is False

    row = await _row(deps, contact)
    assert row.status == "needs_check" and "сейчас отвечает администратор" in row.status_reason


async def test_a_greeting_written_by_the_model_is_not_doubled(deps) -> None:
    deps.llm.push(FakeTurn.answer("Здравствуйте! Вы спрашивали про бокс для сына — подобрать зал рядом с домом?"))
    contact = await _contact(deps, status=CampaignStatus.READY, text=None)

    await campaign.draft(deps, kb=deps.kb(), settings=deps.settings)

    row = await _row(deps, contact)
    assert row.draft_source == "model"
    assert row.draft_text == "Здравствуйте, Анна! Вы спрашивали про бокс для сына — подобрать зал рядом с домом?"


async def test_a_pass_with_only_drafts_to_write_still_runs(deps) -> None:
    deps.llm.push(FakeTurn.answer(MODEL_DRAFT))
    contact = await _contact(deps, status=CampaignStatus.READY, text=None)

    await campaign.campaign_cron({"deps": deps})

    assert (await _row(deps, contact)).status == "draft"


@pytest.mark.parametrize(
    "change",
    ["campaign_off", "bot_off", "night", "limit", "gap", "later", "zero_limit"],
)
async def test_nothing_goes_out_when_the_pace_forbids_it(deps, owner, change: str) -> None:
    now = NOW
    values = {"bot_enabled": "on", "campaign_enabled": "on", "campaign_daily_limit": "12", "campaign_hours": "10:00-20:00"}
    if change == "campaign_off":
        values["campaign_enabled"] = "off"
    elif change == "bot_off":
        values["bot_enabled"] = "off"
    elif change == "zero_limit":
        values["campaign_daily_limit"] = "0"
    elif change == "night":
        now = NOW + timedelta(hours=10)  # 21:00 по Алматы
    elif change == "limit":
        values["campaign_daily_limit"] = "2"
        for index in range(2):
            await _contact(deps, chat_id=f"7701555080{index}", status=CampaignStatus.SENT,
                           sent_at=NOW - timedelta(hours=2 + index))
    elif change == "gap":
        await _contact(deps, chat_id="77015550800", status=CampaignStatus.SENT, sent_at=NOW - timedelta(minutes=2))
    owner["now"] = RuntimeSettings.from_values(values)
    contact = await _contact(deps, not_before=NOW + timedelta(days=3) if change == "later" else None)

    assert await _send(deps, owner, now=now) is False
    assert (await _row(deps, contact)).status == "approved"


async def test_messages_go_one_at_a_time_with_a_pause(deps, owner) -> None:
    first = await _contact(deps)
    second = await _contact(deps, chat_id=OTHER, approved_at=NOW - timedelta(hours=1))

    assert await _send(deps, owner) is True
    assert await _send(deps, owner, now=NOW + timedelta(minutes=1)) is False
    assert (await _row(deps, first)).status == "sent" and (await _row(deps, second)).status == "approved"
    assert await _send(deps, owner, now=NOW + timedelta(minutes=10)) is True
    assert (await _row(deps, second)).status == "sent"


# --------------------------------------------------------------------------- #
# Статусы и напоминание
# --------------------------------------------------------------------------- #
async def _sent(deps: PipelineDeps, *, sent_at: datetime = NOW - timedelta(days=1), outbox_state: str = "sent",
                error: str | None = None, **extra) -> tuple[UUID, UUID]:
    conv_id = await _conversation(deps, PHONE, last_inbound=None)
    contact = await _contact(deps, status=CampaignStatus.SENT, sent_at=sent_at, conversation_id=None, **extra)
    async with deps.sessionmaker() as session:
        await session.execute(sa.update(CampaignContact).values(conversation_id=conv_id.hex))
        session.add(OutboxMessage(
            conversation_id=conv_id, crm_message_id=campaign.first_message_id(contact.hex),
            payload={"text": "x"}, state=outbox_state, attempts=1, last_error=error,
        ))
        await session.commit()
    return contact, conv_id


async def _set_conv(deps: PipelineDeps, conv_id: UUID, **values) -> None:
    async with deps.sessionmaker() as session:
        await session.execute(sa.update(Conversation).where(Conversation.id == conv_id).values(**values))
        await session.commit()


async def _lead(deps: PipelineDeps, conv_id: UUID, status: str, trial: datetime | None) -> None:
    async with deps.sessionmaker() as session:
        session.add(Lead(conversation_id=conv_id, child_name="Тимур", child_age=7, child_gender="male",
                         phone_source="channel", status=status, escalation=False, messages_count=2, trial_slot=trial))
        await session.commit()


async def test_a_reply_a_booking_and_a_purchase_move_the_status(deps) -> None:
    contact, conv_id = await _sent(deps)
    await _set_conv(deps, conv_id, last_inbound_at=NOW - timedelta(hours=3))

    await _track(deps, now=NOW)
    row = await _row(deps, contact)
    assert row.status == "replied" and row.replied_at is not None

    await _lead(deps, conv_id, "trial_booked", NOW + timedelta(days=2))
    await _track(deps, now=NOW)
    assert (await _row(deps, contact)).status == "booked"

    async with deps.sessionmaker() as session:
        await session.execute(sa.update(Lead).values(status="converted"))
        await session.commit()
    await _track(deps, now=NOW)
    row = await _row(deps, contact)
    assert row.status == "bought" and "Купил" in row.status_reason


async def test_an_old_booking_is_not_a_result_of_the_message(deps) -> None:
    """Пробное до сообщения человек видел, проверяя контакт: записью по рассылке оно не считается."""
    contact, conv_id = await _sent(deps)
    await _lead(deps, conv_id, "trial_booked", NOW - timedelta(days=10))

    await _track(deps, now=NOW)

    assert (await _row(deps, contact)).status == "sent"


async def test_a_stop_word_after_the_message_closes_the_contact(deps) -> None:
    contact, conv_id = await _sent(deps)
    await _set_conv(deps, conv_id, last_inbound_at=NOW - timedelta(hours=3), followup_blocked=True)

    await _track(deps, now=NOW)

    row = await _row(deps, contact)
    assert row.status == "opted_out" and row.replied_at is not None


async def test_an_unknown_number_is_undelivered_without_an_alert_per_number(deps) -> None:
    contact, _ = await _sent(deps, outbox_state="failed", error="badcontact: no such chat")

    await _track(deps, now=NOW)

    row = await _row(deps, contact)
    assert row.status == "undelivered" and "нет в WhatsApp" in row.status_reason


async def test_a_retry_in_progress_is_not_a_failure(deps) -> None:
    contact, _ = await _sent(deps, outbox_state="failed", error="500: server")
    async with deps.sessionmaker() as session:
        await session.execute(sa.update(OutboxMessage).values(next_attempt_at=NOW + timedelta(minutes=1)))
        await session.commit()

    await _track(deps, now=NOW)

    assert (await _row(deps, contact)).status == "sent"


async def test_one_reminder_then_no_answer(deps, owner) -> None:
    policy = deps.kb().policies.campaign
    sent_at = NOW - timedelta(days=policy.remind_after_days, minutes=5)
    contact, conv_id = await _sent(deps, sent_at=sent_at)

    due = await _track(deps, now=NOW)
    assert due == [contact.hex]
    assert await _send(deps, owner, due=due) is True

    row = await _row(deps, contact)
    assert row.reminder_sent_at is not None and row.status == "sent"
    reminder = [item for item in await _outbox(deps) if item.crm_message_id == campaign.reminder_message_id(contact.hex)]
    assert reminder and reminder[0].payload["text"] == deps.kb().text("campaign.reminder", "ru")
    # Второго напоминания нет.
    assert await _track(deps, now=NOW + timedelta(hours=1)) == []

    later = NOW + timedelta(days=policy.give_up_after_days, minutes=1)
    await _track(deps, now=later)
    row = await _row(deps, contact)
    assert row.status == "no_answer"


async def test_a_late_reply_after_no_answer_still_counts(deps) -> None:
    contact, conv_id = await _sent(deps, sent_at=NOW - timedelta(days=12))
    async with deps.sessionmaker() as session:
        await session.execute(sa.update(CampaignContact).values(status="no_answer"))
        await session.commit()
    await _track(deps, now=NOW)
    assert (await _row(deps, contact)).status == "no_answer"

    await _set_conv(deps, conv_id, last_inbound_at=NOW - timedelta(hours=1))
    await _track(deps, now=NOW)

    row = await _row(deps, contact)
    assert row.status == "replied" and row.replied_at is not None


async def test_no_reminder_to_a_client_who_already_answered(deps, owner) -> None:
    policy = deps.kb().policies.campaign
    contact, conv_id = await _sent(deps, sent_at=NOW - timedelta(days=policy.remind_after_days + 1))
    await _set_conv(deps, conv_id, last_inbound_at=NOW - timedelta(hours=1))

    assert await campaign.send_reminder(deps, contact.hex, now=NOW, kb=deps.kb(), silent=frozenset()) is False
    assert await _track(deps, now=NOW) == []
    assert (await _row(deps, contact)).status == "replied"


def _campaign_setting(admin_db: Path, value: str | None = None) -> str:
    store = AdminStore(admin_db)
    try:
        if value is not None:
            store.set("campaign_enabled", value)
        return store.get("campaign_enabled")
    finally:
        store.close()


async def _alerts(deps: PipelineDeps) -> list[str]:
    return [row.payload.get("text", "") for row in await _outbox(deps) if "Рассылка" in row.payload.get("text", "")]


async def test_a_spam_mark_switches_the_campaign_off_once(deps, admin_db: Path, monkeypatch) -> None:
    from app.notify import manager

    monkeypatch.setattr(manager, "manager_target", lambda *a, **k: (manager.ChannelKind.WHATSAPP, CHANNEL_ID, "77000000077"))
    _campaign_setting(admin_db, "on")
    contact, _ = await _sent(deps, outbox_state="failed", error="messagesisspam: spam")
    async with deps.sessionmaker() as session:
        await session.execute(sa.update(OutboxMessage).values(updated_at=NOW - timedelta(minutes=1)))
        await session.commit()

    await _track(deps, now=NOW)

    assert (await _row(deps, contact)).status == "undelivered"
    assert _campaign_setting(admin_db) == "off"
    assert len(await _alerts(deps)) == 1
    # Тот же сигнал на следующем проходе — ни второй тревоги, ни повторного выключения.
    await deps.state.delete("alert:campaign_spam")  # не подавление повторов держит, а выключенная рассылка
    await _track(deps, now=NOW + timedelta(minutes=1))
    assert len(await _alerts(deps)) == 1


async def test_a_spam_mark_on_a_reminder_also_stops_and_an_old_one_does_not(deps, admin_db: Path, monkeypatch) -> None:
    from app.notify import manager

    monkeypatch.setattr(manager, "manager_target", lambda *a, **k: (manager.ChannelKind.WHATSAPP, CHANNEL_ID, "77000000077"))
    _campaign_setting(admin_db, "on")
    contact, conv_id = await _sent(deps, reminder_sent_at=NOW - timedelta(hours=1))
    async with deps.sessionmaker() as session:
        session.add(OutboxMessage(
            conversation_id=conv_id, crm_message_id=campaign.reminder_message_id(contact.hex), payload={"text": "x"},
            state="failed", attempts=1, last_error="messagesisspam: spam", updated_at=NOW - timedelta(hours=2),
        ))
        await session.commit()

    await _track(deps, now=NOW)
    assert _campaign_setting(admin_db) == "on"  # сигнал двухчасовой давности уже видели

    async with deps.sessionmaker() as session:
        await session.execute(sa.update(OutboxMessage).where(OutboxMessage.state == "failed")
                              .values(updated_at=NOW - timedelta(minutes=2)))
        await session.commit()
    await _track(deps, now=NOW)
    assert _campaign_setting(admin_db) == "off"
    assert (await _row(deps, contact)).status == "sent"
    assert len(await _alerts(deps)) == 1
    # Контакт по-прежнему «отправлено», сигнал ещё свежий: выключать уже нечего, тревоги нет.
    await deps.state.delete("alert:campaign_spam")
    await _track(deps, now=NOW + timedelta(minutes=1))
    assert len(await _alerts(deps)) == 1


async def test_the_whole_pass_runs_from_the_queue(deps, owner, monkeypatch) -> None:
    """Задание целиком: черновик пишется, одобренное уходит — за один проход."""
    monkeypatch.setattr(campaign, "_now", lambda: NOW)
    deps.llm.push(FakeTurn.answer(MODEL_DRAFT))
    ready = await _contact(deps, status=CampaignStatus.READY, text=None)
    approved = await _contact(deps, chat_id=OTHER)

    await campaign.campaign_cron({"deps": deps})

    assert (await _row(deps, ready)).status == "draft"
    assert (await _row(deps, approved)).status == "sent"


async def test_an_empty_list_costs_one_query(deps) -> None:
    await campaign.campaign_cron({"deps": deps})
    assert deps.llm.generate_calls == 0 and deps.queue.outbox == []


# --------------------------------------------------------------------------- #
# Правила без базы
# --------------------------------------------------------------------------- #
def test_hours_day_and_pause() -> None:
    assert campaign.within_hours(NOW, (600, 1200), tz_name="Asia/Almaty")
    assert not campaign.within_hours(NOW + timedelta(hours=9), (600, 1200), tz_name="Asia/Almaty")
    assert not campaign.within_hours(NOW, (600, 600), tz_name="Asia/Almaty")
    assert campaign.within_hours(NOW + timedelta(hours=12), (1320, 120), tz_name="Asia/Almaty")
    assert campaign.day_start(NOW, tz_name="Asia/Almaty") == datetime(2026, 10, 26, 19, 0, tzinfo=UTC)
    pauses = {campaign.pause_after(NOW + timedelta(seconds=step), 4, 9) for step in range(30)}
    assert min(pauses) == timedelta(minutes=4) and max(pauses) == timedelta(minutes=9)


def test_progress_rules() -> None:
    sent = NOW - timedelta(days=1)
    common = {"now": NOW, "sent_at": sent, "reminder_sent_at": None, "delivered": True, "undelivered": None,
              "remind_after": timedelta(days=3), "give_up_after": timedelta(days=4)}
    assert progress(CampaignStatus.SENT, CampaignSegment.FADED, BotFacts(), **common).decision is None
    assert progress(CampaignStatus.APPROVED, CampaignSegment.FADED, BotFacts(stop_word=True), **common).decision is None
    # Не доставлено — напоминать нечего.
    late = {**common, "sent_at": NOW - timedelta(days=4), "delivered": False}
    assert progress(CampaignStatus.SENT, CampaignSegment.FADED, BotFacts(), **late).remind is False
    # Напоминание так и не ушло — через срок «не ответил», а не запоздалое «напомню о себе».
    silent = {**common, "sent_at": NOW - timedelta(days=8)}
    assert progress(CampaignStatus.SENT, CampaignSegment.FADED, BotFacts(), **silent).decision.status is CampaignStatus.NO_ANSWER


def test_send_check_and_results() -> None:
    approved = NOW - timedelta(days=1)
    ok = send_check(CampaignStatus.APPROVED, CampaignSegment.FADED, BotFacts(), now=NOW, checked_at=None, approved_at=approved)
    assert ok is None
    ignored = send_check(CampaignStatus.APPROVED, CampaignSegment.FADED, BotFacts(ignored=True), now=NOW,
                         checked_at=None, approved_at=approved)
    assert ignored.status is CampaignStatus.EXCLUDED
    assert apply_result(CampaignResult.BOUGHT, CampaignStatus.NO_ANSWER, CampaignSegment.FADED).status is CampaignStatus.BOUGHT
    assert apply_result(CampaignResult.BOOKED, CampaignStatus.OPTED_OUT, CampaignSegment.FADED) is None
    assert apply_result(CampaignResult.DECLINED, CampaignStatus.APPROVED, CampaignSegment.FADED) is None


async def test_a_fresh_install_adds_the_new_column_to_an_old_table(tmp_path: Path) -> None:
    import sqlite3
    from contextlib import closing

    from app.storage import db as storage_db
    from app.storage.models import Base
    from app.storage.upgrade import upgrade_sqlite_data

    path = tmp_path / "bot.db"
    engine = storage_db.build_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    with closing(sqlite3.connect(path)) as conn, conn:
        # Как на сервере: правка 1 давно выполнена, таблица возврата — из прошлого выпуска.
        conn.execute("PRAGMA user_version = 1")
        conn.execute("ALTER TABLE campaign_contact DROP COLUMN draft_source")
        conn.execute("INSERT INTO campaign_contact (id, chat_id, channel, segment, status, check_required, created_at,"
                     " updated_at) VALUES (?, ?, 'whatsapp', 'faded', 'ready', 1, '2026-10-27', '2026-10-27')",
                     (uuid4().hex, PHONE))

    reports = []
    for _ in range(2):
        engine = storage_db.build_engine(f"sqlite+aiosqlite:///{path}")
        async with engine.begin() as connection:
            reports.append(await upgrade_sqlite_data(connection, timezone_name="Asia/Almaty"))
        await engine.dispose()

    assert reports[0] == ["в campaign_contact добавлены колонки: draft_source"] and reports[1] == []
    with closing(sqlite3.connect(path)) as conn, conn:
        assert conn.execute("SELECT draft_source FROM campaign_contact").fetchone() == (None,)


@pytest.mark.parametrize(("kind", "alerts"), [("campaign", 0), ("bot_reply", 1)])
async def test_an_unknown_number_alerts_only_outside_the_campaign(deps, monkeypatch, kind: str, alerts: int) -> None:
    """Старый список полон номеров без WhatsApp: «нужен звонок» на каждый — шум; на ответ бота — нет."""
    from app.types import ChannelKind, Language, OutboundKind, OutboundMessage, WazzupBadContactError
    from app.workers import tasks_outbound

    raised: list[str] = []

    async def alert(_deps, text: str, *, code: str) -> None:
        raised.append(code)

    monkeypatch.setattr(tasks_outbound, "_alert", alert)
    message = OutboundMessage(channel_id=CHANNEL_ID, channel=ChannelKind.WHATSAPP, chat_id=PHONE, lang=Language.RU,
                              kind=OutboundKind(kind), text="Здравствуйте!")

    await tasks_outbound._react_terminal(deps, message, WazzupBadContactError("bad", error_code="BAD_CONTACT"),
                                         code="badcontact", reason="needs_human", conversation_id=None)

    assert len(raised) == alerts


# --------------------------------------------------------------------------- #
# Живая рассылка 27.10.2026: «индивидуальные», «тарифы», «борьба», postMessageError
# --------------------------------------------------------------------------- #
async def test_the_analysts_next_step_is_not_an_instruction_for_the_model(deps) -> None:
    deps.llm.push(FakeTurn.answer(MODEL_DRAFT))
    contact = await _contact(deps, status=CampaignStatus.READY, text=None,
                             next_step="Предложить индивидуальные тренировки")

    await campaign.draft(deps, kb=deps.kb(), settings=deps.settings)

    assert "индивидуальн" not in deps.llm.requests[0].user_text.lower()
    assert "индивидуальн" in deps.llm.requests[0].system_instruction.lower()  # запрет — в инструкции
    assert (await _row(deps, contact)).draft_source == "model"


@pytest.mark.parametrize(
    "answer",
    [
        "Вы интересовались индивидуальными тренировками для сына. Подобрать удобное время?",
        "Вы ранее интересовались у нас занятиями для ребёнка. Подскажите, подходят ли вам наши тарифы?",
        "Вы писали нам насчёт занятий борьбой, а мы так и не ответили. Вопрос ещё актуален?",
        "Абонемент со скидкой ждёт вас. Подобрать зал?",
    ],
)
async def test_an_offer_the_template_does_not_make_falls_back_to_it(deps, answer: str) -> None:
    deps.llm.push(FakeTurn.answer(answer))
    contact = await _contact(deps, status=CampaignStatus.READY, text=None,
                             summary="Спрашивал про индивидуальные тренировки, тарифы и борьбу для сына, и про бокс")

    await campaign.draft(deps, kb=deps.kb(), settings=deps.settings)

    assert (await _row(deps, contact)).draft_source == "template"


async def test_a_contact_who_asked_only_about_another_sport_goes_to_a_human(deps) -> None:
    deps.llm.push(FakeTurn.answer("Вы спрашивали про занятия для сына. Подобрать зал рядом с домом?"))
    unchecked = await _contact(deps, status=CampaignStatus.READY, text=None, summary="Спрашивал про борьбу для сына")
    cleared = await _contact(deps, chat_id=OTHER, status=CampaignStatus.READY, text=None, outcome="write",
                             summary="Спрашивал про борьбу для сына")

    await campaign.draft(deps, kb=deps.kb(), settings=deps.settings)

    row = await _row(deps, unchecked)
    assert row.status == "needs_check" and "борьб" in row.status_reason
    # Человек посмотрел и решил писать — пишем, о борьбе не упоминая.
    assert (await _row(deps, cleared)).status == "draft"


async def test_a_reminder_does_not_stop_on_its_own_history_note(deps) -> None:
    contact = await _contact(deps, next_step="Предложить индивидуальные тренировки")
    assert await campaign.send_first(deps, contact.hex, now=NOW, kb=deps.kb(), settings=deps.settings,
                                     silent=frozenset()) is True
    async with deps.sessionmaker() as session:
        notes = [row.gemini_content for row in (await session.execute(sa.select(Message))).scalars()
                 if row.gemini_content and row.gemini_content.get("role") == "user"]
    assert notes and "индивидуальн" not in notes[0]["parts"][0]["text"].lower()


async def _failed_sends(deps: PipelineDeps, errors: list[str], *, updated: datetime) -> None:
    for index, error in enumerate(errors):
        contact = await _contact(deps, chat_id=f"7701555077{index}", status=CampaignStatus.UNDELIVERED,
                                 sent_at=updated - timedelta(minutes=5 * (len(errors) - index)))
        async with deps.sessionmaker() as session:
            session.add(OutboxMessage(
                conversation_id=None, crm_message_id=campaign.first_message_id(contact.hex), payload={"text": "x"},
                state="failed" if error else "sent", attempts=1, last_error=error or None, updated_at=updated,
            ))
            await session.commit()


@pytest.mark.parametrize(
    ("errors", "minutes_ago", "stopped"),
    [
        (["postmessageerror: Wazzup 400: postMessageError: x"] * 3, 2, True),
        (["badcontact: Wazzup 400: BAD_CONTACT"] * 3, 2, False),           # номера без WhatsApp — не беда канала
        (["postmessageerror: Wazzup 400: postMessageError: x"] * 3, 60, False),  # старая серия
        (["postmessageerror: x", "", "postmessageerror: x"], 2, False),     # одно дошло — серии нет
    ],
)
async def test_a_series_of_failures_switches_the_campaign_off(deps, admin_db: Path, monkeypatch, errors, minutes_ago,
                                                             stopped) -> None:
    from app.notify import manager

    monkeypatch.setattr(manager, "manager_target", lambda *a, **k: (manager.ChannelKind.WHATSAPP, CHANNEL_ID, "77000000077"))
    _campaign_setting(admin_db, "on")
    await _failed_sends(deps, errors, updated=NOW - timedelta(minutes=minutes_ago))
    await _sent(deps)  # хоть один в работе — иначе проход ничего не смотрит

    await _track(deps, now=NOW)

    assert (_campaign_setting(admin_db) == "off") is stopped
    alerts = await _alerts(deps)
    assert len(alerts) == (1 if stopped else 0)
    if stopped:
        assert "подряд не доставлены" in alerts[0] and "postmessageerror" in alerts[0]


async def test_an_unknown_refusal_keeps_the_answer_of_wazzup(deps) -> None:
    contact, _ = await _sent(deps, outbox_state="failed", error="postmessageerror: Wazzup 400: postMessageError: chat is busy")

    await _track(deps, now=NOW)

    row = await _row(deps, contact)
    assert row.status == "undelivered" and "postMessageError: chat is busy" in row.status_reason


async def test_a_campaign_refusal_raises_no_alert_per_message(deps, monkeypatch) -> None:
    from app.types import ChannelKind, Language, OutboundKind, OutboundMessage, WazzupError, WazzupSpamError
    from app.workers import tasks_outbound

    raised: list[str] = []

    async def alert(_deps, text: str, *, code: str) -> None:
        raised.append(code)

    async def stop(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(tasks_outbound, "_alert", alert)
    monkeypatch.setattr(tasks_outbound, "_stop_channel", stop)
    message = OutboundMessage(channel_id=CHANNEL_ID, channel=ChannelKind.WHATSAPP, chat_id=PHONE, lang=Language.RU,
                              kind=OutboundKind.CAMPAIGN, text="Здравствуйте!")
    await tasks_outbound._react_terminal(deps, message, WazzupError("x", error_code="postMessageError"),
                                         code="postmessageerror", reason="fatal", conversation_id=None)
    assert raised == []
    await tasks_outbound._react_terminal(deps, message, WazzupSpamError("x", error_code="MESSAGES_IS_SPAM"),
                                         code="messagesisspam", reason="needs_human", conversation_id=None)
    assert raised == ["wazzup_spam"]
