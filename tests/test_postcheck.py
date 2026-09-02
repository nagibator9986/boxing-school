"""Анти-галлюцинационный пост-фильтр — самый дорогой модуль набора.

Проверяется симметрично, и это принципиально:

* **пропущенная выдумка** — родителю названа цена, адрес, время или телефон,
  которых школа не подтверждала. Дальше либо спор на кассе, либо клиент едет
  по несуществующему адресу;
* **ложное срабатывание** — фильтр молча съел нормальный ответ («сыну 8 лет»,
  «12 занятий», «6 мкр»), клиент получил заглушку и ушёл к человеку. Это тоже
  дефект, просто менее заметный: он не падает в логах красным.

Пограничные случаи сформулированы от предметной области (что реально пишет бот
школы бокса родителю), а не от текущих регулярок.
"""

from __future__ import annotations

import pytest

from app.core.postcheck import (
    MAX_REPLY_CHARS,
    check,
    extract_numbers,
    extract_times,
    has_prompt_leak,
    normalize_number,
)
from app.types import Language, PostcheckFailKind, ToolResult

from tests.conftest import invocation

NO_TOOLS: list = []
NO_NGRAMS: frozenset[str] = frozenset()


def verdict(
    text: str,
    kb,
    *,
    invocations=None,
    lang=Language.RU,
    strict: bool = False,
    known_phones=(),
):
    """Короткий вызов пост-фильтра с общими для тестов умолчаниями."""
    return check(
        text,
        invocations=NO_TOOLS if invocations is None else invocations,
        lang=lang,
        kb=kb,
        prompt_ngrams=NO_NGRAMS,
        strict=strict,
        known_phones=known_phones,
    )


PRICE_CALL = invocation(
    "calculate_price",
    {
        "currency": "KZT",
        "scope": "city",
        "settlement": "Костанай",
        "plan": "standard",
        "children_count": 1,
        "sessions_included": 12,
        "validity_days": 30,
        "price_per_child_base": 25000,
        "per_child": [{"index": 1, "discount_pct": 0, "price": 25000}],
        "total": 25000,
        "price_per_session": 2083,
    },
)


# --------------------------------------------------------------------------- #
# 1. Выдумка обязана блокироваться
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "Абонемент стоит 25 000 ₸ в месяц.",
        "Разовая тренировка 3200 тенге.",
        "Абонемент обойдётся в 20 тысяч.",
        "Цена вопроса — 18000.",
        "Стоимость 25000 тг, приходите.",
        "Абонемент — 25 000 теңге.",
    ],
)
def test_invented_money_is_blocked(kb, text) -> None:
    """Цена без подтверждения калькулятором наружу не уходит."""
    result = verdict(text, kb)

    assert result.ok is False
    assert result.kind is PostcheckFailKind.MONEY
    assert result.text == ""  # неподтверждённый текст физически нельзя отправить


def test_price_different_from_tool_result_is_blocked(kb) -> None:
    """Калькулятор посчитал 25 000, модель написала 30 000 — это выдумка."""
    result = verdict("Абонемент — 30 000 ₸.", kb, invocations=[PRICE_CALL])

    assert result.ok is False
    assert result.kind is PostcheckFailKind.MONEY
    assert result.offending == ("30 000",)


def test_invented_discount_percent_is_blocked(kb) -> None:
    """Процент скидки — те же деньги: 20 % в прайсе нет."""
    result = verdict("Для второго ребёнка скидка 20 %.", kb, invocations=[PRICE_CALL])

    assert result.ok is False
    assert result.kind is PostcheckFailKind.MONEY


def test_invented_time_is_blocked_when_schedule_unknown(kb) -> None:
    """Правило G-1: расписания в базе нет, любое время занятий — выдумка."""
    result = verdict("Приходите к 18:00, тренировка длится час.", kb)

    assert result.ok is False
    assert result.kind is PostcheckFailKind.TIME


