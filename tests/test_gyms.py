"""Подбор зала по тому, как о районе говорит родитель.

Клиент почти никогда не пишет так, как записано в базе: «кск», «возле рахата»,
«жанакала», «15й магазин», «6 мкр», «zhitikara». Два правила важнее качества
поиска:

* **зал не выдумывается никогда** — нет совпадения, значит пустой список и
  предложение показать полный перечень;
* **два зала в «Центре» не сливаются в один** — Касымханова 10 это Жана-Кала,
  Каирбекова 24 это «Рахат», абстрактного «центрального зала» не существует.
"""

from __future__ import annotations

import pytest

from app.tools.gyms import (
    MAX_DISTRICT_MATCHES,
    find_gym_by_district,
    fuzzy_threshold,
    get_gyms,
    is_fuzzy_match,
    levenshtein,
    normalize_text,
    translit_to_cyrillic,
)
from app.types import GapRef, Scope, ToolStatus


def ids_of(result) -> list[str]:
    """Список id залов из результата инструмента."""
    return [gym["id"] for gym in result.data["gyms"]]


# --------------------------------------------------------------------------- #
# Разговорные формы
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # КСК — головной зал
        ("кск", "ksk_kairbekova_334"),
        ("КСК", "ksk_kairbekova_334"),
        ("ksk", "ksk_kairbekova_334"),
        ("школа 9", "ksk_kairbekova_334"),
        ("школа №9", "ksk_kairbekova_334"),
        ("каирбекова 334", "ksk_kairbekova_334"),
        # Плаза
        ("плаза", "plaza_szm_70"),
        ("plaza", "plaza_szm_70"),
        ("костанай плаза", "plaza_szm_70"),
        ("аймед", "plaza_szm_70"),
        ("сзм", "plaza_szm_70"),
        # Жана-Кала
        ("жана кала", "center_kasymkhanova_10"),
        ("жанакала", "center_kasymkhanova_10"),
        ("жаңа қала", "center_kasymkhanova_10"),
        ("касымханова", "center_kasymkhanova_10"),
        # Рахат
        ("рахат", "center_kairbekova_24"),
        ("возле рахата", "center_kairbekova_24"),
        ("у магазина рахат", "center_kairbekova_24"),
        ("каирбекова 24", "center_kairbekova_24"),
        # 15-й магазин
        ("15й магазин", "magazin15_voinov_8b"),
        ("15-й магазин", "magazin15_voinov_8b"),
        ("15 магазин", "magazin15_voinov_8b"),
        ("романтик", "magazin15_voinov_8b"),
        # 6-й микрорайон
        ("6 мкр", "mkr6_arystanbekova_6"),
        ("6мкр", "mkr6_arystanbekova_6"),
        ("шестой микрорайон", "mkr6_arystanbekova_6"),
        ("арыстанбекова", "mkr6_arystanbekova_6"),
        # райцентры, в том числе транслитом
        ("житикара", "region_zhitikara"),
        ("zhitikara", "region_zhitikara"),
        ("федоровка", "region_fedorovka"),
        ("фёдоровка", "region_fedorovka"),
        ("карабалык", "region_karabalyk"),
        ("сарыколь", "region_sarykol"),
        ("сарыкөл", "region_sarykol"),
    ],
)
async def test_conversational_forms_find_the_right_gym(ctx, query, expected) -> None:
    """Разговорная форма, транслит и казахское написание ведут в тот же зал."""
    result = await find_gym_by_district(ctx, district_text=query)

    assert result.ok, f"{query!r}: инструмент вернул {result.status}"
    assert ids_of(result)[0] == expected, f"{query!r} -> {ids_of(result)}"
    assert len(result.data["gyms"]) <= MAX_DISTRICT_MATCHES


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("плаzа", "plaza_szm_70"),          # смешанная раскладка
        ("жанакола", "center_kasymkhanova_10"),  # опечатка
        ("житикора", "region_zhitikara"),
        ("карабалик", "region_karabalyk"),
    ],
)
async def test_typos_are_forgiven(ctx, query, expected) -> None:
    """Опечатка в одну-две буквы не должна ронять подбор."""
    result = await find_gym_by_district(ctx, district_text=query)

    assert result.ok
    assert ids_of(result)[0] == expected


