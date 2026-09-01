"""Сквозной бизнес-путь: от «сколько стоит» до записи на пробное занятие.

Это главный денежный сценарий продукта, и до появления этого файла он не был
покрыт ни одним тестом: ``create_trial_lead`` не вызывался нигде, карточки
менеджеру никто не проверял. Здесь диалог проигрывается целиком, через
настоящий ``process_inbound``, на настоящей базе знаний.

Модель заменена скриптом (``FakeLLMClient``), потому что проверяется не качество
формулировок, а маршрутизация: вызвался ли нужный инструмент, дошёл ли лид до
базы, ушла ли карточка администратору, не снял ли ответ пост-фильтр.
"""

from __future__ import annotations

from typing import Sequence

import pytest

from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
from app.storage import repo_lead
from app.types import DecisionAction, ManagerCardKind, PipelineDecision

from tests.conftest import RecordingQueue, webhook_payload

CHAT_ID = "77015550101"
GYM_ID = "mkr6_arystanbekova_6"


@pytest.fixture
async def llm() -> FakeLLMClient:
    """Модель-скрипт: сценарий задаётся отдельно перед каждым ходом."""
    return FakeLLMClient([])


@pytest.fixture
async def deps(kb, state, sessionmaker, settings, llm) -> PipelineDeps:
    """Пайплайн на sqlite в памяти, с очередью-регистратором вместо Redis."""
    kb_loader.swap(kb)
    return PipelineDeps(
        sessionmaker=sessionmaker,
        state=state,
        llm=llm,
        kb=kb_loader.get_snapshot,
        queue=RecordingQueue(),
        settings=settings,
    )


async def say(
    deps: PipelineDeps,
    llm: FakeLLMClient,
    message_id: str,
    text: str,
    script: Sequence[FakeTurn],
) -> list[PipelineDecision]:
    """Проигрывает один ход: реплика клиента плюс заготовленный ответ модели."""
    llm.reset(script)
    return await process_inbound(deps, webhook_payload(message_id, text, chat_id=CHAT_ID))


def replies(decisions: Sequence[PipelineDecision]) -> list[str]:
    """Тексты, которые ушли бы клиенту."""
    return [out.text for d in decisions for out in d.outbound]


def tools(decisions: Sequence[PipelineDecision]) -> list[str]:
    """Имена вызванных инструментов по порядку."""
    return [inv.name for d in decisions for inv in d.invocations]


def assert_no_block(decisions: Sequence[PipelineDecision]) -> None:
    """Пост-фильтр не имеет права снимать ответ, собранный из данных инструментов."""
    blocked = [d.postcheck_fail for d in decisions if d.postcheck_fail]
    assert not blocked, f"пост-фильтр снял ответ: {blocked}"


