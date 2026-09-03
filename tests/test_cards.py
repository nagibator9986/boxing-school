"""Оформление того, что видит клиент: карточки, подписи, расписание.

Проверяется не «текст непустой», а свойства, за которые эти карточки и делались:
клиент читает их с телефона, и стена текста с трижды повторённым адресом
отпугивает сильнее, чем отсутствие ответа.
"""

from __future__ import annotations

from datetime import UTC

import pytest

from app.kb.models import KBSnapshot
from app.kb.render import (
    render_artifact_body,
    render_gym_location,
    render_gyms_list_card,
    render_price_card,
    render_route_caption,
    render_schedule_card,
)
from app.types import Language, Scope


def test_gyms_list_is_split_into_blocks(kb: KBSnapshot) -> None:
    """Каждый зал — отдельный блок, между блоками пустая строка."""
    text = render_gyms_list_card(kb, scope=Scope.CITY, lang=Language.RU)
    blocks = text.split("\n\n")
    assert len(blocks) >= 6, "залы слиплись в один блок"
    assert all(block.strip() for block in blocks)


def test_gyms_list_is_numbered(kb: KBSnapshot) -> None:
    """Залы пронумерованы: кнопок в мессенджерах нет, номер — это ответ клиента."""
    text = render_gyms_list_card(kb, scope=Scope.CITY, lang=Language.RU)
    assert "1. " in text and "2. " in text
    assert "напишите номер" in text.lower()


def test_gyms_list_does_not_repeat_itself(kb: KBSnapshot) -> None:
    """Название, адрес и ориентир не пересказывают друг друга.

    До правки строка выглядела так: «КСК — школа №9: Каирбекова 334, школа №9
    (цокольный этаж) (цокольный этаж школы №9)» — три повтора в одной строке,
    и это читалось как сбой программы.
    """
    text = render_gyms_list_card(kb, scope=Scope.CITY, lang=Language.RU)
    for line in text.splitlines():
        assert line.count("Рахат") <= 1, line
        assert line.count("Жана-Кала") <= 1, line
        assert line.count("цокольный этаж") <= 1, line


def test_price_card_keeps_plan_order(kb: KBSnapshot) -> None:
    """Тарифы идут в порядке базы знаний: стандартный раньше гибкого.

    Сортировка по алфавиту выносила вперёд «Гибкий» — просто потому, что «Г»
    раньше «С», — и первым клиент видел тариф подороже.
    """
    text = render_price_card(kb, scope=Scope.CITY, lang=Language.RU)
    assert text.index("Стандартный") < text.index("Гибкий")


def test_price_card_shows_caveat_on_its_own_line(kb: KBSnapshot) -> None:
    """Оговорка тарифа видна до покупки, а не теряется в хвосте строки."""
    text = render_price_card(kb, scope=Scope.CITY, lang=Language.RU)
    lines = text.splitlines()
    price_line = next(i for i, line in enumerate(lines) if "25 000" in line)
    assert "перерасчёт" in lines[price_line + 1].lower()


def test_schedule_card_groups_by_discipline(kb: KBSnapshot) -> None:
    """Занятия сгруппированы по виду, дни сведены в одну строку."""
    gym = kb.gym("ksk_kairbekova_334")
    assert gym is not None and gym.schedule
    text = render_schedule_card(kb, gym_id=gym.id, slots=gym.schedule, lang=Language.RU)
    assert "🥊 Кикбоксинг" in text and "🥊 Бокс" in text
    assert "Пн, Ср, Пт" in text, "дни не сведены в строку"
    assert text.count("🥊 Кикбоксинг") == 1, "вид занятий повторяется"


def test_schedule_card_shows_the_age_group(kb: KBSnapshot) -> None:
    """Владелец назвал возраст групп — карточка обязана его показывать.

    03.09.2026 на вопрос «какой возраст это время у вас» отвечал человек: «с 7
    до 12». Теперь это в базе знаний у каждого занятия.
    """
    gym = kb.gym("ksk_kairbekova_334")
    text = render_schedule_card(kb, gym_id=gym.id, slots=gym.schedule, lang=Language.RU)

    assert "(7–12)" in text
    assert "уточнит администратор" not in text.lower()


