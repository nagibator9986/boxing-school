"""Анти-галлюцинационный пост-фильтр: последняя проверка перед отправкой.

Модель работает с реальными деньгами родителей и с репутацией школы, поэтому
между «сгенерировали текст» и «отправили клиенту» стоит проверка фактов.
Работает **после** ``safe_text`` и **до** постановки в outbox.

Что проверяется и чем подтверждается
------------------------------------

============== =============================================================
Класс факта    Чем подтверждается
============== =============================================================
деньги         только ``data`` инструментов ЭТОГО хода (``calculate_price``)
время          только ``data`` этого хода; ``get_schedule`` без слотов ничего
               не подтверждает, поэтому время остаётся выдумкой (правило G-1).
               Ловится и ``18:00``, и словесная запись «с 18 до 20 часов»
день недели    только ``data`` этого хода
номер телефона ``data`` хода, телефон зала из KB **или** номер самого клиента
адрес с домом  ``data`` хода **или** адрес зала из KB
номер медформы только ``data`` хода (075/026 школа не подтверждала)
название зала  ``data`` хода **или** реестр залов KB
утечка промпта пересечение ≥ 8 слов подряд с системной инструкцией
запрещённое    ``forbidden_claims`` из KB и список обещаний результата
============== =============================================================

Телефон и адрес разрешено подтверждать базой знаний, а не только инструментом:
номер зала, взятый из KB, — по определению не выдумка. Номер, который родитель
сам продиктовал в этом диалоге, — тоже не выдумка: подтверждение записи
(«записал ваш номер 8 705 …, верно?») обязано доходить до клиента. Деньги,
время и дни недели — только инструментом: цена зависит от количества детей и
района, и «вспомнить» её из префикса промпта модель не имеет права.

Правило G-1 держится не флагом «расписание не спрашивали», а множеством
подтверждённых значений: ``facts.times`` наполняется ТОЛЬКО из ``data``
инструментов, причём эхо пожеланий клиента (``create_trial_lead``) в него не
попадает. Пока расписание в KB не заполнено, множество пустое — и любое время
блокируется. Как только у одного зала расписание появилось, время этого зала
подтверждается, а «нет данных» по соседнему залу его больше не отменяет.

Как отличается цена от возраста ребёнка
---------------------------------------

Это главный источник **ложных** срабатываний, а ложное срабатывание — тоже
дефект: оно молча съедает правильный ответ и отправляет живого клиента к
администратору. Поэтому число проверяется не «потому что оно число», а только
если оно **выглядит как деньги**. Классификация идёт по контексту вокруг числа
(24 знака слева, 16 справа), правила применяются по порядку, первое сработавшее
побеждает:

1. **время** — ``\\d{1,2}[:.]\\d{2}`` с валидными часами и минутами;
2. **телефон** — число внутри телефонного шаблона;
3. **возраст** — сразу после числа стоит ``лет / год / года / годика / жас /
   жаста / -ти / -летний``: «с 5 лет», «сыну 8 лет», «6 жаста» — это НЕ деньги;
4. **счёт штук** — после числа ``детей / ребёнка / человек / занятий /
   тренировок / раз / месяцев / дней / минут``: «12 занятий», «двое детей» —
   НЕ деньги (частая и дорогая ошибка: абонемент на 12 занятий);
5. **место** — перед числом ``мкр / микрорайон / дом / кв / этаж / подъезд /
   зал / школа / № / автобус``: «5 мкр», «зал №2», «22 школа» — НЕ деньги;
6. **год** — четыре цифры в диапазоне 1900…текущий год: «2018 г.р.» — НЕ деньги;
7. **процент** — сразу после числа ``%`` или ``процент``: это скидка, ПРОВЕРЯЕТСЯ
   как деньги;
8. **деньги** — после числа валюта (``₸ тг тнг тенге теңге``), либо слева в
   пределах 24 знаков денежное слово (``цена, стоит, стоимость, абонемент,
   оплата, скидка, обойдётся, выйдет``) и число ≥ 100, либо «голое» число ≥ 1000.
   Ноль деньгами не считается: «пробное бесплатное, 0 ₸» — это отсутствие цены;
9. всё остальное — **нейтральное**, не проверяется вовсе.

Границы подобраны по предметной области: возраст ребёнка не бывает больше 99,
цена абонемента не бывает меньше 1000 ₸, номер микрорайона и номер зала всегда
идут с ключевым словом. Поэтому «от 5 лет», «двое детей», «12 занятий», «5 мкр»,
«зал №2» и «2018 г.р.» проходят фильтр молча, а «25 000 ₸», «скидка 20 %» и
«разовое 3000» обязаны найтись в результате ``calculate_price``.

Строгий режим (``strict=True``) включается после тревоги охраны: в нём
запрещены **любые** числа, кроме пришедших из ``data``. Он намеренно
параноидальный — на подозрительном ходу лучше промолчать и позвать человека.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final, Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from app.kb.models import KBSnapshot
from app.types import (
    MAX_MESSAGE_CHARS,
    Language,
    PostcheckFailKind,
    Scope,
    ToolInvocation,
    ToolStatus,
)

__all__ = [
    "MAX_REPLY_CHARS",
    "PostcheckVerdict",
    "check",
    "extract_numbers",
    "extract_phones",
    "extract_times",
    "has_prompt_leak",
    "find_prompt_leak",
    "normalize_number",
]

#: Ответ длиннее не влезет даже в два сообщения — это сбой генерации, а не текст.
MAX_REPLY_CHARS: Final[int] = MAX_MESSAGE_CHARS * 2

# --------------------------------------------------------------------------- #
# Регулярки
# --------------------------------------------------------------------------- #
#: Число с бытовыми разделителями разрядов: ``25 000``, ``25.000``, ``25000``.
_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"\d+(?:[ \u00a0\u202f.,]\d{3})*")
_TIME_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)")
#: Телефон в любом виде, в каком его пишут в Костанае. Три формы, и все три
#: обязаны ловиться: телефона в базе сегодня нет вовсе (пробел G-2), поэтому
#: ЛЮБОЙ номер в ответе — выдумка модели.
_PHONE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\d\-])(?:"
    # мобильный / полный: +7 705 123 45 67, 8-705-123-45-67, 87051234567
    r"(?:\+?7|8)[\s\-()._]*\d{3}[\s\-()._]*\d{3}[\s\-()._]*\d{2}[\s\-()._]*\d{2}"
    # городской с четырёхзначным кодом города: 8 (7142) 54-32-10
    r"|(?:\+?7|8)[\s\-()._]*\d{4}[\s\-()._]*\d{2}[\s\-()._]*\d{2}[\s\-()._]*\d{2}"
    # местный городской без кода: 54-32-10, 543-21-05, 54 32 10
    r"|\d{2,3}[\s\-]\d{2}[\s\-]\d{2}"
    r")(?!\d)"
)
_MEDICAL_FORM_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)(075|086|026)\s*[-/]?\s*[уy]?(?!\d)", re.IGNORECASE
)
_ADDRESS_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:ул\.|улиц\w*|проспект\w*|пр-т|бульвар\w*|көше|даңғыл\w*)"
    r"[^;.\n]{0,30}?\d{1,4}(?!\s*[а-яёәғқңөұүһі])",
    re.IGNORECASE,
)
_ADDRESS_INTRO_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:по адресу|адрес[:\s]|находимся|находится|расположен\w*)\s*[:\-]?\s*"
    r"([^\n.;]{4,60}?\d{1,4})(?!\s*[а-яёәғқңөұүһі])",
    re.IGNORECASE,
)
#: Заглавное название + номер дома БЕЗ слова «улица» — ровно так адреса записаны
#: в ``kb/gyms.yaml`` («Каирбекова, 334»), и ровно так их повторяет модель.
#: Двух признаков достаточно, чтобы отличить адрес от «Стоимость 3000»:
#: запятая между названием и числом либо предлог места перед названием.
_CAPITALIZED: Final[str] = r"[А-ЯЁӘҒҚҢӨҰҮҺІ][\w\-]{2,}"
_ADDRESS_COMMA_RE: Final[re.Pattern[str]] = re.compile(
    rf"({_CAPITALIZED}(?:\s+{_CAPITALIZED})?\s*,\s*(?<!\d)\d{{1,4}})(?!\d)"
    r"(?!\s*[а-яёәғқңөұүһі])"
)
#: Слово, превращающее «Заглавное слово, число» в заявку об адресе. Шаблон с
#: запятой — единственный без собственного контекста, и без этого условия он
#: ловит «Записал: Айдана, 8.» (имя ребёнка и возраст) как выдуманный адрес,
#: то есть съедает подтверждение записи. Ищется в том же предложении.
_ADDRESS_CUE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:зал\w*|филиал\w*|секци\w*|клуб\w*|адрес\w*|мекенжай\w*|көше\w*|даңғыл\w*|"
    r"ждём|ждем|күтеміз|приход\w*|приезжай\w*|келіңіз|заезжай\w*|"
    r"находим\w*|находит\w*|располож\w*|орналас\w*)",
    re.IGNORECASE,
)
_ADDRESS_PREP_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:[Нн]а|[Вв]|[Пп]о|около|возле|рядом с)\s+"
    rf"({_CAPITALIZED}(?:\s+{_CAPITALIZED})?\s*,?\s*(?<!\d)\d{{1,4}})(?!\d)"
    r"(?!\s*[а-яёәғқңөұүһі])"
)
_GYM_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:зал\w*|филиал\w*|клуб\w*|секци\w*)\s+(?:на|в|по|около|возле|рядом с)\s+"
    r"([А-ЯЁӘҒҚҢӨҰҮҺІ][\w\-]{2,})"
)
_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[^\W_]+", re.UNICODE)

#: Валюта сразу после числа.
_CURRENCY_AFTER_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:₸|тг\b|тнг\b|тенге|теңге|kzt\b|тыс\w*\s*(?:₸|тг|тенге)?)", re.IGNORECASE
)
#: Множитель «тысяч»: «25 тысяч» — это 25000.
_THOUSANDS_AFTER_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(?:тыс\w*|мың)", re.IGNORECASE)
#: Возрастная единица сразу после числа.
_AGE_AFTER_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:-?\s*(?:ти|летн\w*|годовал\w*)|лет\b|год\w*\b|л\.\s|жаст\w*|жас\b)", re.IGNORECASE
)
#: Счётные существительные сразу после числа.
_COUNT_AFTER_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:детей|ребен\w*|ребён\w*|человек\w*|бала\w*|занят\w*|тренировк\w*|сабақ\w*|"
    r"раз\b|раза\b|разов\w*|месяц\w*|мес\.|недел\w*|дней|дня|день|күн|минут\w*|мин\.|"
    r"групп\w*|мест\b|места\b|тренер\w*|филиал\w*|зал\w*|балл\w*|штук)",
    re.IGNORECASE,
)
#: Слова-места перед числом.
_PLACE_BEFORE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:мкр\.?|микрорайон\w*|дом\b|д\.|кв\.|квартир\w*|этаж\w*|подъезд\w*|кабинет\w*|"
    r"зал\w*|школ\w*|№|#|автобус\w*|маршрут\w*|остановк\w*|корпус\w*)\s*[-№#]?\s*$",
    re.IGNORECASE,
)
#: Денежные слова перед числом.
_MONEY_BEFORE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:цен\w*|стоит|стоим\w*|абонемент\w*|оплат\w*|плат\w*|скидк\w*|составля\w*|"
    r"обойдётся|обойдется|выйдет|разов\w*|бағас\w*|төлем\w*)[^\d]{0,24}$",
    re.IGNORECASE,
)
_PERCENT_AFTER_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(?:%|процент\w*|пайыз)", re.IGNORECASE)

# --------------------------------------------------------------------------- #
# Время словами
# --------------------------------------------------------------------------- #
# Правило G-1 писалось ради ``18:00``, но модель обходит его одной сменой формата:
# «с 18 до 20 часов», «в 10 утра», «сағат 18-ден». Для родителя это то же самое
# время, и точно так же ведёт ребёнка к закрытой двери. Поэтому час в словесной
# форме — тоже время, но только там, где он однозначно час СУТОК, а не
# длительность: перед числом обязан стоять предлог времени, а рядом — «час»,
# «сағат» или часть суток. «Занятие длится 2 часа» под правило не попадает.
_HOUR_RANGE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:с|со|от)\s+(\d{1,2})(?!\d)\s*(?:-[а-яёәғқңөұүһі]{1,4})?\s*(?:час\w*|сағат\w*)?\s*"
    r"(?:до|–|—|-|дейін)\s*(\d{1,2})(?!\d)\s*(?:-[а-яёәғқңөұүһі]{1,4})?\s*"
    r"(?:час\w*|сағат\w*|утра|вечера|ночи)",
    re.IGNORECASE,
)
_HOUR_WORD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:в|во|с|со|до|к|от|после)\s+(\d{1,2})(?!\d)\s*(?:-[а-яёәғқңөұүһі]{1,4})?\s*"
    r"(?:час\w*|сағат\w*)",
    re.IGNORECASE,
)
_DAYPART_AFTER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)(\d{1,2})(?!\d)\s*(?:-[а-яёәғқңөұүһі]{1,4})?\s*(?:утра|вечера|ночи)\b",
    re.IGNORECASE,
)
_DAYPART_BEFORE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:сағат|таңғы|кешкі|түнгі)\s*(\d{1,2})(?!\d)",
    re.IGNORECASE,
)
#: Слева стоит длительность, а не час суток: «занятие длится около 2 часов».
_DURATION_BEFORE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:длит\w*|продолжительн\w*|продолжа\w*|в течение|ұзақтығ\w*|созыл\w*)[^\d]{0,24}$",
    re.IGNORECASE,
)
#: (шаблон, номера групп с часами). Порядок важен: диапазон разбирается первым,
#: иначе «с 18 до 20 часов» даст только 20.
_WORD_TIME_PATTERNS: Final[tuple[tuple[re.Pattern[str], tuple[int, ...]], ...]] = (
    (_HOUR_RANGE_RE, (1, 2)),
    (_HOUR_WORD_RE, (1,)),
    (_DAYPART_AFTER_RE, (1,)),
    (_DAYPART_BEFORE_RE, (1,)),
)

#: Инструменты, чьи ``data`` НЕ подтверждают время: они возвращают эхо пожеланий
#: клиента (``preferred_time_text``), а не расписание школы.
_TIME_UNTRUSTED_TOOLS: Final[frozenset[str]] = frozenset({"create_trial_lead"})

#: Дни недели: русский, казахский и коды расписания из KB.
_WEEKDAYS: Final[dict[str, tuple[str, ...]]] = {
    "mon": ("понедельник", "дүйсенбі", "дуйсенби"),
    "tue": ("вторник", "сейсенбі", "сейсенби"),
    "wed": ("среда", "среду", "сәрсенбі", "сарсенби"),
    "thu": ("четверг", "бейсенбі", "бейсенби"),
    "fri": ("пятница", "пятницу", "жұма", "жума"),
    "sat": ("суббота", "субботу", "сенбі", "сенби"),
    "sun": ("воскресенье", "жексенбі", "жексенби"),
}

#: Поиск дня недели ТОЛЬКО с левой границей слова.
#:
#: «сенбі» (суббота) — подстрока пяти из семи казахских дней: дүй-сенбі,
#: сей-сенбі, сәр-сенбі, бей-сенбі, жек-сенбі. Без границы слова любой ответ на
#: казахском с днём недели обвинялся в выдуманной субботе, а выдуманная «сенбі»
#: наоборот подтверждалась корпусом, где лежит «дүйсенбі». Правая граница
#: намеренно свободна: «сенбіде», «понедельникам» — те же дни в падеже.
_WEEKDAY_RES: Final[dict[str, tuple[tuple[str, re.Pattern[str]], ...]]] = {
    code: tuple(
        (name, re.compile(r"(?<![^\W\d_])" + re.escape(name), re.IGNORECASE))
        for name in names
    )
    for code, names in _WEEKDAYS.items()
}

#: Оговорки, снимающие претензию к дню недели: бот не утверждает, а обещает уточнить.
_HEDGES: Final[dict[Language, tuple[str, ...]]] = {
    Language.RU: (
        "уточн", "администратор", "подскаж", "напиш", "перезвон", "свяж", "не знаю",
        "к сожалению", "пока нет", "нет данных", "передам",
    ),
    Language.KK: ("нақтыла", "әкімші", "хабарлас", "білмеймін", "өкініш", "жоқ", "жеткіз"),
}

#: Обещания, запрещённые политикой бренда независимо от базы знаний.
_FORBIDDEN_CLAIMS: Final[tuple[str, ...]] = (
    "100% безопас", "полностью безопас", "абсолютно безопас", "без травм",
    "гарантирую результат", "гарантируем результат", "станет чемпионом",
    "обязательно выиграет", "точно выиграет", "получит разряд",
    "последнее место", "только сегодня", "цена завтра", "успейте сегодня",
    # У бота нет связи с тренерами: он пишет в чат и зовёт администратора.
    # 03.09.2026 родителю, предупредившему о пропуске, он ответил «спасибо, что
    # предупредили — я передам тренеру». Никто ничего не передал.
    "передам тренеру", "передам вашему тренеру", "сообщу тренеру",
    "передам это тренеру", "предупрежу тренера", "скажу тренеру",
)

#: Следы служебного слоя, которых в сообщении клиенту быть не может.
_LEAK_MARKERS: Final[tuple[str, ...]] = (
    "<user_message>", "</user_message>", "system prompt", "системный промпт",
    "системная инструкция", "function_call", "functioncall", "function_response",
    "tool_call", "kb_hash", "render_hint", "say_if_no_data", "```json", "```system",
    "role: system", "мои инструкции",
)


class PostcheckVerdict(BaseModel):
    """Итог проверки одного готового ответа."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    kind: PostcheckFailKind | None = None
    #: Значения, из-за которых ответ отклонён (для лога и метрики).
    offending: tuple[str, ...] = ()
    #: Очищенный текст, если ``ok=True``; при отказе — пустая строка,
    #: чтобы неподтверждённый текст физически нельзя было отправить.
    text: str = ""


