"""Одна служба на один порт: приём Wazzup и веб-интерфейс CRM.

На Railway у службы ровно один публичный порт, а разнести бота и CRM по двум
службам нельзя: диск между службами не разделяется, а им нужны одни и те же
файлы — база знаний и базы SQLite. Поэтому оба приложения живут в одном
процессе: FastAPI принимает вебхуки Wazzup и раздаёт медиа, а CRM монтируется
внутрь него на ``/crm``.

Второе решение здесь — **запасной режим**. Пока Wazzup не настроен (нет ключа
либо конфигурация не проходит блокеры старта), FastAPI поднимать нельзя: он
откажется стартовать, и вместе с ним пропадёт CRM — единственное место, где эту
конфигурацию можно исправить. В таком случае служба поднимает CRM и проверку
живости, а причину пишет в журнал открытым текстом.

Адрес CRM в обоих режимах один и тот же — ``/crm``. Меняющийся адрес был бы
худшим из решений: владелец школы не обязан знать, настроен ли Wazzup.
"""

from __future__ import annotations

from typing import Any

from a2wsgi import WSGIMiddleware
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

from app.config import Settings, get_settings
from app.logging_conf import get_logger

__all__ = ["build_app", "wazzup_ready"]

log = get_logger(__name__)


def wazzup_ready(settings: Settings) -> tuple[bool, list[str]]:
    """Можно ли поднимать приём Wazzup. Возвращает решение и причины отказа.

    Отсутствие ключа — не ошибка, а состояние «канал ещё не подключён»: бот
    работает в Telegram, CRM работает, вебхуку просто неоткуда взяться.
    """
    if not settings.wazzup_api_key.strip():
        return False, ["WAZZUP_API_KEY не задан — канал Wazzup не подключён"]
    blockers = settings.startup_blockers()
    return (not blockers), blockers


def _crm_app() -> Any:
    """WSGI-приложение CRM, обёрнутое для ASGI.

    Обёртка — из ``a2wsgi``, а не из Starlette: встроенная помечена устаревшей и
    будет удалена, а обновление Starlette приезжает вместе с любым обновлением
    FastAPI. Тихо отвалившаяся CRM после планового обновления — не тот сюрприз,
    который стоит экономии одной зависимости.
    """
    from crm.app import create_app as create_crm
    from crm.config import CrmConfig

    return WSGIMiddleware(create_crm(CrmConfig.from_env()))


def _fallback_app(reasons: list[str]) -> Starlette:
    """Служба без приёма Wazzup: только CRM и проверка живости."""
    for reason in reasons:
        log.warning("wazzup_disabled", reason=reason)

    async def health(_request: Any) -> JSONResponse:
        # Служба жива и обслуживает CRM. Признак wazzup=false виден снаружи:
        # молчаливое «ok» скрывало бы, что канал не поднялся.
        return JSONResponse({"status": "ok", "wazzup": False, "crm": True})

    async def home(_request: Any) -> RedirectResponse:
        return RedirectResponse("/crm/", status_code=307)

    return Starlette(
        routes=[
            Route("/healthz", health),
            Route("/readyz", health),
            Route("/", home),
            Mount("/crm", _crm_app()),
        ]
    )


def build_app(settings: Settings | None = None) -> Any:
    """Собирает приложение под текущую конфигурацию."""
    resolved = settings or get_settings()
    ready, reasons = wazzup_ready(resolved)
    if not ready:
        return _fallback_app(reasons)

    from starlette.routing import Route as StarletteRoute

    from app.main import create_app as create_api

    api = create_api(resolved)
    api.mount("/crm", _crm_app())

    async def home(_request: Any) -> RedirectResponse:
        """Корень ведёт в CRM: владелец школы набирает домен, а не путь."""
        return RedirectResponse("/crm/", status_code=307)

    # Маршрут ставится ПЕРВЫМ: в FastAPI совпадает первый подошедший, а корень
    # уже занят служебным признаком жизни из app.main. Проверку живости он не
    # трогает — Railway опрашивает /healthz.
    api.router.routes.insert(0, StarletteRoute("/", home, methods=["GET"]))
    log.info("asgi_built", wazzup=True, crm_path="/crm")
    return api


# Модульного экземпляра здесь намеренно нет: сборка приложения зависит от
# окружения и поднимает подключения, а импорт модуля происходит и в тестах.
# Запуск идёт фабрикой: ``uvicorn app.asgi:build_app --factory``.
