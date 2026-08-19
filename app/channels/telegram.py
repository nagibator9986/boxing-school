"""Прямой канал Telegram через Bot API — без агрегатора.

Зачем отдельный клиент, если Wazzup умеет Telegram: чтобы поднять канал за
минуту, показать бота с телефона и протестировать сценарии до того, как заказчик
заведёт кабинет Wazzup. Плюс это единственный канал, куда **видео уходит
вложением**: у Wazzup потолок вложения 10 МБ, а Bot API принимает 50 МБ.

Формат входящего приводится к тому же виду, что и вебхук Wazzup
(``chatType: telegram``), поэтому ниже по потоку работает ровно тот же пайплайн.
Когда Telegram подключат через Wazzup, поменяется только транспорт.

Сети при импорте нет: клиент создаётся лениво.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Final

import httpx

from app.logging_conf import get_logger
from app.types import BotError

__all__ = [
    "TELEGRAM_API",
    "TelegramClient",
    "TelegramError",
    "update_to_webhook",
]

_log = get_logger(__name__)

TELEGRAM_API: Final[str] = "https://api.telegram.org"

#: Синтетический ``channelId``: у Bot API его нет, а пайплайну нужен стабильный
#: идентификатор канала, чтобы ключ диалога не менялся между перезапусками.
TELEGRAM_CHANNEL_ID: Final[str] = "telegram-bot-api"

#: Bot API: 50 МБ на sendVideo/sendDocument, 10 МБ на sendPhoto.
MAX_VIDEO_BYTES: Final[int] = 50 * 1024 * 1024
MAX_PHOTO_BYTES: Final[int] = 10 * 1024 * 1024


class TelegramError(BotError):
    """Ошибка Bot API. ``retryable`` — можно ли повторить отправку."""

    def __init__(self, message: str, *, code: str = "telegram_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class TelegramClient:
    """Минимальный асинхронный клиент Bot API: приём обновлений и отправка."""

    def __init__(self, *, token: str, timeout_s: int = 30) -> None:
        if not token:
            raise TelegramError("пустой telegram_bot_token", code="telegram_no_token")
        self._token = token
        self._timeout_s = max(5, int(timeout_s))
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ HTTP
    async def _http(self) -> httpx.AsyncClient:
        """Ленивое создание пула: в конструкторе сети быть не должно."""
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        base_url=f"{TELEGRAM_API}/bot{self._token}",
                        timeout=httpx.Timeout(self._timeout_s + 10),
                    )
        return self._client

    async def _call(self, method: str, **payload: Any) -> Any:
        """POST к методу Bot API. Разбирает конверт ``{ok, result, description}``."""
        client = await self._http()
        try:
            response = await client.post(f"/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise TelegramError(f"сеть недоступна: {exc}", retryable=True) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramError(f"{method}: ответ не JSON ({response.status_code})") from exc

        if not body.get("ok"):
            description = str(body.get("description") or "")
            # 429 и 5xx лечатся повтором, остальное — наша ошибка запроса.
            retryable = response.status_code == 429 or response.status_code >= 500
            raise TelegramError(
                f"{method}: {description or response.status_code}",
                code=f"telegram_{response.status_code}",
                retryable=retryable,
            )
        return body.get("result")

    async def aclose(self) -> None:
        """Закрывает пул. Повторный вызов безопасен."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --------------------------------------------------------------- методы
    async def get_me(self) -> dict[str, Any]:
        """``getMe`` — проверка токена и имя бота."""
        return await self._call("getMe")

    async def get_updates(self, *, offset: int | None = None, timeout_s: int = 25) -> list[Any]:
        """``getUpdates`` в режиме long polling.

        Берём только сообщения: остальные типы обновлений боту-консультанту
        не нужны и лишь удлиняют разбор.
        """
        result = await self._call(
            "getUpdates",
            offset=offset,
            timeout=timeout_s,
            allowed_updates=["message"],
        )
        return list(result or [])

    async def delete_webhook(self) -> None:
        """Снимает вебхук: ``getUpdates`` и вебхук взаимоисключающи."""
        await self._call("deleteWebhook", drop_pending_updates=False)

    async def send_message(self, chat_id: str | int, text: str) -> dict[str, Any]:
        """``sendMessage``. Разметку не включаем: бот пишет обычным текстом.

        Превью ссылок отключено намеренно. Карточка зала содержит ссылку на 2ГИС,
        и Telegram разворачивал её в блок на пол-экрана с заголовком и описанием
        сайта — он дублировал адрес, который бот только что назвал, и вытеснял
        сам ответ за пределы экрана.
        """
        return await self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            link_preview_options={"is_disabled": True},
        )

    async def get_file_path(self, file_id: str) -> str:
        """``getFile`` — путь файла на серверах Telegram для последующей загрузки."""
        result = await self._call("getFile", file_id=file_id)
        path = (result or {}).get("file_path")
        if not path:
            raise TelegramError("getFile не вернул file_path", code="telegram_no_file_path")
        return str(path)

    async def download_file(self, file_id: str, target: Path) -> Path:
        """Скачивает файл, присланный администратором, в ``target``.

        Bot API отдаёт файлы до 20 МБ. Больше — только через клиент Telegram,
        поэтому крупное видео администратору придётся сжать; об этом сообщает
        понятная ошибка, а не молчаливый обрыв.
        """
        file_path = await self.get_file_path(file_id)
        url = f"{TELEGRAM_API}/file/bot{self._token}/{file_path}"
        client = await self._http()
        try:
            response = await client.get(url, timeout=httpx.Timeout(120))
        except httpx.HTTPError as exc:
            raise TelegramError(f"файл не скачался: {exc}", retryable=True) from exc
        if response.status_code != 200:
            raise TelegramError(
                f"файл не скачался: HTTP {response.status_code}", code="telegram_download_failed"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)
        return target

    async def send_chat_action(self, chat_id: str | int, action: str = "typing") -> None:
        """Индикатор «печатает»: без него пауза на ответ модели выглядит зависанием."""
        try:
            await self._call("sendChatAction", chat_id=chat_id, action=action)
        except TelegramError:
            # Индикатор — украшение, ронять из-за него ход нельзя.
            _log.debug("telegram_chat_action_failed")

    async def send_file(
        self,
        chat_id: str | int,
        *,
        path: Path,
        kind: str,
        caption: str | None = None,
    ) -> dict[str, Any]:
        """Отправка файла с диска: ``video`` / ``photo`` / ``document``.

        Multipart идёт мимо :meth:`_call`, потому что тело здесь не JSON.
        """
        if not path.is_file():
            raise TelegramError(f"файл не найден: {path}", code="telegram_file_missing")

        size = path.stat().st_size
        limit = MAX_PHOTO_BYTES if kind == "photo" else MAX_VIDEO_BYTES
        if size > limit:
            raise TelegramError(
                f"{path.name}: {size / 1048576:.1f} МБ больше предела {limit // 1048576} МБ",
                code="telegram_file_too_large",
            )

        method = {"video": "sendVideo", "photo": "sendPhoto", "document": "sendDocument"}[kind]
        field = {"video": "video", "photo": "photo", "document": "document"}[kind]

        client = await self._http()
        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1024]  # Bot API: предел подписи
        try:
            with path.open("rb") as handle:
                response = await client.post(
                    f"/{method}", data=data, files={field: (path.name, handle)}
                )
        except httpx.HTTPError as exc:
            raise TelegramError(f"сеть недоступна: {exc}", retryable=True) from exc

        body = response.json()
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description')}", code="telegram_send_failed")
        return body.get("result") or {}


