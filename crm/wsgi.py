"""Точка входа WSGI, если CRM поднимают отдельно от бота.

В боевом запуске она не используется: ``scripts/serve.py`` монтирует CRM внутрь
ASGI-приложения (:mod:`app.asgi`), чтобы вебхук Wazzup и CRM жили на одном порту.
Модуль оставлен для случая, когда CRM нужна сама по себе — под любым WSGI-сервером.
"""

from __future__ import annotations

from crm.app import create_app
from crm.config import CrmConfig

app = create_app(CrmConfig.from_env())

__all__ = ["app"]