@dataclass(slots=True)
class _Facts:
    """Всё, чем можно подтвердить факт в ответе, собранное за один проход."""

    numbers: set[str] = field(default_factory=set)
    times: set[str] = field(default_factory=set)
    weekdays: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    #: Нормализованный текстовый корпус (значения ``data``, caveats, KB-адреса).
    corpus: str = ""
    places: set[str] = field(default_factory=set)


# --------------------------------------------------------------------------- #
# Публичное
# --------------------------------------------------------------------------- #
def check(
    text: str,
    *,
    invocations: Sequence[ToolInvocation],
    lang: Language,
    kb: KBSnapshot,
    prompt_ngrams: frozenset[str],
    strict: bool = False,
    known_phones: Sequence[str] = (),
    known_names: Sequence[str] = (),
) -> PostcheckVerdict:
    """Анти-галлюцинационный фильтр. Работает ПОСЛЕ ``safe_text`` и ДО постановки в outbox.

    Извлекает и сверяет с ``data`` вызовов ТЕКУЩЕГО хода: деньги, время
    ``HH:MM``, дни недели, телефоны, адреса с номером дома, номера медицинских
    форм (075, 026), названия залов. Любое неподтверждённое значение →
    ``ok=False``: ответ НЕ отправляется, пишется ``postcheck_fail_total{kind}``,
    клиенту уходит нейтральный текст из ``kb/i18n.yaml``, диалог эскалируется.

    Отличие цены от возраста ребёнка, количества детей, числа занятий, номера
    микрорайона и года рождения описано в docstring модуля: проверяется только
    то, что выглядит как деньги, всё остальное фильтр не трогает.

    ``strict=True`` (после guard-тревоги) дополнительно запрещает любые числа,
    кроме пришедших из ``data``.

    ``known_phones`` — номера, которые в этом диалоге назвал сам клиент (или из
    которых он пишет). Повторить их в подтверждении записи бот обязан, выдумать
    чужой номер по-прежнему не может.
    """
    if not text or not text.strip():
        return PostcheckVerdict(ok=False, kind=None, offending=("empty",), text="")

    cleaned = text.strip()
    facts = _collect_facts(invocations, kb, known_phones=known_phones)

    matched_ngram = find_prompt_leak(cleaned, prompt_ngrams)
    if matched_ngram is not None:
        # В offending кладём саму совпавшую фразу, а не слово «prompt_ngram»:
        # по метке без текста невозможно отличить настоящую утечку от ложного
        # срабатывания, а цена ошибки здесь — замолчавший бот.
        return _fail(PostcheckFailKind.PROMPT_LEAK, (matched_ngram,))

    leaked = [marker for marker in _LEAK_MARKERS if marker in cleaned.lower()]
    if leaked:
        return _fail(PostcheckFailKind.PROMPT_LEAK, tuple(leaked))

    forbidden = _forbidden_claims(cleaned, kb)
    if forbidden:
        return _fail(PostcheckFailKind.FORBIDDEN_CLAIM, forbidden)

    denied = _false_denials(cleaned, kb)
    if denied:
        return _fail(PostcheckFailKind.FALSE_DENIAL, denied)

    wrong_age = _wrong_age_limit(cleaned, kb)
    if wrong_age:
        return _fail(PostcheckFailKind.FALSE_DENIAL, wrong_age)

    invented = _invented_name(cleaned, known_names, invocations)
    if invented:
        return _fail(PostcheckFailKind.FALSE_DENIAL, invented)

    if len(cleaned) > MAX_REPLY_CHARS:
        return _fail(PostcheckFailKind.TOO_LONG, (str(len(cleaned)),))

    # Правило G-1: время подтверждается ТОЛЬКО данными инструментов этого хода.
    # Расписания в KB нет — ``facts.times`` пусто, и любое время блокируется;
    # расписание появилось — блокируется всё, кроме пришедшего из него.
    times = extract_times(cleaned)
    bad_times = tuple(value for value in times if value not in facts.times)
    if bad_times:
        return _fail(PostcheckFailKind.TIME, bad_times)

    money, unknown = _classify_numbers(cleaned, facts, strict=strict)
    if money:
        return _fail(PostcheckFailKind.MONEY, money)

    bad_days = _unconfirmed_weekdays(cleaned, facts, lang=lang)
    if bad_days:
        return _fail(PostcheckFailKind.WEEKDAY, bad_days)

    bad_phones = _unconfirmed_phones(cleaned, facts)
    if bad_phones:
        return _fail(PostcheckFailKind.PHONE, bad_phones)

    bad_forms = _unconfirmed_forms(cleaned, facts)
    if bad_forms:
        return _fail(PostcheckFailKind.MEDICAL_FORM, bad_forms)

    bad_address = _unconfirmed_addresses(cleaned, facts)
    if bad_address:
        return _fail(PostcheckFailKind.ADDRESS, bad_address)

    bad_gyms = _unconfirmed_gyms(cleaned, facts)
    if bad_gyms:
        return _fail(PostcheckFailKind.GYM_NAME, bad_gyms)

    if strict and unknown:
        # Строгий режим: на подозрительном ходу цифра без подтверждения запрещена.
        return _fail(PostcheckFailKind.MONEY, unknown)

    return PostcheckVerdict(ok=True, kind=None, offending=(), text=cleaned)


