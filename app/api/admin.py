"""Служебная админка: hot-reload базы знаний, лиды, пауза бота, сводка, каналы.

Весь роутер закрыт двумя зависимостями: грубым rate limit'ом по адресу клиента и
сверкой ``Authorization: Bearer <admin_token>`` constant-time. Токен не задан в
настройках — админки не существует (404 на все пути), а не 401.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Final

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import (
    admin_rate_limit,
    get_session,
    get_settings_dep,
    metrics_module,
    require_admin,
    runtime_or_none,
)
from app.kb import loader as kb_loader
from app.logging_conf import get_logger
from app.storage import repo_conversation, repo_lead, repo_outbox
from app.storage.models import Conversation, Lead, OutboxMessage
from app.types import ChannelState, KBValidationError, LeadStatus, PauseReason

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(admin_rate_limit), Depends(require_admin)],
)

log = get_logger(__name__)

#: Потолок выборки лидов: админка — не выгрузка базы.
MAX_LEADS_LIMIT: Final[int] = 500

#: Потолок паузы, поставленной руками: сутки.
MAX_PAUSE_MINUTES: Final[int] = 24 * 60


# --------------------------------------------------------------------------- #
# База знаний
# --------------------------------------------------------------------------- #
@router.post("/kb/reload")
async def reload_kb(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    """Перечитывает KB с диска.

    Успех -> ``{"old_hash", "new_hash"}``; ошибка валидации -> 422 с перечнем полей,
    при этом **старый снимок продолжает работать**: подмена ссылки происходит только
    после успешной загрузки и валидации (``kb.loader.reload``).
    """
    settings = get_settings_dep()
    try:
        old_hash, new_hash = await kb_loader.reload(
            settings.kb_dir,
            media_dir=settings.media_dir,
            schema_version=settings.kb_schema_version,
        )
    except KBValidationError as exc:
        log.warning("kb_reload_rejected", errors=len(exc.errors))
        _count_kb_failure()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": exc.code,
                "message": exc.message,
                "errors": list(exc.errors),
            },
        ) from exc
    except Exception as exc:
        log.error("kb_reload_failed", error=type(exc).__name__)
        _count_kb_failure()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "kb_reload_failed"},
        ) from exc

    mirrored = "0"
    try:
        snapshot = kb_loader.get_snapshot()
        mirrored = str(await kb_loader.mirror_to_db(session, snapshot))
    except Exception as exc:  # зеркало в БД — удобство отчётов, не условие работы бота
        await session.rollback()
        log.warning("kb_mirror_failed", error=type(exc).__name__)

    log.info("kb_switch", old_hash=old_hash, new_hash=new_hash, gyms_mirrored=mirrored)
    return {
        "status": "reloaded" if old_hash != new_hash else "unchanged",
        "old_hash": old_hash,
        "new_hash": new_hash,
        "gyms_mirrored": mirrored,
    }


def _count_kb_failure() -> None:
    """``kb_load_failures_total`` — метрика опциональна, ответ от неё не зависит."""
    metrics = metrics_module()
    if metrics is None:
        return
    try:
        metrics.observe_kb_load_failure()
    except Exception:  # pragma: no cover
        pass


# --------------------------------------------------------------------------- #
# Лиды
# --------------------------------------------------------------------------- #
@router.get("/leads")
async def list_leads(
    limit: int = 50,
    status: LeadStatus | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Последние лиды, при необходимости — только с нужным статусом."""
    capped = max(1, min(int(limit), MAX_LEADS_LIMIT))
    rows = await repo_lead.list_recent(session, limit=capped, status=status)
    return [_lead_to_dict(row) for row in rows]


def _lead_to_dict(lead: Lead) -> dict[str, Any]:
    """Плоское представление лида. Телефон отдаётся как есть: он и нужен, чтобы позвонить."""
    return {
        "id": str(lead.id),
        "conversation_id": str(lead.conversation_id),
        "created_at": _iso(lead.created_at),
        "updated_at": _iso(lead.updated_at),
        "status": lead.status,
        "escalation": lead.escalation,
        "lang": lead.lang,
        "channel": lead.channel,
        "channel_user": lead.channel_user,
        "instagram_username": lead.instagram_username,
        "parent_name": lead.parent_name,
        "parent_relation": lead.parent_relation,
        "phone": lead.phone,
        "phone_source": lead.phone_source,
        "child_name": lead.child_name,
        "child_age": lead.child_age,
        "child_gender": lead.child_gender,
        "district": lead.district,
        "gym_id": lead.gym_id,
        "trial_slot": _iso(lead.trial_slot),
        "trial_slot_text": lead.trial_slot_text,
        "motivation": lead.motivation,
        "main_objection": lead.main_objection,
        "health_notes": lead.health_notes,
        "dialog_url": lead.dialog_url,
        "messages_count": lead.messages_count,
        "notified_at": _iso(lead.notified_at),
    }