# --------------------------------------------------------------------------- #
# Полный диалог
# --------------------------------------------------------------------------- #
async def test_full_booking_flow_creates_lead_and_notifies_manager(deps, llm) -> None:
    """Четыре хода: цена → район → согласие → запись. Лид в базе, карточка у менеджера."""

    # --- ход 1: вопрос о цене -------------------------------------------- #
    first = await say(
        deps,
        llm,
        "wz-book-1",
        "Здравствуйте, сколько стоит?",
        [
            FakeTurn.tool(
                FakeCall(
                    "calculate_price",
                    {"scope": "city", "plan": "standard", "children_count": 1},
                )
            ),
            FakeTurn.answer(
                "Здравствуйте. Стандартный абонемент — 25 000 ₸ за 12 занятий на месяц. "
                "В каком районе вам удобно заниматься?"
            ),
        ],
    )
    assert_no_block(first)
    assert "calculate_price" in tools(first), f"цену назвали без калькулятора: {tools(first)}"
    assert replies(first), "бот не ответил на вопрос о цене"

    # --- ход 2: клиент называет район ------------------------------------ #
    second = await say(
        deps,
        llm,
        "wz-book-2",
        "Мы живём в 6 микрорайоне",
        [
            FakeTurn.tool(FakeCall("find_gym_by_district", {"district_text": "6 микрорайон"})),
            FakeTurn.answer(
                "У нас есть зал в вашем районе. Записать ребёнка на бесплатное пробное занятие?"
            ),
        ],
    )
    assert_no_block(second)
    assert "find_gym_by_district" in tools(second)
    gym_lookup = [inv for d in second for inv in d.invocations if inv.name == "find_gym_by_district"]
    assert gym_lookup[0].result.ok, f"подбор зала упал: {gym_lookup[0].result.error}"

    # --- ход 3: согласие и данные ребёнка -------------------------------- #
    third = await say(
        deps,
        llm,
        "wz-book-3",
        "Да, давайте. Сына зовут Ержан, ему 8 лет",
        [
            FakeTurn.tool(
                FakeCall(
                    "create_trial_lead",
                    {
                        "child_name": "Ержан",
                        "child_age": 8,
                        "child_gender": "m",
                        "gym_id": GYM_ID,
                        "parent_name": "Айгуль",
                        "preferred_time_text": "будни вечером",
                        "parent_agreed": True,
                    },
                )
            ),
            FakeTurn.answer(
                "Записала: Ержан, 8 лет. Администратор свяжется с вами и подтвердит время."
            ),
        ],
    )
    assert_no_block(third)

    booking = [inv for d in third for inv in d.invocations if inv.name == "create_trial_lead"]
    assert booking, f"инструмент записи не вызван, вызваны: {tools(third)}"
    assert booking[0].result.ok, f"запись не удалась: {booking[0].result.error}"

    # --- лид доехал до базы ---------------------------------------------- #
    conv_id = next(d.conversation_id for d in third if d.conversation_id)
    async with deps.sessionmaker() as session:
        lead = await repo_lead.get_by_conversation(session, conv_id)

    assert lead is not None, "лид не сохранён в базе"
    assert lead.child_name == "Ержан"
    assert lead.child_age == 8
    assert lead.gym_id == GYM_ID
    assert lead.channel_user == CHAT_ID

    # Телефон в WhatsApp известен из chat_id, спрашивать его у родителя незачем.
    assert lead.phone, "телефон не подставлен из chat_id WhatsApp"
    assert lead.phone.endswith("5550101"), f"подставлен чужой номер: {lead.phone}"

    # --- карточка менеджеру ---------------------------------------------- #
    cards = [c for d in third for c in d.manager_cards]
    assert cards, "администратор не получил карточку лида"
    lead_cards = [c for c in cards if c.kind is ManagerCardKind.LEAD]
    assert lead_cards, f"карточка есть, но не типа lead: {[c.kind for c in cards]}"

    card = lead_cards[0].text
    assert "Ержан" in card, f"в карточке нет имени ребёнка:\n{card}"
    assert "8" in card, f"в карточке нет возраста:\n{card}"
    assert "5550101" in card, f"в карточке нет телефона:\n{card}"


async def test_lead_is_not_duplicated_when_model_books_twice(deps, llm) -> None:
    """Повторный вызов записи в том же диалоге не плодит второй лид."""
    booking_call = FakeCall(
        "create_trial_lead",
        {"child_name": "Аружан", "child_age": 7, "gym_id": GYM_ID, "parent_agreed": True},
    )

    await say(
        deps,
        llm,
        "wz-dup-book-1",
        "Запишите нас",
        [FakeTurn.tool(booking_call), FakeTurn.answer("Записала, администратор перезвонит.")],
    )
    second = await say(
        deps,
        llm,
        "wz-dup-book-2",
        "И ещё раз запишите на всякий случай",
        [FakeTurn.tool(booking_call), FakeTurn.answer("Заявка уже принята, дублировать не нужно.")],
    )

    conv_id = next(d.conversation_id for d in second if d.conversation_id)
    async with deps.sessionmaker() as session:
        leads = await repo_lead.list_recent(session, limit=50)

    mine = [lead for lead in leads if lead.conversation_id == conv_id]
    assert len(mine) == 1, f"в одном диалоге создано лидов: {len(mine)}"


