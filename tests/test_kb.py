"""Загрузка и валидация базы знаний, стабильность рендера.

База знаний — единственный источник правды о ценах и адресах, и правит её
владелец школы руками. Поэтому:

* **невалидная база не применяется никогда** — на старте это отказ подняться,
  на hot-reload отказ применить. Половинчатая база хуже отсутствующей: бот
  начнёт врать клиентам ценами из одного файла и адресами из другого;
* **ошибки собираются все сразу** — иначе владелец пойдёт по кругу «исправил
  одну, вылезла вторая»;
* **системный промпт байт-в-байт стабилен** при одном ``kb_hash`` — иначе
  implicit-кэш Gemini сбрасывается на каждом ходу и счёт растёт на ровном месте.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from app.kb.loader import KB_FILES, compute_kb_hash, load_sync
from app.kb.render import (
    render_gaps_manifest,
    render_gyms_block,
    render_price_card,
    render_pricing_showcase,
    render_system_prompt,
)
from app.types import KBValidationError, Language, Scope


@pytest.fixture
def broken_kb(tmp_path: Path, kb_dir: Path, media_dir: Path) -> Callable[..., Path]:
    """Фабрика испорченной копии базы знаний.

    Возвращает функцию ``mutate(file_name, changer)``: она копирует настоящую
    ``kb/``, применяет правку к разобранному YAML и отдаёт путь к копии.
    """
    target = tmp_path / "kb"
    target.mkdir()
    for name in KB_FILES:
        shutil.copy(kb_dir / name, target / name)

    def _mutate(file_name: str, changer: Callable[[dict[str, Any]], None]) -> Path:
        path = target / file_name
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        changer(data)
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8")
        return target

    return _mutate


def load_broken(directory: Path, media_dir: Path) -> KBValidationError:
    """Загружает заведомо битую базу и возвращает пойманное исключение."""
    with pytest.raises(KBValidationError) as caught:
        load_sync(directory, media_dir=media_dir, schema_version=1)
    return caught.value


# --------------------------------------------------------------------------- #
# Валидная база
# --------------------------------------------------------------------------- #
def test_real_kb_loads(kb) -> None:
    """Настоящая база знаний обязана грузиться: иначе бот не поднимется вовсе."""
    assert kb.kb_hash
    # 7 залов Костаная + 7 райцентров; пятнадцатая запись — заглушка КЖБИ (C-3),
    # она живёт в базе со статусом unresolved и в выдачу не попадает.
    # Седьмой городской — Полевая 7/3, БЦ «Кеме»: данные владельца от 02.09.2026,
    # до этого бот отвечал «шесть залов», а школа считала свои восемь адресов.
    assert len(kb.active_gyms(Scope.ALL)) == 14
    assert len(kb.active_gyms(Scope.CITY)) == 7
    assert len(kb.active_gyms(Scope.REGION)) == 7
    assert len(list(kb.unresolved_gyms())) == 1
    assert kb.pricing.city_plans["standard"].price == 25_000
    assert kb.pricing.region_family_price_per_child == 8_000


def test_kb_hash_is_content_addressed() -> None:
    """Хеш считается от содержимого и не зависит от переводов строк."""
    unix = {"gyms.yaml": b"a: 1\nb: 2\n"}
    windows = {"gyms.yaml": b"a: 1\r\nb: 2\r\n"}

    assert compute_kb_hash(unix) == compute_kb_hash(windows)
    assert compute_kb_hash(unix) != compute_kb_hash({"gyms.yaml": b"a: 1\nb: 3\n"})


# --------------------------------------------------------------------------- #
# Битые данные
# --------------------------------------------------------------------------- #
def test_duplicate_gym_id_is_rejected(broken_kb, media_dir) -> None:
    """Два зала с одним id — заявки клиентов и статистика поедут молча."""

    def duplicate(data: dict[str, Any]) -> None:
        clone = dict(data["gyms"][1])
        clone["id"] = data["gyms"][0]["id"]
        data["gyms"].append(clone)

    error = load_broken(broken_kb("gyms.yaml", duplicate), media_dir)

    assert any("дубликат gym_id" in line for line in error.errors)


def test_negative_price_is_rejected(broken_kb, media_dir) -> None:
    """Отрицательная цена — это не «скидка», это сломанный прайс."""

    def break_price(data: dict[str, Any]) -> None:
        data["city_plans"]["standard"]["price"] = -1

    error = load_broken(broken_kb("pricing.yaml", break_price), media_dir)

    assert any("pricing.yaml" in line and "price" in line for line in error.errors)


def test_zero_price_is_rejected(broken_kb, media_dir) -> None:
    """Ноль в прайсе означал бы «бесплатно» — такого предложения у школы нет."""

    def break_price(data: dict[str, Any]) -> None:
        data["region_plans"]["standard"]["price"] = 0

    error = load_broken(broken_kb("pricing.yaml", break_price), media_dir)

    assert any("pricing.yaml" in line for line in error.errors)


def test_missing_kazakh_translation_is_rejected(broken_kb, media_dir) -> None:
    """Заполнена только русская локаль — казахоязычный клиент увидит пустоту."""

    def drop_kk(data: dict[str, Any]) -> None:
        data["gyms"][0]["title"]["kk"] = None

    error = load_broken(broken_kb("gyms.yaml", drop_kk), media_dir)

    assert any("нет перевода" in line for line in error.errors)


def test_missing_kazakh_translation_in_pricing_is_rejected(broken_kb, media_dir) -> None:
    """То же правило для прайса: обе локали или ни одной."""

    def drop_kk(data: dict[str, Any]) -> None:
        data["city_plans"]["flexible"]["label"]["kk"] = None

    error = load_broken(broken_kb("pricing.yaml", drop_kk), media_dir)

    assert any("нет перевода" in line for line in error.errors)


def test_reference_to_unknown_artifact_is_rejected(broken_kb, media_dir) -> None:
    """Зал ссылается на несуществующий артефакт — бот пошлёт клиенту пустоту."""

    def break_ref(data: dict[str, Any]) -> None:
        data["gyms"][0]["media"] = ["gym_location_does_not_exist"]

    error = load_broken(broken_kb("gyms.yaml", break_ref), media_dir)

    assert any("несуществующий артефакт" in line for line in error.errors)


def test_empty_string_is_not_a_valid_empty_value(broken_kb, media_dir) -> None:
    """Пустые кавычки — ошибка загрузки: «нет данных» пишется как ``null``."""

    def blank(data: dict[str, Any]) -> None:
        data["gyms"][0]["title"]["ru"] = "   "

    error = load_broken(broken_kb("gyms.yaml", blank), media_dir)

    assert error.errors


def test_alias_owned_by_two_gyms_is_rejected(broken_kb, media_dir) -> None:
    """Один алиас у двух залов — матчинг района становится неоднозначным."""

    def collide(data: dict[str, Any]) -> None:
        data["gyms"][1]["district_aliases"] = list(data["gyms"][1]["district_aliases"]) + ["кск"]

    error = load_broken(broken_kb("gyms.yaml", collide), media_dir)

    assert any("алиас района" in line for line in error.errors)


def test_schema_version_mismatch_blocks_startup(broken_kb, media_dir) -> None:
    """Формат файла и код разошлись — это блокер старта, а не предупреждение."""

    def bump(data: dict[str, Any]) -> None:
        data["schema_version"] = 999

    error = load_broken(broken_kb("pricing.yaml", bump), media_dir)

    assert any("schema_version" in line for line in error.errors)


def test_missing_file_is_reported_by_name(tmp_path: Path, kb_dir: Path, media_dir: Path) -> None:
    """Не хватает файла — в ошибке обязано быть его имя, а не «что-то не так»."""
    partial = tmp_path / "kb-partial"
    partial.mkdir()
    for name in KB_FILES[:-1]:
        shutil.copy(kb_dir / name, partial / name)

    error = load_broken(partial, media_dir)

    assert any(KB_FILES[-1] in line for line in error.errors)


def test_broken_yaml_syntax_is_reported(tmp_path: Path, kb_dir: Path, media_dir: Path) -> None:
    """Синтаксическая ошибка YAML — самая частая правка «мимо» у владельца."""
    directory = tmp_path / "kb-syntax"
    directory.mkdir()
    for name in KB_FILES:
        shutil.copy(kb_dir / name, directory / name)
    (directory / "faq.yaml").write_text("entries: [ {id: 'x'\n", encoding="utf-8")

    error = load_broken(directory, media_dir)

    assert any("faq.yaml" in line for line in error.errors)


def test_all_errors_are_collected_not_just_the_first(broken_kb, media_dir) -> None:
    """Владелец правит YAML сам: десять раундов «исправил одну» его прогонят."""
    broken_kb("pricing.yaml", lambda data: data["city_plans"]["standard"].__setitem__("price", -1))
    directory = broken_kb(
        "gyms.yaml", lambda data: data["gyms"][0]["title"].__setitem__("kk", None)
    )

    error = load_broken(directory, media_dir)

    assert any("pricing.yaml" in line for line in error.errors)
    assert any("gyms.yaml" in line for line in error.errors)


def test_invalid_kb_does_not_replace_the_loaded_one(broken_kb, media_dir, kb) -> None:
    """Отказ валидации не имеет права тронуть уже работающий снимок."""
    from app.kb import loader as kb_loader

    kb_loader.swap(kb)
    before = kb_loader.current_hash()

    load_broken(broken_kb("pricing.yaml", lambda d: d.__setitem__("city_sessions", 0)), media_dir)

    assert kb_loader.current_hash() == before


# --------------------------------------------------------------------------- #
# Рендер
# --------------------------------------------------------------------------- #
def test_system_prompt_is_byte_stable(kb) -> None:
    """Один ``kb_hash`` — один и тот же префикс промпта, байт в байт.

    Иначе implicit-кэш Gemini сбрасывается на каждом ходу: те же токены платятся
    заново, а латентность растёт.
    """
    first = render_system_prompt(kb)
    second = render_system_prompt(kb)

    assert first == second


def test_system_prompt_is_stable_across_reloads(kb_dir, media_dir) -> None:
    """Повторная загрузка тех же файлов даёт тот же промпт и тот же хеш."""
    first, _ = load_sync(kb_dir, media_dir=media_dir, schema_version=1)
    second, _ = load_sync(kb_dir, media_dir=media_dir, schema_version=1)

    assert first.kb_hash == second.kb_hash
    assert render_system_prompt(first) == render_system_prompt(second)


@pytest.mark.parametrize(
    "renderer",
    [render_gyms_block, render_pricing_showcase, render_gaps_manifest],
)
def test_prompt_blocks_are_stable(kb, renderer) -> None:
    """Каждый блок промпта детерминирован по отдельности."""
    assert renderer(kb) == renderer(kb)


def test_system_prompt_contains_registry_and_gaps(kb) -> None:
    """В промпте обязаны быть реестр залов и манифест пробелов."""
    prompt = render_system_prompt(kb)

    assert "ksk_kairbekova_334" in prompt
    assert "Житикара" in prompt
    # Самый важный блок: чего в базе нет и о чём модель обязана молчать.
    assert "ЧЕГО В БАЗЕ НЕТ" in prompt


def test_system_prompt_hides_internal_notes(kb) -> None:
    """``internal_note`` — служебное поле владельца, в промпт оно попадать не должно."""
    prompt = render_system_prompt(kb)
    notes = [gym.internal_note for gym in kb.gyms.gyms if gym.internal_note]

    assert notes, "тест бессмысленен, если внутренних заметок в базе нет"
    for note in notes:
        assert note not in prompt


@pytest.mark.parametrize("lang", [Language.RU, Language.KK])
@pytest.mark.parametrize("scope", [Scope.CITY, Scope.REGION])
def test_price_card_is_stable_and_localised(kb, scope, lang) -> None:
    """Карточка прайса детерминирована на обоих языках."""
    card = render_price_card(kb, scope=scope, lang=lang)

    assert card == render_price_card(kb, scope=scope, lang=lang)
    assert card.strip()


def test_price_card_shows_city_prices(kb) -> None:
    """Городская карточка называет городские цены, а не районные."""
    card = render_price_card(kb, scope=Scope.CITY, lang=Language.RU)

    assert "25 000" in card
    assert "30 000" in card


def test_price_card_shows_region_prices(kb) -> None:
    """Районная карточка — районные: разница более чем вдвое."""
    card = render_price_card(kb, scope=Scope.REGION, lang=Language.RU)

    assert "10 000" in card
    assert "25 000" not in card


def test_every_i18n_key_used_by_code_is_enforced() -> None:
    """Ключ, который произносит код, обязан быть в списке обязательных.

    Иначе связь односторонняя: код читает ключ, а загрузчик его отсутствия не
    замечает — и пропажа вскрывается пустым местом в ответе живому клиенту.
    Так уже случалось с подписями карточек: пятнадцать ключей кода не были
    защищены проверкой базы знаний.
    """
    import re
    from pathlib import Path

    from app.kb.gaps import required_i18n_keys

    root = Path(__file__).resolve().parent.parent
    sources = [
        root / "app" / "kb" / "render.py",
        root / "app" / "core" / "guards.py",
    ]
    pattern = re.compile(r"['\"]((?:card|escalation|greeting|bridge|system)\.[a-z_]+)['\"]")

    used: set[str] = set()
    for path in sources:
        used.update(pattern.findall(path.read_text(encoding="utf-8")))

    missing = sorted(used - set(required_i18n_keys()))
    assert not missing, (
        "код произносит эти ключи, а загрузчик их не требует: " + ", ".join(missing)
    )


def test_no_unused_card_texts(kb) -> None:
    """В базе знаний нет подписей карточек, которых никто не читает.

    Мёртвый ключ виден владельцу в разделе «Готовые фразы»: он правит текст,
    сохраняет — и ничего не меняется.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    render = (root / "app" / "kb" / "render.py").read_text(encoding="utf-8")
    used = set(re.findall(r"['\"](card\.[a-z_]+)['\"]", render))
    # Дисциплины подставляются по коду: card.{discipline}.
    used.update({"card.boxing", "card.kickboxing"})

    declared = {key for key in kb.i18n.strings if key.startswith("card.")}
    assert not declared - used, f"ключи никем не читаются: {sorted(declared - used)}"


