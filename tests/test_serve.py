"""Пусковой процесс: подготовка диска перед стартом бота и CRM.

Проверяется то, что ломается тихо и дорого: подмена каталога данных (вся история
диалогов теряется при передеплое) и перезапись правок владельца файлами из образа
(неделя работы с ценами и расписанием исчезает без следа).
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _serve():  # type: ignore[no-untyped-def]
    """Загружает ``scripts/serve.py`` как модуль: пакетом он не является."""
    spec = importlib.util.spec_from_file_location("serve_module", ROOT / "scripts" / "serve.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["serve_module"] = module
    spec.loader.exec_module(module)
    return module


serve = _serve()


def test_explicit_data_dir_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Заданный каталог данных используется как есть."""
    target = tmp_path / "volume"
    monkeypatch.setenv("DATA_DIR", str(target))
    assert serve.resolve_data_dir() == target
    assert target.is_dir()


def test_unwritable_data_dir_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Недоступный на запись том останавливает запуск, а не подменяется тихо.

    Молчаливый уход в каталог внутри образа выглядел бы как исправная служба —
    и терял бы историю диалогов, заявки и права администраторов при каждом
    передеплое. Такую поломку не находят месяцами.
    """
    blocked = tmp_path / "readonly"
    blocked.mkdir()
    blocked.chmod(0o500)
    monkeypatch.setenv("DATA_DIR", str(blocked))
    try:
        with pytest.raises(SystemExit) as info:
            serve.resolve_data_dir()
        assert "недоступен на запись" in str(info.value)
    finally:
        blocked.chmod(0o700)


def test_seed_copies_once(tmp_path: Path) -> None:
    """Первый запуск наполняет том, повторный ничего не копирует."""
    first = serve.seed_from_image(tmp_path)
    assert first["kb"] >= 7 and first["media"] >= 1
    assert (tmp_path / "kb" / "gyms.yaml").is_file()

    second = serve.seed_from_image(tmp_path)
    assert second == {"kb": 0, "media": 0}


def test_seed_never_overwrites_owner_edits(tmp_path: Path) -> None:
    """Файл с диска важнее файла из образа.

    Иначе передеплой возвращал бы цены и расписание к состоянию репозитория —
    молча отменяя всё, что владелец наменял через CRM.
    """
    serve.seed_from_image(tmp_path)
    edited = tmp_path / "kb" / "pricing.yaml"
    edited.write_text("# правка владельца\n", encoding="utf-8")

    serve.seed_from_image(tmp_path)
    assert edited.read_text(encoding="utf-8") == "# правка владельца\n"


def test_seed_adds_files_from_new_release(tmp_path: Path) -> None:
    """Файл, которого на диске нет, приезжает из образа."""
    serve.seed_from_image(tmp_path)
    (tmp_path / "kb" / "lexicon.yaml").unlink()
    assert serve.seed_from_image(tmp_path)["kb"] == 1
    assert (tmp_path / "kb" / "lexicon.yaml").is_file()


def test_env_points_bot_and_crm_to_same_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Бот и CRM получают одни и те же пути.

    Разъехавшись, они работают с разными копиями данных: владелец правит цену в
    CRM, бот отвечает по своей — и это никак не проявляется до первого клиента.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    env = serve.prepare_env(tmp_path)
    assert env["KB_DIR"] == str(tmp_path / "kb")
    assert env["MEDIA_DIR"] == str(tmp_path / "media")
    assert env["CRM_BOT_DB"] == str(tmp_path / "bot.db")
    assert env["CRM_BOT_DB"] in env["DATABASE_URL"]
    assert env["ADMIN_DB_PATH"] == str(tmp_path / "admin.db")
    assert env["STATE_SQLITE_PATH"] == str(tmp_path / "state.db")


def test_inline_worker_is_forced_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Без inline-воркера эта служба принимает сообщения и не отвечает ни на одно.

    Отдельного процесса ARQ ``serve.py`` не запускает, поэтому при
    ``INLINE_WORKER=false`` ответы уходили бы в очередь, которую некому читать,
    а строки ``outbox`` оставались бы ``pending``. Снаружи это выглядит как
    полностью исправная служба и молчащий бот.
    """
    monkeypatch.delenv("INLINE_WORKER", raising=False)
    assert serve.prepare_env(tmp_path)["INLINE_WORKER"] == "true"


