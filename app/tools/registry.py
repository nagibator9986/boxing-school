"""Реестр инструментов: имя -> реализация, JSON-схемы, метаданные для постчека.

Единственный источник JSON-схем в проекте. ``app/llm/tools_schema.py`` их только
преобразует в объекты SDK, но не хранит: иначе схема и реализация разъедутся, и
модель начнёт слать аргументы, которых код не ждёт.

Два приёма, на которых держится анти-галлюцинационный контур:

* **enum'ы подставляются из базы знаний.** В :data:`RAW_TOOL_SPECS` вместо списка
  залов стоит плейсхолдер, а :func:`build_tool_specs` заменяет его на реальные
  ``gyms[].id`` текущего снимка. Модель физически не может назвать зал, которого
  нет, — вариант просто отсутствует в схеме;
* **аргументы валидируются повторно, уже у нас.** :func:`dispatch` сверяет вызов
  с той же схемой: лишние ключи отбрасывает, недостающие обязательные и
  значения вне enum превращает в ``ToolResult.invalid_input``. Ни одно
  исключение из реализации наружу не выходит.
"""

from __future__ import annotations

import copy
import inspect
from typing import Any, Awaitable, Callable, Final, Mapping

from app.kb.models import KBSnapshot
from app.types import (
    TOOL_ESCALATION_REASONS,
    PostcheckFailKind,
    RenderHint,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from app.tools.booking import create_trial_lead
from app.tools.content import send_content
from app.tools.escalation import escalate_to_manager
from app.tools.facts import get_kb_fact
from app.tools.gyms import find_gym_by_district, get_gyms
from app.tools.pricing import calculate_price
from app.tools.schedule import get_schedule

#: Реализация инструмента: первый позиционный параметр — ``ctx``, остальные — только ключевые.
ToolFn = Callable[..., Awaitable[ToolResult]]

TOOL_NAMES: Final[tuple[str, ...]] = (
    "get_gyms", "find_gym_by_district", "calculate_price", "get_schedule",
    "get_kb_fact", "send_content", "create_trial_lead", "escalate_to_manager",
)

ENUM_GYM_ID: Final[str] = "{{ENUM:gym_id}}"
ENUM_ARTIFACT_ID: Final[str] = "{{ENUM:artifact_id}}"
ENUM_FAQ_TOPIC: Final[str] = "{{ENUM:topic}}"

#: Плейсхолдер -> метод снимка KB, дающий актуальный список значений.
_ENUM_SOURCES: Final[dict[str, str]] = {
    ENUM_GYM_ID: "gym_ids",
    ENUM_ARTIFACT_ID: "artifact_ids",
    ENUM_FAQ_TOPIC: "faq_topics",
}

_IMPLS: Final[dict[str, ToolFn]] = {
    "get_gyms": get_gyms,
    "find_gym_by_district": find_gym_by_district,
    "calculate_price": calculate_price,
    "get_schedule": get_schedule,
    "get_kb_fact": get_kb_fact,
    "send_content": send_content,
    "create_trial_lead": create_trial_lead,
    "escalate_to_manager": escalate_to_manager,
}


# --------------------------------------------------------------------------- #
# Схемы
# --------------------------------------------------------------------------- #
RAW_TOOL_SPECS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name="get_gyms",
        description=(
            "Использовать всегда при вопросах об адресах, районах, «где вы», «сколько у вас залов». "
            "Никогда не перечислять залы по памяти."
        ),
        parameters={
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["city", "region", "all"],
                    "description": "city — Костанай; region — райцентры области; all — всё",
                },
                "settlement": {
                    "type": "string",
                    "description": "Название населённого пункта, если клиент назвал",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 12},
            },
            "required": ["scope"],
        },
        deterministic=True,
        side_effect=False,
    ),
    ToolSpec(
        name="find_gym_by_district",
        description=(
            "Подобрать зал по району, ориентиру или адресу, которые назвал клиент. "
            "Использовать всегда, когда прозвучало название района или ориентир. "
            "Слова клиента передавать как есть, не исправляя."
        ),
        parameters={
            "type": "object",
            "properties": {
                "district_text": {
                    "type": "string",
                    "description": "Слова клиента о районе как есть, без правки",
                },
                "settlement": {"type": "string"},
            },
            "required": ["district_text"],
        },
        deterministic=True,
        side_effect=False,
    ),
    ToolSpec(
        name="calculate_price",
        description=(
            "Рассчитать стоимость. Использовать ВСЕГДА, когда речь о деньгах, скидках, "
            "«сколько выйдет», сравнении абонемента с разовыми. Никогда не называть и не "
            "складывать цены самостоятельно. Перед вызовом обязательно определить, город это "
            "или райцентр."
        ),
        parameters={
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["city", "region"],
                    "description": "city — Костанай; region — райцентр области",
                },
                "plan": {
                    "type": "string",
                    "enum": ["standard", "flexible", "single", "unknown"],
                    "description": (
                        "standard — без перерасчёта; flexible — с перерасчётом; single — разовая"
                    ),
                },
                "children_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "Сколько детей из ОДНОЙ семьи будут заниматься",
                },
                "single_sessions": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 60,
                    "description": "Только для plan=single: сколько разовых тренировок посчитать",
                },
            },
            "required": ["scope", "plan", "children_count"],
        },
        deterministic=True,
        side_effect=False,
    ),
    ToolSpec(
        name="get_schedule",
        description=(
            "Узнать расписание конкретного зала. Использовать всегда при вопросе о времени, "
            "днях недели и возрастных группах. Время занятий не называть без вызова этого "
            "инструмента ни при каких условиях."
        ),
        parameters={
            "type": "object",
            "properties": {
                "gym_id": {"type": "string", "enum": [ENUM_GYM_ID]},
                "child_age": {"type": "integer", "minimum": 3, "maximum": 60},
                "shift": {
                    "type": "string",
                    "enum": ["first", "second", "unknown"],
                    "description": "Смена в школе: first — учится в первую, second — во вторую",
                },
            },
            "required": ["gym_id"],
        },
        deterministic=True,
        side_effect=False,
    ),
    ToolSpec(
        name="get_kb_fact",
        description=(
            "Получить готовый ответ школы по теме: пробное занятие, документы, экипировка, "
            "безопасность, возрастные группы, оплата, заморозка, тренеры, результаты, группы "
            "для девочек, взрослые, соцсети, контакты, договор, количество занятий, размер "
            "группы, соревнования, лето. Эти темы запрещено закрывать по памяти."
        ),
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "enum": [ENUM_FAQ_TOPIC]},
                "scope": {"type": "string", "enum": ["city", "region", "any"]},
            },
            "required": ["topic"],
        },
        deterministic=True,
        side_effect=False,
    ),
    ToolSpec(
        name="send_content",
        description=(
            "Отправить клиенту готовый материал из реестра школы: карточку цен, список залов, "
            "адрес зала, ссылку на Instagram. Материал уходит отдельным сообщением, его "
            "содержимое пересказывать не нужно."
        ),
        parameters={
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "enum": [ENUM_ARTIFACT_ID]},
                "gym_id": {
                    "type": "string",
                    "enum": [ENUM_GYM_ID],
                    "description": "Если артефакт привязан к конкретному залу",
                },
            },
            "required": ["artifact_id"],
        },
        deterministic=True,
        side_effect=True,
    ),
    ToolSpec(
        name="create_trial_lead",
        description=(
            "Записать ребёнка на бесплатное пробное занятие. Вызывать, когда известны имя "
            "ребёнка, возраст и выбранный зал. Точное время пробного не подтверждать — его "
            "назначает администратор."
        ),
        parameters={
            "type": "object",
            "properties": {
                "child_name": {"type": "string", "maxLength": 60},
                "child_age": {"type": "integer", "minimum": 3, "maximum": 17},
                "child_gender": {"type": "string", "enum": ["m", "f", "unknown"]},
                "gym_id": {"type": "string", "enum": [ENUM_GYM_ID]},
                "preferred_time_text": {
                    "type": "string",
                    "description": "Как сказал родитель: «среда вечером»",
                },
                "parent_name": {"type": "string"},
                "phone": {
                    "type": "string",
                    "description": (
                        "Только если родитель назвал номер сам. Для WhatsApp не спрашивать — "
                        "номер уже известен"
                    ),
                },
                "motivation": {"type": "string", "maxLength": 120},
                "main_objection": {"type": "string", "maxLength": 120},
                "health_notes": {"type": "string", "maxLength": 200},
                "parent_agreed": {
                    "type": "boolean",
                    "description": (
                        "true — ТОЛЬКО если родитель прямо согласился записаться в ответ "
                        "на твой вопрос «Записать на пробное занятие?». Названные им имя "
                        "и возраст ребёнка согласием НЕ являются."
                    ),
                },
            },
            "required": ["child_name", "child_age", "gym_id", "parent_agreed"],
        },
        deterministic=False,
        side_effect=True,
    ),
    ToolSpec(
        name="escalate_to_manager",
        description=(
            "Передать вопрос живому администратору. Вызывать, когда данных в базе нет, клиент "
            "просит человека, речь о здоровье, жалобе, цене вне прайса, рассрочке, возрасте вне "
            "диапазона или о разговоре на третьем языке."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {"type": "string", "enum": list(TOOL_ESCALATION_REASONS)},
                "question_summary": {"type": "string", "maxLength": 200},
                "urgency": {"type": "string", "enum": ["normal", "high"]},
            },
            "required": ["reason", "question_summary"],
        },
        deterministic=False,
        side_effect=True,
    ),
)


