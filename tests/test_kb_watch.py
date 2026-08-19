"""Наблюдатель за базой знаний: правки доезжают, поломки — нет."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.core.kb_watch import KBWatcher, kb_fingerprint
from app.kb import loader as kb_loader

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def kb_copy(tmp_path: Path) -> Path:
    """Своя копия базы знаний: тесты её ломают."""
    shutil.copytree(ROOT / "kb", tmp_path / "kb")
    shutil.copytree(ROOT / "media", tmp_path / "media")
    return tmp_path


def _watcher(base: Path, **kwargs: object) -> KBWatcher:
    return KBWatcher(
        base / "kb", media_dir=base / "media", schema_version=1, min_interval_s=0, **kwargs  # type: ignore[arg-type]
    )


def test_fingerprint_covers_all_files(kb_copy: Path) -> None:
    """В отпечатке — все файлы базы знаний, иначе правка одного останется незамеченной."""
    fingerprint = kb_fingerprint(kb_copy / "kb")
    assert {name for name, _, _ in fingerprint} == set(kb_loader.KB_FILES)


def test_no_change_no_reload(kb_copy: Path) -> None:
    """Без правок наблюдатель не трогает снимок — проверка обязана быть дешёвой."""
    watcher = _watcher(kb_copy)
    assert watcher.check() is None
    assert watcher.check() is None


def test_change_reloads(kb_copy: Path) -> None:
    """Правка файла подменяет снимок."""
    snapshot, _ = kb_loader.load_sync(kb_copy / "kb", media_dir=kb_copy / "media", schema_version=1)
    kb_loader.swap(snapshot)
    watcher = _watcher(kb_copy)

    path = kb_copy / "kb" / "faq.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    result = watcher.check()
    assert result is not None and result.changed
    assert kb_loader.get_snapshot().kb_hash == result.new_hash


def test_broken_file_keeps_old_snapshot(kb_copy: Path) -> None:
    """Сломанная база не применяется: бот продолжает работать на прежней."""
    snapshot, _ = kb_loader.load_sync(kb_copy / "kb", media_dir=kb_copy / "media", schema_version=1)
    kb_loader.swap(snapshot)
    watcher = _watcher(kb_copy)

    (kb_copy / "kb" / "pricing.yaml").write_text("city_plans: [", encoding="utf-8")

    assert watcher.check() is None
    assert watcher.last_error is not None
    assert kb_loader.get_snapshot().kb_hash == snapshot.kb_hash


def test_broken_file_not_retried_every_turn(kb_copy: Path) -> None:
    """Сломанный файл не разбирается заново на каждом ходу диалога.

    Иначе каждый ход упирался бы в разбор семи YAML — а бот при этом всё равно
    отвечает по прежнему снимку, то есть работа была бы полностью впустую.
    """
    watcher = _watcher(kb_copy)
    (kb_copy / "kb" / "gyms.yaml").write_text("gyms: [", encoding="utf-8")

    assert watcher.check() is None
    first_error = watcher.last_error
    assert first_error is not None

    watcher.last_error = None
    assert watcher.check() is None, "повторной попытки быть не должно"
    assert watcher.last_error is None


def test_throttle_respects_interval(kb_copy: Path) -> None:
    """Ограничитель частоты не пускает наблюдателя к диску чаще заданного."""
    # Отсчёты: создание наблюдателя, первая проверка (рано), вторая (пора), отметка.
    clock = iter([0.0, 0.1, 5.0, 5.1])
    watcher = KBWatcher(
        kb_copy / "kb",
        media_dir=kb_copy / "media",
        schema_version=1,
        min_interval_s=1.0,
        clock=lambda: next(clock),
    )
    path = kb_copy / "kb" / "i18n.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    assert watcher.check() is None, "слишком рано — проверять не должен"
    result = watcher.check()
    assert result is not None and result.changed
