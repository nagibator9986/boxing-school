#!/usr/bin/env python
"""Боевой запуск: CRM и бот в одном процессе-надзирателе.

Почему вместе, а не двумя службами Railway. Бот и CRM обязаны видеть **одни и те
же файлы**: базу знаний ``kb/*.yaml`` и базы SQLite в ``/data``. Диск на Railway
принадлежит одной службе и между службами не разделяется — разнеся их, мы бы
получили две независимые копии базы знаний: владелец правит цену в CRM, а бот
отвечает по своей старой копии и никогда о правке не узнает.

Второе решение здесь — **база знаний живёт на диске, а не в образе**. Иначе
любой передеплой возвращал бы файлы из репозитория и стирал всё, что владелец
наменял через CRM за неделю. При первом запуске файлы копируются из образа на
диск; дальше образ их не трогает и дописывает только те, которых на диске нет.

Запуск::

    python scripts/serve.py

Переменные:

* ``PORT`` — порт CRM (Railway передаёт сам);
* ``DATA_DIR`` — каталог диска, по умолчанию ``/data``, если он доступен на запись;
* ``RUN_WEB`` (прежнее имя ``RUN_CRM``) / ``RUN_BOT`` — ``false`` отключает процесс;
* ``TELEGRAM_BOT_TOKEN`` — без него бот не запускается, а CRM работает.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Каталоги, которые обязаны пережить передеплой: их правит владелец школы.
SEEDED: tuple[str, ...] = ("kb", "media")

#: Сколько ждать завершения дочернего процесса после SIGTERM.
STOP_TIMEOUT_S: float = 15.0


def _log(message: str) -> None:
    """Однострочный вывод в журнал Railway."""
    print(f"[serve] {message}", flush=True)


def _flag(name: str, default: bool = True) -> bool:
    """Булева переменная окружения."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "да")


def _forget_settings() -> None:
    """Сбрасывает кеш конфигурации после работы во временном окружении.

    Проверки схемы и состояния Wazzup подменяют переменные окружения, чтобы
    посмотреть на конфигурацию дочерних процессов. Оставить после них кеш,
    собранный на подменённом окружении, значит незаметно раздать его всему
    остальному коду надзирателя.
    """
    try:
        from app.config import reset_settings_cache

        reset_settings_cache()
    except Exception:  # noqa: BLE001 - сброс кеша не повод падать
        pass


def _report_wazzup(env: dict[str, str]) -> None:
    """Пишет в журнал, поднимется ли приём Wazzup и почему нет.

    Без этой строки «канал не работает» выясняется по молчанию в чате: вебхук
    зарегистрирован, сообщения уходят в никуда, а в журнале ничего.
    """
    saved = dict(os.environ)
    try:
        os.environ.update(env)
        from app.config import get_settings, reset_settings_cache

        reset_settings_cache()
        from app.asgi import wazzup_ready

        ready, reasons = wazzup_ready(get_settings())
        if ready:
            _log("Wazzup: приём включён")
        else:
            for reason in reasons:
                _log(f"Wazzup: приём выключен — {reason}")
            _log("Telegram и CRM работают как обычно.")
    except Exception as exc:  # noqa: BLE001 - диагностика не имеет права ронять запуск
        _log(f"Wazzup: состояние определить не удалось ({type(exc).__name__})")
    finally:
        os.environ.clear()
        os.environ.update(saved)
        _forget_settings()


def resolve_data_dir() -> Path:
    """Каталог для данных: диск Railway либо ``./data`` при локальном запуске.

    Проверяется именно запись, а не существование: том, смонтированный только на
    чтение, выглядит как обычный каталог, и ошибка вскрылась бы посреди диалога
    при попытке записать историю.
    """
    def writable(path: Path) -> bool:
        """Проверяется именно запись: том, смонтированный только на чтение,
        выглядит как обычный каталог, и ошибка вскрылась бы посреди диалога."""
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError:
            return False
        return True

    explicit = os.environ.get("DATA_DIR", "").strip()
    if explicit:
        # Заданный каталог подменять нельзя. Молча уйдя в ./data внутри образа,
        # служба работала бы как ни в чём не бывало — и теряла бы всё: историю
        # диалогов, заявки, права администраторов — при каждом передеплое.
        path = Path(explicit)
        if not writable(path):
            raise SystemExit(
                f"каталог данных {path} недоступен на запись. На Railway это значит, "
                "что том не подключён к службе либо смонтирован не по этому пути."
            )
        return path

    for candidate in ("/data", str(ROOT / "data")):
        path = Path(candidate)
        if writable(path):
            return path
    raise SystemExit("нет каталога для данных: ни /data, ни ./data не доступны на запись")


