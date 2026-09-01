"""Настройки, которые владелец меняет на ходу — и которые бот обязан слушать.

До этого модуля таблица ``bot_setting`` была витриной: админка её писала, а
поведение бота определялось переменными окружения, заданными при запуске.
Переключатель в интерфейсе, который ничего не переключает, хуже отсутствующего:
владелец выключает напоминания, видит «выключено» — и клиенты продолжают их
получать.

Здесь настройки превращаются в три вещи, каждая из которых доходит до бота:

* :meth:`RuntimeSettings.apply_to` — накладывает их на :class:`~app.config.Settings`,
  откуда напоминания читают своё расписание и тихие часы;
* :meth:`RuntimeSettings.prompt_block` — короткий блок в системной инструкции:
  так модель узнаёт часы работы администратора и то, платное ли первое занятие;
* :attr:`RuntimeSettings.lead_notify` — читается перед отправкой карточки.

Значения читаются из SQLite при каждом обращении. Это осознанно: таблица
крошечная, чтение локального файла стоит микросекунды, а кеш означал бы, что
правка из CRM доедет до бота «когда-нибудь».
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from app.admin.admin_store import SETTING_SPECS, AdminStore
from app.logging_conf import get_logger

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from app.config import Settings

__all__ = ["RuntimeSettings", "load_runtime_settings"]

_log = get_logger(__name__)

#: ``21:00-09:00`` → (21, 9). Формат проверяется админкой на вводе.
_RANGE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")

_DEFAULTS: Final[dict[str, str]] = {spec.key: spec.default for spec in SETTING_SPECS}


def _parse_range(value: str, fallback: tuple[int, int]) -> tuple[int, int]:
    """Диапазон времени в пару часов. Мусор → ``fallback``, а не исключение.

    Настройка приходит из базы, которую мог править человек руками; уронить из-за
    неё бота нельзя.
    """
    match = _RANGE_RE.match(value or "")
    if not match:
        return fallback
    start, end = int(match.group(1)), int(match.group(3))
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return fallback
    return start, end


def _as_minutes(value: str, fallback: int) -> int:
    """Минуты из строки настройки. Мусор и бессмыслица → ``fallback``.

    Верхняя граница — сутки: пауза длиннее означает «бот выключен в этом
    диалоге навсегда», а для этого есть отдельное решение человека, а не
    случайно введённое число.
    """
    try:
        minutes = int(str(value).strip())
    except (TypeError, ValueError):
        return fallback
    return min(1440, max(1, minutes))


def _as_bool(value: str, fallback: bool) -> bool:
    """Строка настройки в булево. Понимает ``on/off``, ``true/false``, ``1/0``."""
    text = (value or "").strip().lower()
    if text in ("on", "true", "1", "yes", "да"):
        return True
    if text in ("off", "false", "0", "no", "нет"):
        return False
    return fallback


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Снимок настроек владельца на момент чтения."""

    followup_enabled: bool = True
    quiet_start: int = 21
    quiet_end: int = 9
    work_start: str = "10:00"
    work_end: str = "20:00"
    lead_notify: bool = True
    trial_free: bool = True
    operator_pause_minutes: int = 120

    # ------------------------------------------------------------------ чтение
    @classmethod
    def defaults(cls) -> RuntimeSettings:
        """Значения по умолчанию — те же, что показывает админка."""
        return cls.from_values(dict(_DEFAULTS))

    @classmethod
    def from_values(cls, values: dict[str, str]) -> RuntimeSettings:
        """Собирает настройки из сырых строк ``key -> value``."""
        quiet = _parse_range(values.get("quiet_hours", ""), (21, 9))
        work_raw = values.get("work_hours", "") or _DEFAULTS.get("work_hours", "10:00-20:00")
        work_match = _RANGE_RE.match(work_raw)
        work_start = f"{int(work_match.group(1)):02d}:{work_match.group(2)}" if work_match else "10:00"
        work_end = f"{int(work_match.group(3)):02d}:{work_match.group(4)}" if work_match else "20:00"
        return cls(
            followup_enabled=_as_bool(values.get("followup_enabled", ""), True),
            operator_pause_minutes=_as_minutes(values.get("operator_pause_minutes", ""), 120),
            quiet_start=quiet[0],
            quiet_end=quiet[1],
            work_start=work_start,
            work_end=work_end,
            lead_notify=_as_bool(values.get("lead_notify", ""), True),
            trial_free=_as_bool(values.get("trial_free", ""), True),
        )

    # ------------------------------------------------------------ применение
    def apply_to(self, settings: Settings) -> Settings:
        """Копия конфигурации с наложенными настройками владельца.

        Возвращается копия, а не изменённый оригинал: ``Settings`` кешируется
        процессом и общий на всё приложение — править его на месте значило бы
        менять конфигурацию под ногами у соседних вызовов.
        """
        return settings.model_copy(
            update={
                "followup_enabled": self.followup_enabled,
                "followup_quiet_hours_start": self.quiet_start,
                "followup_quiet_hours_end": self.quiet_end,
                "pause_operator_minutes": self.operator_pause_minutes,
            }
        )

    def prompt_block(self) -> str:
        """Блок для системной инструкции. Пустая строка — если всё по умолчанию.

        В модель уходит только то, что меняет её реплики: часы работы человека и
        платность первого занятия. Напоминания и уведомления о лидах — механика
        бота, модели о ней знать незачем.
        """
        work = (
            f"- Администратор на связи с {self.work_start} до {self.work_end}."
            " Вне этого времени честно скажи, когда с клиентом свяжутся."
        )
        lines = ["НАСТРОЙКИ ШКОЛЫ (заданы администратором, важнее общих правил):", work]
        if not self.trial_free:
            # Дефолт базы знаний — бесплатное пробное; отклонение обязано
            # звучать явно, иначе бот пообещает то, чего больше нет.
            lines.append(
                "- ПЕРВОЕ ЗАНЯТИЕ СЕЙЧАС ПЛАТНОЕ."
                " Не обещай бесплатное пробное ни при каких формулировках вопроса."
            )
        return "\n".join(lines)

    @property
    def fingerprint(self) -> str:
        """Отпечаток значений — ключ кеша системной инструкции.

        Без него правка часов работы не доехала бы до модели: инструкция
        кешируется по ``kb_hash``, а база знаний при смене настройки не меняется.
        """
        raw = "|".join(
            str(part)
            for part in (
                self.followup_enabled,
                self.quiet_start,
                self.quiet_end,
                self.work_start,
                self.work_end,
                self.trial_free,
                self.operator_pause_minutes,
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_runtime_settings(path: str | Path) -> RuntimeSettings:
    """Читает настройки из файла ``admin.db``.

    Недоступная база — не повод падать: бот работает на значениях по умолчанию,
    а причина уходит в лог.
    """
    try:
        store = AdminStore(path)
    except Exception as exc:  # noqa: BLE001 - повреждённый файл, права, что угодно
        _log.warning("runtime_settings_unavailable", error=str(exc))
        return RuntimeSettings.defaults()
    try:
        return RuntimeSettings.from_values({spec.key: store.get(spec.key) for spec in SETTING_SPECS})
    finally:
        store.close()