def extract_numbers(text: str) -> tuple[str, ...]:
    """Все числовые токены текста как они написаны: ``25 000``, ``2018``, ``12``."""
    if not text:
        return ()
    return tuple(match.group(0) for match in _NUMBER_RE.finditer(text))


def extract_times(text: str) -> tuple[str, ...]:
    """Время в каноническом написании ``HH:MM`` с двоеточием и ведущим нулём.

    ``18.30`` и ``18:30`` дают одно и то же значение ``18:30``; ``25:70`` временем
    не считается, а ``25.000`` не считается временем тем более — иначе цена
    блокировала бы сама себя.

    Словесная запись часа — это тоже время: «с 18 до 20 часов» → ``18:00`` и
    ``20:00``, «в 10 утра» → ``10:00``, «сағат 18» → ``18:00``. Без этого правило
    G-1 обходилось одной сменой формата. Длительность («занятие длится 2 часа»)
    временем не считается: перед часом суток обязан стоять предлог времени.
    """
    _, values = _time_hits(text)
    return values


def _time_hits(text: str) -> tuple[tuple[tuple[int, int], ...], tuple[str, ...]]:
    """``(участки текста с временем, канонические значения)``.

    Участки нужны разбору чисел: цифры внутри оборота времени деньгами не
    являются и второй раз не проверяются.
    """
    if not text:
        return (), ()
    spans: list[tuple[int, int]] = []
    values: list[str] = []

    def remember(value: str) -> None:
        if value not in values:
            values.append(value)

    for match in _TIME_RE.finditer(text):
        hours, minutes = int(match.group(1)), int(match.group(2))
        if hours > 23 or minutes > 59:
            continue
        spans.append(match.span())
        remember(f"{hours:02d}:{minutes:02d}")

    canonical = list(spans)
    for pattern, groups in _WORD_TIME_PATTERNS:
        for match in pattern.finditer(text):
            before = text[max(0, match.start() - 24) : match.start()]
            if _DURATION_BEFORE_RE.search(before):
                continue
            hours_found: list[int] = []
            for group in groups:
                raw = match.group(group)
                if raw is None or int(raw) > 23:
                    continue
                if _inside(match.start(group), match.end(group), canonical):
                    continue  # это часть уже разобранного ``HH:MM``
                hours_found.append(int(raw))
            if not hours_found:
                continue
            spans.append(match.span())
            for hours in hours_found:
                remember(f"{hours:02d}:00")

    return tuple(spans), tuple(values)


