"""Идемпотентность входящих и склейка серии сообщений.

Wazzup повторяет доставку вебхука, если не получил 200 вовремя, а Railway
рестартует службу когда угодно. Без дедупликации родитель получает один ответ
дважды, а школа — второй лид на того же ребёнка.

Родитель почти никогда не пишет одним сообщением: «Здравствуйте» / «сколько
стоит» / «а где вы» — это три вебхука за четыре секунды. Без склейки бот
отвечает трижды, причём вразнобой, потому что задачи идут параллельно.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.core import debounce, dedup
from app.core.debounce import conversation_lock, join_messages
from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
from app.storage.state import key_debounce
from app.types import DecisionAction, MessageStatus

from tests.conftest import CHANNEL_ID, RecordingQueue, webhook_payload


# --------------------------------------------------------------------------- #
# Дедупликация
# --------------------------------------------------------------------------- #
async def test_same_message_id_seen_once(state, sessionmaker) -> None:
    """Первая доставка — новая, вторая — уже обработанная."""
    async with sessionmaker() as db:
        assert await dedup.seen_message(state, db, "wz-1") is False
        await db.commit()
    async with sessionmaker() as db:
        assert await dedup.seen_message(state, db, "wz-1") is True
        await db.commit()


async def test_dedup_survives_state_loss(state, sessionmaker) -> None:
    """Redis потерян — решение обязана принять база, а не «сообщение новое»."""
    async with sessionmaker() as db:
        assert await dedup.seen_message(state, db, "wz-2") is False
        await db.commit()

    state.clear()  # имитируем очистку кэша / переезд плагина Redis

    async with sessionmaker() as db:
        assert await dedup.seen_message(state, db, "wz-2") is True
        await db.commit()


async def test_empty_message_id_is_treated_as_duplicate(state, sessionmaker) -> None:
    """Без ключа идемпотентности отвечать нельзя: повтор породит второй ответ."""
    async with sessionmaker() as db:
        assert await dedup.seen_message(state, db, "") is True


async def test_statuses_are_deduplicated_per_status(state, sessionmaker) -> None:
    """``sent → delivered → read`` — три разных события, схлопывать их нельзя."""
    async with sessionmaker() as db:
        assert await dedup.seen_status(state, db, "wz-3", MessageStatus.SENT) is False
        assert await dedup.seen_status(state, db, "wz-3", MessageStatus.DELIVERED) is False
        assert await dedup.seen_status(state, db, "wz-3", MessageStatus.SENT) is True
        await db.commit()


async def test_concurrent_duplicates_pass_barrier_once(state, sessionmaker) -> None:
    """Две одновременные доставки одного вебхука: пройти обязана ровно одна."""

    async def attempt() -> bool:
        async with sessionmaker() as db:
            seen = await dedup.seen_message(state, db, "wz-race")
            await db.commit()
            return seen

    results = await asyncio.gather(*(attempt() for _ in range(5)))

    assert results.count(False) == 1, "барьер пропустил больше одной обработки"


# --------------------------------------------------------------------------- #
# Склейка серии
# --------------------------------------------------------------------------- #
def test_join_messages_collapses_repeats() -> None:
    """Повтор бывает и от клиента, и от повторной доставки — модель от них теряется."""
    assert join_messages(["сколько стоит", "сколько стоит?"]) == "сколько стоит"
    assert join_messages(["Здравствуйте", "сколько стоит"]) == "Здравствуйте\nсколько стоит"
    assert join_messages(["", "  ", "привет"]) == "привет"
    assert join_messages([]) == ""


async def test_zero_window_returns_text_as_is(state) -> None:
    """Окно выключено — сообщение обрабатывается сразу и в одиночку."""
    assert await debounce.collect(state, "c1", "привет", window_s=0, max_window_s=0) == "привет"


async def test_series_is_merged_by_the_last_message(state) -> None:
    """Ранняя задача уступает: серию закрывает то сообщение, что пришло последним."""

    async def early() -> str | None:
        return await debounce.collect(
            state, "conv:1", "здравствуйте", window_s=1, max_window_s=3
        )

    async def late() -> str | None:
        await asyncio.sleep(0.2)
        return await debounce.collect(
            state, "conv:1", "сколько стоит", window_s=1, max_window_s=3
        )

    first, second = await asyncio.gather(early(), late())

    assert first is None, "раннее сообщение не имеет права ответить в одиночку"
    assert second == "здравствуйте\nсколько стоит"
    # Буфер после закрытия окна обязан быть убран, иначе следующая серия склеится с этой.
    assert await state.get(key_debounce("conv:1")) is None


async def test_state_failure_degrades_to_single_message(state, monkeypatch) -> None:
    """Состояние недоступно — серия не склеивается, но диалог не ломается."""

    async def boom(*args, **kwargs):
        raise RuntimeError("redis недоступен")

    monkeypatch.setattr(state, "get", boom)

    assert await debounce.collect(state, "c2", "привет", window_s=1, max_window_s=2) == "привет"


# --------------------------------------------------------------------------- #
# Лок диалога
# --------------------------------------------------------------------------- #
async def test_conversation_lock_allows_only_one_holder(state) -> None:
    """Одновременность на диалог — ровно одна: два воркера не пишут в один чат."""
    async with conversation_lock(state, "conv:lock", ttl_s=5) as first:
        assert first is True
        async with conversation_lock(state, "conv:lock", ttl_s=5) as second:
            assert second is False

    # После выхода лок обязан освободиться, иначе диалог замрёт до истечения TTL.
    async with conversation_lock(state, "conv:lock", ttl_s=5) as third:
        assert third is True


async def test_conversation_lock_is_per_conversation(state) -> None:
    """Разные диалоги друг друга не блокируют."""
    async with conversation_lock(state, "conv:a", ttl_s=5) as first:
        async with conversation_lock(state, "conv:b", ttl_s=5) as second:
            assert first is True and second is True


# --------------------------------------------------------------------------- #
# Пайплайн целиком
# --------------------------------------------------------------------------- #
def script() -> list[FakeTurn]:
    """Скрипт модели: расчёт цены, затем ответ теми же числами."""
    return [
        FakeTurn.tool(
            FakeCall("calculate_price", {"scope": "city", "plan": "standard", "children_count": 1})
        ),
        FakeTurn.answer(
            "Здравствуйте! Стандартный абонемент — 25 000 ₸ за 12 занятий на месяц."
        ),
    ]


@pytest.fixture
async def deps(kb, state, sessionmaker, settings) -> PipelineDeps:
    """Пайплайн на sqlite в памяти, с фейковой моделью и очередью-регистратором."""
    kb_loader.swap(kb)
    return PipelineDeps(
        sessionmaker=sessionmaker,
        state=state,
        llm=FakeLLMClient(script()),
        kb=kb_loader.get_snapshot,
        queue=RecordingQueue(),
        settings=settings,
    )


async def test_repeated_webhook_yields_single_reply(deps) -> None:
    """Один ``messageId``, доставленный дважды, даёт ровно один ответ клиенту."""
    payload = webhook_payload("wz-dup", "Сколько стоит абонемент?", chat_id="77010000010")

    first = await process_inbound(deps, payload)
    second = await process_inbound(deps, payload)

    replies = [out for d in first + second for out in d.outbound]
    assert len(replies) == 1
    assert [d.action for d in second] == [DecisionAction.DROP]
    assert [d.reason for d in second] == ["duplicate"]
    assert deps.llm.generate_calls == 1


async def test_concurrent_delivery_of_same_message_yields_single_reply(deps) -> None:
    """Гонка повторных доставок в одном диалоге не имеет права дать два ответа."""
    payload = webhook_payload("wz-race-1", "Сколько стоит?", chat_id="77010000011")

    batches = await asyncio.gather(
        process_inbound(deps, payload), process_inbound(deps, payload)
    )

    replies = [out for batch in batches for d in batch for out in d.outbound]
    assert len(replies) == 1
    assert deps.llm.generate_calls == 1


async def test_message_during_bot_turn_waits_its_turn_instead_of_vanishing(deps, state) -> None:
    """Сообщение, пришедшее во время хода бота, дожидается лока и получает ответ.

    Родитель пишет «Сколько стоит?», через несколько секунд дописывает «А во
    сколько тренировки?» — первый ход ещё держит лок. Мгновенный лок отдавал
    такому сообщению ``DEFER``, и на этом всё заканчивалось: задачу никто не
    переставлял в очередь, вебхуку Wazzup уже ответили 200. Второй вопрос
    родителя пропадал навсегда.
    """
    deps = replace(deps, settings=deps.settings.model_copy(update={"conv_lock_wait_seconds": 5}))
    payload = webhook_payload("wz-busy-1", "А где вы находитесь?", chat_id="77010000012")
    conv_key = f"{payload['messages'][0]['channelId']}:whatsapp:77010000012"

    async def busy_turn() -> None:
        """Чужой ход: держит лок диалога и отпускает его."""
        async with conversation_lock(state, conv_key, ttl_s=5) as held:
            assert held is True
            await asyncio.sleep(0.3)

    other = asyncio.create_task(busy_turn())
    await asyncio.sleep(0.05)  # даём чужому ходу забрать лок

    decisions = await process_inbound(deps, payload)
    await other

    assert [d.reason for d in decisions] != ["conversation_locked"]
    assert len([out for d in decisions for out in d.outbound]) == 1
    assert deps.llm.generate_calls == 1


async def test_unreleasable_lock_returns_text_to_the_series_buffer(deps, state) -> None:
    """Бюджет ожидания исчерпан — текст возвращается в буфер серии, а не в никуда.

    Это последняя страховка: чужой ход завис или процесс убит. Ответить сейчас
    нельзя, но реплика клиента обязана уехать к модели вместе со следующим его
    сообщением. Ожидание здесь нулевое — тест не имеет права спать секундами.
    """
    deps = replace(deps, settings=deps.settings.model_copy(update={"conv_lock_wait_seconds": 0}))
    payload = webhook_payload("wz-busy-2", "А где вы находитесь?", chat_id="77010000014")
    conv_key = f"{payload['messages'][0]['channelId']}:whatsapp:77010000014"

    async with conversation_lock(state, conv_key, ttl_s=30) as held:
        assert held is True
        decisions = await process_inbound(deps, payload)

    assert [d.action for d in decisions] == [DecisionAction.DEFER]
    assert [d.reason for d in decisions] == ["conversation_locked"]
    assert not [out for d in decisions for out in d.outbound]
    assert deps.llm.generate_calls == 0

    buffered = await state.get(key_debounce(conv_key))
    assert buffered is not None, "текст клиента потерян: буфер серии пуст"
    assert "А где вы находитесь?" in buffered


async def test_killed_turn_releases_dedup_so_the_retry_answers(deps, monkeypatch) -> None:
    """Задача, убитая по таймауту или деплою, не имеет права оставить клиента в тишине.

    Отметка дедупа ставится до вызова модели — иначе повтор вебхука породит
    второй ответ. Но ход может не дожить до ответа: ``worker_job_timeout_s``
    или остановка процесса при деплое Railway приходят в задачу как
    ``CancelledError``. arq вернёт задачу в очередь — и повтор обязан застать
    сообщение необработанным, а не отбросить его как дубль.
    """
    payload = webhook_payload("wz-killed-1", "Сколько стоит абонемент?", chat_id="77010000015")
    original = deps.llm.generate
    attempts = {"n": 0}

    async def killed_first_time(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise asyncio.CancelledError("воркер убит посреди хода")
        return await original(*args, **kwargs)

    monkeypatch.setattr(deps.llm, "generate", killed_first_time)

    with pytest.raises(asyncio.CancelledError):
        await process_inbound(deps, payload)

    decisions = await process_inbound(deps, payload)  # arq вернул задачу в очередь

    assert [d.reason for d in decisions] != ["duplicate"], "повтор отброшен как дубль"
    assert len([out for d in decisions for out in d.outbound]) == 1
    assert attempts["n"] == 2


async def test_answered_message_stays_deduplicated(deps, monkeypatch) -> None:
    """Обратная сторона: если ответ уже в outbox, отметку дедупа снимать нельзя.

    Иначе авария на хвосте хода превращала бы страховку в источник второго
    ответа клиенту.
    """
    payload = webhook_payload("wz-late-fail", "Сколько стоит абонемент?", chat_id="77010000016")

    async def boom(*args, **kwargs):
        # Строка outbox уже закоммичена; процесс убит на постановке задачи отправки.
        raise asyncio.CancelledError("процесс остановлен после коммита ответа")

    monkeypatch.setattr("app.core.pipeline._enqueue_job", boom)

    with pytest.raises(asyncio.CancelledError):
        await process_inbound(deps, payload)

    decisions = await process_inbound(deps, payload)

    assert [d.action for d in decisions] == [DecisionAction.DROP]
    assert [d.reason for d in decisions] == ["duplicate"]


async def test_fail_safe_stub_reaches_instagram_client(deps, monkeypatch) -> None:
    """Аварийная заглушка обязана нести ``conversation_id``, иначе Instagram её не пустит.

    Без диалога отправщик не знает ``last_inbound_at``, а «неизвестно, когда
    клиент писал» для Instagram означает «мы пишем первыми» — запрещено
    правилами канала. Ровно в момент аварии клиент не получал ни ответа модели,
    ни честного «передам администратору».
    """
    from app.channels.outbound import check_send_allowed
    from app.storage import repo_conversation
    from app.types import ChannelKind, OutboundKind

    payload = webhook_payload(
        "wz-crash-ig", "Сколько стоит?", chat_id="ig-4400", chat_type="instagram"
    )

    async def boom(*args, **kwargs):
        # Падение вне обработчиков хода: сюда штатные страховки не достают,
        # остаётся только _fail_safe.
        raise RuntimeError("пайплайн упал")

    monkeypatch.setattr("app.core.pipeline._run_turn", boom)

    decisions = await process_inbound(deps, payload)

    assert [d.action for d in decisions] == [DecisionAction.ESCALATE]
    assert [d.reason for d in decisions] == ["pipeline_error:RuntimeError"]
    stub = [out for d in decisions for out in d.outbound]
    assert len(stub) == 1, "клиент остался без заглушки"
    assert stub[0].conversation_id is not None, "заглушка без диалога: Instagram её не пропустит"

    async with deps.sessionmaker() as db:
        conv = await repo_conversation.get_by_id(db, stub[0].conversation_id)
    assert conv is not None
    last_inbound_at = conv.last_inbound_at
    assert last_inbound_at is not None, "окно канала не открыто: отправщику не на что опереться"
    if last_inbound_at.tzinfo is None:  # sqlite отдаёт наивное время
        last_inbound_at = last_inbound_at.replace(tzinfo=timezone.utc)
    assert (
        check_send_allowed(
            channel=ChannelKind.INSTAGRAM,
            last_inbound_at=last_inbound_at,
            now=datetime.now(timezone.utc),
            kind=OutboundKind.ESCALATION_NOTICE,
        )
        is None
    ), "окно диалога открыто, но отправка заглушки запрещена"


# --------------------------------------------------------------------------- #
# Согласованность таймингов хода
# --------------------------------------------------------------------------- #
def test_lock_ttl_outlives_the_longest_turn(settings) -> None:
    """TTL лока обязан пережить ход целиком, иначе клиент получает два ответа.

    Лок брался на 60 с, а ход стоит до ``llm_turn_budget_s``: ключ истекал
    посреди хода, следующее сообщение того же родителя спокойно брало лок и
    запускало второй ход на неполной истории.
    """
    assert settings.timing_blockers() == []
    assert settings.conv_lock_ttl_seconds > settings.llm_turn_budget_s
    assert settings.conv_lock_wait_seconds >= settings.llm_turn_budget_s
    assert (
        settings.worker_job_timeout_s
        > settings.conv_lock_wait_seconds + settings.llm_turn_budget_s
    )


def test_short_lock_ttl_is_a_startup_blocker(settings) -> None:
    """Рассогласование таймингов через ENV обязано быть видно на старте."""
    broken = settings.model_copy(update={"conv_lock_ttl_seconds": 30})

    assert any("CONV_LOCK_TTL_SECONDS" in item for item in broken.timing_blockers())
    assert any("CONV_LOCK_TTL_SECONDS" in item for item in broken.startup_blockers())


async def test_own_echo_is_dropped_not_taken_for_operator(deps) -> None:
    """Своё эхо — не оператор, иначе бот ставил бы себе паузу после каждого ответа."""
    from app.storage import repo_outbox

    payload = webhook_payload("wz-first", "Сколько стоит?", chat_id="77010000013")
    await process_inbound(deps, payload)

    outbox_id, _ = deps.queue.outbox[0]
    async with deps.sessionmaker() as db:
        await repo_outbox.claim(db, outbox_id)
        await repo_outbox.mark_sent(db, outbox_id, wazzup_message_id="wz-own-echo")
        await db.commit()

    decisions = await process_inbound(
        deps,
        webhook_payload(
            "wz-own-echo", "Здравствуйте! Стандартный абонемент...",
            is_echo=True, chat_id="77010000013",
        ),
    )

    assert [d.reason for d in decisions] == ["echo_own"]
    assert not [out for d in decisions for out in d.outbound]


async def test_echo_after_repeated_crm_id_does_not_pause_the_bot(deps) -> None:
    """Дубль по ``crmMessageId`` не имеет права заткнуть бота на 30 минут.

    Сквозная проверка блокера: ответ Wazzup на первую попытку не доехал, повтор
    вернул ``repeatedCrmMessageId`` — id сообщения нам так и не сообщили. Через
    секунду приходит вебхук-эхо этого же текста с НЕИЗВЕСТНЫМ нам ``messageId``.
    Опознать его можно только по свежей строке ``sent`` с тем же текстом; без
    этого бот считал эхо репликой оператора, ставил себе паузу и переставал
    отвечать клиенту.
    """
    from app.core import pause
    from app.storage import repo_outbox
    from app.workers import tasks_outbound

    chat_id = "77010000014"
    await process_inbound(
        deps, webhook_payload("wz-crm-1", "Сколько стоит?", chat_id=chat_id)
    )
    outbox_id, _ = deps.queue.outbox[0]

    async with deps.sessionmaker() as db:
        row = await repo_outbox.get(db, outbox_id)
        message = tasks_outbound._message_of(row)
        conv_id = row.conversation_id
        await repo_outbox.claim(db, outbox_id)
        await db.commit()

    # Ветка WazzupDuplicateError: строка становится sent, id остаётся неизвестным.
    await tasks_outbound._mark_duplicate_sent(deps, outbox_id, message)
    async with deps.sessionmaker() as db:
        assert (await repo_outbox.get(db, outbox_id)).wazzup_message_id is None

    decisions = await process_inbound(
        deps,
        webhook_payload("wz-never-seen", message.text or "", is_echo=True, chat_id=chat_id),
    )

    assert [d.reason for d in decisions] == ["echo_own"]
    conv_key = f"{CHANNEL_ID}:whatsapp:{chat_id}"
    async with deps.sessionmaker() as db:
        assert (
            await pause.is_paused(
                deps.state, db, conv_id, conv_key, datetime.now(timezone.utc)
            )
            is False
        ), "бот принял собственное эхо за оператора и поставил себе паузу"

    deps.llm.push(FakeTurn.answer("Записываю вас на пробное занятие."))
    followup = await process_inbound(
        deps, webhook_payload("wz-crm-2", "Да, записывайте", chat_id=chat_id)
    )
    assert [d.action for d in followup] == [DecisionAction.REPLY]
