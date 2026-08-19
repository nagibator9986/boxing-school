"""Раздача медиа по короткоживущей подписанной ссылке.

Wazzup скачивает файл по ``contentUri`` сразу после приёма сообщения и **редиректы
не поддерживает** — поэтому ответ здесь всегда прямой: 200, тело файла, честные
``Content-Type`` и ``Content-Length``.

Кодек токена живёт в ``app.tools.content`` (INTERFACES §18 п.6): ``api`` импортирует
``tools``, обратной зависимости нет. Здесь только проверка подписи, срока и пути.
"""

from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from app.deps import get_settings_dep
from app.logging_conf import get_logger
from app.tools.content import parse_media_token

router = APIRouter(tags=["media"])

log = get_logger(__name__)

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def _resolve(token: str) -> Path:
    """Токен → абсолютный путь внутри ``media_dir``. Любая проблема — 404.

    404 (а не 403 и не 410) на всё: срок, подпись, выход за каталог, отсутствие файла.
    Различать эти случаи наружу — значит помогать перебирать токены.
    """
    settings = get_settings_dep()
    secret = settings.media_signing_key
    if not secret:
        log.error("media_secret_missing")
        raise _NOT_FOUND

    try:
        rel_path = parse_media_token(token, secret=secret, now=datetime.now(timezone.utc))
    except ValueError as exc:
        log.info("media_token_rejected", reason=str(exc))
        raise _NOT_FOUND from exc

    root = Path(settings.media_dir).resolve()
    try:
        path = (root / rel_path).resolve()
    except OSError as exc:  # pragma: no cover - битый путь в ФС
        raise _NOT_FOUND from exc

    # Подстраховка поверх проверки в кодеке токена: наружу из media_dir не выходим.
    if not path.is_relative_to(root):
        log.warning("media_path_escape", rel_path=rel_path)
        raise _NOT_FOUND
    if not path.is_file():
        log.warning("media_file_missing", rel_path=rel_path)
        raise _NOT_FOUND
    return path


@router.get("/media/{token}")
async def get_media(token: str) -> FileResponse:
    """Отдаёт файл напрямую: 200 + ``Content-Type``, без единого редиректа.

    Просроченный или поддельный токен -> 404. Путь вне ``MEDIA_DIR`` -> 404.
    """
    path = _resolve(token)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    log.info("media_served", name=path.name, media_type=media_type)
    # FileResponse сам ставит Content-Length по stat() и обрабатывает HEAD.
    return FileResponse(
        path=path,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{path.name}"',
            "Cache-Control": "private, max-age=60",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.head("/media/{token}", include_in_schema=False)
async def head_media(token: str) -> FileResponse:
    """``HEAD`` перед скачиванием: те же заголовки, пустое тело."""
    return await get_media(token)