def extract_phones(text: str) -> tuple[str, ...]:
    """Телефоны в тексте как они написаны. Нужен пайплайну: номер, который
    клиент продиктовал сам, подтверждением записи выдумкой не является."""
    if not text:
        return ()
    return tuple(match.group(0) for match in _PHONE_RE.finditer(text))


def has_prompt_leak(text: str, ngrams: frozenset[str], *, n: int = 8) -> bool:
    """Пересечение ≥ ``n`` слов подряд с системным промптом → блок ответа.

    N-граммы строятся так же, как в ``app.llm.prompt.prompt_ngrams``: слова в
    нижнем регистре, склеенные одним пробелом. Если множество пустое (промпт не
    передан), проверка молча пропускается — блокировать по отсутствию данных
    нельзя.
    """
    return find_prompt_leak(text, ngrams, n=n) is not None


def find_prompt_leak(text: str, ngrams: frozenset[str], *, n: int = 8) -> str | None:
    """Первая совпавшая n-грамма или ``None``.

    Отдельная функция нужна, чтобы блокировка была диагностируемой: по одной лишь
    метке «утечка» нельзя понять, выдал ли бот устройство системы или его сняли за
    фразу, которую промпт сам велел произнести. Совпавший текст едет в лог и в
    метрику.
    """
    if not text or not ngrams or n <= 0:
        return None
    words = [word.lower() for word in _WORD_RE.findall(text)]
    if len(words) < n:
        return None
    for index in range(len(words) - n + 1):
        candidate = " ".join(words[index : index + n])
        if candidate in ngrams:
            return candidate
    return None