def test_schedule_card_is_honest_when_the_age_is_unknown(kb: KBSnapshot) -> None:
    """Появится занятие без возраста — карточка снова скажет об этом прямо.

    Правило осталось: выдумывать возраст группы бот не имеет права.
    """
    gym = kb.gym("ksk_kairbekova_334")
    nameless = [slot.model_copy(update={"age_from": None, "age_to": None}) for slot in gym.schedule]

    text = render_schedule_card(kb, gym_id=gym.id, slots=nameless, lang=Language.RU)

    assert "возраст группы уточнит администратор" in text.lower()


def test_route_caption_has_everything_the_owner_sends(kb: KBSnapshot) -> None:
    """Подпись к видео — то же, что школа отправляет клиентам вручную.

    Адрес, ссылка на 2ГИС и расписание. Собирается из базы знаний, поэтому не
    расходится с ней: владелец поменял время в CRM — подпись поменялась сама.
    """
    text = render_route_caption(kb, gym_id="ksk_kairbekova_334", lang=Language.RU)
    assert "Каирбекова 334" in text
    assert "2gis.kz" in text
    assert "🕒" in text and "09:00–10:30" in text
    assert len(text) <= 1024, "подпись не влезет в предел Telegram"


def test_route_caption_is_used_as_video_body(kb: KBSnapshot) -> None:
    """Видео маршрута берёт подпись из рендера, а не из статичного текста."""
    body = render_artifact_body(kb, artifact_id="route_ksk_kairbekova_334", lang=Language.RU)
    assert "2gis.kz" in body and "🗓" in body


def test_cards_render_in_kazakh(kb: KBSnapshot) -> None:
    """Казахская версия собирается целиком, включая дни недели."""
    text = render_route_caption(kb, gym_id="center_kasymkhanova_10", lang=Language.KK)
    assert "Картада" in text
    assert "Сабақ кестесі" in text
    assert any(day in text for day in ("Дс", "Сс", "Ср", "Бс", "Жм", "Сн", "Жк"))


def test_landmark_keeps_its_case(kb: KBSnapshot) -> None:
    """Регистр ориентира не ломается.

    Принудительное понижение первой буквы портило казахские названия:
    «Жаңа Қала ауданы» превращалось в «жаңа Қала ауданы».
    """
    text = render_gym_location(kb, gym_id="center_kasymkhanova_10", lang=Language.KK)
    assert "жаңа Қала" not in text


@pytest.mark.parametrize("scope", [Scope.CITY, Scope.REGION])
def test_no_wall_of_text(kb: KBSnapshot, scope: Scope) -> None:
    """Ни одна карточка не приходит сплошной простынёй без пустых строк."""
    for text in (
        render_gyms_list_card(kb, scope=scope, lang=Language.RU),
        render_price_card(kb, scope=scope, lang=Language.RU),
    ):
        assert "\n\n" in text, f"нет ни одного разделителя блоков:\n{text}"
        longest = max(len(line) for line in text.splitlines())
        assert longest <= 120, f"строка длиннее экрана телефона: {longest}"


async def test_address_request_sends_route_video(kb: KBSnapshot) -> None:
    """В Telegram адрес зала уходит вместе с видео дороги — как это делает школа.

    Владелец годами отправляет клиенту видео с подписью, а не текстовый адрес:
    на видео виден вход. Карточка адреса после видео была бы повтором, поэтому
    запрос карточки повышается до видео.
    """
    from datetime import datetime
    from uuid import uuid4

    from app.tools.content import send_content
    from app.types import ChannelKind, ToolContext
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
        now=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
        correlation_id="test",
        services=services,
    )
    result = await send_content(ctx, artifact_id="gym_location_ksk_kairbekova_334")

    assert result.ok
    assert result.meta["artifact_id"] == "route_ksk_kairbekova_334", "видео не подставилось"
    caption = [message for message in services.outbound if message.text]
    assert caption and "2gis.kz" in caption[0].text


