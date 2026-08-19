"""Заявки на пробное занятие."""

from __future__ import annotations

import csv
import io
from typing import Any, Final

from flask import Blueprint, Response, render_template, request

from crm.app import data, login_required, snapshot
from crm.forms import text

#: Метка кодировки в начале файла: без неё Excel открывает кириллицу
#: иероглифами, и выгрузку считают битой.
BOM: Final[str] = "\ufeff"

bp = Blueprint("leads", __name__, url_prefix="/leads")

#: Человеческие названия статусов заявки.
STATUS_TITLES: dict[str, str] = {
    "trial_booked": "Записан на пробное",
    "thinking": "Думает",
    "needs_call": "Нужен звонок",
    "escalated": "Передан администратору",
    "not_target": "Не наш клиент",
    "no_show": "Не пришёл",
    "converted": "Купил абонемент",
}


@bp.route("/")
@login_required
def index() -> Any:
    """Список заявок с фильтром по статусу."""
    bot = data()
    status = text(request.args, "status")
    gyms = {gym.id: (gym.title.ru or gym.id) for gym in snapshot().gyms.gyms}
    return render_template(
        "leads.html",
        leads=bot.leads(status=status),
        statuses=bot.lead_statuses(),
        status=status,
        titles=STATUS_TITLES,
        gyms=gyms,
    )


@bp.route("/export.csv")
@login_required
def export() -> Any:
    """Выгрузка заявок в CSV."""
    rows = data().leads(status=text(request.args, "status"), limit=100000)
    gyms = {gym.id: (gym.title.ru or gym.id) for gym in snapshot().gyms.gyms}
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        ["Когда", "Канал", "Родитель", "Телефон", "Ребёнок", "Возраст", "Район", "Зал",
         "Когда придут", "Статус", "Мотив", "Возражение"]
    )
    for lead in rows:
        writer.writerow(
            [
                lead.created_at.strftime("%d.%m.%Y %H:%M") if lead.created_at else "",
                lead.channel_title, lead.parent_name or "", lead.phone or "",
                lead.child_name, lead.child_age, lead.district or "",
                gyms.get(lead.gym_id or "", lead.gym_id or ""),
                lead.trial_slot_text or "",
                STATUS_TITLES.get(lead.status, lead.status),
                lead.motivation or "", lead.main_objection or "",
            ]
        )
    return Response(
        BOM + buffer.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=leads.csv"},
    )
