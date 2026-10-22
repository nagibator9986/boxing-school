"""Возврат клиентов, этап 1: список из разбора старых переписок и проверка исхода.

Отправки здесь нет. Раздел нужен, чтобы до первого сообщения человек отметил, кто уже
занимается, кто был на пробном и не купил, кто не дошёл, — и чтобы список сверился с
перепиской самого бота.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.campaign.contacts import CampaignOutcome, CampaignStatus
from app.logging_conf import get_logger
from crm.app import config, login_required
from crm.campaign_excel import parse_workbook
from crm.campaign_store import CampaignStore
from crm.forms import choice, opt_text, text

bp = Blueprint("campaign", __name__, url_prefix="/campaign")

_log = get_logger(__name__)

#: Вкладки списка: название и статусы, которые в ней видны.
TABS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "check": ("Проверить исход", (CampaignStatus.NEEDS_CHECK.value,)),
    "ready": ("Можно писать", (CampaignStatus.READY.value,)),
    "closed": ("Не пишем", (CampaignStatus.EXCLUDED.value, CampaignStatus.OPTED_OUT.value)),
}

SEGMENT_TITLES: Final[dict[str, str]] = {
    "faded": "Интерес затих до записи",
    "no_reply": "Школа не ответила",
    "booked_unknown": "Записан — дошёл ли, неизвестно",
    "after_trial": "Был на пробном — купил ли, неизвестно",
    "no_show": "Не пришёл на пробное",
    "invoice": "Просил счёт — оплата неизвестна",
    "later": "Просил написать позже",
    "former": "Занимался раньше",
    "live": "Разговор идёт сейчас",
    "other_program": "Другая программа",
    "current_client": "Уже занимается",
    "refused": "Отказался",
    "not_client": "Не клиент",
    "unknown": "Этап не распознан",
}

STATUS_TITLES: Final[dict[str, str]] = {
    "needs_check": "Проверить",
    "ready": "Можно писать",
    "draft": "Черновик",
    "approved": "Одобрен",
    "sent": "Отправлено",
    "replied": "Ответил",
    "booked": "Записался",
    "declined": "Отказ",
    "no_answer": "Не ответил",
    "opted_out": "Стоп",
    "excluded": "Не пишем",
}

#: Кнопки проверки — в том порядке, в каком их чаще нажимают.
OUTCOME_TITLES: Final[dict[str, str]] = {
    CampaignOutcome.BOUGHT.value: "Купил / занимается",
    CampaignOutcome.CAME.value: "Был, не купил",
    CampaignOutcome.NO_SHOW.value: "Не пришёл",
    CampaignOutcome.WRITE.value: "Писать можно",
    CampaignOutcome.REFUSED.value: "Отказ",
    CampaignOutcome.NOT_CLIENT.value: "Не клиент",
    CampaignOutcome.LATER.value: "Позже",
}


def _store() -> CampaignStore:
    cfg = config()
    return CampaignStore(cfg.bot_db, tz=cfg.timezone, silent_numbers=_silent_numbers())


def _silent_numbers() -> frozenset[str]:
    """Номера, которым бот не пишет: сотрудники, номер для заявок, бухгалтер, рабочие чаты."""
    try:
        from app.admin.runtime_settings import load_runtime_settings
        from app.config import get_settings
        from app.core.ignore_list import silent_numbers

        runtime = load_runtime_settings(config().admin_db)
        return silent_numbers(runtime.apply_to(get_settings()), runtime)
    except Exception as exc:  # noqa: BLE001 - без списка сверка всё равно работает
        _log.warning("campaign_silent_numbers_unavailable", error=type(exc).__name__)
        return frozenset()


def _now() -> datetime:
    return datetime.now(tz=UTC)


@bp.route("/")
@login_required
def index() -> Any:
    """Список с вкладками и фильтром по сегменту."""
    store = _store()
    tab = choice(request.args, "tab", TABS, default="check")
    segment = text(request.args, "segment")
    statuses = TABS[tab][1]
    counts = store.counts()
    return render_template(
        "campaign.html",
        ready=store.ready,
        tab=tab,
        tabs={key: (title, sum(counts.get(status, 0) for status in keys)) for key, (title, keys) in TABS.items()},
        total=sum(counts.values()),
        counts=counts,
        segment=segment,
        segments=store.segment_counts(statuses=statuses),
        contacts=store.contacts(statuses=statuses, segment=segment),
        segment_titles=SEGMENT_TITLES,
        status_titles=STATUS_TITLES,
        outcome_titles=OUTCOME_TITLES,
    )


@bp.route("/import", methods=["POST"])
@login_required
def upload() -> Any:
    """Загрузка Excel разбора. Файл не сохраняется: из него берутся только нужные поля."""
    store = _store()
    if not store.ready:
        flash("Таблица возврата появится после перезапуска бота — тогда загрузите файл ещё раз.", "error")
        return redirect(url_for("campaign.index"))
    upload_file = request.files.get("file")
    if upload_file is None or not upload_file.filename:
        flash("Файл не выбран.", "error")
        return redirect(url_for("campaign.index"))
    if Path(upload_file.filename).suffix.lower() != ".xlsx":
        flash("Нужен файл Excel (.xlsx) из разбора переписок.", "error")
        return redirect(url_for("campaign.index"))
    parsed = parse_workbook(io.BytesIO(upload_file.read()))
    if not parsed.rows:
        flash(" ".join(parsed.errors) or "В файле нет ни одного контакта.", "error")
        return redirect(url_for("campaign.index"))
    now = _now()
    report = store.import_rows(parsed.rows, batch=f"{upload_file.filename} · {now:%d.%m.%Y}", now=now)
    message = (
        f"Лист «{parsed.sheet}»: новых {report.added}, обновлено {report.updated}, "
        f"без смены статуса (уже проверены или им писали) {report.kept}. "
        f"По переписке бота изменено: {report.reconciled}."
    )
    if parsed.duplicates:
        message += f" Повторы номеров в файле пропущены: {parsed.duplicates}."
    if parsed.errors:
        message += f" Не разобрано строк: {len(parsed.errors)}."
    flash(message, "ok")
    _log.info("campaign_import", added=report.added, updated=report.updated, kept=report.kept,
              reconciled=report.reconciled, skipped=len(parsed.errors))
    return redirect(url_for("campaign.index"))


@bp.route("/reconcile", methods=["POST"])
@login_required
def recheck() -> Any:
    """Сверить список с перепиской бота заново."""
    changed = _store().reconcile_all(now=_now())
    flash(f"Сверено с перепиской бота. Изменилось: {changed}.", "ok")
    return redirect(url_for("campaign.index", tab=text(request.form, "tab") or "check"))


@bp.route("/<contact_id>")
@login_required
def show(contact_id: str) -> Any:
    """Карточка контакта: итог переписки, что проверить, отметка исхода."""
    contact = _store().contact(contact_id)
    if contact is None:
        abort(404)
    return render_template(
        "campaign_contact.html",
        contact=contact,
        segment_titles=SEGMENT_TITLES,
        status_titles=STATUS_TITLES,
        outcome_titles=OUTCOME_TITLES,
    )


@bp.route("/<contact_id>/outcome", methods=["POST"])
@login_required
def outcome(contact_id: str) -> Any:
    """Отметка исхода — из списка одной кнопкой или из карточки с датой и заметкой."""
    value = choice(request.form, "outcome", OUTCOME_TITLES, default="")
    back = _back(contact_id)
    if not value:
        flash("Не выбран исход.", "error")
        return back
    result = CampaignOutcome(value)
    not_before = _local_date(text(request.form, "not_before"))
    if result is CampaignOutcome.LATER and not_before is None:
        flash("Для «Позже» укажите дату — с неё можно будет писать.", "error")
        return redirect(url_for("campaign.show", contact_id=contact_id))
    if not _store().set_outcome(
        contact_id, result, now=_now(), not_before=not_before, note=opt_text(request.form, "note")
    ):
        flash("Этот контакт уже не на проверке — отметку не поставить.", "error")
        return back
    flash(f"Отмечено: {OUTCOME_TITLES[value]}.", "ok")
    return back


@bp.route("/<contact_id>/restore", methods=["POST"])
@login_required
def restore(contact_id: str) -> Any:
    """Вернуть контакт на проверку."""
    if _store().restore(contact_id, now=_now()):
        flash("Контакт возвращён на проверку.", "ok")
    else:
        flash("Этот контакт вернуть на проверку нельзя.", "error")
    return _back(contact_id)


def _back(contact_id: str) -> Any:
    """Назад туда, откуда нажали: в карточку или в ту же вкладку списка."""
    if text(request.form, "back") == "list":
        return redirect(
            url_for("campaign.index", tab=text(request.form, "tab") or "check",
                    segment=text(request.form, "segment") or None)
        )
    return redirect(url_for("campaign.show", contact_id=contact_id))


def _local_date(value: str) -> datetime | None:
    """«2026-11-13» из поля даты → начало этого дня по часам школы, в UTC."""
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    try:
        zone = ZoneInfo(config().timezone)
    except Exception:  # noqa: BLE001 - кривое имя зоны не повод падать
        zone = ZoneInfo("Asia/Almaty")
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
