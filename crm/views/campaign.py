"""Возврат клиентов: список из разбора старых переписок, проверка, черновики, итоги.

Путь контакта: человек отмечает исход (кто уже занимается, кто был на пробном и не
купил, кто не дошёл) → бот пишет черновик первого сообщения → человек правит и
одобряет → бот отправляет по одному, в часы рассылки, и дальше ведёт разговор сам →
статус по переписке (ответил, записался, купил, стоп) или по отметке человека.
Отправляет воркер бота (``app/workers/tasks_campaign.py``); здесь — решения человека.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.campaign.contacts import CampaignOutcome, CampaignResult, CampaignStatus
from app.logging_conf import get_logger
from crm.app import config, login_required
from crm.campaign_excel import IMPORTABLE_SHEETS, PREFERRED_SHEET, parse_workbook
from crm.campaign_store import MAX_MESSAGE_CHARS, CampaignStore, ContactRow, StoreBusy
from crm.forms import choice, opt_text, text

bp = Blueprint("campaign", __name__, url_prefix="/campaign")

_log = get_logger(__name__)

#: Вкладки списка: название и статусы, которые в ней видны.
TABS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "check": ("Проверить исход", (CampaignStatus.NEEDS_CHECK.value,)),
    "drafts": ("Черновики", (CampaignStatus.READY.value, CampaignStatus.DRAFT.value)),
    "approved": ("Одобрены", (CampaignStatus.APPROVED.value,)),
    "sent": (
        "Отправлены",
        (
            CampaignStatus.SENT.value,
            CampaignStatus.REPLIED.value,
            CampaignStatus.BOOKED.value,
            CampaignStatus.BOUGHT.value,
            CampaignStatus.NO_ANSWER.value,
            CampaignStatus.DECLINED.value,
            CampaignStatus.UNDELIVERED.value,
        ),
    ),
    "closed": ("Не пишем", (CampaignStatus.EXCLUDED.value, CampaignStatus.OPTED_OUT.value)),
}

#: Прежнее имя вкладки «Можно писать» — ссылки и закладки на него ведут в черновики.
_TAB_ALIASES: Final[dict[str, str]] = {"ready": "drafts"}

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
    "unclear": "Не хватает контекста (голосовые)",
    "unknown": "Этап не распознан",
}

STATUS_TITLES: Final[dict[str, str]] = {
    "needs_check": "Проверить",
    "ready": "Черновик пишется",
    "draft": "Черновик",
    "approved": "Одобрен",
    "sent": "Отправлено",
    "replied": "Ответил",
    "booked": "Записался",
    "bought": "Купил",
    "declined": "Отказ",
    "no_answer": "Не ответил",
    "opted_out": "Стоп",
    "undelivered": "Не доставлено",
    "excluded": "Не пишем",
}

#: Цвет метки статуса: хорошо, ждём, плохо.
STATUS_TONES: Final[dict[str, str]] = {
    "replied": "ok", "booked": "ok", "bought": "ok", "approved": "ok",
    "declined": "bad", "opted_out": "bad", "undelivered": "bad", "excluded": "bad", "no_answer": "bad",
}

#: Итог после сообщения — кнопки во вкладке «Отправлены».
RESULT_TITLES: Final[dict[str, str]] = {
    CampaignResult.BOOKED.value: "Записался",
    CampaignResult.BOUGHT.value: "Купил",
    CampaignResult.DECLINED.value: "Отказ",
}

#: Что сказать, когда база бота занята записью: бот пишет в тот же файл.
BUSY: Final[str] = "База бота сейчас занята — повторите через минуту. Ничего не потеряно."

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
    """Список с вкладками, фильтром по сегменту и пультом рассылки."""
    store = _store()
    raw_tab = text(request.args, "tab")
    tab = choice({"tab": _TAB_ALIASES.get(raw_tab, raw_tab)}, "tab", TABS, default="check")
    segment = text(request.args, "segment")
    statuses = TABS[tab][1]
    funnel = store.funnel(since=_day_start())
    counts = funnel.statuses
    return render_template(
        "campaign.html",
        ready=store.ready,
        tab=tab,
        tabs={key: (title, sum(counts.get(status, 0) for status in keys)) for key, (title, keys) in TABS.items()},
        total=funnel.total,
        counts=counts,
        funnel=funnel,
        pult=_pult(),
        segment=segment,
        segments=store.segment_counts(statuses=statuses),
        contacts=store.contacts(statuses=statuses, segment=segment),
        segment_titles=SEGMENT_TITLES,
        status_titles=STATUS_TITLES,
        status_tones=STATUS_TONES,
        outcome_titles=OUTCOME_TITLES,
        result_titles=RESULT_TITLES,
        max_chars=MAX_MESSAGE_CHARS,
        sheets=IMPORTABLE_SHEETS,
        drafts_to_rewrite=len(store.drafts_to_rewrite()) if tab == "drafts" else 0,
    )


def _pult() -> dict[str, Any]:
    """Состояние рассылки для пульта: включена ли, лимит, часы и что ей мешает."""
    from app.admin.runtime_settings import load_runtime_settings

    runtime = load_runtime_settings(config().admin_db)
    start, end = runtime.campaign_hours
    problems: list[str] = []
    if not runtime.bot_enabled:
        problems.append(
            "Бот выключен («Бот отвечает клиентам» в настройках): ответы клиентов вести некому, "
            "поэтому рассылка не идёт."
        )
    if runtime.campaign_daily_limit <= 0:
        problems.append("Сообщений в день — 0: рассылка ничего не отправит.")
    if not _templates_ready():
        problems.append(
            "В базе знаний нет текстов возврата (группа campaign в «Системных фразах»): черновики не пишутся."
        )
    problems.extend(_model_problems())
    return {
        "enabled": runtime.campaign_enabled,
        "bot_enabled": runtime.bot_enabled,
        "limit": runtime.campaign_daily_limit,
        "hours": f"{start // 60:02d}:{start % 60:02d}–{end // 60:02d}:{end % 60:02d}",
        "problems": problems,
    }


def _model_problems() -> list[str]:
    """Модель не отвечает — тому, кто ответит на рассылку, бот ответит карточками и «сбоем»."""
    try:
        from crm.app import data
        from crm.health import _model_issues

        issues = _model_issues(data())
    except Exception as exc:  # noqa: BLE001 - пульт не имеет права падать из-за проверки
        _log.warning("campaign_model_check_failed", error=type(exc).__name__)
        return []
    return [
        f"{issue.title}. Клиенты, которые ответят на рассылку, получат карточки вместо разговора, а черновики "
        f"пишутся по шаблону. {issue.detail}"
        for issue in issues
    ]


def _templates_ready() -> bool:
    """Есть ли в базе знаний тексты первых сообщений.

    Достаточно найти ключ в файле: разбирать и проверять всю базу на каждое открытие
    страницы незачем — сломанную базу показывает «Обзор».
    """
    try:
        return "campaign.first_faded:" in (config().kb_dir / "i18n.yaml").read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("campaign_kb_unavailable", error=type(exc).__name__)
        return False


@bp.route("/switch", methods=["POST"])
@login_required
def switch() -> Any:
    """Включить или выключить рассылку — та же настройка, что в «Настройках бота»."""
    from app.admin.admin_store import AdminStore

    turn_on = text(request.form, "state") == "on"
    store = AdminStore(config().admin_db)
    try:
        store.set("campaign_enabled", "on" if turn_on else "off")
        bot_on = store.get("bot_enabled") in ("on", "true", "1")
    finally:
        store.close()
    if turn_on and not bot_on:
        flash("Рассылка включена, но бот выключен — сообщения не пойдут, пока его не включат в настройках.", "error")
    elif turn_on:
        flash("Рассылка включена. Одобренные сообщения уйдут по одному, в часы рассылки.", "ok")
    else:
        flash("Рассылка выключена. Ничего не уходит; черновики и одобрения сохранены.", "ok")
    _log.info("campaign_switched", enabled=turn_on)
    return redirect(url_for("campaign.index", tab=text(request.form, "tab") or "check"))


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
    sheet = choice(request.form, "sheet", IMPORTABLE_SHEETS, default=PREFERRED_SHEET)
    # Лист по умолчанию — с прежней поблажкой: нет его в файле — первый лист с нужными колонками.
    parsed = parse_workbook(io.BytesIO(upload_file.read()), sheet=None if sheet == PREFERRED_SHEET else sheet)
    if not parsed.rows:
        flash(" ".join(parsed.errors) or "В файле нет ни одного контакта.", "error")
        return redirect(url_for("campaign.index"))
    now = _now()
    try:
        report = store.import_rows(parsed.rows, batch=f"{upload_file.filename} · {now:%d.%m.%Y}", now=now)
    except StoreBusy:
        flash(BUSY, "error")
        return redirect(url_for("campaign.index"))
    message = (
        f"Лист «{parsed.sheet}»: новых {report.added}, обновлено {report.updated}, "
        f"без смены статуса (проверены вами, просили не писать или им уже писали) {report.kept}. "
        f"Поправлено по переписке бота (записан, стоп, свежий диалог): {report.reconciled}."
    )
    if parsed.duplicates:
        message += f" Повторы номеров в файле пропущены: {parsed.duplicates}."
    if parsed.errors:
        # Какие именно строки — иначе их не найти в файле на двести строк.
        shown = " ".join(parsed.errors[:3]) + (" …" if len(parsed.errors) > 3 else "")
        message += f" Не разобрано строк: {len(parsed.errors)}. {shown}"
    flash(message, "ok")
    _log.info("campaign_import", added=report.added, updated=report.updated, kept=report.kept,
              reconciled=report.reconciled, skipped=len(parsed.errors))
    return redirect(url_for("campaign.index"))


@bp.route("/reconcile", methods=["POST"])
@login_required
def recheck() -> Any:
    """Сверить список с перепиской бота заново."""
    try:
        changed = _store().reconcile_all(now=_now())
    except StoreBusy:
        flash(BUSY, "error")
    else:
        flash(f"Сверено с перепиской бота. Изменилось: {changed}.", "ok")
    return redirect(url_for("campaign.index", tab=text(request.form, "tab") or "check"))


@bp.route("/approve", methods=["POST"])
@login_required
def approve_all() -> Any:
    """Одобрить черновики со страницы как есть."""
    ids = request.form.getlist("ids")
    try:
        approved = _store().approve_many(ids, now=_now())
    except StoreBusy:
        flash(BUSY, "error")
    else:
        skipped = len(set(ids)) - approved
        message = f"Одобрено: {approved}."
        if skipped:
            message += f" Не одобрено {skipped}: их уже отправили, вернули на проверку или бот увидел запрет."
        flash(message, "ok" if approved else "error")
    return redirect(url_for("campaign.index", tab="drafts", segment=text(request.form, "segment") or None))


@bp.route("/retry", methods=["POST"])
@login_required
def retry() -> Any:
    """Отправить недоставленные заново: одного (``contact_id``) или всех сразу."""
    store = _store()
    one = text(request.form, "contact_id")
    ids = [one] if one else store.undelivered()
    try:
        changed = store.retry(ids, now=_now())
    except StoreBusy:
        flash(BUSY, "error")
        return _back(one) if one else redirect(url_for("campaign.index", tab="sent"))
    if not changed:
        flash("Отправлять заново некого: недоставленных нет или у них нет одобренного текста.", "error")
    else:
        on = _pult()["enabled"]
        flash(
            f"Отправятся заново: {changed}, по одному с паузой."
            + ("" if on else " Рассылка сейчас выключена — включите её, и сообщения пойдут.")
            + " Если вы перед этим писали клиенту с номера бота, бот сам вернётся в диалог.",
            "ok" if on else "error",
        )
    return _back(one) if one else redirect(url_for("campaign.index", tab="sent"))


@bp.route("/redraft-all", methods=["POST"])
@login_required
def redraft_all() -> Any:
    """Переписать все черновики, которые человек не правил: после смены правил или текстов."""
    store = _store()
    try:
        changed = store.redraft(store.drafts_to_rewrite(), now=_now())
    except StoreBusy:
        flash(BUSY, "error")
    else:
        flash(f"Пишутся заново: {changed}. Бот напишет их в ближайшие минуты, по три в минуту.", "ok")
    return redirect(url_for("campaign.index", tab="drafts"))


@bp.route("/redraft-templates", methods=["POST"])
@login_required
def redraft_templates() -> Any:
    """Переписать моделью все черновики по шаблону — когда модель снова отвечает."""
    store = _store()
    try:
        changed = store.redraft(store.template_drafts(), now=_now())
    except StoreBusy:
        flash(BUSY, "error")
    else:
        flash(f"Пишутся заново: {changed}. Бот возьмётся за них в ближайшие минуты.", "ok")
    return redirect(url_for("campaign.index", tab="drafts"))


@bp.route("/<contact_id>")
@login_required
def show(contact_id: str) -> Any:
    """Карточка контакта: итог переписки, проверка, сообщение, итог."""
    contact = _store().contact(contact_id)
    if contact is None:
        abort(404)
    return render_template(
        "campaign_contact.html",
        contact=contact,
        tab=_tab_of(contact.status),
        segment_titles=SEGMENT_TITLES,
        status_titles=STATUS_TITLES,
        status_tones=STATUS_TONES,
        outcome_titles=OUTCOME_TITLES,
        result_titles=RESULT_TITLES,
        max_chars=MAX_MESSAGE_CHARS,
    )


@bp.route("/<contact_id>/message", methods=["POST"])
@login_required
def message(contact_id: str) -> Any:
    """Черновик: сохранить правку, одобрить, переписать заново, вернуть из одобренных."""
    action = choice(request.form, "action", ("save", "approve", "redraft", "unapprove"), default="")
    back = _back(contact_id)
    store = _store()
    now = _now()
    try:
        if action == "redraft":
            if store.redraft([contact_id], now=now):
                flash("Черновик пишется заново — бот напишет его в ближайшие минуты.", "ok")
            else:
                flash("Этот черновик уже не переписать: сообщение отправлено или контакт закрыт.", "error")
            return back
        if action == "unapprove":
            if store.unapprove(contact_id, now=now):
                flash("Возвращено в черновики — сообщение не уйдёт, пока его снова не одобрят.", "ok")
            else:
                flash("Вернуть нельзя: сообщение уже отправлено или контакт не одобрен.", "error")
            return back
        body = text(request.form, "text").strip()
        if not body:
            flash("Пустое сообщение отправлять нечего.", "error")
            return back
        if len(body) > MAX_MESSAGE_CHARS:
            flash(f"Слишком длинно: {len(body)} знаков, можно до {MAX_MESSAGE_CHARS}.", "error")
            return back
        if action == "approve":
            saved = store.approve(contact_id, body, now=now)
            if saved is None:
                flash("Одобрить нельзя: черновика нет — сообщение отправлено, контакт закрыт или черновик ещё пишется.", "error")
            else:
                _flash_result("Одобрено — уйдёт по очереди.", saved, expected=CampaignStatus.APPROVED.value)
            return back
        if action == "save":
            if store.save_draft(contact_id, body, now=now) is None:
                flash("Сохранить нельзя: черновика уже нет.", "error")
            else:
                flash("Черновик сохранён.", "ok")
            return back
    except StoreBusy:
        flash(BUSY, "error")
        return back
    flash("Не понятно, что сделать с черновиком.", "error")
    return back


@bp.route("/<contact_id>/result", methods=["POST"])
@login_required
def result(contact_id: str) -> Any:
    """Итог после сообщения по отметке человека: записался, купил, отказ."""
    value = choice(request.form, "result", RESULT_TITLES, default="")
    back = _back(contact_id)
    if not value:
        flash("Не выбран итог.", "error")
        return back
    try:
        saved = _store().set_result(contact_id, CampaignResult(value), now=_now(), note=opt_text(request.form, "note"))
    except StoreBusy:
        flash(BUSY, "error")
        return back
    if saved is None:
        flash("Итог ставится только тем, кому уже написали, — и не после «стоп».", "error")
    else:
        flash(f"Отмечено: {RESULT_TITLES[value]}.", "ok")
    return back


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
    try:
        saved = _store().set_outcome(
            contact_id, result, now=_now(), not_before=not_before, note=opt_text(request.form, "note")
        )
    except StoreBusy:
        flash(BUSY, "error")
        return back
    if saved is None:
        flash("Этот контакт уже не на проверке — отметку не поставить.", "error")
        return back
    _flash_result(f"Отмечено: {OUTCOME_TITLES[value]}.", saved, expected=_expected_status(result))
    return back


@bp.route("/<contact_id>/restore", methods=["POST"])
@login_required
def restore(contact_id: str) -> Any:
    """Вернуть контакт на проверку."""
    try:
        restored = _store().restore(contact_id, now=_now())
    except StoreBusy:
        flash(BUSY, "error")
        return _back(contact_id)
    if restored is None:
        flash("Этот контакт вернуть на проверку нельзя.", "error")
    else:
        _flash_result("Контакт возвращён на проверку.", restored, expected=CampaignStatus.NEEDS_CHECK.value)
    return _back(contact_id)


def _tab_of(status: str) -> str:
    """Вкладка, где виден контакт в этом статусе."""
    return next((key for key, (_, statuses) in TABS.items() if status in statuses), "check")


def _day_start() -> datetime:
    """Полночь по часам школы — с неё считается «отправлено сегодня»."""
    try:
        zone = ZoneInfo(config().timezone)
    except Exception:  # noqa: BLE001 - кривое имя зоны не повод падать
        zone = ZoneInfo("Asia/Almaty")
    return datetime.now(tz=zone).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def _expected_status(outcome: CampaignOutcome) -> str:
    """Какой статус даёт отметка, если бот ей не мешает."""
    closed = (CampaignOutcome.BOUGHT, CampaignOutcome.REFUSED, CampaignOutcome.NOT_CLIENT)
    return (CampaignStatus.EXCLUDED if outcome in closed else CampaignStatus.READY).value


def _flash_result(done: str, contact: ContactRow, *, expected: str) -> None:
    """Отметка сохранена; если переписка бота её перекрыла — сказать почему, а не молчать."""
    if contact.status == expected:
        flash(done, "ok")
        return
    flash(
        f"{done} Но по переписке бота контакт теперь «{STATUS_TITLES.get(contact.status, contact.status)}»: "
        f"{contact.status_reason or 'см. карточку'}.",
        "error",
    )


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
