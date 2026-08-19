"""Приём вебхуков Wazzup24.

Два правила важнее аккуратности кодов ответа:

* несовпадение секрета → **404**, а не 403: подтверждать существование эндпоинта
  нельзя, иначе путь подбирается перебором;
* всё остальное → **200 OK**, включая битое тело и внутренние сбои. Любой не-200
  заставляет Wazzup ретраить, а при систематических ошибках — отключить вебхук
  совсем, и школа молча перестанет получать заявки.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import deps as app_deps
from app.config import Settings
from app.main import create_app

from tests.conftest import RecordingQueue, webhook_payload


class StubRuntime:
    """Минимальный двойник ``app.deps.Runtime``: вебхуку нужна только очередь."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.queue = RecordingQueue()
        self.started_at = datetime.now(tz=timezone.utc)


@pytest.fixture
def runtime(settings) -> StubRuntime:
    """Ставит двойник контейнера процесса и снимает его после теста."""
    stub = StubRuntime(settings)
    app_deps.set_runtime(stub)  # type: ignore[arg-type]
    try:
        yield stub
    finally:
        app_deps.set_runtime(None)


@pytest.fixture
def client(settings):
    """HTTP-клиент без lifespan: поднимать настоящие ресурсы тесту незачем."""
    return TestClient(create_app(settings), raise_server_exceptions=False)


@pytest.fixture
def url(settings) -> str:
    """Путь вебхука с настоящим секретом."""
    return settings.webhook_path()


# --------------------------------------------------------------------------- #
# Секрет
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "secret",
    [
        "wrong-secret",
        "test-webhook-secret-0123456789a",   # на символ короче
        "test-webhook-secret-0123456789abc",  # на символ длиннее
        "TEST-WEBHOOK-SECRET-0123456789AB",   # другой регистр
        "x",
    ],
)
def test_wrong_secret_returns_404(client, runtime, secret) -> None:
    """Неверный секрет — 404: существование эндпоинта не подтверждается."""
    response = client.post(f"/wazzup/webhook/{secret}", json=webhook_payload("wz-1", "привет"))

    assert response.status_code == 404
    assert runtime.queue.inbound == []


def test_correct_secret_enqueues_payload(client, runtime, url) -> None:
    """Правильный секрет: 200 и payload в очереди, обработки в хендлере нет."""
    payload = webhook_payload("wz-2", "Сколько стоит?")

    response = client.post(url, json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["kind"] == "message"
    assert runtime.queue.inbound == [payload]


# --------------------------------------------------------------------------- #
# Регистрационный тест Wazzup
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("body", [{"test": True}, {}])
def test_registration_test_request_returns_200(client, runtime, url, body) -> None:
    """Не ответим 200 на ``{"test": true}`` — вебхук не зарегистрируется вовсе."""
    response = client.post(url, json=body)

    assert response.status_code == 200
    assert response.json()["kind"] == "test"
    assert runtime.queue.inbound == []


def test_empty_body_is_treated_as_test_request(client, runtime, url) -> None:
    """Пустое тело — тоже регистрационная проверка Wazzup."""
    response = client.post(url, content=b"")

    assert response.status_code == 200
    assert response.json()["kind"] == "test"


# --------------------------------------------------------------------------- #
# Невалидные тела
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body",
    [
        "{битый json".encode("utf-8"),
        b"[1, 2, 3]",
        '{"messages": "не список"}'.encode("utf-8"),
        '{"messages": [{"нет": "обязательных полей"}]}'.encode("utf-8"),
        b'{"messages": [null]}',
        b"\xff\xfe not utf-8",
    ],
)
def test_invalid_payload_still_returns_200(client, runtime, url, body) -> None:
    """4xx на битом теле = ретраи Wazzup и в итоге отключённый вебхук."""
    response = client.post(url, content=body, headers={"content-type": "application/json"})

    assert response.status_code == 200
    assert runtime.queue.inbound == []


def test_oversized_body_returns_200_without_reading(client, runtime, url) -> None:
    """Слишком большое тело не читаем вовсе, но отвечаем 200."""
    response = client.post(
        url,
        content=b"x" * 10,
        headers={"content-type": "application/json", "content-length": "99999999"},
    )

    assert response.status_code == 200
    assert response.json()["kind"] == "too_large"


