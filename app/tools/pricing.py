"""Детерминированный калькулятор стоимости — инструмент ``calculate_price``.

Здесь нет ни модели, ни эвристик: только арифметика поверх ``kb/pricing.yaml``.
Причина жёсткости — конфликт C-2 из ``CONTENT-AUDIT.md``: одно и то же слово
«абонемент» стоит 25 000 ₸ в Костанае и 10 000 ₸ в райцентре, а механика
семейной скидки в городе (проценты по порядку детей) и в области (фиксированная
цена за ребёнка) — принципиально разная. Модель, которой позволили сложить это
самой, рано или поздно назовёт родителю не ту сумму.

Порядок расчёта зафиксирован явно и обязан оставаться таким:

1. берём базовую цену выбранного тарифа из KB;
2. для каждого ребёнка отдельно применяем процент скидки по его порядковому
   номеру (первый — 0 %);
3. округляем цену **каждого ребёнка** по ``rounding_mode`` / ``rounding_to``
   (по умолчанию half_up до 10 ₸) — округление идёт до суммирования, иначе
   разбивка по детям не сойдётся с итогом;
4. складываем округлённые цены детей в ``total``.

Все суммы — целые тенге (ст. 6 п. 4-1 Закона «О рекламе»: цена в тенге).
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any, Final

from app.kb.gaps import say_no_data
from app.kb.models import FamilyDiscount, KBSnapshot, PlanPrice
from app.types import (
    EscalationReason,
    GapRef,
    Plan,
    RenderHint,
    Scope,
    ToolContext,
    ToolResult,
)

#: Тарифы, для которых вообще существует семейная скидка города.
_SUBSCRIPTION_PLANS: Final[tuple[str, ...]] = (Plan.STANDARD.value, Plan.FLEXIBLE.value)

#: Сколько разовых считаем, если модель не передала количество.
_DEFAULT_SINGLE_SESSIONS: Final[int] = 1


# --------------------------------------------------------------------------- #
# Деньги
# --------------------------------------------------------------------------- #
def round_money(value: Decimal | int | float, *, mode: str = "half_up", step: int = 10) -> int:
    """Округляет сумму в тенге по правилу из ``kb/pricing.yaml``.

    ``mode='half_up'`` — обычное «0,5 вверх» с шагом ``step``;
    ``mode='floor'`` — всегда в пользу клиента вниз;
    ``mode='none'`` — только до целого тенге. Дробных тенге не бывает,
    поэтому функция всегда возвращает ``int``.
    """
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    rounding = ROUND_FLOOR if mode == "floor" else ROUND_HALF_UP
    if mode == "none" or step <= 1:
        return int(amount.quantize(Decimal(1), rounding=rounding))
    units = (amount / Decimal(step)).quantize(Decimal(1), rounding=rounding)
    return int(units * Decimal(step))


def apply_discount(base_price: int, percent: int, *, mode: str, step: int) -> int:
    """Цена одного ребёнка после процентной скидки, округлённая по правилу KB.

    При ``percent <= 0`` цена возвращается как есть: округлять неокруглённую
    базовую цену из прайса нельзя, иначе бот назовёт сумму, которой нет в прайсе.
    """
    if percent <= 0:
        return int(base_price)
    raw = Decimal(base_price) * Decimal(100 - percent) / Decimal(100)
    return round_money(raw, mode=mode, step=step)


# --------------------------------------------------------------------------- #
# Разбор аргументов
# --------------------------------------------------------------------------- #
def _scope_of(value: str | None) -> Scope | None:
    """``'city'`` / ``'region'`` -> :class:`Scope`; всё прочее -> ``None``."""
    if value is None:
        return None
    try:
        scope = Scope(str(value).strip().lower())
    except ValueError:
        return None
    return scope if scope in (Scope.CITY, Scope.REGION) else None


def _plan_of(value: str | None) -> Plan | None:
    """Строка тарифа -> :class:`Plan`; неизвестное значение -> ``None``."""
    if value is None:
        return None
    try:
        return Plan(str(value).strip().lower())
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Витрины (для plan=unknown и для неизвестной географии)
# --------------------------------------------------------------------------- #
def _plan_view(plan_key: str, price: PlanPrice) -> dict[str, Any]:
    """Одна строка витрины тарифа: цена, перерасчёт, подписи на двух языках."""
    view: dict[str, Any] = {
        "plan": plan_key,
        "price": price.price,
        "recalculation": price.recalculation,
        "label_ru": price.label.ru,
        "label_kk": price.label.kk,
    }
    if price.note is not None and price.note.filled:
        view["note_ru"] = price.note.ru
        view["note_kk"] = price.note.kk
    return view


def _city_showcase(kb: KBSnapshot) -> dict[str, Any]:
    """Городская витрина целиком: оба абонемента, разовая, семейная скидка."""
    pricing = kb.pricing
    plans = [
        _plan_view(key, value)
        for key, value in ((k, pricing.city_plans.get(k)) for k in _SUBSCRIPTION_PLANS)
        if value is not None
    ]
    showcase: dict[str, Any] = {
        "settlement": pricing.city_settlement,
        "sessions_included": pricing.city_sessions,
        "validity_days": pricing.city_validity_days,
        "validity_note_ru": pricing.city_validity_note.ru,
        "validity_note_kk": pricing.city_validity_note.kk,
        "plans": plans,
    }
    if pricing.city_single is not None:
        showcase["single"] = _plan_view(Plan.SINGLE.value, pricing.city_single)
    discount = pricing.city_family_discount
    showcase["family_discount"] = {
        "type": discount.type,
        "rules": [{"child_index": index, "percent": percent} for index, percent in discount.rules],
        "applies_to": list(discount.applies_to),
        "max_children": discount.max_children,
        "label_ru": discount.label.ru,
        "label_kk": discount.label.kk,
    }
    return showcase


def _region_showcase(kb: KBSnapshot) -> dict[str, Any]:
    """Витрина райцентров: абонемент и фиксированный семейный тариф."""
    pricing = kb.pricing
    plans = [
        _plan_view(key, value)
        for key, value in ((k, pricing.region_plans.get(k)) for k in _SUBSCRIPTION_PLANS)
        if value is not None
    ]
    showcase: dict[str, Any] = {
        "settlements": list(pricing.region_settlements),
        "plans": plans,
        "family": None,
    }
    if pricing.region_single is not None:
        showcase["single"] = _plan_view(Plan.SINGLE.value, pricing.region_single)
    if pricing.region_family_price_per_child is not None:
        showcase["family"] = {
            "type": "fixed_per_child",
            "price_per_child": pricing.region_family_price_per_child,
            "min_children": pricing.region_family_min_children,
            "label_ru": pricing.region_family_label.ru,
            "label_kk": pricing.region_family_label.kk,
        }
    return showcase


def _compare_single(kb: KBSnapshot, *, children_count: int, total: int | None) -> dict[str, Any] | None:
    """Готовое сравнение абонемента с разовыми: ``12 x 3 200`` против абонемента.

    Считается только там, где в KB есть цена разовой (сегодня — только город).
    """
    pricing = kb.pricing
    single = pricing.city_single
    if single is None:
        return None
    sessions = pricing.city_sessions
    twelve = single.price * sessions
    compare: dict[str, Any] = {
        "single_price": single.price,
        "sessions_compared": sessions,
        "twelve_singles": twelve,
        "twelve_singles_total": twelve * children_count,
        "formula": f"{single.price} x {sessions} = {twelve}",
    }
    if total is not None:
        saving = twelve * children_count - total
        compare["saving_vs_single"] = saving
        compare["saving_pct"] = (
            int(round_money(Decimal(saving) * 100 / Decimal(twelve * children_count), mode="none", step=1))
            if twelve * children_count > 0
            else 0
        )
    else:
        compare["saving_vs_single"] = None
        compare["saving_pct"] = None
    return compare


def _derived_facts(kb: KBSnapshot) -> list[dict[str, str]]:
    """Производные аргументы («абонемент выгоднее разовых») — только если разрешены."""
    if not kb.pricing.derived_enabled:
        return []
    return [{"id": fact.id, "ru": fact.ru, "kk": fact.kk} for fact in kb.pricing.derived_facts]


# --------------------------------------------------------------------------- #
# Город
# --------------------------------------------------------------------------- #
def _city_per_child(
    *,
    base_price: int,
    children_count: int,
    discount: FamilyDiscount,
    discount_allowed: bool,
    mode: str,
    step: int,
) -> list[dict[str, int]]:
    """Разбивка по детям: индекс, процент, цена. Скидка — по порядку зачисления."""
    rows: list[dict[str, int]] = []
    for index in range(1, children_count + 1):
        percent = discount.percent_for(index) if discount_allowed else 0
        rows.append(
            {
                "index": index,
                "discount_pct": percent,
                "price": apply_discount(base_price, percent, mode=mode, step=step),
            }
        )
    return rows


def _city_single_total(kb: KBSnapshot, *, children_count: int, sessions: int) -> tuple[list[dict[str, int]], int]:
    """Разовые тренировки: скидки не применяются вообще (правила в прайсе нет)."""
    single = kb.pricing.city_single
    assert single is not None  # проверено вызывающим
    per_one = single.price * sessions
    rows = [{"index": index, "discount_pct": 0, "price": per_one} for index in range(1, children_count + 1)]
    return rows, per_one * children_count


# --------------------------------------------------------------------------- #
# Инструмент
# --------------------------------------------------------------------------- #
async def calculate_price(
    ctx: ToolContext,
    *,
    scope: str,
    plan: str,
    children_count: int,
    single_sessions: int | None = None,
) -> ToolResult:
    """Чистая функция поверх pricing.yaml. render_hint=NUMBERS_ONLY.

    Возвращает не только итог, но и разбивку по детям и готовое сравнение с
    разовыми тренировками — чтобы модель не пересчитывала ничего сама.
    """
    kb = ctx.kb
    pricing = kb.pricing
    mode = pricing.rounding_mode
    step = pricing.rounding_to

    if not isinstance(children_count, int) or children_count < 1:
        return ToolResult.invalid_input("children_count обязан быть целым числом не меньше 1")

    parsed_scope = _scope_of(scope)
    parsed_plan = _plan_of(plan)
    if parsed_plan is None:
        return ToolResult.invalid_input(f"неизвестный тариф '{plan}'")

    # --- география не определена: считать нельзя, показываем обе витрины ---- #
    if parsed_scope is None:
        return ToolResult.no_data(
            say=say_no_data(kb, GapRef.C4),
            data={
                "status": "need_scope",
                "currency": pricing.currency,
                "reason": "не определено: Костанай или райцентр области",
                "city": _city_showcase(kb),
                "region": _region_showcase(kb),
            },
        )

    if parsed_scope is Scope.CITY:
        return _calc_city(
            kb,
            plan=parsed_plan,
            children_count=children_count,
            single_sessions=single_sessions,
            mode=mode,
            step=step,
        )
    return _calc_region(kb, plan=parsed_plan, children_count=children_count, mode=mode, step=step)


def _calc_city(
    kb: KBSnapshot,
    *,
    plan: Plan,
    children_count: int,
    single_sessions: int | None,
    mode: str,
    step: int,
) -> ToolResult:
    """Костанай: проценты по порядку детей, потолок ``max_children``."""
    pricing = kb.pricing
    discount = pricing.city_family_discount
    caveats: list[str] = []
    meta: dict[str, Any] = {"pricing_scope": Scope.CITY.value, "rounding": f"{mode}/{step}"}

    base: dict[str, Any] = {
        "currency": pricing.currency,
        "scope": Scope.CITY.value,
        "settlement": pricing.city_settlement,
        "plan": plan.value,
        "children_count": children_count,
        "sessions_included": pricing.city_sessions,
        "validity_days": pricing.city_validity_days,
        "validity_note_ru": pricing.city_validity_note.ru,
        "validity_note_kk": pricing.city_validity_note.kk,
    }

    # --- разовые ------------------------------------------------------------ #
    if plan is Plan.SINGLE:
        if pricing.city_single is None:
            return ToolResult.no_data(say=say_no_data(kb, GapRef.G10), data={"status": "no_single_price"})
        sessions = single_sessions if isinstance(single_sessions, int) and single_sessions > 0 else None
        if sessions is None:
            sessions = _DEFAULT_SINGLE_SESSIONS
            caveats.append(
                "Количество разовых тренировок не названо — посчитано за одну. "
                "Уточни у родителя, сколько занятий он планирует."
            )
        rows, total = _city_single_total(kb, children_count=children_count, sessions=sessions)
        data = dict(base)
        data.update(
            {
                "recalculation": pricing.city_single.recalculation,
                "plan_label_ru": pricing.city_single.label.ru,
                "plan_label_kk": pricing.city_single.label.kk,
                "single_sessions": sessions,
                "single_price": pricing.city_single.price,
                "per_child": rows,
                "total": total,
                "price_per_session": pricing.city_single.price,
                "discount_total": 0,
                "compare_single": _compare_single(kb, children_count=children_count, total=None),
                "derived_facts": _derived_facts(kb),
            }
        )
        caveats.append("Семейная скидка на разовые тренировки в прайсе не предусмотрена — она не применялась.")
        return ToolResult.success(data=data, render_hint=RenderHint.NUMBERS_ONLY, caveats=caveats, meta=meta)

    # --- потолок семейной скидки ------------------------------------------- #
    # Проверка стоит ДО ветвления по тарифу: правила для четвёртого ребёнка в
    # прайсе нет, и «тариф пока не выбран» этого не меняет. Иначе один и тот же
    # вопрос («сколько за четверых?») получал бы отказ при явном тарифе и
    # выдуманную сумму — четвёртый ребёнок по полной цене — при plan=unknown.
    if discount.max_children is not None and children_count > discount.max_children:
        return ToolResult.needs_operator(
            say=say_no_data(kb, GapRef.C4),
            reason=EscalationReason.PRICE_OFF_LIST,
            gap_ref=GapRef.C4,
        )

    # --- тариф не выбран: обе витрины, выбор за клиентом -------------------- #
    if plan is Plan.UNKNOWN:
        options: list[dict[str, Any]] = []
        for key in _SUBSCRIPTION_PLANS:
            price = pricing.city_plans.get(key)
            if price is None:
                continue
            allowed = key in discount.applies_to
            rows = _city_per_child(
                base_price=price.price,
                children_count=children_count,
                discount=discount,
                discount_allowed=allowed,
                mode=mode,
                step=step,
            )
            option = _plan_view(key, price)
            option.update(
                {
                    "per_child": rows,
                    "total": sum(row["price"] for row in rows),
                    "discount_applies": allowed,
                }
            )
            options.append(option)
        data = dict(base)
        data.update(
            {
                "recalculation": None,
                "per_child": [],
                "total": None,
                "price_per_session": None,
                "options": options,
                "single": _plan_view(Plan.SINGLE.value, pricing.city_single)
                if pricing.city_single is not None
                else None,
                "family_discount": _city_showcase(kb)["family_discount"],
                "compare_single": _compare_single(kb, children_count=children_count, total=None),
                "derived_facts": _derived_facts(kb),
            }
        )
        caveats.append(
            "Тариф не выбран: назови оба варианта и разницу между ними, выбор оставь родителю."
        )
        caveats.extend(_conflict_caveats(kb, plan=None, children_count=children_count))
        return ToolResult.success(data=data, render_hint=RenderHint.NUMBERS_ONLY, caveats=caveats, meta=meta)

    # --- обычный абонемент -------------------------------------------------- #
    price = pricing.city_plans.get(plan.value)
    if price is None:
        return ToolResult.no_data(say=say_no_data(kb, GapRef.G10), data={"status": "no_plan", "plan": plan.value})

    discount_allowed = plan.value in discount.applies_to
    rows = _city_per_child(
        base_price=price.price,
        children_count=children_count,
        discount=discount,
        discount_allowed=discount_allowed,
        mode=mode,
        step=step,
    )
    total = sum(row["price"] for row in rows)
    full_price = price.price * children_count
    sessions_total = pricing.city_sessions * children_count

    data = dict(base)
    data.update(
        {
            "recalculation": price.recalculation,
            "plan_label_ru": price.label.ru,
            "plan_label_kk": price.label.kk,
            "plan_note_ru": price.note.ru if price.note is not None else None,
            "plan_note_kk": price.note.kk if price.note is not None else None,
            "price_per_child_base": price.price,
            "per_child": rows,
            "total": total,
            "discount_total": full_price - total,
            "price_per_session": round_money(
                Decimal(total) / Decimal(sessions_total), mode="none", step=1
            ),
            "family_discount_applied": discount_allowed and children_count > 1,
            "family_discount_label_ru": discount.label.ru,
            "family_discount_label_kk": discount.label.kk,
            "compare_single": _compare_single(kb, children_count=children_count, total=total),
            "derived_facts": _derived_facts(kb),
        }
    )
    if price.note is not None and price.note.filled and not price.recalculation:
        # Конфликт C-6: про отсутствие перерасчёта родитель обязан узнать ДО оплаты.
        caveats.append("Обязательно произнеси оговорку тарифа (plan_note) до того, как родитель решит платить.")
    if not discount_allowed and children_count > 1:
        caveats.append(
            "Семейная скидка на этот тариф в базе не отмечена — расчёт без скидки, "
            "условие по скидке подтвердит администратор."
        )
    caveats.extend(_conflict_caveats(kb, plan=plan, children_count=children_count))
    meta["c5_base_rule"] = discount.base_rule
    meta["c5_base_rule_status"] = discount.base_rule_status
    meta["mixed_plans_needs_operator"] = children_count > 1
    return ToolResult.success(data=data, render_hint=RenderHint.NUMBERS_ONLY, caveats=caveats, meta=meta)


def _conflict_caveats(kb: KBSnapshot, *, plan: Plan | None, children_count: int) -> list[str]:
    """Оговорки по нерешённым конфликтам C-4 и C-5 (KB-SPEC §2.3)."""
    discount = kb.pricing.city_family_discount
    caveats: list[str] = []
    flexible = Plan.FLEXIBLE.value
    touches_flexible = plan is Plan.FLEXIBLE or (plan is None and flexible in kb.pricing.city_plans)
    if (
        children_count > 1
        and touches_flexible
        and flexible in discount.applies_to
        and discount.applies_to_status != "confirmed"
    ):
        caveats.append(
            "Скидка для второго и третьего ребёнка на гибкий тариф владельцем письменно "
            "не подтверждена: скажи прямо, что точное условие подтвердит администратор."
        )
    if children_count > 1 and discount.base_rule_status != "confirmed":
        caveats.append(
            "Если дети записываются на разные тарифы — расчёт не делай, передай администратору: "
            "правило, от какого тарифа считается скидка, не закреплено."
        )
    return caveats


def _calc_region(
    kb: KBSnapshot,
    *,
    plan: Plan,
    children_count: int,
    mode: str,
    step: int,
) -> ToolResult:
    """Райцентры: фиксированная цена за ребёнка, а не процент (конфликт C-2)."""
    pricing = kb.pricing
    caveats: list[str] = []
    meta: dict[str, Any] = {"pricing_scope": Scope.REGION.value, "rounding": f"{mode}/{step}"}

    standard = pricing.region_plans.get(Plan.STANDARD.value)
    family_price = pricing.region_family_price_per_child
    min_children = pricing.region_family_min_children

    # G-10: гибкого тарифа и разовых в райцентрах в базе нет.
    if plan in (Plan.FLEXIBLE, Plan.SINGLE):
        available = (
            pricing.region_plans.get(plan.value)
            if plan is Plan.FLEXIBLE
            else pricing.region_single
        )
        if available is None:
            return ToolResult.no_data(
                say=say_no_data(kb, GapRef.G10),
                gap_ref=GapRef.G10,
                data={
                    "status": "no_data",
                    "scope": Scope.REGION.value,
                    "plan": plan.value,
                    "available": _region_showcase(kb),
                },
            )

    if standard is None:
        return ToolResult.no_data(
            say=say_no_data(kb, GapRef.G10), gap_ref=GapRef.G10, data={"status": "no_plan"}
        )

    base: dict[str, Any] = {
        "currency": pricing.currency,
        "scope": Scope.REGION.value,
        "settlements": list(pricing.region_settlements),
        "plan": Plan.STANDARD.value if plan is Plan.UNKNOWN else plan.value,
        "children_count": children_count,
        "sessions_included": None,  # G-1/G-10: число занятий по райцентрам не передано
        "validity_days": None,
        "recalculation": standard.recalculation,
        "plan_label_ru": standard.label.ru,
        "plan_label_kk": standard.label.kk,
        "plan_note_ru": standard.note.ru if standard.note is not None else None,
        "plan_note_kk": standard.note.kk if standard.note is not None else None,
    }

    if family_price is not None and children_count >= min_children:
        rows = [
            {"index": index, "discount_pct": 0, "price": family_price}
            for index in range(1, children_count + 1)
        ]
        total = family_price * children_count
        base.update(
            {
                "tariff_applied": "family_fixed",
                "price_per_child_base": standard.price,
                "family_price_per_child": family_price,
                "family_min_children": min_children,
                "family_label_ru": pricing.region_family_label.ru,
                "family_label_kk": pricing.region_family_label.kk,
                "per_child": rows,
                "total": total,
                "discount_total": standard.price * children_count - total,
            }
        )
        caveats.append(
            "В райцентрах семейная цена — фиксированная сумма за каждого ребёнка, "
            "а не процент: не пересказывай её как скидку в процентах."
        )
    else:
        rows = [
            {"index": index, "discount_pct": 0, "price": standard.price}
            for index in range(1, children_count + 1)
        ]
        total = standard.price * children_count
        base.update(
            {
                "tariff_applied": "standard",
                "price_per_child_base": standard.price,
                "family_price_per_child": family_price,
                "family_min_children": min_children,
                "family_label_ru": pricing.region_family_label.ru,
                "family_label_kk": pricing.region_family_label.kk,
                "per_child": rows,
                "total": total,
                "discount_total": 0,
            }
        )

    base["price_per_session"] = None  # число занятий по райцентрам не подтверждено
    base["compare_single"] = None     # G-10: цены разовой в райцентрах нет
    base["derived_facts"] = []        # производные аргументы посчитаны от городского прайса
    if plan is Plan.UNKNOWN:
        caveats.append(
            "По райцентрам подтверждён только абонемент: про гибкий тариф и разовые "
            "честно скажи, что уточнит администратор."
        )
    caveats.append(
        "Городские цены к райцентру не применяй: это другой прайс, разница более чем вдвое."
    )
    meta["gap_g10"] = pricing.region_plans.get(Plan.FLEXIBLE.value) is None
    return ToolResult.success(data=base, render_hint=RenderHint.NUMBERS_ONLY, caveats=caveats, meta=meta)


__all__ = ["apply_discount", "calculate_price", "round_money"]
