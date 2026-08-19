"""Детерминированный калькулятор ``calculate_price``.

Ожидаемые суммы посчитаны РУКАМИ по ``docs/CONTENT-AUDIT.md`` §3 (строки 85-108),
а не сняты с текущей реализации:

Костанай — стандартный 25 000 ₸, гибкий 30 000 ₸, разовая 3 200 ₸;
второй ребёнок −10 %, третий −15 %, округление half_up до 10 ₸.

* стандартный: 1 реб. 25 000; 2 реб. 25 000 + 22 500 = 47 500;
  3 реб. 25 000 + 22 500 + 21 250 = 68 750;
* гибкий: 1 реб. 30 000; 2 реб. 30 000 + 27 000 = 57 000;
  3 реб. 30 000 + 27 000 + 25 500 = 82 500.

Райцентры — стандартный 10 000 ₸, семейный 8 000 ₸ **за каждого** ребёнка при
двух и более: 1 реб. 10 000; 2 реб. 16 000; 3 реб. 24 000.

Цена ошибки здесь максимальная: названная не та сумма — это либо потерянный
лид, либо спор с родителем на кассе.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.tools.pricing import apply_discount, calculate_price, round_money
from app.types import GapRef, Plan, Scope, ToolStatus

CITY_STANDARD = 25_000
CITY_FLEXIBLE = 30_000
CITY_SINGLE = 3_200
REGION_STANDARD = 10_000
REGION_FAMILY = 8_000


# --------------------------------------------------------------------------- #
# Костанай
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("children", "expected_total", "expected_rows"),
    [
        (1, 25_000, [25_000]),
        (2, 47_500, [25_000, 22_500]),
        (3, 68_750, [25_000, 22_500, 21_250]),
    ],
)
async def test_city_standard_totals(ctx, children, expected_total, expected_rows) -> None:
    """Городской стандартный тариф: суммы посчитаны по прайсу вручную."""
    result = await calculate_price(
        ctx, scope="city", plan="standard", children_count=children
    )

    assert result.ok
    assert result.data["total"] == expected_total
    assert [row["price"] for row in result.data["per_child"]] == expected_rows
    assert result.data["currency"] == "KZT"
    assert result.data["sessions_included"] == 12


@pytest.mark.parametrize(
    ("children", "expected_total", "expected_rows"),
    [
        (1, 30_000, [30_000]),
        (2, 57_000, [30_000, 27_000]),
        (3, 82_500, [30_000, 27_000, 25_500]),
    ],
)
async def test_city_flexible_totals(ctx, children, expected_total, expected_rows) -> None:
    """Городской гибкий тариф. Скидка применяется к обоим тарифам (конфликт C-4)."""
    result = await calculate_price(
        ctx, scope="city", plan="flexible", children_count=children
    )

    assert result.ok
    assert result.data["total"] == expected_total
    assert [row["price"] for row in result.data["per_child"]] == expected_rows


async def test_city_discount_order_is_by_enrollment(ctx) -> None:
    """Скидка идёт по порядку зачисления: 0 % → 10 % → 15 %, а не наоборот."""
    result = await calculate_price(ctx, scope="city", plan="standard", children_count=3)

    rows = result.data["per_child"]
    assert [row["index"] for row in rows] == [1, 2, 3]
    assert [row["discount_pct"] for row in rows] == [0, 10, 15]
    # Итог обязан быть суммой ОКРУГЛЁННЫХ цен детей, иначе разбивка не сойдётся.
    assert result.data["total"] == sum(row["price"] for row in rows)
    assert result.data["discount_total"] == CITY_STANDARD * 3 - result.data["total"]


async def test_city_single_ignores_family_discount(ctx) -> None:
    """Разовые: 3 200 ₸ за занятие, семейной скидки на них в прайсе нет."""
    result = await calculate_price(
        ctx, scope="city", plan="single", children_count=2, single_sessions=12
    )

    assert result.ok
    assert result.data["total"] == CITY_SINGLE * 12 * 2
    assert all(row["discount_pct"] == 0 for row in result.data["per_child"])
    assert any("скидк" in caveat.lower() for caveat in result.caveats)


async def test_city_single_without_sessions_counts_one_and_warns(ctx) -> None:
    """Количество разовых не названо — считаем одну и обязаны предупредить."""
    result = await calculate_price(ctx, scope="city", plan="single", children_count=1)

    assert result.ok
    assert result.data["single_sessions"] == 1
    assert result.data["total"] == CITY_SINGLE
    assert any("не назван" in caveat.lower() for caveat in result.caveats)


async def test_city_compare_single_matches_content_audit(ctx) -> None:
    """Готовое сравнение с разовыми: 12 × 3 200 = 38 400, экономия 13 400 ₸ (35 %)."""
    result = await calculate_price(ctx, scope="city", plan="standard", children_count=1)

    compare = result.data["compare_single"]
    assert compare["twelve_singles"] == 38_400
    assert compare["saving_vs_single"] == 13_400
    assert compare["saving_pct"] == 35
    # 25 000 / 12 = 2 083 ₸ за занятие — тот же аргумент, что в CONTENT-AUDIT §3.3.
    assert result.data["price_per_session"] == 2_083


# --------------------------------------------------------------------------- #
# Границы количества детей
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("plan", ["standard", "flexible", "unknown"])
@pytest.mark.parametrize("children", [4, 5, 10])
async def test_four_or_more_children_never_invents_a_number(ctx, plan, children) -> None:
    """Правила для четвёртого ребёнка в прайсе НЕТ — обязан быть отказ, а не сумма.

    Это главный сценарий выдумывания: посчитать четвёртого «как первого» —
    значит назвать родителю сумму, которую школа не обещала. Ответ обязан
    уходить администратору при любом тарифе, включая невыбранный.
    """
    result = await calculate_price(
        ctx, scope="city", plan=plan, children_count=children
    )

    assert result.ok is False
    assert result.status is ToolStatus.NEEDS_OPERATOR
    assert result.gap_ref is GapRef.C4
    assert result.say_if_no_data is not None
    assert set(result.say_if_no_data) == {"ru", "kk"}
    # Никакой посчитанной суммы наружу уйти не должно.
    assert not result.data.get("total")
    assert not result.data.get("options")


@pytest.mark.parametrize("children", [0, -1, -5])
async def test_non_positive_children_is_invalid_input(ctx, children) -> None:
    """Ноль и отрицательное количество детей — ошибка аргументов, а не расчёт."""
    result = await calculate_price(
        ctx, scope="city", plan="standard", children_count=children
    )

    assert result.ok is False
    assert result.status is ToolStatus.INVALID_INPUT
    assert result.error


@pytest.mark.parametrize("children", [1.5, 2.0, "2", None, [2]])
async def test_fractional_and_wrong_typed_children_rejected(ctx, children) -> None:
    """Дробное «1.5 ребёнка» и строка вместо числа не должны считаться."""
    result = await calculate_price(
        ctx, scope="city", plan="standard", children_count=children
    )

    assert result.ok is False
    assert result.status is ToolStatus.INVALID_INPUT


# --------------------------------------------------------------------------- #
# Райцентры
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("children", "expected_total", "tariff"),
    [
        (1, 10_000, "standard"),
        (2, 16_000, "family_fixed"),
        (3, 24_000, "family_fixed"),
    ],
)
async def test_region_family_price_is_fixed_not_percent(
    ctx, children, expected_total, tariff
) -> None:
    """Райцентры: 8 000 ₸ за КАЖДОГО ребёнка при двух и более (конфликт C-2)."""
    result = await calculate_price(
        ctx, scope="region", plan="standard", children_count=children
    )

    assert result.ok
    assert result.data["total"] == expected_total
    assert result.data["tariff_applied"] == tariff
    assert all(row["discount_pct"] == 0 for row in result.data["per_child"])
    if children > 1:
        assert all(row["price"] == REGION_FAMILY for row in result.data["per_child"])


async def test_region_never_uses_city_price(ctx) -> None:
    """Городская цена в райцентре — потерянный лид: разница более чем вдвое."""
    result = await calculate_price(ctx, scope="region", plan="standard", children_count=1)

    assert result.data["total"] == REGION_STANDARD
    assert result.data["total"] != CITY_STANDARD
    assert any("город" in caveat.lower() for caveat in result.caveats)


@pytest.mark.parametrize("plan", ["flexible", "single"])
async def test_region_missing_plans_say_no_data(ctx, plan) -> None:
    """Гибкого тарифа и разовых в райцентрах в базе нет (G-10) — не выдумывать."""
    result = await calculate_price(ctx, scope="region", plan=plan, children_count=1)

    assert result.ok is False
    assert result.status is ToolStatus.NO_DATA
    assert result.gap_ref is GapRef.G10
    assert set(result.say_if_no_data or {}) == {"ru", "kk"}
    assert "total" not in result.data


async def test_region_unknown_plan_falls_back_to_standard_with_caveat(ctx) -> None:
    """Тариф не назван: по райцентрам подтверждён только абонемент."""
    result = await calculate_price(ctx, scope="region", plan="unknown", children_count=1)

    assert result.ok
    assert result.data["plan"] == Plan.STANDARD.value
    assert result.data["total"] == REGION_STANDARD
    assert result.data["price_per_session"] is None  # число занятий не подтверждено
    assert result.data["compare_single"] is None


# --------------------------------------------------------------------------- #
# Неопределённые аргументы
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("scope", ["", "kostanay", "область", "CITY ", None])
async def test_unknown_scope_asks_for_geography(ctx, scope) -> None:
    """Пока география не определена, считать нельзя: 25 000 против 10 000 ₸."""
    result = await calculate_price(
        ctx, scope=scope, plan="standard", children_count=1
    )

    if scope == "CITY ":  # регистр и пробелы разбираются, это валидный город
        assert result.ok
        assert result.data["scope"] == Scope.CITY.value
        return

    assert result.ok is False
    assert result.status is ToolStatus.NO_DATA
    assert result.data["status"] == "need_scope"
    assert result.data["city"]["plans"] and result.data["region"]["plans"]


@pytest.mark.parametrize("plan", ["vip", "премиум", ""])
async def test_unknown_plan_is_invalid_input(ctx, plan) -> None:
    """Несуществующий тариф — ошибка аргументов, а не «похожий» тариф."""
    result = await calculate_price(ctx, scope="city", plan=plan, children_count=1)

    assert result.ok is False
    assert result.status is ToolStatus.INVALID_INPUT


async def test_city_unknown_plan_shows_both_options(ctx) -> None:
    """Тариф не выбран: показываем оба варианта, выбор оставляем родителю."""
    result = await calculate_price(ctx, scope="city", plan="unknown", children_count=2)

    assert result.ok
    totals = {option["plan"]: option["total"] for option in result.data["options"]}
    assert totals == {"standard": 47_500, "flexible": 57_000}
    assert result.data["total"] is None  # единого итога быть не может


# --------------------------------------------------------------------------- #
# Оговорки по нерешённым конфликтам
# --------------------------------------------------------------------------- #
async def test_flexible_with_two_children_warns_about_unconfirmed_discount(ctx) -> None:
    """C-4: скидка на гибкий тариф владельцем письменно не подтверждена."""
    result = await calculate_price(ctx, scope="city", plan="flexible", children_count=2)

    assert result.ok
    assert any("гибк" in caveat.lower() for caveat in result.caveats)
    # C-5: правило для детей на разных тарифах не закреплено.
    assert result.meta["mixed_plans_needs_operator"] is True


async def test_single_child_has_no_family_caveats(ctx) -> None:
    """Один ребёнок — семейных оговорок быть не должно, они только мешают."""
    result = await calculate_price(ctx, scope="city", plan="standard", children_count=1)

    assert not any("втор" in caveat.lower() for caveat in result.caveats)
    assert result.meta["mixed_plans_needs_operator"] is False


# --------------------------------------------------------------------------- #
# Округление
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "mode", "step", "expected"),
    [
        (25, "half_up", 10, 30),
        (24, "half_up", 10, 20),
        (25, "floor", 10, 20),
        (29, "floor", 10, 20),
        (Decimal("2083.333"), "none", 1, 2083),
        (Decimal("2083.5"), "none", 1, 2084),
        (22_500, "half_up", 10, 22_500),
    ],
)
def test_round_money(value, mode, step, expected) -> None:
    """Округление тенге: half_up вверх с половины, floor — всегда в пользу клиента."""
    assert round_money(value, mode=mode, step=step) == expected


@pytest.mark.parametrize(
    ("base", "percent", "expected"),
    [
        (25_000, 0, 25_000),
        (25_000, 10, 22_500),
        (25_000, 15, 21_250),
        (30_000, 10, 27_000),
        (30_000, 15, 25_500),
        (3_333, 10, 3_000),  # 2999.7 -> half_up до 10
    ],
)
def test_apply_discount(base, percent, expected) -> None:
    """Скидка считается от базовой цены тарифа и округляется по правилу KB."""
    assert apply_discount(base, percent, mode="half_up", step=10) == expected


def test_zero_percent_returns_price_from_pricelist_untouched() -> None:
    """При нулевой скидке цена не округляется: в прайсе она уже такая, какая есть."""
    assert apply_discount(25_001, 0, mode="half_up", step=10) == 25_001