# --------------------------------------------------------------------------- #
# Метаданные для пост-фильтра
# --------------------------------------------------------------------------- #
#: Какие категории фактов инструмент разрешает произнести в ответе. Пост-фильтр
#: (``app/core/postcheck.py``) сверяет с этим списком: сумма без вызова
#: ``calculate_price`` и время без ``get_schedule`` — это галлюцинация.
TOOL_ALLOWS: Final[dict[str, tuple[PostcheckFailKind, ...]]] = {
    "get_gyms": (PostcheckFailKind.ADDRESS, PostcheckFailKind.GYM_NAME),
    "find_gym_by_district": (PostcheckFailKind.ADDRESS, PostcheckFailKind.GYM_NAME),
    "calculate_price": (PostcheckFailKind.MONEY,),
    "get_schedule": (PostcheckFailKind.TIME, PostcheckFailKind.WEEKDAY),
    "get_kb_fact": (),
    "send_content": (),
    "create_trial_lead": (),
    "escalate_to_manager": (),
}

#: ``render_hint`` по умолчанию (INTERFACES §11.10) — нужен пайплайну до вызова.
TOOL_RENDER_HINTS: Final[dict[str, RenderHint]] = {
    "get_gyms": RenderHint.VERBATIM,
    "find_gym_by_district": RenderHint.VERBATIM,
    "calculate_price": RenderHint.NUMBERS_ONLY,
    "get_schedule": RenderHint.VERBATIM,
    "get_kb_fact": RenderHint.VERBATIM,
    "send_content": RenderHint.SILENT,
    "create_trial_lead": RenderHint.SUMMARIZE,
    "escalate_to_manager": RenderHint.FIXED_REPLY,
}

