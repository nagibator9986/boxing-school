"""Точка входа для gunicorn: ``gunicorn crm.wsgi:app``.

Отдельный модуль нужен, потому что gunicorn импортирует объект приложения, а не
вызывает функцию. Вся настройка — в :func:`crm.app.create_app`.
"""

from __future__ import annotations

from crm.app import create_app
from crm.config import CrmConfig

app = create_app(CrmConfig.from_env())

__all__ = ["app"]
