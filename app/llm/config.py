"""Конфигурация вызовов Gemini.

Все значения сверены с реально установленным ``google-genai`` (интроспекция SDK),
а не с документацией: имена категорий безопасности, режимы function calling и
поля ``ThinkingConfig`` в разных версиях SDK различаются.
"""

from __future__ import annotations

from typing import Final, Sequence

from google.genai import types

from app.config import Settings, get_settings

#: Уровни мышления Gemini 3. В SDK 2.17 это ``types.ThinkingLevel``.
_THINKING_LEVELS: Final[dict[str, str]] = {
    "minimal": types.ThinkingLevel.MINIMAL,
    "low": types.ThinkingLevel.LOW,
    "medium": types.ThinkingLevel.MEDIUM,
    "high": types.ThinkingLevel.HIGH,
}

#: Режимы tool-calling. ``VALIDATED`` есть в SDK 2.17.
_TOOL_MODES: Final[dict[str, str]] = {
    "AUTO": types.FunctionCallingConfigMode.AUTO,
    "ANY": types.FunctionCallingConfigMode.ANY,
    "NONE": types.FunctionCallingConfigMode.NONE,
    "VALIDATED": types.FunctionCallingConfigMode.VALIDATED,
}

#: Бокс — контактный спорт: «опасный контент» и «харассмент» отключены,
#: иначе модель отказывается обсуждать спарринги и удары.
#: Категория называется HARM_CATEGORY_DANGEROUS_CONTENT — HARM_CATEGORY_DANGEROUS не существует.
SAFETY: Final[list[types.SafetySetting]] = [
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=types.HarmBlockThreshold.OFF,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=types.HarmBlockThreshold.OFF,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
    ),
]


def model_primary(settings: Settings | None = None) -> str:
    """Основная модель. Литералов в коде нет — только настройки."""
    return (settings or get_settings()).gemini_model_primary


def model_fallback(settings: Settings | None = None) -> str:
    """Запасная модель, на которую уходим при rate limit основной."""
    return (settings or get_settings()).gemini_model_fallback


def thinking_config(settings: Settings | None = None) -> types.ThinkingConfig:
    """``ThinkingConfig`` из настроек. Мысли в ответ не возвращаются."""
    cfg = settings or get_settings()
    level = _THINKING_LEVELS.get(cfg.gemini_thinking_level, types.ThinkingLevel.MINIMAL)
    return types.ThinkingConfig(thinking_level=level, include_thoughts=False)


def build_config(
    *,
    system_instruction: str,
    tools: list[types.Tool] | None,
    allowed_function_names: Sequence[str] | None,
    mode: str,
    max_output_tokens: int,
    settings: Settings | None = None,
) -> types.GenerateContentConfig:
    """Собирает ``GenerateContentConfig`` для основного хода.

    ``temperature``/``top_p``/``top_k`` НЕ задаются: у Gemini 3 их изменение
    ухудшает следование инструкциям. Автоматический function calling выключен —
    цикл ведём вручную (нужны id параллельных вызовов и thought signatures).
    """
    cfg = settings or get_settings()
    tool_config: types.ToolConfig | None = None
    if tools:
        resolved = _TOOL_MODES.get(mode.upper(), types.FunctionCallingConfigMode.AUTO)
        if allowed_function_names and resolved is types.FunctionCallingConfigMode.AUTO:
            # Ограничение API, а не наш выбор: Gemini отвечает
            # «400 Please set allowed_function_names only when function calling mode is ANY».
            # Пайплайн сужает список инструментов на подозрении в инъекции и на офтопике,
            # оставляя режим AUTO, — и любое такое сообщение роняло ход целиком: клиент
            # получал «сбой на стороне сервиса», бот уходил на паузу и замолкал до конца
            # разговора. Берём VALIDATED, а не ANY: ANY заставил бы модель вызывать
            # инструмент на каждом ходу, тогда как ответить текстом она обязана уметь.
            resolved = types.FunctionCallingConfigMode.VALIDATED
        fcc = types.FunctionCallingConfig(mode=resolved)
        if allowed_function_names:
            fcc.allowed_function_names = list(allowed_function_names)
        tool_config = types.ToolConfig(function_calling_config=fcc)

    return types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=tools or None,
        tool_config=tool_config,
        max_output_tokens=max_output_tokens,
        safety_settings=SAFETY,
        thinking_config=thinking_config(cfg),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


def build_json_config(
    *,
    system_instruction: str,
    response_schema: type,
    max_output_tokens: int,
    settings: Settings | None = None,
) -> types.GenerateContentConfig:
    """Конфиг «тихого» вызова со structured output.

    Structured output и function calling в одном вызове не смешиваются:
    инструменты здесь не передаются вообще.
    """
    cfg = settings or get_settings()
    return types.GenerateContentConfig(
        system_instruction=system_instruction,
        response_mime_type="application/json",
        response_schema=response_schema,
        max_output_tokens=max_output_tokens,
        safety_settings=SAFETY,
        thinking_config=thinking_config(cfg),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


__all__ = [
    "SAFETY",
    "build_config",
    "build_json_config",
    "model_fallback",
    "model_primary",
    "thinking_config",
]
