"""Всё правится в CRM, и бот это подхватывает (владелец 12.11.2026).

Проверяется не «страница открылась», а что правка в CRM делает бота другим: новый зал виден
боту целиком (расписание, карточка адреса, видео дороги), зал в новом посёлке сохраняется,
удаление проходит, а правки из репозитория доезжают и до файла, который правили в CRM.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.admin.kb_merge import merge3
from app.admin.media_store import register_media, video_channels
from app.core.lexicon import extract_name
from app.kb import render
from app.kb.places import place_words
from app.tools.gyms import gyms_named_in
from app.types import CHANNEL_LIMITS, ChannelKind, Language, Scope
from crm.kbio import KBEditor
from tests import test_crm as _crm
from tests.test_crm import form_fields

# Фикстуры песочницы CRM — те же, что в tests/test_crm.py.
sandbox = _crm.sandbox
client = _crm.client
editor = _crm.editor

PLAZA = "plaza_szm_70"


def _raw(kb_dir: Path, name: str) -> Any:
    return yaml.safe_load((kb_dir / name).read_text(encoding="utf-8"))


def _gym(kb_dir: Path, gym_id: str) -> dict[str, Any] | None:
    return next((gym for gym in _raw(kb_dir, "gyms.yaml")["gyms"] if gym["id"] == gym_id), None)


def _new_gym(client: Any, **fields: Any) -> Any:
    form = form_fields(client.get("/kb/gyms/new").data.decode())
    form.update({
        "id": "", "scope": "city", "settlement": "Костанай", "status": "open", "active": "1",
        "title_ru": "Береке Арена", "title_kk": "Береке Арена",
        "address_ru": "улица Абая, 5", "address_kk": "Абай көшесі, 5",
        "slot-0-discipline": "boxing", "slot-0-start": "18:00", "slot-0-end": "19:30",
        "slot-0-days": ["mon", "wed"], "slot-0-shift": "any",
    })
    form.update(fields)
    return client.post("/kb/gyms/new", data=form, follow_redirects=True)


# --------------------------------------------------------------------------- #
# Новый зал из CRM работает в боте целиком
# --------------------------------------------------------------------------- #
def test_a_new_gym_from_crm_is_complete_for_the_bot(client: Any, editor: KBEditor, sandbox: Any) -> None:
    page = _new_gym(client).data.decode()

    assert "Сохранено" in page, page[:2000]
    gym = _gym(sandbox.kb_dir, "bereke_arena")
    assert gym is not None, "идентификатор придуман из названия"
    assert "gym_location_bereke_arena" in gym["media"]
    kb = editor.snapshot()
    assert kb.artifact("gym_location_bereke_arena") is not None, "карточка адреса создана"
    assert "bereke_arena" in gyms_named_in(kb, "Береке Арена")
    card = render.render_schedule_card(kb, gym_id="bereke_arena", slots=kb.gym("bereke_arena").schedule, lang=Language.RU)
    assert "Пн, Ср · 18:00–19:30" in card, card
    assert "Береке Арена" in render.render_gyms_list_card(kb, scope=Scope.CITY, lang=Language.RU)
    assert "Создана карточка адреса" in page


def test_a_region_gym_in_a_new_settlement_gets_region_prices(client: Any, sandbox: Any) -> None:
    page = _new_gym(client, scope="region", settlement="Денисовка", title_ru="Денисовка",
                    address_ru="Денисовка, школа №1").data.decode()

    assert "Сохранено" in page, "раньше сохранение отклонялось: посёлка нет в районных ценах"
    assert "Денисовка" in _raw(sandbox.kb_dir, "pricing.yaml")["region_settlements"]
    assert "добавлен в районные цены" in page


def test_a_city_gym_in_a_suburb_marks_the_suburb_part_of_the_city(client: Any, sandbox: Any) -> None:
    page = _new_gym(client, settlement="Заречное", title_ru="Заречное").data.decode()

    assert "Сохранено" in page
    assert "Заречное" in _raw(sandbox.kb_dir, "gyms.yaml")["city_suburbs"]


def test_city_suburbs_are_edited_on_the_gyms_page(client: Any, sandbox: Any) -> None:
    client.post("/kb/gyms/city", data={"form_complete": "1", "city_suburbs": "Тобыл, Заречное"})
    assert _raw(sandbox.kb_dir, "gyms.yaml")["city_suburbs"] == ["Тобыл", "Заречное"]


def test_slot_shift_is_chosen_in_the_form(client: Any, sandbox: Any) -> None:
    _new_gym(client, **{"slot-0-start": "09:00", "slot-0-end": "10:30", "slot-0-shift": "second"})
    assert _gym(sandbox.kb_dir, "bereke_arena")["schedule"][0]["shift"] == "second"
    page = client.get("/kb/gyms/bereke_arena").data.decode()
    assert '<option value="second" selected>' in page


# --------------------------------------------------------------------------- #
# Видео дороги со страницы зала
# --------------------------------------------------------------------------- #
def _upload(client: Any, gym_id: str, data: bytes, name: str = "doroga.mp4") -> str:
    return client.post(
        f"/kb/gyms/{gym_id}/route-video",
        data={"file": (io.BytesIO(data), name)},
        content_type="multipart/form-data",
        follow_redirects=True,
    ).data.decode()


def test_route_video_becomes_the_gym_route(client: Any, editor: KBEditor, sandbox: Any) -> None:
    _new_gym(client)
    page = _upload(client, "bereke_arena", b"first-video")

    kb = editor.snapshot()
    route = kb.artifact("route_bereke_arena")
    assert route is not None and route.enabled, page[:1500]
    assert route.render_from == "route" and route.gym_id == "bereke_arena"
    assert route.channels["whatsapp"] == "allow", "mp4 до 10 МБ уходит в WhatsApp"
    assert "route_bereke_arena" in kb.gym("bereke_arena").media
    first_file = route.file_path

    _upload(client, "bereke_arena", b"second-video")
    route = editor.snapshot().artifact("route_bereke_arena")
    assert route.file_path != first_file and route.enabled
    assert not (sandbox.media_dir / first_file).exists(), "прежний файл убран"
    assert "Подпись к видео дороги" in client.get("/kb/gyms/bereke_arena").data.decode()


def test_a_large_route_video_is_kept_out_of_whatsapp(client: Any, editor: KBEditor) -> None:
    _new_gym(client)
    limit = CHANNEL_LIMITS[ChannelKind.WHATSAPP].max_file_bytes
    page = _upload(client, "bereke_arena", b"0" * (limit + 1))

    assert editor.snapshot().artifact("route_bereke_arena").channels["whatsapp"] == "deny"
    assert "в WhatsApp оно не уйдёт" in page


def test_a_route_video_must_be_mp4(client: Any, editor: KBEditor) -> None:
    _new_gym(client)
    page = _upload(client, "bereke_arena", b"x", name="doroga.mov")
    assert "Нужен файл mp4" in page
    assert editor.snapshot().artifact("route_bereke_arena") is None


def test_uploaded_videos_go_to_whatsapp_when_they_fit(tmp_path: Path, sandbox: Any) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"clip")
    result = register_media(
        kb_dir=sandbox.kb_dir, media_dir=sandbox.media_dir, schema_version=1, source=source,
        kind="video", when_to_send="когда спрашивают, как проходит тренировка", title_ru="Тренировка",
    )
    artifact = next(a for a in _raw(sandbox.kb_dir, "media.yaml")["artifacts"] if a["id"] == result.artifact_id)
    assert artifact["channels"]["whatsapp"] == "allow"
    assert video_channels(CHANNEL_LIMITS[ChannelKind.WHATSAPP].max_file_bytes + 1)["whatsapp"] == "deny"


# --------------------------------------------------------------------------- #
# Удаление зала и предпросмотр
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("gym_id", ["center_kasymkhanova_10", "bereke_arena"])
def test_a_gym_with_materials_can_be_deleted(client: Any, sandbox: Any, gym_id: str) -> None:
    _new_gym(client)
    _upload(client, "bereke_arena", b"video")
    assert any(a.get("gym_id") == gym_id for a in _raw(sandbox.kb_dir, "media.yaml")["artifacts"])

    page = client.post(f"/kb/gyms/{gym_id}/delete", follow_redirects=True).data.decode()

    assert f"Зал «{gym_id}» удалён" in page, page[:1500]
    assert _gym(sandbox.kb_dir, gym_id) is None
    assert not any(a.get("gym_id") == gym_id for a in _raw(sandbox.kb_dir, "media.yaml")["artifacts"])


def test_the_gym_page_shows_what_the_client_will_get(client: Any) -> None:
    page = client.get(f"/kb/gyms/{PLAZA}").data.decode()

    assert "Как это увидит клиент" in page
    assert "Вт, Чт, Сб · 09:00–10:30" in page
    assert "Расписание действует с 10.11.2026 15:32" in page


def test_the_preview_names_what_is_missing(client: Any) -> None:
    _new_gym(client)
    page = client.get("/kb/gyms/bereke_arena").data.decode()
    assert "Нет видео дороги" in page


# --------------------------------------------------------------------------- #
# Вопросы-ответы и правила
# --------------------------------------------------------------------------- #
def test_a_verbatim_answer_with_emoji_can_be_edited(client: Any, sandbox: Any) -> None:
    entry = next(e for e in _raw(sandbox.kb_dir, "faq.yaml")["entries"] if e.get("verbatim"))
    fields = form_fields(client.get(f"/kb/faq/{entry['id']}").data.decode())
    fields["answer_ru"] = fields["answer_ru"] + "\nЖдём вас!"

    page = client.post(f"/kb/faq/{entry['id']}", data=fields, follow_redirects=True).data.decode()

    assert "Вопрос сохранён" in page, page[:1500]
    saved = next(e for e in _raw(sandbox.kb_dir, "faq.yaml")["entries"] if e["id"] == entry["id"])
    assert saved["verbatim"] is True and saved["emoji_ok"] is True
    assert saved["answer"]["ru"].endswith("Ждём вас!")


def test_trial_lead_time_followups_and_closing_words_are_edited(client: Any, sandbox: Any) -> None:
    fields = form_fields(client.get("/kb/policies").data.decode())
    fields["trial_min_lead_hours"] = "3"
    fields["fu::fu_soft::delay_hours"] = "1,5"
    fields["fu::fu_value::max_times"] = "0"
    fields["fu::trial_reminder_morning::at_local_time"] = "10:00"
    fields["followup_closing_words"] = fields["followup_closing_words"] + "\nсау болыңыз"

    page = client.post("/kb/policies", data=fields, follow_redirects=True).data.decode()

    assert "Правила сохранены" in page, page[:1500]
    doc = _raw(sandbox.kb_dir, "policies.yaml")
    rules = {rule["event"]: rule for rule in doc["followup_policy"]}
    assert doc["trial_min_lead_hours"] == 3
    assert rules["fu_soft"]["delay_hours"] == 1.5
    assert rules["fu_value"]["max_times"] == 0
    assert rules["trial_reminder_morning"]["at_local_time"] == "10:00"
    assert rules["trial_reminder_2h"]["delay_hours"] == -2, "не тронутое не меняется"
    assert "сау болыңыз" in doc["followup_closing_words"]


# --------------------------------------------------------------------------- #
# Бот: зал из CRM — не имя ребёнка
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("Плаза", None), ("Плазой", None), ("Федоровке", None), ("Тобыле", None), ("Романтик", None),
        ("Роман", "Роман"), ("Жанар", "Жанар"), ("Арман", "Арман"), ("Фёдор", "Фёдор"),
        ("Асель, 87015551122", "Асель"),
    ],
)
def test_places_from_the_knowledge_base_are_not_names(kb, text: str, name: str | None) -> None:
    assert extract_name(text, not_names=place_words(kb)) == name


def test_district_words_stay_out_of_place_words(kb) -> None:
    """Районы и соседи зала — не названия мест для разбора имени: «Алтын Арман» — это имена."""
    words = place_words(kb)
    assert not {"второй", "аэропорт", "алтын", "арман"} & words, sorted(words)
    assert {"плаза", "тобыл", "федоровка"} <= words


def test_a_case_ending_is_short_and_a_longer_word_is_not_the_place() -> None:
    from app.core.lexicon import _is_place

    assert _is_place("Тобыле", "тобыл") and _is_place("Плазой", "плаза")
    assert not _is_place("Тобылбеков", "тобыл"), "фамилия с тем же началом — не посёлок"
    assert not _is_place("Жанар", "жана"), "короткое название сверяется целиком"


def test_a_gym_added_in_crm_is_not_taken_for_a_name(client: Any, editor: KBEditor) -> None:
    _new_gym(client)
    words = place_words(editor.snapshot())
    assert extract_name("Береке", not_names=words) is None
    assert extract_name("Береке") == "Береке", "без базы знаний слово похоже на имя"


# --------------------------------------------------------------------------- #
# Слияние выпуска с правками CRM
# --------------------------------------------------------------------------- #
def _doc(**gyms: dict[str, Any]) -> dict[str, Any]:
    return {"updated_at": "2026-11-01", "gyms": [{"id": gym_id, **body} for gym_id, body in gyms.items()]}


def test_release_and_crm_changes_to_different_fields_both_survive() -> None:
    base = _doc(a={"phone": "1", "time": "19:00"}, b={"phone": "2"})
    ours = _doc(a={"phone": "1-crm", "time": "19:00"}, b={"phone": "2"})
    ours["updated_at"] = "2026-11-12"
    theirs = _doc(a={"phone": "1", "time": "19:30"}, b={"phone": "2"}, c={"phone": "3"})
    # Отметку «когда правили» ставят обе стороны — это не конфликт и не перенос.
    theirs["updated_at"] = "2026-11-10"

    report = merge3(base, ours, theirs)

    assert ours["gyms"] == [{"id": "a", "phone": "1-crm", "time": "19:30"}, {"id": "b", "phone": "2"}, {"id": "c", "phone": "3"}]
    assert ours["updated_at"] == "2026-11-12" and report.conflicts == []
    assert set(report.applied) == {"gyms[a].time", "gyms[c]"}


def test_the_owner_wins_where_both_changed_the_same_value() -> None:
    base, ours, theirs = _doc(a={"time": "19:00"}), _doc(a={"time": "18:00"}), _doc(a={"time": "19:30"})
    report = merge3(base, ours, theirs)
    assert ours["gyms"][0]["time"] == "18:00"
    assert report.conflicts == ["gyms[a].time"]


def test_deletions_follow_the_untouched_side() -> None:
    base = _doc(a={"x": 1}, b={"x": 2}, c={"x": 3}, d={"x": 4})
    ours = _doc(a={"x": 1}, b={"x": 20}, d={"x": 40})   # CRM удалил c, правил b и d
    theirs = _doc(b={"x": 2}, c={"x": 30})              # выпуск удалил a и d, правил c

    report = merge3(base, ours, theirs)

    assert [gym["id"] for gym in ours["gyms"]] == ["b", "d"], "a удалён выпуском, c — владельцем, d правил владелец"
    assert "gyms[c]" in report.conflicts, "правку выпуска в удалённой владельцем записи — в журнал"
    assert "gyms[d]" in report.conflicts, "удаление выпуском записи, которую правил владелец, — в журнал"


def test_a_release_added_entry_lands_after_its_neighbour() -> None:
    base, ours = _doc(a={}, c={}), _doc(a={}, c={}, z={})
    theirs = _doc(a={}, b={}, c={})
    merge3(base, ours, theirs)
    assert [gym["id"] for gym in ours["gyms"]] == ["a", "b", "c", "z"]


# --------------------------------------------------------------------------- #
# Перепроверка 12.11.2026
# --------------------------------------------------------------------------- #
def test_a_settlement_typed_differently_is_the_same_place(client: Any, sandbox: Any) -> None:
    """«костанай» и «Карабалык » с пробелом — уже известные места, а не новый пригород и дубль."""
    _new_gym(client, settlement="костанай")
    _new_gym(client, scope="region", settlement=" карабалык ", title_ru="Карабалык 2", address_ru="школа №2")

    gyms = {gym["id"]: gym for gym in _raw(sandbox.kb_dir, "gyms.yaml")["gyms"]}
    assert gyms["bereke_arena"]["settlement"] == "Костанай"
    assert "костанай" not in (_raw(sandbox.kb_dir, "gyms.yaml").get("city_suburbs") or [])
    assert gyms["karabalyk_2"]["settlement"] == "Карабалык"
    assert _raw(sandbox.kb_dir, "pricing.yaml")["region_settlements"].count("Карабалык") == 1


def test_reuploading_the_same_video_keeps_it_when_the_save_fails(client: Any, sandbox: Any, monkeypatch) -> None:
    from crm.kbio import KBEditError

    _new_gym(client)
    _upload(client, "bereke_arena", b"same-video")
    route = next(a for a in _raw(sandbox.kb_dir, "media.yaml")["artifacts"] if a["id"] == "route_bereke_arena")

    def refuse(self, documents, **kwargs):
        raise KBEditError("правка отклонена", ["проверка"])

    monkeypatch.setattr(KBEditor, "save", refuse)
    page = _upload(client, "bereke_arena", b"same-video")

    assert "правка отклонена" in page
    assert (sandbox.media_dir / route["file_path"]).is_file(), "рабочее видео дороги не удалено"


def test_whole_hours_stay_whole(client: Any, sandbox: Any) -> None:
    fields = form_fields(client.get("/kb/policies").data.decode())
    client.post("/kb/policies", data=fields, follow_redirects=True)
    doc = _raw(sandbox.kb_dir, "policies.yaml")
    rules = {rule["event"]: rule for rule in doc["followup_policy"]}
    assert isinstance(rules["fu_value"]["delay_hours"], int), "48, а не 48.0"
    assert isinstance(doc["trial_min_lead_hours"], int)
    assert rules["fu_soft"]["delay_hours"] == 0.5


def test_entries_added_on_both_sides_to_an_empty_list_are_all_kept() -> None:
    base, ours, theirs = {"entries": []}, {"entries": [{"id": "crm"}]}, {"entries": [{"id": "release"}]}
    report = merge3(base, ours, theirs)
    assert [entry["id"] for entry in ours["entries"]] == ["release", "crm"]
    assert report.conflicts == []


def test_lists_of_values_are_merged_whole() -> None:
    """Дни занятий не склеиваются по элементам: поменяли обе стороны — остаётся правка владельца."""
    base, ours, theirs = {"days": ["mon"]}, {"days": ["mon", "wed"]}, {"days": ["tue"]}
    report = merge3(base, ours, theirs)
    assert ours["days"] == ["mon", "wed"] and report.conflicts == ["days"]


async def test_the_bot_tools_see_a_gym_added_in_crm(client: Any, editor: KBEditor) -> None:
    """Зал из CRM есть в списке залов, который видит модель, и get_schedule отдаёт его карточку."""
    from datetime import datetime, timezone
    from uuid import uuid4

    from app.tools import registry
    from app.tools.schedule import get_schedule
    from app.types import ChannelKind, ToolContext
    from tests.conftest import CHANNEL_ID, RecordingServices

    _new_gym(client)
    kb = editor.snapshot()

    specs = {spec.name: spec for spec in registry.build_tool_specs(kb)}
    enum = specs["get_schedule"].parameters["properties"]["gym_id"]["enum"]
    assert "bereke_arena" in enum
    assert "gym_location_bereke_arena" in specs["send_content"].parameters["properties"]["artifact_id"]["enum"]

    services = RecordingServices()
    ctx = ToolContext(
        conversation_id=uuid4(), conv_key="conv:test", channel=ChannelKind.WHATSAPP, channel_id=CHANNEL_ID,
        chat_id="77015550401", lang=Language.RU, kb=kb, kb_hash=kb.kb_hash,
        now=datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc), correlation_id="test", services=services,
    )
    result = await get_schedule(ctx, gym_id="bereke_arena")
    assert result.ok, result
    assert any("Пн, Ср · 18:00–19:30" in (message.text or "") for message in services.outbound), services.outbound


def test_an_unset_redis_url_is_not_an_error(monkeypatch, capsys) -> None:
    """REDIS_URL не задан при встроенной очереди — каждый запуск писал ERROR «redis_url_invalid»."""
    from app.config import get_settings
    from app.workers import queue

    events: list[str] = []
    monkeypatch.setattr(queue.log, "error", lambda event, **kw: events.append(event))
    settings = get_settings().model_copy(update={"redis_url": ""})
    assert queue.redis_settings(settings).host == "localhost"
    queue.redis_settings(settings.model_copy(update={"redis_url": "::not a url::"}))
    assert events == ["redis_url_invalid"], "кривой адрес по-прежнему виден"