async def test_booking_with_invented_phone_is_rejected(deps, llm) -> None:
    """Телефон, придуманный моделью, инструмент обязан отвергнуть.

    Structured output гарантирует синтаксис, но не смысл: модель способна
    «вспомнить» правдоподобный номер. В карточку менеджеру такой номер попадать
    не должен — по нему просто некому звонить.
    """
    decisions = await say(
        deps,
        llm,
        "wz-badphone-1",
        "Запишите, телефон у вас есть",
        [
            FakeTurn.tool(
                FakeCall(
                    "create_trial_lead",
                    {
                        "child_name": "Дана",
                        "child_age": 9,
                        "gym_id": GYM_ID,
                        "parent_agreed": True,
                        "phone": "+7 000 000-00-00",
                    },
                )
            ),
            FakeTurn.answer("Уточните, пожалуйста, номер для связи."),
        ],
    )

    booking = [inv for d in decisions for inv in d.invocations if inv.name == "create_trial_lead"]
    assert booking, "инструмент записи не вызван"

    conv_id = next(d.conversation_id for d in decisions if d.conversation_id)
    async with deps.sessionmaker() as session:
        lead = await repo_lead.get_by_conversation(session, conv_id)

    assert lead is not None, "лид не сохранён"
    digits = "".join(ch for ch in (lead.phone or "") if ch.isdigit())
    assert "0000000000" not in digits, f"выдуманный номер сохранён как контакт: {lead.phone}"
    # Отвергнув выдумку, инструмент обязан оставить настоящий номер из WhatsApp,
    # иначе администратору некуда звонить.
    assert digits.endswith("5550101"), f"потерян настоящий номер клиента: {lead.phone!r}"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("8 705 123 45 67", "+77051234567"),
        ("+77051234567", "+77051234567"),
        ("87051234567", "+77051234567"),
        ("+7 000 000-00-00", None),  # выдумка модели
        ("+7 999 111 22 33", None),  # в Казахстане мобильные начинаются на 70x
        ("123", None),
    ],
)
def test_phone_normalisation_guards_the_lead(raw: str, expected: str | None) -> None:
    """Нормализация телефона — последний барьер перед карточкой администратора.

    Structured output модели гарантирует, что в поле придёт строка, но не что
    это настоящий номер. По неверному номеру администратор просто не дозвонится,
    поэтому проверка живёт в коде, а не в промпте.
    """
    from app.tools.booking import normalize_phone_kz

    assert normalize_phone_kz(raw) == expected


async def test_escalation_reaches_manager_with_reason(deps, llm) -> None:
    """Вопрос без данных уходит человеку: у карточки есть причина, бот не выдумывает."""
    decisions = await say(
        deps,
        llm,
        "wz-esc-1",
        "Во сколько тренировки по вторникам?",
        [
            # Берём зал БЕЗ расписания: у городских оно уже заполнено.
            FakeTurn.tool(FakeCall("get_schedule", {"gym_id": "region_karabalyk"})),
            FakeTurn.tool(
                FakeCall(
                    "escalate_to_manager",
                    {
                        "reason": "no_data",
                        "question_summary": "спрашивает расписание по вторникам",
                    },
                )
            ),
            FakeTurn.answer("Расписание уточню у администратора и вернусь с ответом."),
        ],
    )

    called = tools(decisions)
    assert "get_schedule" in called
    schedule = [inv for d in decisions for inv in d.invocations if inv.name == "get_schedule"][0]
    assert schedule.result.data.get("status") == "no_data", (
        "расписания в данных нет — инструмент обязан честно вернуть no_data"
    )

    cards = [c for d in decisions for c in d.manager_cards]
    assert cards, "эскалация не дошла до администратора"
    assert any(c.kind is ManagerCardKind.ESCALATION for c in cards), (
        f"карточка не помечена как эскалация: {[c.kind for c in cards]}"
    )
    assert all(d.action is not DecisionAction.DROP for d in decisions)


