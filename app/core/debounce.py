"""Склейка серии сообщений и одновременность на диалог.

Родитель почти никогда не пишет одним сообщением. Типичная серия:

    Здравствуйте
    сколько стоит
    а где вы находитесь

Три вебхука за четыре секунды. Без склейки бот отвечает трижды, причём на
первое сообщение — приветствием, на второе — ценой без контекста, а третье
обгоняет второе, потому что задачи выполняются параллельно. Клиент видит кашу.

Поэтому здесь два механизма:

* **окно склейки** — ``debounce_seconds`` (5 с) с продлением каждым новым
  сообщением, но не дольше ``debounce_max_seconds`` (8 с). Считает не таймер в
  памяти процесса, а счётчик в общем состоянии: воркеров может быть несколько;
* **лок диалога** — одновременно один ход на ``conv_key``. Два воркера не
  отвечают в один чат, даже если окна почему-то разошлись.

Буфер и счётчик живут в ключе ``key_debounce(conv_key)`` (счётчик — в его
суффиксе ``:seq``), лок — в ``key_lock_conversation(conv_key)``. Отказ Redis не
ломает диалог: без общего состояния серия просто не склеивается, каждое
сообщение обрабатывается само по себе.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, AsyncIterator, Final, Sequence

from app.logging_conf import get_logger
from app.storage.state import StateStore, key_debounce, key_lock_conversation

__all__ = ["collect", "conversation_lock", "join_messages", "restore"]

_log = get_logger(__name__)

#: Суффикс счётчика серии рядом с буфером — отдельного пространства ключей не заводим.
_SEQ_SUFFIX: Final[str] = ":seq"
#: Больше этого числа кусков в одной серии не бывает; хвост отбрасывается.
_MAX_PARTS: Final[int] = 12
#: Шаг опроса занятого лока диалога, секунды.
_LOCK_POLL_S: Final[float] = 0.25


async def collect(
    state: StateStore, conv_key: str, text: str, *, window_s: int, max_window_s: int
) -> str | None:
    """Кладёт текст в буфер серии. ``None`` — окно ещё открыто, обрабатывать рано.

    Строка — окно закрылось, вернулась склейка сообщений в порядке поступления.

    Механика: сообщение дописывается в общий буфер и увеличивает счётчик серии,
    затем задача спит до конца окна. Если за это время счётчик изменился —
    пришло более позднее сообщение, и закрывать серию будет оно, а текущая
    задача возвращает ``None``. Общая длина окна ограничена ``max_window_s``:
    иначе клиент, пишущий по слову каждые четыре секунды, не получит ответа
    никогда.
    """
    if window_s <= 0:
        return text

    buffer_key = key_debounce(conv_key)
    seq_key = buffer_key + _SEQ_SUFFIX
    ttl = max(1, int(max_window_s) * 2)

    try:
        buffer = _decode(await state.get(buffer_key))
        if not buffer["parts"]:
            buffer["started_at"] = time.time()
        buffer["parts"] = [*buffer["parts"], text][-_MAX_PARTS:]
        await state.set(buffer_key, _encode(buffer), ttl)
        sequence = await state.incr(seq_key, ttl)
    except Exception as exc:  # состояние недоступно — работаем без склейки
        _log.warning("debounce_state_unavailable", error=str(exc))
        return text

    elapsed = max(0.0, time.time() - float(buffer["started_at"]))
    wait = min(float(window_s), max(0.0, float(max_window_s) - elapsed))
    if wait > 0:
        await asyncio.sleep(wait)

    try:
        current = await state.get(seq_key)
        if current is not None and int(current) != int(sequence):
            _log.info("debounce_superseded", parts=len(buffer["parts"]))
            return None

        final = _decode(await state.get(buffer_key))
        await state.delete(buffer_key)
        await state.delete(seq_key)
    except Exception as exc:  # pragma: no cover - деградация до одиночного сообщения
        _log.warning("debounce_flush_failed", error=str(exc))
        return text

    parts = final["parts"] or [text]
    return join_messages(parts)


async def restore(state: StateStore, conv_key: str, text: str, *, max_window_s: int) -> bool:
    """Возвращает несостоявшийся текст в буфер серии. ``True`` — вернули.

    Последняя страховка отложенного хода: если лок так и не освободился, реплика
    клиента хотя бы доедет до модели вместе со следующим его сообщением, а не
    исчезнет бесследно.
    """
    if not text:
        return False
    buffer_key = key_debounce(conv_key)
    ttl = max(1, int(max_window_s) * 2)
    try:
        buffer = _decode(await state.get(buffer_key))
        buffer["parts"] = [text, *buffer["parts"]][-_MAX_PARTS:]
        await state.set(buffer_key, _encode(buffer), ttl)
    except Exception as exc:  # pragma: no cover - состояние недоступно
        _log.warning("debounce_restore_failed", error=str(exc))
        return False
    return True


@asynccontextmanager
async def conversation_lock(
    state: StateStore, conv_key: str, *, ttl_s: int, wait_s: float = 0.0
) -> AsyncIterator[bool]:
    """Одновременность на диалог — ровно 1. ``False`` — лок занят, ход не состоится.

    Лок **ждущий**, и это принципиально. Раньше он был мгновенным: сообщение,
    пришедшее во время хода бота, получало ``False`` и пропадало навсегда —
    задачу никто не переставлял в очередь, а вебхуку Wazzup уже ответили 200.
    Родитель, дописавший «а во сколько тренировки?», не получал ответа никогда.

    Теперь задача дожидается освобождения лока в пределах ``wait_s`` и после
    этого честно доводит свой ход до конца — с уже дописанной историей первого
    хода. Бюджет ожидания настроен так, чтобы покрывать самый долгий ход
    (``conv_lock_wait_seconds`` ≥ ``llm_turn_budget_s``), поэтому отказ по
    исчерпанию бюджета означает зависший или убитый чужой процесс — и тогда лок
    всё равно снимется по TTL.
    """
    key = key_lock_conversation(conv_key)
    ttl = max(1, int(ttl_s))
    deadline = time.monotonic() + max(0.0, float(wait_s))

    try:
        stack = await _acquire(state, key, ttl, deadline)
    except Exception as exc:  # состояние недоступно — обрабатываем без лока
        _log.warning("conv_lock_unavailable", error=str(exc))
        yield True
        return

    if stack is None:
        _log.error("conv_lock_busy", conv_key=conv_key, waited_s=round(max(0.0, wait_s), 1))
        yield False
        return

    # Тело хода выполняется ВНЕ try/except: его исключения обязаны дойти до
    # пайплайна, а лок снимается выходом из ``stack``.
    async with stack:
        yield True


async def _acquire(
    state: StateStore, key: str, ttl: int, deadline: float
) -> AsyncExitStack | None:
    """Захват лока с ожиданием. ``None`` — бюджет ожидания исчерпан."""
    while True:
        stack = AsyncExitStack()
        acquired = bool(await stack.enter_async_context(state.lock(key, ttl)))
        if acquired:
            return stack
        await stack.aclose()
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(_LOCK_POLL_S)


def join_messages(parts: Sequence[str]) -> str:
    """Склейка серии: перевод строки между частями, дубли подряд схлопываются.

    Повтор бывает и от клиента («сколько стоит», «сколько стоит?»), и от
    повторной доставки вебхука. Модель от повторов только теряется.
    """
    joined: list[str] = []
    for part in parts:
        cleaned = (part or "").strip()
        if not cleaned:
            continue
        if joined and _same_meaning(joined[-1], cleaned):
            continue
        joined.append(cleaned)
    return "\n".join(joined)


def _same_meaning(left: str, right: str) -> bool:
    """Считаются ли две подряд идущие реплики повтором друг друга."""
    strip_chars = " \t.,!?…"
    return left.strip(strip_chars).casefold() == right.strip(strip_chars).casefold()


def _decode(raw: str | None) -> dict[str, Any]:
    """Буфер серии из строки состояния; битое значение — пустой буфер."""
    if not raw:
        return {"started_at": time.time(), "parts": []}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):  # pragma: no cover - чужое значение в ключе
        return {"started_at": time.time(), "parts": []}
    parts = data.get("parts")
    return {
        "started_at": float(data.get("started_at") or time.time()),
        "parts": [str(item) for item in parts] if isinstance(parts, list) else [],
    }


def _encode(buffer: dict[str, Any]) -> str:
    """Буфер серии в строку состояния."""
    return json.dumps(buffer, ensure_ascii=False)