def test_faq_never_counts_the_gyms_in_words(kb) -> None:
    """Число залов словами в тексте живёт ровно до открытия следующего зала.

    03.09.2026 бот в одном сообщении сказал «в Костанае шесть залов» и следом
    перечислил семь: седьмой добавили в базу, а текст остался прежним. Список
    бот и так собирает из базы знаний, поэтому число в тексте не нужно.
    """
    numerals = ("шесть зал", "семь зал", "восемь зал", "алты зал", "жеті зал", "6 залов", "7 залов")

    for entry in kb.faq:
        for lang in ("ru", "kk"):
            text = (getattr(entry.answer, lang, "") or "").lower()
            found = [word for word in numerals if word in text]
            assert not found, f"в ответе {entry.id} ({lang}) зашито число залов: {found}"


# --------------------------------------------------------------------------- #
# Выбор записи внутри темы
# --------------------------------------------------------------------------- #
async def test_payment_method_question_reaches_a_human(kb) -> None:
    """«Как оплатить» и «когда оплатить» — разные вопросы одной темы.

    03.09.2026 клиент спросил, как оплатить абонемент, и получил ответ про
    сроки: «оплачивается до 10-го числа». Инструмент брал первую отвеченную
    запись темы, а запись про способы оплаты пустая — владелец их ещё не
    прислал, и по ней положено звать человека.
    """
    from datetime import datetime, timezone
    from uuid import uuid4

    from app.tools.facts import get_kb_fact
    from app.types import ChannelKind, Language, ToolContext, ToolStatus

    from tests.conftest import RecordingServices

    def context() -> ToolContext:
        return ToolContext(
            conversation_id=uuid4(),
            conv_key="c",
            channel=ChannelKind.WHATSAPP,
            channel_id="w",
            chat_id="7701",
            lang=Language.RU,
            kb=kb,
            kb_hash=kb.kb_hash,
            now=datetime(2026, 9, 3, 9, tzinfo=timezone.utc),
            correlation_id="t",
            services=RecordingServices(),
        )

    method = await get_kb_fact(context(), topic="payment", question="как оплатить абонемент")
    assert method.status is ToolStatus.NEEDS_OPERATOR

    deadline = await get_kb_fact(context(), topic="payment", question="когда нужно оплачивать")
    assert deadline.status is ToolStatus.OK
    assert (deadline.data or {}).get("id") == "payment_deadline"


def test_question_match_ignores_a_single_common_word(kb) -> None:
    """Совпадения одного общего слова мало: иначе выберется случайная запись."""
    from app.tools.facts import _best_match

    entries = kb.faq_entries("payment", __import__("app.types", fromlist=["Scope"]).Scope.ANY)

    assert _best_match(entries, "занятие") is None