async def test_child_writing_does_not_silence_the_bot_for_the_parent(deps, llm) -> None:
    """Позвав родителя, бот обязан остаться на связи.

    Найдено живым прогоном. На сообщение ребёнка бот отвечает «покажи это
    сообщение маме или папе, пусть напишут сюда» — и раньше тут же вставал на
    паузу как при обычной эскалации. Родитель, которого он сам и позвал, получал
    полную тишину: диалог умирал ровно в точке, ради которой затевался.
    """
    child = await say(
        deps, llm, "wz-child-1", "мне 12 лет запишите меня сами без родителей", []
    )
    assert replies(child), "ребёнку не ответили"

    parent = await say(
        deps,
        llm,
        "wz-parent-1",
        "Здравствуйте, это мама. Девочке 9 лет, можно на бокс?",
        [FakeTurn.answer("Здравствуйте! Расскажу подробнее. В каком районе вам удобно?")],
    )

    assert [d.action for d in parent] != [DecisionAction.SILENT], (
        "бот замолчал для родителя, которого сам же попросил написать"
    )
    assert replies(parent), "родитель не получил ответа"


async def test_booking_without_parent_consent_is_refused(deps, llm) -> None:
    """Названные имя и возраст — это не согласие записаться.

    Найдено на первом же живом прогоне в Telegram: родитель спросил про районы,
    назвал зал и имя сына — и бот сразу отчитался «записал». Никто не спрашивал,
    хочет ли он записываться. Барьер держится кодом, а не только промптом:
    инструкцию модель нарушает, проверку — нет.
    """
    decisions = await say(
        deps,
        llm,
        "wz-noconsent-1",
        "Сына зовут Алдияр, ему 14",
        [
            FakeTurn.tool(
                FakeCall(
                    "create_trial_lead",
                    {"child_name": "Алдияр", "child_age": 14, "gym_id": GYM_ID},
                )
            ),
            FakeTurn.answer("Записать Алдияра на бесплатное пробное занятие?"),
        ],
    )

    booking = [inv for d in decisions for inv in d.invocations if inv.name == "create_trial_lead"]
    assert booking, "инструмент не вызывался — сценарий не воспроизведён"
    assert not booking[0].result.ok, "запись прошла без согласия родителя"

    conv_id = next(d.conversation_id for d in decisions if d.conversation_id)
    async with deps.sessionmaker() as session:
        lead = await repo_lead.get_by_conversation(session, conv_id)
    if lead is not None:
        assert lead.status != "trial_booked", "лид помечен записанным без согласия"


@pytest.mark.parametrize("placeholder", ["сын", "дочка", "ребёнок", "бала", "мальчик"])
async def test_placeholder_instead_of_name_is_refused(deps, llm, placeholder: str) -> None:
    """«Сын» — это не имя.

    Найдено на живом прогоне: родитель написал «сыну 9 лет», и модель записала
    ребёнка с именем «сын». Администратор получил карточку «Ребёнок: сын, 9 лет»
    и всё равно должен звонить и выяснять, к кому обращаться.
    """
    decisions = await say(
        deps,
        llm,
        f"wz-placeholder-{placeholder}",
        "Давайте запишемся",
        [
            FakeTurn.tool(
                FakeCall(
                    "create_trial_lead",
                    {
                        "child_name": placeholder,
                        "child_age": 9,
                        "gym_id": GYM_ID,
                        "parent_agreed": True,
                    },
                )
            ),
            FakeTurn.answer("Как зовут ребёнка?"),
        ],
    )

    booking = [inv for d in decisions for inv in d.invocations if inv.name == "create_trial_lead"]
    assert booking and not booking[0].result.ok, f"«{placeholder}» принято за имя ребёнка"


# --------------------------------------------------------------------------- #
# Приветствие с меню: единственная форма выбора, работающая во всех каналах
# --------------------------------------------------------------------------- #
async def test_bare_greeting_answers_from_template_without_llm(deps, llm) -> None:
    """На «здравствуйте» бот отвечает шаблоном с меню и не тратит вызов модели.

    Первое сообщение обязано быть одинаковым и подсказывать, что писать дальше:
    владелец жаловался, что растерялся после первого ответа бота.
    """
    decisions = await say(deps, llm, "wz-hi-1", "Здравствуйте", [])

    texts = replies(decisions)
    assert texts, "бот не поздоровался"
    assert "1." in texts[0] and "2." in texts[0] and "3." in texts[0], (
        f"в приветствии нет меню:\n{texts[0]}"
    )
    assert llm.generate_calls == 0, "шаблонное приветствие не должно стоить вызова модели"


