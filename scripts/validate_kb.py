#!/usr/bin/env python3
"""Проверка базы знаний перед заливкой: что заполнено, что пусто, что сломано.

Запуск из корня проекта::

    .venv/bin/python scripts/validate_kb.py

Отчёт на русском и рассчитан на владельца школы, а не на разработчика: он
показывает не только ошибки схемы, но и список вопросов клиентов, которые
сегодня останутся без ответа.

Коды возврата: ``0`` — база валидна, ``1`` — ошибка схемы или ссылок
(в этом случае бот с такой базой не поднимется).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Sequence

# Скрипт запускают как файл, поэтому корень проекта добавляется в путь вручную.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.kb import gaps as gaps_module  # noqa: E402
from app.kb.loader import load_sync  # noqa: E402
from app.kb.models import FAQ_TOPICS, KBSnapshot  # noqa: E402
from app.kb.render import render_system_prompt  # noqa: E402
from app.types import GymStatus, KBValidationError, Language, Scope  # noqa: E402

_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
_PHONE_RE = re.compile(r"\+7\d{10}")
_PRIORITY_RU = {"critical": "критический", "high": "высокий", "medium": "средний"}
_LINE = "-" * 78


def _defaults() -> tuple[Path, Path, int]:
    """Пути и версия схемы из настроек приложения; при недоступности — по умолчанию."""
    try:
        from app.config import get_settings

        settings = get_settings()
        return settings.kb_dir, settings.media_dir, settings.kb_schema_version
    except Exception:
        # app/config.py принадлежит другой волне и может быть ещё не дописан —
        # проверка базы знаний не должна от этого зависеть.
        return Path("kb"), Path("media"), 1


def _head(title: str) -> None:
    print()
    print(title)
    print(_LINE)


def _report_files(snapshot: KBSnapshot) -> None:
    _head("СОДЕРЖИМОЕ БАЗЫ")
    city = snapshot.active_gyms(Scope.CITY)
    region = snapshot.active_gyms(Scope.REGION)
    unresolved = snapshot.unresolved_gyms()
    print(f"gyms.yaml      : {len(snapshot.gyms.gyms)} записей "
          f"(город {len(city)}, райцентры {len(region)}, требуют уточнения {len(unresolved)})")
    plans = sum(1 for plan in snapshot.pricing.city_plans.values() if plan is not None)
    plans += sum(1 for plan in snapshot.pricing.region_plans.values() if plan is not None)
    plans += 1 if snapshot.pricing.city_single is not None else 0
    plans += 1 if snapshot.pricing.region_single is not None else 0
    print(f"pricing.yaml   : {plans} тарифов, {len(snapshot.pricing.derived_facts)} "
          f"аргументов о выгоде, {len(snapshot.pricing.payment_methods)} способов оплаты")
    answered = sum(1 for entry in snapshot.faq if entry.answered)
    print(f"faq.yaml       : {len(snapshot.faq)} записей, из них с готовым ответом {answered}, "
          f"тем {len(snapshot.faq_topics())} из {len(FAQ_TOPICS)}")
    print(f"media.yaml     : {len(snapshot.media)} материалов, включено {len(snapshot.artifact_ids())}")
    print(f"policies.yaml  : {len(snapshot.policies.escalation_triggers)} причин эскалации, "
          f"{len(snapshot.policies.followup_policy)} правил напоминаний, "
          f"{len(snapshot.policies.forbidden_behaviour)} запретов")
    print(f"i18n.yaml      : {len(snapshot.i18n.strings)} системных фраз (ru и kk)")
    lexicon_words = sum(len(words) for words in snapshot.lexicon.intents.values())
    print(f"lexicon.yaml   : {lexicon_words} слов в {len(snapshot.lexicon.intents)} темах, "
          f"{len(snapshot.lexicon.districts_extra)} районов без зала")


def _report_gyms(snapshot: KBSnapshot) -> None:
    _head("ЗАЛЫ: ЧТО ЗАПОЛНЕНО")
    print(f"{'зал':<32}{'адрес':<8}{'тел':<6}{'тренеры':<10}{'расписание':<12}{'геометка'}")
    for gym in sorted(snapshot.gyms.gyms, key=lambda item: (item.scope.value, item.id)):
        if gym.status is GymStatus.UNRESOLVED:
            continue
        print(
            f"{gym.id:<32}"
            f"{('есть' if gym.address.filled else 'НЕТ'):<8}"
            f"{('есть' if gym.phone else 'НЕТ'):<6}"
            f"{(str(len(gym.coaches)) if gym.coaches else 'НЕТ'):<10}"
            f"{(str(len(gym.schedule)) if gym.schedule else 'НЕТ'):<12}"
            f"{'есть' if (gym.geo_lat is not None or gym.map_url) else 'НЕТ'}"
        )
    head = snapshot.head_gym()
    print(f"\nГоловной зал: {head.id if head else 'НЕ ПОМЕЧЕН'}")


def _report_faq(snapshot: KBSnapshot) -> None:
    _head("ВОПРОСЫ КЛИЕНТОВ, КОТОРЫЕ ОСТАНУТСЯ БЕЗ ОТВЕТА")
    empty = [entry for entry in snapshot.faq if not entry.answered]
    if not empty:
        print("Таких нет: по всем темам есть готовый ответ.")
    else:
        print(f"Без ответа {len(empty)} из {len(snapshot.faq)} записей. "
              "По каждой бот честно предложит администратора.\n")
        for entry in sorted(empty, key=lambda item: (item.topic, item.id)):
            variants = entry.question_variants.get(Language.RU) or []
            sample = variants[0] if variants else entry.id
            gap = f" [{entry.gap_ref.value}]" if entry.gap_ref else ""
            print(f"  - {entry.topic}/{entry.id}{gap}: «{sample}»")
    missing_topics = sorted(set(FAQ_TOPICS) - set(snapshot.faq_topics()))
    if missing_topics:
        print(f"\nТемы без единой записи: {', '.join(missing_topics)}")


def _report_gaps(snapshot: KBSnapshot) -> None:
    _head("РЕЕСТР ПРОБЕЛОВ")
    open_gaps = gaps_module.open_gaps(snapshot)
    closed = gaps_module.closed_gaps(snapshot)
    if closed:
        print("Закрыто владельцем: " + ", ".join(gap.value for gap in closed))
    print(f"Открыто: {len(open_gaps)} из {len(gaps_module.all_gaps())}\n")
    for gap in open_gaps:
        info = gaps_module.info(gap)
        if info is None:
            continue
        print(f"  {gap.value}  [{_PRIORITY_RU.get(info.priority, info.priority)}]  {info.title_ru}")
        print(f"      куда вписать : {info.where_ru}")
        print(f"      вопрос клиента: {info.question_ru}")


def _report_pricing(snapshot: KBSnapshot) -> None:
    _head("ПРАЙС И НЕРЕШЁННЫЕ КОНФЛИКТЫ")
    pricing = snapshot.pricing
    for scope, plans, single in (
        ("Костанай", pricing.city_plans, pricing.city_single),
        ("Райцентры", pricing.region_plans, pricing.region_single),
    ):
        parts = []
        for key in sorted(plans):
            plan = plans[key]
            parts.append(f"{key} — {plan.price if plan else 'НЕТ ДАННЫХ'}")
        parts.append(f"разовая — {single.price if single else 'НЕТ ДАННЫХ'}")
        print(f"{scope:<12}: " + ", ".join(parts))
    print(f"Семейная скидка города: "
          f"{', '.join(f'{index}-й ребёнок {percent}%' for index, percent in sorted(pricing.city_family_discount.rules))}")
    print(f"Семейный тариф райцентров: {pricing.region_family_price_per_child} ₸ за ребёнка "
          f"от {pricing.region_family_min_children} детей")
    print()
    print("C-3 зал на КЖБИ            : "
          + ("решён" if gaps_module.has_data(snapshot, gaps_module.GapRef.C3) else
             "НЕ РЕШЁН — бот не подтверждает и не отрицает наличие зала"))
    print("C-4 скидка на гибкий тариф : "
          + ("подтверждена владельцем"
             if pricing.city_family_discount.applies_to_status == "confirmed"
             else "НЕ ПОДТВЕРЖДЕНА — бот считает, но добавляет оговорку"))
    print("C-5 база расчёта скидки    : "
          + ("подтверждена владельцем"
             if pricing.city_family_discount.base_rule_status == "confirmed"
             else "НЕ ПОДТВЕРЖДЕНА — при разных тарифах бот передаёт вопрос администратору"))


def _report_prompt(snapshot: KBSnapshot) -> list[str]:
    """Проверяет системный префикс: стабильность и отсутствие запрещённого."""
    _head("СИСТЕМНЫЙ ПРОМПТ")
    first = render_system_prompt(snapshot)
    second = render_system_prompt(snapshot)
    problems: list[str] = []
    if first != second:
        problems.append("префикс промпта нестабилен между вызовами — сломается кэш модели")
    for gym in snapshot.gyms.gyms:
        if gym.internal_note and gym.internal_note in first:
            problems.append(f"в промпт попала служебная заметка зала '{gym.id}'")
    if _PHONE_RE.search(first):
        problems.append("в промпт попал телефон")
    if _TIME_RE.search(first):
        problems.append("в промпт попало время — расписание в префиксе запрещено")
    print(f"Размер: {len(first)} знаков, примерно {len(first) // 3} токенов.")
    print("Стабильность между вызовами: " + ("да" if first == second else "НЕТ"))
    print("Служебные заметки, телефоны, расписание: " + ("нет" if not problems else "ЕСТЬ"))
    for problem in problems:
        print(f"  ! {problem}")
    return problems


def _report_i18n(snapshot: KBSnapshot) -> None:
    _head("СИСТЕМНЫЕ ФРАЗЫ")
    groups: dict[str, int] = {}
    for key in snapshot.i18n.strings:
        groups[key.split(".", 1)[0]] = groups.get(key.split(".", 1)[0], 0) + 1
    for group in sorted(groups):
        print(f"  {group:<12}: {groups[group]}")
    missing = [key for key in gaps_module.required_i18n_keys() if key not in snapshot.i18n.strings]
    print("Обязательные ключи: " + ("все на месте" if not missing else f"НЕТ {', '.join(missing)}"))


def main(argv: Sequence[str] | None = None) -> int:
    kb_default, media_default, version_default = _defaults()
    parser = argparse.ArgumentParser(
        description="Проверка базы знаний AINAZAROV TOP TEAM",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--kb-dir", type=Path, default=kb_default, help="каталог с YAML-файлами")
    parser.add_argument("--media-dir", type=Path, default=media_default, help="каталог с медиафайлами")
    parser.add_argument("--schema-version", type=int, default=version_default, help="ожидаемая версия схемы")
    parser.add_argument("--quiet", action="store_true", help="только итог и ошибки")
    args = parser.parse_args(list(argv) if argv is not None else None)

    print("ПРОВЕРКА БАЗЫ ЗНАНИЙ AINAZAROV TOP TEAM")
    print(f"каталог: {args.kb_dir}, медиа: {args.media_dir}, версия схемы: {args.schema_version}")

    try:
        snapshot, warnings = load_sync(
            args.kb_dir, media_dir=args.media_dir, schema_version=args.schema_version
        )
    except KBValidationError as exc:
        print()
        print("БАЗА НЕ ПРОШЛА ПРОВЕРКУ — С НЕЙ БОТ НЕ ЗАПУСТИТСЯ")
        print(_LINE)
        print(exc.message)
        for line in exc.errors:
            print(f"  - {line}")
        print()
        print("Исправьте перечисленные строки и запустите проверку снова.")
        return 1

    print(f"Схема и ссылки в порядке. Версия базы (kb_hash): {snapshot.kb_hash[:16]}")
    if warnings:
        print()
        print("ПРЕДУПРЕЖДЕНИЯ (материалы выключены автоматически):")
        for line in warnings:
            print(f"  ! {line}")

    if not args.quiet:
        _report_files(snapshot)
        _report_gyms(snapshot)
        _report_pricing(snapshot)
        _report_faq(snapshot)
        _report_gaps(snapshot)
        _report_i18n(snapshot)
    problems = _report_prompt(snapshot)

    _head("ИТОГ")
    open_gaps = gaps_module.open_gaps(snapshot)
    print("Схема: ОК. Бот с этой базой запустится.")
    print(f"Пробелов открыто: {len(open_gaps)}. По ним бот честно передаёт вопрос администратору.")
    critical = [
        gap for gap in open_gaps
        if (info := gaps_module.info(gap)) is not None and info.priority == "critical"
    ]
    if critical:
        print("Заполнить в первую очередь: " + ", ".join(gap.value for gap in critical))
    if problems:
        print("ВНИМАНИЕ: проблемы с системным промптом (см. выше).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