# --------------------------------------------------------------------------- #
# Приведение к общему формату
# --------------------------------------------------------------------------- #
def update_to_webhook(update: dict[str, Any]) -> dict[str, Any] | None:
    """Обновление Bot API → payload в формате вебхука Wazzup.

    Возвращает ``None`` для всего, что не является текстовым сообщением от
    человека: пайплайн ждёт именно текст, а служебные обновления его только
    сбивают. Голосовые и медиа от клиента отдаём как пустой текст с типом —
    их разберёт та же ветка, что и в Wazzup.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return None

    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if sender.get("is_bot"):
        # Своё же эхо в Bot API не приходит, но чужих ботов пропускать незачем.
        return None

    chat_id = chat.get("id")
    if chat_id is None:
        return None

    text = message.get("text") or message.get("caption") or ""
    if message.get("voice") or message.get("audio"):
        msg_type = "audio"
    elif message.get("video") or message.get("video_note"):
        msg_type = "video"
    elif message.get("photo"):
        msg_type = "image"
    elif message.get("document"):
        msg_type = "document"
    else:
        msg_type = "text"

    name = " ".join(
        part for part in (sender.get("first_name"), sender.get("last_name")) if part
    ).strip()

    return {
        "messages": [
            {
                "messageId": f"tg-{update.get('update_id')}",
                "channelId": TELEGRAM_CHANNEL_ID,
                "chatType": "telegram",
                "chatId": str(chat_id),
                "dateTime": _iso_from_unix(message.get("date")),
                "type": msg_type,
                "isEcho": False,
                "status": "inbound",
                "text": text,
                "contact": {
                    "name": name or sender.get("username") or "Клиент",
                    "username": sender.get("username"),
                    "avatarUri": None,
                },
            }
        ]
    }


def _iso_from_unix(value: Any) -> str:
    """Unix-время Telegram → ISO-8601 в UTC, как в вебхуке Wazzup."""
    from datetime import datetime, timezone

    try:
        moment = datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        moment = datetime.now(tz=timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")
