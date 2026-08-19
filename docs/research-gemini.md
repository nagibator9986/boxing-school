# Google Gemini API — техническая справка для production-бота (AINAZAROV TOP TEAM)

**Дата проверки источников:** 2026-08-09
**Метод:** live-проверка ai.google.dev, googleapis.github.io/python-genai, pypi.org. По памяти НЕ отвечалось.
**Статус:** прошёл независимую верификацию 2026-08-09 (интроспекция установленного `google-genai==2.17.0` + повторный фетч первоисточников). Исправлены 2 blocker'а, 1 high, 3 medium; 2 утверждения помечены как НЕ ПОДТВЕРЖДЁННЫЕ. Полный разбор — в секции [«Результаты независимой верификации»](#результаты-независимой-верификации) в конце файла.
**Проект:** AI-консультант школы бокса/кикбоксинга, WhatsApp + Instagram через Wazzup24, RU/KZ, Python 3.11+/FastAPI.

> ⚠️ **ГЛАВНОЕ, ЧТО ИЗМЕНИЛОСЬ И ЛОМАЕТ ВСЕ СТАРЫЕ ТУТОРИАЛЫ**
> 1. Актуальное поколение моделей — **Gemini 3.x**, а не 2.5. Дефолтная модель в доках — `gemini-3.6-flash`.
> 2. Появился **Interactions API** (`client.interactions.create`), который Google рекомендует для всей новой разработки. Старый `client.models.generate_content` **не удалён и полностью поддерживается**, но новые агентные фичи туда не приезжают.
> 3. `thinking_budget` **заменён** на `thinking_level`.
> 4. Для Gemini 3.x **не рекомендуется трогать `temperature`/`top_p`/`top_k`**.
> 5. Пакет `google-generativeai` **мёртв** (deprecated 30.11.2025).

---

## 0. Оглавление