async def test_whatsapp_gets_the_route_video(kb: KBSnapshot) -> None:
    """В WhatsApp карточка адреса повышается до видео дороги.

    Раньше видео сюда не отправлялось: и возможности канала, и база знаний
    исходили из того, что ролик не влезет в предел API в 10 МБ. Маршруты школы
    весят 1,5–4,1 МБ, и владелец те же видео отправляет вручную.
    """
    from datetime import datetime
    from uuid import uuid4

    from app.tools.content import send_content
    from app.types import ChannelKind, ToolContext
    from tests.conftest import RecordingServices

    services = RecordingServices()
    ctx = ToolContext(
        conversation_id=uuid4(),
        conv_key="conv:wa",
        channel=ChannelKind.WHATSAPP,
        channel_id="wa",
        chat_id="77010001234",
        lang=Language.RU,
        kb=kb,
        kb_hash=kb.kb_hash,
        now=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
        correlation_id="test",
        services=services,
    )
    result = await send_content(ctx, artifact_id="gym_location_ksk_kairbekova_334")

    assert result.ok
    assert result.meta["artifact_id"] == "route_ksk_kairbekova_334"


async def test_schedule_card_is_sent_by_code(kb: KBSnapshot) -> None:
    """Расписание уходит клиенту готовым блоком, а не пересказом модели.

    Живой прогон показал, почему это делает код: модель пересказывала блок
    своими словами и теряла разметку — значки со строк исчезали, дни и время
    сливались в одну фразу.
    """
    from datetime import datetime
    from uuid import uuid4

    from app.tools.schedule import get_schedule
    from app.types import ChannelKind, ToolContext
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
        now=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
        correlation_id="test",
        services=services,
    )
    result = await get_schedule(ctx, gym_id="ksk_kairbekova_334")

    assert result.ok
    sent = [message for message in services.outbound if message.text]
    assert sent, "расписание не отправлено"
    assert "🗓" in sent[0].text and "🕒" in sent[0].text
    assert any("не пересказывай" in caveat.lower() for caveat in result.caveats)


async def test_route_video_is_not_sent_twice(kb: KBSnapshot) -> None:
    """Одно и то же видео не приходит клиенту дважды за диалог.

    Лимит ``max_send_per_dialog`` считается по очереди исходящих, поэтому
    подмена карточки адреса на видео обязана этот счётчик уважать — иначе
    каждый вопрос про зал приносил бы клиенту тот же ролик заново.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.tools.content import send_content
    from app.types import ChannelKind, ToolContext
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
        now=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
        correlation_id="test",
        services=services,
    )
    first = await send_content(ctx, artifact_id="gym_location_ksk_kairbekova_334")
    assert first.meta["artifact_id"] == "route_ksk_kairbekova_334"

    # Видео ушло — счётчик диалога это знает.
    services.artifact_sends["route_ksk_kairbekova_334"] = 1

    second = await send_content(ctx, artifact_id="gym_location_ksk_kairbekova_334")
    assert second.meta["artifact_id"] == "gym_location_ksk_kairbekova_334", (
        "видео подставилось второй раз — клиент получит тот же ролик дважды"
    )


async def test_schedule_not_repeated_after_route_video(kb: KBSnapshot) -> None:
    """Расписание не приходит вторым блоком, если оно было в подписи к видео."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.tools.content import send_content
    from app.tools.schedule import get_schedule
    from app.types import ChannelKind, ToolContext
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
        now=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
        correlation_id="test",
        services=services,
    )
    # Видео этого зала ушло тем же ходом — расписание в его подписи клиент видит.
    await send_content(ctx, artifact_id="gym_location_ksk_kairbekova_334")
    before = len(services.outbound)

    result = await get_schedule(ctx, gym_id="ksk_kairbekova_334")

    assert result.ok and result.data.get("already_shown") is True
    assert len(services.outbound) == before, (
        "расписание отправлено вторым блоком подряд — клиент видит одно и то же дважды"
    )


