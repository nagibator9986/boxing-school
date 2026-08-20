"""Отправка в Telegram: файл и подпись — одно сообщение, а не два.

Пайплайн отдаёт вложение и подпись к нему двумя сообщениями: так требует
Wazzup, где текст и файл в одном сообщении запрещены. Telegram такого
ограничения не имеет, и буквальная отправка двух сообщений давала клиенту
два видео подряд — первое молча, второе с подписью.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _runner():  # type: ignore[no-untyped-def]
    """Загружает ``scripts/telegram_bot.py`` без запуска бота."""
    import os

    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
    spec = importlib.util.spec_from_file_location(
        "telegram_runner", ROOT / "scripts" / "telegram_bot.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["telegram_runner"] = module
    spec.loader.exec_module(module)
    return module


runner = _runner()


@dataclass
class _Message:
    """Минимальная копия исходящего сообщения пайплайна."""

    text: str | None = None
    content_uri: str | None = None
    artifact_id: str | None = None


VIDEO = Path("/media/route.mp4")


def _resolve(artifact_id: str | None) -> Path | None:
    return VIDEO if artifact_id == "route_gym" else None


def test_file_and_caption_become_one_message() -> None:
    """Видео уходит один раз — сразу с подписью."""
    plan = runner.plan_delivery(
        [
            _Message(content_uri="https://media/route.mp4", artifact_id="route_gym"),
            _Message(text="📍 Зал\nАдрес", artifact_id="route_gym"),
        ],
        _resolve,
    )
    assert plan == [(VIDEO, "📍 Зал\nАдрес")]


def test_caption_before_file_also_merges() -> None:
    """Порядок сообщений значения не имеет."""
    plan = runner.plan_delivery(
        [
            _Message(text="📍 Зал\nАдрес", artifact_id="route_gym"),
            _Message(content_uri="https://media/route.mp4", artifact_id="route_gym"),
        ],
        _resolve,
    )
    assert plan == [(VIDEO, "📍 Зал\nАдрес")]


def test_file_without_caption_is_still_sent() -> None:
    """Без подписи вложение всё равно уходит."""
    plan = runner.plan_delivery([_Message(content_uri="x", artifact_id="route_gym")], _resolve)
    assert plan == [(VIDEO, None)]


def test_plain_texts_are_untouched() -> None:
    """Обычные ответы бота идут как есть и в том же порядке."""
    plan = runner.plan_delivery(
        [_Message(text="Здравствуйте"), _Message(text="Подойдёт такое время?")], _resolve
    )
    assert plan == [(None, "Здравствуйте"), (None, "Подойдёт такое время?")]


def test_text_card_with_artifact_id_is_not_swallowed() -> None:
    """Текстовая карточка без файла уходит текстом, а не пропадает.

    У карточки прайса тоже есть ``artifact_id``, но файла за ним нет — она
    обязана дойти до клиента обычным сообщением.
    """
    plan = runner.plan_delivery(
        [_Message(text="💳 Костанай — стоимость", artifact_id="price_card_city")], _resolve
    )
    assert plan == [(None, "💳 Костанай — стоимость")]


def test_two_different_videos_both_arrive() -> None:
    """Разные артефакты не схлопываются друг в друга."""

    def resolve(artifact_id: str | None) -> Path | None:
        return Path(f"/media/{artifact_id}.mp4") if artifact_id and artifact_id.startswith("route_") else None

    plan = runner.plan_delivery(
        [
            _Message(content_uri="a", artifact_id="route_one"),
            _Message(text="подпись 1", artifact_id="route_one"),
            _Message(content_uri="b", artifact_id="route_two"),
            _Message(text="подпись 2", artifact_id="route_two"),
        ],
        resolve,
    )
    assert plan == [
        (Path("/media/route_one.mp4"), "подпись 1"),
        (Path("/media/route_two.mp4"), "подпись 2"),
    ]