def warn_if_not_a_volume(data_dir: Path) -> bool:
    """Предупреждает, если каталог данных — не подключённый том. ``True`` — том.

    В образе каталог ``/data`` создаётся заранее, поэтому без подключённого тома
    он существует и доступен на запись: служба поднимется как ни в чём не бывало
    и будет терять историю диалогов, заявки и права администраторов при каждом
    передеплое. Молчать об этом нельзя — поломка обнаружится через месяц, когда
    восстанавливать будет нечего.
    """
    try:
        mounted = os.path.ismount(data_dir)
    except OSError:
        mounted = False
    if mounted:
        return True
    if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        _log("=" * 72)
        _log(f"ВНИМАНИЕ: {data_dir} — не подключённый том, а каталог внутри контейнера.")
        _log("Всё, что накопит бот, пропадёт при следующем передеплое: история")
        _log("диалогов, заявки, права администраторов и правки базы знаний.")
        _log("Railway → Settings → Volumes → Add Volume, точка подключения /data")
        _log("=" * 72)
    return False


def seed_from_image(data_dir: Path) -> dict[str, int]:
    """Копирует базу знаний и медиа из образа на диск. Существующее не трогает.

    Правило одно: **файл с диска всегда важнее файла из образа**. На диске лежит
    то, что владелец правил через CRM; в образе — то, что было в репозитории на
    момент сборки. Перезапись означала бы молчаливый откат его работы.
    """
    copied: dict[str, int] = {}
    for name in SEEDED:
        source = ROOT / name
        target = data_dir / name
        target.mkdir(parents=True, exist_ok=True)
        count = 0
        if not source.is_dir():
            copied[name] = 0
            continue
        for item in sorted(source.iterdir()):
            if not item.is_file() or item.name.startswith("."):
                continue
            destination = target / item.name
            if destination.exists():
                continue
            shutil.copy2(item, destination)
            count += 1
        copied[name] = count
    return copied


def prepare_env(data_dir: Path) -> dict[str, str]:
    """Единое окружение для обоих процессов: одни и те же файлы у бота и CRM."""
    env = dict(os.environ)
    env.setdefault("APP_ENV", "prod")
    env.setdefault("PYTHONUNBUFFERED", "1")

    # Пути задаём принудительно: разъехавшись, бот и CRM начнут работать с
    # разными копиями данных, и это никак не проявится до первой правки.
    env["KB_DIR"] = str(data_dir / "kb")
    env["MEDIA_DIR"] = str(data_dir / "media")
    env["DATABASE_URL"] = env.get("DATABASE_URL") or f"sqlite+aiosqlite:///{data_dir / 'bot.db'}"
    env["STATE_BACKEND"] = env.get("STATE_BACKEND") or "sqlite"
    env["STATE_SQLITE_PATH"] = str(data_dir / "state.db")
    env["ADMIN_DB_PATH"] = str(data_dir / "admin.db")
    env["CRM_BOT_DB"] = str(data_dir / "bot.db")
    env["PYTHONPATH"] = str(ROOT)
    return env


def ensure_schema(env: dict[str, str]) -> bool:
    """Создаёт недостающие таблицы в базе диалогов до запуска процессов.

    Раньше схему создавал только Telegram-раннер, и это работало, пока он был
    единственным. С приёмом Wazzup появился второй путь: вебхук мог прийти
    раньше, чем бот поднимется, — а на чистом диске он приходил в базу без
    единой таблицы. Клиент при этом получал молчание, а в журнале лежало
    «no such table: conversation».

    Операция идемпотентна: существующие таблицы не трогаются.
    """
    import asyncio

    saved = dict(os.environ)
    try:
        os.environ.update(env)
        from app.config import reset_settings_cache

        reset_settings_cache()
        from app.storage import db as storage_db
        from app.storage.models import Base

        async def _create() -> None:
            engine = storage_db.build_engine()
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
            finally:
                await engine.dispose()

        asyncio.run(_create())
        _log("схема базы диалогов на месте")
        return True
    except Exception as exc:  # noqa: BLE001 - причину показываем целиком
        _log(f"ВНИМАНИЕ: не удалось подготовить базу диалогов: {type(exc).__name__}: {exc}")
        return False
    finally:
        os.environ.clear()
        os.environ.update(saved)
        _forget_settings()