def test_time_blocked_even_if_schedule_tool_returned_no_data(kb) -> None:
    """``get_schedule`` вернул «нет данных» — время всё равно называть нельзя."""
    empty = invocation(
        "get_schedule",
        result=ToolResult.no_data(say={"ru": "уточню", "kk": "нақтылаймын"}),
    )
    result = verdict("Занятия в 17:30.", kb, invocations=[empty])

    assert result.ok is False
    assert result.kind is PostcheckFailKind.TIME


def test_invented_weekday_is_blocked(kb) -> None:
    """Дни недели подтверждаются только расписанием."""
    result = verdict("Тренировки по понедельникам и средам.", kb)

    assert result.ok is False
    assert result.kind is PostcheckFailKind.WEEKDAY


@pytest.mark.parametrize(
    "text",
    [
        "Звоните: +7 705 123 45 67.",
        "Наш номер 87051234567.",
        "Свяжитесь по 8-705-123-45-67.",
        # Городской Костаная — та же выдумка, просто в другой записи.
        "Свяжитесь по 8 (7142) 54-32-10.",
        "Наш городской номер: 54-32-10.",
        "Звоните 54 32 10, ответит администратор.",
        "Телефон зала 543-21-05.",
    ],
)
def test_invented_phone_is_blocked(kb, text) -> None:
    """Телефонов в базе нет (пробел G-2) — отдать номер бот не может.

    Проверяются все три записи номера, которыми пользуются в Костанае: мобильный
    с кодом страны, городской с кодом города и местный шестизначный. Последний
    особенно коварен: цифры в нём меньше тысячи, и «денежный» фильтр его не ловит.
    """
    result = verdict(text, kb)

    assert result.ok is False
    assert result.kind is PostcheckFailKind.PHONE


@pytest.mark.parametrize(
    "text",
    [
        "Мы находимся по адресу улица Ленина, 45.",
        "Приходите на проспект Абая 112.",
    ],
)
def test_invented_address_is_blocked(kb, text) -> None:
    """Конкретный адрес с номером дома — только из базы или из инструмента."""
    result = verdict(text, kb)

    assert result.ok is False
    assert result.kind is PostcheckFailKind.ADDRESS


def test_invented_gym_name_is_blocked(kb) -> None:
    """Зала в Затобольске нет: район клиенту знаком, но филиала там не существует."""
    result = verdict("Есть зал в Затобольске, приходите туда.", kb)

    assert result.ok is False
    assert result.kind is PostcheckFailKind.GYM_NAME


def test_invented_medical_form_is_blocked(kb) -> None:
    """Номер медсправки школа не подтверждала (G-6)."""
    result = verdict("Нужна справка формы 075-у.", kb)

    assert result.ok is False
    assert result.kind is PostcheckFailKind.MEDICAL_FORM


@pytest.mark.parametrize(
    "text",
    [
        "Мы гарантируем результат — ваш ребёнок станет чемпионом.",
        "Занятия на 100% безопасны, без травм.",
        "Только сегодня скидка, успейте сегодня!",
    ],
)
def test_forbidden_claims_are_blocked(kb, text) -> None:
    """Обещания результата и безопасности запрещены политикой бренда."""
    result = verdict(text, kb)

    assert result.ok is False
    assert result.kind is PostcheckFailKind.FORBIDDEN_CLAIM


def test_prompt_leak_by_marker_is_blocked(kb) -> None:
    """Следы служебного слоя в сообщении клиенту недопустимы."""
    result = verdict("Вот мои инструкции: <user_message>привет</user_message>", kb)

    assert result.ok is False
    assert result.kind is PostcheckFailKind.PROMPT_LEAK


