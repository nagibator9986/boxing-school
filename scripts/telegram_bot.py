#!/usr/bin/env python
"""Бот в Telegram на настоящем пайплайне — чтобы тестировать с телефона.

Тот же код, что пойдёт в прод: обновление Bot API приводится к формату вебхука
Wazzup (``chatType: telegram``) и уходит в ``app.core.pipeline.process_inbound``.
Отличается только транспорт — вместо Wazzup ответы уходят напрямую в Bot API.

Запуск::

    .venv/bin/python scripts/telegram_bot.py

Нужны в ``.env``: ``TELEGRAM_BOT_TOKEN`` (от @BotFather) и ``GEMINI_API_KEY``.
Без ключа Gemini бот поднимется, но отвечать будет заглушка.

Состояние живёт в памяти и в файле sqlite рядом с проектом: перезапуск скрипта
не теряет историю диалогов.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _read_dotenv(path: Path) -> dict[str, str]:
    """Читает ``.env`` без сторонних зависимостей."""
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
_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or _DOTENV.get("TELEGRAM_BOT_TOKEN", "")
_GEMINI = os.environ.get("GEMINI_API_KEY") or _DOTENV.get("GEMINI_API_KEY", "")

if not _TOKEN:
    raise SystemExit("Не задан TELEGRAM_BOT_TOKEN — добавьте его в .env (токен от @BotFather)")

_DATA = Path(os.environ.get("DATA_DIR", "").strip() or (ROOT / "data"))
_DATA.mkdir(parents=True, exist_ok=True)

# Значения по умолчанию, а не принудительные: в бою пути задаёт scripts/serve.py,
# и они обязаны совпадать с теми, что видит CRM. Раньше здесь стоял update(),
# и бот на Railway писал бы в свою базу, а CRM показывала пустую чужую.
for key, value in {
    "APP_ENV": "local",
    "DATABASE_URL": f"sqlite+aiosqlite:///{_DATA / 'telegram.db'}",
    # Состояние в файле: пауза бота и дедуп обязаны пережить перезапуск.
    "STATE_BACKEND": "sqlite",
    "REDIS_URL": "",
    "INLINE_WORKER": "false",
    "WAZZUP_API_KEY": "telegram-local",
    "WAZZUP_WEBHOOK_SECRET": "telegram-secret-telegram-secret",
    "LOG_LEVEL": os.environ.get("TG_LOG_LEVEL", "WARNING"),
    "DEBOUNCE_SECONDS": "0",
    "SECOND_MESSAGE_DELAY_MS": "0",
}.items():
    os.environ.setdefault(key, value)

# Ключи берём из .env, если в окружении их нет.
os.environ["TELEGRAM_BOT_TOKEN"] = _TOKEN
if _GEMINI:
    os.environ["GEMINI_API_KEY"] = _GEMINI

from app.admin.admin_store import AdminStore  # noqa: E402
from app.admin.runtime_settings import load_runtime_settings  # noqa: E402
from app.admin.telegram_admin import AdminConsole  # noqa: E402
from app.channels.telegram import TelegramClient, TelegramError, update_to_webhook  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core.kb_watch import KBWatcher  # noqa: E402
from app.core.pipeline import PipelineDeps, process_inbound  # noqa: E402
from app.kb import loader as kb_loader  # noqa: E402
from app.llm.client import FakeLLMClient, FakeTurn, GeminiClient  # noqa: E402
from app.storage import db as storage_db  # noqa: E402
from app.storage.models import Base  # noqa: E402
from app.storage.state import build_state_store  # noqa: E402
from app.types import PipelineDecision  # noqa: E402


class LocalQueue:
    """Очередь-заглушка: отправкой занимается сам цикл, воркер здесь не нужен."""

    def __init__(self) -> None:
        self.followups: list[tuple[UUID, datetime]] = []

    async def enqueue_inbound(self, payload: dict[str, Any]) -> str:
        """Входящие обрабатываются синхронно в цикле опроса."""
        return str(uuid4())

    async def enqueue_outbox(self, outbox_id: UUID, *, delay_ms: int = 0) -> str:
        """Строки outbox отправляются сразу после хода, из решения пайплайна."""
        return str(uuid4())

    async def enqueue_followup(self, task_id: UUID, *, run_at: datetime) -> str:
        """Follow-up в тестовом прогоне только регистрируется."""
        self.followups.append((task_id, run_at))
        return str(uuid4())

    async def startup(self) -> None:
        """Ничего не открывает."""

    async def shutdown(self) -> None:
        """Ничего не закрывает."""


async def build_deps(llm: Any) -> PipelineDeps:
    """Поднимает пайплайн на файловой sqlite: история переживает перезапуск."""
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

    return PipelineDeps(
        sessionmaker=storage_db.get_sessionmaker(),
        state=build_state_store(settings),
        llm=llm,
        kb=kb_loader.get_snapshot,
        queue=LocalQueue(),
        settings=settings,
        # Настройки владельца из CRM и из /admin читаются на каждом ходу: правка
        # часов работы обязана действовать сразу, а не после перезапуска.
        runtime=lambda: load_runtime_settings(settings.admin_db_path),
    )


async def notify_admins(
    tg: TelegramClient, admin_ids: "tuple[int, ...] | list[int]", decisions: list[PipelineDecision]
) -> int:
    """Отправляет карточки лидов и эскалаций администраторам в Telegram.

    Без этого бот доводит родителя до записи, обещает «администратор свяжется» —
    и карточку не получает никто: боевой адресат (MANAGER_NOTIFY_TARGET) в
    тестовом режиме не задан, и лид молча пропадал. Здесь адресат — те же
    TELEGRAM_ADMIN_IDS, что уже нужны для /admin.
    """
    cards = [card for d in decisions for card in d.manager_cards]
    if not cards:
        return 0
    if not load_runtime_settings(get_settings().admin_db_path).lead_notify:
        # Владелец выключил уведомления. Лид при этом никуда не пропадает:
        # он лежит в базе и виден в CRM — молчит только Telegram.
        print("  (уведомления о лидах выключены в настройках — карточка не отправлена)")
        return 0
    if not admin_ids:
        # Печатаем в консоль, чтобы лид был виден хотя бы оператору запуска.
        for card in cards:
            print(f"  [!] ЛИД НЕКОМУ ОТПРАВИТЬ (пришлите боту пароль, чтобы стать админом):\n{card.text}")
        return 0

    sent = 0
    for card in cards:
        header = "НОВЫЙ ЛИД" if card.kind.value == "lead" else "НУЖЕН ЖИВОЙ ОТВЕТ"
        for admin_id in admin_ids:
            try:
                await tg.send_message(admin_id, f"{header}\n\n{card.text}")
                sent += 1
            except TelegramError as exc:
                # Администратор мог не начать диалог с ботом — это частая причина.
                print(f"  [!] карточка не дошла до {admin_id}: {exc}")
    return sent


def plan_delivery(
    messages: "list[Any]", resolve: "Callable[[str | None], Path | None]"
) -> "list[tuple[Path | None, str | None]]":
    """Что именно отправить в Telegram: список пар «файл, подпись».

    Пайплайн отдаёт файл и подпись к нему ДВУМЯ сообщениями — так требует
    Wazzup, где текст и вложение в одном сообщении запрещены. В Telegram это
    ограничение не действует, и отправлять их порознь нельзя: получалось два
    видео подряд — первое молча, второе с подписью.

    Поэтому здесь подпись приклеивается к своему файлу, а второе сообщение
    выбрасывается. Пара опознаётся по ``artifact_id``, порядок значения не имеет.
    """
    captions: dict[str, str] = {}
    for message in messages:
        artifact_id = getattr(message, "artifact_id", None)
        text = getattr(message, "text", None)
        if artifact_id and text and not getattr(message, "content_uri", None):
            if resolve(artifact_id) is not None:
                captions.setdefault(artifact_id, text)

    plan: list[tuple[Path | None, str | None]] = []
    files_done: set[str] = set()
    for message in messages:
        artifact_id = getattr(message, "artifact_id", None)
        text = getattr(message, "text", None)
        path = resolve(artifact_id) if artifact_id else None

        if path is not None and artifact_id not in files_done:
            plan.append((path, text or captions.get(artifact_id or "")))
            files_done.add(artifact_id or "")
            continue
        if artifact_id in files_done and text and text == captions.get(artifact_id or ""):
            # Подпись уже ушла вместе с файлом.
            continue
        if text:
            plan.append((None, text))
    return plan


async def mark_delivered(delivered: set[UUID]) -> None:
    """Закрывает строки очереди, доставленные этим циклом опроса.

    Отправкой в Telegram занимается сам цикл, а не воркер, и без этой отметки
    строка навсегда осталась бы «в очереди»: счётчик неотправленного рос бы с
    каждым ответом бота, и по нему нельзя было бы понять, есть ли настоящая
    проблема с доставкой.
    """
    if not delivered:
        return
    from app.storage import repo_outbox

    sessionmaker = storage_db.get_sessionmaker()
    async with sessionmaker() as session:
        for crm_message_id in delivered:
            await repo_outbox.mark_sent_by_crm_id(session, crm_message_id)
        await session.commit()


async def deliver(tg: TelegramClient, chat_id: str, decisions: list[PipelineDecision]) -> int:
    """Отправляет клиенту всё, что решил отправить пайплайн. Возвращает число сообщений.

    Артефакты с файлом (видео, фото, документ) уходят вложением: Telegram —
    единственный наш канал, где это возможно для видео.
    """
    settings = get_settings()
    snapshot = kb_loader.get_snapshot()
    sent = 0
    delivered: set[UUID] = set()
    for decision in decisions:
        resolve = lambda artifact_id: _attachment_path(  # noqa: E731
            snapshot, artifact_id, settings.media_dir
        )
        messages = list(decision.outbound)
        # Счётчик именно этого решения: общий на все решения пометил бы
        # доставленными и те сообщения, которые отправить не удалось.
        sent_here = 0
        for path, text in plan_delivery(messages, resolve):
            if path is not None:
                try:
                    await tg.send_file(chat_id, path=path, kind=_file_kind(path), caption=text)
                    sent_here += 1
                    continue
                except TelegramError as exc:
                    # Файл не ушёл — клиент всё равно обязан получить текст.
                    print(f"  [!] вложение не отправлено: {exc}")
            if text:
                await tg.send_message(chat_id, text)
                sent_here += 1
        sent += sent_here
        if sent_here:
            delivered.update(
                message.crm_message_id
                for message in messages
                if getattr(message, "crm_message_id", None) is not None
            )
    await mark_delivered(delivered)
    return sent


def _attachment_path(snapshot: Any, artifact_id: str | None, media_dir: Path) -> Path | None:
    """Путь к файлу артефакта, если он есть на диске.

    Файл берётся из базы знаний по ``artifact_id``: у ``OutboundMessage`` своего
    пути нет, он несёт только идентификатор артефакта. ``file_path`` в
    ``media.yaml`` задаётся относительно каталога медиа.
    """
    if not artifact_id:
        return None
    artifact = snapshot.artifact(artifact_id)
    if artifact is None or not artifact.enabled or not artifact.file_path:
        return None
    path = Path(artifact.file_path)
    if not path.is_absolute():
        base = media_dir if media_dir.is_absolute() else ROOT / media_dir
        path = base / path
    return path if path.is_file() else None


def _incoming_file(message: dict[str, Any]) -> tuple[str | None, str]:
    """``(file_id, kind)`` присланного файла. Видео и фото — разные типы.

    Из фото Telegram присылает несколько размеров; берём последний — он самый
    крупный, а мелкие превью в базе знаний бесполезны.
    """
    if isinstance(message.get("video"), dict):
        return message["video"].get("file_id"), "video"
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        return photos[-1].get("file_id"), "image"
    document = message.get("document")
    if isinstance(document, dict):
        mime = str(document.get("mime_type") or "")
        if mime.startswith("video/"):
            return document.get("file_id"), "video"
        if mime.startswith("image/"):
            return document.get("file_id"), "image"
    return None, ""


def _file_kind(path: Path) -> str:
    """Метод Bot API по расширению файла."""
    suffix = path.suffix.lower()
    if suffix in {".mp4", ".mov", ".m4v"}:
        return "video"
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return "photo"
    return "document"


def describe(decisions: list[PipelineDecision]) -> str:
    """Однострочный разбор хода для консоли оператора."""
    parts: list[str] = []
    for d in decisions:
        tools = ", ".join(inv.name for inv in d.invocations)
        parts.append(f"{d.action.value}/{d.reason}" + (f" [{tools}]" if tools else ""))
        if d.postcheck_fail:
            parts.append(f"ПОСТФИЛЬТР: {d.postcheck_fail}")
        if d.lead_id:
            parts.append(f"лид {str(d.lead_id)[:8]}")
    return " | ".join(parts)


async def main() -> int:
    """Цикл long polling до Ctrl-C."""
    settings = get_settings()
    live = bool(_GEMINI)
    llm: Any = (
        GeminiClient(api_key=_GEMINI, settings=settings)
        if live
        else FakeLLMClient([FakeTurn.answer("Офлайн-режим: ключ Gemini не задан.")])
    )

    tg = TelegramClient(token=_TOKEN, timeout_s=settings.telegram_poll_timeout_s)
    deps = await build_deps(llm)

    store = AdminStore(settings.admin_db_path)
    # Стартовый список из .env переносим в базу один раз: дальше администраторы
    # добавляются паролем прямо в чате, и права переживают перезапуск.
    for seed_id in settings.admin_ids:
        store.grant(seed_id, title="из настроек")

    admin = AdminConsole(
        kb_dir=settings.kb_dir,
        media_dir=settings.media_dir,
        schema_version=settings.kb_schema_version,
        snapshot=kb_loader.get_snapshot,
        store=store,
    )

    # Наблюдатель за базой знаний: правки из CRM и из /admin приходят в другом
    # процессе, а снимок живёт в памяти этого. Без него владелец менял бы цену
    # в CRM, видел «сохранено» — и бот называл бы старую до перезапуска.
    kb_watch = KBWatcher(
        settings.kb_dir,
        media_dir=settings.media_dir,
        schema_version=settings.kb_schema_version,
        min_interval_s=1.0,
    )

    me = await tg.get_me()
    await tg.delete_webhook()  # getUpdates и вебхук взаимоисключающи

    print(f"\nБот @{me.get('username')} запущен")
    print(f"  модель      : {'живой ' + settings.gemini_model_primary if live else 'заглушка'}")
    print(f"  база знаний : {kb_loader.get_snapshot().kb_hash[:12]}")
    print(f"  напишите ему в Telegram: https://t.me/{me.get('username')}")
    known = [a.telegram_id for a in store.admins()]
    print(f"  админы     : {known or 'нет — пришлите боту пароль, чтобы стать первым'}")
    print(f"  хранилище  : SQLite ({settings.admin_db_path.parent})")
    print("  правки базы знаний из CRM подхватываются на лету, перезапуск не нужен")
    print("  Ctrl-C — остановить\n")

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    offset: int | None = None
    while not stopping.is_set():
        reloaded = await kb_watch.maybe_reload()
        if reloaded is not None and reloaded.changed:
            print(f"  база знаний обновлена: {reloaded.old_hash[:8]} → {reloaded.new_hash[:8]}")
        try:
            updates = await tg.get_updates(
                offset=offset, timeout_s=settings.telegram_poll_timeout_s
            )
        except TelegramError as exc:
            print(f"[!] getUpdates: {exc}")
            await asyncio.sleep(3)
            continue

        for update in updates:
            offset = int(update.get("update_id", 0)) + 1
            # Перед каждым ходом: клиент обязан получить ответ по свежим данным,
            # даже если правку сохранили секунду назад.
            fresh = await kb_watch.maybe_reload()
            if fresh is not None and fresh.changed:
                print(f"  база знаний обновлена: {fresh.old_hash[:8]} → {fresh.new_hash[:8]}")
            payload = update_to_webhook(update)
            if payload is None:
                continue

            message = payload["messages"][0]
            chat_id = message["chatId"]
            text = message.get("text") or "(без текста)"
            print(f"← [{chat_id}] {text[:70]}")

            # Админка перехватывает диалог целиком: пока идёт правка расписания,
            # сообщения администратора не должны уходить в клиентский пайплайн.
            raw_message = update.get("message") or {}
            raw_from = raw_message.get("from") or {}
            user_id = raw_from.get("id")

            # Вход в админку по паролю. Проверяется раньше всего: пароль не должен
            # уйти в пайплайн и попасть в историю диалога с моделью.
            if AdminStore.password_matches(text, settings.admin_password):
                name = " ".join(
                    p for p in (raw_from.get("first_name"), raw_from.get("last_name")) if p
                ).strip()
                is_new = store.grant(int(user_id), title=name or raw_from.get("username") or "")
                print(f"  админка: вход по паролю, id={user_id}, новый={is_new}")
                await tg.send_message(
                    chat_id,
                    ("Готово, теперь вы администратор." if is_new
                     else "У вас уже есть права администратора.")
                    + "\n\nОткрыть управление: /admin",
                )
                continue

            # Файл от администратора — это добавление материала в базу знаний,
            # а не сообщение клиента.
            file_id, file_kind = _incoming_file(raw_message)
            if file_id and store.is_admin(user_id):
                caption = raw_message.get("caption") or ""
                print(f"  админка: файл {file_kind}, подпись: {caption[:60]!r}")
                try:
                    tmp = Path(tempfile.gettempdir()) / f"tg-{uuid4().hex}"
                    await tg.download_file(file_id, tmp)
                    reply = admin.add_media(
                        chat_id, source=tmp, kind=file_kind, caption=caption
                    )
                    tmp.unlink(missing_ok=True)
                except TelegramError as exc:
                    reply = f"Файл не скачался: {exc}"
                await tg.send_message(chat_id, reply)
                continue
            if text.strip().lower().startswith(("/admin", "/admin_off")) or admin.active(chat_id):
                if not store.is_admin(user_id):
                    await tg.send_message(chat_id, "Эта команда доступна только администратору.")
                    continue
                reply = admin.handle(chat_id, text)
                print(f"  админка: {reply.splitlines()[0][:70]}")
                await tg.send_message(chat_id, reply)
                continue

            await tg.send_chat_action(chat_id)
            try:
                decisions = await process_inbound(deps, payload)
            except Exception as exc:  # noqa: BLE001 — один диалог не роняет бота
                print(f"  [!] пайплайн упал: {type(exc).__name__}: {exc}")
                await tg.send_message(
                    chat_id, "Секунду, у меня сбой. Передаю администратору, он ответит здесь же."
                )
                continue

            print(f"  {describe(decisions)}")
            count = await deliver(tg, chat_id, decisions)
            admin_ids = [a.telegram_id for a in store.admins()]
            cards_sent = await notify_admins(tg, admin_ids, decisions)
            if cards_sent:
                print(f"  → администратору отправлено карточек: {cards_sent}")
            if not count:
                print("  (бот промолчал)")

    print("\nОстановлен.")
    await tg.aclose()
    close = getattr(llm, "aclose", None)
    if close is not None:
        await close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