# --------------------------------------------------------------------------- #
# Два зала в «Центре»
# --------------------------------------------------------------------------- #
async def test_two_central_gyms_are_never_merged(ctx) -> None:
    """«Центр» — это два разных зала, бот обязан переспросить про ориентир."""
    result = await find_gym_by_district(ctx, district_text="центр")

    assert result.ok
    found = ids_of(result)
    assert "center_kairbekova_24" in found
    assert "center_kasymkhanova_10" in found
    assert result.data["ambiguous"] is True
    assert any("ориентир" in caveat.lower() for caveat in result.caveats)


async def test_landmark_disambiguates_two_central_gyms(ctx) -> None:
    """Ориентир различает залы: «Рахат» и «Жана-Кала» — разные адреса."""
    rakhat = await find_gym_by_district(ctx, district_text="центр рахат")
    zhana = await find_gym_by_district(ctx, district_text="центр жана кала")

    assert ids_of(rakhat)[0] == "center_kairbekova_24"
    assert ids_of(zhana)[0] == "center_kasymkhanova_10"


async def test_house_number_is_compared_exactly(ctx) -> None:
    """Каирбекова 24 и Каирбекова 334 — два разных зала на одной улице."""
    result = await find_gym_by_district(ctx, district_text="каирбекова 24")

    assert ids_of(result)[0] == "center_kairbekova_24"
    # Одна опечатка не имеет права склеить дом 24 с домом 334.
    assert is_fuzzy_match("каирбекова 24", "каирбекова 334")[0] is False


# --------------------------------------------------------------------------- #
# Отсутствие совпадения
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("query", ["марс", "абракадабра", "ленинградская 5", "qqqq"])
async def test_no_match_never_invents_a_gym(ctx, query) -> None:
    """Нет совпадения — пустой список и полный перечень, а не «ближайший, наверное»."""
    result = await find_gym_by_district(ctx, district_text=query)

    assert result.ok
    assert result.data["gyms"] == []
    assert result.data["total"] == 0
    assert result.data["offer_full_list"] is True
    assert result.data["districts"], "модели нужен список реальных районов"
    assert any("придумывать нельзя" in caveat.lower() for caveat in result.caveats)


@pytest.mark.parametrize("query", ["наримановка", "юбилейный", "хбк", "кооператор"])
async def test_known_district_without_gym_is_named_honestly(ctx, query) -> None:
    """Район клиенту знаком, но зала там нет — об этом надо сказать прямо."""
    result = await find_gym_by_district(ctx, district_text=query)

    assert result.data["gyms"] == []
    assert result.data["known_district_without_gym"] is True
    assert any("зала там нет" in caveat.lower() for caveat in result.caveats)


@pytest.mark.parametrize("query", ["кжби", "kzhbi", "КЖБИ"])
async def test_unresolved_kzhbi_goes_to_operator(ctx, query) -> None:
    """Конфликт C-3: про КЖБИ нельзя утверждать ни «есть», ни «нет»."""
    result = await find_gym_by_district(ctx, district_text=query)

    assert result.ok is False
    assert result.status is ToolStatus.NEEDS_OPERATOR
    assert result.gap_ref is GapRef.C3
    assert set(result.say_if_no_data or {}) == {"ru", "kk"}


@pytest.mark.parametrize("query", ["", "   ", "\n"])
async def test_empty_query_is_invalid_input(ctx, query) -> None:
    """Пустой запрос — ошибка аргументов, а не «покажу что-нибудь»."""
    result = await find_gym_by_district(ctx, district_text=query)

    assert result.ok is False
    assert result.status is ToolStatus.INVALID_INPUT


# --------------------------------------------------------------------------- #
# get_gyms
# --------------------------------------------------------------------------- #
async def test_get_gyms_city_returns_six_open_gyms(ctx) -> None:
    """Костанай — шесть залов. Закреплённый креатив говорит «5» (конфликт C-1)."""
    result = await get_gyms(ctx, scope="city")

    assert result.ok
    assert result.data["total_in_scope"] == 6
    assert all(gym["scope"] == Scope.CITY.value for gym in result.data["gyms"])
    assert sum(1 for gym in result.data["gyms"] if gym["is_head"]) == 1