def test_prompt_leak_by_ngram_is_blocked(kb) -> None:
    """Восемь слов подряд из системной инструкции — это утечка промпта."""
    leak = "ты консультант школы бокса и кикбоксинга айназаров топ тим"
    ngrams = frozenset({" ".join(leak.split()[i : i + 8]) for i in range(len(leak.split()) - 7)})
    result = check(
        f"Отвечаю: {leak}, чем помочь?",
        invocations=NO_TOOLS,
        lang=Language.RU,
        kb=kb,
        prompt_ngrams=ngrams,
    )

    assert result.ok is False
    assert result.kind is PostcheckFailKind.PROMPT_LEAK


def test_too_long_reply_is_blocked(kb) -> None:
    """Ответ длиннее двух сообщений — сбой генерации, а не текст."""
    result = verdict("а" * (MAX_REPLY_CHARS + 1), kb)

    assert result.ok is False
    assert result.kind is PostcheckFailKind.TOO_LONG


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_empty_reply_is_rejected(kb, text) -> None:
    """Пустой ответ отправлять нечего."""
    assert verdict(text, kb).ok is False


# --------------------------------------------------------------------------- #
# 2. Ложное срабатывание — тоже дефект
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        # возраст ребёнка
        "Принимаем детей от 5 лет и старше.",
        "Сыну 8 лет — это как раз наша группа.",
        "Дочке 6 годиков, возьмём.",
        "8-летний ребёнок занимается в младшей группе.",
        "Мальчику 5-ти лет тоже можно.",
        "Ребёнок 2018 года рождения — подходит.",
        "Нам уже 10 лет, школа работает давно.",
        # количество детей и занятий
        "Записываем двоих детей из одной семьи.",
        "У нас 2 ребёнка в группе новичков.",
        "В абонемент входит 12 занятий на месяц.",
        "Абонемент рассчитан на 12 занятий и действует 30 дней.",
        "Тренировки 3 раза в неделю.",
        "В группе до 15 человек.",
        "Занятие длится 60 минут.",
        # география: номера микрорайонов, домов, школ
        "Наш зал в 6 микрорайоне, возле школы №10.",
        "Есть зал в 6 мкр и в 15 магазине.",
        "Занимаемся в школе 10, вход с торца.",
        "Ждём вас в зале у школы №9.",
        "У нас 6 залов в Костанае.",
        # казахский
        "Балаңыз 7 жаста ма? 5 жастан бастап қабылдаймыз.",
        "Абонементте 12 сабақ бар.",
        # перечисления чисел подряд — это не телефон
        "Мы работаем 10 20 30 лет? нет, 15 лет.",
        "Занятия идут 60 90 120 минут в разных группах.",
        # честное «не знаю»
        "Стоимость уточнит администратор, я не называю суммы наугад.",
        "Расписание подскажет администратор, я передам ваш вопрос.",
    ],
)
def test_neutral_facts_are_not_blocked(kb, text) -> None:
    """Возраст, количество, номер микрорайона и номер школы деньгами не являются.

    Каждое ложное срабатывание здесь — это молча съеденный нормальный ответ.
    """
    result = verdict(text, kb)

    assert result.ok is True, f"ложное срабатывание {result.kind}: {result.offending}"
    assert result.text == text


def test_confirmed_money_passes(kb) -> None:
    """Цена, посчитанная калькулятором, проходит вместе со всей арифметикой хода."""
    text = (
        "Стандартный абонемент — 25 000 ₸ за 12 занятий, "
        "одно занятие выходит примерно 2 083 ₸."
    )
    result = verdict(text, kb, invocations=[PRICE_CALL])

    assert result.ok is True
    assert result.text == text


def test_confirmed_discount_percent_passes(kb) -> None:
    """10 % пришли из разбивки калькулятора — это подтверждённое число."""
    call = invocation(
        "calculate_price",
        {
            "total": 47500,
            "per_child": [
                {"index": 1, "discount_pct": 0, "price": 25000},
                {"index": 2, "discount_pct": 10, "price": 22500},
            ],
        },
    )
    result = verdict("На второго ребёнка скидка 10 %: 22 500 ₸.", kb, invocations=[call])

    assert result.ok is True


