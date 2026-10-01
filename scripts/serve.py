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
диск. Дальше новая версия файла из репозитория приезжает, только если владелец
этот файл не правил: см. :func:`seed_from_image`.

Запуск::

    python scripts/serve.py

Переменные:

* ``PORT`` — порт CRM (Railway передаёт сам);
* ``DATA_DIR`` — каталог диска, по умолчанию ``/data``, если он доступен на запись;
* ``RUN_WEB`` (прежнее имя ``RUN_CRM``) / ``RUN_BOT`` — ``false`` отключает процесс;
* ``TELEGRAM_BOT_TOKEN`` — без него бот не запускается, а CRM работает.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Каталоги, которые обязаны пережить передеплой: их правит владелец школы.
SEEDED: tuple[str, ...] = ("kb", "media")

#: Файлы, где у каждой записи свой ключ: недостающие ключи из репозитория дописываются
#: и в файл, который правил владелец. Код требует свои тексты на загрузке базы — без
#: этого правка «Текстов» в CRM и следующий выпуск с новым текстом клали бы бота.
MERGED_KEYS: dict[str, str] = {"i18n.yaml": "strings"}

#: Какую версию каждого файла положил на диск сам запуск — отпечаток SHA-256.
#: Лежит в корне каталога данных, а не в ``kb/``: там его подхватил бы отпечаток
#: базы знаний.
SEED_STATE: str = ".seed-state.json"

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


def seed_from_image(data_dir: Path, *, image_root: Path = ROOT) -> dict[str, int]:
    """Переносит базу знаний и медиа из образа на диск, не трогая правок владельца.

    Возвращает, сколько файлов в каждом каталоге появилось или обновилось.

    Правило: **файл, который владелец правил, важнее файла из образа**. На диске
    лежит то, что он менял через CRM; перезапись молча отменила бы его работу. Но и
    правки из репозитория — новые районы, тексты владельца — обязаны доезжать до
    сервера: раньше на подключённом томе они не появлялись никогда.

    Поэтому для каждого файла запоминается отпечаток версии, которую положил сам
    запуск (:data:`SEED_STATE`):

    * отпечаток совпадает с диском — файл никто не правил, и новая версия из
      репозитория его заменяет (база знаний перед этим уходит в резервную копию,
      которую видно и можно вернуть в CRM);
    * не совпадает — правка владельца остаётся, а в журнал уходит, что в
      репозитории есть новее;
    * истории нет (том подключён до этого правила) — файл, равный образу, берётся
      под учёт; отличающийся не трогается: чья это правка, уже не узнать.

    Новая версия, с которой база перестала проходить проверку, откатывается: файл
    из репозитория мог не сойтись с правками владельца в соседнем файле, а бот с
    непрочитанной базой не отвечает никому.
    """
    state = _read_seed_state(data_dir)
    copies: list[tuple[str, Path, Path, str]] = []
    updates: list[tuple[str, Path, Path, str]] = []
    merges: list[tuple[Path, Path]] = []
    for name in SEEDED:
        source_dir = image_root / name
        target_dir = data_dir / name
        target_dir.mkdir(parents=True, exist_ok=True)
        known = state.setdefault(name, {})
        if not source_dir.is_dir():
            continue
        for item in sorted(source_dir.iterdir()):
            if not item.is_file() or item.name.startswith("."):
                continue
            destination = target_dir / item.name
            image_hash = _sha256(item)
            if not destination.exists():
                copies.append((name, item, destination, image_hash))
                continue
            disk_hash = _sha256(destination)
            seeded = known.get(item.name)
            if disk_hash == image_hash:
                known[item.name] = image_hash
            elif seeded is None:
                _log(
                    f"{name}/{item.name}: отличается от версии в репозитории, а правил ли его "
                    "владелец — неизвестно. Оставлен как есть."
                )
                if name == "kb" and item.name in MERGED_KEYS:
                    merges.append((item, destination))
            elif disk_hash != seeded:
                _log(
                    f"{name}/{item.name}: в репозитории новая версия, но файл правили в CRM — "
                    "оставлена правка владельца."
                )
                if name == "kb" and item.name in MERGED_KEYS:
                    merges.append((item, destination))
            else:
                updates.append((name, item, destination, image_hash))

    kb_dir, media_dir = data_dir / "kb", data_dir / "media"
    kb_updates = [row for row in updates if row[0] == "kb"]
    touches_kb = bool(kb_updates or merges)
    valid_before = touches_kb and _kb_loads(kb_dir, media_dir)
    backup = _backup_kb(kb_dir) if touches_kb else None

    report = {name: 0 for name in SEEDED}
    for name, item, destination, image_hash in copies + updates:
        _replace(item, destination)
        state[name][item.name] = image_hash
        report[name] += 1
    for name, item, _, _ in updates:
        _log(f"{name}/{item.name}: обновлён из репозитория")
    # Отпечаток после дописывания не запоминается: файл остаётся «правленым
    # владельцем», и следующий выпуск снова только допишет новое, а не заменит его.
    merged = [destination for item, destination in merges if _merge_missing_keys(item, destination)]

    if valid_before and not _kb_loads(kb_dir, media_dir) and backup is not None:
        for destination in merged:
            shutil.copy2(backup / destination.name, destination)
        for name, item, destination, _ in kb_updates:
            shutil.copy2(backup / item.name, destination)
            state[name][item.name] = _sha256(destination)
            report[name] -= 1
        _log("=" * 72)
        _log("ВНИМАНИЕ: новая версия базы знаний из репозитория не сошлась с правками")
        _log("владельца в других файлах — возвращена прежняя: "
             + ", ".join(item.name for _, item, _, _ in kb_updates))
        _log("=" * 72)

    _write_seed_state(data_dir, state)
    return report


