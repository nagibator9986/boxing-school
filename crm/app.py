"""Сборка Flask-приложения CRM: маршруты, вход, фильтры шаблонов.

Приложение синхронное и однопроцессное — так и задумано. Нагрузка здесь равна
одному-двум людям, которые открывают страницы руками, а вся сложность лежит в
данных, а не в конкурентности. Зато при правке базы знаний в один момент времени
пишет ровно один процесс, и гонки за файлами YAML не существует.
"""

from __future__ import annotations

import functools
import os
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from flask import Flask, Response, current_app, g, redirect, request, session, url_for

from app.logging_conf import get_logger
from app.types import KBValidationError
from crm.botdb import BotData
from crm.config import CrmConfig
from crm.kbio import KBEditor

__all__ = ["config", "create_app", "data", "editor", "login_required"]

_log = get_logger(__name__)

#: Сколько неудачных попыток пароля терпим с одного адреса и на сколько запираем.
LOGIN_ATTEMPTS: int = 7
LOGIN_LOCK_SECONDS: int = 300

#: Счётчик попыток входа. В памяти процесса: CRM одна, а переживать перезапуск
#: блокировке незачем — перезапуск делает владелец, а не подбирающий пароль.
_ATTEMPTS: dict[str, tuple[int, float]] = {}


# --------------------------------------------------------------------------- #
# Доступ к общим объектам
# --------------------------------------------------------------------------- #
def config() -> CrmConfig:
    """Конфигурация текущего приложения."""
    return current_app.config["CRM"]


def editor() -> KBEditor:
    """Редактор базы знаний. Один на запрос."""
    if "kb_editor" not in g:
        cfg = config()
        g.kb_editor = KBEditor(
            cfg.kb_dir,
            media_dir=cfg.media_dir,
            schema_version=cfg.schema_version,
            tz=cfg.timezone,
        )
    return g.kb_editor


def data() -> BotData:
    """Доступ к базе диалогов. Один на запрос."""
    if "bot_data" not in g:
        cfg = config()
        g.bot_data = BotData(cfg.bot_db, state_db=cfg.state_db, tz=cfg.timezone)
    return g.bot_data


def snapshot() -> Any:
    """Снимок базы знаний для текущего запроса.

    Читается один раз на запрос: страница залов трогает его в пяти местах, а
    разбор семи YAML на каждое обращение — заметная задержка.
    """
    if "kb_snapshot" not in g:
        g.kb_snapshot = editor().snapshot()
    return g.kb_snapshot


def same_origin(req: Any) -> bool:
    """Пришёл ли POST с нашей же страницы.

    Защита от запроса, подсунутого чужим сайтом: браузер отправит форму на наш
    адрес вместе с сессионной кукой, и без проверки чужая страница смогла бы
    менять цены и снимать администраторов. Куки уже помечены ``SameSite=Lax``,
    но полагаться на одну меру нельзя — заголовок проверяем сами.

    Запрос без ``Origin`` и ``Referer`` считается своим: их нет у curl и у части
    корпоративных прокси, а подделать заголовок из браузера нельзя.
    """
    source = req.headers.get("Origin") or req.headers.get("Referer")
    if not source:
        return True
    host = urlparse(source).netloc
    return bool(host) and host == req.host


# --------------------------------------------------------------------------- #
# Вход
# --------------------------------------------------------------------------- #
def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """Пускает дальше только вошедших. Иначе — на страницу входа."""

    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not session.get("authenticated"):
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


def login_blocked(ip: str) -> int:
    """Сколько секунд осталось до конца блокировки. ``0`` — не заблокирован."""
    attempts, until = _ATTEMPTS.get(ip, (0, 0.0))
    if attempts < LOGIN_ATTEMPTS:
        return 0
    left = int(until - time.monotonic())
    if left <= 0:
        _ATTEMPTS.pop(ip, None)
        return 0
    return left


def login_failed(ip: str) -> None:
    """Отмечает неудачную попытку и при переборе включает паузу."""
    attempts, _ = _ATTEMPTS.get(ip, (0, 0.0))
    _ATTEMPTS[ip] = (attempts + 1, time.monotonic() + LOGIN_LOCK_SECONDS)


def login_succeeded(ip: str) -> None:
    """Сбрасывает счётчик после верного пароля."""
    _ATTEMPTS.pop(ip, None)