def test_confirmed_time_and_weekday_pass(kb) -> None:
    """Расписание пришло инструментом — время и день можно называть."""
    call = invocation(
        "get_schedule",
        {"slots": [{"day": "mon", "time_start": "18:00", "time_end": "19:00"}]},
    )
    result = verdict("В понедельник тренировка в 18:00.", kb, invocations=[call])

    assert result.ok is True


def test_weekday_with_hedge_is_not_blocked(kb) -> None:
    """Бот не утверждает день, а обещает уточнить — претензии нет."""
    result = verdict(
        "Скорее всего понедельник, но точное расписание уточнит администратор.", kb
    )

    assert result.ok is True


def test_address_from_knowledge_base_passes(kb) -> None:
    """Адрес зала взят из реестра KB — это по определению не выдумка."""
    result = verdict("Мы находимся по адресу Каирбекова 334, школа №9.", kb)

    assert result.ok is True


def test_gym_name_from_registry_passes(kb) -> None:
    """Название зала есть в реестре — блокировать нечего."""
    result = verdict("Ближайший зал в Житикаре, запишем вас туда.", kb)

    assert result.ok is True


def test_district_without_house_number_is_not_an_address_claim(kb) -> None:
    """Упоминание района без номера дома фактом-адресом не считается."""
    result = verdict("Мы работаем в районе КСК и в Костанай Плазе.", kb)

    assert result.ok is True


# --------------------------------------------------------------------------- #
# 3. Строгий режим
# --------------------------------------------------------------------------- #
def test_strict_mode_blocks_even_neutral_numbers(kb) -> None:
    """После тревоги охраны запрещено любое неподтверждённое число."""
    loose = verdict("Сыну 8 лет — берём.", kb)
    strict = verdict("Сыну 8 лет — берём.", kb, strict=True)

    assert loose.ok is True
    assert strict.ok is False
    assert strict.kind is PostcheckFailKind.MONEY


def test_strict_mode_allows_numbers_from_tool_data(kb) -> None:
    """Строгий режим не запрещает то, что пришло из инструмента."""
    result = verdict(
        "Абонемент — 25 000 ₸ за 12 занятий.", kb, invocations=[PRICE_CALL], strict=True
    )

    assert result.ok is True


def test_strict_mode_allows_text_without_numbers(kb) -> None:
    """Ответ без чисел строгий режим не трогает."""
    result = verdict("Здравствуйте! Подскажите, в каком районе вам удобно?", kb, strict=True)

    assert result.ok is True


# --------------------------------------------------------------------------- #
# 4. Разбор чисел и времени
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("18:30", ("18:30",)),
        ("18.30", ("18:30",)),
        ("с 9:00 до 21:00", ("09:00", "21:00")),
        ("25:70", ()),          # не время
        ("25.000", ()),         # цена не имеет права блокировать сама себя
        ("итого 3 200 ₸", ()),
    ],
)
def test_extract_times(text, expected) -> None:
    """Время приводится к каноническому ``HH:MM``; ценам сюда попадать нельзя."""
    assert extract_times(text) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("25 000 ₸", "25000"),
        ("25.000", "25000"),
        ("25000", "25000"),
        ("", ""),
    ],
)
def test_normalize_number(raw, expected) -> None:
    """Разделители разрядов в живом тексте пишут как угодно — сравниваем цифры."""
    assert normalize_number(raw) == expected


def test_extract_numbers_keeps_written_form() -> None:
    """Числа возвращаются так, как написаны: их показывают в логе отказа."""
    assert extract_numbers("абонемент 25 000 ₸ на 12 занятий") == ("25 000", "12")


def test_has_prompt_leak_ignores_empty_ngrams() -> None:
    """Промпт не передали — блокировать по отсутствию данных нельзя."""
    assert has_prompt_leak("любой текст из восьми и более слов подряд тут", frozenset()) is False


