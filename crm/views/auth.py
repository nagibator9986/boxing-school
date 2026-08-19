"""Вход в CRM по тому же паролю, что открывает ``/admin`` в Telegram."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from app.admin.admin_store import AdminStore
from crm.app import config, login_blocked, login_failed, login_succeeded

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login() -> Any:
    """Страница входа. Пароль сверяется постоянным временем."""
    cfg = config()
    ip = request.remote_addr or "?"

    if request.method == "POST":
        left = login_blocked(ip)
        if left:
            flash(f"Слишком много попыток. Подождите {left // 60 + 1} мин.", "error")
            return render_template("login.html"), 429

        if AdminStore.password_matches(request.form.get("password", ""), cfg.password):
            login_succeeded(ip)
            session["authenticated"] = True
            session.permanent = True
            target = request.args.get("next") or url_for("dashboard.index")
            # Открытый редирект: адрес из параметра берём только если он свой.
            return redirect(target if target.startswith("/") else url_for("dashboard.index"))

        login_failed(ip)
        flash("Пароль не подошёл.", "error")

    return render_template("login.html")


@bp.route("/logout")
def logout() -> Any:
    """Выход."""
    session.clear()
    return redirect(url_for("auth.login"))
