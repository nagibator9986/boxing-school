"""Расписание зала — инструмент ``get_schedule``.

Расписания в базе знаний сегодня нет ни по одному залу (пробел G-1) — и это
самый дорогой пробел проекта: отдельный хайлайт «ГРАФИК» в Instagram означает,
что про время спрашивает почти каждый второй.

Функция существует **именно поэтому**. Она не «заглушка на будущее»: пока
``gyms[].schedule`` пуст, она возвращает машиночитаемый ``no_data`` с
``gap_ref=G-1`` и готовой фразой на двух языках, а пост-фильтр
(``app/core/postcheck.py``) на этом основании вырезает из ответа модели любое
``HH:MM``. Без вызова инструмента модель обязательно придумает «вторник и
четверг, 18:00» — это ровно тот случай, когда галлюцинация приводит родителя с
ребёнком к закрытой двери.

Ни строчки кода менять не придётся, когда владелец заполнит расписание: как
только у зала появится хоть один слот, тот же код начнёт отдавать реальные
данные с фильтрацией по возрасту и школьной смене.
"""

from __future__ import annotations

from typing import Any, Final

from app.kb.gaps import say_no_data
from app.kb.models import Gym, ScheduleSlot
from app.types import (
    EscalationReason,
    GapRef,
    GymStatus,
    Language,
    RenderHint,
    ToolContext,
    ToolResult,
)

#: Порядок дней недели для стабильной сортировки слотов.
_WEEKDAY_ORDER: Final[dict[str, int]] = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

#: Допустимые значения аргумента ``shift``.
_SHIFTS: Final[tuple[str, ...]] = ("first", "second", "unknown")


def _slot_payload(slot: ScheduleSlot, lang: Language) -> dict[str, Any]:
    """Слот расписания в том виде, в каком его увидит модель."""
    return {
        "days": list(slot.days),
        "discipline": slot.discipline,
        "time_start": slot.time_start,
        "time_end": slot.time_end,
        "age_from": slot.age_from,
        "age_to": slot.age_to,
        # Явный признак для модели: возрастных групп в данных нет, обещать
        # «эта группа как раз для вашего ребёнка» нельзя.
        "age_known": slot.age_known,
        "shift": slot.shift,
        "note": slot.note.get(lang) if slot.note is not None else None,
    }


def _sort_key(slot: ScheduleSlot) -> tuple[int, str, int]:
    return (_WEEKDAY_ORDER.get(slot.days[0], 9), slot.time_start, slot.age_from)


def _matches(slot: ScheduleSlot, *, child_age: int | None, shift: str) -> bool:
    """Подходит ли слот ребёнку заданного возраста и школьной смены.

    Слот с НЕИЗВЕСТНЫМ возрастом по возрасту не отсеивается. В расписании от
    владельца возрастных групп нет, и если такие слоты выкидывать, бот на вопрос
    «есть группа для семилетки?» ответит «ничего нет» при полном расписании —
    это хуже, чем честное «время такое, возраст уточнит администратор».
    """
    if child_age is not None and slot.age_known:
        if not (slot.age_from <= child_age <= slot.age_to):
            return False
    if shift in ("first", "second") and slot.shift not in ("any", shift):
        return False
    return True


async def get_schedule(
    ctx: ToolContext,
    *,
    gym_id: str,
    child_age: int | None = None,
    shift: str = "unknown",
) -> ToolResult:
    """Пока gyms[].schedule == [] возвращает no_data с gap_ref=G-1 и готовой фразой на RU/KK.
    Существует именно для того, чтобы модель не выдумывала время."""
    kb = ctx.kb
    lang = ctx.lang

    gym: Gym | None = kb.gym(gym_id)
    if gym is None:
        return ToolResult.invalid_input(f"в kb/gyms.yaml нет зала '{gym_id}'")

    # Нерешённый конфликт C-3: про такой зал вообще ничего утверждать нельзя.
    if gym.status is GymStatus.UNRESOLVED:
        return ToolResult.needs_operator(
            say=say_no_data(kb, GapRef.C3),
            reason=EscalationReason.NO_DATA,
            gap_ref=GapRef.C3,
        )
    if not gym.active or gym.status is not GymStatus.OPEN:
        return ToolResult.needs_operator(
            say=say_no_data(kb, GapRef.G1),
            reason=EscalationReason.NO_DATA,
            gap_ref=GapRef.G1,
        )

    normalized_shift = shift if shift in _SHIFTS else "unknown"
    age: int | None
    if isinstance(child_age, int) and 3 <= child_age <= 60:
        age = child_age
    else:
        age = None

    # ------------------------------------------------------------------ #
    # G-1: данных нет. Сюда попадает каждый вызов, пока владелец не заполнил
    # расписание. `status: no_data` в data — то, на что смотрит пост-фильтр.
    # ------------------------------------------------------------------ #
    if not gym.schedule:
        return ToolResult.no_data(
            say=say_no_data(kb, GapRef.G1),
            gap_ref=GapRef.G1,
            data={
                "status": "no_data",
                "gym_id": gym.id,
                "gym_title": gym.title.get(lang) or gym.title.ru,
                "address": gym.address.get(lang),
                "landmark": gym.landmark.get(lang),
                "child_age": age,
                "shift": normalized_shift,
                "slots": [],
                "render": "escalate_or_offer_trial",
                "next_step_ru": (
                    "Время занятий не называть. Предложить записать на бесплатное пробное "
                    "и передать вопрос о расписании администратору."
                ),
            },
        )

    # ------------------------------------------------------------------ #
    # Данные появились — тот же код отдаёт их без единой правки.
    # ------------------------------------------------------------------ #
    all_slots = sorted(gym.schedule, key=_sort_key)
    matched = [slot for slot in all_slots if _matches(slot, child_age=age, shift=normalized_shift)]
    caveats: list[str] = []
    status = "ok"
    if not matched:
        status = "no_match"
        caveats.append(
            "Под этот возраст и смену подходящей группы в расписании нет. Не подгоняй "
            "соседнюю: назови существующие варианты из alternatives или передай администратору."
        )
    caveats.append("Дни и время бери только из этих данных, ничего не добавляя от себя.")

    return ToolResult.success(
        data={
            "status": status,
            "gym_id": gym.id,
            "gym_title": gym.title.get(lang) or gym.title.ru,
            "address": gym.address.get(lang),
            "landmark": gym.landmark.get(lang),
            "child_age": age,
            "shift": normalized_shift,
            "slots": [_slot_payload(slot, lang) for slot in matched],
            "alternatives": [_slot_payload(slot, lang) for slot in all_slots] if not matched else [],
            "total": len(matched),
            "total_in_gym": len(all_slots),
        },
        render_hint=RenderHint.VERBATIM,
        caveats=caveats,
        meta={"gap_refs": [] if matched else [GapRef.G1.value]},
    )


__all__ = ["get_schedule"]
