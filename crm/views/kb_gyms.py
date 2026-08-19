"""Залы и расписание: то, что владелец школы правит чаще всего.

Расписание правится двумя способами, и оба нужны:

* **построчно** — когда меняется одно занятие;
* **вставкой текста** — когда расписание переписывают целиком. Администратор и
  так ведёт его в WhatsApp сообщением с эмодзи; тот же текст вставляется сюда
  как есть и разбирается тем же кодом, что и в Telegram-админке
  (:func:`app.admin.schedule_text.parse_schedule_text`). Заставлять человека
  вводить двадцать занятий по одному — верный способ получить незаполненное
  расписание.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.admin.schedule_text import ScheduleParseError, parse_schedule_text
from app.types import GapRef
from crm.app import editor, login_required, snapshot
from crm.forms import bilingual, choice, csv_list, flag, opt_float, opt_integer, opt_text, text
from crm.kbio import KBEditError, merge_into

bp = Blueprint("kb_gyms", __name__, url_prefix="/kb/gyms")

DAYS: tuple[tuple[str, str], ...] = (
    ("mon", "Пн"), ("tue", "Вт"), ("wed", "Ср"), ("thu", "Чт"),
    ("fri", "Пт"), ("sat", "Сб"), ("sun", "Вс"),
)
DISCIPLINES: tuple[tuple[str, str], ...] = (("boxing", "Бокс"), ("kickboxing", "Кикбоксинг"))
SCOPES: tuple[tuple[str, str], ...] = (("city", "Костанай"), ("region", "Область"))
STATUSES: tuple[tuple[str, str], ...] = (
    ("open", "Работает"), ("closed", "Закрыт"), ("unresolved", "Не подтверждён"),
)
#: Сколько пустых строк расписания показывать под заполненными.
BLANK_SLOTS = 3


def _gym_index(document: Any, gym_id: str) -> int:
    """Позиция зала в документе. ``-1`` — такого зала нет."""
    for index, gym in enumerate(document.get("gyms") or []):
        if gym.get("id") == gym_id:
            return index
    return -1


def _read_slots(form: Any) -> list[dict[str, Any]]:
    """Собирает расписание из строк формы, отбрасывая незаполненные заготовки."""
    indices: set[int] = set()
    for key in form:
        parts = key.split("-")
        if len(parts) >= 3 and parts[0] == "slot" and parts[1].isdigit():
            indices.add(int(parts[1]))

    slots: list[dict[str, Any]] = []
    for index in sorted(indices):
        if flag(form, f"slot-{index}-delete"):
            continue
        days = [day for day in form.getlist(f"slot-{index}-days") if day in dict(DAYS)]
        start = text(form, f"slot-{index}-start")
        end = text(form, f"slot-{index}-end")
        if not days or not start or not end:
            continue
        age_from = opt_integer(form, f"slot-{index}-age_from")
        age_to = opt_integer(form, f"slot-{index}-age_to")
        # Возраст указывается парой либо не указывается вовсе — так требует схема,
        # и это осмысленно: «от 7 и до кого угодно» бот не сможет объяснить.
        if (age_from is None) != (age_to is None):
            age_from = age_to = None
        note_ru = opt_text(form, f"slot-{index}-note_ru")
        slots.append(
            {
                "discipline": choice(
                    form, f"slot-{index}-discipline", dict(DISCIPLINES), default="kickboxing"
                ),
                "age_from": age_from,
                "age_to": age_to,
                "days": days,
                "time_start": start,
                "time_end": end,
                "shift": "any",
                "note": {"ru": note_ru, "kk": opt_text(form, f"slot-{index}-note_kk") or note_ru}
                if note_ru
                else None,
            }
        )
    return slots


def _read_coaches(form: Any) -> list[dict[str, Any]]:
    """Тренеры зала из строк формы."""
    indices: set[int] = set()
    for key in form:
        parts = key.split("-")
        if len(parts) >= 3 and parts[0] == "coach" and parts[1].isdigit():
            indices.add(int(parts[1]))
    out: list[dict[str, Any]] = []
    for index in sorted(indices):
        name = text(form, f"coach-{index}-name")
        if not name or flag(form, f"coach-{index}-delete"):
            continue
        out.append(
            {
                "name": name,
                "credentials": opt_text(form, f"coach-{index}-credentials"),
                "groups": csv_list(form, f"coach-{index}-groups"),
                "speaks": [lang for lang in form.getlist(f"coach-{index}-speaks") if lang in ("ru", "kk")],
            }
        )
    return out


def _read_gym(form: Any, *, gym_id: str) -> dict[str, Any]:
    """Полный зал из формы — ровно в том виде, в каком он ляжет в YAML."""
    landmark = bilingual(form, "landmark")
    capacity = bilingual(form, "capacity_note")
    return {
        "id": gym_id,
        "scope": choice(form, "scope", dict(SCOPES), default="city"),
        "settlement": text(form, "settlement") or "Костанай",
        "is_head": flag(form, "is_head"),
        "active": flag(form, "active"),
        "status": choice(form, "status", dict(STATUSES), default="open"),
        "title": bilingual(form, "title"),
        "address": bilingual(form, "address"),
        "landmark": landmark,
        "district": bilingual(form, "district"),
        "district_aliases": csv_list(form, "district_aliases"),
        "geo_lat": opt_float(form, "geo_lat"),
        "geo_lon": opt_float(form, "geo_lon"),
        "map_url": opt_text(form, "map_url"),
        "phone": opt_text(form, "phone"),
        "coaches": _read_coaches(form),
        "schedule": _read_slots(form),
        "capacity_note": capacity if capacity.get("ru") or capacity.get("kk") else None,
        "media": [value for value in form.getlist("media") if value],
        "gap_refs": [value for value in form.getlist("gap_refs") if value],
        "internal_note": opt_text(form, "internal_note"),
    }


# --------------------------------------------------------------------------- #
# Список
# --------------------------------------------------------------------------- #
@bp.route("/")
@login_required
def index() -> Any:
    """Все залы: где есть расписание, где нет, где не хватает данных."""
    kb = snapshot()
    gyms = sorted(kb.gyms.gyms, key=lambda gym: (gym.scope.value != "city", gym.id))
    return render_template("gyms.html", gyms=gyms, city=kb.gyms.city_settlement)


# --------------------------------------------------------------------------- #
# Правка
# --------------------------------------------------------------------------- #
@bp.route("/new", methods=["GET", "POST"])
@bp.route("/<gym_id>", methods=["GET", "POST"])
@login_required
def edit(gym_id: str | None = None) -> Any:
    """Создание и правка зала. Одна форма на оба случая — поля те же."""
    kb_editor = editor()
    document = kb_editor.load("gyms.yaml")
    position = _gym_index(document, gym_id) if gym_id else -1
    if gym_id and position < 0:
        flash(f"Зала «{gym_id}» нет в базе знаний.", "error")
        return redirect(url_for("kb_gyms.index"))

    if request.method == "POST":
        # Форма собирает зал заново, поэтому неполный POST выглядит как «владелец
        # удалил всё расписание и все материалы». Проверено на живом стенде:
        # запрос без полей занятий молча стёр расписание зала, и заметить это
        # можно было только по пустой сетке. Скрытое поле подтверждает, что
        # форма пришла целиком; без него правку не принимаем.
        if not flag(request.form, "form_complete"):
            flash(
                "Форма пришла не полностью — ничего не изменено. "
                "Откройте страницу заново и сохраните ещё раз.",
                "error",
            )
            return redirect(
                url_for("kb_gyms.edit", gym_id=gym_id) if gym_id else url_for("kb_gyms.edit")
            )

        new_id = text(request.form, "id") or (gym_id or "")
        if not new_id:
            flash("Не задан идентификатор зала.", "error")
            return redirect(url_for("kb_gyms.edit", gym_id=gym_id) if gym_id else url_for("kb_gyms.edit"))

        payload = _read_gym(request.form, gym_id=new_id)
        if position >= 0:
            # Обновляем на месте, а не подменяем: иначе теряются комментарии,
            # которыми в YAML объяснены неочевидные решения по залу.
            document["gyms"][position] = merge_into(document["gyms"][position], payload)
        else:
            if _gym_index(document, new_id) >= 0:
                flash(f"Зал с идентификатором «{new_id}» уже есть.", "error")
                return redirect(url_for("kb_gyms.edit"))
            document["gyms"].append(payload)

        try:
            result = kb_editor.save({"gyms.yaml": document})
        except KBEditError as exc:
            flash(exc.message, "error")
            for line in exc.errors[:6]:
                flash(line, "detail")
            return redirect(
                url_for("kb_gyms.edit", gym_id=gym_id) if gym_id else url_for("kb_gyms.edit")
            )

        flash(f"Сохранено. Бот подхватит правку в течение секунды (версия {result.kb_hash[:8]}).", "ok")
        for warning in result.warnings:
            flash(warning, "detail")
        return redirect(url_for("kb_gyms.edit", gym_id=new_id))

    raw = document["gyms"][position] if position >= 0 else None
    kb = snapshot()
    return render_template(
        "gym_edit.html",
        gym=raw,
        gym_id=gym_id,
        days=DAYS,
        disciplines=DISCIPLINES,
        scopes=SCOPES,
        statuses=STATUSES,
        blank_slots=BLANK_SLOTS,
        artifacts=sorted(kb.media, key=lambda artifact: artifact.id),
        gaps=[gap.value for gap in GapRef],
    )


@bp.route("/<gym_id>/delete", methods=["POST"])
@login_required
def delete(gym_id: str) -> Any:
    """Удаляет зал из базы знаний.

    Удаление, а не «выключение»: выключенный зал остаётся в базе и продолжает
    участвовать в проверках ссылок. Если зал просто закрылся на лето — снимите
    галочку «работает», это другое действие.
    """
    kb_editor = editor()
    document = kb_editor.load("gyms.yaml")
    position = _gym_index(document, gym_id)
    if position < 0:
        flash("Такого зала нет.", "error")
        return redirect(url_for("kb_gyms.index"))
    del document["gyms"][position]
    try:
        kb_editor.save({"gyms.yaml": document})
    except KBEditError as exc:
        flash(exc.message, "error")
        for line in exc.errors[:6]:
            flash(line, "detail")
        return redirect(url_for("kb_gyms.edit", gym_id=gym_id))
    flash(f"Зал «{gym_id}» удалён.", "ok")
    return redirect(url_for("kb_gyms.index"))


# --------------------------------------------------------------------------- #
# Расписание
# --------------------------------------------------------------------------- #
@bp.route("/schedule")
@login_required
def schedule() -> Any:
    """Недельная сетка по всем залам — как это видит родитель."""
    kb = snapshot()
    gyms = sorted(
        (gym for gym in kb.gyms.gyms if gym.active),
        key=lambda gym: (gym.scope.value != "city", gym.id),
    )
    grid: dict[str, dict[str, list[Any]]] = {}
    for gym in gyms:
        by_day: dict[str, list[Any]] = {day: [] for day, _ in DAYS}
        for slot in gym.schedule:
            for day in slot.days:
                by_day.setdefault(day, []).append(slot)
        for slots in by_day.values():
            slots.sort(key=lambda item: item.time_start)
        grid[gym.id] = by_day
    return render_template(
        "schedule.html", gyms=gyms, grid=grid, days=DAYS, disciplines=dict(DISCIPLINES)
    )


@bp.route("/<gym_id>/schedule-text", methods=["POST"])
@login_required
def schedule_text(gym_id: str) -> Any:
    """Замена расписания зала вставленным текстом — тем же, что шлют в WhatsApp."""
    kb_editor = editor()
    document = kb_editor.load("gyms.yaml")
    position = _gym_index(document, gym_id)
    if position < 0:
        flash("Такого зала нет.", "error")
        return redirect(url_for("kb_gyms.schedule"))

    raw = request.form.get("text", "")
    try:
        parsed = parse_schedule_text(raw)
    except ScheduleParseError as exc:
        flash("Расписание не разобрано — ничего не изменено.", "error")
        for line in exc.problems[:6]:
            flash(line, "detail")
        return redirect(url_for("kb_gyms.edit", gym_id=gym_id))

    document["gyms"][position]["schedule"] = parsed.as_yaml_dicts()
    try:
        result = kb_editor.save({"gyms.yaml": document})
    except KBEditError as exc:
        flash(exc.message, "error")
        for line in exc.errors[:6]:
            flash(line, "detail")
        return redirect(url_for("kb_gyms.edit", gym_id=gym_id))

    flash(f"Расписание обновлено: {len(parsed.slots)} занятий (версия {result.kb_hash[:8]}).", "ok")
    for warning in parsed.warnings:
        flash(warning, "detail")
    return redirect(url_for("kb_gyms.edit", gym_id=gym_id))
