"""Репозиторий лидов.

Один активный лид на диалог (``ux_lead_conversation``): повторный ``create_trial_lead``
не плодит строки, а дополняет существующую. Персональные данные лежат открытым текстом —
шифрования и сроков хранения в проекте нет.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import Lead, utcnow
from app.types import Gender, LeadDraft, LeadStatus, PhoneSource, StorageError

__all__ = [
    "get_by_conversation",
    "get_by_id",
    "list_recent",
    "mark_notified",
    "upsert",
]


def _val(value: Enum | str | None) -> str | None:
    """Строковое значение enum'а (или строки как есть)."""
    if value is None:
        return None
    return value.value if isinstance(value, Enum) else value


async def upsert(session: AsyncSession, draft: LeadDraft) -> UUID:
    """Создаёт или обновляет лид диалога, возвращает ``lead.id``.

    Правила слияния: непустые поля черновика перекрывают сохранённые; поля со
    «значением по умолчанию» (``phone_source=none``, ``child_gender=unknown``,
    ``messages_count=0``) сохранённые данные не затирают, а признак ``escalation``
    только взводится и никогда не снимается.
    """
    if draft.conversation_id is None:
        raise StorageError("LeadDraft без conversation_id сохранить нельзя")

    existing = await get_by_conversation(session, draft.conversation_id)
    now = utcnow()

    if existing is None:
        lead = Lead(
            id=draft.lead_id or uuid4(),
            conversation_id=draft.conversation_id,
            created_at=now,
            updated_at=now,
            channel=_val(draft.channel),
            channel_user=draft.channel_user,
            instagram_username=draft.instagram_username,
            lang=_val(draft.lang),
            parent_name=draft.parent_name,
            parent_relation=draft.parent_relation,
            phone=draft.phone,
            phone_source=_val(draft.phone_source) or PhoneSource.NONE.value,
            # Колонки NOT NULL по контракту. Черновик без имени/возраста приходит от
            # эскалации («думает», данных ещё нет) — такой лид тоже обязан сохраниться.
            child_name=draft.child_name or "",
            child_age=draft.child_age if draft.child_age is not None else 0,
            child_birth_year=draft.child_birth_year,
            child_gender=_val(draft.child_gender) or Gender.UNKNOWN.value,
            district=draft.district,
            gym_id=draft.gym_id,
            trial_slot=draft.trial_slot,
            trial_slot_text=draft.trial_slot_text,
            motivation=draft.motivation,
            main_objection=draft.main_objection,
            prior_experience=draft.prior_experience,
            health_notes=draft.health_notes,
            status=_val(draft.status) or LeadStatus.THINKING.value,
            escalation=draft.escalation,
            dialog_url=draft.dialog_url,
            messages_count=draft.messages_count,
        )
        session.add(lead)
        await session.flush()
        return lead.id

    _merge_into(existing, draft)
    existing.updated_at = now
    await session.flush()
    return existing.id


def _merge_into(lead: Lead, draft: LeadDraft) -> None:
    """Переносит непустые поля черновика в сохранённую строку."""
    simple_fields = (
        "channel_user",
        "instagram_username",
        "parent_name",
        "parent_relation",
        "phone",
        "child_name",
        "child_age",
        "child_birth_year",
        "district",
        "gym_id",
        "trial_slot",
        "trial_slot_text",
        "motivation",
        "main_objection",
        "prior_experience",
        "health_notes",
        "dialog_url",
    )
    for name in simple_fields:
        value = getattr(draft, name)
        if value is not None:
            setattr(lead, name, value)

    if draft.channel is not None:
        lead.channel = draft.channel.value
    if draft.lang is not None:
        lead.lang = draft.lang.value
    if draft.phone_source is not PhoneSource.NONE:
        lead.phone_source = draft.phone_source.value
    if draft.child_gender is not Gender.UNKNOWN:
        lead.child_gender = draft.child_gender.value
    # THINKING — значение по умолчанию черновика. Оно не должно откатывать уже
    # оформленную запись на пробное или конверсию.
    if draft.status is not LeadStatus.THINKING or lead.status == LeadStatus.THINKING.value:
        lead.status = draft.status.value
    if draft.escalation:
        lead.escalation = True
    if draft.messages_count:
        lead.messages_count = draft.messages_count


async def get_by_conversation(session: AsyncSession, conv_id: UUID) -> Lead | None:
    """Лид диалога или ``None``."""
    stmt = sa.select(Lead).where(Lead.conversation_id == conv_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_by_id(session: AsyncSession, lead_id: UUID) -> Lead | None:
    """Лид по первичному ключу."""
    return await session.get(Lead, lead_id)


async def mark_notified(session: AsyncSession, lead_id: UUID, at: datetime) -> None:
    """Отмечает, что карточка лида ушла администратору."""
    await session.execute(
        sa.update(Lead).where(Lead.id == lead_id).values(notified_at=at, updated_at=utcnow())
    )


async def list_recent(
    session: AsyncSession, *, limit: int = 50, status: LeadStatus | None = None
) -> list[Lead]:
    """Последние лиды по дате создания, при необходимости — только с нужным статусом."""
    conditions: list[Any] = []
    if status is not None:
        conditions.append(Lead.status == status.value)
    stmt = (
        sa.select(Lead)
        .where(*conditions)
        .order_by(Lead.created_at.desc())
        .limit(max(1, limit))
    )
    return list((await session.execute(stmt)).scalars().all())
