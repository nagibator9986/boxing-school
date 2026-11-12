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

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.admin.schedule_text import ScheduleParseError, parse_schedule_text
from app.kb import render
from app.tools import freshness
from app.types import CHANNEL_LIMITS, ChannelKind, GapRef, Language
from crm import gym_companions
from crm.app import config, editor, login_required, snapshot
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
#: Школьная смена строки расписания (``ScheduleSlot.shift``): для кого это время. Утро
#: Плазы — для второй смены: ребёнку из первой бот его не предложит.
SHIFTS: tuple[tuple[str, str], ...] = (
    ("any", "все смены"), ("first", "1-я смена"), ("second", "2-я смена"),
)
#: Видео дороги: WhatsApp принимает только mp4 (``CHANNEL_LIMITS``) — другой формат ушёл бы
#: клиенту битым файлом.
ROUTE_VIDEO_SUFFIXES: frozenset[str] = frozenset({".mp4"})
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
        slot: dict[str, Any] = {
            "discipline": choice(
                form, f"slot-{index}-discipline", dict(DISCIPLINES), default="kickboxing"
            ),
            "age_from": age_from,
            "age_to": age_to,
            "days": days,
            "time_start": start,
            "time_end": end,
            # Смена в форме не показывается, но и не теряется: раньше сохранение зала
            # ставило всем строкам «any».
            "shift": choice(form, f"slot-{index}-shift", dict(SHIFTS), default="any"),
            "note": {"ru": note_ru, "kk": opt_text(form, f"slot-{index}-note_kk") or note_ru}
            if note_ru
            else None,
        }
        # Особая цена (КСК: бокс Вт, Чт, Сб — 20 000) и группа (Плаза: Вт, Чт 19:00 и Сб 19:30 —
        # одно расписание). До 07.11.2026 полей в форме не было, и любое сохранение зала
        # молча стирало цену из строк расписания.
        price = opt_integer(form, f"slot-{index}-price")
        if price is not None:
            slot["price"] = price
        group = opt_text(form, f"slot-{index}-group")
        if group:
            slot["group"] = group
        slots.append(slot)
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
        "serves_districts": csv_list(form, "serves_districts"),
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
        # Дата открытия нового зала: без неё сохранение в CRM стирало бы её из файла.
        "opens_on": opt_text(form, "opens_on"),
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
    return render_template(
        "gyms.html", gyms=gyms, city=kb.gyms.city_settlement, suburbs=kb.gyms.city_suburbs
    )


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
            # Идентификатор нового зала придумывается из названия: латиница без пробелов —
            # не то, что должен сочинять владелец школы.
            taken = {str(gym.get("id")) for gym in document.get("gyms") or ()}
            new_id = gym_companions.new_gym_id(
                text(request.form, "title_ru"), text(request.form, "settlement"), taken
            )

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

        # Посёлок в ценах, пригород в городе, карточка адреса — то, без чего зал не
        # сохранится или не заработает в боте, CRM доделывает в той же правке.
        prepared = gym_companions.prepare_gym_save(kb_editor, document, new_id)
        try:
            result = kb_editor.save(prepared.documents)
        except KBEditError as exc:
            flash(exc.message, "error")
            for line in exc.errors[:6]:
                flash(line, "detail")
            return redirect(
                url_for("kb_gyms.edit", gym_id=gym_id) if gym_id else url_for("kb_gyms.edit")
            )

        flash(f"Сохранено. Бот подхватит правку в течение секунды (версия {result.kb_hash[:8]}).", "ok")
        for note in prepared.notes:
            flash(note, "detail")
        for warning in result.warnings:
            flash(warning, "detail")
        return redirect(url_for("kb_gyms.edit", gym_id=new_id))

    raw = document["gyms"][position] if position >= 0 else None
    kb = snapshot()
    owned = gym_companions.companions(kb_editor.load("media.yaml"), gym_id) if gym_id else []
    return render_template(
        "gym_edit.html",
        gym=raw,
        gym_id=gym_id,
        days=DAYS,
        disciplines=DISCIPLINES,
        scopes=SCOPES,
        statuses=STATUSES,
        shifts=SHIFTS,
        blank_slots=BLANK_SLOTS,
        artifacts=sorted(kb.media, key=lambda artifact: artifact.id),
        gaps=[gap.value for gap in GapRef],
        preview=_preview(kb, gym_id) if gym_id else None,
        companions=owned,
    )


