"""Разбор расписания из сообщения администратора и безопасная запись в базу."""
from __future__ import annotations
import shutil
import pytest
from app.admin.schedule_text import ScheduleParseError, parse_schedule_text, render_schedule_text
from app.admin.schedule_store import ScheduleWriteError, apply_schedule, clear_schedule, read_schedule

REAL_KSK = """ул. Каирбекова, 334
Школа №9, цокольный этаж, район КСК)
🥊 КИКБОКСИНГ

Понедельник • Среда • Пятница

🕘 09:00–10:30
🕔 17:00–18:30
🕖 19:00–20:30

🥊 БОКС

Вторник • Четверг • Суббота

🕘 09:00–10:30
"""

def test_real_admin_message_is_parsed() -> None:
    """Сообщение из WhatsApp разбирается как есть, без правки привычек человека."""
    parsed = parse_schedule_text(REAL_KSK)
    assert len(parsed.slots) == 4
    kick = [s for s in parsed.slots if s.discipline == "kickboxing"]
    box = [s for s in parsed.slots if s.discipline == "boxing"]
    assert len(kick) == 3 and len(box) == 1
    assert kick[0].days == ["mon", "wed", "fri"]
    assert box[0].days == ["tue", "thu", "sat"]
    assert box[0].time_start == "09:00"

def test_boxing_does_not_inherit_kickboxing_days() -> None:
    """Заголовок дисциплины сбрасывает дни: иначе бокс уедет в дни кикбоксинга."""
    parsed = parse_schedule_text(REAL_KSK)
    for slot in parsed.slots:
        expected = ["mon", "wed", "fri"] if slot.discipline == "kickboxing" else ["tue", "thu", "sat"]
        assert slot.days == expected

@pytest.mark.parametrize("text", ["", "   ", "просто текст без расписания", "📍 ул. Ленина 5"])
def test_garbage_is_rejected_loudly(text: str) -> None:
    """Не разобралось — ошибка администратору, а не пустое расписание молча."""
    with pytest.raises(ScheduleParseError):
        parse_schedule_text(text)

def test_time_without_days_is_an_error() -> None:
    """Время без дней сохранять нельзя: получилось бы занятие «когда-нибудь»."""
    with pytest.raises(ScheduleParseError) as exc:
        parse_schedule_text("🥊 КИКБОКСИНГ\n09:00–10:30")
    assert "дни" in str(exc.value)

def test_time_without_discipline_is_an_error() -> None:
    """Вид занятий обязателен: бокс и кикбоксинг идут в разные дни."""
    with pytest.raises(ScheduleParseError) as exc:
        parse_schedule_text("Понедельник • Среда\n09:00–10:30")
    assert "вид занятий" in str(exc.value)

def test_skipped_lines_are_reported_not_swallowed() -> None:
    """Непонятая строка попадает в замечания: молча терять кусок расписания нельзя."""
    parsed = parse_schedule_text(REAL_KSK)
    assert any("пропущена строка" in w for w in parsed.warnings)

def test_duplicate_slot_is_dropped_with_a_warning() -> None:
    parsed = parse_schedule_text("🥊 БОКС\nВторник\n09:00–10:30\n09:00–10:30")
    assert len(parsed.slots) == 1
    assert any("повтор" in w for w in parsed.warnings)

@pytest.mark.parametrize("dash", ["-", "–", "—"])
def test_any_dash_between_times(dash: str) -> None:
    """Администратор ставит любое тире — разбор не должен от этого зависеть."""
    parsed = parse_schedule_text(f"Кикбоксинг\nПн • Ср\n17:00{dash}18:30")
    assert parsed.slots[0].time_start == "17:00"

def test_kazakh_day_names() -> None:
    parsed = parse_schedule_text("Кикбоксинг\nДүйсенбі • Сәрсенбі\n17:00–18:30")
    assert parsed.slots[0].days == ["mon", "wed"]

def test_render_round_trip() -> None:
    """Показ администратору обязан отражать то, что реально сохранится."""
    parsed = parse_schedule_text(REAL_KSK)
    shown = render_schedule_text(parsed.as_yaml_dicts())
    assert "Кикбоксинг" in shown and "Бокс" in shown
    assert "09:00–10:30" in shown

# ---------------------------------------------------------------- запись
@pytest.fixture
def sandbox(tmp_path, kb_dir, media_dir):
    """Копия базы знаний: тесты не имеют права портить рабочие данные."""
    dst = tmp_path / "kb"
    shutil.copytree(kb_dir, dst)
    return dst, media_dir