async def test_get_gyms_region_returns_all_settlements(ctx) -> None:
    """Райцентров семь. Адрес известен только у Тобыла, у остальных пробел G-3."""
    result = await get_gyms(ctx, scope="region")

    assert result.ok
    assert {gym["settlement"] for gym in result.data["gyms"]} == {
        "Карабалык",
        "Фёдоровка",
        "Сарыколь",
        "Аулиеколь",
        "Узынколь",
        "Житикара",
        # Тобыл добавлен 12.08.2026 вместе с расписанием; прежнее название —
        # Затобольск, по нему зал тоже обязан находиться.
        "Тобыл",
    }
    without_address = {g["settlement"] for g in result.data["gyms"] if g["address"] is None}
    assert without_address == {
        "Карабалык",
        "Фёдоровка",
        "Сарыколь",
        "Аулиеколь",
        "Узынколь",
        "Житикара",
    }, "адрес есть только у Тобыла — он пришёл вместе с расписанием"


async def test_get_gyms_warns_about_duplicate_districts(ctx) -> None:
    """В городе два «Центра» — модель обязана получить об этом предупреждение."""
    result = await get_gyms(ctx, scope="city")

    assert "центр" in result.data["duplicate_districts"]
    assert any("ориентир" in caveat.lower() for caveat in result.caveats)


async def test_get_gyms_unknown_settlement_does_not_offer_another_town(ctx) -> None:
    """Названного посёлка в базе нет — молча показать другие точки значит соврать."""
    result = await get_gyms(ctx, scope="region", settlement="Астана")

    assert result.ok
    assert result.data["gyms"] == []
    assert result.data["offer_full_list"] is True
    assert result.meta["settlement_matched"] is False


async def test_get_gyms_known_settlement_filters(ctx) -> None:
    """Названный посёлок сужает выдачу до него одного."""
    result = await get_gyms(ctx, scope="region", settlement="житикара")

    assert ids_of(result) == ["region_zhitikara"]
    assert result.data["settlement_filtered"] is True


@pytest.mark.parametrize("scope", ["", "область", "kostanay", None])
async def test_get_gyms_rejects_unknown_scope(ctx, scope) -> None:
    """Неизвестный scope — ошибка аргументов."""
    result = await get_gyms(ctx, scope=scope)

    assert result.ok is False
    assert result.status is ToolStatus.INVALID_INPUT


async def test_get_gyms_reports_missing_phone(ctx) -> None:
    """Телефона в базе нет (G-2) — карточка обязана это отражать."""
    result = await get_gyms(ctx, scope="city")

    assert all(gym["phone"] is None for gym in result.data["gyms"])
    assert result.caveats


async def test_city_gyms_carry_map_links(ctx) -> None:
    """У городских залов есть ссылка на 2ГИС: адрес без карты родителю мало помогает."""
    result = await get_gyms(ctx, scope="city")

    links = [gym["map_url"] for gym in result.data["gyms"]]
    assert all(links), f"зал без ссылки на карту: {links}"
    # Настоящие геоточки 2ГИС от владельца — они ведут прямо на объект,
    # в отличие от поисковых ссылок, которые могут промахнуться.
    assert all(str(url).startswith("https://2gis.kz/kostanay/geo/") for url in links)


async def test_region_gyms_have_map_link_only_with_address(ctx) -> None:
    """Ссылка на карту есть ровно там, где есть адрес.

    В большинстве райцентров адрес внутри посёлка не передан (G-3), и ссылка вела
    бы в никуда. Исключение — Тобыл: его адрес пришёл вместе с расписанием.
    """
    result = await get_gyms(ctx, scope="region")

    for gym in result.data["gyms"]:
        if gym["id"] == "region_tobyl":
            assert gym["map_url"], "у Тобыла есть адрес — должна быть и ссылка"
        else:
            assert gym["map_url"] is None, f'{gym["id"]}: ссылка без адреса'