# --------------------------------------------------------------------------- #
# 5. Регрессии состязательного ревью
#
# Каждый тест ниже воспроизводит найденный ревью отказ на живом диалоге и
# падает на коде до правки. Половина из них — про пропущенную выдумку, половина
# про ложное срабатывание: обе стороны фильтра стоят одинаково дорого.
# --------------------------------------------------------------------------- #
SCHEDULE_CALL = invocation(
    "get_schedule",
    {
        "gym_id": "ksk-school-9",
        "slots": [
            {"days": ["mon", "wed"], "time_start": "18:00", "time_end": "19:30"},
        ],
    },
)
SCHEDULE_NO_DATA = invocation(
    "get_schedule",
    result=ToolResult.no_data(say={"ru": "уточнит администратор", "kk": "әкімші нақтылайды"}),
)


def _schedule_for(days: list[str]):
    """Расписание из инструмента ровно на перечисленные дни."""
    return invocation(
        "get_schedule",
        {"slots": [{"days": days, "time_start": "18:00", "time_end": "19:30"}]},
    )


@pytest.mark.parametrize(
    ("text", "days"),
    [
        # «сенбі» (суббота) — подстрока пяти из семи казахских дней недели.
        ("Сабақтар дүйсенбі және сәрсенбі күндері 18:00-де өтеді.", ["mon", "wed"]),
        ("Сейсенбі мен бейсенбі күндері топ бар.", ["tue", "thu"]),
        ("Жексенбі күні де топ жұмыс істейді.", ["sun"]),
        ("Дуйсенби мен сарсенби кундери сабак бар.", ["mon", "wed"]),
    ],
)
def test_kazakh_weekday_is_not_mistaken_for_saturday(kb, text, days) -> None:
    """Казахский день недели не обвиняется в выдуманной субботе.

    Без границы слова ``сенбі`` находилась внутри дүй-сенбі, сей-сенбі,
    сәр-сенбі, бей-сенбі и жек-сенбі: любой казахоязычный ответ с днём недели
    блокировался, бот уходил на паузу и замолкал для клиента навсегда.

    Дни в каждом случае подтверждены ``data`` вызова — придраться фильтру не к
    чему, и «суббота», которой в тексте нет, тем более не повод.
    """
    result = verdict(text, kb, invocations=[_schedule_for(days)], lang=Language.KK)

    assert result.ok is True, f"ложное срабатывание {result.kind}: {result.offending}"


def test_real_kazakh_saturday_is_still_blocked(kb) -> None:
    """Граница слова не ослабляет правило: настоящая выдуманная «сенбі» ловится."""
    result = verdict("Сабақтар сенбі күні өтеді.", kb, invocations=[SCHEDULE_CALL], lang=Language.KK)

    assert result.ok is False
    assert result.kind is PostcheckFailKind.WEEKDAY
    assert result.offending == ("сенбі",)


def test_schedule_gap_in_one_gym_does_not_block_time_of_another(kb) -> None:
    """Расписание заполняется поэтапно: пробел одного зала не режет время другого.

    Владелец завёл расписание головного зала и ещё не завёл соседнему. Время
    18:00 пришло из ``data`` успешного вызова и обязано дойти до клиента.
    """
    result = verdict(
        "В зале на Каирбекова занятия в 18:00, по второму залу расписание уточнит администратор.",
        kb,
        invocations=[SCHEDULE_CALL, SCHEDULE_NO_DATA],
    )

    assert result.ok is True, f"ложное срабатывание {result.kind}: {result.offending}"


def test_hours_from_other_tool_data_are_confirmed_time(kb) -> None:
    """Часы работы администратора пришли из ``data`` — это не выдумка расписания."""
    contacts = invocation(
        "get_kb_fact",
        {"topic": "contacts", "text": "Администратор отвечает с 10:00 до 20:00"},
    )
    result = verdict("Администратор отвечает с 10:00 до 20:00.", kb, invocations=[contacts])

    assert result.ok is True, f"ложное срабатывание {result.kind}: {result.offending}"