async def test_greeting_with_a_question_is_not_replaced_by_menu(deps, llm) -> None:
    """«Здравствуйте, сколько стоит?» — это вопрос, а не приветствие.

    Ответить меню на прямой вопрос хуже, чем ответить по существу.
    """
    decisions = await say(
        deps,
        llm,
        "wz-hi-2",
        "Здравствуйте, сколько стоит абонемент?",
        [FakeTurn.answer("Стоимость зависит от того, где заниматься.")],
    )

    assert llm.generate_calls == 1, "вопрос обязан дойти до модели"
    assert "1." not in (replies(decisions)[0] if replies(decisions) else "")


@pytest.mark.parametrize(
    ("digit", "expected_fragment"),
    [
        ("1", "пробное"),
        ("2", "школе"),
        ("3", "уже занимаемся"),
    ],
)
async def test_bare_digit_after_greeting_expands_to_the_chosen_option(
    deps, llm, digit: str, expected_fragment: str
) -> None:
    """Голая цифра разворачивается в выбранный пункт ДО обращения к модели.

    Кнопок в WhatsApp и Instagram нет, поэтому меню — это текст, и трактовка
    цифры не имеет права зависеть от настроения модели. Проверено живым
    прогоном: без разворота бот на «2» отвечал про пропуск тренировки.
    """
    await say(deps, llm, f"wz-menu-{digit}-1", "Здравствуйте", [])
    llm.reset([FakeTurn.answer("Отвечаю по выбранному пункту.")])
    await say(deps, llm, f"wz-menu-{digit}-2", digit, [FakeTurn.answer("ответ")])

    sent = llm.requests[-1].user_text.lower()
    assert expected_fragment in sent, f"цифра {digit} не развернулась: {sent!r}"


async def test_digit_later_in_dialogue_is_left_alone(deps, llm) -> None:
    """Дальше в разговоре «2» — это возраст, количество детей или номер зала."""
    await say(deps, llm, "wz-late-1", "Здравствуйте", [])
    llm.reset([FakeTurn.answer("ок")])
    await say(deps, llm, "wz-late-2", "Расскажите про залы", [FakeTurn.answer("ок")])
    llm.reset([FakeTurn.answer("ок")])
    await say(deps, llm, "wz-late-3", "2", [FakeTurn.answer("ок")])

    assert llm.requests[-1].user_text == "2", "цифра посреди диалога развёрнута ошибочно"


# --------------------------------------------------------------------------- #
# Контент по контексту: видео маршрута, фото, деградация по каналам
# --------------------------------------------------------------------------- #
async def test_route_video_is_denied_in_whatsapp_with_a_text_fallback(deps, llm, kb) -> None:
    """Видео не уходит в WhatsApp — но это НЕ «данных нет».

    Раньше отказ канала возвращался как no_data, и бот отвечал «уточню у
    администратора», хотя адрес он знает. Теперь инструмент отдаёт успех
    с подсказкой и именем запасного материала — карточки адреса.
    """
    from app.tools.content import send_content
    from app.types import ChannelKind, ToolContext
    from datetime import datetime, timezone
    from uuid import uuid4
    from tests.conftest import RecordingServices

    ctx = ToolContext(
        conversation_id=uuid4(),
        conv_key="conv:test",
        channel=ChannelKind.WHATSAPP,
        channel_id="11111111-1111-1111-1111-111111111111",
        chat_id="77010000001",
        lang=__import__("app.types", fromlist=["Language"]).Language.RU,
        kb=kb,
        kb_hash=kb.kb_hash,
        now=datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc),
        correlation_id="test",
        services=RecordingServices(),
    )
    result = await send_content(ctx, artifact_id="route_mkr6_arystanbekova_6")

    assert result.ok, "отказ канала не должен выглядеть как ошибка инструмента"
    assert result.data["status"] == "channel_unsupported"
    assert result.data["fallback_artifact_id"] == "gym_location_mkr6_arystanbekova_6", (
        "модели не подсказали, чем заменить видео"
    )
    assert result.data["queued"] == [], "в WhatsApp видео не имеет права уйти"


