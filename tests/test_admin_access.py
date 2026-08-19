"""Доступ в админку по паролю и настройки бота.

Права и настройки живут в SQLite, а не в переменных окружения: иначе, чтобы
добавить администратора или выключить напоминания, владельцу пришлось бы править
файл и перезапускать бота — то есть звать программиста.
"""

from __future__ import annotations

import pytest

from app.admin.admin_store import SETTING_SPECS, AdminStore


@pytest.fixture
def store(tmp_path):
    """Чистое хранилище администраторов на временном файле."""
    s = AdminStore(tmp_path / "admin.db")
    try:
        yield s
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Пароль
# --------------------------------------------------------------------------- #
def test_correct_password_grants_admin(store) -> None:
    """Правильный пароль делает человека администратором."""
    assert AdminStore.password_matches("Azamat65", "Azamat65") is True
    assert store.grant(555, "Азамат") is True
    assert store.is_admin(555) is True


@pytest.mark.parametrize("wrong", ["azamat65", "Azamat6", "Azamat655", "", "  "])
def test_wrong_password_is_refused(wrong: str) -> None:
    """Регистр и лишние символы значения имеют: пароль сверяется точно."""
    assert AdminStore.password_matches(wrong, "Azamat65") is False


def test_empty_configured_password_closes_the_door() -> None:
    """Пустой пароль в настройках отключает этот способ входа полностью."""
    assert AdminStore.password_matches("что угодно", "") is False


def test_password_is_compared_in_constant_time() -> None:
    """Сравнение идёт по хешам через compare_digest.

    Обычное сравнение строк завершается на первом несовпавшем символе, и по
    времени ответа пароль подбирается посимвольно.
    """
    import inspect

    source = inspect.getsource(AdminStore.password_matches)
    assert "compare_digest" in source
    assert "sha256" in source


def test_rights_survive_restart(tmp_path) -> None:
    """Права переживают перезапуск: иначе после каждого рестарта вход заново."""
    first = AdminStore(tmp_path / "admin.db")
    first.grant(777, "Зарина")
    first.close()

    second = AdminStore(tmp_path / "admin.db")
    try:
        assert second.is_admin(777) is True
    finally:
        second.close()


def test_granting_twice_is_reported_as_already_admin(store) -> None:
    assert store.grant(1, "Раз") is True
    assert store.grant(1, "Раз") is False


def test_last_admin_cannot_be_removed(store) -> None:
    """Убрать последнего администратора нельзя — управление ботом потерялось бы."""
    store.grant(1, "Один")
    assert store.revoke(1) is False
    assert store.is_admin(1) is True

    store.grant(2, "Второй")
    assert store.revoke(1) is True, "при двух администраторах одного убрать можно"


@pytest.mark.parametrize("stranger", [999, None, "abc", ""])
def test_stranger_is_not_admin(store, stranger) -> None:
    """Посторонний и мусор вместо id прав не получают."""
    store.grant(1, "Свой")
    assert store.is_admin(stranger) is False


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #
def test_settings_have_defaults_before_anything_is_saved(store) -> None:
    """До первой правки настройки отдают значения по умолчанию, а не пустоту."""
    for spec in SETTING_SPECS:
        assert store.get(spec.key) == spec.default


def test_setting_survives_restart(tmp_path) -> None:
    first = AdminStore(tmp_path / "admin.db")
    first.set("followup_enabled", "off")
    first.close()

    second = AdminStore(tmp_path / "admin.db")
    try:
        assert second.get("followup_enabled") == "off"
    finally:
        second.close()


def test_unknown_setting_is_refused(store) -> None:
    """Свободный ввод ключа запрещён: иначе в базу попадёт мусор."""
    with pytest.raises(KeyError):
        store.set("удали_всё", "да")


def test_all_settings_are_listed_with_titles(store) -> None:
    """Список для показа в чате: название на русском и текущее значение."""
    rows = store.all_settings()
    assert len(rows) == len(SETTING_SPECS)
    for spec, value in rows:
        assert spec.title and not spec.title.startswith("setting")
        assert value


# --------------------------------------------------------------------------- #
# Меню в чате
# --------------------------------------------------------------------------- #
@pytest.fixture
def console(tmp_path, kb_dir, media_dir, store):
    """Админка поверх реальной базы знаний и временного хранилища прав."""
    from app.admin.telegram_admin import AdminConsole
    from app.kb.loader import load_sync

    snapshot, _ = load_sync(kb_dir, media_dir=media_dir, schema_version=1)
    return AdminConsole(
        kb_dir=kb_dir, media_dir=media_dir, schema_version=1,
        snapshot=lambda: snapshot, store=store,
    )


def test_admin_opens_a_menu(console) -> None:
    """/admin показывает разделы, а не сразу расписание."""
    reply = console.handle("c", "/admin")
    assert "Управление ботом" in reply
    for item in ("Расписание", "Настройки", "Администраторы"):
        assert item in reply, f"в меню нет раздела «{item}»"


def test_settings_section_shows_current_values(console) -> None:
    console.handle("c", "/admin")
    reply = console.handle("c", "2")
    assert "Настройки бота" in reply
    assert "включено" in reply, "переключатели должны показываться словами"


def test_toggle_flips_the_setting_in_one_step(console, store) -> None:
    """Переключатель меняется сразу: лишний вопрос «включить?» тут не нужен."""
    before = store.get("followup_enabled")
    console.handle("c", "/admin")
    console.handle("c", "2")
    reply = console.handle("c", "1")

    assert store.get("followup_enabled") != before
    assert "выключено" in reply or "включено" in reply


def test_time_range_setting_validates_format(console, store) -> None:
    """Тихие часы принимаются только в формате 21:00-09:00."""
    console.handle("c", "/admin")
    console.handle("c", "2")
    console.handle("c", "2")  # «Тихие часы» — второй пункт

    bad = console.handle("c", "ночью")
    assert "формат" in bad.lower()

    good = console.handle("c", "22:00-08:00")
    assert "Сохранил" in good
    assert store.get("quiet_hours") == "22:00-08:00"


def test_admins_section_lists_people(console, store) -> None:
    store.grant(42, "Зарина")
    console.handle("c", "/admin")
    reply = console.handle("c", "3")
    assert "Зарина" in reply and "42" in reply


def test_gaps_section_names_gyms_without_schedule(console) -> None:
    """Раздел «что бот не знает» показывает залы без расписания поимённо."""
    console.handle("c", "/admin")
    reply = console.handle("c", "4")
    assert "Нет расписания" in reply
    assert "Карабалык" in reply