def _merge_missing_keys(source: Path, destination: Path) -> list[str]:
    """Дописывает в файл владельца ключи, которых в нём нет, из версии в репозитории.

    Тексты владельца не меняются ни на букву: добавляется только то, чего у него нет.
    Разбор с сохранением комментариев и кавычек — тот же, что у CRM.
    """
    section = MERGED_KEYS.get(source.name)
    if not section:
        return []
    try:
        from ruamel.yaml import YAML

        engine = YAML()
        engine.preserve_quotes = True
        engine.width = 4096
        engine.indent(mapping=2, sequence=2, offset=0)
        image = engine.load(source.read_text(encoding="utf-8")) or {}
        disk = engine.load(destination.read_text(encoding="utf-8")) or {}
        theirs, ours = image.get(section) or {}, disk.get(section)
        if ours is None:
            return []
        added = [key for key in theirs if key not in ours]
        if not added:
            return []
        for key in added:
            ours[key] = theirs[key]
        temporary = destination.with_name(f".{destination.name}.seed-tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            engine.dump(disk, handle)
        os.replace(temporary, destination)
    except Exception as exc:  # noqa: BLE001 - не дописали — громко, но запуск продолжается
        _log(f"ВНИМАНИЕ: kb/{destination.name}: не удалось дописать новые тексты: {exc}")
        return []
    _log(f"kb/{destination.name}: правки владельца оставлены, дописаны новые тексты: {', '.join(added)}")
    return added


def _sha256(path: Path) -> str:
    """Отпечаток содержимого файла. Видео читаются кусками, а не целиком."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_seed_state(data_dir: Path) -> dict[str, dict[str, str]]:
    """Отпечатки разложенных версий. Испорченный файл — то же, что истории нет."""
    try:
        raw = json.loads((data_dir / SEED_STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): {str(key): str(value) for key, value in files.items()}
        for name, files in raw.items()
        if isinstance(files, dict)
    }


def _write_seed_state(data_dir: Path, state: dict[str, dict[str, str]]) -> None:
    """Записывает отпечатки атомарно: оборванная запись не должна стереть историю."""
    target = data_dir / SEED_STATE
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, target)


def _replace(source: Path, destination: Path) -> None:
    """Кладёт файл из образа на место прежнего одним шагом — без полузаписанного файла."""
    temporary = destination.with_name(f".{destination.name}.seed-tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def _backup_kb(kb_dir: Path) -> Path:
    """Копия базы знаний перед обновлением — в том же виде, что делает CRM.

    Каталог ``.backups/<время UTC>-repo`` CRM показывает в «Резервных копиях» и
    умеет из него восстановить базу, если новая версия из репозитория не нужна.
    """
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = kb_dir / ".backups" / f"{stamp}-repo"
    suffix = 1
    while target.exists():
        suffix += 1
        target = kb_dir / ".backups" / f"{stamp}-repo{suffix}"
    target.mkdir(parents=True)
    for item in sorted(kb_dir.glob("*.yaml")):
        shutil.copy2(item, target / item.name)
    return target


def _kb_loads(kb_dir: Path, media_dir: Path) -> bool:
    """Читается ли база знаний на диске целиком, со всеми перекрёстными проверками."""
    try:
        from app.kb import loader as kb_loader

        kb_loader.load_sync(
            kb_dir, media_dir=media_dir, schema_version=int(os.environ.get("KB_SCHEMA_VERSION", "1"))
        )
    except Exception:  # noqa: BLE001 - причину покажет check_kb после запуска
        return False
    return True


def sqlite_file_of(url: str) -> Path | None:
    """Путь к файлу из ``DATABASE_URL``. ``None`` — это не SQLite.

    Понимает обе записи SQLAlchemy: ``sqlite:///relative/path.db`` (три косые,
    путь относительный) и ``sqlite:////absolute/path.db`` (четыре — абсолютный).
    """
    scheme, sep, rest = url.partition("://")
    if not sep or scheme.split("+", 1)[0].strip().lower() != "sqlite":
        return None
    # Одна косая — разделитель хоста, всё после неё и есть путь. Поэтому
    # ``sqlite:///data/bot.db`` относителен, а ``sqlite:////data/bot.db`` — нет.
    path = rest[1:] if rest.startswith("/") else rest
    if not path or path.startswith(":memory:"):
        return None
    return Path(path)


def align_db_path(env: dict[str, str], data_dir: Path) -> Path | None:
    """Сводит бота и CRM на один файл базы. Возвращает файл или ``None``.

    Раньше ``DATABASE_URL`` уважался как есть, а путь к базе для CRM
    прописывался жёстко как ``<data_dir>/bot.db``. Совпадало это только при
    пустом ``DATABASE_URL``. Стоило владельцу задать его на Railway по образцу
    из документации — ``sqlite+aiosqlite:///data/bot.db``, путь относительный —
    и бот начинал писать в ``/app/data/bot.db`` внутри контейнера, а CRM читала
    ``/data/bot.db`` на томе. Обе вкладки, «Клиенты» и «Заявки», оставались
    пустыми, и всё написанное пропадало при следующем передеплое.

    Относительный путь на Railway ошибочен всегда, поэтому он переписывается на
    том — молча это делать нельзя, и в журнал уходит явное сообщение.
    """
    default = data_dir / "bot.db"
    url = env.get("DATABASE_URL", "").strip()
    if not url:
        env["DATABASE_URL"] = f"sqlite+aiosqlite:///{default}"
        return default

    target = sqlite_file_of(url)
    if target is None:
        # Postgres — законный выбор, но CRM читает только SQLite напрямую.
        _log("=" * 72)
        _log("ВНИМАНИЕ: DATABASE_URL указывает не на SQLite.")
        _log("Бот с такой базой работать будет, а CRM читать её не умеет:")
        _log("вкладки «Клиенты» и «Заявки» останутся пустыми.")
        _log("=" * 72)
        return None

    if not target.is_absolute():
        _log(f"DATABASE_URL с относительным путём '{target}' переписан на том: {default}")
        _log("Относительный путь живёт внутри контейнера и пропадает при передеплое.")
        target = default
        env["DATABASE_URL"] = f"sqlite+aiosqlite:///{target}"
    return target


def prepare_env(data_dir: Path) -> dict[str, str]:
    """Единое окружение для обоих процессов: одни и те же файлы у бота и CRM."""
    env = dict(os.environ)
    env.setdefault("APP_ENV", "prod")
    env.setdefault("PYTHONUNBUFFERED", "1")

    # Пути задаём принудительно: разъехавшись, бот и CRM начнут работать с
    # разными копиями данных, и это никак не проявится до первой правки.
    env["KB_DIR"] = str(data_dir / "kb")
    env["MEDIA_DIR"] = str(data_dir / "media")
    # Отправкой в этой конфигурации занимается сам веб-процесс: отдельного
    # воркера ARQ здесь никто не запускает. При INLINE_WORKER=false очередь
    # уходит в ARQ, задачи копятся в Redis, который некому читать, а строки
    # outbox остаются pending — бот принимает сообщения и не отвечает ни на
    # одно, при этом выглядит совершенно исправным.
    inline = env.get("INLINE_WORKER", "").strip().lower()
    if inline not in ("1", "true", "yes", "on"):
        if inline:
            _log("=" * 72)
            _log(f"INLINE_WORKER={inline} переопределён на true.")
            _log("Иначе отправлять сообщения было бы некому: воркер ARQ этой")
            _log("службой не запускается, и ответы копились бы в очереди.")
            _log("=" * 72)
        env["INLINE_WORKER"] = "true"

    env["STATE_BACKEND"] = env.get("STATE_BACKEND") or "sqlite"
    env["STATE_SQLITE_PATH"] = str(data_dir / "state.db")
    env["ADMIN_DB_PATH"] = str(data_dir / "admin.db")
    # CRM обязана читать тот же файл, в который пишет бот, — иначе её вкладки
    # пусты, а причина не видна ниоткуда.
    bot_db = align_db_path(env, data_dir)
    env["CRM_BOT_DB"] = str(bot_db) if bot_db is not None else str(data_dir / "bot.db")
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
        _log(f"{name}: из образа новых и обновлённых файлов — {count} (правки владельца не тронуты)")

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
