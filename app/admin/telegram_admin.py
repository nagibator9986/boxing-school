"""Админка расписания прямо в Telegram: команда ``/admin``.

Сделана под то, как администратор школы работает уже сейчас: она присылает
расписание сообщением с эмодзи. Здесь она делает ровно то же самое — выбирает
зал и вставляет тот же текст. Бот показывает, что понял, и ждёт подтверждения.

Почему не Google Таблица: таблица требует аккаунта, раздачи доступов и сети,
а чат администратор открывает и так. Почему не построчный CRUD в чате: вводить
расписание по одному занятию с телефона мучительно, а вставить готовый текст —
одно движение.

Состояние диалога живёт в памяти процесса: правки редкие, а потеря
незавершённого шага при перезапуске ничем не грозит — админ начнёт заново.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.admin.admin_store import SETTING_SPECS, AdminStore
from app.admin.media_store import MediaWriteError, register_media
from app.admin.schedule_store import (
    ScheduleWriteError,
    apply_schedule,
    clear_schedule,
)
from app.admin.schedule_text import (
    ParsedSchedule,
    ScheduleParseError,
    parse_schedule_text,
    render_schedule_text,
)
from app.logging_conf import get_logger

__all__ = ["AdminSession", "AdminConsole", "is_admin"]

_log = get_logger(__name__)

#: Диапазон времени вида 21:00-09:00.
_TIME_RANGE_RE = __import__("re").compile(r"([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d")

HELP = (
    "Админка расписания\n\n"
    "/admin — список залов\n"
    "Дальше просто пришлите номер зала — покажу текущее расписание.\n"
    "Затем вставьте новое расписание тем же текстом, каким вы его обычно пишете.\n\n"
    "/admin_off — выйти из режима правки\n"
    "Внутри правки: «удалить» — очистить расписание зала, «отмена» — выйти.\n\n"
    "ФОТО И ВИДЕО\n"
    "Пришлите файл боту и в подписи к нему одной строкой напишите, "
    "когда его показывать клиенту. Например:\n"
    "«тренировка младшей группы — когда спрашивают, как проходят занятия»\n"
    "Имя файла значения не имеет: бот выбирает материал по вашему описанию."
)


def is_admin(user_id: int | str | None, allowed: tuple[int, ...]) -> bool:
    """Разрешено ли этому пользователю править расписание.

    Пустой список означает «админов не задали»: тогда админка открыта, и об этом
    громко пишется в лог. Так тестовый бот поднимается за минуту, но в проде
    ``TELEGRAM_ADMIN_IDS`` обязателен.
    """
    if not allowed:
        _log.warning("admin_ids_not_set", hint="задайте TELEGRAM_ADMIN_IDS в .env")
        return True
    try:
        return int(user_id) in allowed  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


@dataclass(slots=True)
class AdminSession:
    """Где администратор находится в диалоге правки."""

    step: str = "idle"  # idle | menu | picked | confirm | settings | setting_value
    gym_id: str | None = None
    pending: ParsedSchedule | None = None
    warnings: list[str] = field(default_factory=list)
    setting_key: str | None = None


class AdminConsole:
    """Обработчик команд администратора. Одна на процесс, состояние — по чатам."""

    def __init__(
        self,
        *,
        kb_dir: Path,
        media_dir: Path,
        schema_version: int,
        snapshot: Callable[[], Any],
        store: AdminStore | None = None,
    ) -> None:
        self._kb_dir = kb_dir
        self._media_dir = media_dir
        self._schema_version = schema_version
        self._snapshot = snapshot
        self._store = store
        self._sessions: dict[str, AdminSession] = {}

    # ------------------------------------------------------------------ вход
    def session(self, chat_id: str) -> AdminSession:
        """Сессия правки для чата."""
        return self._sessions.setdefault(chat_id, AdminSession())

    def active(self, chat_id: str) -> bool:
        """Находится ли администратор внутри режима правки."""
        return self._sessions.get(chat_id, AdminSession()).step != "idle"

    def _gyms(self) -> list[Any]:
        """Залы в стабильном порядке: город сначала, затем область.

        Порядок обязан быть устойчивым между вызовами: администратор выбирает зал
        номером, и если список переставится между показом и выбором, правка уедет
        не в тот зал.
        """
        from app.types import Scope

        gyms = list(self._snapshot().active_gyms(Scope.ALL))
        return sorted(gyms, key=lambda g: (0 if g.scope is Scope.CITY else 1, g.id))

    # --------------------------------------------------------------- команды
    def add_media(
        self,
        chat_id: str,
        *,
        source: Path,
        kind: str,
        caption: str,
        gym_id: str | None = None,
    ) -> str:
        """Регистрирует присланный администратором файл. Возвращает ответ ему."""
        description = (caption or "").strip()
        if not description:
            return (
                "Файл получил, но не понял, когда его показывать.\n"
                "Пришлите ещё раз и добавьте подпись — одной строкой, например:\n"
                "«тренировка младшей группы — когда спрашивают, как проходят занятия»"
            )
        title = description.split("—")[0].split(",")[0].strip()[:60] or description[:60]
        try:
            result = register_media(
                kb_dir=self._kb_dir,
                media_dir=self._media_dir,
                schema_version=self._schema_version,
                source=source,
                kind=kind,
                when_to_send=description,
                title_ru=title,
                gym_id=gym_id,
            )
        except MediaWriteError as exc:
            return f"Не получилось сохранить: {exc}"

        where = (
            "Отправляется во все каналы."
            if kind == "image"
            else "Видео уходит только в Telegram: в WhatsApp мешает лимит 10 МБ, "
            "а в Instagram видео не отправляется вовсе — там бот ответит текстом."
        )
        return (
            f"Сохранил: {result.artifact_id}\n"
            f"Размер: {result.size_bytes / 1048576:.1f} МБ\n"
            f"Показываю, когда: {description}\n\n{where}"
        )

    def handle(self, chat_id: str, text: str) -> str:
        """Обрабатывает сообщение администратора и возвращает ответ."""
        command = (text or "").strip()
        low = command.lower()
        session = self.session(chat_id)

        if low in ("/admin_off", "отмена", "/cancel"):
            self._sessions[chat_id] = AdminSession()
            return "Вышли из режима правки. Бот снова отвечает клиентам."

        if low.startswith("/admin"):
            self._sessions[chat_id] = AdminSession(step="menu")
            return self._main_menu()

        if session.step == "menu":
            return self._on_menu(chat_id, command)

        if session.step == "settings":
            return self._on_settings_pick(chat_id, command)

        if session.step == "setting_value":
            return self._on_setting_value(chat_id, command)

        if session.step == "confirm":
            return self._on_confirm(chat_id, low)

        if session.step == "picked":
            if session.gym_id is None:
                return self._on_pick(chat_id, command)
            return self._on_schedule_text(chat_id, command)

        return HELP

    # ------------------------------------------------------------- главное меню
    def _main_menu(self) -> str:
        """Что администратор может сделать. Цифрой — потому что кнопок нет в WhatsApp."""
        return (
            "Управление ботом\n\n"
            "1. Расписание залов\n"
            "2. Настройки бота\n"
            "3. Администраторы\n"
            "4. Что бот пока не знает\n\n"
            "Фото и видео добавляются проще: пришлите файл с подписью, "
            "когда его показывать клиенту.\n\n"
            "Напишите цифру. «отмена» — выйти."
        )

    def _on_menu(self, chat_id: str, text: str) -> str:
        """Выбор раздела в главном меню."""
        choice = text.strip().rstrip(".)")
        if choice == "1":
            self.session(chat_id).step = "picked"
            return self._gym_list()
        if choice == "2":
            self.session(chat_id).step = "settings"
            return self._settings_list()
        if choice == "3":
            self._sessions[chat_id] = AdminSession()
            return self._admins_list()
        if choice == "4":
            self._sessions[chat_id] = AdminSession()
            return self._gaps_report()
        return "Нужна цифра от 1 до 4. Или «отмена»."

    # ---------------------------------------------------------------- настройки
    def _settings_list(self) -> str:
        """Текущие настройки с номерами для изменения."""
        if self._store is None:
            return "Настройки недоступны: хранилище не подключено."
        lines = ["Настройки бота\n"]
        for number, (spec, value) in enumerate(self._store.all_settings(), start=1):
            shown = {"on": "включено", "off": "выключено"}.get(value, value)
            lines.append(f"{number}. {spec.title}: {shown}")
        lines.append("\nПришлите номер, чтобы изменить. «отмена» — выйти.")
        return "\n".join(lines)

    def _on_settings_pick(self, chat_id: str, text: str) -> str:
        """Администратор выбрал настройку номером."""
        if self._store is None:
            return "Настройки недоступны."
        try:
            index = int(text.strip().rstrip(".)"))
        except ValueError:
            return "Нужен номер настройки из списка. Или «отмена»."
        if not (1 <= index <= len(SETTING_SPECS)):
            return f"Номер от 1 до {len(SETTING_SPECS)}."

        spec = SETTING_SPECS[index - 1]
        session = self.session(chat_id)
        session.setting_key = spec.key
        current = self._store.get(spec.key)

        if spec.kind == "bool":
            # Переключатель меняем сразу: спрашивать «включить?» после того, как
            # человек выбрал «включить», — лишний шаг.
            new_value = "off" if current == "on" else "on"
            self._store.set(spec.key, new_value)
            self._sessions[chat_id] = AdminSession()
            state = "включено" if new_value == "on" else "выключено"
            return f"{spec.title}: {state}.\n{spec.hint}"

        session.step = "setting_value"
        hint = (
            "Пришлите число минут, от 1 до 1440."
            if spec.kind == "minutes"
            else "Пришлите новое значение в формате 21:00-09:00."
        )
        return f"{spec.title}\nСейчас: {current}\n{spec.hint}\n\n{hint} «отмена» — выйти."

    def _on_setting_value(self, chat_id: str, text: str) -> str:
        """Новое значение настройки с проверкой формата."""
        session = self.session(chat_id)
        key = session.setting_key or ""
        if self._store is None or not key:
            self._sessions[chat_id] = AdminSession()
            return "Не понял, какую настройку меняем. Начните заново: /admin"

        spec = next((item for item in SETTING_SPECS if item.key == key), None)
        value = text.strip().replace(" ", "").replace("—", "-").replace("–", "-")

        if spec is not None and spec.kind == "minutes":
            if not value.isdigit() or not 1 <= int(value) <= 1440:
                return "Нужно число минут — от 1 до 1440."
            value = str(int(value))
        elif not _TIME_RANGE_RE.fullmatch(value):
            return "Нужен формат 21:00-09:00 — часы и минуты через дефис."

        self._store.set(key, value)
        self._sessions[chat_id] = AdminSession()
        return f"Сохранил: {value}"

    # ------------------------------------------------------------ администраторы
    def _admins_list(self) -> str:
        """Кто может управлять ботом."""
        if self._store is None:
            return "Список администраторов недоступен."
        admins = self._store.admins()
        if not admins:
            return "Администраторов пока нет."
        lines = ["Администраторы\n"]
        for admin in admins:
            name = admin.title or "без имени"
            lines.append(f"• {name} (id {admin.telegram_id})")
        lines.append(
            "\nЧтобы добавить: человек пишет боту пароль, и права выдаются сразу.\n"
            "Последнего администратора убрать нельзя — иначе управление потеряется."
        )
        return "\n".join(lines)

    def _gaps_report(self) -> str:
        """Чего боту не хватает: по этим вопросам он отправляет к администратору."""
        from app.types import Scope

        snapshot = self._snapshot()
        no_schedule = [
            g.title.ru or g.id
            for g in snapshot.active_gyms(Scope.ALL)
            if not g.schedule
        ]
        lines = ["Что бот пока не знает\n"]
        if no_schedule:
            lines.append("Нет расписания:")
            lines.extend(f"• {name}" for name in no_schedule)
        else:
            lines.append("Расписание есть во всех залах.")
        lines.append(
            "\nПо этим вопросам бот не выдумывает ответ, а передаёт его вам. "
            "Заполнить расписание: /admin → 1."
        )
        return "\n".join(lines)

    # ----------------------------------------------------------------- шаги
    def _gym_list(self) -> str:
        """Нумерованный список залов с числом занятий в каждом."""
        lines = ["Какой зал правим? Пришлите номер.\n"]
        for number, gym in enumerate(self._gyms(), start=1):
            count = len(gym.schedule or [])
            mark = f"{count} занятий" if count else "расписания нет"
            title = gym.title.ru or gym.id
            lines.append(f"{number}. {title} — {mark}")
        lines.append("\n«отмена» — выйти.")
        return "\n".join(lines)

    def _on_pick(self, chat_id: str, text: str) -> str:
        """Администратор прислал номер зала."""
        gyms = self._gyms()
        try:
            index = int(text.strip().rstrip("."))
        except ValueError:
            return "Нужен номер зала из списка. Или «отмена»."
        if not (1 <= index <= len(gyms)):
            return f"Номер от 1 до {len(gyms)}."

        gym = gyms[index - 1]
        session = self.session(chat_id)
        session.gym_id = gym.id
        current = render_schedule_text(
            [
                {
                    "discipline": slot.discipline,
                    "days": list(slot.days),
                    "time_start": slot.time_start,
                    "time_end": slot.time_end,
                }
                for slot in (gym.schedule or [])
            ]
        )
        title = gym.title.ru or gym.id
        return (
            f"{title}\n\nСейчас:\n{current}\n\n"
            "Пришлите новое расписание текстом — тем же, каким вы его обычно пишете.\n"
            "«удалить» — очистить расписание, «отмена» — выйти."
        )

    def _on_schedule_text(self, chat_id: str, text: str) -> str:
        """Администратор вставил расписание либо попросил очистить."""
        session = self.session(chat_id)
        gym_id = session.gym_id or ""

        if text.strip().lower() in ("удалить", "очистить", "/del"):
            try:
                result = clear_schedule(
                    self._kb_dir,
                    gym_id,
                    media_dir=self._media_dir,
                    schema_version=self._schema_version,
                )
            except ScheduleWriteError as exc:
                return f"Не получилось: {exc}"
            self._sessions[chat_id] = AdminSession()
            return (
                f"Расписание очищено (было занятий: {result.slots_before}). "
                "Бот снова будет отправлять вопрос о времени администратору."
            )

        try:
            parsed = parse_schedule_text(text)
        except ScheduleParseError as exc:
            return "Не смог разобрать:\n" + "\n".join(f"• {p}" for p in exc.problems)

        session.pending = parsed
        session.step = "confirm"
        preview = render_schedule_text(parsed.as_yaml_dicts())
        note = ""
        if parsed.warnings:
            note = "\n\nОбратите внимание:\n" + "\n".join(f"• {w}" for w in parsed.warnings)
        return (
            f"Вот что я понял — {len(parsed.slots)} занятий:\n\n{preview}{note}\n\n"
            "Сохранить? Ответьте «да» или «нет»."
        )

    def _on_confirm(self, chat_id: str, answer: str) -> str:
        """Подтверждение записи."""
        session = self.session(chat_id)
        if answer not in ("да", "сохранить", "ок", "yes", "иә"):
            self._sessions[chat_id] = AdminSession()
            return "Не сохранил, ничего не изменилось."

        if session.pending is None or session.gym_id is None:
            self._sessions[chat_id] = AdminSession()
            return "Нечего сохранять, начните заново: /admin"

        try:
            result = apply_schedule(
                self._kb_dir,
                session.gym_id,
                session.pending.as_yaml_dicts(),
                media_dir=self._media_dir,
                schema_version=self._schema_version,
            )
        except ScheduleWriteError as exc:
            self._sessions[chat_id] = AdminSession()
            return f"Не сохранил, база осталась прежней: {exc}"

        self._sessions[chat_id] = AdminSession()
        return (
            f"Готово. Было занятий: {result.slots_before}, стало: {result.slots_after}. "
            "Бот уже отвечает по новому расписанию."
        )
