#!/usr/bin/env python
"""Интерактивный диалог с ботом в терминале — на настоящем пайплайне.

Это единственный способ увидеть бота живьём до подключения Wazzup: скрипт гоняет
ровно тот код, который работает в проде (``app.core.pipeline.process_inbound``),
только вместо вебхука — ваш ввод с клавиатуры, а вместо отправки в Wazzup — печать
в консоль.

Два режима, выбираются автоматически:

* **Живой Gemini** — если найден ``GEMINI_API_KEY`` (в окружении или в ``.env``).
  Работают настоящий системный промпт, настоящий выбор инструментов моделью
  и настоящий пост-фильтр по её ответам. Показываются токены и стоимость.
* **Офлайн** — если ключа нет. Модель заменяется скриптованной заглушкой,
  сеть не трогается. Проверяются маршрутизация, язык, дедуп и пост-фильтр,
  но не качество формулировок.

Запуск::

    .venv/bin/python scripts/chat.py
    .venv/bin/python scripts/chat.py --channel instagram --lang kk

Команды внутри диалога — ``/help``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _read_dotenv(path: Path) -> dict[str, str]:
    """Читает ``.env`` без сторонних зависимостей: KEY=VALUE, кавычки снимаются."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            values[key.strip()] = value
    return values


_DOTENV = _read_dotenv(ROOT / ".env")
_GEMINI_KEY = os.environ.get("GEMINI_API_KEY") or _DOTENV.get("GEMINI_API_KEY", "")
_DB_PATH = Path(tempfile.mkdtemp(prefix="ainazarov-chat-")) / "chat.db"

# Окружение выставляется ДО импорта app.config: Settings читает его при создании.
os.environ.update(
    APP_ENV="local",
    DATABASE_URL=f"sqlite+aiosqlite:///{_DB_PATH}",
    STATE_BACKEND="memory",
    REDIS_URL="",
    INLINE_WORKER="false",
    GEMINI_API_KEY=_GEMINI_KEY,
    WAZZUP_API_KEY="chat-local",
    WAZZUP_WEBHOOK_SECRET="chat-secret-chat-secret-123456",
    LOG_LEVEL=os.environ.get("CHAT_LOG_LEVEL", "ERROR"),
    # Диалог в терминале должен отвечать сразу: склейка серии сообщений здесь
    # только заставила бы ждать после каждой строки.
    DEBOUNCE_SECONDS="0",
    SECOND_MESSAGE_DELAY_MS="0",
)

from app.config import get_settings  # noqa: E402
from app.core.pipeline import PipelineDeps, process_inbound  # noqa: E402
from app.kb import loader as kb_loader  # noqa: E402
from app.llm.client import FakeLLMClient, FakeTurn, GeminiClient  # noqa: E402
from app.storage import db as storage_db  # noqa: E402
from app.storage.models import Base  # noqa: E402
from app.storage.state import build_state_store  # noqa: E402
from app.types import ChannelKind, PipelineDecision  # noqa: E402

CHANNEL_UUID = "11111111-1111-1111-1111-111111111111"

DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[36m"
OFF = "\033[0m"

if not sys.stdout.isatty():  # в пайп цвета не пишем
    DIM = BOLD = RED = GREEN = YELLOW = BLUE = OFF = ""


class ConsoleQueue:
    """Очередь-заглушка: задачи регистрируются, но никуда не уходят.

    Отправка в Wazzup и follow-up в интерактивном режиме не нужны — важно, что
    именно пайплайн решил отправить, а не как это доедет до мессенджера.
    """

    def __init__(self) -> None:
        self.inbound: list[dict[str, Any]] = []
        self.outbox: list[tuple[UUID, int]] = []
        self.followups: list[tuple[UUID, datetime]] = []

    async def enqueue_inbound(self, payload: dict[str, Any]) -> str:
        """Регистрирует входящий payload."""
        self.inbound.append(payload)
        return str(uuid4())

    async def enqueue_outbox(self, outbox_id: UUID, *, delay_ms: int = 0) -> str:
        """Регистрирует строку outbox, готовую к отправке."""
        self.outbox.append((outbox_id, delay_ms))
        return str(uuid4())

    async def enqueue_followup(self, task_id: UUID, *, run_at: datetime) -> str:
        """Регистрирует запланированный follow-up."""
        self.followups.append((task_id, run_at))
        return str(uuid4())

    async def startup(self) -> None:
        """Ничего не открывает."""

    async def shutdown(self) -> None:
        """Ничего не закрывает."""


def offline_script() -> list[FakeTurn]:
    """Ответ заглушки для режима без ключа.

    Намеренно не вызывает инструменты и не называет цифр: без живой модели
    осмысленно проверяются только маршрутизация, язык, дедуп и пост-фильтр.
    """
    return [
        FakeTurn.answer(
            "Офлайн-режим: ключ Gemini не найден, отвечает заглушка. "
            "Маршрутизация, определение языка и пост-фильтр при этом настоящие."
        )
    ]


def webhook_payload(
    text: str, *, channel: ChannelKind, chat_id: str, contact_name: str
) -> dict[str, Any]:
    """Собирает payload вебхука Wazzup с одним входящим сообщением."""
    return {
        "messages": [
            {
                "messageId": f"chat-{uuid4()}",
                "channelId": CHANNEL_UUID,
                "chatType": channel.value,
                "chatId": chat_id,
                "dateTime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "type": "text",
                "isEcho": False,
                "status": "inbound",
                "text": text,
                "contact": {"name": contact_name, "avatarUri": None},
            }
        ]
    }


async def build_deps(llm: Any) -> tuple[PipelineDeps, ConsoleQueue]:
    """Поднимает пайплайн на временной sqlite и состоянии в памяти."""
    settings = get_settings()
    engine = storage_db.build_engine()
    storage_db.set_engine(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    snapshot = await kb_loader.load(
        settings.kb_dir,
        media_dir=settings.media_dir,
        schema_version=settings.kb_schema_version,
    )
    kb_loader.swap(snapshot)

    queue = ConsoleQueue()
    deps = PipelineDeps(
        sessionmaker=storage_db.get_sessionmaker(),
        state=build_state_store(settings),
        llm=llm,
        kb=kb_loader.get_snapshot,
        queue=queue,
        settings=settings,
    )
    await queue.startup()
    return deps, queue


class Session:
    """Состояние одного разговора в консоли: собеседник, канал, счётчики."""

    def __init__(self, channel: ChannelKind, chat_id: str, contact: str) -> None:
        self.channel = channel
        self.chat_id = chat_id
        self.contact = contact
        self.turns = 0
        self.cost_usd = 0.0
        self.tokens = 0
        self.tool_calls = 0
        self.blocked = 0


def render(decisions: list[PipelineDecision], session: Session, *, verbose: bool) -> None:
    """Печатает разбор хода: инструменты, вердикт пост-фильтра, ответ, расход."""
    for d in decisions:
        for inv in d.invocations:
            session.tool_calls += 1
            mark = f"{GREEN}ok{OFF}" if inv.result.ok else f"{RED}ERR {inv.result.error}{OFF}"
            args = ", ".join(f"{k}={v!r}" for k, v in (inv.args or {}).items())
            print(f"  {DIM}инструмент{OFF} {BLUE}{inv.name}{OFF}({args}) → {mark}")
            if verbose and inv.result.ok and inv.result.data:
                print(f"    {DIM}{str(inv.result.data)[:400]}{OFF}")

        if d.guard_flags:
            print(f"  {DIM}guards{OFF} {YELLOW}{', '.join(str(f) for f in d.guard_flags)}{OFF}")

        if d.postcheck_fail:
            session.blocked += 1
            print(f"  {RED}пост-фильтр снял ответ: {d.postcheck_fail}{OFF}")

        for u in d.usage:  # по одному замеру на виток обращения к модели
            session.tokens += u.total_tokens or 0
            session.cost_usd += u.cost_usd or 0.0
            cached = f", из кэша {u.cached_tokens}" if u.cached_tokens else ""
            print(
                f"  {DIM}модель {u.model}: {u.prompt_tokens}→{u.candidates_tokens} токенов"
                f"{cached}, {u.latency_ms} мс, ${u.cost_usd:.5f}{OFF}"
            )

        if not d.outbound:
            print(f"  {DIM}(бот молчит: {d.action} / {d.reason}){OFF}")
        for out in d.outbound:
            if out.text:
                print(f"\n{BOLD}Бот:{OFF} {out.text}\n")
            else:
                # Сообщение без текста — это файл: видео, фото или документ.
                print(f"\n{BOLD}Бот:{OFF} {BLUE}[файл: {out.artifact_id}]{OFF}\n")

        for card in d.manager_cards:
            print(f"  {YELLOW}▸ менеджеру:{OFF}\n{DIM}{card.text}{OFF}\n")

        if d.lead_id:
            print(f"  {GREEN}▸ создан лид {d.lead_id}{OFF}")


HELP = f"""{BOLD}Команды{OFF}
  /help              эта справка
  /new               начать новый диалог (новый номер, пустая история)
  /channel whatsapp  переключить канал (whatsapp | instagram)
  /verbose           показывать данные, которые вернули инструменты
  /stats             расход токенов и денег за сессию
  /quit              выход

Всё остальное отправляется боту как сообщение клиента.
Пустая строка — просто пропуск хода.
"""


async def main() -> int:
    """Запускает интерактивный цикл до /quit или Ctrl-D."""
    parser = argparse.ArgumentParser(description="Диалог с ботом в терминале")
    parser.add_argument(
        "--channel", choices=[c.value for c in ChannelKind], default=ChannelKind.WHATSAPP.value
    )
    parser.add_argument("--chat-id", default="77010001234", help="номер клиента")
    parser.add_argument("--name", default="Айгуль", help="имя контакта в мессенджере")
    parser.add_argument("--verbose", action="store_true", help="печатать данные инструментов")
    args = parser.parse_args()

    live = bool(_GEMINI_KEY)
    settings = get_settings()
    if live:
        llm: Any = GeminiClient(api_key=_GEMINI_KEY, settings=settings)
        mode = (
            f"{GREEN}живой Gemini{OFF} "
            f"({settings.gemini_model_primary}, запасная {settings.gemini_model_fallback})"
        )
    else:
        llm = FakeLLMClient(offline_script())
        mode = f"{YELLOW}офлайн, заглушка вместо модели{OFF} — задайте GEMINI_API_KEY для живого прогона"

    deps, queue = await build_deps(llm)
    snapshot = kb_loader.get_snapshot()

    session = Session(ChannelKind(args.channel), args.chat_id, args.name)
    verbose = args.verbose

    print(f"\n{BOLD}AINAZAROV TOP TEAM — консоль диалога{OFF}")
    print(f"  режим     : {mode}")
    print(f"  канал     : {session.channel.value}, номер {session.chat_id}, контакт «{session.contact}»")
    print(f"  база знаний: {snapshot.kb_hash[:12]}")
    print(f"{DIM}  /help — команды, /quit — выход{OFF}\n")

    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, lambda: input(f"{BOLD}Вы:{OFF} "))
        except (EOFError, KeyboardInterrupt):
            print()
            break

        text = line.strip()
        if not text:
            continue

        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd, arg = cmd.lower(), arg.strip().lower()
            if cmd in ("/quit", "/exit", "/q"):
                break
            if cmd == "/help":
                print(HELP)
                continue
            if cmd == "/new":
                session = Session(session.channel, f"7701{uuid4().int % 10_000_000:07d}", session.contact)
                if isinstance(llm, FakeLLMClient):
                    llm.reset(offline_script())
                print(f"{DIM}новый диалог, номер {session.chat_id}{OFF}\n")
                continue
            if cmd == "/channel":
                if arg not in [c.value for c in ChannelKind]:
                    print(f"{RED}канал: whatsapp или instagram{OFF}")
                    continue
                session.channel = ChannelKind(arg)
                print(f"{DIM}канал переключён на {arg}{OFF}\n")
                continue
            if cmd == "/verbose":
                verbose = not verbose
                print(f"{DIM}данные инструментов: {'показываю' if verbose else 'скрыл'}{OFF}\n")
                continue
            if cmd == "/stats":
                print(
                    f"{DIM}ходов {session.turns}, инструментов {session.tool_calls}, "
                    f"снято пост-фильтром {session.blocked}, "
                    f"токенов {session.tokens}, ${session.cost_usd:.4f}{OFF}\n"
                )
                continue
            print(f"{RED}неизвестная команда {cmd}, см. /help{OFF}")
            continue

        if isinstance(llm, FakeLLMClient) and llm.pending == 0:
            llm.reset(offline_script())

        payload = webhook_payload(
            text, channel=session.channel, chat_id=session.chat_id, contact_name=session.contact
        )
        session.turns += 1
        try:
            decisions = await process_inbound(deps, payload)
        except Exception as exc:  # noqa: BLE001 — консоль не должна падать на одной реплике
            print(f"{RED}пайплайн упал: {type(exc).__name__}: {exc}{OFF}\n")
            continue

        langs = {d.lang.value for d in decisions if d.lang}
        if langs:
            print(f"  {DIM}язык: {', '.join(sorted(langs))}{OFF}")
        render(decisions, session, verbose=verbose)

    print(
        f"\n{DIM}Итого: ходов {session.turns}, вызовов инструментов {session.tool_calls}, "
        f"снято пост-фильтром {session.blocked}, токенов {session.tokens}, "
        f"${session.cost_usd:.4f}{OFF}"
    )
    await queue.shutdown()
    close = getattr(llm, "aclose", None)
    if close is not None:
        await close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
