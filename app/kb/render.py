"""Сборка текстов из базы знаний: системный префикс промпта и тела артефактов.

**Главное требование модуля — побайтовая стабильность.** Один и тот же снимок
KB обязан давать один и тот же текст префикса при каждом вызове: Gemini
кэширует общее начало запроса, и любой «плавающий» символ в начале промпта
означает промах кэша и умножение счёта за эксплуатацию. Поэтому здесь нет ни
дат, ни времени, ни случайностей, а все коллекции обходятся отсортированными.

Чего в префиксе нет намеренно: телефонов, расписания, ``internal_note`` и полных
текстов FAQ — их отдают инструменты, и они не должны раздувать кэшируемое начало.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Final

from app.kb import gaps as gaps_module
from app.kb.models import FaqEntry, Gym, KBSnapshot, PlanPrice, ScheduleSlot
from app.types import KBValidationError, Language, Scope

#: Разделитель блоков префикса.
_BLOCK_SEP: Final[str] = "\n\n"

#: Роль и тон. Статический текст: правила поведения, а не факты о школе.
_ROLE_BLOCK: Final[str] = (
    "Ты — консультант школы бокса и кикбоксинга AINAZAROV TOP TEAM в Костанае.\n"
    "Ты переписываешься с родителями в WhatsApp и Instagram Direct. Твоя цель — честно "
    "ответить на вопрос и, если это уместно, записать ребёнка на бесплатное пробное занятие. "
    "Ты не продавец, ты помощник: родитель должен уйти с ощущением, что ему помогли, даже "
    "если он не записался.\n"
    "\n"
    "ТОН. Спокойный, уважительный, всегда на «вы» и «сіз». Короткие предложения, конкретика "
    "вместо эпитетов. Тревогу родителя признавай прямо, а не отмахивайся. Если школа не "
    "подходит — скажи об этом честно.\n"
    "\n"
    "ЯЗЫК. Отвечай на языке последнего сообщения клиента: русский или казахский. Третьего "
    "языка нет — на любом другом языке зови администратора.\n"
    "\n"
    "ФОРМАТ ОТВЕТА. Одно сообщение до 600 знаков. Не больше двух сообщений подряд. Один "
    "вопрос в сообщении, а не два. Не задавай вопрос, ответ на который уже есть в переписке. "
    "Без капса, без markdown, без эмодзи.\n"
    "\n"
    "ГЛАВНОЕ ПРАВИЛО. Все факты — цены, адреса, расписание, имена тренеров, условия — берутся "
    "только из инструментов. Ничего не вычисляй и не вспоминай сам: цены считает калькулятор, "
    "адреса отдаёт справочник залов. Если инструмент вернул «данных нет» — произнеси его фразу "
    "и предложи передать вопрос администратору. Выдуманный факт хуже честного «уточню»."
)

#: Блок эталонных диалогов. Показывает длину, тон и обязательный вызов инструментов.
_FEWSHOT_BLOCK: Final[str] = (
    "ЭТАЛОННЫЕ ДИАЛОГИ\n"
    "\n"
    "Клиент: скок стоит абик\n"
    "Ты: (сначала выясняешь географию, потом зовёшь калькулятор)\n"
    "Ответ: «Стоимость зависит от того, где заниматься: в Костанае и в райцентрах области "
    "цены разные. В каком городе или посёлке вам удобно?»\n"
    "\n"
    "Клиент: а во сколько тренировки у вас на кск\n"
    "Ты: (вызываешь справочник расписания, получаешь «данных нет»)\n"
    "Ответ: «Точное расписание по этому залу подскажет администратор — угадывать не буду. "
    "Передать ему ваш вопрос?»\n"
    "\n"
    "Клиент: боюсь что по голове будут бить, сыну 6\n"
    "Ты: (берёшь готовый ответ по теме безопасности, не сочиняешь свой)\n"
    "Ответ: признаёшь тревогу, объясняешь, что в младших группах нет жёстких спаррингов, "
    "и предлагаешь прийти на бесплатное пробное и посмотреть тренировку своими глазами.\n"
    "\n"
    "Клиент: балама 7 жаста, жазыңызшы\n"
    "Ты: отвечаешь по-казахски, уточняешь район и записываешь на пробное."
)


# --------------------------------------------------------------------------- #
# Помощники форматирования
# --------------------------------------------------------------------------- #
def _money(amount: int) -> str:
    """``25000`` -> ``25 000 ₸``. Разряды разделяются обычным пробелом."""
    return f"{amount:,}".replace(",", " ") + " ₸"


def _text(value: str | None, fallback: str = "не указано") -> str:
    return value if value else fallback


def _lang_text(snapshot: KBSnapshot, key: str, lang: Language) -> str:
    return snapshot.text(key, lang)


def _join(lines: Iterable[str]) -> str:
    return "\n".join(line for line in lines if line)


# --------------------------------------------------------------------------- #
# Блоки системного промпта
# --------------------------------------------------------------------------- #
def render_gyms_block(snapshot: KBSnapshot) -> str:
    """Реестр залов: ``id — район — ориентир — география``.

    Адресов и телефонов здесь нет намеренно: их отдаёт инструмент, а модель
    должна знать только, какие точки существуют и как их различать.
    """
    lines = ["РЕЕСТР ЗАЛОВ (только эти точки существуют)"]
    for scope, title in ((Scope.CITY, "Костанай"), (Scope.REGION, "Райцентры области")):
        gyms = snapshot.active_gyms(scope)
        if not gyms:
            continue
        lines.append(f"{title}:")
        for gym in gyms:
            parts = [gym.id, _text(gym.district.ru, gym.settlement)]
            if gym.landmark.ru:
                parts.append(gym.landmark.ru)
            if gym.is_head:
                parts.append("головной зал")
            lines.append("- " + " | ".join(parts))
    for gym in snapshot.unresolved_gyms():
        lines.append(
            f"- ВНИМАНИЕ, район «{_text(gym.district.ru, gym.settlement)}»: данные не подтверждены. "
            "Не утверждай ни что зал там есть, ни что его нет — зови администратора."
        )
    duplicates = _duplicate_district_names(snapshot)
    if duplicates:
        lines.append(
            "- Одинаково названных районов несколько: "
            + "; ".join(duplicates)
            + ". Никогда не предлагай «центральный зал» вообще — уточняй ориентир."
        )
    return _join(lines)


def _duplicate_district_names(snapshot: KBSnapshot) -> list[str]:
    """Районы, под которые подходит больше одного зала (например, два «Центра»)."""
    buckets: dict[str, list[str]] = {}
    for gym in snapshot.active_gyms(Scope.CITY):
        key = (gym.district.ru or "").split("/")[0].strip().lower()
        if key:
            buckets.setdefault(key, []).append(gym.id)
    return [
        f"{name} — {', '.join(sorted(ids))}"
        for name, ids in sorted(buckets.items())
        if len(ids) > 1
    ]


def render_pricing_showcase(snapshot: KBSnapshot) -> str:
    """Витрина цен: ориентиры без расчёта. Считает только калькулятор."""
    pricing = snapshot.pricing
    lines = ["ЦЕНЫ (ориентиры; итог считает только инструмент расчёта стоимости)"]
    city_bits: list[str] = []
    for key in sorted(pricing.city_plans):
        plan = pricing.city_plans[key]
        if plan is not None:
            city_bits.append(f"{plan.label.ru} — {_money(plan.price)}")
    if pricing.city_single is not None:
        city_bits.append(f"{pricing.city_single.label.ru} — {_money(pricing.city_single.price)}")
    if city_bits:
        lines.append(
            f"{pricing.city_settlement}: абонемент {pricing.city_sessions} занятий на "
            f"{pricing.city_validity_days} дней; " + "; ".join(city_bits) + "."
        )
    region_bits: list[str] = []
    for key in sorted(pricing.region_plans):
        plan = pricing.region_plans[key]
        if plan is not None:
            region_bits.append(f"{plan.label.ru} — {_money(plan.price)}")
    if pricing.region_family_price_per_child is not None:
        region_bits.append(
            f"семейный — {_money(pricing.region_family_price_per_child)} за ребёнка "
            f"от {pricing.region_family_min_children} детей"
        )
    if region_bits:
        lines.append("Райцентры области: " + "; ".join(region_bits) + ".")
    discount = pricing.city_family_discount
    if discount.rules:
        rules = ", ".join(
            f"ребёнок №{index} — минус {percent}%" for index, percent in sorted(discount.rules)
        )
        lines.append(f"Семейная скидка в городе: {rules}.")
    lines.append(
        "Сам не складывай, не умножай и не округляй: любую сумму называй только после вызова "
        "калькулятора. Сначала выясни город или райцентр — одно и то же слово «абонемент» стоит "
        "по-разному."
    )
    if pricing.derived_enabled and pricing.derived_facts:
        lines.append("Разрешённые аргументы о выгоде (произносить дословно по смыслу):")
        for fact in sorted(pricing.derived_facts, key=lambda item: item.id):
            lines.append(f"- {fact.ru}")
    return _join(lines)


def render_faq_digest(snapshot: KBSnapshot) -> str:
    """Дайджест FAQ: тема — о чём спрашивают — есть ли готовый ответ."""
    lines = ["ТЕМЫ ГОТОВЫХ ОТВЕТОВ (полный текст выдаёт инструмент справки, не сочиняй свой)"]
    by_topic: dict[str, list[FaqEntry]] = {}
    for entry in snapshot.faq:
        by_topic.setdefault(entry.topic, []).append(entry)
    for topic in sorted(by_topic):
        entries = sorted(by_topic[topic], key=lambda item: item.id)
        sample = _first_question(entries)
        answered = sum(1 for entry in entries if entry.answered)
        state = "ответ есть" if answered else "ответа нет, нужен администратор"
        suffix = f" — {sample}" if sample else ""
        lines.append(f"- {topic}{suffix} ({state})")
    forbidden = sorted({claim for entry in snapshot.faq for claim in entry.forbidden_claims})
    if forbidden:
        lines.append("Никогда не произноси эти формулировки:")
        for claim in forbidden:
            lines.append(f"- {claim}")
    return _join(lines)


def _first_question(entries: Sequence[FaqEntry]) -> str:
    for entry in entries:
        variants = entry.question_variants.get(Language.RU) or []
        if variants:
            return f"«{variants[0]}»"
    return ""


def render_artifacts_catalog(snapshot: KBSnapshot) -> str:
    """Каталог материалов: ``id — когда уместно отправить``."""
    lines = ["МАТЕРИАЛЫ, КОТОРЫЕ МОЖНО ОТПРАВИТЬ (только эти, текст менять нельзя)"]
    for artifact_id in snapshot.artifact_ids():
        artifact = snapshot.artifact(artifact_id)
        if artifact is None:  # pragma: no cover - защита от рассинхрона
            continue
        lines.append(f"- {artifact.id}: {artifact.when_to_send_ru}")
    if len(lines) == 1:
        lines.append("- материалов нет")
    return _join(lines)


def render_gaps_manifest(snapshot: KBSnapshot) -> str:
    """Манифест пробелов: чего в базе нет и что говорить вместо выдумки."""
    lines = [
        "ЧЕГО В БАЗЕ НЕТ (самый важный блок: здесь модель обязана молчать о фактах)",
    ]
    for gap in gaps_module.open_gaps(snapshot):
        info = gaps_module.info(gap)
        if info is None:  # pragma: no cover - реестр покрывает весь enum
            continue
        phrase = snapshot.text(gaps_module.i18n_key_for(gap), Language.RU)
        lines.append(f"- {gap.value} {info.title_ru}: данных нет. Говори так: «{phrase}»")
    lines.append(
        "По-казахски произноси то же самое по-казахски. Никогда не подставляй вместо "
        "отсутствующих данных «обычно», «как правило» или пример из другой школы."
    )
    return _join(lines)


def render_schedule_overview(snapshot: KBSnapshot) -> str:
    """Какие смены вообще существуют — словами, без единой цифры.

    В промпте не было ни слова о том, когда идут тренировки: реестр залов,
    прайс, FAQ — и всё. На вопрос «можно ли прийти утром» модели нечего было
    прочитать, и она ответила догадкой: «утренних групп у нас нет, все занятия
    во второй половине дня». Утренние группы есть. Владелец увидел этот ответ и
    написал «не верная информация».

    Времени здесь нет намеренно. Настоящие числа в промпте модель списывает
    вместо вызова инструмента, а постфильтр подтверждает время только данными
    ``get_schedule`` — и снимает ответ целиком. Поэтому смены названы словами:
    факт «утро есть» модель получает, а часы по-прежнему обязана спросить у
    инструмента.
    """
    shifts: dict[str, set[str]] = {"утренние": set(), "дневные": set(), "вечерние": set()}
    for gym in snapshot.active_gyms(Scope.ALL):
        for slot in gym.schedule or ():
            hour = int(slot.time_start.split(":", 1)[0]) if ":" in slot.time_start else 0
            name = "утренние" if hour < 12 else "дневные" if hour < 17 else "вечерние"
            shifts[name].add(gym.id)
    if not any(shifts.values()):
        return ""

    lines = ["КАКИЕ СМЕНЫ ЕСТЬ (точное время занятий — только через get_schedule)"]
    for name, gyms in shifts.items():
        if gyms:
            lines.append(f"- {name} группы: {', '.join(sorted(gyms))}")
    if shifts["утренние"]:
        lines.append(
            "Утренние группы ЕСТЬ. Не отвечай, что занятия бывают только вечером "
            "или что прийти утром нельзя."
        )
    else:
        lines.append("Утренних групп нет ни в одном зале.")
    return _join(lines)


def render_escalation_rules(snapshot: KBSnapshot) -> str:
    """Правила передачи диалога администратору и запреты бренда."""
    policies = snapshot.policies
    lines = ["КОГДА ЗВАТЬ АДМИНИСТРАТОРА"]
    for reason in sorted(reason.value for reason in policies.escalation_triggers):
        lines.append(f"- {reason}")
    if policies.audience_adults_only:
        lines.append(
            "Бот разговаривает с родителем. Если по переписке видно, что пишет сам ребёнок — "
            "доброжелательно попроси позвать маму или папу и передай диалог администратору."
        )
    # Бот — консультант и продавец. Всё, что касается уже занимающихся детей,
    # ведёт человек: у бота нет ни журнала посещаемости, ни права переносить
    # занятия. Правило написано здесь, а не только словарём: формулировок у
    # родителей бесконечно много, а словарь ловит перечисленные.
    lines.append(
        "ДЕЙСТВУЮЩИЕ КЛИЕНТЫ — ТОЛЬКО ЧЕЛОВЕК. Пропуск и опоздание, перенос "
        "занятия, смена группы или зала, заморозка и возврат абонемента, "
        "претензия к тренировке, вопрос об уже купленном абонементе: не отвечай "
        "сам и ничего не обещай — вызови escalate_to_manager. У тебя нет ни "
        "журнала посещаемости, ни расписания конкретного ребёнка."
    )
    if policies.work_hours is not None and policies.work_hours.ru:
        lines.append(f"Часы работы администратора: {policies.work_hours.ru}")
    if policies.sla_reply_minutes is not None:
        lines.append(f"Обещай ответ администратора в течение {policies.sla_reply_minutes} минут.")
    else:
        lines.append("Срок ответа администратора не обещай — он не задан.")
    if policies.forbidden_behaviour:
        lines.append("ЗАПРЕЩЕНО ВСЕГДА:")
        for item in policies.forbidden_behaviour:
            lines.append(f"- {item}")
    return _join(lines)


def render_system_prompt(snapshot: KBSnapshot) -> str:
    """Статический префикс ``system_instruction``. Байт-в-байт стабилен при одном ``kb_hash``.

    Порядок блоков зафиксирован (KB-SPEC §8.2): роль и тон -> реестр залов ->
    витрина цен -> дайджест FAQ -> каталог артефактов -> манифест пробелов ->
    правила эскалации -> few-shot. Динамики (дата, язык, имя) здесь нет.
    """
    blocks = (
        _ROLE_BLOCK,
        render_gyms_block(snapshot),
        render_schedule_overview(snapshot),
        render_pricing_showcase(snapshot),
        render_faq_digest(snapshot),
        render_artifacts_catalog(snapshot),
        render_gaps_manifest(snapshot),
        render_escalation_rules(snapshot),
        _FEWSHOT_BLOCK,
    )
    return _BLOCK_SEP.join(block for block in blocks if block)


# --------------------------------------------------------------------------- #
# Оформление карточек
# --------------------------------------------------------------------------- #
#: Значки помечают ТИП строки, а не украшают её. Словарь закрытый и маленький:
#: когда значков много, они перестают что-либо значить и сообщение начинает
#: выглядеть рассылкой, а это детская школа.
ICON_GYM: Final[str] = "🥊"
ICON_PIN: Final[str] = "📍"
ICON_MAP: Final[str] = "🗺"
ICON_TIME: Final[str] = "🕒"
ICON_SCHEDULE: Final[str] = "🗓"
ICON_PRICE: Final[str] = "💳"
ICON_FAMILY: Final[str] = "👨‍👩‍👦"

#: Сокращения дней недели. Это календарные подписи, а не факты о школе, поэтому
#: они живут в коде рендера, а не в базе знаний.
_DAY_SHORT: Final[dict[Language, dict[str, str]]] = {
    Language.RU: {
        "mon": "Пн", "tue": "Вт", "wed": "Ср", "thu": "Чт",
        "fri": "Пт", "sat": "Сб", "sun": "Вс",
    },
    Language.KK: {
        "mon": "Дс", "tue": "Сс", "wed": "Ср", "thu": "Бс",
        "fri": "Жм", "sat": "Сн", "sun": "Жк",
    },
}

_DAY_ORDER: Final[tuple[str, ...]] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _blocks(parts: Iterable[str]) -> str:
    """Склеивает блоки пустой строкой между ними.

    Пустая строка — главный инструмент читаемости в мессенджере: без неё шесть
    залов сливаются в стену текста, которую не читают, а пролистывают.
    """
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def _days_line(days: Sequence[str], lang: Language) -> str:
    """Дни занятия через запятую, в календарном порядке: «Пн, Ср, Пт»."""
    short = _DAY_SHORT.get(lang, _DAY_SHORT[Language.RU])
    ordered = [day for day in _DAY_ORDER if day in days]
    return ", ".join(short.get(day, day) for day in ordered)


def _clean_address(gym: Gym, lang: Language) -> str:
    """Адрес зала без хвоста, который уже сказан в названии.

    В данных название, адрес и ориентир пересказывают друг друга: «КСК — школа
    №9», «Каирбекова 334, школа №9 (цокольный этаж)», «цокольный этаж школы №9».
    Склеенные подряд, они читаются как сбой программы, а не как забота.
    """
    address = gym.address.get(lang) or ""
    landmark = gym.landmark.get(lang) or ""
    if landmark and not _already_said(landmark, f"{gym.title.get(lang) or ''} {address}"):
        return f"{address}, {landmark}" if address else landmark
    return address


# --------------------------------------------------------------------------- #
# Тела артефактов
# --------------------------------------------------------------------------- #
def render_gyms_list_card(snapshot: KBSnapshot, *, scope: Scope, lang: Language) -> str:
    """Тело артефакта ``gyms_list_*`` (``render_from: gyms``).

    Один зал — один блок из двух строк: номер с названием и адрес со значком.
    Нумерация не украшение: кнопок в WhatsApp и Instagram нет, и номер — это то,
    чем родитель отвечает («напишите номер зала»).
    """
    gyms = snapshot.active_gyms(scope if scope in (Scope.CITY, Scope.REGION) else Scope.ALL)
    if not gyms:
        return _lang_text(snapshot, "gap.generic", lang)

    if scope is Scope.CITY:
        title_key = "card.gyms_city_title"
    elif scope is Scope.REGION:
        title_key = "card.gyms_region_title"
    else:
        title_key = "card.gyms_all_title"

    parts: list[str] = [f"{ICON_GYM} {_lang_text(snapshot, title_key, lang)}"]
    for number, gym in enumerate(gyms, start=1):
        name = gym.title.get(lang) or gym.title.ru or gym.id
        address = _clean_address(gym, lang)
        parts.append(f"{number}. {name}\n{ICON_PIN} {address}" if address else f"{number}. {name}")

    if any(not gym.address.filled for gym in gyms):
        parts.append(_lang_text(snapshot, "gap.region_address", lang))

    # Список по городу — не весь список школы. Владелец считает залы вместе с
    # Тобылом и райцентрами, и карточка «6 залов» выглядит так, будто часть
    # точек потеряли. Прайс при этом разный, поэтому смешивать их в один
    # нумерованный список нельзя: строка отдельная и без номеров.
    if scope is Scope.CITY:
        elsewhere = {
            gym.settlement for gym in snapshot.active_gyms(Scope.REGION) if gym.settlement
        }
        if elsewhere:
            # Числом, а не перечислением: семь названий в строке не помещаются на
            # экране телефона, а полный список отдаёт отдельная карточка по
            # области — вместе с районным прайсом, который отличается втрое.
            parts.append(snapshot.text("card.also_elsewhere", lang, count=len(elsewhere)))

    parts.append(_lang_text(snapshot, "card.pick_gym", lang))
    return _blocks(parts)


def render_price_card(snapshot: KBSnapshot, *, scope: Scope, lang: Language) -> str:
    """Тело артефакта ``price_card_*`` (``render_from: pricing``).

    Тарифы идут в том порядке, в каком они записаны в базе знаний, а не по
    алфавиту: владелец ставит первым тот, который продаёт, — а сортировка
    выносила вперёд «Гибкий» просто потому, что «Г» раньше «С».
    """
    pricing = snapshot.pricing
    parts: list[str] = []

    if scope is Scope.REGION:
        parts.append(f"{ICON_PRICE} {_lang_text(snapshot, 'card.price_region_title', lang)}")
        plans = [plan for plan in pricing.region_plans.values() if plan is not None]
        if plans:
            parts.append(_lang_text(snapshot, "card.subscription", lang))
            parts.extend(_plan_block(plan, lang) for plan in plans)
        if pricing.region_single is not None:
            parts.append(f"{ICON_GYM} {_plan_block(pricing.region_single, lang)}")
        if pricing.region_family_price_per_child is not None:
            label = pricing.region_family_label.get(lang)
            if label:
                parts.append(f"{ICON_FAMILY} {label}")
    else:
        parts.append(f"{ICON_PRICE} {_lang_text(snapshot, 'card.price_city_title', lang)}")
        plans = [plan for plan in pricing.city_plans.values() if plan is not None]
        if plans:
            parts.append(
                pricing.city_validity_note.get(lang)
                or _lang_text(snapshot, "card.subscription", lang)
            )
            parts.extend(_plan_block(plan, lang) for plan in plans)
        if pricing.city_single is not None:
            parts.append(f"{ICON_GYM} {_plan_block(pricing.city_single, lang)}")
        discount_label = pricing.city_family_discount.label.get(lang)
        if discount_label:
            parts.append(f"{ICON_FAMILY} {discount_label}")

    if len(parts) <= 1:
        return _lang_text(snapshot, "gap.generic", lang)
    return _blocks(parts)


def _plan_block(plan: PlanPrice, lang: Language) -> str:
    """Тариф: цена в первой строке, оговорка — во второй.

    Оговорка обязана быть видна ДО покупки, а не теряться в конце строки после
    тире: «перерасчёта за пропуски нет» — это то, из-за чего потом спорят.
    """
    label = plan.label.get(lang) or plan.label.ru or ""
    line = f"{label} — {_money(plan.price)}"
    note = plan.note.get(lang) if plan.note is not None else None
    return f"{line}\n{note}" if note else line


def _plan_line(plan: PlanPrice, lang: Language) -> str:
    """Тариф одной строкой — для системного префикса, который читает модель."""
    label = plan.label.get(lang) or plan.label.ru or ""
    line = f"{label}: {_money(plan.price)}"
    note = plan.note.get(lang) if plan.note is not None else None
    return f"{line} — {note}" if note else line


def render_gym_location(snapshot: KBSnapshot, *, gym_id: str, lang: Language) -> str:
    """Тело артефакта ``gym_location_<gym_id>``: адрес, ориентир и ссылка, если есть."""
    gym = snapshot.gym(gym_id)
    if gym is None:
        raise KBValidationError(
            f"в kb/gyms.yaml нет зала '{gym_id}'",
            errors=(f"gyms.yaml: отсутствует зал '{gym_id}'",),
        )
    title = gym.title.get(lang) or gym.title.ru or gym.id
    address = gym.address.get(lang)
    landmark = gym.landmark.get(lang)

    # Заголовок, адрес и ориентир часто пересказывают друг друга: «Центр —
    # Жана-Кала», «Касымханова 10», «район Жана-Кала». В переписке это читается
    # как сбой, поэтому каждая следующая строка печатается только если добавляет
    # что-то новое к уже сказанному.
    lines: list[str] = []
    # Заголовок нужен только если он говорит больше адреса: «Центр — Жана-Кала»
    # + «Касымханова 10» полезно, а «Тобыл» + «Тобыл, улица …» — повтор.
    if title and not (address and _already_said(title, address)):
        lines.append(f"{ICON_PIN} {title}")
    if address:
        lines.append(address)
    if not lines:
        lines.append(f"{ICON_PIN} {title}")
    if landmark and not _already_said(landmark, " ".join(lines)):
        # Регистр ориентира не трогаем: в базе он и так со строчной («цокольный
        # этаж школы №9»), а принудительное понижение портило казахские названия —
        # «Жаңа Қала ауданы» превращалось в «жаңа Қала ауданы».
        label = _lang_text(snapshot, "card.landmark", lang)
        lines.append(f"{label}: {landmark}")

    parts = ["\n".join(lines)]
    if gym.map_url:
        # Ссылку подписываем и выносим отдельным блоком: голый адрес с процентами
        # посреди сообщения читается как мусор.
        parts.append(f"{ICON_MAP} {_lang_text(snapshot, 'card.map', lang)}: {gym.map_url}")
    if not address and not landmark:
        parts.append(_lang_text(snapshot, "gap.region_address", lang))
    return _blocks(parts)


#: Слова, которые сами по себе ничего не различают: они есть почти в каждом
#: адресе и ориентире, и учитывать их при сравнении — значит считать разными
#: строки «Центр — Жана-Кала» и «район Жана-Кала».
_GENERIC_PLACE_WORDS: Final[frozenset[str]] = frozenset(
    {
        "район",
        "районе",
        "микрорайон",
        "квартал",
        "участок",
        "улица",
        "город",
        "центр",
        "магазин",
        "магазина",
        "школа",
        "школы",
        "этаж",
        "цокольный",
        "цокольном",
        "возле",
        "около",
        "рядом",
        "медицинский",
        "медцентр",
        "центре",
        "ориентир",
    }
)


def _already_said(candidate: str, existing: str) -> bool:
    """Повторяет ли строка то, что уже написано выше.

    Сравниваются только РАЗЛИЧАЮЩИЕ слова. «Центр — Жана-Кала» и «район
    Жана-Кала» для читателя — одно и то же: различает их слово «Жана-Кала»,
    а «центр» и «район» встречаются почти в каждой строке.
    """
    def significant(text: str) -> set[str]:
        return {
            word
            for word in re.findall(r"\w{2,}", text.lower())
            if word not in _GENERIC_PLACE_WORDS
        }

    words = significant(candidate)
    if not words:
        return True  # одни общие слова — новой информации нет
    return words.issubset(significant(existing))


def render_schedule_card(
    snapshot: KBSnapshot, *, gym_id: str, slots: Sequence[ScheduleSlot], lang: Language
) -> str:
    """Расписание зала: занятия сгруппированы по виду, дни сведены в одну строку.

    Собирается кодом, а не моделью, по той же причине, по которой код считает
    цену: перечисление «вторник, четверг и суббота с 17:30 до 19:00» одной
    строкой прозой человек читает трижды, прежде чем понять, во сколько ему
    приходить. И ошибиться в дне при пересказе модель может, а рендер — нет.
    """
    gym = snapshot.gym(gym_id)
    title = (gym.title.get(lang) or gym.title.ru or gym_id) if gym else gym_id
    if not slots:
        return _lang_text(snapshot, "gap.schedule", lang)

    by_discipline: dict[str, list[ScheduleSlot]] = {}
    for slot in slots:
        by_discipline.setdefault(slot.discipline, []).append(slot)

    parts: list[str] = [f"{ICON_SCHEDULE} {_lang_text(snapshot, 'card.schedule_title', lang)} — {title}"]
    for discipline, group in by_discipline.items():
        name = _lang_text(snapshot, f"card.{discipline}", lang)
        lines = [f"{ICON_GYM} {name}"]
        for slot in sorted(group, key=lambda item: (item.time_start, item.days[0])):
            days = _days_line(slot.days, lang)
            line = f"{days} · {slot.time_start}–{slot.time_end}"
            if slot.age_known:
                line += f" ({slot.age_from}–{slot.age_to})"
            lines.append(f"{ICON_TIME} {line}")
        parts.append("\n".join(lines))

    if any(not slot.age_known for slot in slots):
        parts.append(_lang_text(snapshot, "card.age_unknown", lang).capitalize() + ".")
    return _blocks(parts)


def render_route_caption(snapshot: KBSnapshot, *, gym_id: str, lang: Language) -> str:
    """Подпись к видео маршрута — в том же виде, в каком её пишет сама школа.

    Владелец годами отправляет клиентам видео дороги с одной и той же подписью:
    ссылка на 2ГИС, адрес, ориентир, вид занятий, дни и время. Это готовый,
    проверенный людьми формат, и выдумывать вместо него свой незачем — здесь он
    просто собирается из базы знаний, чтобы не набирать руками каждый раз.
    """
    gym = snapshot.gym(gym_id)
    if gym is None:
        raise KBValidationError(
            f"в kb/gyms.yaml нет зала '{gym_id}'",
            errors=(f"gyms.yaml: отсутствует зал '{gym_id}'",),
        )
    parts: list[str] = [render_gym_location(snapshot, gym_id=gym_id, lang=lang)]
    if gym.schedule:
        parts.append(render_schedule_card(snapshot, gym_id=gym_id, slots=gym.schedule, lang=lang))
    return _blocks(parts)


def render_artifact_body(snapshot: KBSnapshot, *, artifact_id: str, lang: Language) -> str:
    """Готовый текст любого артефакта: из ``body`` или собранный кодом.

    Единая точка для слоя инструментов: ему незачем знать, какой артефакт
    статический, а какой собирается из прайса или из списка залов.
    """
    artifact = snapshot.artifact(artifact_id)
    if artifact is None:
        raise KBValidationError(
            f"в kb/media.yaml нет артефакта '{artifact_id}'",
            errors=(f"media.yaml: отсутствует артефакт '{artifact_id}'",),
        )
    if artifact.render_from == "pricing":
        return render_price_card(snapshot, scope=artifact.scope, lang=lang)
    if artifact.render_from == "gyms":
        if artifact.gym_id is not None:
            return render_gym_location(snapshot, gym_id=artifact.gym_id, lang=lang)
        return render_gyms_list_card(snapshot, scope=artifact.scope, lang=lang)
    if artifact.render_from == "route" and artifact.gym_id is not None:
        return render_route_caption(snapshot, gym_id=artifact.gym_id, lang=lang)
    body = artifact.body.get(lang) if artifact.body is not None else None
    return body or _lang_text(snapshot, "gap.generic", lang)


__all__ = [
    "render_artifact_body",
    "render_schedule_overview",
    "render_artifacts_catalog",
    "render_escalation_rules",
    "render_faq_digest",
    "render_gaps_manifest",
    "render_gym_location",
    "render_gyms_block",
    "render_gyms_list_card",
    "render_price_card",
    "render_pricing_showcase",
    "render_route_caption",
    "render_schedule_card",
    "render_system_prompt",
]
