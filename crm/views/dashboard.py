"""Главная страница и отчёт о пробелах в данных."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, render_template

from app.kb import gaps as kb_gaps
from crm.app import data, login_required, snapshot

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@login_required
def index() -> Any:
    """Сводка: клиенты, заявки, каналы, расходы на модель, состояние данных."""
    bot = data()
    overview = bot.overview()
    kb = snapshot()

    open_gaps = kb_gaps.open_gaps(kb)
    gyms = list(kb.gyms.gyms)
    without_schedule = [gym for gym in gyms if gym.active and not gym.schedule]

    # «Живой ли бот» определяем по последнему сообщению: отдельного пульса он не
    # шлёт, а выдумывать «онлайн» по факту запущенной CRM нельзя — это разные
    # процессы, и один вполне может работать без другого.
    last_message = bot.last_message_at()

    return render_template(
        "dashboard.html",
        overview=overview,
        daily=bot.daily_counts(days=14),
        clients=bot.clients(limit=8),
        leads=bot.leads(limit=6),
        gaps=[kb_gaps.info(gap) for gap in open_gaps if kb_gaps.info(gap)],
        gyms_total=len(gyms),
        gyms_active=sum(1 for gym in gyms if gym.active),
        without_schedule=without_schedule,
        artifacts=len(kb.media),
        artifacts_on=sum(1 for artifact in kb.media if artifact.enabled),
        faq_count=len(kb.faq),
        last_message=last_message,
    )


@bp.route("/gaps")
@login_required
def gaps() -> Any:
    """Что бот сегодня не может ответить и какой вопрос закроет пробел."""
    kb = snapshot()
    open_refs = set(kb_gaps.open_gaps(kb))
    rows = []
    for ref in kb_gaps.all_gaps():
        info = kb_gaps.info(ref)
        if info is None:
            continue
        rows.append(
            {
                "info": info,
                "open": ref in open_refs,
                "phrase": kb_gaps.say_no_data(kb, ref).get("ru", ""),
            }
        )
    order = {"critical": 0, "high": 1, "medium": 2}
    rows.sort(key=lambda row: (not row["open"], order.get(row["info"].priority, 3)))
    return render_template("gaps.html", rows=rows)