1. [SDK: какой пакет](#1-sdk-какой-пакет)
2. [Модели и цены](#2-модели-и-цены)
3. [system_instruction](#3-system_instruction)
4. [Function calling / tool use](#4-function-calling--tool-use)
5. [Многошаговый диалог и stateless-сервер](#5-многошаговый-диалог-и-stateless-сервер)
6. [Thinking / reasoning budget](#6-thinking--reasoning-budget)
7. [Context caching](#7-context-caching)
8. [Safety settings](#8-safety-settings)
9. [Structured output](#9-structured-output)
10. [Ошибки, лимиты, retry](#10-ошибки-лимиты-retry)
11. [Подсчёт токенов и стоимости](#11-подсчёт-токенов-и-стоимости)
12. [Стриминг](#12-стриминг)
13. [Рекомендуемая архитектура для нашего бота](#13-рекомендуемая-архитектура-для-нашего-бота)
14. [Топ граблей](#14-топ-граблей)
15. [Результаты независимой верификации](#результаты-независимой-верификации)

---

## 1. SDK: какой пакет

**Ответ: `google-genai`. Однозначно. `google-generativeai` использовать нельзя.**

| | Новый (использовать) | Легаси (НЕ использовать) |
|---|---|---|
| pip-пакет | `google-genai` | `google-generativeai` |
| Импорт | `from google import genai` | `import google.generativeai as genai` |
| Статус | GA с мая 2025, "stable, fully supported for production use" | "Not actively maintained", **deprecated с 30 ноября 2025** |
| Версия на 2026-08-09 | **2.17.0** (релиз 06.08.2026) | — |
| Python | **>= 3.10** (поддержка 3.10–3.14) | — |

Легаси-библиотеки не дают доступа к Live API, Veo и вообще ко всему новому. Репозиторий легаси-SDK переименован в `google-gemini/deprecated-generative-ai-python` — это само по себе достаточный сигнал.

> ⚠️ **Interactions API доступен с `google-genai` 1.55.0** (релиз 11.12.2025, запись «Add the Interactions API» в CHANGELOG), а НЕ с 2.0.0. Но пин всё равно ставим `>=2.0.0`: в 2.0.0 (07.05.2026) Google внёс **breaking changes внутри interactions** — добавлены `steps`, SSE-события переименованы в `interaction.created` / `interaction.completed`, `response_format` стал полиморфным. В самом CHANGELOG отдельно оговорено: «The breaking changes are only in interactions. `GenerateContent` usage in unaffected». Т.е. корректная формулировка: *доступен с 1.55.0; пиним >=2.0.0, потому что в 2.0.0 сломали формат interactions*. Минимальной версии SDK ни [libraries](https://ai.google.dev/gemini-api/docs/libraries), ни [migrate-to-interactions](https://ai.google.dev/gemini-api/docs/migrate-to-interactions) не называют. У нас 2.17.0 — норм.

### Установка

```bash
pip install -U "google-genai>=2.17.0,<3.0.0"
```

`requirements.txt` / `pyproject.toml`:

```
google-genai>=2.17.0,<3.0.0
```

### Минимальный клиент (Gemini Developer API, НЕ Vertex)

```python
import os
from google import genai

# Вариант 1 — явный ключ (предпочтительно для FastAPI, ключ из Secret Manager/env)
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# Вариант 2 — из переменной окружения GEMINI_API_KEY автоматически
client = genai.Client()
```

Для Vertex AI пришлось бы передавать `vertexai=True, project=..., location=...` — **нам это не нужно**, работаем на Developer API по API-ключу.

### Асинхронный клиент — то, что реально нужно в FastAPI

```python
response = await client.aio.models.generate_content(
    model="gemini-3.5-flash-lite",
    contents="Сәлеметсіз бе!",
)
print(response.text)
```

`client.aio.*` зеркалит весь синхронный API. **В FastAPI-хендлерах используем только `client.aio`**, иначе блокируется event loop.

**Источники:**
- https://ai.google.dev/gemini-api/docs/libraries
- https://ai.google.dev/gemini-api/docs/quickstart
- https://pypi.org/project/google-genai/
- https://github.com/googleapis/python-genai

---

## 2. Модели и цены

### 2.1. Прайс (Paid Standard, USD за 1M токенов), проверено 2026-08-09

| Model ID | Вход | Выход | Комментарий |
|---|---|---|---|
| `gemini-3.6-flash` | **$1.50** | **$7.50** | Флагманский flash, дефолт в доках. Дорого для нашего кейса |
| `gemini-3.5-flash` | $1.50 | $9.00 | Самый "умный" flash, агентика/код |
| `gemini-3.5-flash-lite` | **$0.30** | **$2.50** | Свежий lite, thinking по умолчанию `minimal` |
| `gemini-3.1-flash-lite` | **$0.25** (текст/img/video); $0.50 (audio) | **$1.50** | Stable, топ по multilingual в лёгком классе |
| `gemini-3.1-pro-preview` | $2.00 (≤200k) / $4.00 (>200k) | $12.00 / $18.00 | Preview, дорого |
| `gemini-3-flash-preview` | $0.50 / $1.00 (audio) | $3.00 | Preview |
| `gemini-2.5-flash` | $0.30 / $1.00 (audio) | $2.50 | Прошлое поколение |
| `gemini-2.5-flash-lite` | **$0.10** / $0.30 (audio) | **$0.40** | Самый дешёвый. Прошлое поколение |
| `gemini-2.5-pro` | $1.25 / $2.50 | $10.00 / $15.00 | Прошлое поколение, pro |

Дополнительные тарифные режимы для всех моделей: **Batch** и **Flex** = 50% от Standard; **Priority** = 180% от Standard.

**Free tier:** доступен почти для всех перечисленных, кроме `gemini-3.1-pro-preview` (там в строке Free Tier прямо «Not available»).
⚠️ **НЕ ПОДТВЕРЖДЕНО — проверить в личном кабинете/на практике:** по `gemini-2.5-pro` показания расходятся. Повторная выборка pricing-страницы 2026-08-09 даёт для 2.5 Pro free tier «Free of charge» (input и output), но независимая верификация читала ту же строку как «Not available». Страница рендерится табами/таблицей, и парсеры видят разные ячейки. На выбор моделей проекта это не влияет (мы на flash-lite и в любом случае на Tier 1+), но если понадобится free-tier доступ к 2.5 Pro — **сверить в AI Studio / Google AI Studio billing, а не по доке**.

### 2.2. Актуальные stable-модели (позиционирование)

- `gemini-3.6-flash` — «balances speed with intelligence… agentic and multimodal tasks»
- `gemini-3.5-flash` — самый интеллектуальный для агентики и кода
- `gemini-3.5-flash-lite` — «fastest, most cost-effective»
- `gemini-3.1-flash-lite` — «frontier performance at reduced expense», stable с мая 2026
- `gemini-2.5-flash` / `gemini-2.5-flash-lite` / `gemini-2.5-pro` — предыдущее поколение, живы
- `gemini-embedding-001`, `gemini-embedding-2-preview` — эмбеддинги (пригодятся для RAG по базе знаний)

**Отключены (shut down):** `gemini-2.0-flash`, `gemini-2.0-flash-lite`, `gemini-3.1-flash-lite-preview`, `gemini-3-pro-preview`. Если где-то в старом коде остались эти ID — сломается.

### 2.3. Что берём мы

**Основная модель: `gemini-3.5-flash-lite`**
- вход 1 048 576 токенов, выход 65 536
- function calling ✓, structured outputs ✓, caching ✓, thinking ✓
- **default `thinking_level` = `minimal`** — это ровно то, что нужно для латентности в мессенджере: не надо ничего отключать, оно уже быстрое
- $0.30 / $2.50

**Fallback: `gemini-3.1-flash-lite`**
- stable, вход 1M / выход 65k, все нужные фичи ✓ (FC, structured output, caching, thinking, batch)
- дешевле по выходу ($1.50 против $2.50) — а в чат-боте выход доминирует
- на официальной model-card-странице use-case описан так: «Fast, cheap, high-volume translation, such as processing chat messages, reviews, and support tickets at scale», модель — «a low-latency, cost-effective multimodal model optimized for high-frequency, lightweight tasks»
- ⚠️ **НЕ ПОДТВЕРЖДЕНО — проверить в личном кабинете/на практике:** формулировка «best-in-class translation and multilingual understanding, with noted improvements in non-Latin scripts» и цифра **MMMLU 88.9%** на странице https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite **отсутствуют** (перепроверено фетчем 2026-08-09: там только лимиты 1 048 576 / 65 536, stable с мая 2026 и use-cases, никаких бенчмарков). Цифра гуляет по третьим источникам с атрибуцией к DeepMind model card / eval-PDF (`storage.googleapis.com/deepmind-media/gemini/gemini_3-1_flash-lite_model_evaluation.pdf`) — то есть, вероятно, существует, но не там, куда ссылался этот документ. Ссылаться на DeepMind model card либо не использовать цифру в аргументации вообще.

**Про казахский:** ⚠️ **НЕ ПОДТВЕРЖДЕНО** — Google нигде не публикует пер-язык метрики по казахскому. Есть только агрегированный MMMLU и общие заявления про multilingual/non-Latin scripts. **Обязателен собственный A/B на ~50 реальных казахских репликах** (запись на пробное, вопросы про расписание, цены, «балаға 7 жас, қай топқа?»). Кириллица казахского содержит специфические ә/ғ/қ/ң/ө/ұ/ү/һ/і — проверять надо и понимание, и генерацию без «сползания» в русский.

**Экономический вариант:** `gemini-2.5-flash-lite` за $0.10/$0.40 — в 6 раз дешевле на выходе, чем 3.5-flash-lite. Но это прошлое поколение. Рассматривать только если A/B покажет приемлемое качество на казахском. Не ставить по умолчанию.

**Источники:**
- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/models
- https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite
- https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite

---

## 3. system_instruction

Передаётся отдельным параметром, **не** внутри `contents`/`input`.

### Legacy generateContent

```python
from google import genai
from google.genai import types

response = client.models.generate_content(
    model="gemini-3.5-flash-lite",
    contents="Сколько стоит абонемент для двоих детей?",
    config=types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,   # str или list[str] или types.Content
    ),
)
```

### Interactions API

```python
interaction = client.interactions.create(
    model="gemini-3.5-flash-lite",
    system_instruction=SYSTEM_PROMPT,
    input="Сколько стоит абонемент для двоих детей?",
)
```

### Ограничения / особенности

- ⚠️ **`system_instruction` в Interactions API — interaction-scoped.** Его надо передавать **в каждом вызове**, даже при использовании `previous_interaction_id`. То же самое касается `tools` и `generation_config`. Забыл передать на втором ходу — бот теряет личность посреди диалога. Это грабля №1 при миграции.
- Отдельного лимита на размер `system_instruction` не документировано — он ест общий контекст (1M токенов у наших моделей). ⚠️ **НЕ ПОДТВЕРЖДЕНО**: отдельный лимит символов.
- Системная инструкция **не является гарантией безопасности** — не полагаться на неё для контроля доступа к функциям; проверять права на своей стороне.
- Тарифицируется как обычные входные токены на **каждом** запросе → см. раздел про кэширование.

**Источники:**
- https://ai.google.dev/gemini-api/docs/generate-content/text-generation
- https://ai.google.dev/gemini-api/docs/interactions-overview

---

## 4. Function calling / tool use

Это ядро нашего бота: `find_nearest_gym`, `calculate_price`, `book_trial_lesson`, `send_content`, `escalate_to_manager`.

### 4.1. Три способа объявить функцию

#### (а) Питоновская функция напрямую — автоматическое объявление (только Python SDK)

SDK сам строит схему из сигнатуры и docstring **и сам выполняет цикл вызова**.

```python
def calculate_price(group: str, months: int, family_members: int) -> dict:
    """Считает стоимость абонемента с учётом семейной скидки.

    Args:
        group: Название группы, например "дети 7-10" или "взрослые".
        months: Срок абонемента в месяцах.
        family_members: Сколько членов одной семьи занимается.
    """
    ...
    return {"total_kzt": 45000, "discount_pct": 10}

response = client.models.generate_content(
    model="gemini-3.5-flash-lite",
    contents="Нас двое из одной семьи, хотим на 3 месяца",
    config=types.GenerateContentConfig(
        tools=[calculate_price],          # ← просто передаём callable
        system_instruction=SYSTEM_PROMPT,
    ),
)
print(response.text)
```

⚠️ **Для production это ловушка.** Автоматический режим сам исполняет функцию внутри SDK — у нас пропадает контроль над таймаутами, ретраями, логированием, идемпотентностью и «человеческим» подтверждением перед записью лида в CRM. **Для `book_trial_lesson` автоматический режим использовать нельзя.** Ручной цикл — ниже.

#### (б) `types.FunctionDeclaration` (legacy generateContent)

```python
from google.genai import types

calc_decl = types.FunctionDeclaration(
    name="calculate_price",
    description=(
        "Рассчитать точную стоимость абонемента в тенге по группе, сроку "
        "и количеству занимающихся членов одной семьи. Использовать ВСЕГДА, "
        "когда клиент спрашивает про цену — никогда не называть цену по памяти."
    ),
    parameters={
        "type": "object",
        "properties": {
            "group": {
                "type": "string",
                "enum": ["kids_5_7", "kids_8_11", "teens_12_15", "adults", "women"],
                "description": "Код возрастной группы",
            },
            "months": {
                "type": "integer",
                "description": "Срок абонемента в месяцах, 1..12",
                "minimum": 1,
                "maximum": 12,
            },
            "family_members": {
                "type": "integer",
                "description": "Количество членов одной семьи, 1 если один",
                "minimum": 1,
            },
        },
        "required": ["group", "months", "family_members"],
    },
)

tools = [types.Tool(function_declarations=[calc_decl])]
```

#### (в) Plain-dict (Interactions API)

В Interactions API инструменты объявляются обычными dict-ами с `"type": "function"`:

```python
calculate_price_declaration = {
    "type": "function",
    "name": "calculate_price",
    "description": "Рассчитать стоимость абонемента с семейными скидками.",
    "parameters": {
        "type": "object",
        "properties": {
            "group": {"type": "string", "enum": ["kids_5_7", "kids_8_11", "teens_12_15", "adults", "women"]},
            "months": {"type": "integer", "description": "Срок в месяцах"},
            "family_members": {"type": "integer", "description": "Членов семьи"},
        },
        "required": ["group", "months", "family_members"],
    },
}
```

### 4.2. Режимы принуждения к вызову

**Legacy generateContent** — `tool_config.function_calling_config.mode`, значения **`AUTO` / `ANY` / `NONE` / `VALIDATED`** (режимов **четыре**, не три; проверено интроспекцией `dir(types.FunctionCallingConfigMode)` на google-genai 2.17.0 → `['ANY','AUTO','MODE_UNSPECIFIED','NONE','VALIDATED']`):

```python
config = types.GenerateContentConfig(
    tools=tools,
    tool_config=types.ToolConfig(
        function_calling_config=types.FunctionCallingConfig(
            mode="ANY",                                # AUTO | ANY | NONE | VALIDATED
            allowed_function_names=["calculate_price"] # ограничить набор
        )
    ),
)
```

- **`AUTO`** — «Model decides whether to call a function or respond directly». Дефолт, когда включён только `function_declarations`. **Наш основной режим.**
- **`ANY`** — «Model is constrained to always predict a function call»: модель **обязана** вызвать одну из функций, свободный текст запрещён.
- **`NONE`** — «Model is prohibited from making function calls»: отвечает как будто инструментов нет.
- **`VALIDATED`** — «Model ensures function schema adherence»; в доке помечен как дефолт при комбинации инструментов (built-in tools или structured outputs включены одновременно с функциями). **Это то, что стоит попробовать на шаге «извлеки лид-данные» вместо жёсткого `ANY`:** модель не принуждается к вызову любой ценой, но соблюдение схемы гарантируется. `ANY` там даёт ложные вызовы, когда данных в реплике просто нет.
- `allowed_function_names` сужает набор и заметно снижает долю кривых вызовов по сравнению с голым `AUTO`.

**Interactions API** — то же через `generation_config.tool_choice`:

```python
generation_config = {
    "tool_choice": {
        "allowed_tools": {
            "mode": "any",
            "tools": ["calculate_price"]
        }
    }
}
```

Короткая форма: `generation_config={"tool_choice": "any"}`.

⚠️ Регистр значений различается между API (`ANY` в legacy vs `any` в Interactions). Не копировать вслепую.

### 4.3. Полный ручной цикл (legacy generateContent) — рекомендуемый для нас

```python
import json
from google import genai
from google.genai import types

TOOL_IMPL = {
    "calculate_price": calculate_price,
    "find_nearest_gym": find_nearest_gym,
    "book_trial_lesson": book_trial_lesson,
}

async def run_turn(client, history: list[types.Content], user_text: str) -> tuple[str, list[types.Content]]:
    contents = list(history)
    contents.append(types.Content(role="user", parts=[types.Part(text=user_text)]))

    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=tools,
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="AUTO")
        ),
        # ВАЖНО: отключаем авто-исполнение, хотим ручной контроль
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        thinking_config=types.ThinkingConfig(thinking_level="minimal"),
    )

    for _ in range(5):  # анти-луп: максимум 5 витков инструментов
        resp = await client.aio.models.generate_content(
            model="gemini-3.5-flash-lite", contents=contents, config=cfg
        )

        cand = resp.candidates[0]
        # 1) кладём ответ модели в историю ЦЕЛИКОМ (включая thought signatures!)
        contents.append(cand.content)

        calls = resp.function_calls or []
        if not calls:
            return resp.text, contents

        # 2) исполняем ВСЕ вызовы (их может быть несколько — параллельный вызов)
        response_parts = []
        for call in calls:
            fn = TOOL_IMPL.get(call.name)
            if fn is None:
                result = {"error": f"unknown function {call.name}"}
            else:
                try:
                    result = await fn(**(call.args or {}))
                except Exception as exc:
                    result = {"error": str(exc)}

            # ВАЖНО: собираем Part вручную — types.Part.from_function_response()
            # НЕ принимает id (сигнатура: name, response, parts), и при параллельных
            # вызовах результаты перепутаются. См. 4.6.
            response_parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        id=call.id,                    # ← обязательно, из function_call
                        name=call.name,
                        response=result if isinstance(result, dict) else {"result": result},
                    )
                )
            )

        # 3) все результаты — ОДНИМ Content с role="user"
        contents.append(types.Content(role="user", parts=response_parts))

    return "Извините, не удалось обработать запрос. Передаю менеджеру.", contents
```

Ключевые моменты:
- Ответ модели кладётся в историю **целиком** (`cand.content`), а не только текст — иначе теряются thought signatures.
- Результаты функций возвращаются с ролью **`user`** (не `function`, не `tool`).
- Результат должен быть **dict**, а не голая строка — иначе SDK ругается.
- ⚠️ **`id` проставляется только вручную.** `types.Part.from_function_response(name=..., response=...)` **не имеет параметра `id`** — проверено на google-genai 2.17.0: `inspect.signature` даёт `(*, name, response, parts=None)`, а `Part.from_function_response(name='calc', response={'a':1}).function_response.id` возвращает `None`. Поле `id` есть только у `types.FunctionResponse`. Поэтому в цикле выше используется явная конструкция `types.Part(function_response=types.FunctionResponse(id=call.id, ...))` (проверено — `id` проставляется). Дока требует: «Always include the exact `id` from the `function_call` in your `function_response` so the API can map the result to the correct request».
- Ограничение числа витков обязательно, иначе модель может зациклиться на compositional calling.

### 4.4. Цикл в Interactions API

```python
import json

interaction = client.interactions.create(
    model="gemini-3.5-flash-lite",
    input="Запишите меня на пробное, Асхат, +7 707 123 45 67",
    tools=[book_trial_declaration],
    system_instruction=SYSTEM_PROMPT,
)

fc_step = next(s for s in interaction.steps if s.type == "function_call")
result = TOOL_IMPL[fc_step.name](**fc_step.arguments)

final = client.interactions.create(
    model="gemini-3.5-flash-lite",
    input=[
        {
            "type": "function_result",
            "name": fc_step.name,
            "call_id": fc_step.id,          # ← обязателен
            "result": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
        }
    ],
    tools=[book_trial_declaration],          # ← повторить!
    system_instruction=SYSTEM_PROMPT,        # ← повторить!
    previous_interaction_id=interaction.id,
)
print(final.output_text)
```

### 4.5. Параллельные и последовательные вызовы

- **Параллельные** (parallel calling): модель может вернуть несколько `function_call` за один ход — например, `find_nearest_gym` + `calculate_price` одновременно. Результаты сопоставляются по уникальному **`id`** вызова. Наш цикл выше это уже обрабатывает (итерируем по `resp.function_calls`).
- **Последовательные/композиционные** (compositional calling): выход одной функции идёт на вход следующей (нашли зал → посчитали цену для его расписания). Обрабатывается тем же циклом, просто за несколько витков.

### 4.6. Грабли function calling

| Грабля | Суть |
|---|---|
| **Thought signatures** | В stateless-режиме «you must include thought blocks with their signatures in subsequent requests to validate authenticity». Если из истории выкинуть служебные части ответа модели и оставить только текст — Gemini 3.x начнёт ошибаться или ронять запрос. Класть в историю **весь** `candidate.content`. |
| **`id` + `name` в FunctionResponse** | «Always include the exact `id` from the `function_call` in your `function_response` so the API can map the result to the correct request» — при параллельных вызовах без `id` результаты перепутаются. **Ловушка SDK:** удобный хелпер `types.Part.from_function_response()` параметра `id` не принимает вообще (сигнатура `(*, name, response, parts=None)`, google-genai 2.17.0) и молча оставляет `function_response.id = None`. Единственная рабочая форма — `types.Part(function_response=types.FunctionResponse(id=call.id, name=call.name, response=result))`. |
| **Подмножество OpenAPI** | «Only a subset of the OpenAPI schema is supported». Не работают/игнорируются `$ref`, `oneOf`/`allOf`/`anyOf` (сложные), `patternProperties`, рекурсивные схемы. Поддержано: `string, number, integer, boolean, object, array, null`; `title, description, properties, required, additionalProperties, enum, format, minimum, maximum, items, prefixItems, minItems, maxItems`. |
| **Большие/глубокие схемы** | «for `any` mode, the API may reject very large or deeply nested schemas». Держать схемы плоскими. |
| **Число функций** | Рекомендация — **10–20 максимум**. У нас 5–6, это ок. |
| **Описание решает всё** | «This is crucial for the model to understand when to use the function» — писать в `description` не «считает цену», а «использовать ВСЕГДА, когда клиент спрашивает про цену; никогда не называть цену по памяти». |
| **Модель не исполняет функцию** | «The Model _doesn't_ execute the function itself» — исполнение и валидация полностью на нас. |

**Источники:**
- https://ai.google.dev/gemini-api/docs/function-calling
- https://ai.google.dev/gemini-api/docs/generate-content/function-calling
- https://ai.google.dev/gemini-api/docs/gemini-3
- https://ai.google.dev/gemini-api/docs/interactions/whats-new-gemini-3.5

---

## 5. Многошаговый диалог и stateless-сервер

Три варианта. Для нас правильный — **третий**.

### Вариант A: `client.chats` (SDK-объект с историей в памяти)

```python
chat = client.chats.create(model="gemini-3.5-flash-lite")
response = chat.send_message("I have 2 dogs in my house.")
response = chat.send_message("How many paws are in my house?")
for message in chat.get_history():
    print(f'role - {message.role}')
```

❌ **Не подходит.** Объект `chat` живёт в памяти процесса. У нас FastAPI за несколькими воркерами/подами, вебхуки Wazzup24 приходят в произвольный инстанс. Хранить `chat` между вебхуками нельзя.

### Вариант B: Interactions API + `previous_interaction_id` (state на сервере Google)

```python
i1 = client.interactions.create(model="gemini-3.5-flash-lite", input="...")
i2 = client.interactions.create(
    model="gemini-3.5-flash-lite",
    input="...",
    previous_interaction_id=i1.id,
)
```

Плюсы: не гоняем всю историю, thought signatures Google хранит сам («The model maintains intermediate reasoning across multi-turn conversations automatically»), лучше попадание в implicit cache.

Минусы, критичные для нас:
- Данные хранятся у Google: `store=true` (дефолт) — **55 дней на платном тарифе / 1 день на free**. Для персданных клиентов (ФИО, телефон, возраст ребёнка) в РК это вопрос к ЗРК «О персональных данных» — нужно решение юриста, а не разработчика.
- Мы теряем единый источник правды: историю всё равно надо дублировать у себя для операторов/аналитики.
- `system_instruction` и `tools` всё равно передаются **каждый раз**.

### Вариант C (рекомендуемый): ручная сборка `contents`, история в нашем сторе

Мы уже храним диалоги в своей БД (нужно для операторов, эскалации, аналитики). Значит — **stateless вызов, история собирается из Postgres/Redis**.

#### Точный формат Content/Part

```python
from google.genai import types

history = [
    types.Content(role="user",  parts=[types.Part(text="Сәлем! Балаға бокс керек")]),
    types.Content(role="model", parts=[types.Part(text="Сәлеметсіз бе! Балаңыз неше жаста?")]),
    types.Content(role="user",  parts=[types.Part(text="8 жаста")]),
]
```

- Роли — **только `"user"` и `"model"`**. Никаких `"assistant"`, `"system"`, `"tool"`. (Роль `system` не существует — для этого есть `system_instruction`.)
- Результаты функций идут **тоже с ролью `"user"`**, через `types.Part(function_response=types.FunctionResponse(id=call.id, name=call.name, response=result))`. Хелпер `types.Part.from_function_response(...)` не годится — в нём нет параметра `id` (см. 4.3/4.6).
- Есть сахар: `types.UserContent` / `types.ModelContent` — то же самое с зафиксированной ролью.
- Строку тоже можно передать напрямую: `contents="текст"` — SDK обернёт в `Content(role="user")`.

#### Сериализация истории в БД и обратно

```python
# Сохранение (Part/Content — pydantic-модели)
row = content.model_dump(mode="json", exclude_none=True)

# Восстановление
content = types.Content.model_validate(row)
```

⚠️ **Не хранить историю как `[{"role": ..., "text": ...}]`.** Так теряются function_call/function_response части и thought signatures. Хранить полный дамп `Content`.

#### Скелет stateless-хендлера

```python
async def handle_wazzup_webhook(chat_id: str, user_text: str) -> str:
    history = await store.load_history(chat_id, limit_turns=20)   # list[types.Content]
    answer, new_history = await run_turn(client, history, user_text)
    await store.save_history(chat_id, new_history)
    return answer
```

#### Стратегия обрезки

Контекст 1M токенов — обрезать по длине почти никогда не придётся. Но для цены и латентности всё равно держим окно: последние **15–20 ходов** + опционально «краткое резюме диалога» в начале. Обрезать всегда **по границе полного цикла tool-call** — нельзя оставить в истории `function_call` без парного `function_response`.

Для Interactions API stateless выглядит так: `store=false` + вручную передавать весь диалог в `input`; `previous_interaction_id` при этом использовать нельзя.

**Источники:**
- https://ai.google.dev/gemini-api/docs/generate-content/text-generation
- https://ai.google.dev/gemini-api/docs/interactions-overview
- https://googleapis.github.io/python-genai/

---

## 6. Thinking / reasoning budget

**Параметр называется `thinking_level`.** `thinking_budget` — устаревший, «Replace deprecated `thinking_budget` with `thinking_level`».

Значения: **`minimal`, `low`, `medium`, `high`**.

| Модель | Допустимые значения | Дефолт |
|---|---|---|
| `gemini-3.6-flash` | minimal, low, medium, high | medium |
| `gemini-3.5-flash` | minimal, low, medium, high | medium |
| **`gemini-3.5-flash-lite`** | minimal, low, medium, high | **minimal** |
| `gemini-3-flash-preview` | minimal, low, medium, high | high |
| `gemini-3.1-pro-preview` | low, medium, high | high |
| `gemini-2.5-pro` | low, medium, high | on |
| `gemini-2.5-flash` | low, medium, high | on |
| `gemini-2.5-flash-lite` | low, medium, high | **off** |

- **Полностью выключить размышления можно только у `gemini-2.5-flash-lite`** (и там это дефолт).
- Для Gemini 3.x минимум — `"minimal"`, что «matches the "no thinking" setting and minimizes latency».
- «Gemini 3 treats these levels as relative allowances for thinking rather than strict token guarantees» — это не жёсткий бюджет в токенах.
- Thinking-токены **тарифицируются как выходные**. При `high` счёт может вырасти в разы.

```python
config = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    thinking_config=types.ThinkingConfig(thinking_level="minimal"),
)
```

В Interactions API:

```python
generation_config={"thinking_level": "minimal"}
```

**Для нас:** `thinking_level="minimal"` везде. Консультация по расписанию и запись на пробное не требуют рассуждений, а WhatsApp требует ответа за 1–2 секунды. У `gemini-3.5-flash-lite` это и так дефолт, но ставим явно — дефолты меняются (у 3.5 Flash дефолт уже менялся с `high` на `medium`).

**Thought signatures:** «encrypted representations of the model's internal reasoning», «required to maintain reasoning continuity across multi-turn interactions». В stateless-режиме их обязательно тащить через историю (см. раздел 4.6).

**Источники:**
- https://ai.google.dev/gemini-api/docs/thinking
- https://ai.google.dev/gemini-api/docs/gemini-3
- https://ai.google.dev/gemini-api/docs/interactions/whats-new-gemini-3.5

---

## 7. Context caching

### 7.1. Implicit (неявное) — работает само

Включено по умолчанию для Gemini 2.5+ без всякой конфигурации, работает и в stateful, и в stateless. «We automatically pass on cost savings if your request hits caches.»

**Минимальный размер входа для срабатывания:**

| Модель | Минимум токенов |
|---|---|
| Gemini 3.5 Flash | 4 096 |
| Gemini 3.1 Pro Preview | 4 096 |
| Gemini 2.5 Flash | 2 048 |
| Gemini 2.5 Pro | 2 048 |

⚠️ **НЕ ПОДТВЕРЖДЕНО:** минимум конкретно для `gemini-3.5-flash-lite` и `gemini-3.1-flash-lite` в доке не указан. Исходить из консервативных 4 096 токенов.

**Главное правило:** кэш ловится по **префиксу**. Значит стабильный `system_instruction` + база знаний должны идти **строго первыми и байт-в-байт одинаково**. Любая динамика (имя клиента, дата, «сегодня 9 августа») в начале промпта убивает кэш. Всё динамическое — **в конец**.

**Проверка попадания — поле зависит от API, не перепутать:**

| API | Где смотреть |
|---|---|
| **legacy `generate_content`** (наш путь, см. раздел 13) | `response.usage_metadata.cached_content_token_count` |
| **Interactions API** | `interaction.usage.total_cached_tokens` |

⚠️ У `GenerateContentResponse` **нет атрибута `usage`** — проверено интроспекцией google-genai 2.17.0: поля ответа `['sdk_http_response','candidates','create_time','model_version','prompt_feedback','response_id','usage_metadata','model_status','automatic_function_calling_history','parsed']`. Обращение к `response.usage.total_cached_tokens` на legacy-пути даст `AttributeError` (или молчаливый `None` при `getattr`), и мониторинг кэша будет всё время показывать ноль. `total_cached_tokens` — поле `google.genai.interactions.Usage`, именно его описывает страница caching. См. также раздел 11.2, где эти два набора разведены правильно.

### 7.2. Explicit (явное) — `client.caches`

```python
from google.genai import types

cache = client.caches.create(
    model="gemini-3.5-flash-lite",
    config=types.CreateCachedContentConfig(
        display_name="ainazarov_kb_v7",
        system_instruction=SYSTEM_PROMPT_WITH_KB,
        contents=[knowledge_base_content],
        ttl="3600s",
    ),
)

response = client.models.generate_content(
    model="gemini-3.5-flash-lite",
    contents="Сколько стоит абонемент?",
    config=types.GenerateContentConfig(cached_content=cache.name),
)
```

Тарификация складывается из: (1) количества кэшированных токенов по льготной ставке, (2) **платы за хранение в зависимости от TTL**, (3) обычных некэшированных input/output токенов.

⚠️ **Explicit caching НЕ поддерживается в Interactions API:** «The Interactions API only supports implicit caching. Explicit caching (manually creating and managing cache objects) is not supported in the Interactions API.» Нужен explicit → сидим на `generate_content`.

### 7.3. Когда окупается у нас

Наш системный промпт + база знаний (расписание, залы по районам, тренеры, правила, прайс) — ориентировочно 6–15k токенов. При 1000 диалогов в день × ~6 ходов = 6000 запросов × 10k токенов = 60M входных токенов/день только на промпт. Это ~$18/день по $0.30/1M на `gemini-3.5-flash-lite`.

- **Implicit** окупается сразу и бесплатен в настройке → **делаем в первую очередь**: фиксируем префикс, всё динамическое в конец.
- **Explicit** имеет смысл добавлять, только если после замера `usage_metadata.cached_content_token_count` окажется, что implicit-hit rate низкий. Иначе платим ещё и за хранение.

**Источники:**
- https://ai.google.dev/gemini-api/docs/caching
- https://ai.google.dev/gemini-api/docs/generate-content/caching

---

## 8. Safety settings

**Для нашей тематики это критично.** Бокс, кикбоксинг, удары, спарринги, «жёсткие тренировки», «нокаут» — классический источник false positive по категории насилия/опасного контента.

### 8.1. Категории (точные enum)

- `HARM_CATEGORY_HARASSMENT`
- `HARM_CATEGORY_HATE_SPEECH`
- `HARM_CATEGORY_SEXUALLY_EXPLICIT`
- **`HARM_CATEGORY_DANGEROUS_CONTENT`**

Все четыре настраиваемые.

> 🛑 **ИСПРАВЛЕНО (была критическая ошибка).** В более ранней редакции этого документа утверждалось, что правильное имя — `HARM_CATEGORY_DANGEROUS`, а `HARM_CATEGORY_DANGEROUS_CONTENT` — легаси из Vertex. **Всё ровно наоборот.** Проверено исполнением на google-genai 2.17.0:
>
> ```
> >>> types.HarmCategory.HARM_CATEGORY_DANGEROUS
> AttributeError: type object 'HarmCategory' has no attribute 'HARM_CATEGORY_DANGEROUS'
> >>> [a for a in dir(types.HarmCategory) if 'DANGER' in a]
> ['HARM_CATEGORY_DANGEROUS_CONTENT', 'HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT']
> ```
>
> Полный enum: `HARM_CATEGORY_UNSPECIFIED`, `HARM_CATEGORY_HARASSMENT`, `HARM_CATEGORY_HATE_SPEECH`, `HARM_CATEGORY_SEXUALLY_EXPLICIT`, `HARM_CATEGORY_DANGEROUS_CONTENT`, `HARM_CATEGORY_CIVIC_INTEGRITY`, `HARM_CATEGORY_JAILBREAK`, `HARM_CATEGORY_IMAGE_*`.
>
> **Обход через строку тоже не работает** — и это опаснее, потому что тихо: `types.SafetySetting(category="HARM_CATEGORY_DANGEROUS", threshold="OFF")` успешно конструируется, но `google/genai/_common.py:651` выдаёт `UserWarning: HARM_CATEGORY_DANGEROUS is not a valid HarmCategory` (воспроизведено), значение уходит как нераспознанное, и настройка **молча не применяется** — категория опасного контента, единственная критичная для школы бокса, остаётся с дефолтом вместо `OFF`.
>
> В таблице на https://ai.google.dev/gemini-api/docs/safety-settings стоит человекочитаемая метка «Dangerous», но все code-примеры используют полную форму (`types.HarmCategory.HARM_CATEGORY_HATE_SPEECH`, REST `"category": "HARM_CATEGORY_HATE_SPEECH"`); enum-имени `HARM_CATEGORY_DANGEROUS` в примерах нет.

Совет проверять на своём SDK остаётся верным — просто вывод из него надо делать правильный:

```python
[a for a in dir(types.HarmCategory) if 'DANGER' in a]
# ['HARM_CATEGORY_DANGEROUS_CONTENT', 'HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT']
```

### 8.2. Пороги (точные enum)

- `HARM_BLOCK_THRESHOLD_UNSPECIFIED`
- `BLOCK_LOW_AND_ABOVE` (самый строгий)
- `BLOCK_MEDIUM_AND_ABOVE`
- `BLOCK_ONLY_HIGH`
- `BLOCK_NONE`
- `OFF` (самый мягкий)

### 8.3. Дефолт

**«If the threshold is not set, the default block threshold is Off for Gemini 2.5 and 3 models.»**

То есть на наших моделях фильтры **по умолчанию уже выключены**. Это важный вывод: паниковать про false positive на «спарринг» заранее не нужно — но и полагаться на неявный дефолт в проде плохо (дефолты меняются). Ставим явно.

### 8.4. Код

```python
from google import genai
from google.genai import types

SAFETY = [
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,  threshold="OFF"),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,         threshold="OFF"),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,        threshold="BLOCK_ONLY_HIGH"),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,  threshold="BLOCK_MEDIUM_AND_ABOVE"),
]

response = client.models.generate_content(
    model="gemini-3.5-flash-lite",
    contents="Расскажи, как проходят спарринги у детей 10 лет",
    config=types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        safety_settings=SAFETY,
    ),
)
```

⚠️ Обратите внимание: блок `SAFETY` — это **константа уровня модуля**. С неверным именем категории он падал бы `AttributeError` **на импорте модуля**, то есть приложение не поднялось бы вообще (а не отвалилось бы на первом запросе). Это хорошая новость по сравнению со строковым вариантом, который не падает и молча не применяется.

Логика выбора: `HARM_CATEGORY_DANGEROUS_CONTENT` → `OFF`, потому что вся наша предметная область — про удары и контактный спорт. `HATE_SPEECH` и `SEXUALLY_EXPLICIT` держим включёнными — школа детская, репутационные риски выше, чем риск ложной блокировки.

### 8.5. Обязательная обработка блокировки

Даже с выключенными фильтрами ответ может быть пустым. **Никогда не читать `response.text` без проверки** — при блокировке он бросит исключение или вернёт None, и бот молча «зависнет» в WhatsApp.

```python
def safe_text(response) -> str | None:
    if getattr(response, "prompt_feedback", None) and response.prompt_feedback.block_reason:
        log.warning("prompt blocked: %s", response.prompt_feedback.block_reason)
        return None
    if not response.candidates:
        return None
    cand = response.candidates[0]
    if cand.finish_reason and str(cand.finish_reason) not in ("STOP", "FinishReason.STOP", "MAX_TOKENS", "FinishReason.MAX_TOKENS"):
        log.warning("candidate finish_reason=%s safety=%s", cand.finish_reason, cand.safety_ratings)
        return None
    return response.text
```

При `None` → отдаём вежливый фолбэк и эскалируем на живого менеджера.

**Источник:** https://ai.google.dev/gemini-api/docs/safety-settings

---

## 9. Structured output

Нужен для извлечения лид-данных (имя, телефон, возраст, район, интересующая группа, предпочитаемое время).

### 9.1. Legacy generateContent

Параметры: **`response_mime_type="application/json"`** + **`response_schema`**.

```python
from pydantic import BaseModel, Field
from typing import Literal
from google.genai import types

class Lead(BaseModel):
    full_name: str | None = Field(default=None, description="Имя клиента или родителя")
    phone: str | None = Field(default=None, description="Телефон в формате +7XXXXXXXXXX")
    athlete_age: int | None = Field(default=None, description="Возраст занимающегося")
    district: str | None = Field(default=None, description="Район Костаная")
    sport: Literal["boxing", "kickboxing", "unknown"] = "unknown"
    preferred_time: Literal["morning", "afternoon", "evening", "unknown"] = "unknown"
    language: Literal["ru", "kk"]
    ready_to_book: bool = Field(description="Явно согласился на пробное занятие")

response = client.models.generate_content(
    model="gemini-3.5-flash-lite",
    contents=dialog_transcript,
    config=types.GenerateContentConfig(
        system_instruction="Извлеки данные лида. Не выдумывай значения — ставь null, если не сказано явно.",
        response_mime_type="application/json",
        response_schema=Lead,          # можно передать pydantic-класс напрямую
        thinking_config=types.ThinkingConfig(thinking_level="minimal"),
    ),
)

lead = Lead.model_validate_json(response.text)
# либо, если SDK распарсил сам:
lead = response.parsed
```

SDK принимает pydantic-класс напрямую; альтернативно `Lead.model_json_schema()`.

### 9.2. Interactions API

`response_format` переехал на **верхний уровень** (из `GenerateContentConfig`):

```python
response_format={
    "type": "text",
    "mime_type": "application/json",
    "schema": Lead.model_json_schema(),
}
```

### 9.3. Поддерживаемая схема

**Типы:** `string`, `number`, `integer`, `boolean`, `object`, `array`, `null`
**Свойства:** `title`, `description`, `properties`, `required`, `additionalProperties`, `enum`, `format`, `minimum`, `maximum`, `items`, `prefixItems`, `minItems`, `maxItems`

Enum задаются через `Literal[...]` в pydantic или `"enum": [...]` в схеме.

### 9.4. Грабли

- «Not all JSON Schema features are supported; unsupported properties are ignored» — **молча игнорируются**. Ваш `pattern` для телефона не сработает, валидируйте регуляркой сами.
- «Very large or deeply nested schemas may be rejected» — держать схему плоской, без вложенных объектов глубже 2 уровней.
- **«While output guarantees syntactically valid JSON, semantic correctness isn't assured»** — JSON будет валидный, но содержимое может быть выдумано. **Обязательно `Lead.model_validate_json()` в try/except + бизнес-валидация телефона и возраста.**
- Для Gemini 2.0 требовался явный `propertyOrdering`. Для 3.x в доке не упоминается. ⚠️ **НЕ ПОДТВЕРЖДЕНО** для 3.x.
- Structured output и function calling **лучше не смешивать в одном вызове**. Извлечение лида делать отдельным «тихим» вызовом после диалога, не в основном чат-турне.

**Источники:**
- https://ai.google.dev/gemini-api/docs/structured-output
- https://ai.google.dev/gemini-api/docs/generate-content/structured-output

---

## 10. Ошибки, лимиты, retry

### 10.1. Коды ошибок

| Код | HTTP | Причина | Действие |
|---|---|---|---|
| `invalid_request` | 400 | Кривой payload/параметры | Чинить код, **не ретраить** |
| `failed_precondition` | 400 | Не выполнено предусловие (напр. выключен биллинг) | Проверить биллинг/проект |
| `parameter_unknown` | 400 | Неизвестный параметр | Убрать параметр |
| `authentication` | 401 | Ключ отсутствует/невалиден/истёк | Проверить ключ |
| `permission_denied` | 403 | Нет прав | Проверить права ключа, **не ретраить** |
| `model_not_found` | 404 | Модели нет | Проверить model ID (см. shut down модели!) |
| `aborted` | 409 | Конфликт/конкурентность | Ретрай на уровне приложения |
| `out_of_range` | 416 | Параметр вне диапазона | Валидировать значения |
| `rate_limit_exceeded` | **429** | Превышен RPM/TPM | **«Wait and retry with exponential backoff»** |
| `quota_exceeded` | **429** | Превышена дневная квота (RPD) | Ждать сброса или повышать квоту — **backoff не поможет** |
| `cancelled` | 499 | Клиент оборвал запрос | — |
| `api_error` | **500** | Внутренняя ошибка | Ретрай |
| `unimplemented` | 501 | Операция не поддерживается | Чинить код |
| `service_unavailable` | **503** | Перегрузка/недоступность | **«Wait and retry with exponential backoff»** |
| `deadline_exceeded` | **504** | Таймаут | Увеличить client deadline |

⚠️ Две разные ошибки под 429: минутный лимит (лечится backoff) и дневная квота (не лечится). Различать по коду ошибки, иначе будете бесполезно долбить API до полуночи.

### 10.2. Встроенный retry в SDK — **есть**

Python SDK «automatically retries transient errors up to four times with an initial delay of approximately 1 second and a maximum delay of 60 seconds».

Настраивается через `types.HttpRetryOptions`:

| Параметр | Дефолт |
|---|---|
| `attempts` | 5 |
| `initial_delay` | 1.0 с |
| `max_delay` | 60 с |
| `exp_base` | 2 |
| `jitter` | 1 |
| `http_status_codes` | список кодов для ретрая |

```python
from google import genai
from google.genai import types

client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"],
    http_options=types.HttpOptions(
        timeout=30_000,                     # мс
        retry_options=types.HttpRetryOptions(
            attempts=4,
            initial_delay=1.0,
            max_delay=20.0,
            exp_base=2,
            jitter=1,
            http_status_codes=[429, 500, 503, 504],
        ),
    ),
)
```

⚠️ Точные имена полей `HttpOptions.timeout` / `retry_options` — ⚠️ **ЧАСТИЧНО ПОДТВЕРЖДЕНО**: параметры `HttpRetryOptions` подтверждены (issue #1149 в python-genai + Google Cloud docs), но README SDK их не документирует явно. **Проверить на своей версии** через `inspect.signature(types.HttpOptions)` перед деплоем.

Официальный образец собственного backoff (если не полагаться на SDK):

```python
import time
import random

def retry_with_backoff(func, max_retries=5, base_delay=1):
    """Retry with exponential backoff for rate limits and server errors."""
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                print(f"Attempt {attempt + 1} failed. Retrying in {delay:.2f}s...")
                time.sleep(delay)
            else:
                raise
```

Рекомендации доки: ждать ~1с, потом 2/4/8с; добавлять jitter; ретраить только `429`, `408`, `5xx`; **не ретраить `400` и `403`**.

### 10.3. Тарифные тиры

- **Free Tier** — без биллинга
- **Tier 1** — привязан активный биллинг, потолок трат $250
- **Tier 2** — потрачено $100+ и прошло 3 дня с первого платежа, потолок $2 000
- **Tier 3** — потрачено $1 000+ и прошло 30 дней, потолок $20 000–$100 000+

Дополнительно есть **spend-based лимит в скользящем окне 10 минут**: Tier 1 — $10, Tier 2/3 — $200.

Лимиты меряются по трём осям: **RPM / TPM / RPD**. «Exceeding any of them will trigger a rate limit error.»

⚠️ **НЕ ПОДТВЕРЖДЕНО (точные числа):** страница rate-limits больше не публикует таблицу по моделям — «Specified rate limits are not guaranteed and actual capacity may vary», отсылает смотреть **свои** лимиты в AI Studio. Числа из сторонних блогов (~15 RPM / 1500 RPD на free, 150–300 RPM на Tier 1) — **не официальный источник, не закладываться**.

**Практический вывод:** для прода — минимум Tier 1. На free tier бота в WhatsApp не запускать: и RPM низкий, и данные `store` хранятся всего 1 день.

**Источники:**
- https://ai.google.dev/gemini-api/docs/api-errors
- https://ai.google.dev/gemini-api/docs/troubleshooting
- https://ai.google.dev/gemini-api/docs/rate-limits
- https://github.com/googleapis/python-genai/issues/1149

---

## 11. Подсчёт токенов и стоимости

### 11.1. Предварительный подсчёт

```python
total = client.models.count_tokens(
    model="gemini-3.5-flash-lite",
    contents="why is the sky blue?",
)
print("total_tokens:", total.total_tokens)
```

Асинхронно: `await client.aio.models.count_tokens(...)`.

### 11.2. Фактическое потребление из ответа

**Legacy generateContent** — `response.usage_metadata`:

```python
um = response.usage_metadata
print(um.prompt_token_count, um.candidates_token_count, um.total_token_count)
print(um.cached_content_token_count)   # попадание в кэш
print(um.thoughts_token_count)         # токены размышлений
```

**Interactions API** — `interaction.usage` с полями:
`total_input_tokens`, `total_output_tokens`, `total_thought_tokens`, `total_cached_tokens`, `total_tool_use_tokens`, `total_tokens`.

### 11.3. Оценка стоимости

Ориентир из доки: **«a token is equivalent to about 4 characters. 100 tokens is equal to about 60-80 English words.»**

⚠️ Для русского и особенно **казахского это соотношение хуже** — кириллица и агглютинативная морфология казахского дают больше токенов на символ. Не полагаться на 4 символа/токен; мерить `count_tokens` на реальных казахских репликах.

```python
PRICES = {  # USD за 1 токен, Paid Standard
    "gemini-3.5-flash-lite": (0.30 / 1e6, 2.50 / 1e6),
    "gemini-3.1-flash-lite": (0.25 / 1e6, 1.50 / 1e6),
    "gemini-2.5-flash-lite": (0.10 / 1e6, 0.40 / 1e6),
    "gemini-3.6-flash":      (1.50 / 1e6, 7.50 / 1e6),
}

def turn_cost(model: str, um) -> float:
    pin, pout = PRICES[model]
    cached = getattr(um, "cached_content_token_count", 0) or 0
    billed_in = (um.prompt_token_count or 0) - cached          # кэш дешевле, тут консервативно
    thoughts = getattr(um, "thoughts_token_count", 0) or 0
    billed_out = (um.candidates_token_count or 0) + thoughts   # thinking считается как output
    return billed_in * pin + billed_out * pout
```

**Записывать `usage_metadata` в БД на каждый вызов** — без этого невозможно понять, окупается ли кэш и не разъедает ли thinking маржу.

**Грубая прикидка для нашего бота** (`gemini-3.5-flash-lite`, промпт 10k токенов, ответ ~250 токенов, 6 ходов на диалог):
- вход: 6 × 10 250 ≈ 61 500 токенов ≈ $0.018
- выход: 6 × 250 = 1 500 токенов ≈ $0.004
- **≈ $0.022 за диалог** без кэша; при хорошем implicit-hit — заметно меньше.
- 1 000 диалогов/день ≈ **$22/день ≈ $660/мес**. На `gemini-3.1-flash-lite` ощутимо ниже за счёт выхода.

Это тот самый расчёт, ради которого стоит вкладываться в кэширование префикса.

**Источники:**
- https://ai.google.dev/gemini-api/docs/tokens
- https://ai.google.dev/gemini-api/docs/pricing

---

## 12. Стриминг

Wazzup24 отдаёт сообщение целиком — **для WhatsApp/Instagram стриминг не нужен**. Понадобится для будущего веб-виджета на сайте.

### Legacy generateContent

```python
response = client.models.generate_content_stream(
    model="gemini-3.5-flash-lite",
    contents=["Explain how AI works"],
)
for chunk in response:
    print(chunk.text, end="")
```

Асинхронно:

```python
async for chunk in await client.aio.models.generate_content_stream(
    model="gemini-3.5-flash-lite", contents=contents, config=cfg
):
    if chunk.text:
        yield chunk.text
```

### Interactions API

```python
stream = client.interactions.create(
    model="gemini-3.6-flash",
    input="Explain how AI works",
    stream=True,
)
for event in stream:
    if event.event_type == "step.delta":
        if event.delta.type == "text":
            print(event.delta.text, end="")
```

Interactions-стрим — событийный, по типизированным шагам (SSE), включая посимвольный стриминг аргументов function call. Для UI с индикатором «бот думает / бот ищет зал» это плюс.

### FastAPI SSE

```python
from fastapi.responses import StreamingResponse

@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    async def gen():
        async for chunk in await client.aio.models.generate_content_stream(
            model="gemini-3.5-flash-lite",
            contents=await build_contents(req),
            config=cfg,
        ):
            if chunk.text:
                yield f"data: {json.dumps({'t': chunk.text}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
```

⚠️ При стриминге `usage_metadata` приходит **в последнем чанке** — аккумулировать оттуда, а не с первого.
⚠️ Стриминг + function calling: вызов функции приходит целиком в чанке, но текст до него уже улетел клиенту. Логику tool-calls в стриминге проектировать отдельно.

**Источники:**
- https://ai.google.dev/gemini-api/docs/generate-content/text-generation
- https://ai.google.dev/gemini-api/docs/text-generation
- https://github.com/googleapis/python-genai

---

## 13. Рекомендуемая архитектура для нашего бота

**Решение: legacy `generate_content` + ручная сборка `contents` + история в нашей БД.**

Обоснование:
1. `generate_content` «remains fully supported» — не легаси в смысле «скоро выключат».
2. Нам не нужны агентные фичи Interactions (background tasks, Deep Research, серверные тулы).
3. Explicit caching **недоступен** в Interactions API, а он может понадобиться.
4. Персданные клиентов не уезжают на хранение к Google на 55 дней (`store`).
5. История и так нужна у нас — для операторов, эскалации и аналитики.

Если через полгода понадобится агентика — миграция на Interactions делается локально в одном модуле-адаптере. **Поэтому: всю работу с Gemini спрятать за собственным интерфейсом `LLMClient`, не разбрасывать `client.models.generate_content` по коду.**

### Итоговый конфиг

```python
from google import genai
from google.genai import types

MODEL_PRIMARY  = "gemini-3.5-flash-lite"
MODEL_FALLBACK = "gemini-3.1-flash-lite"

client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"],
    http_options=types.HttpOptions(timeout=30_000),
)

BASE_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,        # стабильный префикс → implicit cache
    tools=TOOLS,
    tool_config=types.ToolConfig(
        function_calling_config=types.FunctionCallingConfig(mode="AUTO")
    ),
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    thinking_config=types.ThinkingConfig(thinking_level="minimal"),
    safety_settings=SAFETY,
    max_output_tokens=1024,                  # ответы в мессенджер должны быть короткими
    # temperature НЕ ЗАДАЁМ — для Gemini 3.x рекомендован дефолт 1.0
)
```

### Фолбэк между моделями

```python
async def generate_with_fallback(contents, cfg):
    for model in (MODEL_PRIMARY, MODEL_FALLBACK):
        try:
            return await client.aio.models.generate_content(
                model=model, contents=contents, config=cfg
            )
        except Exception as exc:
            log.warning("model %s failed: %s", model, exc)
    raise RuntimeError("all models failed")
```

При полном отказе — сообщение «сейчас передам вас менеджеру» + эскалация.

### Определение языка

Не городить отдельный детектор. Правило в `system_instruction`: «Отвечай ТОЛЬКО на языке последнего сообщения пользователя. Казахский — только литературный казахский, без вставок русских слов, кроме имён собственных.» Плюс поле `language: Literal["ru","kk"]` в структурированном извлечении лида — для маршрутизации на казахоязычного менеджера.

⚠️ Отдельно проверить смешанные реплики («Сәлем, а сколько стоит?») — типичная для Костаная ситуация, на которой модели часто «залипают» в один язык.

---

## 14. Топ граблей

1. **`system_instruction`, `tools` и `generation_config` — interaction-scoped в Interactions API.** Их надо слать в КАЖДОМ вызове, даже с `previous_interaction_id`. Забыл — бот на втором сообщении теряет личность и все инструменты.

2. **Thought signatures нельзя терять при stateless-истории.** «In stateless mode you must include thought blocks with their signatures in subsequent requests to validate authenticity». Класть в историю **весь** `candidate.content`, а не только `.text`. Хранить в БД полный `model_dump()` объекта `Content`, а не `{"role", "text"}`. Это самая коварная грабля: ломается не сразу, а на многоходовых диалогах с function calling.

3. **`thinking_budget` мёртв → `thinking_level`; `temperature` для Gemini 3.x не трогать.** «Strongly recommend keeping the temperature parameter at its default value of 1.0», изменение «may lead to unexpected behavior, such as looping or degraded performance». Привычка ставить `temperature=0.3` для «предсказуемости» здесь вредна. Детерминизм цен обеспечиваем **функцией-калькулятором**, а не температурой.

4. **Structured output гарантирует только синтаксис, не смысл.** «Output guarantees syntactically valid JSON, semantic correctness isn't assured», а неподдерживаемые ключевые слова схемы (`pattern`, `$ref`, `oneOf`) **молча игнорируются**. Телефон и возраст валидировать своим кодом, иначе в CRM поедут выдуманные номера.

5. **Кэш ловится по префиксу — любая динамика в начале промпта убивает экономию.** Дата, имя клиента, район — только в конец `contents`. Порог implicit-кэша ~2–4k токенов, а попадание в кэш надо логировать с первого дня, иначе счёт вырастет в разы незаметно. **Поле зависит от API:** на нашем legacy-пути это `response.usage_metadata.cached_content_token_count`; `usage.total_cached_tokens` — это Interactions API, у `GenerateContentResponse` атрибута `usage` вообще нет (см. 7.1).

**Бонусные, тоже больно:**

6. **Два разных 429.** `rate_limit_exceeded` (минутный, лечится backoff) vs `quota_exceeded` (дневной, backoff бесполезен). Различать по коду ошибки.

7. **`response.text` без проверки `finish_reason`/`prompt_feedback`** — при блокировке или пустом кандидате бот молча замолкает в WhatsApp. Всегда через `safe_text()`.

8. **Модели 2.0 выключены.** `gemini-2.0-flash`, `gemini-2.0-flash-lite`, `gemini-3-pro-preview` — shut down. Любой скопированный из туториала 2025 года model ID сломается.

9. **Автоматический function calling исполняет функции внутри SDK** — для записи лида в CRM это недопустимо. `AutomaticFunctionCallingConfig(disable=True)` + ручной цикл.

10. **Результаты функций возвращаются с ролью `"user"`**, dict-ом, и с `id`+`name` при параллельных вызовах. **`types.Part.from_function_response()` для этого не годится — у него нет параметра `id`** и он молча оставляет `id=None`. Собирать вручную: `types.Part(function_response=types.FunctionResponse(id=call.id, name=call.name, response=result))`.

11. **`HARM_CATEGORY_DANGEROUS` не существует.** Правильное имя — `HARM_CATEGORY_DANGEROUS_CONTENT`. Через enum — `AttributeError` на импорте; через строку — падения нет, но `UserWarning: ... is not a valid HarmCategory` и настройка молча игнорируется. Для школы бокса это ровно та категория, которую нельзя терять.

12. **Режимов function calling четыре, а не три:** `AUTO` / `ANY` / `NONE` / **`VALIDATED`**. `VALIDATED` («Model ensures function schema adherence») часто лучше жёсткого `ANY` на шаге извлечения лид-данных.

---

## Приложение: чек-лист перед продом

- [ ] `google-genai>=2.17.0,<3.0.0` в requirements, пин зафиксирован
- [ ] Все вызовы через `client.aio.*`
- [ ] `thinking_level="minimal"` задан ЯВНО (не полагаемся на дефолт)
- [ ] `temperature` / `top_p` / `top_k` НЕ задаются
- [ ] `safety_settings` заданы явно, `HARM_CATEGORY_DANGEROUS_CONTENT` = `OFF`
- [ ] Имя категории `HARM_CATEGORY_DANGEROUS_CONTENT` проверено на установленной версии SDK (`[a for a in dir(types.HarmCategory) if 'DANGER' in a]`), в логах старта нет `UserWarning: ... is not a valid HarmCategory`
- [ ] `FunctionResponse` собирается вручную с `id=call.id` (НЕ через `Part.from_function_response`, там нет `id`)
- [ ] `automatic_function_calling` отключён, цикл ручной, лимит витков ≤ 5
- [ ] История хранится как полный дамп `types.Content` (с thought signatures)
- [ ] Обрезка истории — только по границе завершённого tool-цикла
- [ ] `safe_text()` вместо голого `response.text`
- [ ] `usage_metadata` пишется в БД на каждый вызов
- [ ] `usage_metadata.cached_content_token_count` мониторится (НЕ `usage.total_cached_tokens` — это Interactions API), префикс промпта стабилен
- [ ] Фолбэк-модель настроена, эскалация на менеджера при полном отказе
- [ ] Retry: 429/500/503/504 — да; 400/403 — нет
- [ ] Тариф минимум Tier 1, лимиты сверены в AI Studio
- [ ] A/B на 50 казахских репликах проведён, качество зафиксировано
- [ ] Вся работа с Gemini — за интерфейсом `LLMClient` (задел на миграцию в Interactions API)
- [ ] Юридически закрыт вопрос хранения ПДн (мы на `generate_content`, `store` не используется)

---

## Все источники

**Официальные (ai.google.dev):**
- [Quickstart](https://ai.google.dev/gemini-api/docs/quickstart)
- [Libraries / SDK](https://ai.google.dev/gemini-api/docs/libraries)
- [Pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Models](https://ai.google.dev/gemini-api/docs/models)
- [Gemini 3.5 Flash-Lite model card](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)
- [Gemini 3.1 Flash-Lite model card](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite)
- [Gemini 3 Developer Guide](https://ai.google.dev/gemini-api/docs/gemini-3)
- [What's new in Gemini 3.5 Flash](https://ai.google.dev/gemini-api/docs/interactions/whats-new-gemini-3.5)
- [Interactions API overview](https://ai.google.dev/gemini-api/docs/interactions-overview)
- [Migrating to the Interactions API](https://ai.google.dev/gemini-api/docs/migrate-to-interactions)
- [Function calling](https://ai.google.dev/gemini-api/docs/function-calling)
- [Function calling (legacy generateContent)](https://ai.google.dev/gemini-api/docs/generate-content/function-calling)
- [Text generation](https://ai.google.dev/gemini-api/docs/text-generation)
- [Text generation (legacy)](https://ai.google.dev/gemini-api/docs/generate-content/text-generation)
- [Thinking](https://ai.google.dev/gemini-api/docs/thinking)
- [Caching](https://ai.google.dev/gemini-api/docs/caching)
- [Caching (legacy)](https://ai.google.dev/gemini-api/docs/generate-content/caching)
- [Safety settings](https://ai.google.dev/gemini-api/docs/safety-settings)
- [Structured output](https://ai.google.dev/gemini-api/docs/structured-output)
- [Structured output (legacy)](https://ai.google.dev/gemini-api/docs/generate-content/structured-output)
- [API errors](https://ai.google.dev/gemini-api/docs/api-errors)
- [Troubleshooting](https://ai.google.dev/gemini-api/docs/troubleshooting)
- [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
- [Tokens](https://ai.google.dev/gemini-api/docs/tokens)

**SDK:**
- [python-genai reference](https://googleapis.github.io/python-genai/)
- [python-genai GitHub](https://github.com/googleapis/python-genai)
- [python-genai issue #1149 — HttpRetryOptions defaults](https://github.com/googleapis/python-genai/issues/1149)
- [google-genai on PyPI](https://pypi.org/project/google-genai/)

**Прочее:**
- [gemini-skills: Interactions API migration reference](https://github.com/google-gemini/gemini-skills/blob/main/skills/gemini-interactions-api/references/migration.md)
- [Gemini 3.5 Flash-Lite model card (DeepMind)](https://deepmind.google/models/model-cards/gemini-3-5-flash-lite/)

---

## Результаты независимой верификации

**Дата верификации:** 2026-08-09
**Метод:** (1) интроспекция реально установленного пакета `google-genai==2.17.0` в чистом venv — `dir()`, `inspect.signature()`, `model_fields`, перехват `warnings`; (2) повторный фетч первоисточников на ai.google.dev и CHANGELOG репозитория `googleapis/python-genai`. По памяти не проверялось ничего.

| Утверждение (в исходной редакции) | Вердикт | Что на самом деле |
|---|---|---|
| «В текущей доке категория называется `HARM_CATEGORY_DANGEROUS`, а `HARM_CATEGORY_DANGEROUS_CONTENT` — легаси из Vertex» | 🛑 **НЕВЕРНО (blocker)** | Ровно наоборот. `types.HarmCategory.HARM_CATEGORY_DANGEROUS` → `AttributeError`. Реально существуют `HARM_CATEGORY_DANGEROUS_CONTENT` и `HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT`. Исправлено в 8.1. |
| `types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS, threshold="OFF")` в коде 8.4 и в чек-листе | 🛑 **НЕВЕРНО (blocker)** | Код падает `AttributeError` **на импорте модуля** (SAFETY — константа уровня модуля), т.е. приложение не стартует. «Починка» заменой на строку хуже: `UserWarning: HARM_CATEGORY_DANGEROUS is not a valid HarmCategory`, падения нет, настройка молча не применяется, и единственная критичная для школы бокса категория остаётся с дефолтом. Исправлено в 8.1, 8.4 и в двух пунктах чек-листа. |
| «Add `id` and matching `name` to all `FunctionResponse` parts» + грабля №10 | ⚠️ **ВЕРНО ПО СУТИ, НО КОД ДОКУМЕНТА ЕГО НАРУШАЛ (high)** | Требование доки подтверждено: «Always include the exact `id` from the `function_call` in your `function_response`». Но `types.Part.from_function_response` не имеет параметра `id` (`(*, name, response, parts=None)`) и оставляет `function_response.id = None` — а именно он использовался в «рекомендуемом для нас» цикле 4.3. При параллельных вызовах сопоставление ломалось ровно так, как предупреждала 4.6. Рабочая форма: `types.Part(function_response=types.FunctionResponse(id=call.id, name=call.name, response=result))` (проверено — `id` проставляется). Исправлено в 4.3, 4.6, грабле №10, чек-листе. |
| «Проверка попадания в кэш: `usage.total_cached_tokens` в ответе» (7.1, грабля №5) | ❌ **НЕВЕРНО (medium)** | У `GenerateContentResponse` **нет** атрибута `usage`. Поля: `sdk_http_response, candidates, create_time, model_version, prompt_feedback, response_id, usage_metadata, model_status, automatic_function_calling_history, parsed`. Кэш на legacy-пути: `usage_metadata.cached_content_token_count`. `total_cached_tokens` — поле `google.genai.interactions.Usage`. Раздел 11.2 разводил их правильно — противоречие было внутреннее. Исправлено в 7.1, 7.3, грабле №5, чек-листе. |
| «Interactions API требует `google-genai >= 2.0.0`» | ❌ **НЕВЕРНО (medium)** | Interactions API появился в **1.55.0** (11.12.2025, «Add the Interactions API»). 2.0.0 (07.05.2026) внесла breaking changes *внутри* interactions (steps, переименование SSE-событий в `interaction.created`/`interaction.completed`, полиморфный `response_format`) с пометкой «The breaking changes are only in interactions. `GenerateContent` usage in unaffected». Пин `>=2.0.0` практически защищён и разумен — но по другой причине. Формулировка исправлена в разделе 1. |
| «`tool_config.function_calling_config.mode`, значения `AUTO` / `ANY` / `NONE`» | ❌ **НЕПОЛНО (medium)** | Режимов **четыре**: `dir(types.FunctionCallingConfigMode)` → `ANY, AUTO, MODE_UNSPECIFIED, NONE, VALIDATED`. `VALIDATED` документирован («Model ensures function schema adherence»; дефолт при комбинации инструментов), в Interactions — `tool_choice: validated`, что подтверждается `interactions.AllowedTools.mode -> Literal['auto','any','none','validated']`. Пропущенный режим — как раз кандидат на шаг «извлеки лид-данные» вместо жёсткого `ANY`. Добавлен в 4.2 и в граблю №12. |
| «Free tier доступен почти для всех перечисленных (кроме `gemini-3.1-pro-preview`)» | ⚠️ **СПОРНО — НЕ ПОДТВЕРЖДЕНО** | Верификация утверждала, что free tier недоступен **и** для `gemini-2.5-pro`. Повторный фетч pricing-страницы (два независимых запроса, 2026-08-09) даёт для 2.5 Pro free tier «Free of charge», для 3.1 Pro Preview — «Not available». Расхождение, вероятно, из-за табличного/табового рендера страницы. По `gemini-3.1-pro-preview` «Not available» подтверждено обеими сторонами. Помечено в 2.1 как требующее сверки в AI Studio; на выбор моделей проекта не влияет. |
| «MMMLU 88.9%, best-in-class translation… non-Latin scripts» со ссылкой на model card `gemini-3.1-flash-lite` | ⚠️ **НЕ ПОДТВЕРЖДЕНО** | На указанной странице ни цифры, ни цитаты нет — подтверждено собственным фетчем: там только лимиты 1 048 576 / 65 536, stable с мая 2026 и use-cases («Fast, cheap, high-volume translation, such as processing chat messages, reviews, and support tickets at scale»). Цифра гуляет по третьим источникам с атрибуцией к DeepMind model card / eval-PDF. Помечено в 2.3; вывод документа не меняется — казахский там и так честно помечен как НЕ ПОДТВЕРЖДЕНО с требованием A/B. |
| Список поддержанных свойств схемы: `title`, `description`, `properties`, `required`, … (4.6 и 9.3) | ✅ **ПОДТВЕРЖДЕНО при перепроверке** | Верификация помечала это как UNVERIFIED (якобы `title` в перечне нет). Собственный фетч https://ai.google.dev/gemini-api/docs/structured-output показывает обратное: там явно документированы `'title': A short description of a property` и `'description': A longer and more detailed description of a property`, плюс группировка по типам (object → `properties`, `required`, `additionalProperties`; string → `enum`, `format`; number/integer → `enum`, `minimum`, `maximum`; array → `items`, `prefixItems`, `minItems`, `maxItems`) и типы `string, number, integer, boolean, object, array, null`. **Правка не вносилась.** Практического риска в любом случае нет: неподдержанные ключи, по той же доке, молча игнорируются. |

### Что осталось проверить руками перед продом

- Free tier для `gemini-2.5-pro` — сверить в AI Studio, а не по доке (см. 2.1).
- MMMLU/multilingual-цифры по `gemini-3.1-flash-lite` — брать из DeepMind model card либо не использовать в аргументации (см. 2.3).
- После правки safety-настроек — убедиться, что в логах старта **нет** `UserWarning: ... is not a valid HarmCategory`. Это единственный признак того, что категория ушла нераспознанной.
- Прогнать один параллельный tool-call (например `find_nearest_gym` + `calculate_price` в одном ходу) и убедиться, что `function_response.id` заполнены и совпадают с `function_call.id`.

**Источники верификации:**
- интроспекция `google-genai==2.17.0` (чистый venv, Python 3.13)
- https://ai.google.dev/gemini-api/docs/safety-settings
- https://ai.google.dev/gemini-api/docs/generate-content/function-calling
- https://ai.google.dev/gemini-api/docs/function-calling
- https://ai.google.dev/gemini-api/docs/structured-output
- https://ai.google.dev/gemini-api/docs/caching
- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite
- https://raw.githubusercontent.com/googleapis/python-genai/main/CHANGELOG.md