async def test_tobyl_is_found_by_its_old_name(ctx) -> None:
    """Затобольск — прежнее название Тобыла, и зал там теперь есть.

    Раньше «затобольск» лежал в списке районов без зала, и родителю из Тобыла бот
    отвечал «зала там нет» — при том что зал есть и у него полное расписание.
    """
    for query in ("тобыл", "затобольск", "tobyl"):
        result = await find_gym_by_district(ctx, district_text=query)
        assert result.ok, f"{query}: {result.status}"
        assert ids_of(result)[0] == "region_tobyl", f"{query} -> {ids_of(result)}"


async def test_get_gyms_respects_limit(ctx) -> None:
    """Потолок выдачи соблюдается, и модели говорят, что список можно продолжить."""
    result = await get_gyms(ctx, scope="all", limit=2)

    assert len(result.data["gyms"]) == 2
    assert result.data["total_in_scope"] == 13
    assert any("не все" in caveat.lower() for caveat in result.caveats)


# --------------------------------------------------------------------------- #
# Нормализация
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Жаңа Қала!  ", "жана кала"),
        ("КСК", "кск"),
        ("6-й микрорайон", "6 й микрорайон"),
        ("Фёдоровка", "федоровка"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_text(raw, expected) -> None:
    """Казахские буквы и «ё» сворачиваются к русской основе: для клиента это одно слово."""
    assert normalize_text(raw) == expected


def test_translit_to_cyrillic() -> None:
    """Грубый транслит нужен клиентам, которые пишут русские слова латиницей."""
    assert translit_to_cyrillic("zhitikara") == "житикара"
    assert translit_to_cyrillic("shkola") == "школа"


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [("кск", "кск", 0), ("кск", "ксм", 1), ("", "кск", 3), ("плаза", "плаzа", 1)],
)
def test_levenshtein(left, right, expected) -> None:
    """Своя реализация расстояния: внешних зависимостей ради одной функции нет."""
    assert levenshtein(left, right) == expected


@pytest.mark.parametrize(
    ("length", "expected"), [(3, 0), (4, 0), (5, 1), (6, 1), (7, 2), (10, 2), (11, 3)]
)
def test_fuzzy_threshold(length, expected) -> None:
    """Коротким строкам опечатки не прощаются: «кск» и «ксм» — разные слова."""
    assert fuzzy_threshold(length) == expected


def test_short_strings_require_exact_match() -> None:
    """Три-четыре буквы сравниваются точно, иначе «кск» поймает половину базы."""
    assert is_fuzzy_match("кск", "ксм")[0] is False
    assert is_fuzzy_match("кск", "кск")[0] is True


async def test_nearby_district_is_not_the_same_as_being_there(kb) -> None:
    """Район, который зал закрывает, не выдаётся за район, где зал стоит.

    Владелец сказал: «на Полевой зал отвечает и за Аэропорт, там недалеко».
    Записать «аэропорт» в обычные алиасы было бы проще всего — и бот начал бы
    говорить «наш зал в Аэропорту». Человек поехал бы искать его там.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.tools.gyms import find_gym_by_district
    from app.types import ChannelKind, Language, ToolContext
    from tests.conftest import RecordingServices

    ctx = ToolContext(
        conversation_id=uuid4(),
        conv_key="c",
        channel=ChannelKind.TELEGRAM,
        channel_id="tg",
        chat_id="1",
        lang=Language.RU,
        kb=kb,
        kb_hash=kb.kb_hash,
        now=datetime(2026, 8, 20, tzinfo=UTC),
        correlation_id="t",
        services=RecordingServices(),
    )
    result = await find_gym_by_district(ctx, district_text="Аэропорт")

    assert result.ok and result.data["gyms"], "ближайший зал не предложен"
    assert all(gym["match"] == "nearby" for gym in result.data["gyms"])
    assert result.data["nearby_only"] is True
    assert "зала НЕТ" in result.caveats[0], "модель не предупреждена, что зала там нет"


async def test_generic_word_does_not_match_a_gym(kb) -> None:
    """Одно служебное слово не должно приводить чужой зал.

    На «Западный микрорайон» бот предлагал 6-й микрорайон: совпало слово
    «микрорайон», которое есть в обоих. Клиенту это выглядит как «бот не понял».
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.tools.gyms import find_gym_by_district
    from app.types import ChannelKind, Language, ToolContext
    from tests.conftest import RecordingServices

    ctx = ToolContext(
        conversation_id=uuid4(),
        conv_key="c",
        channel=ChannelKind.TELEGRAM,
        channel_id="tg",
        chat_id="1",
        lang=Language.RU,
        kb=kb,
        kb_hash=kb.kb_hash,
        now=datetime(2026, 8, 20, tzinfo=UTC),
        correlation_id="t",
        services=RecordingServices(),
    )
    result = await find_gym_by_district(ctx, district_text="Западный микрорайон")
    found = {gym["id"] for gym in result.data.get("gyms", [])}
    assert "mkr6_arystanbekova_6" not in found, "6-й микрорайон приехал по слову «микрорайон»"

    # А точный запрос по-прежнему находит нужный зал.
    exact = await find_gym_by_district(ctx, district_text="6 микрорайон")
    assert exact.data["gyms"][0]["id"] == "mkr6_arystanbekova_6"


