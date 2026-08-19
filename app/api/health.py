"""Проба живости, проба готовности и экспорт метрик.

Разделение принципиальное (research-railway §11, §13):

* ``/healthz`` — путь healthcheck Railway. Проверяет **только** то, что процесс жив и
  принимает соединения. Ни БД, ни Redis, ни Wazzup: временно недоступная зависимость
  не должна валить деплой, а хост проверки — ``healthcheck.railway.app``.
* ``/readyz`` — эксплуатационная проба: БД, Redis, KB, канал Wazzup. В healthcheck Railway
  её ставить нельзя.

В Gemini не ходит ни та, ни другая: прогрев модели стоит денег и времени.
"""

from __future__ import annotations

import asyncio
import hmac
import time
from typing import Final

from fastapi import APIRouter, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse

from app.config import Settings
from app.deps import get_settings_dep, metrics_module, runtime_or_none
from app.kb import loader as kb_loader
from app.logging_conf import get_logger
from app.storage import db as storage_db

router = APIRouter(tags=["health"])

log = get_logger(__name__)

#: Сколько ждём каждую проверку готовности по отдельности.
CHECK_TIMEOUT_S: Final[float] = 3.0

#: Состояние каналов Wazzup кэшируется: /readyz не должен долбить внешний API.
CHANNELS_CACHE_TTL_S: Final[float] = 60.0

_channels_cache: tuple[float, bool] = (0.0, False)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Живость процесса. Ничего не проверяет, всегда 200. Это путь healthcheck Railway."""
    return {"status": "ok"}


def _ops_authorized(authorization: str | None, settings: Settings) -> bool:
    """Тот же Bearer, что и у админки. В ``app_env=local`` эксплуатация открыта.

    Домен Railway публичный, а TrustedHostMiddleware его, разумеется, пропускает:
    без этой проверки любой желающий снимал полный дамп Prometheus (выручка в
    ``llm_cost_usd_total``, число лидов, id каналов Wazzup в лейблах) и видел в
    ``/readyz``, какая именно зависимость лежит.
    """
    if settings.app_env == "local":
        return True
    token = (settings.admin_token or "").strip()
    if not token:
        return False
    header = (authorization or "").strip()
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(value.strip().encode("utf-8"), token.encode("utf-8"))


@router.get("/readyz")
async def readyz(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Готовность: БД, Redis, загруженная KB, состояние канала Wazzup.

    Не готово -> 503. Разбивку по зависимостям (``{"db": bool, ...}``) отдаём только
    по админскому Bearer: код ответа виден всем — он и нужен эксплуатации, — а вот
    сведения о том, что именно сломано, помогают только атакующему.
    """
    settings = get_settings_dep()
    db_ok, redis_ok, channels_ok = await asyncio.gather(
        _check_db(),
        _check_redis(settings),
        _check_channels(settings),
    )
    checks = {
        "db": db_ok,
        "redis": redis_ok,
        "kb": _check_kb(),
        "channels": channels_ok,
    }
    ready = all(checks.values())
    if not ready:
        log.warning("readyz_not_ready", **checks)
    code = status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
    if not _ops_authorized(authorization, settings):
        return JSONResponse(
            status_code=code, content={"status": "ok" if ready else "not_ready"}
        )
    return JSONResponse(status_code=code, content=checks)


@router.get("/metrics", include_in_schema=False)
async def metrics(authorization: str | None = Header(default=None)) -> Response:
    """Prometheus. Только при ``metrics_enabled`` и только по админскому Bearer."""
    settings = get_settings_dep()
    if not settings.metrics_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    if not _ops_authorized(authorization, settings):
        # 404, а не 401: существование ручки на публичном домене не подтверждаем.
        log.warning("metrics_unauthorized")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    module = metrics_module()
    if module is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="метрики недоступны"
        )
    payload, content_type = module.render_latest()
    return Response(content=payload, media_type=content_type)


# --------------------------------------------------------------------------- #
# Проверки
# --------------------------------------------------------------------------- #
async def _check_db() -> bool:
    """``SELECT 1`` в отдельной сессии. Любая ошибка — это ``False``, а не исключение."""
    runtime = runtime_or_none()
    maker = runtime.sessionmaker if runtime is not None else None
    try:
        if maker is None:
            maker = storage_db.get_sessionmaker()
        async with maker() as session:
            return await asyncio.wait_for(storage_db.ping(session), timeout=CHECK_TIMEOUT_S)
    except Exception as exc:
        log.warning("readyz_db_failed", error=type(exc).__name__)
        return False


async def _check_redis(settings: Settings) -> bool:
    """Роундтрип к хранилищу состояний. При ``state_backend=memory`` проверять нечего."""
    if settings.state_backend != "redis":
        return True
    runtime = runtime_or_none()
    if runtime is None:
        return False
    try:
        await asyncio.wait_for(runtime.state.get("readyz:probe"), timeout=CHECK_TIMEOUT_S)
        return True
    except Exception as exc:
        log.warning("readyz_redis_failed", error=type(exc).__name__)
        return False


def _check_kb() -> bool:
    """Снимок KB загружен и непустой."""
    return bool(kb_loader.current_hash())


async def _check_channels(settings: Settings) -> bool:
    """Активность канала Wazzup с кэшем на :data:`CHANNELS_CACHE_TTL_S` секунд.

    Учитываются только каналы, указанные в настройках; если ни один id не задан —
    достаточно любого активного канала аккаунта.
    """
    global _channels_cache

    if not settings.wazzup_api_key:
        return False

    cached_at, cached_value = _channels_cache
    now = time.monotonic()
    if now - cached_at < CHANNELS_CACHE_TTL_S:
        return cached_value

    runtime = runtime_or_none()
    if runtime is None:
        return False

    wanted = {
        channel_id
        for channel_id in (
            settings.wazzup_channel_id_whatsapp,
            settings.wazzup_channel_id_instagram,
        )
        if channel_id
    }
    try:
        rows = await asyncio.wait_for(runtime.wazzup.get_channels(), timeout=CHECK_TIMEOUT_S)
    except Exception as exc:
        log.warning("readyz_channels_failed", error=type(exc).__name__)
        _channels_cache = (now, False)
        return False

    if wanted:
        value = any(row.is_active for row in rows if row.channel_id in wanted)
    else:
        value = any(row.is_active for row in rows)
    _channels_cache = (now, value)
    return value


def reset_channels_cache() -> None:
    """Сброс кэша состояния каналов. Только для тестов и ручного перезапроса."""
    global _channels_cache
    _channels_cache = (0.0, False)
