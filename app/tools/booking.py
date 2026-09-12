"""Запись на пробное — инструмент ``create_trial_lead``.

Здесь заканчивается разговор и начинается обязательство школы перед родителем,
поэтому всё, что модель передала словами, проверяется своим кодом:

* **телефон** нормализуется и сверяется с :data:`app.types.PHONE_E164_KZ_RE`.
  Модель охотно «поправит» номер, если родитель ошибся в цифре, — принять такую
  правку значит потерять лид навсегда;
* **возраст** обязан попасть в 3..17. Вне диапазона лид не создаётся вообще:
  решение о взрослом или о четырёхлетнем принимает человек;
* **зал** обязан существовать в базе знаний и работать.

Проверки согласия и кода ``need_consent`` здесь **нет** (SCOPE-OVERRIDE §1):
телефон принимается и сохраняется сразу.

Идемпотентность: повторный вызов в пределах диалога обновляет тот же лид.
Ключ — ``conversation_id``, а не содержимое аргументов: родитель может дважды
уточнить имя ребёнка, и это не повод заводить второго лида.
"""

from __future__ import annotations

import re
import string
from datetime import datetime
from typing import Any, Final, Mapping

from app.config import get_settings
from app.kb import render
from app.kb.gaps import say_no_data
from app.kb.models import Gym, KBSnapshot, ScheduleSlot
from app.kb.sessions import (
    SessionChoice,
    TrialSession,
    client_named_age,
    client_named_day,
    client_named_discipline,
    client_named_name,
    client_named_time,
    normalize_time,
    resolve_trial_session,
)
from app.types import (
    TRIAL_CONFIRMATION_ARTIFACT,
    EscalationReason,
    GapRef,
    Gender,
    GymStatus,
    Language,
    LeadDraft,
    LeadStatus,
    ManagerCard,
    ManagerCardKind,
    MAX_CHILD_AGE,
    MIN_CHILD_AGE,
    OutboundKind,
    OutboundMessage,
    PhoneSource,
    RenderHint,
    ToolContext,
    ToolResult,
    Urgency,
    normalize_phone_kz,
)

#: Границы возраста и нормализация телефона живут в :mod:`app.types`: те же
#: правила нужны слою LLM при извлечении лида, а импортировать ``app.tools``
#: ему запрещено правилом зависимостей (INTERFACES §1.1). Здесь — реэкспорт,
#: чтобы публичный API инструмента не менялся.
__all__ = [
    "MAX_CHILD_AGE",
    "MIN_CHILD_AGE",
    "create_trial_lead",
    "format_local_dt",
    "normalize_phone_kz",
    "render_card_text",
]

#: Максимальная длина имени ребёнка (JSON-схема инструмента).
MAX_NAME_CHARS: Final[int] = 60

_LETTER_RE: Final[re.Pattern[str]] = re.compile(r"[^\W\d_]", re.UNICODE)
_PLACEHOLDER: Final[str] = "—"


# --------------------------------------------------------------------------- #
# Время
# --------------------------------------------------------------------------- #
def format_local_dt(moment: datetime) -> str:
    """Момент в таймзоне школы, строкой для карточки администратора.

    Настройки читаются лениво и защищённо: карточка обязана уйти даже тогда,
    когда таймзона в окружении задана неверно.
    """
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(get_settings().timezone)
        local = moment.astimezone(tz)
    except Exception:  # pragma: no cover - защита от битой настройки
        local = moment
    return local.strftime("%d.%m.%Y %H:%M")


def _school_tz() -> str:
    """Часовой пояс школы; при битой настройке — Алматы, как в карточках."""
    try:
        return get_settings().timezone or "Asia/Almaty"
    except Exception:  # pragma: no cover - настройки чинятся на старте
        return "Asia/Almaty"


_SURNAME_HINT: Final[str] = (
    "Для записи нужна фамилия ребёнка. Спроси её коротко. Если родитель не хочет "
    "называть фамилию — вызови инструмент снова с no_surname=true."
)

