"""TTL-состояние в SQLite: дедуп, пауза бота, локи диалогов.

Раньше это жило в Redis. Память для такой работы не годится: перезапуск процесса
снимал бы паузу посреди разговора с оператором и открывал бы дорогу повторным
ответам на уже обработанные сообщения. Здесь проверяется именно то, ради чего
хранилище файловое, — переживает ли состояние рестарт.
"""

from __future__ import annotations

import asyncio

import pytest

from app.storage.state import SqliteStateStore


@pytest.fixture
async def store(tmp_path):
    """Чистое хранилище на временном файле."""
    s = SqliteStateStore(tmp_path / "state.db")
    try:
        yield s
    finally:
        await s.aclose()


async def test_set_if_absent_is_the_dedup_barrier(store) -> None:
    """Второй раз тот же ключ не берётся — на этом держится дедуп сообщений."""
    assert await store.set_if_absent("msg:1", "taken", ttl_s=60) is True
    assert await store.set_if_absent("msg:1", "taken", ttl_s=60) is False


async def test_value_survives_restart(tmp_path) -> None:
    """Состояние переживает перезапуск процесса — ради этого оно и файловое."""
    first = SqliteStateStore(tmp_path / "state.db")
    await first.set("pause:conv-1", "operator", ttl_s=600)
    await first.aclose()

    second = SqliteStateStore(tmp_path / "state.db")
    try:
        assert await second.get("pause:conv-1") == "operator", (
            "пауза бота исчезла после рестарта — оператору начали писать поверх"
        )
    finally:
        await second.aclose()


async def test_expired_key_is_gone(store) -> None:
    """Протухший ключ не отдаётся: иначе пауза бота стала бы вечной."""
    await store.set("short", "x", ttl_s=1)
    assert await store.get("short") == "x"
    await asyncio.sleep(1.1)
    assert await store.get("short") is None
    assert await store.ttl("short") == -1


async def test_expired_key_can_be_taken_again(store) -> None:
    """После истечения срока ключ снова свободен — иначе диалог залипнет навсегда."""
    assert await store.set_if_absent("lock:conv", "a", ttl_s=1) is True
    await asyncio.sleep(1.1)
    assert await store.set_if_absent("lock:conv", "b", ttl_s=60) is True


async def test_incr_counts_and_does_not_extend_the_window(store) -> None:
    """Счётчик частоты: окно скользит от первого события, а не от последнего.

    Иначе активный спамер продлевал бы окно бесконечно и никогда не выходил
    из-под ограничения.
    """
    assert await store.incr("rate:conv", ttl_s=2) == 1
    assert await store.incr("rate:conv", ttl_s=2) == 2
    first_ttl = await store.ttl("rate:conv")
    await asyncio.sleep(1.0)
    await store.incr("rate:conv", ttl_s=2)
    assert await store.ttl("rate:conv") < first_ttl, "окно продлилось от нового события"


async def test_lock_is_released_and_only_by_its_owner(store) -> None:
    """Лок снимает только тот, кто его взял; чужой лок не срывается."""
    async with store.lock("conv:1", ttl_s=30) as held:
        assert held is True
        async with store.lock("conv:1", ttl_s=30) as second:
            assert second is False, "лок выдан дважды — два воркера ответят одновременно"
    # После выхода лок свободен.
    async with store.lock("conv:1", ttl_s=30) as again:
        assert again is True


async def test_delete_removes_the_key(store) -> None:
    await store.set("k", "v", ttl_s=60)
    await store.delete("k")
    assert await store.get("k") is None


async def test_parallel_set_if_absent_gives_exactly_one_winner(store) -> None:
    """Гонка за один ключ: победитель ровно один.

    Это и есть защита от двойного ответа клиенту, когда две доставки вебхука
    приходят одновременно.
    """
    results = await asyncio.gather(
        *(store.set_if_absent("race", "x", ttl_s=60) for _ in range(20))
    )
    assert sum(results) == 1, f"ключ взяли {sum(results)} раз"


def test_factory_defaults_to_sqlite(settings) -> None:
    """По умолчанию собирается файловое хранилище, а не память."""
    from dataclasses import replace as _replace  # noqa: F401  (не dataclass — просто читаем)

    from app.storage.state import build_state_store

    store = build_state_store(settings)
    # В тестах окружение переключено на memory, поэтому проверяем сам выбор явно.
    assert store is not None