async def test_route_video_reaches_telegram(deps, llm, kb) -> None:
    """В Telegram видео уходит вложением — это единственный канал, где оно проходит."""
    from app.tools.content import send_content
    from app.types import ChannelKind, Language, ToolContext
    from datetime import datetime, timezone
    from uuid import uuid4
    from tests.conftest import RecordingServices

    services = RecordingServices()
    ctx = ToolContext(
        conversation_id=uuid4(),
        conv_key="conv:tg",
        channel=ChannelKind.TELEGRAM,
        channel_id="telegram-bot-api",
        chat_id="777000111",
        lang=Language.RU,
        kb=kb,
        kb_hash=kb.kb_hash,
        now=datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc),
        correlation_id="test",
        services=services,
    )
    result = await send_content(ctx, artifact_id="route_mkr6_arystanbekova_6")

    assert result.ok and result.data["queued"], "видео не поставлено в отправку"
    # Файл и подпись — ДВА сообщения: Wazzup запрещает text и contentUri вместе.
    assert len(services.outbound) == 2, f"ожидались файл и подпись, а ушло: {len(services.outbound)}"
    file_msg = [m for m in services.outbound if m.content_uri]
    caption = [m for m in services.outbound if m.text]
    assert file_msg, "сам файл не ушёл"
    assert caption, "видео ушло без подписи"

    # Подпись собирается из базы знаний в том же виде, в каком её пишет сама
    # школа: адрес, ссылка на 2ГИС и расписание. Статичный текст здесь жил бы
    # своей жизнью — владелец правит расписание, а под видео старое время.
    text = caption[0].text
    assert "Арыстанбекова 6" in text, "в подписи нет адреса"
    assert "2gis.kz" in text, "в подписи нет ссылки на карту"
    assert "🕒" in text and "19:00–20:30" in text, "в подписи нет расписания"


async def test_every_city_gym_has_a_route_video_or_none(kb) -> None:
    """Видео маршрута привязано к существующему залу и не потеряно.

    Проверяет, что перенос файлов не разошёлся с базой: артефакт без зала или
    зал с ссылкой на несуществующее видео — молчаливая поломка.
    """
    routes = {a.id: a for a in kb.media if a.id.startswith("route_")}
    gym_ids = {g.id for g in kb.gyms.gyms}
    for artifact in routes.values():
        assert artifact.gym_id in gym_ids, f"{artifact.id}: зал '{artifact.gym_id}' не существует"
        assert artifact.file_path, f"{artifact.id}: не указан файл"
        assert artifact.channels.get("telegram") == "allow"


async def test_menu_item_four_reaches_a_human_without_the_model(deps, llm) -> None:
    """Пункт «Написать менеджеру» отрабатывает кодом, без обращения к модели.

    Разворот цифры в фразу стоит ДО проверок. Пока он стоял после них, guard
    видел голое «4», просьбу к человеку не опознавал, и цифру разбирала модель:
    медленнее, дороже и с ответом «чтобы не сказать вам неточность» вместо
    «передаю менеджеру».
    """
    await say(deps, llm, "m-greet", "Здравствуйте", [])
    # Пустой сценарий: любой вызов модели здесь означал бы, что ход пошёл не тем путём.
    decisions = await say(deps, llm, "m-four", "4", [])

    assert not tools(decisions), "ход не должен обращаться к инструментам"
    text = " ".join(replies(decisions))
    assert "менеджер" in text.lower()
    assert "неточность" not in text, "клиенту ответили как на незнание, а он просто позвал человека"
    from app.types import DecisionAction, EscalationReason

    assert any(d.action is DecisionAction.ESCALATE for d in decisions), "диалог не передан человеку"
    assert any(d.escalation_reason is EscalationReason.USER_REQUEST for d in decisions)
    assert any(d.manager_cards for d in decisions), "менеджер не получил карточку"


async def test_menu_digit_outside_the_greeting_is_not_a_menu_choice(deps, llm) -> None:
    """Цифра в середине разговора остаётся цифрой.

    После приветствия «4» — это выбор пункта, а дальше в диалоге может быть
    возраст ребёнка, число детей или номер зала.
    """
    from app.core.pipeline import expand_menu_choice

    assert expand_menu_choice("4", after_greeting=False) == "4"
    assert expand_menu_choice("4", after_greeting=True) != "4"
