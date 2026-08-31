"""Одна служба на один порт: приём Wazzup и CRM вместе.

Проверяется то, из-за чего эта сборка вообще существует: у службы Railway один
публичный порт, а разнести бота и CRM по двум службам нельзя — диск между
службами не разделяется.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.asgi import build_app, wazzup_ready
from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Свой каталог данных и своя база знаний: тесты ничего не трогают в проекте."""
    shutil.copytree(ROOT / "kb", tmp_path / "kb")
    shutil.copytree(ROOT / "media", tmp_path / "media")
    monkeypatch.setenv("KB_DIR", str(tmp_path / "kb"))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("ADMIN_DB_PATH", str(tmp_path / "admin.db"))
    monkeypatch.setenv("STATE_SQLITE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/bot.db")
    monkeypatch.setenv("CRM_BOT_DB", str(tmp_path / "bot.db"))
    monkeypatch.setenv("CRM_SECRET_KEY", "test-secret")
    return tmp_path


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "local",
        "wazzup_api_key": "",
        "wazzup_webhook_secret": "",
        "gemini_api_key": "test",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Когда Wazzup не подключён
# --------------------------------------------------------------------------- #
def test_without_wazzup_key_service_still_serves_crm(workspace: Path) -> None:
    """Без ключа Wazzup служба поднимает CRM.

    Иначе получилась бы ловушка: чтобы исправить конфигурацию, нужна CRM, а CRM
    не поднимается из-за конфигурации.
    """
    settings = _settings()
    ready, reasons = wazzup_ready(settings)
    assert ready is False
    assert any("WAZZUP_API_KEY" in reason for reason in reasons)

    with TestClient(build_app(settings)) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "wazzup": False, "crm": True}
        assert client.get("/crm/login").status_code == 200


def test_root_leads_to_crm(workspace: Path) -> None:
    """Владелец школы набирает домен, а не путь."""
    with TestClient(build_app(_settings())) as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/crm/"


def test_crm_links_carry_the_prefix(workspace: Path) -> None:
    """Ссылки и статика внутри CRM знают про префикс ``/crm``.

    Без этого каждая ссылка вела бы в корень, где живёт вебхук, а не CRM: сайт
    открывался бы и разваливался на первом же переходе.
    """
    with TestClient(build_app(_settings())) as client:
        page = client.get("/crm/login").text
        assert "/crm/static/crm.css" in page
        assert client.get("/crm/static/crm.css").status_code == 200


def test_incomplete_wazzup_config_does_not_take_crm_down(workspace: Path) -> None:
    """Ключ есть, но конфигурация неполная — служба говорит, чего не хватает."""
    settings = _settings(app_env="prod", wazzup_api_key="key", wazzup_webhook_secret="short")
    ready, reasons = wazzup_ready(settings)
    assert ready is False
    assert reasons, "причины отказа обязаны быть названы"

    with TestClient(build_app(settings)) as client:
        assert client.get("/healthz").json()["wazzup"] is False
        assert client.get("/crm/login").status_code == 200


# --------------------------------------------------------------------------- #
# Когда Wazzup подключён
# --------------------------------------------------------------------------- #
def test_with_wazzup_both_endpoints_live(workspace: Path) -> None:
    """Вебхук и CRM отвечают на одном порту."""
    secret = "abcdefghijklmnopqrstuvwxyz123456"
    settings = _settings(
        app_env="prod",
        wazzup_api_key="key",
        wazzup_webhook_secret=secret,
        public_base_url="https://example.up.railway.app",
        manager_notify_target="77010001234",
        manager_notify_channel_id="chan-1",
        inline_worker=True,
    )
    ready, reasons = wazzup_ready(settings)
    assert ready is True, reasons

    with TestClient(build_app(settings), base_url=settings.public_base_url) as client:
        assert client.post(f"/wazzup/webhook/{secret}", json={"test": True}).status_code == 200
        assert client.post("/wazzup/webhook/wrong", json={"test": True}).status_code == 404
        assert client.get("/healthz").status_code == 200
        assert client.get("/crm/login").status_code == 200
        assert client.get("/", follow_redirects=False).headers["location"] == "/crm/"