# --------------------------------------------------------------------------- #
# Отказы инфраструктуры
# --------------------------------------------------------------------------- #
def test_queue_failure_still_returns_200(client, runtime, url) -> None:
    """Очередь недоступна — громко в лог, но клиенту 200: не-200 отключит вебхук."""
    runtime.queue.fail_inbound = True

    response = client.post(url, json=webhook_payload("wz-3", "привет"))

    assert response.status_code == 200
    assert response.json()["kind"] == "dropped"


def test_runtime_not_ready_still_returns_200(client, url) -> None:
    """Служба ещё поднимается — вебхук всё равно обязан ответить 200."""
    app_deps.set_runtime(None)

    response = client.post(url, json=webhook_payload("wz-4", "привет"))

    assert response.status_code == 200
    assert response.json()["kind"] == "dropped"


# --------------------------------------------------------------------------- #
# Прочие виды payload
# --------------------------------------------------------------------------- #
def test_status_update_is_accepted(client, runtime, url) -> None:
    """Вебхук статусов принимается и раскладывается как ``status``."""
    response = client.post(
        url,
        json={"statuses": [{"messageId": "wz-5", "status": "delivered", "timestamp": "2026-08-10T09:00:00Z"}]},
    )

    assert response.status_code == 200
    assert response.json()["kind"] == "status"
    assert len(runtime.queue.inbound) == 1


def test_channel_update_is_accepted(client, runtime, url) -> None:
    """Обновления каналов тоже приходят на тот же адрес."""
    response = client.post(
        url,
        json={
            "channelsUpdates": [
                {"channelId": "11111111-1111-1111-1111-111111111111", "state": "active"}
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["kind"] == "channel_update"


def test_webhook_never_returns_5xx_on_unknown_shape(client, runtime, url) -> None:
    """Неизвестная форма тела не имеет права дать 5xx."""
    response = client.post(url, json={"somethingCompletelyNew": [{"a": 1}]})

    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# Пачка с одним битым элементом (регрессия: терялась целиком)
# --------------------------------------------------------------------------- #
def test_broken_element_does_not_discard_valid_neighbours(client, runtime, url) -> None:
    """Один нестандартный элемент не имеет права утащить с собой вопрос клиента.

    Wazzup складывает несколько сообщений в один вебхук и, получив 200, эту пачку
    не повторит. Раньше схема валидировала тело целиком: элемент без обязательных
    полей давал ``kind=invalid``, и вместе с ним пропадало соседнее сообщение
    реального клиента.
    """
    good = webhook_payload("wz-good", "Сколько стоит абонемент?")["messages"][0]
    broken = {"messageId": "wz-broken"}  # ни канала, ни чата, ни типа

    response = client.post(url, json={"messages": [broken, good]})

    assert response.status_code == 200
    assert response.json()["kind"] == "message"
    assert len(runtime.queue.inbound) == 1
    kept = runtime.queue.inbound[0]["messages"]
    assert [item["messageId"] for item in kept] == ["wz-good"]


def test_batch_of_only_broken_elements_is_still_invalid(client, runtime, url) -> None:
    """Спасать нечего — прежнее поведение: 200, ``invalid``, ничего в очередь."""
    response = client.post(url, json={"messages": [{"messageId": "a"}, {"messageId": "b"}]})

    assert response.status_code == 200
    assert response.json()["kind"] == "invalid"
    assert runtime.queue.inbound == []


# --------------------------------------------------------------------------- #
# Потеря входящего должна быть видна (регрессия: пустой job_id в webhook_received)
# --------------------------------------------------------------------------- #
def test_empty_job_id_is_reported_as_dropped(client, runtime, url, monkeypatch) -> None:
    """Очередь вернула пустой id — это потеря лида, а не штатный приём.

    У входящих нет страховки: в БД сообщение попадает уже внутри задачи, сметки
    для него не существует. Раньше вебхук в этом случае писал ``webhook_received``
    с пустым ``job_id`` и увеличивал метрику приёма — потеря выглядела как успех.
    """

    async def _no_job(payload):
        runtime.queue.inbound.append(payload)
        return ""

    monkeypatch.setattr(runtime.queue, "enqueue_inbound", _no_job)

    response = client.post(url, json=webhook_payload("wz-6", "привет"))

    assert response.status_code == 200
    assert response.json()["kind"] == "dropped"