def test_apply_replaces_schedule_and_clears_gap(sandbox) -> None:
    kb, media = sandbox
    parsed = parse_schedule_text(REAL_KSK)
    result = apply_schedule(kb, "center_kairbekova_24", parsed.as_yaml_dicts(),
                            media_dir=media, schema_version=1)
    assert result.slots_after == 4
    assert len(read_schedule(kb, "center_kairbekova_24")) == 4

def test_broken_edit_is_rolled_back(sandbox) -> None:
    """Невалидная правка откатывается, а бот продолжает работать по старой базе."""
    kb, media = sandbox
    before = read_schedule(kb, "ksk_kairbekova_334")
    bad = [{"discipline": "karate", "days": ["mon"], "time_start": "09:00", "time_end": "10:30"}]
    with pytest.raises(ScheduleWriteError):
        apply_schedule(kb, "ksk_kairbekova_334", bad, media_dir=media, schema_version=1)
    assert read_schedule(kb, "ksk_kairbekova_334") == before, "файл не вернулся к прежнему виду"

def test_unknown_gym_is_rejected(sandbox) -> None:
    kb, media = sandbox
    with pytest.raises(ScheduleWriteError):
        apply_schedule(kb, "no_such_gym", [], media_dir=media, schema_version=1)

def test_clear_restores_the_gap(sandbox) -> None:
    """Очистка возвращает пробел G-1: бот снова отправит вопрос администратору."""
    kb, media = sandbox
    result = clear_schedule(kb, "ksk_kairbekova_334", media_dir=media, schema_version=1)
    assert result.slots_after == 0
    assert read_schedule(kb, "ksk_kairbekova_334") == []


# ---------------------------------------------------------------- диалог /admin
@pytest.fixture
def console(sandbox):
    """Админка поверх копии базы знаний."""
    from app.admin.telegram_admin import AdminConsole
    from app.kb.loader import load_sync

    kb, media = sandbox
    snapshot, _ = load_sync(kb, media_dir=media, schema_version=1)
    holder = {"snap": snapshot}

    def reload_snapshot():
        return holder["snap"]

    return AdminConsole(
        kb_dir=kb, media_dir=media, schema_version=1, snapshot=reload_snapshot
    ), kb, media


def test_admin_dialogue_saves_schedule(console) -> None:
    """Путь целиком: /admin → номер зала → вставка текста → «да» → сохранено."""
    admin, kb, _ = console

    menu = admin.handle("chat-1", "/admin")
    assert "Управление ботом" in menu, "после /admin должно открываться меню"
    listing = admin.handle("chat-1", "1")
    assert "Какой зал правим" in listing
    assert "1." in listing

    picked = admin.handle("chat-1", "1")
    assert "Сейчас:" in picked

    preview = admin.handle("chat-1", REAL_KSK)
    assert "Вот что я понял" in preview
    assert "Кикбоксинг" in preview and "Бокс" in preview

    saved = admin.handle("chat-1", "да")
    assert "Готово" in saved


def test_admin_refusal_changes_nothing(console) -> None:
    """«Нет» не сохраняет: администратор должен иметь право передумать."""
    admin, kb, _ = console
    admin.handle("chat-2", "/admin")
    admin.handle("chat-2", "1")   # раздел «Расписание залов»
    admin.handle("chat-2", "1")   # первый зал в списке
    before = read_schedule(kb, "center_kairbekova_24")
    admin.handle("chat-2", REAL_KSK)
    answer = admin.handle("chat-2", "нет")

    assert "не сохранил" in answer.lower()
    assert read_schedule(kb, "center_kairbekova_24") == before


def test_admin_reports_parse_errors_instead_of_saving_garbage(console) -> None:
    """Мусор не сохраняется молча — администратор видит, что именно не понято."""
    admin, _, _ = console
    admin.handle("chat-3", "/admin")
    admin.handle("chat-3", "1")
    admin.handle("chat-3", "1")
    answer = admin.handle("chat-3", "какой-то текст без расписания")

    assert "не смог разобрать" in answer.lower()


def test_admin_session_is_per_chat(console) -> None:
    """Две правки одновременно не мешают друг другу."""
    admin, _, _ = console
    admin.handle("chat-a", "/admin")
    admin.handle("chat-b", "/admin")
    admin.handle("chat-a", "1")
    admin.handle("chat-a", "1")

    assert admin.session("chat-a").gym_id is not None
    assert admin.session("chat-b").gym_id is None


def test_cancel_leaves_edit_mode(console) -> None:
    """«Отмена» возвращает бота к обслуживанию клиентов."""
    admin, _, _ = console
    admin.handle("chat-4", "/admin")
    assert admin.active("chat-4") is True
    admin.handle("chat-4", "отмена")
    assert admin.active("chat-4") is False