_CHOICE_HINTS: Final[dict[str, str]] = {
    "need_time": (
        "Родитель ещё не выбрал время пробного. Спроси одним вопросом, какое время "
        "удобнее, перечислив варианты нумерованным списком (1., 2.): {options}. Затем вызови "
        "инструмент снова с session_time."
    ),
    "unknown_time": "Такого времени в расписании зала нет. Предложи варианты нумерованным списком (1., 2.): {options}.",
    "unknown_day": "В этот день такого занятия нет. Предложи варианты нумерованным списком (1., 2.): {options}.",
    "unknown_discipline": "Такой секции в этом зале нет. Предложи варианты нумерованным списком (1., 2.): {options}.",
    "need_discipline": (
        "В это время идут и бокс, и кикбоксинг. Спроси родителя, на какую секцию записать, "
        "перечислив варианты нумерованным списком (1., 2.), и передай discipline. Варианты: {options}."
    ),
}


def _ask_child_hint(kb: KBSnapshot, lang: Language, *, name: bool, age: bool) -> str:
    """Подсказка модели: спросить ребёнка формулировкой базы, а не своей «как зовут»."""
    key = "funnel.name_age" if name and age else ("funnel.name" if name else "funnel.age")
    what = "фамилию, имя и возраст" if name and age else ("фамилию и имя" if name else "возраст")
    question = (kb.text(key, lang) or "").strip()
    return f"Родитель ещё не называл {what} ребёнка — не придумывай. Спроси дословно: «{question}»"


def _options_line(kb: KBSnapshot, slots: tuple[ScheduleSlot, ...]) -> str:
    """Варианты для родителя: «Кикбоксинг — Пн, Ср, Пт 19:00; Бокс — Вт, Чт, Сб 09:00»."""
    by_discipline: dict[str, list[str]] = {}
    for slot in slots:
        label = kb.text(f"card.{slot.discipline}", Language.RU)
        entry = f"{render.days_label(slot.days, Language.RU)} {slot.time_start}"
        if entry not in by_discipline.setdefault(label, []):
            by_discipline[label].append(entry)
    return "; ".join(f"{label} — {', '.join(entries)}" for label, entries in by_discipline.items())


def _choice_hint(kb: KBSnapshot, choice: SessionChoice) -> str:
    template = _CHOICE_HINTS.get(choice.problem or "", "Уточни у родителя удобное время: {options}.")
    return template.format(options=_options_line(kb, choice.options) or "—")


def _slot_text(kb: KBSnapshot, session: TrialSession) -> str:
    """Время пробного для карточки администратору: «Ср 09.09 19:00, Кикбоксинг»."""
    day = render.days_label([session.weekday], Language.RU)
    label = kb.text(f"card.{session.discipline}", Language.RU)
    return f"{day} {session.starts_at:%d.%m} {session.time_start}, {label}"


def _what_to_bring(kb: KBSnapshot, lang: Language) -> str | None:
    """Что взять на первую тренировку — ответ базы знаний, единственный источник."""
    entry = next((item for item in kb.faq if item.id == "gear_first_lesson"), None)
    if entry is None or not entry.answered:
        return None
    return entry.answer.get(lang) or entry.answer.ru


# --------------------------------------------------------------------------- #
# Служебное
# --------------------------------------------------------------------------- #
#: Слова, которыми модель подменяет неизвестное имя. В карточке администратора
#: «Ребёнок: сын, 9 лет» бесполезно: звонить и спрашивать имя придётся заново.
#: Найдено на живом прогоне — модель записала ребёнка как «сын», не спросив имени.
_NAME_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {
        "сын",
        "сынок",
        "дочь",
        "дочка",
        "ребенок",
        "ребёнок",
        "мальчик",
        "девочка",
        "малыш",
        "бала",
        "ұл",
        "ұлым",
        "қыз",
        "қызым",
        "балам",
        # Косвенные падежи: модель пишет «сына» и «дочку», а не «сын» и «дочь».
        "сына",
        "сыну",
        "сыночка",
        "дочку",
        "дочки",
        "дочери",
        "ребёнка",
        "ребенка",
        "мальчика",
        "девочку",
        "баланы",
        "ұлымды",
        "қызымды",
        "неизвестно",
        "не указано",
        "нет имени",
        "имя",
    }
)