def _preview(kb: Any, gym_id: str) -> dict[str, Any] | None:
    """Как зал увидит клиент: ровно те карточки, которые соберёт бот, и чего не хватает.

    Владелец 11.11.2026 написал «вы не исправили расписание» — правка была, а проверить, что
    отправит бот, можно было только написав ему. Здесь видно сразу после сохранения.
    """
    gym = kb.gym(gym_id)
    if gym is None:
        return None
    cards: list[tuple[str, str]] = []
    for lang, label in ((Language.RU, "по-русски"), (Language.KK, "по-казахски")):
        try:
            cards.append((f"Расписание {label}", render.render_schedule_card(
                kb, gym_id=gym_id, slots=gym.schedule, lang=lang
            )))
        except Exception as exc:  # noqa: BLE001 - предпросмотр не роняет страницу
            cards.append((f"Расписание {label}", f"не собралось: {exc}"))
    location = kb.artifact(f"{gym_companions.LOCATION_PREFIX}{gym_id}")
    if location is not None:
        cards.append(("Карточка адреса", render.render_gym_location(kb, gym_id=gym_id, lang=Language.RU)))
    route = kb.artifact(f"{gym_companions.ROUTE_PREFIX}{gym_id}")
    if route is not None:
        cards.append(("Подпись к видео дороги", render.render_route_caption(
            kb, gym_id=gym_id, lang=Language.RU, with_schedule=True
        )))

    problems: list[str] = []
    if not gym.active:
        problems.append("Галочка «бот предлагает этот зал» снята — клиентам зал не показывается.")
    if gym.status.value != "open":
        problems.append("Состояние зала не «Работает» — бот его не предлагает.")
    if not gym.schedule:
        problems.append("Расписания нет: на вопрос о времени бот ответит, что уточнит администратор.")
    if not (gym.address.ru or "").strip():
        problems.append("Адреса нет: бот не сможет объяснить, где зал.")
    if route is None:
        problems.append("Нет видео дороги — его можно загрузить ниже.")
    elif not route.enabled:
        problems.append("Видео дороги выключено: файл не найден или не совпал — загрузите заново.")
    elif route.channels.get("whatsapp") != "allow":
        problems.append("Видео дороги больше 10 МБ — в WhatsApp не уходит. Сожмите и загрузите заново.")
    since = freshness.since_of(kb, gym)
    return {
        "cards": cards,
        "problems": problems,
        "since": since.astimezone(_school_tz(kb)).strftime("%d.%m.%Y %H:%M") if since else None,
        "route": route,
        "kb_hash": kb.kb_hash[:8],
    }


def _school_tz(kb: Any) -> Any:
    from zoneinfo import ZoneInfo

    return ZoneInfo(kb.gyms.timezone)


@bp.route("/<gym_id>/delete", methods=["POST"])
@login_required
def delete(gym_id: str) -> Any:
    """Удаляет зал из базы знаний.

    Удаление, а не «выключение»: выключенный зал остаётся в базе и продолжает
    участвовать в проверках ссылок. Если зал просто закрылся на лето — снимите
    галочку «работает», это другое действие.
    """
    kb_editor = editor()
    try:
        # Карточка адреса и видео дороги ссылаются на зал: без них удаление проходит проверку.
        prepared = gym_companions.remove_gym(kb_editor, gym_id)
    except KeyError:
        flash("Такого зала нет.", "error")
        return redirect(url_for("kb_gyms.index"))
    try:
        kb_editor.save(prepared.documents)
    except KBEditError as exc:
        flash(exc.message, "error")
        for line in exc.errors[:6]:
            flash(line, "detail")
        return redirect(url_for("kb_gyms.edit", gym_id=gym_id))
    flash(f"Зал «{gym_id}» удалён.", "ok")
    for note in prepared.notes:
        flash(note, "detail")
    return redirect(url_for("kb_gyms.index"))


