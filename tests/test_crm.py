"""CRM: правки базы знаний, настройки и мониторинг.

Главное, что проверяется здесь, — не «страница открылась», а два свойства, ради
которых CRM вообще существует:

* **невалидная правка не доезжает до бота**: файл откатывается целиком, а бот
  продолжает отвечать по прежней версии;
* **валидная правка доезжает без перезапуска**: наблюдатель за каталогом
  подменяет снимок, а настройки владельца попадают в инструкцию модели.
"""

from __future__ import annotations

import shutil
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from app.admin.admin_store import AdminStore
from app.admin.runtime_settings import RuntimeSettings, load_runtime_settings
from app.core.kb_watch import KBWatcher
from app.kb import loader as kb_loader
from app.storage.models import Base
from crm.app import create_app
from crm.config import CrmConfig
from crm.kbio import KBEditError, KBEditor

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Песочница: своя копия базы знаний, своя база диалогов
# --------------------------------------------------------------------------- #
@pytest.fixture
def sandbox(tmp_path: Path) -> CrmConfig:
    """Полная копия рабочих данных во временном каталоге.

    Тесты правят базу знаний по-настоящему — на копии, а не на рабочем каталоге:
    иначе один упавший тест испортил бы данные живого бота.
    """
    shutil.copytree(ROOT / "kb", tmp_path / "kb")
    shutil.copytree(ROOT / "media", tmp_path / "media")

    bot_db = tmp_path / "bot.db"
    engine = sa.create_engine(f"sqlite:///{bot_db}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversation (id, conv_key, channel_id, chat_type, chat_id,"
                " contact_name, lang, lang_locked, state, msg_in_count, msg_out_count,"
                " bot_miss_count, followup_stage, followup_blocked, created_at, updated_at,"
                " first_inbound_at, last_inbound_at)"
                " VALUES ('c1', 'wa:whatsapp:7700', 'wa', 'whatsapp', '7700', 'Гульнара', 'ru',"
                " 0, 'new', 3, 3, 0, 0, 0, '2026-08-18 10:00:00', '2026-08-18 10:00:00',"
                " '2026-08-18 10:00:00', '2026-08-18 10:05:00')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO message (id, conversation_id, direction, author, msg_type,"
                " text_raw, status, created_at)"
                " VALUES ('m1', 'c1', 'in', 'client', 'text', 'Сколько стоит?', 'inbound',"
                " '2026-08-18 10:00:00')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO lead (id, conversation_id, created_at, updated_at, channel,"
                " child_name, child_age, child_gender, phone_source, status, escalation,"
                " messages_count)"
                " VALUES ('l1', 'c1', '2026-08-18 10:05:00', '2026-08-18 10:05:00', 'whatsapp',"
                " 'Алия', 8, 'female', 'none', 'trial_booked', 0, 4)"
            )
        )
        # Ответ бота лежит в очереди отправки, а не в message: отправкой
        # занимается воркер. Без этой строки проверка не увидела бы, что
        # оператору показывают только половину разговора.
        conn.execute(
            sa.text(
                "INSERT INTO outbox_message (id, conversation_id, crm_message_id, payload,"
                " state, attempts, created_at, updated_at)"
                " VALUES ('o1', 'c1', 'o1', :payload, 'sent', 1,"
                " '2026-08-18 10:01:00', '2026-08-18 10:01:00')"
            ),
            {
                "payload": '{"kind": "bot_reply", "text": "Абонемент в Костанае — 25 000 тенге.",'
                ' "artifact_id": null}'
            },
        )
        conn.execute(
            sa.text(
                "INSERT INTO escalation_state (conversation_id, paused, escalation_count,"
                " resume_policy) VALUES ('c1', 0, 0, 'timeout')"
            )
        )
    engine.dispose()

    return CrmConfig(
        root=tmp_path,
        kb_dir=tmp_path / "kb",
        media_dir=tmp_path / "media",
        schema_version=1,
        admin_db=tmp_path / "admin.db",
        state_db=tmp_path / "state.db",
        bot_db=bot_db,
        password="Azamat65",
        timezone="Asia/Almaty",
        secret_key="test-secret",
    )


