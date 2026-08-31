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
            return redirect(_safe_next(request.args.get("next")))

        login_failed(ip)
        flash("Пароль не подошёл.", "error")

    return render_template("login.html")


def _safe_next(target: str | None) -> str:
    """Куда вернуть человека после входа.

    Две проверки. Первая — открытый редирект: адрес из параметра берём только
    если он ведёт на этот же сайт. Вторая — префикс монтирования: в бою CRM
    живёт на ``/crm``, а Flask отдаёт пути уже без него, и без ``script_root``
    человек после входа улетал бы в корень домена.
    """
    candidate = (target or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return url_for("dashboard.index")
    root = request.script_root or ""
    if root and not candidate.startswith(f"{root}/") and candidate != root:
        candidate = f"{root}{candidate}"
    return candidate


@bp.route("/logout")
def logout() -> Any:
    """Выход."""
    session.clear()
    return redirect(url_for("auth.login"))
