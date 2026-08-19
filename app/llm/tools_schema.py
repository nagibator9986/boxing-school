"""Превращение ``ToolSpec`` в ``types.Tool`` для google-genai.

Слой LLM не знает про ``app.tools``: сюда приходят только данные (``ToolSpec``
из ``app.types``). Схемы плоские — Gemini поддерживает лишь подмножество
OpenAPI, вложенность глубже двух уровней и ``anyOf`` в параметрах ломают вызов.
"""

from __future__ import annotations

from typing import Any, Final, Sequence

from google.genai import types

from app.types import ToolSpec

#: JSON-тип → ``types.Type``. Всё непонятое считаем строкой.
_TYPES: Final[dict[str, str]] = {
    "string": types.Type.STRING,
    "integer": types.Type.INTEGER,
    "number": types.Type.NUMBER,
    "boolean": types.Type.BOOLEAN,
    "array": types.Type.ARRAY,
    "object": types.Type.OBJECT,
}


def _schema(raw: dict[str, Any]) -> types.Schema:
    """JSON Schema (подмножество) → ``types.Schema``."""
    kind = _TYPES.get(str(raw.get("type", "string")).lower(), types.Type.STRING)
    schema = types.Schema(type=kind)

    description = raw.get("description")
    if isinstance(description, str) and description:
        schema.description = description

    enum = raw.get("enum")
    if isinstance(enum, (list, tuple)) and enum:
        schema.enum = [str(v) for v in enum]

    if kind == types.Type.ARRAY:
        items = raw.get("items")
        schema.items = _schema(items) if isinstance(items, dict) else types.Schema(type=types.Type.STRING)

    if kind == types.Type.OBJECT:
        props = raw.get("properties")
        if isinstance(props, dict):
            schema.properties = {k: _schema(v) for k, v in props.items() if isinstance(v, dict)}
        required = raw.get("required")
        if isinstance(required, (list, tuple)) and required:
            schema.required = [str(v) for v in required]

    for src, dst in (("minimum", "minimum"), ("maximum", "maximum"), ("maxLength", "max_length")):
        value = raw.get(src)
        if value is not None:
            setattr(schema, dst, value)
    return schema


def to_function_declarations(specs: Sequence[ToolSpec]) -> list[types.Tool]:
    """``ToolSpec`` → ``types.Tool``.

    Все объявления кладутся в ОДИН ``Tool``: несколько объектов ``Tool`` с
    ``function_declarations`` API не принимает.
    """
    declarations: list[types.FunctionDeclaration] = []
    for spec in specs:
        params = spec.parameters if isinstance(spec.parameters, dict) else {}
        if params.get("type") != "object":
            params = {"type": "object", "properties": params.get("properties", {})}
        declarations.append(
            types.FunctionDeclaration(
                name=spec.name,
                description=spec.description,
                parameters=_schema(params),
            )
        )
    if not declarations:
        return []
    return [types.Tool(function_declarations=declarations)]


def allowed_names(specs: Sequence[ToolSpec], subset: Sequence[str] | None) -> list[str]:
    """Пересечение доступных инструментов с разрешённым подмножеством.

    ``subset=None`` — ограничений нет, возвращаются все имена.
    """
    names = [spec.name for spec in specs]
    if subset is None:
        return names
    allow = set(subset)
    return [name for name in names if name in allow]


__all__ = ["allowed_names", "to_function_declarations"]