@pytest.fixture
def client(sandbox: CrmConfig):
    """Вошедший в CRM клиент."""
    app = create_app(sandbox)
    app.config["TESTING"] = True
    web = app.test_client()
    web.post("/login", data={"password": sandbox.password})
    return web


@pytest.fixture
def editor(sandbox: CrmConfig) -> KBEditor:
    """Редактор базы знаний песочницы."""
    return KBEditor(
        sandbox.kb_dir, media_dir=sandbox.media_dir, schema_version=sandbox.schema_version
    )


class _Form(HTMLParser):
    """Достаёт поля формы из отданной страницы — так же, как их отправит браузер.

    Множественные поля (галочки дней, списки материалов с ``multiple``) собираются
    в список, а не затираются последним значением. Это не педантизм: пока парсер
    оставлял от списка одно значение, проверка «сохранили без изменений» не
    замечала, что зал теряет привязанные видео и отметки пробелов.
    """

    def __init__(self) -> None:
        super().__init__()
        self.data: dict[str, Any] = {}
        self._select: str | None = None
        self._multiple = False
        self._textarea: str | None = None

    def _add(self, name: str, value: str, *, multiple: bool) -> None:
        """Кладёт значение: списком, если поле допускает несколько значений."""
        if not multiple:
            self.data[name] = value
            return
        current = self.data.get(name)
        if isinstance(current, list):
            current.append(value)
        else:
            self.data[name] = [value]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: (value or "") for key, value in attrs}
        name = attributes.get("name")
        if tag == "input" and name:
            kind = attributes.get("type", "text")
            if kind == "checkbox":
                # Невыбранную галочку браузер не отправляет вовсе.
                if "checked" in attributes:
                    self._add(name, attributes.get("value", "on"), multiple=True)
            elif kind != "file":
                self.data[name] = attributes.get("value", "")
        elif tag == "select" and name:
            self._select = name
            self._multiple = "multiple" in attributes
            if not self._multiple:
                self.data.setdefault(name, "")
        elif tag == "option" and self._select and "selected" in attributes:
            self._add(self._select, attributes.get("value", ""), multiple=self._multiple)
        elif tag == "textarea" and name:
            self._textarea = name
            self.data.setdefault(name, "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._select = None
            self._multiple = False
        if tag == "textarea":
            self._textarea = None

    def handle_data(self, data: str) -> None:
        if self._textarea:
            self.data[self._textarea] = data


def form_fields(html: str) -> dict[str, Any]:
    """Поля формы страницы в виде, готовом к отправке."""
    parser = _Form()
    parser.feed(html)
    return parser.data


# --------------------------------------------------------------------------- #
# Доступ
# --------------------------------------------------------------------------- #
def test_anonymous_redirected(sandbox: CrmConfig) -> None:
    """Без входа не показывается ничего, включая данные клиентов."""
    web = create_app(sandbox).test_client()
    for url in ("/", "/clients/", "/leads/", "/kb/pricing", "/settings/"):
        response = web.get(url)
        assert response.status_code == 302, url
        assert "/login" in response.headers["Location"]


def test_wrong_password_rejected(sandbox: CrmConfig) -> None:
    """Чужой пароль не пускает."""
    web = create_app(sandbox).test_client()
    response = web.post("/login", data={"password": "Azamat64"}, follow_redirects=True)
    assert "не подошёл" in response.data.decode()
    assert web.get("/").status_code == 302


def test_bruteforce_locked(sandbox: CrmConfig) -> None:
    """Перебор пароля упирается в паузу, а не продолжается бесконечно."""
    import crm.app as crm_app

    crm_app._ATTEMPTS.clear()
    web = create_app(sandbox).test_client()
    for _ in range(8):
        web.post("/login", data={"password": "нет"})
    response = web.post("/login", data={"password": "нет"})
    assert response.status_code == 429
    crm_app._ATTEMPTS.clear()


def test_pages_render(client: Any) -> None:
    """Все разделы открываются."""
    for url in (
        "/", "/gaps", "/clients/", "/leads/", "/kb/gyms/", "/kb/gyms/schedule",
        "/kb/pricing", "/kb/faq", "/kb/media", "/kb/policies", "/kb/texts",
        "/kb/lexicon", "/kb/backups", "/kb/raw/gyms.yaml", "/settings/", "/settings/admins",
    ):
        assert client.get(url).status_code == 200, url


# --------------------------------------------------------------------------- #
# Клиенты и заявки
# --------------------------------------------------------------------------- #
def test_client_list_shows_channel(client: Any) -> None:
    """В списке клиентов виден канал, из которого написал человек."""
    body = client.get("/clients/").data.decode()
    assert "Гульнара" in body
    assert "WhatsApp" in body


def test_dialog_and_lead_visible(client: Any) -> None:
    """В карточке клиента видны переписка и заявка."""
    body = client.get("/clients/c1").data.decode()
    assert "Сколько стоит?" in body
    assert "Алия" in body


def test_dialog_shows_bot_answers(client: Any) -> None:
    """В переписке видно и вопрос клиента, и ответ бота.

    Реплики хранятся в разных таблицах: входящие в ``message``, ответы бота в
    очереди отправки. Пока CRM читала одну, оператор видел вопросы без ответов —
    по такой ленте невозможно понять, что в диалоге вообще произошло.
    """
    body = client.get("/clients/c1").data.decode()
    assert "Сколько стоит?" in body
    assert "Абонемент в Костанае — 25 000 тенге." in body
    assert body.index("Сколько стоит?") < body.index("Абонемент в Костанае")


def test_leads_export_csv(client: Any) -> None:
    """Заявки выгружаются в CSV, пригодный для Excel."""
    response = client.get("/leads/export.csv")
    assert response.status_code == 200
    text = response.data.decode("utf-8")
    assert text.startswith("﻿"), "без BOM Excel покажет кириллицу иероглифами"
    assert "Алия" in text


def test_pause_and_resume(client: Any, sandbox: CrmConfig) -> None:
    """Пауза бота ставится и снимается — в базе и в быстром ключе состояния."""
    import sqlite3

    client.post("/clients/c1/pause", data={"minutes": "30"})
    with sqlite3.connect(sandbox.bot_db) as conn:
        assert conn.execute("SELECT paused FROM escalation_state WHERE conversation_id='c1'").fetchone()[0] == 1

    client.post("/clients/c1/resume")
    with sqlite3.connect(sandbox.bot_db) as conn:
        row = conn.execute(
            "SELECT paused, paused_until FROM escalation_state WHERE conversation_id='c1'"
        ).fetchone()
    assert row[0] == 0 and row[1] is None


# --------------------------------------------------------------------------- #
# Правка базы знаний
# --------------------------------------------------------------------------- #
def test_gym_form_roundtrip_keeps_data(client: Any, editor: KBEditor) -> None:
    """Открыть зал и сохранить без изменений — данные не теряются.

    Форма собирает зал заново, поэтому забытое в шаблоне поле молча исчезло бы
    из базы знаний. Проверяем именно это.
    """
    before = editor.load("gyms.yaml")["gyms"][0]
    page = client.get("/kb/gyms/ksk_kairbekova_334").data.decode()
    fields = form_fields(page)
    client.post("/kb/gyms/ksk_kairbekova_334", data=fields, follow_redirects=True)

    after = editor.load("gyms.yaml")["gyms"][0]
    for key in ("id", "scope", "settlement", "map_url", "district_aliases", "is_head"):
        assert after.get(key) == before.get(key), key
    assert after["title"]["ru"] == before["title"]["ru"]
    assert after["address"]["ru"] == before["address"]["ru"]
    # Связи теряются тише всего: зал перестаёт отправлять видео маршрута, и
    # заметить это можно только в живом диалоге.
    assert after["media"] == before["media"]
    assert after["gap_refs"] == before["gap_refs"]
    assert len(after["schedule"]) == len(before["schedule"])
    assert [slot["days"] for slot in after["schedule"]] == [
        slot["days"] for slot in before["schedule"]
    ]
    assert [slot["time_start"] for slot in after["schedule"]] == [
        slot["time_start"] for slot in before["schedule"]
    ]


def test_gym_edit_reaches_bot(client: Any, editor: KBEditor, sandbox: CrmConfig) -> None:
    """Правка зала попадает к боту без перезапуска — через наблюдателя каталога."""
    snapshot, _ = kb_loader.load_sync(
        sandbox.kb_dir, media_dir=sandbox.media_dir, schema_version=1
    )
    kb_loader.swap(snapshot)
    watcher = KBWatcher(
        sandbox.kb_dir, media_dir=sandbox.media_dir, schema_version=1, min_interval_s=0
    )
    assert watcher.check() is None, "без правок перезагрузки быть не должно"

    page = client.get("/kb/gyms/plaza_szm_70").data.decode()
    fields = form_fields(page)
    fields["address_ru"] = "Проспект Аль-Фараби 70, второй этаж"
    client.post("/kb/gyms/plaza_szm_70", data=fields, follow_redirects=True)

    # В бою CRM и бот — разные процессы, и снимок бота правка не трогает. В тесте
    # процесс один, поэтому возвращаем боту его прежний снимок: иначе проверялось
    # бы то, что CRM обновила себя, а не то, что правка дошла до бота.
    kb_loader.swap(snapshot)
    result = watcher.check()
    assert result is not None and result.changed
    assert result.old_hash == snapshot.kb_hash
    gym = kb_loader.get_snapshot().gym("plaza_szm_70")
    assert gym is not None and "второй этаж" in (gym.address.ru or "")


def test_invalid_edit_rolled_back(editor: KBEditor, sandbox: CrmConfig) -> None:
    """Невалидная правка откатывается целиком, база остаётся рабочей."""
    before = (sandbox.kb_dir / "pricing.yaml").read_text(encoding="utf-8")
    document = editor.load("pricing.yaml")
    document["city_plans"]["standard"]["price"] = -1

    with pytest.raises(KBEditError) as info:
        editor.save({"pricing.yaml": document})

    assert info.value.errors, "ошибки обязаны быть перечислены, а не скрыты"
    assert (sandbox.kb_dir / "pricing.yaml").read_text(encoding="utf-8") == before
    editor.snapshot()  # база по-прежнему загружается


def test_broken_kb_does_not_break_bot(sandbox: CrmConfig) -> None:
    """Если базу знаний сломали руками, бот работает на прежнем снимке."""
    snapshot, _ = kb_loader.load_sync(
        sandbox.kb_dir, media_dir=sandbox.media_dir, schema_version=1
    )
    kb_loader.swap(snapshot)
    watcher = KBWatcher(
        sandbox.kb_dir, media_dir=sandbox.media_dir, schema_version=1, min_interval_s=0
    )

    (sandbox.kb_dir / "gyms.yaml").write_text("gyms: [", encoding="utf-8")
    assert watcher.check() is None
    assert watcher.last_error is not None
    assert kb_loader.get_snapshot().kb_hash == snapshot.kb_hash, "снимок не должен был смениться"


def test_comments_survive_edit(client: Any, sandbox: CrmConfig) -> None:
    """Комментарии в YAML переживают сохранение из CRM.

    В ``pricing.yaml`` комментариями объяснены решения — почему оговорка звучит
    до продажи, что означает конфликт C-4. Стирать их при каждой правке значило
    бы медленно уничтожать знание о системе.
    """
    page = client.get("/kb/pricing").data.decode()
    fields = form_fields(page)
    client.post("/kb/pricing", data=fields, follow_redirects=True)
    text = (sandbox.kb_dir / "pricing.yaml").read_text(encoding="utf-8")
    assert "КОНФЛИКТ C-4" in text
    assert text.count("#") > 30


def test_faq_create_and_delete(client: Any, editor: KBEditor) -> None:
    """Вопрос добавляется и удаляется."""
    client.post(
        "/kb/faq/new",
        data={
            "form_complete": "1",
            "id": "test_question",
            "topic": "trial",
            "scope": "any",
            "answer_ru": "Да, конечно.",
            "answer_kk": "Иә, әрине.",
            "variants_ru": "а можно?\nразрешено?",
            "source": "owner_confirmed",
            "escalate_if_empty": "1",
        },
        follow_redirects=True,
    )
    ids = [entry["id"] for entry in editor.load("faq.yaml")["entries"]]
    assert "test_question" in ids

    client.post("/kb/faq/test_question/delete", follow_redirects=True)
    ids = [entry["id"] for entry in editor.load("faq.yaml")["entries"]]
    assert "test_question" not in ids


def test_schedule_paste(client: Any, editor: KBEditor) -> None:
    """Расписание, вставленное текстом из WhatsApp, разбирается и заменяет прежнее."""
    text = (
        "🥊 КИКБОКСИНГ\n"
        "Понедельник • Среда • Пятница\n"
        "🕘 09:00–10:30\n"
        "🕔 17:00–18:30\n"
    )
    client.post(
        "/kb/gyms/region_karabalyk/schedule-text", data={"text": text}, follow_redirects=True
    )
    gym = next(
        gym for gym in editor.load("gyms.yaml")["gyms"] if gym["id"] == "region_karabalyk"
    )
    assert len(gym["schedule"]) == 2
    assert gym["schedule"][0]["days"] == ["mon", "wed", "fri"]


def test_schedule_paste_garbage_rejected(client: Any, editor: KBEditor) -> None:
    """Непонятый текст не стирает расписание молча."""
    before = next(
        gym for gym in editor.load("gyms.yaml")["gyms"] if gym["id"] == "ksk_kairbekova_334"
    )["schedule"]
    response = client.post(
        "/kb/gyms/ksk_kairbekova_334/schedule-text",
        data={"text": "завтра как обычно"},
        follow_redirects=True,
    )
    assert "не разобрано" in response.data.decode()
    after = next(
        gym for gym in editor.load("gyms.yaml")["gyms"] if gym["id"] == "ksk_kairbekova_334"
    )["schedule"]
    assert len(after) == len(before)


def test_backup_and_restore(client: Any, editor: KBEditor) -> None:
    """Откат возвращает базу знаний к состоянию до правки."""
    page = client.get("/kb/gyms/mkr6_arystanbekova_6").data.decode()
    fields = form_fields(page)
    original = fields["title_ru"]
    fields["title_ru"] = "Изменённое название"
    client.post("/kb/gyms/mkr6_arystanbekova_6", data=fields, follow_redirects=True)

    backups = editor.backups()
    assert backups, "копия обязана создаваться перед каждой правкой"
    client.post(f"/kb/backups/{backups[0].stamp}/restore", follow_redirects=True)

    gym = next(
        gym for gym in editor.load("gyms.yaml")["gyms"] if gym["id"] == "mkr6_arystanbekova_6"
    )
    assert gym["title"]["ru"] == original


def test_media_toggle(client: Any, editor: KBEditor) -> None:
    """Материал выключается — и бот перестаёт его отправлять."""
    artifact = editor.load("media.yaml")["artifacts"][0]
    page = client.get(f"/kb/media/{artifact['id']}").data.decode()
    fields = form_fields(page)
    fields.pop("enabled", None)  # снятая галочка не отправляется браузером
    client.post(f"/kb/media/{artifact['id']}", data=fields, follow_redirects=True)
    assert editor.load("media.yaml")["artifacts"][0]["enabled"] is False


def test_texts_edit(client: Any, editor: KBEditor) -> None:
    """Готовая фраза меняется через интерфейс."""
    page = client.get("/kb/texts").data.decode()
    fields = form_fields(page)
    key = next(name for name in fields if name.startswith("s::") and name.endswith("::ru"))
    fields[key] = "Обновлённая фраза."
    client.post("/kb/texts", data=fields, follow_redirects=True)
    plain = key.split("::")[1]
    assert editor.load("i18n.yaml")["strings"][plain]["ru"] == "Обновлённая фраза."


# --------------------------------------------------------------------------- #
# Настройки доходят до бота
# --------------------------------------------------------------------------- #
def test_settings_saved_and_read_by_bot(client: Any, sandbox: CrmConfig) -> None:
    """Настройка из CRM читается тем же кодом, которым её читает бот."""
    client.post(
        "/settings/",
        data={"form_complete": "1", "quiet_hours": "22:00-08:00", "work_hours": "09:00-19:00"},
        follow_redirects=True,
    )
    runtime = load_runtime_settings(sandbox.admin_db)
    assert runtime.quiet_start == 22 and runtime.quiet_end == 8
    assert runtime.work_start == "09:00" and runtime.work_end == "19:00"
    # Выключенные галочки браузер не присылает — значит, они выключены.
    assert runtime.followup_enabled is False and runtime.lead_notify is False


def test_settings_reach_the_model(client: Any, sandbox: CrmConfig) -> None:
    """Часы работы и платность пробного попадают в инструкцию модели."""
    client.post(
        "/settings/",
        data={
            "form_complete": "1",
            "quiet_hours": "21:00-09:00",
            "work_hours": "11:00-21:00",
            "followup_enabled": "1",
        },
        follow_redirects=True,
    )
    block = load_runtime_settings(sandbox.admin_db).prompt_block()
    assert "с 11:00 до 21:00" in block
    assert "ПЕРВОЕ ЗАНЯТИЕ СЕЙЧАС ПЛАТНОЕ" in block, "галочка снята — модель обязана это знать"


def test_bad_time_range_rejected(client: Any, sandbox: CrmConfig) -> None:
    """Время в неверном формате не сохраняется."""
    response = client.post(
        "/settings/",
        data={"form_complete": "1", "quiet_hours": "вечером", "work_hours": "10:00-20:00"},
        follow_redirects=True,
    )
    assert "21:00-09:00" in response.data.decode()
    assert load_runtime_settings(sandbox.admin_db).quiet_start == 21


def test_settings_apply_to_followups(sandbox: CrmConfig) -> None:
    """Выключенные напоминания доходят до конфигурации, по которой работает воркер."""
    from app.config import Settings

    store = AdminStore(sandbox.admin_db)
    store.set("followup_enabled", "off")
    store.set("quiet_hours", "23:00-07:00")
    store.close()

    base = Settings(
        wazzup_api_key="x", wazzup_webhook_secret="y" * 24, gemini_api_key="z"
    )
    applied = load_runtime_settings(sandbox.admin_db).apply_to(base)
    assert applied.followup_enabled is False
    assert applied.followup_quiet_hours_start == 23
    assert applied.followup_quiet_hours_end == 7
    assert base.followup_enabled is True, "исходная конфигурация меняться не должна"


def test_admins_managed(client: Any, sandbox: CrmConfig) -> None:
    """Администратор добавляется, последнего снять нельзя."""
    client.post("/settings/admins", data={"telegram_id": "111", "title": "Азамат"})
    store = AdminStore(sandbox.admin_db)
    assert [admin.telegram_id for admin in store.admins()] == [111]

    client.post("/settings/admins", data={"action": "remove", "telegram_id": "111"})
    assert [admin.telegram_id for admin in store.admins()] == [111], "последний админ несменяем"

    client.post("/settings/admins", data={"telegram_id": "222"})
    client.post("/settings/admins", data={"action": "remove", "telegram_id": "111"})
    assert [admin.telegram_id for admin in store.admins()] == [222]
    store.close()


def test_runtime_defaults_survive_missing_db(tmp_path: Path) -> None:
    """Нет базы настроек — бот работает на значениях по умолчанию, а не падает."""
    runtime = load_runtime_settings(tmp_path / "нет-такого" / "admin.db")
    assert runtime == RuntimeSettings.defaults() or runtime.followup_enabled is True


def test_partial_post_does_not_wipe_gym(client: Any, editor: KBEditor) -> None:
    """Неполная форма отклоняется, а не стирает расписание и материалы.

    Так и случилось на живом стенде: запрос, в котором не было полей занятий,
    сохранился как «занятий нет» — расписание зала исчезло молча. Теперь форма
    подтверждает свою полноту скрытым полем, а без него правка не принимается.
    """
    before = next(
        gym for gym in editor.load("gyms.yaml")["gyms"] if gym["id"] == "center_kasymkhanova_10"
    )
    assert before["schedule"] and before["media"], "проверять нужно на заполненном зале"

    response = client.post(
        "/kb/gyms/center_kasymkhanova_10",
        data={"id": "center_kasymkhanova_10", "title_ru": "Центр", "address_ru": "Адрес"},
        follow_redirects=True,
    )
    assert "не полностью" in response.data.decode()

    after = next(
        gym for gym in editor.load("gyms.yaml")["gyms"] if gym["id"] == "center_kasymkhanova_10"
    )
    assert len(after["schedule"]) == len(before["schedule"])
    assert after["media"] == before["media"]


def test_healthz_open_and_honest(sandbox: CrmConfig) -> None:
    """Проверка живости отвечает без пароля и показывает версию базы знаний.

    Без пароля — потому что у проверяльщика Railway пароля нет; закрытый
    healthz означал бы «сервис нездоров» при каждом опросе.
    """
    web = create_app(sandbox).test_client()
    response = web.get("/healthz")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok"
    assert len(body["kb"]) == 12 and body["kb"] != "invalid"


def test_healthz_reports_broken_kb(sandbox: CrmConfig) -> None:
    """Сломанная база знаний видна в проверке живости, а не прячется."""
    (sandbox.kb_dir / "gyms.yaml").write_text("gyms: [", encoding="utf-8")
    web = create_app(sandbox).test_client()
    assert web.get("/healthz").get_json()["kb"] == "invalid"


def test_fresh_install_without_bot_db(sandbox: CrmConfig, tmp_path: Path) -> None:
    """Свежий деплой: бот ещё не запускался, базы диалогов нет — CRM работает.

    Это первое, что видит владелец после развёртывания. Страница с ошибкой на
    старте выглядит как «ничего не работает», хотя всё в порядке.
    """
    empty = CrmConfig(
        root=sandbox.root,
        kb_dir=sandbox.kb_dir,
        media_dir=sandbox.media_dir,
        schema_version=1,
        admin_db=tmp_path / "admin-new.db",
        state_db=tmp_path / "state-new.db",
        bot_db=tmp_path / "нет-такой.db",
        password=sandbox.password,
        timezone=sandbox.timezone,
        secret_key="test",
    )
    web = create_app(empty).test_client()
    web.post("/login", data={"password": empty.password})
    for url in ("/", "/clients/", "/leads/", "/kb/gyms/", "/settings/"):
        assert web.get(url).status_code == 200, url


def test_search_finds_russian_names(client: Any) -> None:
    """Поиск по имени работает с кириллицей.

    Родная ``LOWER()`` в SQLite приводит регистр только у латиницы, поэтому
    «Гульнара» не находилась по запросу «гуль» — то есть поиск по именам
    клиентов не работал вовсе.
    """
    assert "Гульнара" in client.get("/clients/?q=гуль").data.decode()
    assert "Гульнара" in client.get("/clients/?q=ГУЛЬНАРА").data.decode()


def test_cross_site_post_blocked(client: Any, editor: KBEditor) -> None:
    """Запрос с чужой страницы не меняет данные.

    CRM выставлена в интернет, и чужой сайт может отправить форму на её адрес
    вместе с сессионной кукой. Без проверки источника так менялись бы цены и
    снимались администраторы.
    """
    before = editor.load("policies.yaml").get("sla_reply_minutes")
    response = client.post(
        "/kb/policies",
        data={"form_complete": "1", "org_brand": "Чужой", "org_city": "Чужой",
              "sla_reply_minutes": "999", "escalation_pause_minutes": "60"},
        headers={"Origin": "https://зло.example"},
    )
    assert response.status_code == 403
    assert editor.load("policies.yaml").get("sla_reply_minutes") == before


def test_same_site_post_allowed(client: Any, editor: KBEditor) -> None:
    """Обычная отправка со своей же страницы проходит."""
    response = client.post(
        "/kb/policies",
        data={"form_complete": "1", "org_brand": "AINAZAROV TOP TEAM", "org_city": "Костанай",
              "escalation_pause_minutes": "60", "audience_adults_only": "1"},
        headers={"Origin": "http://localhost"},
        base_url="http://localhost",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert editor.load("policies.yaml")["org_city"] == "Костанай"


def test_partial_settings_post_rejected(client: Any, sandbox: CrmConfig) -> None:
    """Неполная форма настроек не выключает всё разом."""
    client.post(
        "/settings/",
        data={"form_complete": "1", "followup_enabled": "1", "lead_notify": "1",
              "trial_free": "1", "quiet_hours": "21:00-09:00", "work_hours": "10:00-20:00"},
        follow_redirects=True,
    )
    response = client.post("/settings/", data={"quiet_hours": "22:00-08:00"}, follow_redirects=True)
    assert "не полностью" in response.data.decode()
    runtime = load_runtime_settings(sandbox.admin_db)
    assert runtime.followup_enabled is True and runtime.lead_notify is True


def test_media_upload_and_delete(client: Any, editor: KBEditor, sandbox: CrmConfig) -> None:
    """Загрузка материала через браузер: файл ложится в media/, запись — в базу знаний."""
    import io

    # Минимальный валидный PNG: пиксель. Содержимое неважно, важен путь загрузки.
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
    )
    response = client.post(
        "/kb/media/upload",
        data={
            "file": (io.BytesIO(png), "тренировка.png"),
            "title": "Тренировка младшей группы",
            "when_to_send": "когда спрашивают, как проходят занятия",
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    body = response.data.decode()
    assert "добавлен" in body, body[:400]

    artifacts = {a["id"]: a for a in editor.load("media.yaml")["artifacts"]}
    new_id = next(key for key in artifacts if key.startswith("photo_trenirovka"))
    assert (sandbox.media_dir / artifacts[new_id]["file_path"]).is_file()
    assert artifacts[new_id]["when_to_send_ru"].startswith("когда спрашивают")

    client.post(f"/kb/media/{new_id}/delete", follow_redirects=True)
    assert new_id not in {a["id"] for a in editor.load("media.yaml")["artifacts"]}


def test_upload_rejects_unsupported_format(client: Any, editor: KBEditor) -> None:
    """Формат, который бот не умеет отправлять, отклоняется на входе.

    Файл, сохранённый под чужим расширением, дошёл бы до клиента битым, и
    разбираться пришлось бы уже по его жалобе.
    """
    import io

    before = len(editor.load("media.yaml")["artifacts"])
    response = client.post(
        "/kb/media/upload",
        data={
            "file": (io.BytesIO(b"%PDF-1.4"), "прайс.pdf"),
            "when_to_send": "когда просят прайс",
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "отправлять не умеет" in response.data.decode()
    assert len(editor.load("media.yaml")["artifacts"]) == before


def test_upload_without_description_rejected(client: Any) -> None:
    """Без описания «когда отправлять» материал бесполезен: модель не выберет его."""
    import io

    response = client.post(
        "/kb/media/upload",
        data={"file": (io.BytesIO(b"x"), "фото.jpg"), "when_to_send": ""},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "когда бот должен отправлять" in response.data.decode()