def _clean_name(value: str | None, limit: int = MAX_NAME_CHARS) -> str | None:
    """Имя без служебного мусора; ``None``, если это не имя.

    Слово-заглушка вместо имени («сын», «дочка», «бала») именем не считается:
    администратору нужно, к кому обращаться, а не категория родства.
    """
    if not value:
        return None
    cleaned = " ".join(str(value).split())[:limit].strip()
    if not cleaned or _LETTER_RE.search(cleaned) is None:
        return None
    if cleaned.casefold().strip(".,!?") in _NAME_PLACEHOLDERS:
        return None
    # «Айгуль сын» — имя мамы из контакта и слово «сын»: живой прогон 10.09.2026
    # записал ребёнка именно так. Слово родства внутри имени — признак подмены.
    if any(token.casefold().strip(".,!?") in _NAME_PLACEHOLDERS for token in cleaned.split()):
        return None
    return cleaned


def _clean_text(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    cleaned = " ".join(str(value).split())[:limit].strip()
    return cleaned or None


def _gender_of(value: str | None) -> Gender:
    try:
        return Gender(str(value or "unknown").strip().lower())
    except ValueError:
        return Gender.UNKNOWN


def _missing_required(draft: LeadDraft) -> tuple[str, ...]:
    """Каких полей не хватает для статуса ``trial_booked``.

    Повторяет контракт :meth:`app.types.LeadDraft.missing_required`, но считается
    здесь: инструмент обязан работать одинаково независимо от того, что положил
    в черновик пайплайн.
    """
    missing: list[str] = []
    if not draft.child_name:
        missing.append("child_name")
    if draft.child_age is None:
        missing.append("child_age")
    if not draft.gym_id:
        missing.append("gym_id")
    if draft.lang is None:
        missing.append("lang")
    if not draft.phone and not draft.channel_user:
        missing.append("phone|channel_user")
    return tuple(missing)


class _SafeDict(dict):
    """Словарь для ``format_map``: неизвестный плейсхолдер остаётся видимым."""

    def __missing__(self, key: str) -> str:  # pragma: no cover - тривиально
        return "{" + key + "}"


def render_card_text(kb: Any, key: str, params: Mapping[str, Any]) -> str:
    """Подставляет параметры в шаблон карточки из ``kb/i18n.yaml``.

    Своя подстановка нужна потому, что среди плейсхолдеров карточек есть
    ``{lang}``, а у :meth:`KBSnapshot.text` параметр с таким же именем —
    передать его через ``**kwargs`` нельзя. Шаблон берётся из KB (отсутствие
    ключа по-прежнему даёт ``KBValidationError``), а форматирование делается
    здесь и не падает на забытом параметре.
    """
    template = kb.text(key, Language.RU)
    try:
        return string.Formatter().vformat(template, (), _SafeDict(params))
    except (IndexError, KeyError, ValueError):  # pragma: no cover - битый шаблон
        return template


#: Подписи для карточки администратору: она всегда по-русски.
_LANG_TITLES: Final[dict[str, str]] = {"ru": "русский", "kk": "казахский"}
_CHANNEL_TITLES: Final[dict[str, str]] = {"whatsapp": "WhatsApp", "instagram": "Instagram", "telegram": "Telegram"}


def _readable_phone(value: str | None) -> str | None:
    """«77019990013» → «+7 701 999 00 13»: так номер читается и набирается с экрана."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("7"):
        return f"+7 {digits[1:4]} {digits[4:7]} {digits[7:9]} {digits[9:11]}"
    return value or None


def _dialog_link(ctx: ToolContext) -> str | None:
    """Ссылка на переписку в CRM — чтобы не искать клиента вручную."""
    try:
        base = (get_settings().public_base_url or "").rstrip("/")
    except Exception:  # pragma: no cover - настройки чинятся на старте
        return None
    return f"{base}/crm/clients/{ctx.conversation_id}" if base.startswith("https://") else None


def _card_text(ctx: ToolContext, draft: LeadDraft, gym: Gym, *, changed: bool = False) -> str:
    """Карточка администратору. Текст один на оба языка — его читает сотрудник школы.

    Пустые поля не печатаются: «Мотив: —» на телефоне администратора — шум, из-за
    которого главное (кто, когда, куда) читается хуже.
    """
    lang = (draft.lang or ctx.lang).value
    text = render_card_text(
        ctx.kb,
        "lead_card.trial_changed" if changed else "lead_card.trial_booked",
        {
            "child": f"{draft.child_name}, {draft.child_age} лет"
            if draft.child_age
            else draft.child_name,
            "parent": draft.parent_name or _PLACEHOLDER,
            "phone": _readable_phone(draft.phone or draft.channel_user) or _PLACEHOLDER,
            "lang": _LANG_TITLES.get(lang, lang),
            "gym": f"{gym.title.ru} ({gym.address.ru})" if gym.address.ru else (gym.title.ru or gym.id),
            "when": draft.trial_slot_text or _PLACEHOLDER,
            "motivation": draft.motivation or _PLACEHOLDER,
            "objection": draft.main_objection or _PLACEHOLDER,
            "channel": _CHANNEL_TITLES.get(ctx.channel.value, ctx.channel.value),
            "dt": format_local_dt(ctx.now),
            "dialog": _dialog_link(ctx) or _PLACEHOLDER,
        },
    )
    return "\n".join(line for line in text.splitlines() if not line.rstrip().endswith(f": {_PLACEHOLDER}"))


def _lead_card_this_turn(ctx: ToolContext) -> ManagerCard | None:
    """Карточка записи, уже отправленная в этом ходу по этому диалогу."""
    cards = getattr(ctx.services, "cards", None) or []
    return next(
        (
            card for card in reversed(list(cards))
            if card.kind is ManagerCardKind.LEAD and card.conversation_id == ctx.conversation_id
        ),
        None,
    )


def _booking_key(gym_id: str | None, slot: datetime | None, child_name: str | None, child_age: int | None) -> tuple:
    """Что делает запись той же самой: зал, минута начала по времени школы, ребёнок.

    SQLite пояс не хранит: слот, записанный во времени школы, возвращается «19:00» без
    пояса. Поэтому сравнивается местное время школы, а не UTC — иначе та же запись
    выглядела бы переносом на пять часов и уходила бы второй карточкой.
    """
    from zoneinfo import ZoneInfo

    moment = slot
    if moment is not None and moment.tzinfo is not None:
        moment = moment.astimezone(ZoneInfo(_school_tz())).replace(tzinfo=None)
    minute = moment.replace(second=0, microsecond=0) if moment else None
    return gym_id, minute, (child_name or "").casefold(), child_age


# --------------------------------------------------------------------------- #
# Инструмент
# --------------------------------------------------------------------------- #
async def create_trial_lead(
    ctx: ToolContext,
    *,
    child_name: str,
    gym_id: str,
    child_age: int | None = None,
    parent_agreed: bool = False,
    child_gender: str = "unknown",
    preferred_time_text: str | None = None,
    session_time: str | None = None,
    session_day: str | None = None,
    discipline: str | None = None,
    no_surname: bool = False,
    parent_name: str | None = None,
    phone: str | None = None,
    motivation: str | None = None,
    main_objection: str | None = None,
    health_notes: str | None = None,
) -> ToolResult:
    """Фиксирует запись и отдаёт карточку администратору.

    Проверок согласия и кода need_consent НЕТ (SCOPE-OVERRIDE §1): телефон принимается сразу.
    Остаются как защита от галлюцинаций модели: валидация телефона PHONE_E164_KZ_RE,
    диапазон возраста 3..17, gym_id из KB.
    Идемпотентность: повторный вызов в пределах диалога ОБНОВЛЯЕТ лид, а не плодит новый.
    Транзакция: lead + outbox(карточка) атомарно. render_hint=SUMMARIZE.
    data: {lead_id, status: "trial_booked" | "needs_call", admin_notified: true}
    """
    kb = ctx.kb
    caveats: list[str] = []

    # Барьер согласия. Держится кодом, а не только промптом: модель, узнав имя и
    # возраст ребёнка, охотно «записывает» его сама — так и произошло на первом же
    # живом прогоне, родителя никто не спросил. Согласие — это ответ на прямой
    # вопрос «Записать на пробное?», а не факт, что данные названы.
    if not parent_agreed:
        return ToolResult.invalid_input(
            "родитель ещё не согласился на запись: сначала спроси «Записать на "
            "пробное занятие?» и вызывай этот инструмент только после явного «да»"
        )

    name = _clean_name(child_name)
    if name is None:
        return ToolResult.invalid_input(
            "child_name не похоже на имя ребёнка: спроси у родителя фамилию и имя ребёнка"
        )

    if child_age is not None and (isinstance(child_age, bool) or not isinstance(child_age, int)):
        return ToolResult.invalid_input("child_age обязан быть целым числом")
    # Возраст вне приёма решает человек — если его назвал сам родитель. Выдуманный
    # моделью возраст считается не названным: родителя просто спросят.
    if (
        child_age is not None
        and not (MIN_CHILD_AGE <= child_age <= MAX_CHILD_AGE)
        and client_named_age(child_age, ctx.client_texts)
    ):
        # Ни отказать, ни записать бот не вправе: решает человек.
        return ToolResult.needs_operator(
            say=kb.bilingual_text("gap.age_limits"),
            reason=EscalationReason.AGE_OUT_OF_RANGE,
            gap_ref=GapRef.G7,
        )

    gym: Gym | None = kb.gym(str(gym_id or "").strip())
    if gym is None:
        return ToolResult.invalid_input(f"в kb/gyms.yaml нет зала '{gym_id}'")
    if not gym.active or gym.status is not GymStatus.OPEN:
        return ToolResult.needs_operator(
            say=say_no_data(kb, GapRef.C3),
            reason=EscalationReason.NO_DATA,
            gap_ref=GapRef.C3,
        )

    # --- конкретное занятие -------------------------------------------------- #
    # Владелец 10.09.2026: «пускай бот полностью сам конвертирует — мы записали вас
    # на 19:00». Дату считает код по расписанию зала; модель её не называет.
    session: TrialSession | None = None
    choice: SessionChoice | None = None
    said_time: str | None = None
    if gym.schedule:
        # Время, день и секцию выбирает родитель, а не модель. Живой прогон
        # 10.09.2026: на «Сериков Ержан, 8 лет» модель сама подставила «бокс,
        # суббота 17:00» и отправила подтверждение — клиент ничего из этого не
        # называл. Аргумент, которого нет в словах клиента, считается не выбранным.
        texts = ctx.client_texts
        said_time = normalize_time(session_time) if client_named_time(session_time, texts) else None
        choice = resolve_trial_session(
            gym,
            time_text=said_time,
            day=session_day if client_named_day(session_day, texts) else None,
            discipline=discipline if client_named_discipline(discipline, texts) else None,
            now=ctx.now,
            tz_name=_school_tz(),
            min_lead_hours=float(kb.policies.trial_min_lead_hours),
        )
        session = choice.session

    # Чего не хватает для записи — спрашивается разом, одним сообщением. Не ошибка,
    # а следующий вопрос родителю. Варианты времени уходят данными, а не текстом
    # ошибки: иначе фильтр не признал бы время в «вам удобнее в 17:00 или 19:00?».
    needs: list[str] = []
    hints: list[str] = []
    # Имя и возраст — тоже слова родителя, а не догадка модели. Живой прогон
    # 10.09.2026: на «На бокс» бот спросил «фамилию сына» — ребёнка ещё никто не
    # называл. Имя, названное раньше и уже разобранное в заявку, тоже годится.
    known = ctx.lead_draft
    if child_age is None and known.child_age is not None:
        # «Айназаров Али, 8 лет» — возраст уже разобран из слов клиента, а модель его не
        # передала. Переспрашивать то, что родитель только что написал, нельзя.
        child_age = known.child_age
    name_said = client_named_name(name, ctx.client_texts) or (
        bool(known.child_name) and client_named_name(name, (known.child_name or "",))
    )
    age_said = child_age is not None and (
        client_named_age(child_age, ctx.client_texts) or known.child_age == child_age
    )
    if not name_said:
        needs.append("need_name")
    if not age_said:
        needs.append("need_age")
    if not (name_said and age_said):
        hints.append(_ask_child_hint(kb, ctx.lang, name=not name_said, age=not age_said))
    # Владелец 10.09.2026: «для записи спрашивать не только имя, а ФИ». Одно слово —
    # это имя без фамилии. Родитель не хочет её называть — записываем как есть.
    if name_said and len(name.split()) < 2 and not no_surname:
        needs.append("need_surname")
        if not hints:
            hints.append(_SURNAME_HINT)
    if choice is not None and session is None:
        needs.append(choice.problem or "need_time")
        if not hints:
            hints.append(_choice_hint(kb, choice))
    if needs:
        # Один вопрос за сообщение — правило промпта. Скриншот владельца 11.09.2026:
        # модель получила «спроси всё одним сообщением», спросила секцию, а возраст так
        # и не спросили. Подсказка — только к первому шагу, остальное следующим ходом.
        asked_now = {"need_name", "need_age"} if not (name_said and age_said) else {needs[0]}
        if any(need not in asked_now for need in needs):
            hints.append("Спроси только это. Остальное уточнишь следующим сообщением, по одному вопросу.")
        return ToolResult.success(
            data={
                "booked": False,
                "status": needs[0],
                "needs": needs,
                "gym_id": gym.id,
                "child_name": name if name_said else None,
                # Время, которое назвал сам клиент: «в 17:30 занятий нет» — не выдумка,
                # и фильтр не должен снимать такой ответ.
                "requested_time": said_time,
                "options": [
                    {
                        "discipline": slot.discipline,
                        "days": list(slot.days),
                        "time_start": slot.time_start,
                        "time_end": slot.time_end,
                    }
                    for slot in (choice.options if choice is not None and session is None else ())
                ],
            },
            render_hint=RenderHint.SUMMARIZE,
            caveats=hints,
        )

    # --- телефон ------------------------------------------------------------ #
    previous = ctx.lead_draft
    normalized = normalize_phone_kz(phone)
    phone_value = previous.phone
    phone_source = previous.phone_source
    if normalized is not None:
        phone_value = normalized
        phone_source = PhoneSource.TYPED
    elif phone:
        # Номер назвали, но он не разобрался. Лид создаётся без телефона:
        # выдуманный номер хуже отсутствующего.
        caveats.append(
            "Названный номер не разобрался — он не сохранён. Попроси родителя написать номер "
            "ещё раз в формате +7 7XX XXX XX XX либо скажи, что администратор ответит здесь же."
        )
    if phone_value is None and phone_source is not PhoneSource.NONE:
        phone_source = PhoneSource.NONE

    # --- черновик лида ------------------------------------------------------ #
    update: dict[str, Any] = {
        "conversation_id": ctx.conversation_id,
        "channel": ctx.channel,
        "lang": ctx.lang,
        "child_name": name,
        "child_age": child_age,
        "child_gender": _gender_of(child_gender),
        "gym_id": gym.id,
        "district": gym.district.ru or gym.settlement,
        "phone": phone_value,
        "phone_source": phone_source,
    }
    for field, value, limit in (
        ("parent_name", parent_name, MAX_NAME_CHARS),
        ("trial_slot_text", preferred_time_text, 120),
        ("motivation", motivation, 120),
        ("main_objection", main_objection, 120),
        ("health_notes", health_notes, 200),
    ):
        cleaned = _clean_text(value, limit)
        if cleaned is not None:
            update[field] = cleaned
    if not previous.channel_user and ctx.chat_id:
        update["channel_user"] = ctx.chat_id

    if session is not None:
        update["trial_slot"] = session.starts_at
        update["trial_slot_text"] = _slot_text(kb, session)

    draft = previous.model_copy(update=update)
    missing = _missing_required(draft)
    draft.status = LeadStatus.TRIAL_BOOKED if not missing else LeadStatus.NEEDS_CALL

    try:
        lead_id = await ctx.services.upsert_lead(draft)
    except Exception as exc:
        return ToolResult.failure(f"лид не сохранён: {type(exc).__name__}: {exc}")
    draft.lead_id = lead_id

    # --- карточка администратору ------------------------------------------- #
    # Карточка уходит на новую запись и на перенос. Повторный вызов с той же записью
    # второй карточки не шлёт: карточки теперь приходят администратору в WhatsApp, и
    # каждое уточнение модели превращалось бы в ещё одну «новую заявку». Черновик
    # хода о сохранённой заявке не знает — поэтому сверка идёт с ней.
    saved = ctx.saved_lead
    earlier_card = _lead_card_this_turn(ctx)
    if earlier_card is not None:
        # В этом же ходу запись уже ушла карточкой: сохранённой заявки ход ещё не видит.
        booked_before = True
        same_booking = earlier_card.text in (
            _card_text(ctx, draft, gym), _card_text(ctx, draft, gym, changed=True)
        )
    else:
        booked_before = saved is not None and saved.status == LeadStatus.TRIAL_BOOKED.value
        same_booking = booked_before and _booking_key(
            saved.gym_id, saved.trial_slot, saved.child_name, saved.child_age
        ) == _booking_key(draft.gym_id, draft.trial_slot, draft.child_name, draft.child_age)
    admin_notified = False
    if not same_booking:
        card = ManagerCard(
            kind=ManagerCardKind.LEAD,
            text=_card_text(ctx, draft, gym, changed=booked_before),
            conversation_id=ctx.conversation_id,
            lead_id=lead_id,
            lang=ctx.lang,
            urgency=Urgency.NORMAL,
        )
        try:
            await ctx.services.notify_manager(card)
            admin_notified = True
        except Exception as exc:
            # Лид уже сохранён — терять его из-за сбоя доставки карточки нельзя.
            caveats.append("Карточка администратору не ушла — сообщи, что с родителем свяжутся, и не обещай сроков.")
            admin_notified = False
            _ = exc
    else:
        caveats.append("Запись не изменилась — карточка администратору повторно не отправлялась.")

    confirmation_sent = False
    if session is not None and not missing:
        bring = _what_to_bring(kb, ctx.lang)
        await ctx.services.enqueue_outbound(
            OutboundMessage(
                conversation_id=ctx.conversation_id,
                channel_id=ctx.channel_id,
                channel=ctx.channel,
                chat_id=ctx.chat_id,
                lang=ctx.lang,
                kind=OutboundKind.LEAD_CONFIRMATION,
                text=render.render_trial_confirmation(
                    kb, child=draft.child_name, gym=gym, session=session, lang=ctx.lang, bring=bring
                ),
                artifact_id=TRIAL_CONFIRMATION_ARTIFACT,
            )
        )
        confirmation_sent = True
        caveats.append(
            "Клиент уже получил готовое подтверждение записи: секция, адрес, день, время, "
            "когда прийти и что взять с собой. Ничего от себя не добавляй."
        )
    elif missing:
        caveats.append(
            "Данных для полноценной записи не хватает (" + ", ".join(missing) + "): "
            "не обещай подтверждённое время, скажи, что администратор свяжется."
        )
    else:
        # У зала нет расписания — конкретное время подбирает человек.
        caveats.append("Конкретный день и час не называй: у этого зала нет расписания в базе.")
    if health_notes:
        caveats.append("Про здоровье ребёнка советов не давай — это вопрос к врачу и администратору.")

    return ToolResult.success(
        data={
            "lead_id": str(lead_id),
            "status": draft.status.value,
            "admin_notified": admin_notified,
            "created": saved is None,
            "child_name": draft.child_name,
            "child_age": draft.child_age,
            "gym_id": gym.id,
            "gym_title": gym.title.get(ctx.lang) or gym.title.ru,
            "gym_address": gym.address.get(ctx.lang),
            "phone_saved": draft.phone is not None,
            "phone_source": draft.phone_source.value,
            # Время — только из расписания школы. Эхо слов родителя сюда не кладётся:
            # фильтр доверяет времени из этих данных.
            "trial_time": draft.trial_slot_text if session is not None else None,
            "booked": True,
            "missing_fields": list(missing),
            "confirmation_sent": confirmation_sent,
        },
        render_hint=RenderHint.SILENT if confirmation_sent else RenderHint.SUMMARIZE,
        caveats=caveats,
        meta={"lead_id": str(lead_id), "gap_refs": [GapRef.G1.value, GapRef.G4.value]},
    )