def test_explicit_inline_worker_off_is_overridden_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Явное «false» тоже переопределяется — но молча этого делать нельзя."""
    monkeypatch.setenv("INLINE_WORKER", "false")
    env = serve.prepare_env(tmp_path)

    assert env["INLINE_WORKER"] == "true"
    assert "переопределён на true" in capsys.readouterr().out


def test_relative_sqlite_url_is_moved_onto_the_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Относительный путь в ``DATABASE_URL`` — молчаливая потеря данных.

    Ровно этот образец лежит в документации: ``sqlite+aiosqlite:///data/bot.db``.
    Заданный на Railway, он отправлял бота писать в ``/app/data/bot.db`` внутри
    контейнера, пока CRM читала ``/data/bot.db`` на томе: обе вкладки пустые,
    всё написанное пропадает при следующем передеплое.
    """
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///data/bot.db")
    env = serve.prepare_env(tmp_path)

    assert env["CRM_BOT_DB"] == str(tmp_path / "bot.db")
    assert env["CRM_BOT_DB"] in env["DATABASE_URL"]
    assert "переписан на том" in capsys.readouterr().out


def test_absolute_sqlite_url_is_followed_by_crm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Осознанный абсолютный путь остаётся за владельцем — CRM идёт следом."""
    target = tmp_path / "custom" / "place.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{target}")
    env = serve.prepare_env(tmp_path)

    assert env["DATABASE_URL"] == f"sqlite+aiosqlite:///{target}"
    assert env["CRM_BOT_DB"] == str(target)


def test_postgres_url_is_kept_and_warned_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Postgres — законный выбор, но CRM его не читает, и это обязано быть видно."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@host:5432/db")
    env = serve.prepare_env(tmp_path)

    assert env["DATABASE_URL"] == "postgresql+asyncpg://u:p@host:5432/db"
    assert "не на SQLite" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite+aiosqlite:///data/bot.db", "data/bot.db"),
        ("sqlite+aiosqlite:////data/bot.db", "/data/bot.db"),
        ("sqlite:///./data/bot.db", "data/bot.db"),
        ("sqlite+aiosqlite:///:memory:", None),
        ("postgresql+asyncpg://u:p@h/db", None),
        ("не url вовсе", None),
    ],
)
def test_sqlite_file_of(url: str, expected: str | None) -> None:
    """Три косые — путь относительный, четыре — абсолютный. Разница в один символ."""
    result = serve.sqlite_file_of(url)
    assert (str(result) if result is not None else None) == expected


def test_broken_kb_does_not_block_startup(tmp_path: Path) -> None:
    """Сломанная база знаний не мешает поднять CRM — ею её и чинят."""
    serve.seed_from_image(tmp_path)
    (tmp_path / "kb" / "gyms.yaml").write_text("gyms: [", encoding="utf-8")
    env = serve.prepare_env(tmp_path)
    assert serve.check_kb(env) is False


def test_warns_when_volume_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """На Railway без тома служба громко предупреждает о потере данных.

    Каталог /data есть в образе, поэтому без тома всё выглядит исправным —
    и данные исчезают при первом же передеплое.
    """
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert serve.warn_if_not_a_volume(tmp_path) is False
    printed = capsys.readouterr().out
    assert "не подключённый том" in printed
    assert "Add Volume" in printed


def test_no_warning_outside_railway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Локальный запуск не ругается: там каталог рядом с проектом — норма."""
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    serve.warn_if_not_a_volume(tmp_path)
    assert "ВНИМАНИЕ" not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Новые версии базы из репозитория на подключённом томе
# --------------------------------------------------------------------------- #
def _image(tmp_path: Path, files: dict[str, str]) -> Path:
    """Образ с указанными файлами ``kb/…`` и ``media/…``."""
    root = tmp_path / "image"
    for relative, body in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


def test_untouched_file_takes_the_new_release(tmp_path: Path) -> None:
    """Владелец 01.10.2026 подключил том — и правки из репозитория перестали доезжать.

    Файл, который никто не правил, обязан обновиться, а прежняя база — лечь в
    резервную копию, которую видно в CRM.
    """
    data = tmp_path / "data"
    image = _image(tmp_path, {"kb/faq.yaml": "v1\n", "media/route.mp4": "видео 1"})
    serve.seed_from_image(data, image_root=image)

    _image(tmp_path, {"kb/faq.yaml": "v2\n", "media/route.mp4": "видео 2"})
    report = serve.seed_from_image(data, image_root=image)

    assert (data / "kb" / "faq.yaml").read_text(encoding="utf-8") == "v2\n"
    assert (data / "media" / "route.mp4").read_text(encoding="utf-8") == "видео 2"
    assert report == {"kb": 1, "media": 1}
    backups = list((data / "kb" / ".backups").glob("*-repo/faq.yaml"))
    assert [b.read_text(encoding="utf-8") for b in backups] == ["v1\n"], "прежняя версия — в копии"


