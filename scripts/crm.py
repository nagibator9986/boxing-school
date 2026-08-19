#!/usr/bin/env python
"""Запуск CRM: управление ботом, база знаний, клиенты.

Запуск::

    .venv/bin/python scripts/crm.py

Откроется на http://127.0.0.1:8000. Пароль — тот же, что открывает ``/admin``
в Telegram (``ADMIN_PASSWORD`` в ``.env``, по умолчанию тот, что задан в
конфигурации).

CRM и бот — **разные процессы**, и работать могут независимо: CRM правит файлы
базы знаний и настройки, бот замечает правки сам. Останавливать бота на время
работы в CRM не нужно.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

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


def main() -> int:
    """Поднимает веб-сервер CRM."""
    dotenv = _read_dotenv(ROOT / ".env")
    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)

    os.environ.setdefault("APP_ENV", "local")
    os.environ.setdefault("STATE_BACKEND", "sqlite")
    os.environ.setdefault("WAZZUP_API_KEY", "crm-local")
    os.environ.setdefault("WAZZUP_WEBHOOK_SECRET", "crm-secret-crm-secret-crm")
    for key in ("ADMIN_PASSWORD", "GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "CRM_SECRET_KEY"):
        if key in dotenv and key not in os.environ:
            os.environ[key] = dotenv[key]

    from crm.app import create_app
    from crm.config import CrmConfig

    config = CrmConfig.from_env(ROOT)
    app = create_app(config)

    host = os.environ.get("CRM_HOST", "127.0.0.1")
    port = int(os.environ.get("CRM_PORT", "8000"))

    print("\nCRM AINAZAROV TOP TEAM")
    print(f"  адрес        : http://{host}:{port}")
    print(f"  база знаний  : {config.kb_dir}")
    print(f"  диалоги      : {config.bot_db}{'' if config.bot_db.is_file() else '  (пока не создана — бот ещё не запускался)'}")
    print(f"  настройки    : {config.admin_db}")
    print("  вход по тому же паролю, что и /admin в Telegram")
    print("  Ctrl-C — остановить\n")

    # debug=False намеренно: перезагрузчик Flask поднимает второй процесс, а два
    # процесса, пишущих в одни и те же YAML, однажды сделают это одновременно.
    app.run(host=host, port=port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
