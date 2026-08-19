"""Пусковой процесс: подготовка диска перед стартом бота и CRM.

Проверяется то, что ломается тихо и дорого: подмена каталога данных (вся история
диалогов теряется при передеплое) и перезапись правок владельца файлами из образа
(неделя работы с ценами и расписанием исчезает без следа).
"""

from __future__ import annotations

import importlib.util
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
