"""Запись до конца: «Да» на предложение бота, одно время у секции, эскалация без просьбы.

Живая переписка владельца 11.09.2026: бот предложил «Записать Айназарова Али на
ближайшее занятие в понедельник?», клиент ответил «Да» — и получил «Здесь лучше
ответит администратор». Воспроизведение показало больше: на «Да» после
предложения записи, которое отправляет код, модель звала администратора, потому
что не видела, на что клиент согласился.

Номера здесь вымышленные: настоящий номер для заявок живёт в настройках владельца
и в публичный репозиторий не попадает.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from app.admin.runtime_settings import RuntimeSettings
from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.kb.agreement import with_agreement
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
from app.types import ChannelKind, IntentHint, Language, ToolContext

from tests.conftest import RecordingQueue, RecordingServices, webhook_payload

#: Бокс и кикбоксинг Пн·Ср·Пт в 09:00 и в 19:00.
RAHAT = "center_kairbekova_24"
#: Бокс и кикбоксинг только Пн·Ср·Пт в 19:00.
KEME = "polevaya_7_3"
LEADS_NUMBER = "77000000077"
PROPOSAL = (
    "Занятия по боксу проходят по понедельникам, средам и пятницам в 19:00. "
    "Записать Айназарова Али на ближайшее занятие в понедельник?"
)


def _ctx(kb, *texts: str, intents: tuple[IntentHint, ...] = ()) -> ToolContext:
    return ToolContext(
        conversation_id=uuid4(), conv_key="conv:wa", channel=ChannelKind.WHATSAPP, channel_id="wa",
        chat_id="7701", lang=Language.RU, kb=kb, kb_hash=kb.kb_hash,
        # Четверг 10.09.2026, 12:00 по Алматы.
        now=datetime(2026, 9, 10, 7, 0, tzinfo=UTC), correlation_id="test", services=RecordingServices(),
        intents=intents, client_texts=texts,
    )


def _slot(ctx: ToolContext) -> tuple[int, int]:
    slot = ctx.services.leads[-1].trial_slot.astimezone(ZoneInfo("Asia/Almaty"))
    return slot.weekday(), slot.hour


async def _book(ctx: ToolContext, gym: str, **args):
    from app.tools.booking import create_trial_lead

    return await create_trial_lead(
        ctx, child_name="Айназаров Али", child_age=8, gym_id=gym, parent_agreed=True, **args
    )


# --------------------------------------------------------------------------- #
# Инструмент записи
# --------------------------------------------------------------------------- #
async def test_yes_to_the_bots_concrete_offer_books_that_session(kb) -> None:
    """Скриншот владельца: «Да» на «в понедельник в 19:00» — это выбор понедельника и 19:00."""
    agreed = with_agreement("Да", PROPOSAL, kb.lexicon.agreement)
    ctx = _ctx(kb, "Айназаров Али, 8 лет", "Бокс", agreed)

    result = await _book(ctx, RAHAT, session_time="19:00", session_day="mon", discipline="boxing")

    assert result.data["booked"] is True, result.data
    assert _slot(ctx) == (0, 19)


async def test_yes_to_several_options_is_not_a_choice(kb) -> None:
    """«Утром в 09:00 или вечером в 19:00?» — «Да» ничего не выбирает."""
    agreed = with_agreement("Да", "Удобнее утром в 09:00 или вечером в 19:00?", kb.lexicon.agreement)
    ctx = _ctx(kb, "Айназаров Али, 8 лет", "Бокс", agreed)

    result = await _book(ctx, RAHAT, session_time="19:00", discipline="boxing")

    assert result.data["booked"] is False
    assert "need_time" in result.data["needs"], result.data


async def test_days_listed_in_the_schedule_are_not_a_chosen_day(kb) -> None:
    """Перечисление дней расписания — не выбор дня: запись идёт на ближайшее занятие."""
    agreed = with_agreement(
        "Да", "Бокс проходит по понедельникам, средам и пятницам в 19:00. Записать?", kb.lexicon.agreement
    )
    ctx = _ctx(kb, "Айназаров Али, 8 лет", "Бокс", agreed)

    result = await _book(ctx, RAHAT, session_time="19:00", session_day="wed", discipline="boxing")

    assert result.data["booked"] is True, result.data
    assert _slot(ctx) == (4, 19), "ближайший бокс в 19:00 после четверга — пятница"


async def test_single_time_for_the_section_needs_no_extra_question(kb) -> None:
    """В «Кеме» бокс только в 19:00 — спрашивать время значит тянуть запись."""
    ctx = _ctx(kb, "Айназаров Али, 8 лет, на бокс")

    result = await _book(ctx, KEME, discipline="boxing")

    assert result.data["booked"] is True, result.data
    assert _slot(ctx) == (4, 19)


async def test_two_times_for_the_section_are_still_asked(kb) -> None:
    ctx = _ctx(kb, "Айназаров Али, 8 лет, на бокс")

    result = await _book(ctx, RAHAT, discipline="boxing")

    assert result.data["booked"] is False
    assert "need_time" in result.data["needs"], result.data


# --------------------------------------------------------------------------- #
# Эскалация
# --------------------------------------------------------------------------- #
async def test_handover_without_a_request_for_a_human_is_refused(kb) -> None:
    """Воспроизведение: на «Да» модель звала администратора «по просьбе клиента»."""
    from app.tools.escalation import escalate_to_manager

    ctx = _ctx(kb, "Да")

    result = await escalate_to_manager(
        ctx, reason="user_request", question_summary="Родитель ответил «Да» на неясный контекст"
    )

    assert not result.ok
    assert not ctx.services.pauses and not ctx.services.cards


async def test_real_request_for_a_human_is_handed_over(kb) -> None:
    from app.tools.escalation import escalate_to_manager

    ctx = _ctx(kb, "Позовите администратора, пожалуйста", intents=(IntentHint.MANAGER,))

    result = await escalate_to_manager(ctx, reason="user_request", question_summary="Просит администратора")

    assert result.ok
    assert ctx.services.pauses and ctx.services.cards


async def test_request_for_a_human_earlier_in_the_dialog_counts(kb) -> None:
    from app.tools.escalation import escalate_to_manager

    ctx = _ctx(kb, "Можно поговорить с менеджером?", "Да")

    result = await escalate_to_manager(ctx, reason="user_request", question_summary="Просил менеджера")

    assert result.ok


async def test_other_reasons_are_not_affected(kb) -> None:
    from app.tools.escalation import escalate_to_manager

    ctx = _ctx(kb, "У ребёнка астма, можно ли заниматься?")

    result = await escalate_to_manager(ctx, reason="medical", question_summary="Астма")

    assert result.ok


# --------------------------------------------------------------------------- #
# Пайплайн
# --------------------------------------------------------------------------- #
async def _deps(kb, state, sessionmaker, settings, llm, runtime: RuntimeSettings | None = None) -> PipelineDeps:
    kb_loader.swap(kb)
    extra = {"runtime": (lambda: runtime)} if runtime is not None else {}
    return PipelineDeps(
        sessionmaker=sessionmaker, state=state, llm=llm, kb=kb_loader.get_snapshot,
        queue=RecordingQueue(), settings=settings, **extra,
    )


async def test_yes_after_the_booking_offer_reaches_the_model_with_context(kb, state, sessionmaker, settings) -> None:
    """Модель видит, на что клиент согласился, и помнит, что сама предложила запись."""
    llm = FakeLLMClient([])
    deps = await _deps(kb, state, sessionmaker, settings, llm)
    chat = "77015557001"
    offer = kb.text("funnel.book_trial", Language.RU)

    llm.reset([FakeTurn.tool(FakeCall("get_schedule", {"gym_id": RAHAT})), FakeTurn.answer("Зал в самом центре.")])
    first = await process_inbound(deps, webhook_payload(f"{chat}-1", "Какое расписание у зала у Рахата?", chat_id=chat))
    assert offer in "\n".join(out.text or "" for d in first for out in d.outbound), "предложение записи не ушло"

    llm.reset([FakeTurn.answer("Подскажите фамилию, имя и возраст ребёнка?")])
    await process_inbound(deps, webhook_payload(f"{chat}-2", "Да", chat_id=chat))

    request = [item for item in llm.requests if item.user_text][-1]
    assert "согласие на предложение бота" in request.user_text and offer in request.user_text, request.user_text
    said = [
        part.get("text") or ""
        for item in request.history if item.get("role") == "model"
        for part in item.get("parts") or []
    ]
    assert any(offer in text for text in said), f"модель не помнит своё предложение записи: {said}"


def test_number_for_leads_from_crm_overrides_the_server_setting(settings) -> None:
    applied = RuntimeSettings.from_values({"lead_notify_target": "+7 700 000 00 77"}).apply_to(settings)

    assert applied.manager_notify_target == LEADS_NUMBER
    unchanged = RuntimeSettings.from_values({}).apply_to(settings)
    assert unchanged.manager_notify_target == settings.manager_notify_target


async def test_messages_from_the_number_for_leads_get_no_answer(kb, state, sessionmaker, settings) -> None:
    """Администратор ответил «принял» в чате с заявками — бот не начинает с ним продажу."""
    runtime = RuntimeSettings.from_values({"lead_notify_target": "+7 700 000 00 77"})
    deps = await _deps(kb, state, sessionmaker, settings, FakeLLMClient([FakeTurn.answer("Здравствуйте!")]), runtime)

    decisions = await process_inbound(deps, webhook_payload("lead-1", "Принял, позвоню", chat_id=LEADS_NUMBER))

    assert [d.reason for d in decisions] == ["ignored_number"]


async def _cards_to(sessionmaker, number: str) -> list[str]:
    from app.storage.models import OutboxMessage

    async with sessionmaker() as db:
        payloads = (await db.execute(sa.select(OutboxMessage.payload))).scalars().all()
    return [str(p.get("text") or "") for p in payloads if p and p.get("chat_id") == number]


async def test_booked_lead_card_goes_to_the_number_for_leads(kb, state, sessionmaker, settings) -> None:
    channel = settings.model_copy(update={"wazzup_channel_id_whatsapp": "00000000-0000-4000-8000-000000000002"})
    runtime = RuntimeSettings.from_values({"lead_notify_target": "+7 700 000 00 77"})
    llm = FakeLLMClient([
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Айназаров Али", "child_age": 8, "gym_id": RAHAT, "parent_agreed": True,
            "session_time": "19:00", "discipline": "boxing",
        })),
        FakeTurn.answer("Готово"),
    ])
    deps = await _deps(kb, state, sessionmaker, channel, llm, runtime)

    await process_inbound(deps, webhook_payload("lead-2", "Айназаров Али, 8 лет, на бокс в 19:00", chat_id="77015557002"))

    cards = await _cards_to(sessionmaker, LEADS_NUMBER)
    assert any("Айназаров Али" in card and "Бокс" in card for card in cards), cards


async def test_lead_cards_can_be_switched_off_in_crm(kb, state, sessionmaker, settings) -> None:
    channel = settings.model_copy(update={"wazzup_channel_id_whatsapp": "00000000-0000-4000-8000-000000000002"})
    runtime = RuntimeSettings.from_values({"lead_notify_target": "+7 700 000 00 77", "lead_notify": "off"})
    llm = FakeLLMClient([
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Айназаров Али", "child_age": 8, "gym_id": RAHAT, "parent_agreed": True,
            "session_time": "19:00", "discipline": "boxing",
        })),
        FakeTurn.answer("Готово"),
    ])
    deps = await _deps(kb, state, sessionmaker, channel, llm, runtime)

    await process_inbound(deps, webhook_payload("lead-3", "Айназаров Али, 8 лет, на бокс в 19:00", chat_id="77015557003"))

    assert not await _cards_to(sessionmaker, LEADS_NUMBER)


async def test_echo_in_the_chat_for_leads_creates_no_client(kb, state, sessionmaker, settings) -> None:
    """Эхо карточки в чате заявок — не «оператор вошёл» и не новый клиент в CRM."""
    runtime = RuntimeSettings.from_values({"lead_notify_target": "+7 700 000 00 77"})
    deps = await _deps(kb, state, sessionmaker, settings, FakeLLMClient([]), runtime)

    decisions = await process_inbound(
        deps, webhook_payload("lead-echo-1", "НОВАЯ ЗАПИСЬ НА ПРОБНОЕ", chat_id=LEADS_NUMBER, is_echo=True)
    )

    assert [d.reason for d in decisions] == ["ignored_number"]


# --------------------------------------------------------------------------- #
# Скриншот 11.09.2026: запись не доходила до конца
# --------------------------------------------------------------------------- #
async def test_day_from_the_bots_previous_message_is_not_blocked(kb, state, sessionmaker, settings) -> None:
    """Бот назвал дни ходом раньше — на «Бокс» повторил «в понедельник» без инструмента."""
    llm = FakeLLMClient([])
    deps = await _deps(kb, state, sessionmaker, settings, llm)
    chat = "77015557020"

    llm.reset([FakeTurn.tool(FakeCall("get_schedule", {"gym_id": RAHAT})), FakeTurn.answer("Какая секция интересует?")])
    await process_inbound(deps, webhook_payload(f"{chat}-1", "Какое расписание у зала у Рахата?", chat_id=chat))
    llm.reset([FakeTurn.answer("Бокс идёт в понедельник, среду и пятницу. Записать на понедельник?")])
    second = await process_inbound(deps, webhook_payload(f"{chat}-2", "Бокс", chat_id=chat))

    assert not [d.postcheck_fail for d in second if d.postcheck_fail]
    assert "Записать на понедельник?" in "\n".join(out.text or "" for d in second for out in d.outbound)


async def test_age_is_optional_and_asked_first(kb) -> None:
    """Возраст неизвестен — модель его не выдумывает, а спрашивает, и только его."""
    from app.tools.booking import create_trial_lead

    ctx = _ctx(kb, "Айназаров Али, на бокс")

    result = await create_trial_lead(ctx, child_name="Айназаров Али", gym_id=RAHAT, parent_agreed=True, discipline="boxing")

    assert result.data["booked"] is False
    assert result.data["needs"][0] == "need_age", result.data
    assert any("Спроси только это" in caveat for caveat in result.caveats)
    assert not any("session_time" in caveat for caveat in result.caveats)


async def test_invented_age_outside_the_range_is_asked_not_escalated(kb) -> None:
    from app.tools.booking import create_trial_lead

    ctx = _ctx(kb, "Айназаров Али, на бокс")

    result = await create_trial_lead(
        ctx, child_name="Айназаров Али", child_age=2, gym_id=RAHAT, parent_agreed=True, discipline="boxing"
    )

    assert result.ok and "need_age" in result.data["needs"], result.data
    assert not ctx.services.pauses


async def test_empty_answer_after_the_confirmation_is_not_a_failure(kb, state, sessionmaker, settings) -> None:
    """Подтверждение ушло, модель промолчала — это не «сбой модели» и не пауза."""
    llm = FakeLLMClient([
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Айназаров Али", "child_age": 8, "gym_id": KEME, "parent_agreed": True,
            "discipline": "boxing",
        })),
        FakeTurn.answer(""),
    ])
    deps = await _deps(kb, state, sessionmaker, settings, llm)

    decisions = await process_inbound(deps, webhook_payload("empty-1", "Айназаров Али, 8 лет, на бокс", chat_id="77015557021"))

    assert [d.action.value for d in decisions] == ["reply"], [d.reason for d in decisions]
    client = "\n".join(out.text or "" for d in decisions for out in d.outbound)
    assert "Мы записали вас" in client and "сбой" not in client
    assert not [c for d in decisions for c in d.manager_cards if c.kind.value == "escalation"]


async def test_handover_with_no_data_after_the_clients_yes_is_refused(kb) -> None:
    from app.tools.escalation import escalate_to_manager

    ctx = _ctx(kb, "Айназаров Али, 8 лет", with_agreement("Да", PROPOSAL, kb.lexicon.agreement))

    result = await escalate_to_manager(ctx, reason="no_data", question_summary="Непонятно, что делать дальше")

    assert not result.ok
    assert not ctx.services.pauses and not ctx.services.cards


async def test_yes_to_passing_the_question_on_is_handed_over(kb) -> None:
    from app.tools.escalation import escalate_to_manager

    agreed = with_agreement("Да", "Способы оплаты подтвердит администратор. Передать ваш вопрос ему?", kb.lexicon.agreement)
    ctx = _ctx(kb, "Как можно оплатить?", agreed)

    result = await escalate_to_manager(ctx, reason="no_data", question_summary="Способы оплаты")

    assert result.ok


async def test_blank_model_answer_is_not_stored_in_history(kb, state, sessionmaker, settings) -> None:
    """Пустой ответ модели в истории ломает следующий запрос: пустых частей API не принимает."""
    from types import SimpleNamespace

    from app.core.pipeline import _save_history
    from app.storage import repo_message
    from app.storage.models import Conversation

    deps = await _deps(kb, state, sessionmaker, settings, FakeLLMClient([FakeTurn.answer("Здравствуйте!")]))
    await process_inbound(deps, webhook_payload("blank-1", "Сколько стоит?", chat_id="77015557022"))
    turn = [
        {"role": "user", "parts": [{"text": "<user_message>\nНу\n</user_message>"}]},
        {"role": "model", "parts": [{"text": ""}]},
    ]

    async with sessionmaker() as db:
        conv = (await db.execute(sa.select(Conversation).where(Conversation.conv_key.like("%77015557022")))).scalars().first()
        await _save_history(db, conv, [], SimpleNamespace(history=turn))
        await db.commit()
        stored = await repo_message.load_history(db, conv.id, max_turns=50)

    blank = [
        item for item in stored
        if item.get("role") == "model" and not "".join(str(p.get("text") or "") for p in item.get("parts") or []).strip()
        and not any(set(p) - {"text"} for p in item.get("parts") or [])
    ]
    assert not blank, stored


def test_age_is_not_a_required_booking_argument() -> None:
    """Обязательный возраст модель выдумывала, лишь бы вызвать запись."""
    from app.tools.registry import RAW_TOOL_SPECS

    spec = next(spec for spec in RAW_TOOL_SPECS if spec.name == "create_trial_lead")

    assert "child_age" not in spec.parameters["required"]
    assert "child_age" in spec.parameters["properties"]


async def test_bot_word_administrator_in_the_offer_is_not_a_request_for_a_human(kb) -> None:
    """«Время подберёт администратор. Записать?» — «Да» не просьба позвать человека."""
    from app.tools.escalation import escalate_to_manager

    agreed = with_agreement("Да", "Время подберёт администратор. Записать на понедельник?", kb.lexicon.agreement)
    ctx = _ctx(kb, "Айназаров Али, 8 лет", agreed, intents=(IntentHint.MANAGER,))

    result = await escalate_to_manager(ctx, reason="user_request", question_summary="Согласился на запись")

    assert not result.ok
    assert not ctx.services.pauses


async def test_yes_to_a_handover_question_counts_as_a_request(kb) -> None:
    from app.tools.escalation import escalate_to_manager

    agreed = with_agreement("Да", "Этот вопрос решает администратор. Передать ему?", kb.lexicon.agreement)
    ctx = _ctx(kb, "Можно перенести абонемент?", agreed)

    result = await escalate_to_manager(ctx, reason="user_request", question_summary="Перенос абонемента")

    assert result.ok


async def test_gym_number_does_not_count_as_the_childs_age(kb, state, sessionmaker, settings) -> None:
    """«7» из списка — «Полевая 7/3, напротив 5-й поликлиники». Возраст 7 это не называет."""
    llm = FakeLLMClient([])
    deps = await _deps(kb, state, sessionmaker, settings, llm)
    chat = "77015557030"

    await process_inbound(deps, webhook_payload(f"{chat}-1", "Здравствуйте", chat_id=chat))
    await process_inbound(deps, webhook_payload(f"{chat}-2", "2", chat_id=chat))
    llm.reset([FakeTurn.answer("Хороший зал.")])
    await process_inbound(deps, webhook_payload(f"{chat}-3", "7", chat_id=chat))
    llm.reset([
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Айназаров Али", "child_age": 7, "gym_id": KEME, "parent_agreed": True,
        })),
        FakeTurn.answer("Сколько лет ребёнку?"),
    ])
    decisions = await process_inbound(deps, webhook_payload(f"{chat}-4", "Айназаров Али", chat_id=chat))

    booking = [inv for d in decisions for inv in d.invocations if inv.name == "create_trial_lead"]
    assert booking and "need_age" in booking[0].result.data.get("needs", []), booking[0].result.data


async def test_greeting_and_menu_answer_stay_in_the_models_history(kb, state, sessionmaker, settings) -> None:
    """Ответы кода хранятся парой с репликой клиента — обрезка истории их не выбрасывает."""
    from app.core.session import trim_history
    from app.storage import repo_message
    from app.storage.models import Conversation

    deps = await _deps(kb, state, sessionmaker, settings, FakeLLMClient([]))
    chat = "77015557031"
    await process_inbound(deps, webhook_payload(f"{chat}-1", "Здравствуйте", chat_id=chat))
    await process_inbound(deps, webhook_payload(f"{chat}-2", "2", chat_id=chat))

    async with sessionmaker() as db:
        conv = (await db.execute(sa.select(Conversation).where(Conversation.conv_key.like(f"%{chat}")))).scalars().first()
        stored = await repo_message.load_history(db, conv.id, max_turns=50)

    assert [item.get("role") for item in stored] == ["user", "model", "user", "model"], stored
    assert trim_history(stored, max_turns=10) == stored


async def test_repeated_identical_call_is_not_run_again(kb, state, sessionmaker, settings) -> None:
    """Повтор того же вызова в одном ходе — прежний результат и подсказка, а не новый виток."""
    args = {"child_name": "Айназаров Али", "child_age": 8, "gym_id": RAHAT, "parent_agreed": True, "discipline": "boxing"}
    llm = FakeLLMClient([
        FakeTurn.tool(FakeCall("create_trial_lead", args)),
        FakeTurn.tool(FakeCall("create_trial_lead", args)),
        FakeTurn.answer("Во сколько удобнее: в 09:00 или в 19:00?"),
    ])
    deps = await _deps(kb, state, sessionmaker, settings, llm)

    decisions = await process_inbound(deps, webhook_payload("rep-1", "Айназаров Али, 8 лет, на бокс", chat_id="77015557032"))

    calls = [inv for d in decisions for inv in d.invocations if inv.name == "create_trial_lead"]
    assert len(calls) == 2
    assert not any("уже был в этом ходе" in caveat for caveat in calls[0].result.caveats)
    assert any("уже был в этом ходе" in caveat for caveat in calls[1].result.caveats)


async def test_inconvenient_time_in_reply_to_an_offer_is_a_choice(kb, state, sessionmaker, settings) -> None:
    """«В понедельник неудобно в 19, можно в среду?» на предложение записи — выбор времени."""
    llm = FakeLLMClient([])
    deps = await _deps(kb, state, sessionmaker, settings, llm)
    chat = "77015557033"

    llm.reset([
        FakeTurn.tool(FakeCall("get_schedule", {"gym_id": RAHAT})),
        FakeTurn.answer("Бокс идёт в 19:00 по понедельникам. Записать на понедельник в 19:00?"),
    ])
    await process_inbound(deps, webhook_payload(f"{chat}-1", "Какое расписание у зала у Рахата?", chat_id=chat))
    llm.reset([FakeTurn.answer("В среду тоже есть бокс в 19:00. Записать на среду?")])
    decisions = await process_inbound(deps, webhook_payload(f"{chat}-2", "В понедельник неудобно в 19, можно в среду?", chat_id=chat))

    assert [d.action.value for d in decisions] == ["reply"], [d.reason for d in decisions]


async def test_yes_with_a_time_keeps_the_offered_day(kb) -> None:
    """«Да, в 19» на «в понедельник в 19:00?» — понедельник, а не ближайшая пятница."""
    agreed = with_agreement("Да, в 19", PROPOSAL, kb.lexicon.agreement)
    ctx = _ctx(kb, "Айназаров Али, 8 лет", "Бокс", agreed)

    result = await _book(ctx, RAHAT, session_time="19:00", session_day="mon", discipline="boxing")

    assert result.data["booked"] is True, result.data
    assert _slot(ctx) == (0, 19)


async def test_name_said_before_the_history_was_trimmed_still_counts(kb, state, sessionmaker, settings) -> None:
    """Имя и возраст назвали три хода назад — обрезка истории для модели их не отменяет."""
    short = settings.model_copy(update={"llm_history_turns": 1})
    llm = FakeLLMClient([])
    deps = await _deps(kb, state, sessionmaker, short, llm)
    chat = "77015557034"

    llm.reset([FakeTurn.answer("На какую секцию записать?")])
    await process_inbound(deps, webhook_payload(f"{chat}-1", "Айназаров Али, 8 лет", chat_id=chat))
    llm.reset([FakeTurn.answer("Во сколько удобнее: в 09:00 или в 19:00?")])
    await process_inbound(deps, webhook_payload(f"{chat}-2", "Бокс", chat_id=chat))
    llm.reset([
        FakeTurn.tool(FakeCall("create_trial_lead", {
            "child_name": "Айназаров Али", "child_age": 8, "gym_id": RAHAT, "parent_agreed": True,
            "discipline": "boxing", "session_time": "19:00",
        })),
        FakeTurn.answer("Готово"),
    ])
    decisions = await process_inbound(deps, webhook_payload(f"{chat}-3", "В 19:00", chat_id=chat))

    booking = [inv for d in decisions for inv in d.invocations if inv.name == "create_trial_lead"]
    assert booking and booking[0].result.data.get("booked") is True, booking[0].result.data


# --------------------------------------------------------------------------- #
# Карточки заявок в WhatsApp администратора
# --------------------------------------------------------------------------- #
async def test_one_card_per_booking_and_a_change_card_on_reschedule(kb, state, sessionmaker, settings) -> None:
    """Повтор записи — без новой карточки; перенос времени — отдельная «изменение»."""
    channel = settings.model_copy(update={"wazzup_channel_id_whatsapp": "00000000-0000-4000-8000-000000000002"})
    runtime = RuntimeSettings.from_values({"lead_notify_target": "+7 700 000 00 77"})
    llm = FakeLLMClient([])
    deps = await _deps(kb, state, sessionmaker, channel, llm, runtime)
    chat = "77015557040"
    booking = {"child_name": "Айназаров Али", "child_age": 8, "gym_id": RAHAT, "parent_agreed": True, "discipline": "boxing"}

    turns = (
        ("Айназаров Али, 8 лет, на бокс в 19:00", "19:00"),
        ("Да, всё верно, на бокс в 19:00", "19:00"),
        ("Давайте лучше в 09:00", "09:00"),
    )
    for index, (text, time) in enumerate(turns, 1):
        llm.reset([FakeTurn.tool(FakeCall("create_trial_lead", {**booking, "session_time": time})), FakeTurn.answer("Готово")])
        await process_inbound(deps, webhook_payload(f"{chat}-{index}", text, chat_id=chat))

    cards = await _cards_to(sessionmaker, LEADS_NUMBER)
    assert sum("НОВАЯ ЗАПИСЬ" in card for card in cards) == 1, cards
    assert sum("ИЗМЕНЕНИЕ ЗАПИСИ" in card for card in cards) == 1, cards
    first = next(card for card in cards if "НОВАЯ ЗАПИСЬ" in card)
    assert "+7 701 555 70 40" in first and "Язык: русский" in first and "WhatsApp" in first, first
    assert not any(": —" in card for card in cards), "пустые поля попали в карточку"


async def test_operator_entering_keeps_the_lead_card(kb, state, sessionmaker, settings, monkeypatch) -> None:
    """Человек вошёл в диалог посреди хода: ответ клиенту отменяется, карточка — нет."""
    from datetime import timedelta
    from importlib import import_module

    from app.core.pipeline import _Services, _withhold_if_operator_took_over
    from app.storage.models import Conversation, OutboxMessage
    from app.types import OutboundKind, OutboundMessage

    deps = await _deps(kb, state, sessionmaker, settings, FakeLLMClient([FakeTurn.answer("Здравствуйте!")]))
    chat = "77015557041"
    await process_inbound(deps, webhook_payload(f"{chat}-1", "Сколько стоит?", chat_id=chat))

    async def operator_just_now(*_args, **_kwargs):
        return datetime.now(UTC) + timedelta(minutes=5)

    monkeypatch.setattr(import_module("app.core.pause"), "operator_last_seen", operator_just_now)
    now = datetime.now(UTC)
    async with sessionmaker() as db:
        conv = (await db.execute(sa.select(Conversation).where(Conversation.conv_key.like(f"%{chat}")))).scalars().first()
        services = _Services(deps=deps, session=db, conv=conv, turn_started_at=now)
        common = dict(conversation_id=conv.id, channel_id="school", channel=ChannelKind.WHATSAPP, lang=Language.RU)
        reply_id = await services.enqueue_outbound(OutboundMessage(**common, chat_id=chat, kind=OutboundKind.BOT_REPLY, text="Готово"))
        card_id = await services._enqueue(
            OutboundMessage(**common, chat_id=LEADS_NUMBER, kind=OutboundKind.MANAGER_CARD, text="НОВАЯ ЗАПИСЬ"),
            to_client=False,
        )

        withheld = await _withhold_if_operator_took_over(deps, db, services, conv, now=now)
        await db.commit()
        states = dict((await db.execute(sa.select(OutboxMessage.id, OutboxMessage.state))).all())

    assert withheld is True
    assert states[reply_id] == "skipped"
    assert states[card_id] != "skipped", states
    assert [entry[0] for entry in services.outbox] == [card_id], "карточка обязана уйти сразу, а не ждать сметку"


def test_card_links_to_the_dialog_only_on_a_public_https_address(kb, settings, monkeypatch) -> None:
    """Ссылка на переписку в CRM — только если адрес бота настоящий, https."""
    import app.tools.booking as booking
    from app.types import LeadDraft

    ctx = _ctx(kb, "Айназаров Али, 8 лет")
    draft = LeadDraft(child_name="Айназаров Али", child_age=8, gym_id=RAHAT, channel_user="77015557042")

    monkeypatch.setattr(booking, "get_settings", lambda: settings.model_copy(update={"public_base_url": "https://bot.example"}))
    card = booking._card_text(ctx, draft, kb.gym(RAHAT))
    assert f"Переписка: https://bot.example/crm/clients/{ctx.conversation_id}" in card, card

    monkeypatch.setattr(booking, "get_settings", lambda: settings.model_copy(update={"public_base_url": "http://localhost:8000"}))
    assert "Переписка" not in booking._card_text(ctx, draft, kb.gym(RAHAT))


# --------------------------------------------------------------------------- #
# Перепроверка 12.09.2026
# --------------------------------------------------------------------------- #
def test_owner_settings_are_cached_and_survive_a_failed_read(tmp_path, monkeypatch) -> None:
    """Одно открытие admin.db в несколько секунд; сбой чтения не откатывает настройки."""
    import app.admin.runtime_settings as runtime_settings

    now = [100.0]
    calls: list[int] = []
    real = runtime_settings.load_runtime_settings

    def counting(path):
        calls.append(1)
        return real(path)

    monkeypatch.setattr(runtime_settings, "load_runtime_settings", counting)
    load = runtime_settings.cached_runtime_settings(tmp_path / "admin.db", ttl_seconds=3.0, clock=lambda: now[0])

    first = load()
    load()
    assert len(calls) == 1, "в пределах кеша база открывается один раз"

    def broken(path):
        raise OSError("database is locked")

    monkeypatch.setattr(runtime_settings, "load_runtime_settings", broken)
    now[0] += 5
    assert load() is first, "при сбое остаётся последнее прочитанное значение"


async def test_age_written_in_this_message_is_not_asked_again(kb) -> None:
    """Модель не передала возраст, а родитель написал «8 лет» — переспрашивать нельзя."""
    from dataclasses import replace

    from app.tools.booking import create_trial_lead
    from app.types import LeadDraft

    ctx = replace(_ctx(kb, "Айназаров Али, 8 лет, на бокс"), lead_draft=LeadDraft(child_age=8))

    result = await create_trial_lead(ctx, child_name="Айназаров Али", gym_id=KEME, parent_agreed=True, discipline="boxing")

    assert result.data["booked"] is True, result.data


async def test_second_booking_in_one_turn_is_a_change_not_a_second_new_card(kb) -> None:
    """Две записи с разным временем за один ход: «новая», затем «изменение»."""
    from app.tools.booking import create_trial_lead

    ctx = _ctx(kb, "Айназаров Али, 8 лет, на бокс в 19:00, а лучше в 09:00")
    args = dict(child_name="Айназаров Али", child_age=8, gym_id=RAHAT, parent_agreed=True, discipline="boxing")

    await create_trial_lead(ctx, session_time="19:00", **args)
    await create_trial_lead(ctx, session_time="09:00", **args)
    await create_trial_lead(ctx, session_time="09:00", **args)

    texts = [card.text for card in ctx.services.cards]
    assert sum("НОВАЯ ЗАПИСЬ" in text for text in texts) == 1, texts
    assert sum("ИЗМЕНЕНИЕ ЗАПИСИ" in text for text in texts) == 1, texts


async def test_name_said_long_ago_is_not_an_invention(kb, state, sessionmaker, settings) -> None:
    """Фамилию назвали до обрезки истории — «Записать Айназарова Али?» не выдумка."""
    short = settings.model_copy(update={"llm_history_turns": 1})
    llm = FakeLLMClient([])
    deps = await _deps(kb, state, sessionmaker, short, llm)
    chat = "77015557050"

    llm.reset([FakeTurn.answer("На какую секцию записать?")])
    await process_inbound(deps, webhook_payload(f"{chat}-1", "Айназаров Али, 8 лет", chat_id=chat))
    llm.reset([FakeTurn.answer("Какой зал вам ближе?")])
    await process_inbound(deps, webhook_payload(f"{chat}-2", "Бокс", chat_id=chat))
    llm.reset([FakeTurn.answer("Записать Айназарова Али на пробное занятие?")])
    third = await process_inbound(deps, webhook_payload(f"{chat}-3", "Любой в центре", chat_id=chat))

    assert not [d.postcheck_fail for d in third if d.postcheck_fail], [d.reason for d in third]


async def test_empty_answer_after_a_schedule_card_is_not_a_failure(kb, state, sessionmaker, settings) -> None:
    """Модель промолчала, но расписание ушло — это ответ, а не «сбой модели»."""
    llm = FakeLLMClient([FakeTurn.tool(FakeCall("get_schedule", {"gym_id": RAHAT})), FakeTurn.answer("")])
    deps = await _deps(kb, state, sessionmaker, settings, llm)

    decisions = await process_inbound(deps, webhook_payload("empty-card-1", "Расписание у Рахата?", chat_id="77015557051"))

    assert [d.action.value for d in decisions] == ["reply"], [d.reason for d in decisions]
    client = "\n".join(out.text or "" for d in decisions for out in d.outbound)
    assert "Расписание" in client and "сбой" not in client, client