# --------------------------------------------------------------------------- #
# Фильтры шаблонов
# --------------------------------------------------------------------------- #
def _fmt_dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Дата и время в привычном виде. ``None`` — прочерк, а не пустота."""
    return value.strftime(fmt) if isinstance(value, datetime) else "—"


def _fmt_ago(value: datetime | None) -> str:
    """«2 часа назад» — так быстрее понять, живой диалог или прошлогодний."""
    if not isinstance(value, datetime):
        return "—"
    delta = datetime.now(tz=value.tzinfo) - value
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "только что"
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    if seconds < 86400:
        return f"{seconds // 3600} ч назад"
    if seconds < 86400 * 30:
        return f"{seconds // 86400} дн назад"
    return value.strftime("%d.%m.%Y")


def _fmt_money(value: Any) -> str:
    """Тенге с разделителем тысяч: 25 000 ₸."""
    try:
        return f"{int(value):,}".replace(",", " ") + " ₸"
    except (TypeError, ValueError):
        return "—"


def _yesno(value: Any) -> str:
    """Булево значение словом."""
    return "да" if value else "нет"


def _kb_state(cfg: CrmConfig) -> str:
    """Короткая версия базы знаний либо ``invalid`` — для проверки живости."""
    try:
        return KBEditor(
            cfg.kb_dir, media_dir=cfg.media_dir, schema_version=cfg.schema_version
        ).snapshot().kb_hash[:12]
    except Exception:  # noqa: BLE001 - для healthz важен факт, а не причина
        return "invalid"


# --------------------------------------------------------------------------- #
# Фабрика
# --------------------------------------------------------------------------- #
def create_app(cfg: CrmConfig | None = None) -> Flask:
    """Собирает приложение. ``cfg`` подменяется в тестах."""
    app = Flask(__name__, template_folder="templates", static_folder="static")
    resolved = cfg or CrmConfig.from_env()
    app.config["CRM"] = resolved
    app.secret_key = resolved.secret_key
    # Куки сессии не должны уезжать на сторонние сайты и в JavaScript.
    # За прокси Railway приложение видит http, поэтому «только по https» решаем
    # по окружению, а не по схеме запроса: иначе кука сессии уедет открытым текстом.
    behind_https = os.environ.get("CRM_HTTPS", "").strip().lower() in ("1", "true", "yes") or bool(
        os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    )
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=behind_https,
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,  # видео из телефона бывает крупным
        TEMPLATES_AUTO_RELOAD=True,
    )
    if behind_https and not os.environ.get("CRM_SECRET_KEY", "").strip():
        # Случайный ключ на процесс означает, что каждый перезапуск разлогинивает
        # всех. На своём ноутбуке это мелочь, на Railway — постоянная помеха.
        _log.warning(
            "crm_secret_key_missing",
            hint="задайте CRM_SECRET_KEY, иначе вход слетает при каждом перезапуске",
        )
    if behind_https and resolved.password in ("", "Azamat65"):
        _log.warning(
            "crm_default_password",
            hint="CRM открыта в интернете с паролем по умолчанию — смените ADMIN_PASSWORD",
        )

    app.jinja_env.filters["dt"] = _fmt_dt
    app.jinja_env.filters["ago"] = _fmt_ago
    app.jinja_env.filters["money"] = _fmt_money
    app.jinja_env.filters["yesno"] = _yesno

    from crm.views import auth, clients, dashboard, kb_content, kb_gyms, leads, settings

    app.register_blueprint(auth.bp)
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(clients.bp)
    app.register_blueprint(leads.bp)
    app.register_blueprint(kb_gyms.bp)
    app.register_blueprint(kb_content.bp)
    app.register_blueprint(settings.bp)

    @app.before_request
    def _guard_origin() -> Any:
        """Отклоняет изменяющие запросы, пришедшие с чужой страницы."""
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not same_origin(request):
            _log.warning("crm_cross_origin_blocked", origin=request.headers.get("Origin"))
            return Response("Запрос пришёл с чужой страницы.", status=403)
        return None

    @app.get("/healthz")
    def _healthz() -> Any:
        """Проверка живости для Railway. Без пароля: пароля у проверяльщика нет."""
        return {"status": "ok", "kb": _kb_state(resolved)}, 200

    @app.context_processor
    def _globals() -> dict[str, Any]:
        """То, что нужно каждой странице: версия базы знаний и состояние бота."""
        payload: dict[str, Any] = {"cfg": resolved}
        if session.get("authenticated"):
            try:
                payload["kb_hash"] = snapshot().kb_hash[:8]
            except Exception:  # noqa: BLE001 - база знаний сломана руками
                payload["kb_hash"] = "ошибка"
        return payload

    @app.errorhandler(404)
    def _not_found(_: Any) -> tuple[str, int]:
        from flask import render_template

        return render_template("error.html", code=404, message="Такой страницы нет"), 404

    @app.errorhandler(413)
    def _too_large(_: Any) -> tuple[str, int]:
        """Файл больше предела. Без обработчика это была бы страница с трассировкой."""
        from flask import render_template

        limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return (
            render_template(
                "error.html",
                code=413,
                message=f"Файл больше {limit} МБ. Сожмите видео и попробуйте ещё раз.",
            ),
            413,
        )

    @app.errorhandler(KBValidationError)
    def _kb_broken(exc: KBValidationError) -> tuple[str, int]:
        """База знаний не читается — показываем, что именно сломано.

        Так бывает, когда YAML правили в обход CRM. Страница с трассировкой тут
        бесполезна: человеку нужно знать, в каком файле и в какой строке ошибка.
        """
        from flask import render_template

        return (
            render_template(
                "error.html",
                code=500,
                message="База знаний не читается. Бот продолжает отвечать по прежней версии.",
                errors=list(exc.errors)[:20],
            ),
            500,
        )

    @app.errorhandler(500)
    def _failed(exc: Any) -> tuple[str, int]:  # pragma: no cover - защитная сетка
        from flask import render_template

        app.logger.exception("crm_error")
        return render_template("error.html", code=500, message=str(exc)), 500

    return app
