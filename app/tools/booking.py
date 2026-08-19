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
from app.kb.gaps import say_no_data
from app.kb.models import Gym
from app.types import (
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


def _card_text(ctx: ToolContext, draft: LeadDraft, gym: Gym) -> str:
    """Карточка администратору. Текст один на оба языка — его читает сотрудник школы."""
    return render_card_text(
        ctx.kb,
        "lead_card.trial_booked",
        {
            "child": f"{draft.child_name}, {draft.child_age} лет"
            if draft.child_age
            else draft.child_name,
            "parent": draft.parent_name or _PLACEHOLDER,
            "phone": draft.phone or draft.channel_user or _PLACEHOLDER,
            "lang": (draft.lang or ctx.lang).value,
            "gym": f"{gym.title.ru} ({gym.address.ru})" if gym.address.ru else (gym.title.ru or gym.id),
            "when": draft.trial_slot_text or _PLACEHOLDER,
            "motivation": draft.motivation or _PLACEHOLDER,
            "objection": draft.main_objection or _PLACEHOLDER,
            "channel": ctx.channel.value,
            "dt": format_local_dt(ctx.now),
        },
    )


# --------------------------------------------------------------------------- #
# Инструмент
# --------------------------------------------------------------------------- #
async def create_trial_lead(
    ctx: ToolContext,
    *,
    child_name: str,
    child_age: int,
    gym_id: str,
    parent_agreed: bool = False,
    child_gender: str = "unknown",
    preferred_time_text: str | None = None,
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
        return ToolResult.invalid_input("child_name не похоже на имя")

    if isinstance(child_age, bool) or not isinstance(child_age, int):
        return ToolResult.invalid_input("child_age обязан быть целым числом")
    if not (MIN_CHILD_AGE <= child_age <= MAX_CHILD_AGE):
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

    draft = previous.model_copy(update=update)
    missing = _missing_required(draft)
    draft.status = LeadStatus.TRIAL_BOOKED if not missing else LeadStatus.NEEDS_CALL

    existed = previous.lead_id is not None
    try:
        lead_id = await ctx.services.upsert_lead(draft)
    except Exception as exc:
        return ToolResult.failure(f"лид не сохранён: {type(exc).__name__}: {exc}")
    draft.lead_id = lead_id

    # --- карточка администратору ------------------------------------------- #
    admin_notified = False
    if not existed or previous.status is not draft.status:
        card = ManagerCard(
            kind=ManagerCardKind.LEAD,
            text=_card_text(ctx, draft, gym),
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
        caveats.append("Лид уже был создан в этом диалоге — карточка администратору повторно не отправлялась.")

    if missing:
        caveats.append(
            "Данных для полноценной записи не хватает (" + ", ".join(missing) + "): "
            "не обещай подтверждённое время, скажи, что администратор свяжется."
        )
    # G-1: расписания в базе нет, поэтому конкретное время не подтверждает никто, кроме человека.
    caveats.append("Конкретный день и час не называй: время пробного подтверждает администратор.")
    if health_notes:
        caveats.append("Про здоровье ребёнка советов не давай — это вопрос к врачу и администратору.")

    return ToolResult.success(
        data={
            "lead_id": str(lead_id),
            "status": draft.status.value,
            "admin_notified": admin_notified,
            "created": not existed,
            "child_name": draft.child_name,
            "child_age": draft.child_age,
            "gym_id": gym.id,
            "gym_title": gym.title.get(ctx.lang) or gym.title.ru,
            "gym_address": gym.address.get(ctx.lang),
            "phone_saved": draft.phone is not None,
            "phone_source": draft.phone_source.value,
            "preferred_time_text": draft.trial_slot_text,
            "missing_fields": list(missing),
        },
        render_hint=RenderHint.SUMMARIZE,
        caveats=caveats,
        meta={"lead_id": str(lead_id), "gap_refs": [GapRef.G1.value, GapRef.G4.value]},
    )