def check_kb(env: dict[str, str]) -> bool:
    """Проверяет базу знаний на диске. ``False`` — она невалидна.

    Останавливать запуск нельзя: CRM нужна как раз для того, чтобы исправить
    сломанную базу. Но сказать об этом громко — обязательно.
    """
    try:
        from app.kb import loader as kb_loader

        snapshot, warnings = kb_loader.load_sync(
            Path(env["KB_DIR"]),
            media_dir=Path(env["MEDIA_DIR"]),
            schema_version=int(env.get("KB_SCHEMA_VERSION", "1")),
        )
    except Exception as exc:  # noqa: BLE001 - причину показываем целиком
        _log(f"ВНИМАНИЕ: база знаний на диске не читается: {exc}")
        _log("CRM поднимется, бот отвечать не сможет. Исправьте файлы в разделе «Файлы базы».")
        return False
    for warning in warnings:
        _log(f"предупреждение базы знаний: {warning}")
    _log(f"база знаний в порядке: версия {snapshot.kb_hash[:12]}, залов {len(snapshot.gyms.gyms)}")
    return True


def start_web(env: dict[str, str]) -> subprocess.Popen[bytes]:
    """Веб-служба: приём вебхуков Wazzup и CRM в одном процессе на одном порту.

    Один рабочий процесс — намеренно. Несколько означали бы несколько снимков
    базы знаний в памяти, каждый со своим временем жизни, и несколько писателей
    в один файл SQLite.
    """
    port = env.get("PORT", "8000")
    command = [
        sys.executable, "-m", "uvicorn",
        "app.asgi:build_app",
        "--factory",
        "--host", "0.0.0.0",
        "--port", str(port),
        "--workers", "1",
        # За прокси Railway: без этого приложение видит адрес прокси вместо
        # клиента, а схему — http вместо https.
        "--proxy-headers",
        "--forwarded-allow-ips", "*",
        "--no-access-log",
    ]
    _log(f"веб-служба: http://0.0.0.0:{port} — CRM на /crm, вебхук Wazzup на /wazzup/webhook/…")
    return subprocess.Popen(command, env=env, cwd=str(ROOT))


def start_bot(env: dict[str, str]) -> subprocess.Popen[bytes] | None:
    """Бот в Telegram. Без токена не запускается — это не ошибка."""
    if not env.get("TELEGRAM_BOT_TOKEN", "").strip():
        _log("бот не запущен: не задан TELEGRAM_BOT_TOKEN")
        return None
    _log("бот Telegram: опрос запущен")
    return subprocess.Popen(
        [sys.executable, "-u", "scripts/telegram_bot.py"], env=env, cwd=str(ROOT)
    )


def supervise(children: dict[str, subprocess.Popen[bytes]]) -> int:
    """Ждёт процессы. Падение любого из них останавливает службу целиком.

    Перезапуском занимается Railway. Чинить упавший процесс самостоятельно —
    значит прятать поломку: служба выглядит живой, а половина её не работает.
    """
    stopping = {"value": False}

    def _stop(signum: int, _frame: object) -> None:
        stopping["value"] = True
        _log(f"получен сигнал {signum}, останавливаю процессы")
        for name, process in children.items():
            if process.poll() is None:
                process.terminate()
                _log(f"  {name}: отправлен SIGTERM")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while True:
        for name, process in children.items():
            code = process.poll()
            if code is None:
                continue
            if stopping["value"]:
                continue
            _log(f"процесс {name} завершился с кодом {code} — останавливаю остальные")
            for other_name, other in children.items():
                if other_name != name and other.poll() is None:
                    other.terminate()
            _wait_all(children)
            return code or 1
        if stopping["value"] and all(p.poll() is not None for p in children.values()):
            return 0
        time.sleep(0.5)


def _wait_all(children: dict[str, subprocess.Popen[bytes]]) -> None:
    """Дожидается остановки всех процессов, дорезая зависшие."""
    deadline = time.monotonic() + STOP_TIMEOUT_S
    for name, process in children.items():
        remaining = max(0.1, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _log(f"  {name}: не остановился за {STOP_TIMEOUT_S:.0f} с, убиваю")
            process.kill()


def main() -> int:
    """Готовит диск и поднимает процессы."""
    data_dir = resolve_data_dir()
    _log(f"каталог данных: {data_dir}")
    warn_if_not_a_volume(data_dir)

    copied = seed_from_image(data_dir)
    for name, count in copied.items():
        _log(f"{name}: скопировано из образа файлов — {count} (существующие не тронуты)")

    env = prepare_env(data_dir)
    ensure_schema(env)
    check_kb(env)

    children: dict[str, subprocess.Popen[bytes]] = {}
    if _flag("RUN_WEB", _flag("RUN_CRM")):
        _report_wazzup(env)
        children["web"] = start_web(env)
    if _flag("RUN_BOT"):
        bot = start_bot(env)
        if bot is not None:
            children["bot"] = bot

    if not children:
        _log("нечего запускать: RUN_CRM и RUN_BOT выключены")
        return 1

    return supervise(children)


if __name__ == "__main__":
    raise SystemExit(main())