_SPEC_BY_NAME: Final[dict[str, ToolSpec]] = {spec.name: spec for spec in RAW_TOOL_SPECS}


def tool_meta(name: str) -> dict[str, Any]:
    """Всё, что о инструменте нужно знать пайплайну и пост-фильтру, одним словарём."""
    spec = _SPEC_BY_NAME.get(name)
    if spec is None:
        raise KeyError(name)
    return {
        "name": name,
        "deterministic": spec.deterministic,
        "side_effect": spec.side_effect,
        "render_hint": TOOL_RENDER_HINTS[name],
        "allows": TOOL_ALLOWS[name],
        "required": tuple(spec.parameters.get("required", ())),
    }


# --------------------------------------------------------------------------- #
# Подстановка enum'ов из KB
# --------------------------------------------------------------------------- #
def _resolve_enums(node: Any, kb: KBSnapshot) -> Any:
    """Рекурсивно заменяет плейсхолдеры enum'ов значениями из снимка KB."""
    if isinstance(node, dict):
        resolved: dict[str, Any] = {}
        for key, value in node.items():
            if key == "enum" and isinstance(value, list):
                values = _enum_values(value, kb)
                if values is None:
                    continue  # список пуст — enum убираем, иначе схему отвергнет SDK
                resolved[key] = values
                continue
            resolved[key] = _resolve_enums(value, kb)
        return resolved
    if isinstance(node, list):
        return [_resolve_enums(item, kb) for item in node]
    return node