def test_owner_edit_survives_a_new_release(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    image = _image(tmp_path, {"kb/pricing.yaml": "v1\n"})
    serve.seed_from_image(data, image_root=image)
    (data / "kb" / "pricing.yaml").write_text("# правка владельца\n", encoding="utf-8")

    _image(tmp_path, {"kb/pricing.yaml": "v2\n"})
    serve.seed_from_image(data, image_root=image)

    assert (data / "kb" / "pricing.yaml").read_text(encoding="utf-8") == "# правка владельца\n"
    assert "правили в CRM" in capsys.readouterr().out, "о новой версии в репозитории — в журнал"


def test_file_without_history_is_taken_under_watch_only_when_it_matches(tmp_path: Path) -> None:
    """Том подключили до этого правила: чья версия на диске — неизвестно."""
    data = tmp_path / "data"
    image = _image(tmp_path, {"kb/gyms.yaml": "v1\n", "kb/faq.yaml": "v1\n"})
    (data / "kb").mkdir(parents=True)
    (data / "kb" / "gyms.yaml").write_text("v1\n", encoding="utf-8")   # совпадает с образом
    (data / "kb" / "faq.yaml").write_text("старое\n", encoding="utf-8")  # не совпадает
    serve.seed_from_image(data, image_root=image)

    _image(tmp_path, {"kb/gyms.yaml": "v2\n", "kb/faq.yaml": "v2\n"})
    serve.seed_from_image(data, image_root=image)

    assert (data / "kb" / "gyms.yaml").read_text(encoding="utf-8") == "v2\n"
    assert (data / "kb" / "faq.yaml").read_text(encoding="utf-8") == "старое\n"


def test_broken_seed_state_means_no_history(tmp_path: Path) -> None:
    data = tmp_path / "data"
    image = _image(tmp_path, {"kb/faq.yaml": "v1\n"})
    serve.seed_from_image(data, image_root=image)
    (data / serve.SEED_STATE).write_text("{оборвано", encoding="utf-8")
    (data / "kb" / "faq.yaml").write_text("# правка владельца\n", encoding="utf-8")

    _image(tmp_path, {"kb/faq.yaml": "v2\n"})
    serve.seed_from_image(data, image_root=image)

    assert (data / "kb" / "faq.yaml").read_text(encoding="utf-8") == "# правка владельца\n"


def test_owner_files_outside_the_image_are_left_alone(tmp_path: Path) -> None:
    data = tmp_path / "data"
    image = _image(tmp_path, {"media/route.mp4": "видео"})
    (data / "media").mkdir(parents=True)
    (data / "media" / "своё.jpg").write_text("фото владельца", encoding="utf-8")

    serve.seed_from_image(data, image_root=image)

    assert (data / "media" / "своё.jpg").read_text(encoding="utf-8") == "фото владельца"


def test_release_that_breaks_the_base_is_rolled_back(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Файл из репозитория не сошёлся с правками владельца — бот не должен остаться без базы."""
    data = tmp_path / "data"
    image = tmp_path / "image"
    shutil.copytree(ROOT / "kb", image / "kb", ignore=shutil.ignore_patterns(".*"))
    shutil.copytree(ROOT / "media", image / "media", ignore=shutil.ignore_patterns(".*"))
    serve.seed_from_image(data, image_root=image)
    good = (data / "kb" / "gyms.yaml").read_text(encoding="utf-8")

    (image / "kb" / "gyms.yaml").write_text("gyms: [\n", encoding="utf-8")
    report = serve.seed_from_image(data, image_root=image)

    assert (data / "kb" / "gyms.yaml").read_text(encoding="utf-8") == good
    assert report["kb"] == 0
    assert serve._kb_loads(data / "kb", data / "media"), "база осталась рабочей"
    assert "возвращена прежняя" in capsys.readouterr().out

    (image / "kb" / "gyms.yaml").write_text(good + "# исправленный выпуск\n", encoding="utf-8")
    serve.seed_from_image(data, image_root=image)
    assert (data / "kb" / "gyms.yaml").read_text(encoding="utf-8").endswith("# исправленный выпуск\n")


def test_backup_before_a_release_can_be_restored_from_crm(tmp_path: Path) -> None:
    """Копия «…-repo» — обычная копия CRM: её видно в списке, и из неё база возвращается."""
    from crm.kbio import KBEditor

    data = tmp_path / "data"
    image = tmp_path / "image"
    shutil.copytree(ROOT / "kb", image / "kb", ignore=shutil.ignore_patterns(".*"))
    shutil.copytree(ROOT / "media", image / "media", ignore=shutil.ignore_patterns(".*"))
    serve.seed_from_image(data, image_root=image)
    before = (data / "kb" / "faq.yaml").read_text(encoding="utf-8")

    (image / "kb" / "faq.yaml").write_text(before + "# новый выпуск\n", encoding="utf-8")
    serve.seed_from_image(data, image_root=image)
    assert (data / "kb" / "faq.yaml").read_text(encoding="utf-8").endswith("# новый выпуск\n")

    editor = KBEditor(data / "kb", media_dir=data / "media", schema_version=1)
    stamps = [backup.stamp for backup in editor.backups()]
    assert any(stamp.endswith("-repo") for stamp in stamps), stamps
    editor.restore(next(stamp for stamp in stamps if stamp.endswith("-repo")))
    assert (data / "kb" / "faq.yaml").read_text(encoding="utf-8") == before
