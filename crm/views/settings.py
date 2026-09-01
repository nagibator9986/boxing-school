"""Настройки бота и список администраторов.

Настройки лежат в ``data/admin.db`` — той же базе, что видит Telegram-админка,
и читаются ботом на каждом ходу (:mod:`app.admin.runtime_settings`). Поэтому
переключатель здесь и переключатель в чате — это один и тот же переключатель,
а не две копии, которые однажды разойдутся.
"""

from __future__ import annotations

import re
from typing import Any

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.admin.admin_store import SETTING_SPECS, AdminStore
from app.admin.runtime_settings import load_runtime_settings
from crm.app import config, login_required
from crm.forms import flag, integer, text

bp = Blueprint("settings", __name__, url_prefix="/settings")

#: Диапазон времени вида ``21:00-09:00``.
_RANGE_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")


@bp.route("/", methods=["GET", "POST"])
@login_required
def index() -> Any:
    """Все настройки одной формой."""
    store = AdminStore(config().admin_db)
    try:
        if request.method == "POST":
            # Выключенные галочки браузер не присылает, поэтому неполная форма
            # выглядит как «владелец выключил всё сразу»: напоминания, уведомления
            # о заявках и бесплатное пробное. Скрытое поле подтверждает полноту.
            if not flag(request.form, "form_complete"):
                flash(
                    "Форма пришла не полностью — ничего не изменено. "
                    "Откройте страницу заново и сохраните ещё раз.",
                    "error",
                )
                return redirect(url_for("settings.index"))

            problems: list[str] = []
            changed = 0
            for spec in SETTING_SPECS:
                if spec.kind == "bool":
                    value = "on" if flag(request.form, spec.key) else "off"
                elif spec.kind == "minutes":
                    minutes = integer(request.form, spec.key, default=0)
                    if not 1 <= minutes <= 1440:
                        problems.append(
                            f"{spec.title}: нужно число минут от 1 до 1440, получено «"
                            f"{text(request.form, spec.key)}»"
                        )
                        continue
                    value = str(minutes)
                else:
                    value = text(request.form, spec.key).replace(" ", "").replace("—", "-")
                    if not _RANGE_RE.match(value):
                        problems.append(
                            f"{spec.title}: нужен формат 21:00-09:00, получено «{value}»"
                        )
                        continue
                if store.get(spec.key) != value:
                    store.set(spec.key, value)
                    changed += 1

            for problem in problems:
                flash(problem, "error")
            if changed and not problems:
                flash("Настройки сохранены. Бот применит их со следующего сообщения.", "ok")
            elif not changed and not problems:
                flash("Ничего не изменилось.", "detail")
            return redirect(url_for("settings.index"))

        return render_template(
            "settings.html",
            settings=store.all_settings(),
            runtime=load_runtime_settings(config().admin_db),
            admins=store.admins(),
        )
    finally:
        store.close()


@bp.route("/admins", methods=["GET", "POST"])
@login_required
def admins() -> Any:
    """Кому приходят заявки и кто может править бота из Telegram."""
    store = AdminStore(config().admin_db)
    try:
        if request.method == "POST":
            action = text(request.form, "action")
            telegram_id = integer(request.form, "telegram_id", default=0)
            if not telegram_id:
                flash("Нужен числовой Telegram-id.", "error")
            elif action == "remove":
                if not store.is_admin(telegram_id):
                    flash(f"{telegram_id} и так не администратор.", "detail")
                elif store.revoke(telegram_id):
                    flash(f"Права у {telegram_id} сняты.", "ok")
                else:
                    # Единственного администратора убрать нельзя: иначе управление
                    # ботом теряется, и вернуть его можно будет только паролем.
                    flash("Нельзя убрать последнего администратора.", "error")
            elif store.grant(telegram_id, title=text(request.form, "title")):
                flash(f"{telegram_id} теперь администратор.", "ok")
            else:
                flash("У этого пользователя уже есть права.", "detail")
            return redirect(url_for("settings.admins"))

        return render_template("admins.html", admins=store.admins(), password=config().password)
    finally:
        store.close()