def _enum_values(values: list[Any], kb: KBSnapshot) -> list[str] | None:
    """Разворачивает плейсхолдер в реальный список; ``None`` — значений нет."""
    result: list[str] = []
    for value in values:
        source = _ENUM_SOURCES.get(value) if isinstance(value, str) else None
        if source is None:
            result.append(value)
            continue
        result.extend(getattr(kb, source)())
    unique = list(dict.fromkeys(result))
    return unique or None


_SPEC_CACHE: dict[str, tuple[ToolSpec, ...]] = {}
_SPEC_CACHE_LIMIT: Final[int] = 4


def build_tool_specs(kb: KBSnapshot) -> tuple[ToolSpec, ...]:
    """Подставляет enum'ы из KB: gym_id из kb.gym_ids(), artifact_id из kb.artifact_ids(),
    topic из kb.faq_topics(). Модель физически не может назвать несуществующий id."""
    cached = _SPEC_CACHE.get(kb.kb_hash)
    if cached is not None:
        return cached
    built = tuple(
        spec.model_copy(update={"parameters": _resolve_enums(copy.deepcopy(spec.parameters), kb)})
        for spec in RAW_TOOL_SPECS
    )
    if len(_SPEC_CACHE) >= _SPEC_CACHE_LIMIT:
        _SPEC_CACHE.clear()
    _SPEC_CACHE[kb.kb_hash] = built
    return built


def _spec_for(kb: KBSnapshot, name: str) -> ToolSpec | None:
    for spec in build_tool_specs(kb):
        if spec.name == name:
            return spec
    return None


# --------------------------------------------------------------------------- #
# Реестр
# --------------------------------------------------------------------------- #
def get_impl(name: str) -> ToolFn:
    """Имя -> реализация. Неизвестное имя -> KeyError."""
    return _IMPLS[name]


def is_deterministic(name: str) -> bool:
    """Считается ли инструмент детерминированным (результат зависит только от KB и аргументов)."""
    spec = _SPEC_BY_NAME.get(name)
    if spec is None:
        raise KeyError(name)
    return spec.deterministic


def has_side_effect(name: str) -> bool:
    """Меняет ли инструмент состояние: outbox, лид, пауза."""
    spec = _SPEC_BY_NAME.get(name)
    if spec is None:
        raise KeyError(name)
    return spec.side_effect


