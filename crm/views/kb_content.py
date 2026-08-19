"""Остальная база знаний: цены, вопросы-ответы, материалы, политики, тексты.

Все страницы устроены одинаково: читаем YAML с сохранением комментариев, меняем
только то, что пришло из формы, отдаём :class:`crm.kbio.KBEditor` — он проверит
базу целиком и откатит правку, если она ломает бота.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.admin.media_store import MediaWriteError, register_media
from app.kb import loader as kb_loader
from app.types import ArtifactKind, FactSource, GapRef, Scope
from crm.app import config, editor, login_required, snapshot
from crm.forms import (
    bilingual,
    choice,
    csv_list,
    flag,
    integer,
    lines,
    opt_integer,
    opt_text,
    text,
)
from crm.kbio import KBEditError, merge_into

bp = Blueprint("kb_content", __name__, url_prefix="/kb")

#: Каналы, по которым артефакт разрешают или запрещают.
CHANNELS: tuple[str, ...] = ("telegram", "whatsapp", "instagram")

#: Что бот умеет отправлять вложением. Всё остальное отклоняем на входе: файл,
#: сохранённый под чужим расширением, дошёл бы до клиента битым, и разбираться
#: пришлось бы уже по жалобе.
VIDEO_SUFFIXES: frozenset[str] = frozenset({".mp4", ".mov", ".m4v"})
IMAGE_SUFFIXES: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp"})
ALLOWED_UPLOADS: frozenset[str] = VIDEO_SUFFIXES | IMAGE_SUFFIXES


def _incomplete(back: str) -> Any | None:
    """Отклоняет неполный POST. ``None`` — форма пришла целиком.

    Формы этого раздела собирают запись заново, поэтому отсутствие полей
    неотличимо от «владелец всё стёр». Скрытое поле ``form_complete``
    подтверждает, что браузер прислал форму полностью.
    """
    if flag(request.form, "form_complete"):
        return None
    flash(
        "Форма пришла не полностью — ничего не изменено. "
        "Откройте страницу заново и сохраните ещё раз.",
        "error",
    )
    return redirect(back)


def _save(documents: dict[str, Any], *, back: str, ok: str) -> Any:
    """Общий хвост всех правок: сохранить, рассказать о результате, вернуться."""
    try:
        result = editor().save(documents)
    except KBEditError as exc:
        flash(exc.message, "error")
        for line in exc.errors[:8]:
            flash(line, "detail")
        return redirect(back)
    flash(f"{ok} Версия базы знаний {result.kb_hash[:8]}, бот подхватит сам.", "ok")
    for warning in result.warnings:
        flash(warning, "detail")
    return redirect(back)


# --------------------------------------------------------------------------- #
# Цены
# --------------------------------------------------------------------------- #
def _plan_from_form(form: Any, prefix: str) -> dict[str, Any] | None:
    """Один тариф из формы. Пустая цена означает «тарифа нет»."""
    price = opt_integer(form, f"{prefix}_price")
    if price is None:
        return None
    note = bilingual(form, f"{prefix}_note")
    return {
        "price": price,
        "recalculation": flag(form, f"{prefix}_recalculation"),
        "label": bilingual(form, f"{prefix}_label"),
        "note": note if note.get("ru") or note.get("kk") else None,
    }


@bp.route("/pricing", methods=["GET", "POST"])
@login_required
def pricing() -> Any:
    """Тарифы, семейные скидки, оплата.

    Здесь живут числа, по которым считает калькулятор бота. Модель их не
    придумывает и не пересчитывает: ошибка в цифре на этой странице — это ошибка
    во всех ответах бота о цене сразу.
    """
    document = editor().load("pricing.yaml")

    if request.method == "POST":
        stop = _incomplete(url_for("kb_content.pricing"))
        if stop is not None:
            return stop
        form = request.form
        document["city_settlement"] = text(form, "city_settlement") or "Костанай"
        document["city_sessions"] = integer(form, "city_sessions", default=12)
        document["city_validity_days"] = integer(form, "city_validity_days", default=30)
        document["city_validity_note"] = merge_into(
            document.get("city_validity_note"), bilingual(form, "city_validity_note")
        )
        document["city_plans"] = merge_into(
            document.get("city_plans"),
            {
                "standard": _plan_from_form(form, "standard"),
                "flexible": _plan_from_form(form, "flexible"),
            },
        )
        document["city_single"] = merge_into(
            document.get("city_single"), _plan_from_form(form, "single")
        )

        discount = document.get("city_family_discount") or {}
        rules: list[list[int]] = []
        for line in lines(form, "family_rules"):
            head, _, tail = line.partition(":")
            try:
                rules.append([int(head.strip()), int(tail.strip())])
            except ValueError:
                continue
        discount["rules"] = rules
        discount["applies_to"] = csv_list(form, "family_applies_to")
        discount["max_children"] = opt_integer(form, "family_max_children")
        discount["applies_to_status"] = choice(
            form, "family_applies_to_status", ("confirmed", "unconfirmed"), default="unconfirmed"
        )
        discount["label"] = merge_into(discount.get("label"), bilingual(form, "family_label"))
        document["city_family_discount"] = discount

        document["region_settlements"] = lines(form, "region_settlements")
        document["region_plans"] = merge_into(
            document.get("region_plans"), {"standard": _plan_from_form(form, "region_standard")}
        )
        document["region_single"] = merge_into(
            document.get("region_single"), _plan_from_form(form, "region_single")
        )
        document["region_family_price_per_child"] = opt_integer(form, "region_family_price")
        document["region_family_min_children"] = integer(form, "region_family_min", default=2)
        document["region_family_label"] = merge_into(
            document.get("region_family_label"), bilingual(form, "region_family_label")
        )

        document["payment_methods"] = csv_list(form, "payment_methods")
        details = bilingual(form, "payment_details")
        document["payment_details"] = (
            merge_into(document.get("payment_details"), details)
            if details.get("ru") or details.get("kk")
            else None
        )
        freeze = bilingual(form, "freeze_policy")
        document["freeze_policy"] = (
            merge_into(document.get("freeze_policy"), freeze)
            if freeze.get("ru") or freeze.get("kk")
            else None
        )

        return _save(
            {"pricing.yaml": document}, back=url_for("kb_content.pricing"), ok="Цены сохранены."
        )

    return render_template("pricing.html", doc=document)


# --------------------------------------------------------------------------- #
# Вопросы и ответы
# --------------------------------------------------------------------------- #
@bp.route("/faq")
@login_required
def faq() -> Any:
    """Список вопросов с поиском по тексту."""
    document = editor().load("faq.yaml")
    query = text(request.args, "q").lower()
    topic = text(request.args, "topic")
    entries = list(document.get("entries") or [])

    def matches(entry: Any) -> bool:
        if topic and entry.get("topic") != topic:
            return False
        if not query:
            return True
        haystack = [entry.get("id", ""), entry.get("topic", "")]
        answer = entry.get("answer") or {}
        haystack.extend(str(answer.get(lang) or "") for lang in ("ru", "kk"))
        for variants in (entry.get("question_variants") or {}).values():
            haystack.extend(str(v) for v in variants or [])
        return any(query in str(item).lower() for item in haystack)

    topics = sorted({str(entry.get("topic")) for entry in entries if entry.get("topic")})
    return render_template(
        "faq.html",
        entries=[entry for entry in entries if matches(entry)],
        total=len(entries),
        topics=topics,
        query=query,
        topic=topic,
    )


@bp.route("/faq/new", methods=["GET", "POST"])
@bp.route("/faq/<entry_id>", methods=["GET", "POST"])
@login_required
def faq_edit(entry_id: str | None = None) -> Any:
    """Создание и правка одного вопроса."""
    kb_editor = editor()
    document = kb_editor.load("faq.yaml")
    entries = document.setdefault("entries", [])
    position = next(
        (i for i, entry in enumerate(entries) if entry.get("id") == entry_id), -1
    ) if entry_id else -1
    if entry_id and position < 0:
        flash("Такого вопроса нет.", "error")
        return redirect(url_for("kb_content.faq"))

    if request.method == "POST":
        stop = _incomplete(url_for("kb_content.faq_edit", entry_id=entry_id) if entry_id else url_for("kb_content.faq"))
        if stop is not None:
            return stop
        form = request.form
        new_id = text(form, "id") or (entry_id or "")
        if not new_id:
            flash("Не задан идентификатор вопроса.", "error")
            return redirect(url_for("kb_content.faq"))
        payload = {
            "id": new_id,
            "topic": text(form, "topic") or "general",
            "scope": choice(form, "scope", [s.value for s in Scope], default="any"),
            "question_variants": {
                "ru": lines(form, "variants_ru"),
                "kk": lines(form, "variants_kk"),
            },
            "answer": bilingual(form, "answer"),
            "source": choice(form, "source", [s.value for s in FactSource], default="owner_confirmed"),
            "gap_ref": opt_text(form, "gap_ref"),
            "escalate_if_empty": flag(form, "escalate_if_empty"),
            "requires_tool": opt_text(form, "requires_tool"),
            "forbidden_claims": lines(form, "forbidden_claims"),
        }
        if position >= 0:
            entries[position] = merge_into(entries[position], payload)
        else:
            if any(entry.get("id") == new_id for entry in entries):
                flash(f"Вопрос с идентификатором «{new_id}» уже есть.", "error")
                return redirect(url_for("kb_content.faq_edit"))
            entries.append(payload)
        return _save(
            {"faq.yaml": document},
            back=url_for("kb_content.faq_edit", entry_id=new_id),
            ok="Вопрос сохранён.",
        )

    entry = entries[position] if position >= 0 else None
    return render_template(
        "faq_edit.html",
        entry=entry,
        entry_id=entry_id,
        scopes=[s.value for s in Scope],
        sources=[s.value for s in FactSource],
        gaps=[g.value for g in GapRef],
    )


@bp.route("/faq/<entry_id>/delete", methods=["POST"])
@login_required
def faq_delete(entry_id: str) -> Any:
    """Удаляет вопрос."""
    kb_editor = editor()
    document = kb_editor.load("faq.yaml")
    entries = document.get("entries") or []
    position = next((i for i, entry in enumerate(entries) if entry.get("id") == entry_id), -1)
    if position < 0:
        flash("Такого вопроса нет.", "error")
        return redirect(url_for("kb_content.faq"))
    del entries[position]
    return _save({"faq.yaml": document}, back=url_for("kb_content.faq"), ok="Вопрос удалён.")


# --------------------------------------------------------------------------- #
# Материалы
# --------------------------------------------------------------------------- #
@bp.route("/media")
@login_required
def media() -> Any:
    """Фото, видео и текстовые карточки, которые бот отправляет клиентам."""
    document = editor().load("media.yaml")
    kb = snapshot()
    on_disk = {artifact.id: artifact.enabled for artifact in kb.media}
    return render_template(
        "media.html",
        artifacts=list(document.get("artifacts") or []),
        effective=on_disk,
        media_dir=config().media_dir,
    )


@bp.route("/media/upload", methods=["POST"])
@login_required
def media_upload() -> Any:
    """Загрузка файла: он ляжет в ``media/`` и появится в базе знаний.

    Работу делает тот же :func:`app.admin.media_store.register_media`, что и приём
    файла в Telegram: копия, атомарная запись, валидация, откат. Второй реализации
    у этой операции быть не должно — разошлись бы правила отправки по каналам.
    """
    upload = request.files.get("file")
    caption = text(request.form, "when_to_send")
    if upload is None or not upload.filename:
        flash("Файл не выбран.", "error")
        return redirect(url_for("kb_content.media"))
    if len(caption) < 5:
        flash("Опишите одной строкой, когда бот должен отправлять этот материал.", "error")
        return redirect(url_for("kb_content.media"))

    suffix = Path(upload.filename).suffix.lower()
    if suffix not in ALLOWED_UPLOADS:
        flash(
            f"Формат {suffix or 'без расширения'} бот отправлять не умеет. "
            f"Подойдут: {', '.join(sorted(ALLOWED_UPLOADS))}.",
            "error",
        )
        return redirect(url_for("kb_content.media"))
    kind = "video" if suffix in VIDEO_SUFFIXES else "image"
    cfg = config()
    # Имя с точностью до процесса: два одновременных сохранения с общим именем
    # затирали бы файл друг друга, и в базу знаний попадало бы чужое видео.
    handle, tmp_name = tempfile.mkstemp(prefix="crm-upload-", suffix=suffix or ".bin")
    os.close(handle)
    tmp = Path(tmp_name)
    upload.save(tmp)
    try:
        result = register_media(
            kb_dir=cfg.kb_dir,
            media_dir=cfg.media_dir,
            schema_version=cfg.schema_version,
            source=tmp,
            kind=kind,
            when_to_send=caption,
            title_ru=text(request.form, "title") or caption[:60],
            gym_id=opt_text(request.form, "gym_id"),
        )
    except MediaWriteError as exc:
        flash(f"Не сохранилось: {exc}", "error")
        return redirect(url_for("kb_content.media"))
    finally:
        tmp.unlink(missing_ok=True)

    flash(
        f"Материал {result.artifact_id} добавлен ({result.size_bytes / 1048576:.1f} МБ). "
        + ("Видео уходит только в Telegram." if kind == "video" else "Отправляется во все каналы."),
        "ok",
    )
    return redirect(url_for("kb_content.media_edit", artifact_id=result.artifact_id))


@bp.route("/media/<artifact_id>", methods=["GET", "POST"])
@login_required
def media_edit(artifact_id: str) -> Any:
    """Правка одного материала: когда отправлять, куда можно, включён ли."""
    kb_editor = editor()
    document = kb_editor.load("media.yaml")
    artifacts = document.get("artifacts") or []
    position = next((i for i, a in enumerate(artifacts) if a.get("id") == artifact_id), -1)
    if position < 0:
        flash("Такого материала нет.", "error")
        return redirect(url_for("kb_content.media"))

    if request.method == "POST":
        stop = _incomplete(url_for("kb_content.media_edit", artifact_id=artifact_id))
        if stop is not None:
            return stop
        form = request.form
        artifact = artifacts[position]
        artifact["enabled"] = flag(form, "enabled")
        artifact["scope"] = choice(form, "scope", [s.value for s in Scope], default="any")
        artifact["gym_id"] = opt_text(form, "gym_id")
        artifact["title"] = merge_into(artifact.get("title"), bilingual(form, "title"))
        artifact["when_to_send_ru"] = text(form, "when_to_send_ru")
        body = bilingual(form, "body")
        artifact["body"] = (
            merge_into(artifact.get("body"), body) if body.get("ru") or body.get("kk") else None
        )
        artifact["channels"] = merge_into(
            artifact.get("channels"),
            {
                channel: ("allow" if flag(form, f"channel_{channel}") else "deny")
                for channel in CHANNELS
            },
        )
        artifact["max_send_per_dialog"] = integer(form, "max_send_per_dialog", default=1)
        return _save(
            {"media.yaml": document},
            back=url_for("kb_content.media_edit", artifact_id=artifact_id),
            ok="Материал сохранён.",
        )

    kb = snapshot()
    return render_template(
        "media_edit.html",
        artifact=artifacts[position],
        channels=CHANNELS,
        scopes=[s.value for s in Scope],
        kinds=[k.value for k in ArtifactKind],
        gyms=sorted(kb.gyms.gyms, key=lambda gym: gym.id),
        media_dir=config().media_dir,
    )


@bp.route("/media/<artifact_id>/delete", methods=["POST"])
@login_required
def media_delete(artifact_id: str) -> Any:
    """Убирает материал из базы знаний. Файл на диске остаётся."""
    kb_editor = editor()
    document = kb_editor.load("media.yaml")
    artifacts = document.get("artifacts") or []
    position = next((i for i, a in enumerate(artifacts) if a.get("id") == artifact_id), -1)
    if position < 0:
        flash("Такого материала нет.", "error")
        return redirect(url_for("kb_content.media"))
    del artifacts[position]
    return _save({"media.yaml": document}, back=url_for("kb_content.media"), ok="Материал удалён.")


# --------------------------------------------------------------------------- #
# Политики и тексты
# --------------------------------------------------------------------------- #
@bp.route("/policies", methods=["GET", "POST"])
@login_required
def policies() -> Any:
    """Правила поведения бота: часы работы, эскалации, напоминания, запреты."""
    document = editor().load("policies.yaml")

    if request.method == "POST":
        stop = _incomplete(url_for("kb_content.policies"))
        if stop is not None:
            return stop
        form = request.form
        document["org_brand"] = text(form, "org_brand")
        document["org_city"] = text(form, "org_city")
        document["audience_adults_only"] = flag(form, "audience_adults_only")
        work = bilingual(form, "work_hours")
        document["work_hours"] = (
            merge_into(document.get("work_hours"), work) if work.get("ru") or work.get("kk") else None
        )
        document["sla_reply_minutes"] = opt_integer(form, "sla_reply_minutes")
        document["escalation_pause_minutes"] = integer(form, "escalation_pause_minutes", default=60)
        document["followup_stop_words"] = lines(form, "followup_stop_words")
        document["forbidden_behaviour"] = lines(form, "forbidden_behaviour")
        return _save(
            {"policies.yaml": document}, back=url_for("kb_content.policies"), ok="Правила сохранены."
        )

    return render_template("policies.html", doc=document)


@bp.route("/texts", methods=["GET", "POST"])
@login_required
def texts() -> Any:
    """Готовые фразы бота: приветствие, отказы, заглушки «данных нет».

    Это единственное место, где меняется буквальный текст, который клиент
    получает без участия модели. Ключи менять нельзя — на них ссылается код,
    и отсутствие ключа не даст базе знаний загрузиться вовсе.
    """
    document = editor().load("i18n.yaml")
    strings = document.get("strings") or {}

    if request.method == "POST":
        changed = 0
        for key in list(strings.keys()):
            for lang in ("ru", "kk"):
                field = f"s::{key}::{lang}"
                if field not in request.form:
                    continue
                value = text(request.form, field)
                current = strings[key].get(lang)
                if value != (current or ""):
                    strings[key][lang] = value or None
                    changed += 1
        if not changed:
            flash("Ничего не изменилось.", "detail")
            return redirect(url_for("kb_content.texts"))
        return _save(
            {"i18n.yaml": document},
            back=url_for("kb_content.texts"),
            ok=f"Изменено фраз: {changed}.",
        )

    query = text(request.args, "q").lower()
    rows = [
        (key, value)
        for key, value in sorted(strings.items())
        if not query
        or query in key.lower()
        or query in str(value.get("ru") or "").lower()
        or query in str(value.get("kk") or "").lower()
    ]
    return render_template("texts.html", rows=rows, total=len(strings), query=query)


@bp.route("/lexicon", methods=["GET", "POST"])
@login_required
def lexicon() -> Any:
    """Слова, по которым бот узнаёт намерение и язык клиента.

    Сюда добавляют то, как пишут живые люди: «скока», «абик», «где зал». Каждое
    добавленное слово — это диалог, в котором бот понял клиента с первого раза.
    """
    document = editor().load("lexicon.yaml")

    if request.method == "POST":
        intents = document.get("intents") or {}
        for name in list(intents.keys()):
            field = f"intent::{name}"
            if field in request.form:
                intents[name] = lines(request.form, field)
        for name in ("kk_words", "kk_translit", "ru_translit", "districts_extra"):
            if name in request.form:
                document[name] = lines(request.form, name)
        return _save(
            {"lexicon.yaml": document}, back=url_for("kb_content.lexicon"), ok="Словарь сохранён."
        )

    return render_template("lexicon.html", doc=document)


# --------------------------------------------------------------------------- #
# Файлы и копии
# --------------------------------------------------------------------------- #
@bp.route("/raw/<name>")
@login_required
def raw(name: str) -> Any:
    """Показывает файл базы знаний как есть — на случай «а что там на самом деле»."""
    if name not in kb_loader.KB_FILES:
        abort(404)
    path = config().kb_dir / name
    if not path.is_file():
        abort(404)
    return render_template(
        "raw.html", name=name, body=path.read_text(encoding="utf-8"), files=kb_loader.KB_FILES
    )


@bp.route("/backups")
@login_required
def backups() -> Any:
    """Резервные копии базы знаний: одна на каждую правку."""
    return render_template("backups.html", backups=editor().backups())


@bp.route("/backups/<stamp>/restore", methods=["POST"])
@login_required
def backup_restore(stamp: str) -> Any:
    """Откат базы знаний к состоянию из копии."""
    try:
        result = editor().restore(stamp)
    except KBEditError as exc:
        flash(exc.message, "error")
        for line in exc.errors[:6]:
            flash(line, "detail")
        return redirect(url_for("kb_content.backups"))
    flash(f"База знаний возвращена к копии {stamp} (версия {result.kb_hash[:8]}).", "ok")
    return redirect(url_for("kb_content.backups"))
