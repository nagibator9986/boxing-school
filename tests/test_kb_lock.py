"""Блокировка записи в базу знаний: две правки не затирают друг друга."""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import pytest

from app.admin.kb_lock import KBLockTimeout, kb_write_lock
from crm.kbio import KBEditError, KBEditor

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def kb_copy(tmp_path: Path) -> Path:
    """Копия базы знаний: тесты в неё пишут."""
    shutil.copytree(ROOT / "kb", tmp_path / "kb")
    shutil.copytree(ROOT / "media", tmp_path / "media")
    return tmp_path


def _editor(base: Path) -> KBEditor:
    return KBEditor(base / "kb", media_dir=base / "media", schema_version=1)


def test_lock_is_exclusive(kb_copy: Path) -> None:
    """Пока один держит блокировку, второй ждёт, а не пишет параллельно."""
    holder_ready = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with kb_write_lock(kb_copy / "kb"):
            holder_ready.set()
            release.wait(timeout=2)

    thread = threading.Thread(target=hold)
    thread.start()
    assert holder_ready.wait(timeout=2)

    with pytest.raises(KBLockTimeout), kb_write_lock(kb_copy / "kb", timeout_s=0.2):
        pass  # pragma: no cover - сюда попасть не должны

    release.set()
    thread.join(timeout=2)

    # После освобождения блокировка берётся сразу.
    with kb_write_lock(kb_copy / "kb", timeout_s=1):
        pass


def test_save_waits_for_lock(kb_copy: Path) -> None:
    """Сохранение ждёт освобождения, а не падает и не пишет поверх."""
    editor = _editor(kb_copy)
    started = threading.Event()

    def hold() -> None:
        with kb_write_lock(kb_copy / "kb"):
            started.set()
            time.sleep(0.3)

    thread = threading.Thread(target=hold)
    thread.start()
    assert started.wait(timeout=2)

    document = editor.load("faq.yaml")
    document["entries"][0]["topic"] = "trial"
    begin = time.monotonic()
    editor.save({"faq.yaml": document})
    waited = time.monotonic() - begin

    thread.join(timeout=2)
    assert waited >= 0.2, "правка прошла, не дождавшись чужой — блокировка не работает"


def test_two_writers_do_not_lose_changes(kb_copy: Path) -> None:
    """Две одновременные правки разных файлов обе доживают до диска.

    Без блокировки откат неудачной правки возвращал файлы из своей копии и
    вместе с собой стирал чужую удачную — при том, что обе стороны видели
    «сохранено».
    """
    results: list[str] = []
    errors: list[Exception] = []

    def write(file_name: str, mutate) -> None:  # type: ignore[no-untyped-def]
        try:
            editor = _editor(kb_copy)
            document = editor.load(file_name)
            mutate(document)
            results.append(editor.save({file_name: document}).kb_hash)
        except Exception as exc:  # noqa: BLE001 - ошибку показываем в проверке
            errors.append(exc)

    threads = [
        threading.Thread(
            target=write, args=("gyms.yaml", lambda d: d["gyms"][0].update({"phone": "+77000000001"}))
        ),
        threading.Thread(
            target=write, args=("policies.yaml", lambda d: d.update({"sla_reply_minutes": 15}))
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, f"правка не прошла: {errors}"
    assert len(results) == 2

    editor = _editor(kb_copy)
    assert editor.load("gyms.yaml")["gyms"][0]["phone"] == "+77000000001"
    assert editor.load("policies.yaml")["sla_reply_minutes"] == 15


def test_lock_timeout_is_readable(kb_copy: Path) -> None:
    """Занятая база сообщает об этом по-человечески, а не трассировкой."""
    editor = KBEditor(
        kb_copy / "kb", media_dir=kb_copy / "media", schema_version=1, lock_timeout_s=0.2
    )
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with kb_write_lock(kb_copy / "kb"):
            holding.set()
            release.wait(timeout=3)

    thread = threading.Thread(target=hold)
    thread.start()
    assert holding.wait(timeout=2)
    try:
        document = editor.load("faq.yaml")
        with pytest.raises(KBEditError) as info:
            editor.save({"faq.yaml": document})
        assert "занята" in str(info.value)
    finally:
        release.set()
        thread.join(timeout=3)