# --------------------------------------------------------------------------- #
# Валидация аргументов
# --------------------------------------------------------------------------- #
def _coerce(value: Any, schema: Mapping[str, Any], key: str, errors: list[str]) -> Any:
    """Мягко приводит значение к типу схемы. Модель нередко шлёт число строкой."""
    expected = schema.get("type")
    if expected == "integer":
        if isinstance(value, bool):
            errors.append(f"{key}: ожидалось целое число")
            return None
        if isinstance(value, int):
            result = value
        elif isinstance(value, float) and value.is_integer():
            result = int(value)
        elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
            result = int(value.strip())
        else:
            errors.append(f"{key}: ожидалось целое число")
            return None
        minimum, maximum = schema.get("minimum"), schema.get("maximum")
        if minimum is not None and result < minimum:
            errors.append(f"{key}: значение {result} меньше допустимого {minimum}")
            return None
        if maximum is not None and result > maximum:
            errors.append(f"{key}: значение {result} больше допустимого {maximum}")
            return None
        return result
    if expected == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        errors.append(f"{key}: ожидалось число")
        return None
    if expected == "boolean":
        if isinstance(value, bool):
            return value
        errors.append(f"{key}: ожидалось булево значение")
        return None
    # string и всё остальное
    if isinstance(value, str):
        result_str = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        result_str = str(value)
    else:
        errors.append(f"{key}: ожидалась строка")
        return None
    max_length = schema.get("maxLength")
    if isinstance(max_length, int) and len(result_str) > max_length:
        result_str = result_str[:max_length]
    allowed = schema.get("enum")
    if isinstance(allowed, list) and allowed and result_str not in allowed:
        errors.append(f"{key}: значение '{result_str}' отсутствует в базе знаний")
        return None
    return result_str


def validate_args(spec: ToolSpec, args: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Сверяет аргументы вызова со схемой инструмента.

    Лишние ключи отбрасываются молча (модель любит добавлять «пояснения»),
    недостающие обязательные и значения вне enum — это ошибки.
    """
    payload = dict(args or {})
    properties: Mapping[str, Any] = spec.parameters.get("properties", {})
    required = list(spec.parameters.get("required", ()))
    errors: list[str] = []
    cleaned: dict[str, Any] = {}

    rejected: set[str] = set()
    for key, schema in properties.items():
        if key not in payload:
            continue
        value = payload[key]
        if value is None:
            continue
        coerced = _coerce(value, schema, key, errors)
        if coerced is None:
            rejected.add(key)
        else:
            cleaned[key] = coerced

    for key in required:
        # Про отклонённое значение ошибка уже есть — второй раз не жалуемся.
        if key not in cleaned and key not in rejected:
            errors.append(f"не передан обязательный аргумент '{key}'")
    return cleaned, errors


# --------------------------------------------------------------------------- #
# Исполнение
# --------------------------------------------------------------------------- #
async def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Валидирует args по схеме (лишние ключи отбрасываются, недостающие обязательные ->
    ToolResult.invalid_input) и вызывает реализацию. Никогда не пробрасывает исключение:
    любое падение превращается в ToolResult.failure."""
    try:
        impl = get_impl(name)
    except KeyError:
        return ToolResult.invalid_input(f"неизвестный инструмент '{name}'")

    try:
        spec = _spec_for(ctx.kb, name) or _SPEC_BY_NAME[name]
        cleaned, errors = validate_args(spec, args)
    except Exception as exc:  # pragma: no cover - битый снимок KB
        return ToolResult.failure(f"{name}: не удалось разобрать аргументы: {exc}")

    if errors:
        return ToolResult.invalid_input("; ".join(errors))

    try:
        result = impl(ctx, **cleaned)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        # Бизнес-исходы возвращаются через ToolResult; сюда попадает только сбой.
        return ToolResult.failure(f"{name}: {type(exc).__name__}: {exc}")

    if not isinstance(result, ToolResult):  # pragma: no cover - защита контракта
        return ToolResult.failure(f"{name}: реализация вернула {type(result).__name__}, а не ToolResult")
    return result


__all__ = [
    "ENUM_ARTIFACT_ID",
    "ENUM_FAQ_TOPIC",
    "ENUM_GYM_ID",
    "RAW_TOOL_SPECS",
    "TOOL_ALLOWS",
    "TOOL_NAMES",
    "TOOL_RENDER_HINTS",
    "ToolFn",
    "build_tool_specs",
    "dispatch",
    "get_impl",
    "has_side_effect",
    "is_deterministic",
    "tool_meta",
    "validate_args",
]