async def test_schedule_is_sent_again_in_a_later_turn(kb: KBSnapshot) -> None:
    """Спросили про время позже — расписание приходит снова.

    Подавление действует ровно один ход. Молчание в ответ на прямой вопрос
    «а во сколько занятия?» хуже любого повтора: клиент решит, что бот сломался.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.tools.schedule import get_schedule
    from app.types import ChannelKind, ToolContext
    from tests.conftest import RecordingServices

    services = RecordingServices()
    # Видео уходило раньше, но не этим ходом: список сообщений хода пуст.
    services.artifact_sends["route_ksk_kairbekova_334"] = 1
    ctx = ToolContext(
        conversation_id=uuid4(),
        conv_key="conv:tg",
        channel=ChannelKind.TELEGRAM,
        channel_id="telegram-bot-api",
        chat_id="777000111",
        lang=Language.RU,
        kb=kb,
        kb_hash=kb.kb_hash,
        now=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
        correlation_id="test",
        services=services,
    )
    result = await get_schedule(ctx, gym_id="ksk_kairbekova_334")

    assert result.ok and not result.data.get("already_shown")
    sent = [message for message in services.outbound if message.text]
    assert sent and "🗓" in sent[0].text, "на прямой вопрос о времени бот промолчал"


def test_city_card_mentions_the_rest_of_the_region(kb: KBSnapshot) -> None:
    """Список по городу не выдаёт себя за весь список школы.

    Владелец считает залы вместе с Тобылом и райцентрами, и карточка «6 залов»
    выглядела так, будто часть точек потеряли. Смешивать их в один нумерованный
    список нельзя: районный прайс отличается втрое.
    """
    text = render_gyms_list_card(kb, scope=Scope.CITY, lang=Language.RU)
    assert "населённых пунктах области" in text

    region_count = len({gym.settlement for gym in kb.active_gyms(Scope.REGION) if gym.settlement})
    assert str(region_count) in text
    # Сами райцентры в нумерованный список города не попадают.
    assert "Житикара" not in text


def test_all_gyms_card_covers_every_address(kb: KBSnapshot) -> None:
    """«Скиньте все залы» — это все залы, а не только городские.

    Владелец насчитал у школы восемь адресов, а бот присылал шесть: в базе
    знаний были только списки «по Костанаю» и «по области», и модель выбирала
    городской. Карточка ``gyms_list_all`` показывает и город, и районы сразу.
    """
    from app.kb.render import render_artifact_body
    from app.types import Scope

    card = render_artifact_body(kb, artifact_id="gyms_list_all", lang=Language.RU)

    for gym in kb.active_gyms(Scope.ALL):
        title = gym.title.get(Language.RU) or gym.title.ru
        assert title in card, f"в списке «все залы» нет зала {gym.id}"


def test_every_city_gym_is_in_the_city_card(kb: KBSnapshot) -> None:
    """Новый зал обязан появляться в карточке сам, без правки кода."""
    from app.kb.render import render_gyms_list_card
    from app.types import Scope

    card = render_gyms_list_card(kb, scope=Scope.CITY, lang=Language.RU)

    for gym in kb.active_gyms(Scope.CITY):
        assert (gym.address.ru or "") in card, f"в карточке города нет адреса зала {gym.id}"


# --------------------------------------------------------------------------- #
# Что модель знает о сменах
# --------------------------------------------------------------------------- #
def test_prompt_says_morning_groups_exist(kb: KBSnapshot) -> None:
    """В промпте не было ни слова о том, когда идут занятия.

    Поэтому на вопрос «можно ли прийти утром» модель ответила догадкой —
    «утренних групп нет, все занятия во второй половине дня», — и владелец
    написал «не верная информация». Утренняя группа есть на КСК.
    """
    from app.kb.render import render_schedule_overview, render_system_prompt
    from app.types import Scope

    morning = [
        gym.id
        for gym in kb.active_gyms(Scope.ALL)
        for slot in gym.schedule or ()
        if slot.time_start < "12:00"
    ]
    assert morning, "фикстура без утренних занятий не проверяет ничего"

    overview = render_schedule_overview(kb)
    assert "Утренние группы ЕСТЬ" in overview
    assert morning[0] in overview
    assert overview in render_system_prompt(kb), "сводка не попала в системную инструкцию"


def test_prompt_overview_carries_no_clock_times(kb: KBSnapshot) -> None:
    """Настоящее время в промпте модель списывает вместо вызова инструмента.

    Постфильтр подтверждает время только данными ``get_schedule`` и снимает
    ответ целиком — поэтому смены названы словами, а часов в сводке нет.
    """
    import re

    from app.kb.render import render_schedule_overview

    assert not re.search(r"\d{1,2}[:.]\d{2}", render_schedule_overview(kb))