@bp.route("/<gym_id>/route-video", methods=["POST"])
@login_required
def route_video(gym_id: str) -> Any:
    """Видео дороги зала: загрузка прямо со страницы зала.

    Раньше видео из «Материалов» получало имя ``video_<название>``, и бот не узнавал в нём
    дорогу к залу: он ищет ``route_<id зала>``. К тому же видео по умолчанию шло только в
    Telegram, хотя WhatsApp принимает mp4 до 10 МБ.

    Файл ложится под новым именем: прежнее видео остаётся на месте, пока база не приняла
    правку, — при отказе бот продолжает отправлять старое.
    """
    back = url_for("kb_gyms.edit", gym_id=gym_id)
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        flash("Файл не выбран.", "error")
        return redirect(back)
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in ROUTE_VIDEO_SUFFIXES:
        flash(
            f"Нужен файл mp4 — {suffix or 'файл без расширения'} WhatsApp не примет. "
            "Сохраните видео из телефона как mp4 и загрузите снова.",
            "error",
        )
        return redirect(back)
    media_dir = config().media_dir
    media_dir.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=".route-", suffix=suffix, dir=media_dir)
    os.close(handle)
    tmp = Path(tmp_name)
    final: Path | None = None
    try:
        upload.save(tmp)
        blob = tmp.read_bytes()
        if not blob:
            flash("Файл пустой.", "error")
            return redirect(back)
        sha = hashlib.sha256(blob).hexdigest()
        final = media_dir / f"{gym_companions.ROUTE_PREFIX}{gym_id}_{sha[:8]}{suffix}"
        previous = snapshot().artifact(f"{gym_companions.ROUTE_PREFIX}{gym_id}")
        previous_file = previous.file_path if previous is not None else None
        os.replace(tmp, final)
        kb_editor = editor()
        try:
            prepared = gym_companions.attach_route_video(
                kb_editor, gym_id, file_name=final.name, size=len(blob), sha256=sha
            )
        except KeyError:
            flash("Такого зала нет.", "error")
            return redirect(url_for("kb_gyms.index"))
        try:
            result = kb_editor.save(prepared.documents)
        except KBEditError as exc:
            flash(exc.message, "error")
            for line in exc.errors[:6]:
                flash(line, "detail")
            return redirect(back)
        final = None  # принято — файл остаётся
        _drop_unused_file(media_dir, previous_file, keep=f"{gym_companions.ROUTE_PREFIX}{gym_id}_{sha[:8]}{suffix}")
    finally:
        tmp.unlink(missing_ok=True)
        if final is not None:
            final.unlink(missing_ok=True)
    limit_mb = CHANNEL_LIMITS[ChannelKind.WHATSAPP].max_file_bytes // (1024 * 1024)
    flash(
        f"Видео дороги загружено ({len(blob) / 1048576:.1f} МБ, версия {result.kb_hash[:8]}). "
        f"Бот отправит его, когда разговор дойдёт до этого зала.",
        "ok",
    )
    for note in prepared.notes:
        flash(note.replace("10 МБ", f"{limit_mb} МБ"), "detail")
    return redirect(back)


def _drop_unused_file(media_dir: Path, name: str | None, *, keep: str) -> None:
    """Убирает прежний файл видео, если на него больше не ссылается ни один материал."""
    if not name or name == keep:
        return
    try:
        still_used = any(artifact.file_path == name for artifact in editor().snapshot().media)
        if not still_used:
            (media_dir / name).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - лишний файл на диске не повод ронять загрузку
        return


@bp.route("/city", methods=["POST"])
@login_required
def city() -> Any:
    """Посёлки, которые школа считает частью города: там городские цены и городской список залов."""
    if not flag(request.form, "form_complete"):
        flash("Форма пришла не полностью — ничего не изменено.", "error")
        return redirect(url_for("kb_gyms.index"))
    kb_editor = editor()
    document = kb_editor.load("gyms.yaml")
    document["city_suburbs"] = csv_list(request.form, "city_suburbs")
    try:
        result = kb_editor.save({"gyms.yaml": document})
    except KBEditError as exc:
        flash(exc.message, "error")
        for line in exc.errors[:6]:
            flash(line, "detail")
        return redirect(url_for("kb_gyms.index"))
    flash(f"Сохранено (версия {result.kb_hash[:8]}). Бот подхватит сам.", "ok")
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
