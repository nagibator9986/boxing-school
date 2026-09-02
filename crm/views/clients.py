"""Клиенты: кто написал, из какого канала, о чём говорили, что делает бот."""

from __future__ import annotations

import csv
import io
from typing import Any, Final

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

from crm.app import config, data, login_required
from crm.botdb import CHANNEL_TITLES
from crm.forms import integer, text

#: Метка кодировки в начале файла: без неё Excel открывает кириллицу
#: иероглифами, и выгрузку считают битой.
BOM: Final[str] = "\ufeff"

bp = Blueprint("clients", __name__, url_prefix="/clients")

#: Сколько строк на странице. Больше сотни в таблице всё равно не читают.
PAGE_SIZE = 50


@bp.route("/")
@login_required
def index() -> Any:
    """Список клиентов с фильтрами по каналу, поиском и постраничным выводом."""
    bot = data()
    channel = text(request.args, "channel")
    search = text(request.args, "q")
    only_leads = text(request.args, "leads") == "1"
    page = max(1, integer(request.args, "page", default=1))

    rows = bot.clients(
        channel=channel,
        search=search,
        only_leads=only_leads,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
    )
    total = bot.clients_count(channel=channel, search=search, only_leads=only_leads)

    return render_template(
        "clients.html",
        clients=rows,
        total=total,
        page=page,
        pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
        channel=channel,
        search=search,
        only_leads=only_leads,
        channels=sorted(bot.overview()["channels"].items()),
        channel_titles=CHANNEL_TITLES,
    )


@bp.route("/<conv_id>")
@login_required
def show(conv_id: str) -> Any:
    """Карточка клиента: переписка целиком, заявка и управление ботом."""
    bot = data()
    client = bot.client(conv_id)
    if client is None:
        flash("Такого клиента нет.", "error")
        return redirect(url_for("clients.index"))
    return render_template(
        "client.html",
        client=client,
        dialog=bot.dialog(conv_id),
        lead=bot.lead_for(conv_id),
    )


@bp.route("/<conv_id>/reply", methods=["POST"])
@login_required
def reply(conv_id: str) -> Any:
    """Ответ клиенту прямо отсюда — и пауза боту, чтобы не писал поверх человека.

    Раньше владелец видел переписку и эскалацию, но отвечать шёл в WhatsApp с
    телефона: CRM показывала разговор и не давала в него вступить.
    """
    bot = data()
    client = bot.client(conv_id)
    if client is None:
        flash("Такого клиента нет.", "error")
        return redirect(url_for("clients.index"))

    message = text(request.form, "text")
    denial = bot.reply_to_client(conv_id, message)
    if denial is not None:
        flash(denial, "error")
        return redirect(url_for("clients.show", conv_id=conv_id))

    # Отвечает человек — бот обязан замолчать. Иначе следующий вопрос клиента
    # он подхватит сам и заговорит поверх начатого разговора.
    minutes = _operator_pause_minutes()
    if bot.pause_bot(conv_id, client.conv_key, minutes=minutes):
        flash(f"Сообщение отправлено. Бот молчит в этом диалоге {minutes} мин.", "ok")
    else:
        # Обещать тишину, которой не будет, нельзя: оператор начнёт разговор, а
        # бот ответит поверх него, и никто не поймёт, откуда второй голос.
        flash(
            "Сообщение отправлено, но поставить бота на паузу не удалось — "
            "он может ответить в этот диалог сам.",
            "error",
        )
    return redirect(url_for("clients.show", conv_id=conv_id))


def _operator_pause_minutes() -> int:
    """Пауза бота после ответа человека — та же, что задана владельцем в настройках."""
    try:
        from app.admin.runtime_settings import load_runtime_settings

        settings = load_runtime_settings(config().admin_db)
        return max(1, int(settings.operator_pause_minutes))
    except Exception:  # noqa: BLE001 - настройки недоступны: работает значение конфигурации
        from app.config import get_settings

        return max(1, int(get_settings().pause_operator_minutes))


@bp.route("/<conv_id>/close", methods=["POST"])
@login_required
def close(conv_id: str) -> Any:
    """Завершает разговор: следующее сообщение клиента начнётся с меню."""
    bot = data()
    if bot.client(conv_id) is None:
        flash("Такого клиента нет.", "error")
        return redirect(url_for("clients.index"))
    if bot.close_dialog(conv_id):
        flash("Диалог завершён. Следующее сообщение клиента начнётся с меню.", "ok")
    else:
        flash("Не получилось завершить диалог.", "error")
    return redirect(url_for("clients.show", conv_id=conv_id))


@bp.route("/<conv_id>/pause", methods=["POST"])
@login_required
def pause(conv_id: str) -> Any:
    """Ставит бота на паузу: дальше в диалоге отвечает человек."""
    bot = data()
    client = bot.client(conv_id)
    if client is None:
        flash("Такого клиента нет.", "error")
        return redirect(url_for("clients.index"))
    minutes = integer(request.form, "minutes", default=60)
    if bot.pause_bot(conv_id, client.conv_key, minutes=minutes):
        flash(f"Бот молчит в этом диалоге {minutes} мин — отвечайте сами.", "ok")
    else:
        flash("Не получилось поставить паузу.", "error")
    return redirect(url_for("clients.show", conv_id=conv_id))


@bp.route("/<conv_id>/resume", methods=["POST"])
@login_required
def resume(conv_id: str) -> Any:
    """Возвращает бота в диалог."""
    bot = data()
    client = bot.client(conv_id)
    if client is None:
        flash("Такого клиента нет.", "error")
        return redirect(url_for("clients.index"))
    if bot.resume_bot(conv_id, client.conv_key):
        flash("Бот снова отвечает в этом диалоге.", "ok")
    else:
        flash("Не получилось снять паузу.", "error")
    return redirect(url_for("clients.show", conv_id=conv_id))


@bp.route("/export.csv")
@login_required
def export() -> Any:
    """Выгрузка клиентов в CSV — чтобы работать с ними в таблице."""
    rows = data().clients(
        channel=text(request.args, "channel"),
        search=text(request.args, "q"),
        only_leads=text(request.args, "leads") == "1",
        limit=100000,
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        ["Имя", "Канал", "Идентификатор", "Телефон", "Язык", "Входящих", "Исходящих",
         "Первое обращение", "Последнее", "Есть заявка"]
    )
    for row in rows:
        writer.writerow(
            [
                row.name, row.channel_title, row.chat_id, row.phone or "", row.lang or "",
                row.msg_in, row.msg_out,
                row.first_at.strftime("%d.%m.%Y %H:%M") if row.first_at else "",
                row.last_at.strftime("%d.%m.%Y %H:%M") if row.last_at else "",
                "да" if row.has_lead else "нет",
            ]
        )
    # BOM: без него Excel открывает кириллицу как иероглифы, и файл считают битым.
    payload = BOM + buffer.getvalue()
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=clients.csv"},
    )
