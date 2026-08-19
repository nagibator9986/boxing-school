#!/usr/bin/env python
"""Ручные проверки трёх подсистем: калькулятор, язык, нормализация DATABASE_URL.

Эталон цен — docs/CONTENT-AUDIT.md §3 (строки 60–140). Скрипт печатает
фактические числа и помечает расхождения с эталоном.

Запуск::

    .venv/bin/python scripts/verify.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.update(
    APP_ENV="local",
    DATABASE_URL="sqlite+aiosqlite:///./data/verify.db",
    STATE_BACKEND="memory",
    REDIS_URL="",
    GEMINI_API_KEY="",
    WAZZUP_API_KEY="verify",
    WAZZUP_WEBHOOK_SECRET="verify-secret-verify-secret-12",
    LOG_LEVEL="ERROR",
)

from app.config import get_settings, normalize_database_url  # noqa: E402
from app.core.language import detect  # noqa: E402
from app.kb import loader as kb_loader  # noqa: E402
from app.tools.pricing import calculate_price  # noqa: E402
from app.types import ChannelKind, Language, ToolContext  # noqa: E402

failures: list[str] = []


def check(label: str, actual: Any, expected: Any) -> None:
    """Печатает факт и сверяет с эталоном."""
    ok = actual == expected
    mark = "OK " if ok else "FAIL"
    print(f"  [{mark}] {label}: {actual}" + ("" if ok else f"  (ожидалось {expected})"))
    if not ok:
        failures.append(f"{label}: {actual} != {expected}")


def make_ctx(snapshot: Any) -> ToolContext:
    """Минимальный ToolContext для чистых инструментов (сервисы не задействованы)."""
    return ToolContext(
        conversation_id=uuid4(),
        conv_key="verify",
        channel=ChannelKind.WHATSAPP,
        channel_id="ch",
        chat_id="77010000000",
        lang=Language.RU,
        kb=snapshot,
        kb_hash=kb_loader.current_hash(),
        now=datetime.now(timezone.utc),
        correlation_id="verify",
        services=None,  # type: ignore[arg-type]  # чистый калькулятор в сервисы не ходит
    )


async def check_pricing() -> Any:
    """Калькулятор: город и райцентр, 1/2/3 ребёнка. Эталон — CONTENT-AUDIT §3."""
    print("\n=== КАЛЬКУЛЯТОР (эталон: CONTENT-AUDIT.md §3) ===")
    settings = get_settings()
    snapshot = await kb_loader.load(
        settings.kb_dir,
        media_dir=settings.media_dir,
        schema_version=settings.kb_schema_version,
    )
    kb_loader.swap(snapshot)
    ctx = make_ctx(snapshot)

    async def total(scope: str, plan: str, n: int, **kw: Any) -> Any:
        res = await calculate_price(ctx, scope=scope, plan=plan, children_count=n, **kw)
        if not res.ok:
            failures.append(f"calculate_price({scope},{plan},{n}) -> ошибка {res.error}")
            print(f"  [FAIL] {scope}/{plan}/{n} детей -> ОШИБКА {res.error}")
            return None
        return res

    print("\n-- Костанай (город), СТАНДАРТНЫЙ 25 000 ₸, скидки 2-й −10 %, 3-й −15 % --")
    for n, expected in ((1, 25000), (2, 47500), (3, 68750)):
        res = await total("city", "standard", n)
        if res is None:
            continue
        data = res.data
        print(f"     разбивка: {data.get('per_child')}")
        check(f"город standard, {n} реб.", data.get("total"), expected)

    print("\n-- Костанай, ГИБКИЙ 30 000 ₸ --")
    for n, expected in ((1, 30000), (2, 57000), (3, 82500)):
        res = await total("city", "flexible", n)
        if res is not None:
            check(f"город flexible, {n} реб.", res.data.get("total"), expected)

    print("\n-- Костанай, РАЗОВЫЕ 3 200 ₸ --")
    res = await total("city", "single", 1, single_sessions=12)
    if res is not None:
        check("город single ×12", res.data.get("total"), 38400)

    print("\n-- Райцентр: СТАНДАРТНЫЙ 10 000 ₸; при 2+ детях семейный 8 000 ₸/ребёнка --")
    for n, expected in ((1, 10000), (2, 16000), (3, 24000)):
        res = await total("region", "standard", n)
        if res is None:
            continue
        print(f"     разбивка: {res.data.get('per_child')}")
        check(f"райцентр standard, {n} реб.", res.data.get("total"), expected)

    return snapshot


def check_language(snapshot: Any) -> None:
    """Язык: 10 реплик — чистые, смешанная, транслит, «салем», эмодзи.

    По контракту приветствие отбрасывается и язык берётся по смысловой части:
    «Сәлем! Скажите цену» — это русский вопрос с казахской вежливостью.
    """
    print("\n=== ЯЗЫК (app.core.language.detect) ===")
    lex = snapshot.lexicon
    cases: list[tuple[str, Language, str]] = [
        ("Здравствуйте, сколько стоит абонемент?", Language.RU, "чистый русский"),
        ("Добрый день! Хочу записать ребёнка на бокс", Language.RU, "чистый русский 2"),
        ("Сәлеметсіз бе, бала жаттығуға қанша тұрады?", Language.KK, "чистый казахский"),
        ("Балама жазылғым келеді, қай уақытта сабақ бар?", Language.KK, "чистый казахский 2"),
        ("Сәлем! Скажите пожалуйста цену", Language.RU, "смешанная: каз. приветствие + рус. вопрос"),
        ("Salem, balany jazgym keledi", Language.KK, "транслит казахского"),
        ("салем", Language.KK, "«салем» без диакритики"),
        ("Salam, skolko stoit?", Language.RU, "транслит русского"),
        ("👋🥊", Language.RU, "только эмодзи -> дефолт"),
        ("Сәлем 👋 қанша тұрады? 🥊", Language.KK, "казахский с эмодзи"),
    ]
    for text, expected, label in cases:
        d = detect(text, lexicon=lex)
        actual = d.lang
        ok = actual == expected
        mark = "OK " if ok else "FAIL"
        print(
            f"  [{mark}] {label}: {text!r} -> {actual.value} "
            f"(source={d.source}, conf={d.confidence}, bridge={d.needs_bridge})"
        )
        if not ok:
            failures.append(f"язык {text!r}: {actual} != {expected}")


def check_dsn() -> None:
    """Нормализация DATABASE_URL: оба postgres-префикса -> postgresql+asyncpg."""
    print("\n=== НОРМАЛИЗАЦИЯ DATABASE_URL ===")
    cases = [
        (
            "postgres://u:p@host:5432/db",
            "postgresql+asyncpg://u:p@host:5432/db",
        ),
        (
            "postgresql://u:p@host:5432/db",
            "postgresql+asyncpg://u:p@host:5432/db",
        ),
        (
            "postgresql+asyncpg://u:p@host:5432/db",
            "postgresql+asyncpg://u:p@host:5432/db",
        ),
        (
            "postgres://u:p%40word@host:5432/db?sslmode=require",
            "postgresql+asyncpg://u:p%40word@host:5432/db",
        ),
        ("sqlite+aiosqlite:///./data/bot.db", "sqlite+aiosqlite:///./data/bot.db"),
    ]
    for raw, expected in cases:
        check(raw, normalize_database_url(raw), expected)


async def main() -> int:
    """Прогоняет все три блока проверок."""
    snapshot = await check_pricing()
    check_language(snapshot)
    check_dsn()
    print("\n" + "=" * 60)
    if failures:
        print(f"ПРОВАЛОВ: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОШЛИ")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