def normalize_number(value: str) -> str:
    """``25 000 ₸``, ``25000``, ``25.000`` → ``25000`` для сравнения с ``data``.

    Остаются только цифры: дробных цен в тенге не бывает, а разделители разрядов
    в живом тексте пишут как угодно.
    """
    if not value:
        return ""
    return re.sub(r"\D", "", str(value))


# --------------------------------------------------------------------------- #
# Сбор подтверждающих фактов
# --------------------------------------------------------------------------- #
def _collect_facts(
    invocations: Sequence[ToolInvocation],
    kb: KBSnapshot,
    *,
    known_phones: Sequence[str] = (),
) -> _Facts:
    """Собирает всё, чем можно подтвердить факт: ``data`` хода плюс реестр KB."""
    facts = _Facts()
    chunks: list[str] = []

    for invocation in invocations:
        result = invocation.result
        # Неуспешный вызов ничего не подтверждает: его ``data`` — описание отказа.
        trust_times = (
            result.status is ToolStatus.OK and invocation.name not in _TIME_UNTRUSTED_TOOLS
        )
        _harvest(result.data, facts, chunks, trust_times=trust_times)
        for caveat in result.caveats:
            _harvest(caveat, facts, chunks, trust_times=False)
        if result.say_if_no_data:
            _harvest(result.say_if_no_data, facts, chunks, trust_times=False)

    for phone in known_phones:
        normalized = _normalize_phone(phone)
        if normalized:
            facts.phones.add(normalized)

    # Реестр залов: телефоны, адреса и названия — не выдумка по определению.
    for gym in kb.active_gyms():
        for value in (
            gym.title.ru, gym.title.kk, gym.address.ru, gym.address.kk,
            gym.landmark.ru, gym.landmark.kk, gym.district.ru, gym.district.kk,
            gym.settlement,
        ):
            if value:
                chunks.append(value.lower())
                facts.places.add(_place_key(value))
        for alias in gym.district_aliases or ():
            facts.places.add(_place_key(alias))
        if gym.phone:
            facts.phones.add(_normalize_phone(gym.phone))

    facts.corpus = " ".join(chunks)
    return facts


def _harvest(node: Any, facts: _Facts, chunks: list[str], *, trust_times: bool) -> None:
    """Рекурсивно вытаскивает из результата инструмента числа, время и тексты.

    ``trust_times=False`` — значение попадает в корпус и в числа, но временем
    ничего не подтверждает: так эхо пожеланий клиента и оговорки инструмента не
    превращаются в «подтверждённое школой» расписание.
    """
    if node is None or isinstance(node, bool):
        return
    if isinstance(node, (int, float)):
        facts.numbers.add(normalize_number(str(int(node))))
        return
    if isinstance(node, str):
        lowered = node.lower()
        chunks.append(lowered)
        for token in extract_numbers(node):
            facts.numbers.add(normalize_number(token))
        if trust_times:
            for value in extract_times(node):
                facts.times.add(value)
        for code, patterns in _WEEKDAY_RES.items():
            if lowered == code or any(pattern.search(lowered) for _, pattern in patterns):
                facts.weekdays.add(code)
        for phone in _PHONE_RE.finditer(node):
            facts.phones.add(_normalize_phone(phone.group(0)))
        facts.places.add(_place_key(node))
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            if isinstance(key, str):
                chunks.append(key.lower())
            _harvest(value, facts, chunks, trust_times=trust_times)
        return
    if isinstance(node, Iterable):
        for item in node:
            _harvest(item, facts, chunks, trust_times=trust_times)