@pytest.mark.parametrize(
    "text",
    [
        "Тренировки проходят с 18 до 20 часов.",
        "Группы для малышей занимаются в 10 утра, старшие — вечером.",
        "Приходите к 18 часам.",
        "Сабақтар сағат 18-де басталады.",
        "Занятия идут до 20 часов.",
    ],
)
def test_time_written_in_words_is_blocked(kb, text) -> None:
    """Смена формата не обходит правило G-1: словесный час — тоже время.

    Расписания у школы нет ни в одном зале, поэтому любой час занятий выдуман,
    в каком бы виде он ни был записан. Раньше проверялся только ``HH:MM``, и
    родитель по «с 18 до 20 часов» приводил ребёнка к закрытой двери.
    """
    result = verdict(text, kb, invocations=[SCHEDULE_NO_DATA])

    assert result.ok is False
    assert result.kind is PostcheckFailKind.TIME


@pytest.mark.parametrize(
    "text",
    [
        "Занятие длится 2 часа.",
        "Тренировка продолжается около 2 часов.",
        "В абонемент входит 12 занятий по 60 минут.",
    ],
)
def test_duration_in_hours_is_not_a_time_claim(kb, text) -> None:
    """Длительность занятия — не час суток: расписания она не утверждает."""
    result = verdict(text, kb, invocations=[SCHEDULE_NO_DATA])

    assert result.ok is True, f"ложное срабатывание {result.kind}: {result.offending}"


def test_word_time_confirmed_by_schedule_passes(kb) -> None:
    """Словесный час, совпавший с расписанием из ``data``, доходит до клиента."""
    result = verdict("Тренировка начинается в 18 часов.", kb, invocations=[SCHEDULE_CALL])

    assert result.ok is True, f"ложное срабатывание {result.kind}: {result.offending}"


@pytest.mark.parametrize(
    "text",
    [
        "Записал: Айдана, 8 лет, номер +7 705 123 45 67. Всё верно?",
        "Спасибо! Номер 8 705 123 45 67 записан, тренер свяжется с вами.",
        "Ваш номер 87051234567 сохранён.",
    ],
)
def test_own_phone_of_the_client_passes(kb, text) -> None:
    """Номер, который родитель продиктовал сам, — подтверждённый факт.

    Подтверждение записи блокировалось ровно на точке конверсии: лид уже готов,
    а клиент вместо «записали» получал заглушку и уход к человеку.
    """
    result = verdict(text, kb, known_phones=["+7 705 123 45 67"])

    assert result.ok is True, f"ложное срабатывание {result.kind}: {result.offending}"


def test_phone_other_than_the_clients_own_is_still_blocked(kb) -> None:
    """Известный номер клиента не легализует любой другой номер в ответе."""
    result = verdict(
        "Звоните администратору: +7 701 999 88 77.", kb, known_phones=["+7 705 123 45 67"]
    )

    assert result.ok is False
    assert result.kind is PostcheckFailKind.PHONE


@pytest.mark.parametrize(
    "text",
    [
        "Ждём вас на Гоголя, 15.",
        "Приходите на Абая, 42 — это рядом с базаром.",
        "Наш зал: Тәуелсіздік, 88.",
    ],
)
def test_address_without_street_word_is_checked(kb, text) -> None:
    """Адрес в стиле самого KB («Каирбекова, 334») проверяется наравне с «ул.».

    Регулярка требовала слова «улица» или вводного оборота, а kb/gyms.yaml
    пишет адреса без них — модель копирует этот стиль, и выдуманный адрес
    уходил родителю без единой проверки.
    """
    result = verdict(text, kb)

    assert result.ok is False
    assert result.kind is PostcheckFailKind.ADDRESS