def test_non_admin_is_rejected() -> None:
    """Список админов задан — посторонний правит расписание только через отказ."""
    from app.admin.telegram_admin import is_admin

    assert is_admin(111, (111, 222)) is True
    assert is_admin(999, (111, 222)) is False
    assert is_admin(None, (111,)) is False
    # Список пуст — админка открыта: это режим первого запуска, он логируется.
    assert is_admin(999, ()) is True


# --------------------------------------------------------------------------- #
# Приём фото и видео от администратора
# --------------------------------------------------------------------------- #
def test_slugify_makes_a_valid_artifact_id() -> None:
    """Русское описание превращается в допустимый id ``^[a-z0-9_]+$``."""
    from app.admin.media_store import slugify
    import re as _re

    for text in ("Тренировка младшей группы", "Дорога до зала — КСК", "Жаттығу үрдісі"):
        slug = slugify(text)
        assert _re.fullmatch(r"[a-z0-9_]+", slug), f"{text!r} -> {slug!r}"


def test_photo_from_admin_is_registered_and_sendable(sandbox, tmp_path) -> None:
    """Файл с подписью попадает в базу знаний и становится доступен боту."""
    from app.admin.media_store import register_media
    from app.kb.loader import load_sync

    kb, media_src = sandbox
    media = tmp_path / "media"
    media.mkdir()
    for existing in media_src.iterdir():
        if existing.is_file():
            (media / existing.name).write_bytes(existing.read_bytes())

    source = tmp_path / "incoming.jpg"
    source.write_bytes(b"\xff\xd8\xff" + b"0" * 2048)  # достаточно любого содержимого

    result = register_media(
        kb_dir=kb, media_dir=media, schema_version=1, source=source, kind="image",
        when_to_send="тренировка младшей группы — когда спрашивают, как проходят занятия",
        title_ru="Тренировка младшей группы",
    )

    assert result.artifact_id.startswith("photo_")
    assert (media / result.file_name).is_file(), "файл не лёг в каталог медиа"

    snapshot, problems = load_sync(kb, media_dir=media, schema_version=1)
    assert not problems
    artifact = snapshot.artifact(result.artifact_id)
    assert artifact is not None and artifact.enabled
    assert "как проходят занятия" in artifact.when_to_send_ru
    # Подпись не выдумывается: её пишет модель на языке клиента.
    assert artifact.body.ru is None


def test_video_from_admin_is_telegram_only(sandbox, tmp_path) -> None:
    """Видео помечается доставляемым только в Telegram — в других каналах оно не пройдёт."""
    from app.admin.media_store import register_media
    from app.kb.loader import load_sync

    kb, media_src = sandbox
    media = tmp_path / "media"
    media.mkdir()
    for existing in media_src.iterdir():
        if existing.is_file():
            (media / existing.name).write_bytes(existing.read_bytes())

    source = tmp_path / "incoming.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 4096)

    result = register_media(
        kb_dir=kb, media_dir=media, schema_version=1, source=source, kind="video",
        when_to_send="дорога до зала на КСК — когда спрашивают, как добраться",
        title_ru="Дорога до зала КСК", gym_id="ksk_kairbekova_334",
    )

    snapshot, _ = load_sync(kb, media_dir=media, schema_version=1)
    artifact = snapshot.artifact(result.artifact_id)
    assert artifact.channels.get("telegram") == "allow"
    assert artifact.channels.get("whatsapp") == "deny"
    assert artifact.channels.get("instagram") == "deny"
    assert artifact.gym_id == "ksk_kairbekova_334"


def test_media_without_description_is_refused(sandbox, tmp_path) -> None:
    """Без подписи материал бесполезен: бот не поймёт, когда его показывать."""
    from app.admin.media_store import MediaWriteError, register_media

    kb, media = sandbox
    source = tmp_path / "x.jpg"
    source.write_bytes(b"\xff\xd8\xff" + b"0" * 100)

    with pytest.raises(MediaWriteError) as exc:
        register_media(kb_dir=kb, media_dir=tmp_path, schema_version=1, source=source,
                       kind="image", when_to_send="  ", title_ru="Фото")
    assert "когда отправлять" in str(exc.value)


def test_broken_media_registration_rolls_back(sandbox, tmp_path) -> None:
    """Отклонённый материал не оставляет ни файла, ни следа в базе."""
    from app.admin.media_store import MediaWriteError, register_media

    kb, media = sandbox
    source = tmp_path / "x.jpg"
    source.write_bytes(b"\xff\xd8\xff" + b"0" * 100)
    before = (kb / "media.yaml").read_text()

    with pytest.raises(MediaWriteError):
        register_media(kb_dir=kb, media_dir=tmp_path, schema_version=1, source=source,
                       kind="karate", when_to_send="описание достаточной длины", title_ru="Ф")
    assert (kb / "media.yaml").read_text() == before, "media.yaml изменился при отказе"