def _place_key(value: str) -> str:
    """Ключ места: только буквы и цифры в нижнем регистре, без пробелов."""
    return re.sub(r"[^\w]", "", str(value).lower())


def _normalize_phone(value: str) -> str:
    """Телефон к сравнимому виду: последние 10 цифр."""
    digits = re.sub(r"\D", "", str(value))
    return digits[-10:] if len(digits) >= 10 else digits


# --------------------------------------------------------------------------- #
# Числа
# --------------------------------------------------------------------------- #
def _classify_numbers(
    text: str, facts: _Facts, *, strict: bool
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Возвращает (неподтверждённые деньги, неподтверждённые прочие числа).

    Второй кортеж нужен только строгому режиму: в обычном режиме нейтральные
    числа (возраст, количество, номер зала) не проверяются вовсе.
    """
    money_bad: list[str] = []
    other_bad: list[str] = []
    time_spans = list(_time_hits(text)[0])
    phone_spans = [match.span() for match in _PHONE_RE.finditer(text)]

    for match in _NUMBER_RE.finditer(text):
        start, end = match.span()
        if _inside(start, end, time_spans) or _inside(start, end, phone_spans):
            continue

        raw = match.group(0)
        before = text[max(0, start - 24) : start].lower()
        after = text[end : end + 20]
        digits = normalize_number(raw)
        if not digits:
            continue

        if (
            _AGE_AFTER_RE.match(after)          # возраст ребёнка
            or _COUNT_AFTER_RE.match(after)     # «12 занятий», «двое детей»
            or _PLACE_BEFORE_RE.search(before)  # «5 мкр», «зал №2», «22 школа»
            or _is_year(digits, after)          # «2018 г.р.»
        ):
            # В обычном режиме такие числа не проверяются вовсе. В строгом —
            # проверяются: после тревоги охраны контекст вокруг числа пишет тот,
            # кто эту тревогу и вызвал, и доверять ему как классификатору нельзя.
            if strict and digits not in facts.numbers:
                other_bad.append(raw.strip())
            continue

        value = int(digits)
        if _THOUSANDS_AFTER_RE.match(after):
            value *= 1000
            digits = str(value)

        if value == 0:
            # Ноль — не цена, а отсутствие цены: «пробное бесплатное, 0 ₸».
            # Подтвердить его нечем (калькулятор нулевых сумм не считает), а
            # запрет ничего не защищает: словесное «бесплатно» фильтр всё равно
            # не проверяет, так что блокировался только цифровой синоним — прямо
            # на главной точке конверсии. В строгом режиме число без
            # подтверждения остаётся подозрительным наравне с остальными.
            if strict and digits not in facts.numbers:
                other_bad.append(raw.strip())
            continue

        looks_like_money = bool(
            _CURRENCY_AFTER_RE.match(after)
            or _PERCENT_AFTER_RE.match(after)
            or (_MONEY_BEFORE_RE.search(before) and value >= 100)
            or value >= 1000
        )
        confirmed = digits in facts.numbers or str(value) in facts.numbers
        if looks_like_money:
            if not confirmed:
                money_bad.append(raw.strip())
        elif strict and not confirmed:
            other_bad.append(raw.strip())

    return tuple(dict.fromkeys(money_bad)), tuple(dict.fromkeys(other_bad))


def _inside(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    """Попадает ли число внутрь уже распознанного шаблона (время, телефон)."""
    return any(span_start <= start and end <= span_end for span_start, span_end in spans)


def _is_year(digits: str, after: str) -> bool:
    """Четыре цифры в диапазоне 1900…2100 — это год, а не сумма."""
    if len(digits) != 4:
        return False
    value = int(digits)
    if not 1900 <= value <= 2100:
        return False
    return not _CURRENCY_AFTER_RE.match(after)


# --------------------------------------------------------------------------- #
# Дни недели, телефоны, адреса, залы
# --------------------------------------------------------------------------- #
def _unconfirmed_weekdays(text: str, facts: _Facts, *, lang: Language) -> tuple[str, ...]:
    """Дни недели, которых нет в ``data``.

    Оговорка в том же предложении («уточню у администратора») снимает претензию:
    бот не утверждает факт, а обещает его выяснить.
    """
    lowered = text.lower()
    hedges = _HEDGES.get(lang, ()) + _HEDGES[Language.RU]
    bad: list[str] = []
    for code, patterns in _WEEKDAY_RES.items():
        for name, pattern in patterns:
            match = pattern.search(lowered)
            if match is None:
                continue
            if code in facts.weekdays or pattern.search(facts.corpus) is not None:
                break
            if _hedged(lowered, match.start(), hedges):
                break
            bad.append(name)
            break
    return tuple(bad)


def _hedged(lowered: str, position: int, hedges: Sequence[str]) -> bool:
    """Есть ли рядом со спорным местом оговорка «уточню / администратор»."""
    start = lowered.rfind(".", 0, position) + 1
    end = lowered.find(".", position)
    sentence = lowered[start : end if end > 0 else len(lowered)]
    return any(hedge in sentence for hedge in hedges)


def _unconfirmed_phones(text: str, facts: _Facts) -> tuple[str, ...]:
    """Телефоны, которых нет ни в ``data``, ни в реестре залов KB.

    Счётная единица сразу после числа снимает подозрение: «10 20 30 лет» —
    это не номер, а перечисление. За настоящим номером «лет» не стоит никогда.
    """
    bad: list[str] = []
    for match in _PHONE_RE.finditer(text):
        after = text[match.end() : match.end() + 20]
        if _AGE_AFTER_RE.match(after) or _COUNT_AFTER_RE.match(after):
            continue
        normalized = _normalize_phone(match.group(0))
        if normalized and normalized not in facts.phones:
            bad.append(match.group(0).strip())
    return tuple(dict.fromkeys(bad))


def _unconfirmed_forms(text: str, facts: _Facts) -> tuple[str, ...]:
    """Номера медицинских форм: школа их не подтверждала, придумывать нельзя."""
    bad: list[str] = []
    for match in _MEDICAL_FORM_RE.finditer(text):
        number = match.group(1)
        if number not in facts.numbers and number not in facts.corpus:
            bad.append(match.group(0).strip())
    return tuple(dict.fromkeys(bad))


def _unconfirmed_addresses(text: str, facts: _Facts) -> tuple[str, ...]:
    """Адреса с номером дома, которых нет ни в ``data``, ни в KB.

    Проверяются только конкретные адреса («ул. Кайырбекова, 334»): упоминание
    района без номера дома фактом не считается и фильтр не трогает.

    Слово «улица» и вводный оборот не обязательны: в ``kb/gyms.yaml`` адреса
    записаны как «Каирбекова, 334», и модель повторяет именно этот стиль, так
    что «Ждём вас на Гоголя, 15» обязано проверяться наравне с «ул. Гоголя, 15».
    Голое «Слово, число» без предлога места («Наш зал: Тәуелсіздік, 88»)
    считается адресом только рядом со словом-указателем места — иначе под
    проверку попадало бы «Записал: Айдана, 8.», то есть имя ребёнка с возрастом.
    """
    candidates: list[str] = [match.group(0) for match in _ADDRESS_RE.finditer(text)]
    candidates += [match.group(1) for match in _ADDRESS_INTRO_RE.finditer(text)]
    candidates += [
        match.group(1)
        for match in _ADDRESS_COMMA_RE.finditer(text)
        if _has_address_cue(text, match.start(1))
    ]
    candidates += [match.group(1) for match in _ADDRESS_PREP_RE.finditer(text)]

    bad: list[str] = []
    for candidate in candidates:
        digits = re.findall(r"\d{1,4}", candidate)
        words = [word for word in _WORD_RE.findall(candidate.lower()) if len(word) >= 4]
        if not digits:
            continue
        house = digits[-1]
        if _is_year(house, ""):
            # «Костанай, 2018» — это год, а не номер дома.
            continue
        street_ok = any(word[:5] in facts.corpus for word in words) if words else False
        house_ok = house in facts.numbers or house in facts.corpus
        if not (street_ok and house_ok):
            bad.append(" ".join(candidate.split()))
    return tuple(dict.fromkeys(bad))


def _has_address_cue(text: str, position: int) -> bool:
    """Есть ли в этом же предложении слово, делающее «Слово, число» адресом."""
    boundary = max(text.rfind(char, 0, position) for char in ".!?\n") + 1
    window = text[max(boundary, position - 60) : position]
    return _ADDRESS_CUE_RE.search(window) is not None


def _unconfirmed_gyms(text: str, facts: _Facts) -> tuple[str, ...]:
    """Названия залов и районов, которых нет в реестре KB.

    Ловится узкий шаблон «зал/филиал/секция + предлог + Слово с большой буквы»:
    именно так модель придумывает несуществующий филиал. Строчные обороты
    («зал в центре») не трогаются — это не название.
    """
    bad: list[str] = []
    for match in _GYM_NAME_RE.finditer(text):
        name = match.group(1)
        key = _place_key(name)
        if not key:
            continue
        if key in facts.places or key[:6] in facts.corpus.replace(" ", ""):
            continue
        bad.append(name)
    return tuple(dict.fromkeys(bad))


#: Отрицания утренних занятий. Живой ответ 02.09.2026: «Утренних групп у нас
#: нет, все занятия проходят во второй половине дня, поэтому прийти утром не
#: получится». Владелец: «не верная информация» — утренние группы есть.
_MORNING_DENIAL_RE: Final[re.Pattern[str]] = re.compile(
    # Прямое отрицание утренних групп.
    r"утренн\w*\s+(?:групп\w*|занят\w*|тренировк\w*)\s+(?:у\s+нас\s+)?нет"
    r"|нет\s+утренн\w+"
    r"|не\s+бывает\s+утренн\w+"
    # «прийти утром не получится» и «не получится прийти утром» — порядок слов
    # в русском свободный, а живой ответ был именно в обратном.
    r"|утром\s+(?:\w+\s+){0,2}?не\s+(?:получится|выйдет|сможете|занимаемся|тренируемся|проводим)"
    r"|не\s+(?:получится|выйдет)\s+(?:\w+\s+){0,2}?утром"
    r"|утром\s+(?:нельзя|не\s+работаем)"
    # Общие утверждения «всё только вечером». Оговорка про конкретный зал
    # («в этом зале занятия во второй половине дня») сюда не попадает: она
    # верна для большинства залов, и снимать её было бы вредительством.
    r"|(?:все|всё)\s+(?:занятия|тренировки|группы)\s+(?:\w+\s+){0,2}?"
    r"(?:во\s+второй\s+половине\s+дня|вечером|после\s+обеда)"
    r"|(?:занятия|тренировки|группы)\s+(?:идут\s+|проход\w+\s+)?только\s+"
    r"(?:вечером|по\s+вечерам|во\s+второй\s+половине\s+дня)",
    re.IGNORECASE,
)


#: Как бот называет возрастную границу приёма: «берём детей с 5 лет»,
#: «принимаем с 7», «группы с 7 лет». Число — то, что проверяется.
_AGE_LIMIT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:бер[её]м|принима\w+|записыва\w+|занима\w+|групп\w+|набира\w+|мектеб\w*|қабылда\w+)"
    r"[^.!?\n]{0,40}?\bс\s+(\d{1,2})\s*(?:лет|года|годов|жас\w*)",
    re.IGNORECASE,
)


#: Имя ребёнка или родителя в ответе бота: «рады записать Айсулу», «ждём Бекзата».
_NAME_IN_REPLY_RE: Final = re.compile(
    r"(?:[Зз]апис\w+|[Жж]д[её]м|[Пп]ривед\w+|[Вв]стрет\w+)\s+"
    r"(?!вас\b|ваш|его\b|её\b|ее\b|их\b|реб[её]нка|дочь|сына|мальчика|девочку)"
    r"([А-ЯЁ][а-яё]{2,14})\b",
    re.UNICODE,
)

#: Обращение сразу после приветствия: «Здравствуйте, Айгуль!». Запятая и слово
#: с заглавной буквы вплотную к приветствию — это имя, других вариантов нет.
#: С восклицательным знаком («Здравствуйте! Это школа…») шаблон не совпадает.
_NAME_AFTER_HELLO_RE: Final = re.compile(
    r"(?:[Зз]дравствуйте|[Дд]обрый день|[Дд]обрый вечер|[Дд]оброе утро|[Пп]ривет|"
    r"[Сс]әлеметсіз бе|[Ққ]айырлы күн|[Сс]әлем),\s+([А-ЯЁ][а-яё]{2,14})\b",
    re.UNICODE,
)


def _name_stem(word: str) -> str:
    """Основа имени без падежного окончания: «Бекзата» и «Бекзат» — одно и то же."""
    return word.lower()[:4]


def _invented_name(text: str, known_names: Sequence[str], invocations) -> tuple[str, ...]:
    """Имена, которых клиент не называл и которых нет в данных инструментов.

    03.09.2026 на «хочу записать дочь на пробное» бот ответил «рады записать
    Айсулу». Имени не было ни в переписке, ни в базе знаний — модель придумала
    его целиком. Для родителя это выглядит как переписка с чужим ребёнком.

    Проверяются два контекста: имя рядом с глаголом записи или ожидания и
    обращение сразу после приветствия («Здравствуйте, Айгуль!»). Имя в начале
    фразы («Айгерим, здравствуйте») правило НЕ ловит: там оно неотличимо от
    любого другого слова с запятой («Ответ, который…», «Скажите, …»), а снятый
    верный ответ дороже пропущенной выдумки. От остального защищает запрет в
    ``kb/policies.yaml``.
    """
    allowed = {_name_stem(name) for name in known_names if name}
    for inv in invocations:
        payload = f"{inv.args} {inv.result.data if inv.result else ''}"
        allowed.update(_name_stem(word) for word in re.findall(r"[А-ЯЁ][а-яё]{2,14}", payload))

    found: list[str] = []
    for match in (*_NAME_IN_REPLY_RE.finditer(text), *_NAME_AFTER_HELLO_RE.finditer(text)):
        word = match.group(1)
        if _name_stem(word) not in allowed:
            found.append(word)
    return tuple(dict.fromkeys(found))


def _confirmed_min_age(kb: KBSnapshot) -> int | None:
    """Возраст приёма, подтверждённый владельцем, — из ответа FAQ про возраст.

    Единственный источник: то, что владелец написал сам. Отдельного поля для
    этого нет намеренно — иначе оно разъедется с текстом, который читает клиент.
    """
    for entry in kb.faq:
        if entry.id != "age_min":
            continue
        for text in (getattr(entry.answer, "ru", None), getattr(entry.answer, "kk", None)):
            match = _AGE_LIMIT_RE.search(text or "")
            if match:
                return int(match.group(1))
    return None


def _wrong_age_limit(text: str, kb: KBSnapshot) -> tuple[str, ...]:
    """Возрастная граница, не совпадающая с подтверждённой владельцем.

    03.09.2026 бот написал родителю шестилетнего: «по возрасту группы на
    кикбоксинг рассчитаны на детей с 7 лет». Владелец: «такую информацию мы не
    давали, у нас дети с 5 лет». Числа в такой фразе есть, но обычные проверки
    их пропускают: возраст — не деньги и не время, и в данных инструментов его
    не было.

    Возраст самого ребёнка («ему 6 лет») под правило не попадает: оно ищет
    именно границу приёма — «берём/принимаем/группы … с N лет».
    """
    confirmed = _confirmed_min_age(kb)
    if confirmed is None:
        return ()
    bad = [
        match.group(0)
        for match in _AGE_LIMIT_RE.finditer(text)
        if int(match.group(1)) != confirmed
    ]
    return tuple(dict.fromkeys(bad))


def _has_morning_slots(kb: KBSnapshot) -> bool:
    """Есть ли в расписании занятия до полудня."""
    for gym in kb.active_gyms(Scope.ALL):
        for slot in gym.schedule or ():
            head, _, _ = slot.time_start.partition(":")
            if head.isdigit() and int(head) < 12:
                return True
    return False


def _false_denials(text: str, kb: KBSnapshot) -> tuple[str, ...]:
    """Утверждения, которые отрицают то, что в базе знаний есть.

    Обычные проверки ловят выдуманные значения: цену, время, адрес. Но
    «утренних групп нет» не содержит ни одного значения — проверять в нём
    нечего, и такой ответ проходил фильтр насквозь. Между тем ошибка здесь
    дороже выдуманной цифры: клиенту говорят, что школа ему не подходит, и он
    уходит.

    Проверка узкая и названа честно: сейчас в ней одно утверждение — про
    утренние занятия. Каждое следующее добавляется тем же способом — по живому
    случаю, а не про запас.
    """
    match = _MORNING_DENIAL_RE.search(text)
    if match is None or not _has_morning_slots(kb):
        return ()
    return (match.group(0),)


def _forbidden_claims(text: str, kb: KBSnapshot) -> tuple[str, ...]:
    """Запрещённые утверждения: список из KB плюс обещания результата."""
    lowered = text.lower()
    claims: list[str] = []
    for entry in kb.faq:
        for claim in entry.forbidden_claims or ():
            cleaned = claim.strip().lower()
            if cleaned and cleaned in lowered:
                claims.append(claim)
    for claim in _FORBIDDEN_CLAIMS:
        if claim in lowered:
            claims.append(claim)
    return tuple(dict.fromkeys(claims))


def _fail(kind: PostcheckFailKind, offending: Sequence[str]) -> PostcheckVerdict:
    """Отказ: текст наружу не отдаётся, чтобы его нельзя было отправить по ошибке."""
    return PostcheckVerdict(ok=False, kind=kind, offending=tuple(offending), text="")