@pytest.mark.parametrize(
    "text",
    [
        "Ждём вас на Каирбекова, 334.",
        "Приходите на Касымханова, 10.",
    ],
)
def test_address_from_registry_without_street_word_passes(kb, text) -> None:
    """Тот же формат, но адрес настоящий — блокировать нечего."""
    result = verdict(text, kb)

    assert result.ok is True, f"ложное срабатывание {result.kind}: {result.offending}"


@pytest.mark.parametrize(
    "text",
    [
        "Записал: Айдана, 8.",
        "Хорошо, Айдана, 8. Ждём вас на пробное занятие.",
        "Записываю: Ержан, 12. Тренер свяжется с вами.",
    ],
)
def test_name_with_age_is_not_an_address(kb, text) -> None:
    """«Имя, число» — это ребёнок и возраст, а не улица и дом.

    Шаблон «Заглавное слово, число» без слова-указателя места превращал
    подтверждение записи в «выдуманный адрес» и обрывал диалог.
    """
    result = verdict(text, kb)

    assert result.ok is True, f"ложное срабатывание {result.kind}: {result.offending}"


@pytest.mark.parametrize(
    "text",
    [
        "Приходите на пробное — оно бесплатное, 0 тенге.",
        "Первое занятие — 0 ₸.",
    ],
)
def test_zero_price_is_not_unconfirmed_money(kb, text) -> None:
    """Ноль — отсутствие цены, а не цена: калькулятор нулевых сумм не считает.

    Фраза про бесплатное пробное — главная точка конверсии, и она съедалась
    фильтром, хотя словесное «бесплатно» он и так не проверяет.
    """
    result = verdict(text, kb)

    assert result.ok is True, f"ложное срабатывание {result.kind}: {result.offending}"
    assert result.text == text


def test_zero_is_still_suspicious_in_strict_mode(kb) -> None:
    """После тревоги охраны неподтверждённое число остаётся неподтверждённым."""
    result = verdict("Пробное бесплатное, 0 тенге.", kb, strict=True)

    assert result.ok is False
    assert result.kind is PostcheckFailKind.MONEY


# --------------------------------------------------------------------------- #
# 6. Регрессия живого прогона: детектор утечки против фактов из базы знаний
# --------------------------------------------------------------------------- #
def _leak_ngrams(kb):
    """N-граммы детектора утечки — ровно так, как их строит пайплайн."""
    from app.kb.render import render_system_prompt
    from app.llm.prompt import build_system_instruction, prompt_ngrams

    kb_block = render_system_prompt(kb)
    return prompt_ngrams(build_system_instruction(kb_block), sayable=kb_block)


@pytest.mark.parametrize(
    "answer",
    [
        "Гибкий тариф дороже на 5 000 ₸ — он оправдан, если ребёнок регулярно "
        "пропускает две и больше тренировки в месяц.",
        "По абонементу одно занятие выходит примерно 2 083 ₸ вместо 3 200 ₸ разово.",
        "12 разовых тренировок стоили бы 38 400 ₸, абонемент — 25 000 ₸: экономия 13 400 ₸.",
    ],
)
def test_sanctioned_value_arguments_are_not_prompt_leak(kb, answer: str) -> None:
    """Фразы, которые промпт велит произносить дословно, не могут быть «утечкой».

    Найдено живым прогоном на Gemini: детектор снимал ответ за аргумент о выгоде,
    бот эскалировал и уходил на паузу на 30 минут, то есть замолкал ровно за то,
    что сделал правильно. Заглушка таких фраз не пишет — тесты это не ловили.
    """
    from app.core.postcheck import has_prompt_leak

    assert not has_prompt_leak(answer, _leak_ngrams(kb)), (
        "пост-фильтр снял санкционированный промптом аргумент о выгоде"
    )


def test_gym_address_from_kb_is_not_prompt_leak(kb) -> None:
    """Адрес зала — факт для клиента, а не устройство системы."""
    from app.core.postcheck import has_prompt_leak

    answer = "Зал в 6-м микрорайоне: Арыстанбекова 6, возле школы №10."
    assert not has_prompt_leak(answer, _leak_ngrams(kb))