def _iso(value: datetime | None) -> str | None:
    """Дата в ISO-8601 или ``None``."""
    return value.isoformat() if value is not None else None


# --------------------------------------------------------------------------- #
# Пауза бота
# --------------------------------------------------------------------------- #
@router.post("/pause")
async def pause(
    conv_key: str,
    minutes: int = 60,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Ставит паузу бота в диалоге вручную (``PauseReason.MANUAL``)."""
    runtime = _require_runtime()
    conv = await repo_conversation.get_by_conv_key(session, conv_key)
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="диалог не найден")

    from app.core import pause as pause_mod  # ленивый импорт: ядро тяжелее, чем нужно API

    capped = max(1, min(int(minutes), MAX_PAUSE_MINUTES))
    await pause_mod.set_pause(
        runtime.state,
        session,
        conv.id,
        conv_key,
        minutes=capped,
        reason=PauseReason.MANUAL,
        now=datetime.now(timezone.utc),
    )
    log.info("admin_pause_set", minutes=capped)
    return {"status": "paused", "conversation_id": str(conv.id), "minutes": str(capped)}


@router.post("/resume")
async def resume(
    conv_key: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Снимает паузу бота руками администратора."""
    runtime = _require_runtime()
    conv = await repo_conversation.get_by_conv_key(session, conv_key)
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="диалог не найден")

    from app.core import pause as pause_mod

    await pause_mod.resume(runtime.state, session, conv.id, conv_key, by="admin")
    log.info("admin_pause_cleared")
    return {"status": "resumed", "conversation_id": str(conv.id)}


# --------------------------------------------------------------------------- #
# Сводка и каналы
# --------------------------------------------------------------------------- #
@router.get("/stats")
async def stats(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Короткая эксплуатационная сводка: процесс, KB, очередь, объёмы в БД.

    Внешних вызовов не делает — ни Wazzup, ни Gemini.
    """
    settings = get_settings_dep()
    runtime = runtime_or_none()
    now = datetime.now(timezone.utc)

    conversations = await session.scalar(sa.select(sa.func.count()).select_from(Conversation))
    leads_total = await session.scalar(sa.select(sa.func.count()).select_from(Lead))
    by_status = await session.execute(
        sa.select(Lead.status, sa.func.count()).group_by(Lead.status)
    )
    outbox_by_state = await session.execute(
        sa.select(OutboxMessage.state, sa.func.count()).group_by(OutboxMessage.state)
    )
    pending = await repo_outbox.pending_count(session)

    kb_hash = kb_loader.current_hash()
    kb_info: dict[str, Any] = {"kb_hash": kb_hash, "loaded": bool(kb_hash)}
    if kb_hash:
        snapshot = kb_loader.get_snapshot()
        kb_info.update(
            loaded_at=_iso(snapshot.loaded_at),
            gyms=len(snapshot.gyms.gyms),
            faq=len(snapshot.faq),
            artifacts=len(snapshot.media),
        )

    return {
        "now": now.isoformat(),
        "app_env": settings.app_env,
        "started_at": _iso(runtime.started_at) if runtime is not None else None,
        "uptime_s": int((now - runtime.started_at).total_seconds()) if runtime is not None else 0,
        "queue": {
            "kind": runtime.queue_kind if runtime is not None else None,
            "inline_worker": settings.inline_worker,
            "max_jobs": settings.worker_max_jobs,
        },
        "kb": kb_info,
        "db": {
            "conversations": int(conversations or 0),
            "leads": int(leads_total or 0),
            "leads_by_status": {str(row[0]): int(row[1]) for row in by_status},
            "outbox_by_state": {str(row[0]): int(row[1]) for row in outbox_by_state},
            "outbox_pending": int(pending),
        },
        "channels": {
            "whatsapp": bool(settings.wazzup_channel_id_whatsapp),
            "instagram": bool(settings.wazzup_channel_id_instagram),
        },
    }


@router.get("/health/channels")
async def channels() -> list[ChannelState]:
    """Живое состояние каналов Wazzup (``GET /v3/channels``)."""
    runtime = _require_runtime()
    try:
        return await runtime.wazzup.get_channels()
    except Exception as exc:
        log.warning("admin_channels_failed", error=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Wazzup недоступен"
        ) from exc


def _require_runtime() -> Any:
    """Ресурсы процесса; до окончания старта админка отвечает 503."""
    runtime = runtime_or_none()
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="приложение ещё не инициализировано",
        )
    return runtime
