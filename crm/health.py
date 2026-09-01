"""Почему бот не работает — на экране, а не в журнале Railway.

Каждая проверка здесь куплена живым случаем, когда служба выглядела исправной,
а половина её молчала:

* кончились кредиты на ключе Gemini — клиенты сутки получали «у меня сбой», и
  владелец узнал причину, только спросив;
* ``MANAGER_NOTIFY_TARGET`` не задан — карточки эскалации не уходят никуда,
  в журнале ``lead_lost_no_manager_target``, на экране ничего;
* ``DATABASE_URL`` разъехался с путём CRM — вкладки «Клиенты» и «Заявки» пусты,
  хотя клиенты пишут.

Общее у всех трёх — поломка не видна оттуда, откуда за ботом смотрят. Здесь
она видна: часть проверок читает конфигурацию, часть — саму базу, где
застрявшая очередь отправки говорит о неисправности лучше любых настроек.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = ["Issue", "collect_issues"]

#: Сколько минут строка в очереди может ждать отправки, прежде чем это поломка.
#: Отправщик подметает очередь раз в минуту, поэтому десять — с большим запасом.
STUCK_AFTER_MINUTES: Final[int] = 10


@dataclass(frozen=True, slots=True)
class Issue:
    """Одна найденная неисправность."""

    level: str  # "error" — бот не работает; "warn" — работает хуже, чем должен
    title: str
    detail: str

    @property
    def is_error(self) -> bool:
        return self.level == "error"


def collect_issues(bot, settings) -> list[Issue]:
    """Список неисправностей, самые тяжёлые первыми. Пустой список — всё в порядке."""
    issues: list[Issue] = []
    issues.extend(_config_issues(settings))
    issues.extend(_model_issues(bot))
    issues.extend(_delivery_issues(bot))
    return sorted(issues, key=lambda item: 0 if item.is_error else 1)


def _config_issues(settings) -> list[Issue]:
    """Проверки по настройкам: без них бот не сможет часть своей работы."""
    found: list[Issue] = []

    if not (settings.gemini_api_key or "").strip():
        found.append(
            Issue(
                "error",
                "Не задан ключ Gemini",
                "Бот не сможет отвечать своими словами: на каждый вопрос уйдёт "
                "карточка из базы знаний и приглашение администратора. "
                "Переменная GEMINI_API_KEY.",
            )
        )

    if not (settings.manager_notify_target or "").strip():
        found.append(
            Issue(
                "error",
                "Администратор не получает карточки",
                "Когда бот передаёт диалог человеку, уведомление уходить некуда — "
                "клиент остаётся без ответа. Задайте MANAGER_NOTIFY_TARGET "
                "и MANAGER_NOTIFY_CHANNEL_ID.",
            )
        )

    if not (settings.wazzup_api_key or "").strip():
        found.append(
            Issue(
                "error",
                "Не настроен Wazzup",
                "Без ключа WAZZUP_API_KEY бот не принимает и не отправляет "
                "сообщения в WhatsApp и Instagram.",
            )
        )

    return found


#: Доля отказов модели, начиная с которой это уже неисправность, а не помеха.
FAILURE_SHARE: Final[float] = 0.5

#: Что означают коды отказов на языке владельца.
_ERROR_TEXTS: Final[dict[str, str]] = {
    "llm_quota": (
        "кончились оплаченные кредиты или исчерпана квота ключа. "
        "Пополнить: ai.studio → Projects → Billing"
    ),
    "llm_rate_limit": "слишком много запросов в минуту — модель отвечает не всем",
    "llm_timeout": "модель не успевает ответить за отведённое время",
    "llm_bad_request": "модель отклоняет запрос — вероятно, дело в настройках",
}


def _model_issues(bot) -> list[Issue]:
    """Ключ может быть задан и при этом не работать.

    Ровно это и случилось: ключ на месте, кредиты кончились, и сутки каждый
    клиент получал «у меня сбой на стороне сервиса». Настройки выглядели
    исправными; о неисправности говорила только доля отказов.
    """
    try:
        health = bot.llm_health(hours=1)
    except Exception:  # noqa: BLE001 - обзор не имеет права падать из-за проверки
        return []

    calls, errors = int(health.get("calls", 0)), int(health.get("errors", 0))
    if not calls or errors / calls < FAILURE_SHARE:
        return []

    code = str(health.get("last_error") or "").split(":", 1)[0].strip()
    reason = _ERROR_TEXTS.get(code, f"код отказа: {code or 'неизвестен'}")
    return [
        Issue(
            "error",
            f"Модель отвечает с ошибками: {errors} из {calls} за час",
            f"Клиенты получают карточки из базы знаний вместо ответа. Причина — {reason}.",
        )
    ]


def _delivery_issues(bot) -> list[Issue]:
    """Проверки по данным: очередь отправки честнее любых настроек.

    Настройки могут выглядеть правильными, а сообщения — не уходить: не тот
    номер канала, отозванный ключ, выключенный отправщик. Застрявшая очередь
    видна независимо от причины.
    """
    found: list[Issue] = []
    try:
        stuck = bot.stuck_outbox(minutes=STUCK_AFTER_MINUTES)
        failed = bot.failed_outbox(hours=24)
    except Exception:  # noqa: BLE001 - обзор не имеет права падать из-за проверки
        return found

    if stuck:
        found.append(
            Issue(
                "error",
                f"Не отправлено сообщений: {stuck}",
                f"Они ждут в очереди дольше {STUCK_AFTER_MINUTES} минут. Обычно это "
                "значит, что отправщик не работает или ключ Wazzup недействителен. "
                "Клиенты этих сообщений не получили.",
            )
        )

    if failed:
        count, reason = failed
        found.append(
            Issue(
                "warn",
                f"Сообщений не доставлено за сутки: {count}",
                f"Последняя причина отказа: {reason}",
            )
        )

    return found