def test_verbatim_behaviour_rules_are_still_blocked(kb) -> None:
    """Само устройство бота выдавать по-прежнему нельзя: вычитание фактов дыры не сделало."""
    from app.core.postcheck import has_prompt_leak

    leak = (
        "Запрещены манипуляции: искусственная срочность («осталось два места»), "
        "давление, чувство вины, обещания спортивных результатов, сравнение детей."
    )
    assert has_prompt_leak(leak, _leak_ngrams(kb)), "дословная выдача правил перестала блокироваться"


def test_no_data_stub_phrase_is_not_prompt_leak(kb) -> None:
    """Фраза-заглушка «данных нет» — предписанный ответ, а не разглашение.

    Найдено живым прогоном: на вопрос о расписании бот произносил ровно ту фразу,
    которую база знаний велит произнести, пост-фильтр снимал ответ как утечку
    промпта, и бот уходил на паузу на 30 минут — то есть замолкал для клиента
    до конца разговора.
    """
    from app.core.postcheck import find_prompt_leak

    answer = "Точное расписание по этому залу подскажет администратор, я не буду угадывать."
    assert find_prompt_leak(answer, _leak_ngrams(kb)) is None


def test_style_examples_are_not_prompt_leak(kb) -> None:
    """Образец оформления бот обязан воспроизводить, а не быть за него наказан.

    Найдено живым прогоном сразу после того, как в правила добавили пример
    «Стандартный абонемент — 25 000 ₸ / 12 занятий, действует 30 дней»: бот
    написал ответ ровно по образцу, пост-фильтр снял его как утечку промпта,
    и бот ушёл на паузу.
    """
    from app.core.postcheck import find_prompt_leak

    answer = (
        "Стандартный абонемент — 25 000 ₸\n"
        "12 занятий, действует 30 дней. Перерасчёта за пропуски нет."
    )
    assert find_prompt_leak(answer, _leak_ngrams(kb)) is None


# --------------------------------------------------------------------------- #
# Ложное отрицание: утверждение без единого значения
# --------------------------------------------------------------------------- #
FALSE_DENIALS: tuple[str, ...] = (
    "Утренних групп у нас нет, все занятия проходят во второй половине дня, "
    "поэтому прийти утром не получится.",
    "У нас нет утренних групп.",
    "Прийти утром не получится.",
    "Все занятия проходят вечером.",
    "Занятия идут только по вечерам.",
)


@pytest.mark.parametrize("text", FALSE_DENIALS)
def test_denying_morning_groups_is_blocked(text: str, kb) -> None:
    """Живой ответ 02.09.2026, на который владелец написал «не верная информация».

    Обычные проверки ловят выдуманные значения: цену, время, адрес. Здесь нет
    ни одного значения — проверять нечего, и такой ответ проходил насквозь.
    А цена ошибки выше, чем у выдуманной цифры: клиенту говорят, что школа ему
    не подходит, и он уходит.
    """
    result = verdict(text, kb)

    assert not result.ok
    assert result.kind is PostcheckFailKind.FALSE_DENIAL


def test_statement_about_one_gym_is_not_touched(kb) -> None:
    """«В этом зале занятия во второй половине дня» — правда для большинства залов.

    Проверка обязана ловить общие отрицания, а не любое упоминание вечера:
    снятый верный ответ — это молчащий бот.
    """
    assert verdict("В этом зале занятия проходят во второй половине дня.", kb).ok


def test_denial_is_allowed_when_the_schedule_agrees(kb) -> None:
    """Если утренних занятий в базе знаний нет, отрицание — правда, а не ошибка."""
    empty = kb.model_copy(
        update={
            "gyms": kb.gyms.model_copy(
                update={
                    "gyms": [gym.model_copy(update={"schedule": []}) for gym in kb.gyms.gyms]
                }
            )
        }
    )

    assert verdict("У нас нет утренних групп.", empty).ok