async def test_unknown_district_offers_the_whole_list(kb) -> None:
    """Незнакомый район — повод показать ВСЕ залы, а не три на выбор бота."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.tools.gyms import find_gym_by_district
    from app.types import ChannelKind, Language, ToolContext
    from tests.conftest import RecordingServices

    ctx = ToolContext(
        conversation_id=uuid4(),
        conv_key="c",
        channel=ChannelKind.TELEGRAM,
        channel_id="tg",
        chat_id="1",
        lang=Language.RU,
        kb=kb,
        kb_hash=kb.kb_hash,
        now=datetime(2026, 8, 20, tzinfo=UTC),
        correlation_id="t",
        services=RecordingServices(),
    )
    result = await find_gym_by_district(ctx, district_text="Марсианская улица")

    assert result.data["offer_full_list"] is True
    assert result.data["full_list_artifact"] == "gyms_list_city"
    assert "gyms_list_city" in result.caveats[0]


def test_menu_choice_four_hands_over_to_a_human(kb) -> None:
    """Пункт «Написать менеджеру» передаёт диалог человеку без обращения к модели.

    Цифра разворачивается во фразу со словом из словаря интентов, а её ловит
    guard — до вызова модели. Так пункт отрабатывает мгновенно и одинаково,
    а не зависит от того, как модель поймёт цифру.
    """
    from app.core.guards import scan
    from app.core.pipeline import expand_menu_choice
    from app.types import EscalationReason, GuardFlag, Language

    expanded = expand_menu_choice("4", after_greeting=True)
    assert expanded != "4", "цифра не развернулась в фразу"

    verdict = scan(expanded, lang=Language.RU, lexicon=kb.lexicon, policies=kb.policies)
    assert verdict.escalate is True
    assert verdict.reason is EscalationReason.USER_REQUEST
    assert GuardFlag.MANAGER_REQUEST in verdict.flags
    assert verdict.fixed_reply_key == "escalation.manager_requested"


def test_manager_request_understands_word_forms(kb) -> None:
    """Живые формулировки тоже доходят до человека.

    Клиент пишет «напишите администратору» или «свяжите с менеджером», а не
    словарную форму «менеджер».
    """
    from app.core.guards import scan
    from app.types import Language

    for text in (
        "Хочу написать менеджеру",
        "Свяжите меня с менеджером",
        "Передайте администратору, пожалуйста",
    ):
        verdict = scan(text, lang=Language.RU, lexicon=kb.lexicon, policies=kb.policies)
        assert verdict.escalate is True, text


def test_greeting_offers_the_manager_option(kb) -> None:
    """В приветствии есть пункт «Написать менеджеру» на обоих языках."""
    from app.types import Language

    assert "4." in kb.text("greeting.first", Language.RU)
    assert "менеджер" in kb.text("greeting.first", Language.RU).lower()
    assert "4." in kb.text("greeting.first", Language.KK)
