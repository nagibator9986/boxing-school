"""Сборка исходящих: таблица возможностей каналов, сплит текста, санитайз, backoff.

Главная идея файла — **декларативная таблица возможностей канала**
(:data:`CHANNEL_CAPABILITIES`). Ограничения Instagram («писать первым нельзя»,
«окно 7 дней», «только jpg/png/bmp до 8 МБ», «видео не отправляется») живут строками
таблицы, а не ветками ``if channel == INSTAGRAM`` в бизнес-логике. Появится третий
канал — добавится строка, а не десять условий по коду.

Лимиты длины взяты по **самому строгому** значению: таблицы в документации Wazzup
противоречат друг другу (research-wazzup24 §2.3 и §8.4: Instagram — 1000 в таблице
лимитов и 10 000 в описании кода ``MESSAGES_TOO_LONG_INSTAGRAM``; VK — 1000 против 4096).
Берём минимум и всегда обрабатываем ошибку по коду, а не по цифре.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from app.channels.wazzup_schemas import SendMessageRequest
from app.config import get_settings
from app.types import (
    CHANNEL_LIMITS,
    MAX_MESSAGE_CHARS,
    ArtifactKind,
    ChannelKind,
    OutboundKind,
    OutboundMessage,
)

# --------------------------------------------------------------------------- #
# Константы лимитов
# --------------------------------------------------------------------------- #
#: Жёсткий предел одного исходящего — 1000 знаков.
#: Источник: research-wazzup24 §2.3 (таблица S4: Instagram — 1000) и §13 п.8
#: «резать текст по 1000 символов (общий безопасный минимум)». Противоречащее
#: значение 10 000 из описания кода MESSAGES_TOO_LONG_INSTAGRAM сознательно НЕ берём.
HARD_TEXT_CHARS: Final[int] = MAX_MESSAGE_CHARS

#: Предел размера вложения для API — 10 МБ на любой канал.
#: Источник: research-wazzup24 §2.4 (S12, дословно «в том числе по API… — 10 МБ»)
#: и код ошибки MESSAGES_CONTENT_SIZE_EXCEEDED.
API_MAX_FILE_BYTES: Final[int] = 10 * 1024 * 1024

#: Окно Instagram Direct — 7 суток, а не 24 часа (research §9.1, S10).
#: Каждое входящее от клиента продлевает окно ещё на 7 суток.
INSTAGRAM_WINDOW_HOURS: Final[int] = 168

#: Сколько эмодзи максимум оставляем в одном сообщении (тон бренда, не API).
MAX_EMOJI_PER_MESSAGE: Final[int] = 2

#: Сообщений подряд от бота — 2 (глобальная константа контракта, INTERFACES §2).
#: Используется, только если настройки почему-то недоступны.
DEFAULT_MAX_PARTS: Final[int] = 2

#: Верхняя граница задержки повтора — 8 × base (экспонента 1/2/4/8).
BACKOFF_MAX_MULTIPLIER: Final[int] = 8

#: Доля джиттера от вычисленной задержки.
BACKOFF_JITTER_RATIO: Final[float] = 0.25


# --------------------------------------------------------------------------- #
# Таблица возможностей канала
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    """Что физически умеет канал. Единственный источник правды для ветвлений.

    Числовые лимиты берутся из :data:`app.types.CHANNEL_LIMITS`, здесь к ним
    добавляются продуктово-технические флаги, которых в общем типе нет.
    """

    channel: ChannelKind
    #: Жёсткий предел текста, знаков.
    max_text_chars: int
    #: Мягкий предел: длиннее — режем на несколько сообщений.
    soft_text_chars: int
    #: Можно ли написать клиенту первым, вне ответа на его сообщение.
    can_initiate: bool
    #: Окно обслуживания в часах; None — окна нет.
    service_window_hours: int | None
    #: Разрешено ли вообще слать вложения.
    allows_media: bool
    #: Допустимые MIME вложений.
    allowed_mime: frozenset[str]
    #: Предел размера вложения, байт.
    max_file_bytes: int
    #: Виды артефактов, которые канал принимает.
    allowed_artifact_kinds: frozenset[ArtifactKind]
    #: Видео не отправляется вложением — только ссылкой в тексте.
    video_as_link_only: bool
    #: Поддерживается ли цитирование (refMessageId).
    supports_quote: bool
    #: Рендерит ли канал markdown (ни один из наших — нет).
    renders_markdown: bool
    #: Человеческое пояснение ограничения — уходит менеджеру в карточку.
    note: str


CHANNEL_CAPABILITIES: Final[dict[ChannelKind, ChannelCapabilities]] = {
    ChannelKind.WHATSAPP: ChannelCapabilities(
        channel=ChannelKind.WHATSAPP,
        max_text_chars=min(CHANNEL_LIMITS[ChannelKind.WHATSAPP].max_text_chars, HARD_TEXT_CHARS),
        soft_text_chars=CHANNEL_LIMITS[ChannelKind.WHATSAPP].soft_text_chars,
        # Личный WhatsApp: окна обслуживания и шаблонов нет, писать первым можно
        # (research §9.2). Риск — не окно, а спам-блок MESSAGES_IS_SPAM.
        can_initiate=True,
        service_window_hours=CHANNEL_LIMITS[ChannelKind.WHATSAPP].service_window_hours,
        allows_media=CHANNEL_LIMITS[ChannelKind.WHATSAPP].allows_media,
        allowed_mime=CHANNEL_LIMITS[ChannelKind.WHATSAPP].allowed_mime,
        max_file_bytes=min(CHANNEL_LIMITS[ChannelKind.WHATSAPP].max_file_bytes, API_MAX_FILE_BYTES),
        allowed_artifact_kinds=frozenset(
            {
                ArtifactKind.TEXT_CARD,
                ArtifactKind.IMAGE,
                ArtifactKind.DOCUMENT,
                ArtifactKind.LINK,
                ArtifactKind.LOCATION_TEXT,
                ArtifactKind.VIDEO,
            }
        ),
        # Видео уходит вложением: Wazzup принимает для WhatsApp .mp4, а предел
        # API в 10 МБ маршруты школы проходят с запасом (1,5–4,1 МБ). Ролик
        # крупнее предела отсеет проверка размера, и клиент получит карточку со
        # ссылкой — как и раньше.
        video_as_link_only=False,
        supports_quote=True,
        renders_markdown=False,
        note="WhatsApp: текст до 1000 знаков, картинки и pdf до 10 МБ.",
    ),
    ChannelKind.INSTAGRAM: ChannelCapabilities(
        channel=ChannelKind.INSTAGRAM,
        max_text_chars=min(CHANNEL_LIMITS[ChannelKind.INSTAGRAM].max_text_chars, HARD_TEXT_CHARS),
        soft_text_chars=CHANNEL_LIMITS[ChannelKind.INSTAGRAM].soft_text_chars,
        # «Можно ли писать первым — Нет» (S10, дословно). Окно открывает клиент.
        can_initiate=False,
        service_window_hours=CHANNEL_LIMITS[ChannelKind.INSTAGRAM].service_window_hours
        or INSTAGRAM_WINDOW_HOURS,
        allows_media=CHANNEL_LIMITS[ChannelKind.INSTAGRAM].allows_media,
        # Безопасное пересечение S12 (jpg, png, bmp) и MESSAGES_UNSUPPORTED_CONTENT_TYPE_INSTAPI
        # (jpg, gif, png, ico, bmp) — см. research §2.4.
        allowed_mime=CHANNEL_LIMITS[ChannelKind.INSTAGRAM].allowed_mime,
        max_file_bytes=min(CHANNEL_LIMITS[ChannelKind.INSTAGRAM].max_file_bytes, API_MAX_FILE_BYTES),
        allowed_artifact_kinds=frozenset(
            {
                ArtifactKind.TEXT_CARD,
                ArtifactKind.IMAGE,
                ArtifactKind.LINK,
                ArtifactKind.LOCATION_TEXT,
            }
        ),
        video_as_link_only=True,  # «сообщения с документами, фото, видео и аудио не отправятся»
        supports_quote=False,  # НП: цитирование комментария доступно «только из чатов Wazzup»
        renders_markdown=False,
        note="Instagram Direct: только текст и картинки jpg/png/bmp до 8 МБ, "
        "окно 7 дней, первым писать нельзя.",
    ),
    ChannelKind.TELEGRAM: ChannelCapabilities(
        channel=ChannelKind.TELEGRAM,
        max_text_chars=min(CHANNEL_LIMITS[ChannelKind.TELEGRAM].max_text_chars, HARD_TEXT_CHARS),
        soft_text_chars=CHANNEL_LIMITS[ChannelKind.TELEGRAM].soft_text_chars,
        # Пользователь сам открывает диалог кнопкой «Запустить»; окна обслуживания нет.
        can_initiate=True,
        service_window_hours=CHANNEL_LIMITS[ChannelKind.TELEGRAM].service_window_hours,
        allows_media=CHANNEL_LIMITS[ChannelKind.TELEGRAM].allows_media,
        allowed_mime=CHANNEL_LIMITS[ChannelKind.TELEGRAM].allowed_mime,
        # Единственный канал, где НЕ действует потолок вложения Wazzup в 10 МБ:
        # Bot API принимает до 50 МБ, а через агрегатор мы сюда не ходим.
        max_file_bytes=CHANNEL_LIMITS[ChannelKind.TELEGRAM].max_file_bytes,
        allowed_artifact_kinds=frozenset(
            {
                ArtifactKind.TEXT_CARD,
                ArtifactKind.IMAGE,
                ArtifactKind.DOCUMENT,
                ArtifactKind.LINK,
                ArtifactKind.LOCATION_TEXT,
                ArtifactKind.VIDEO,
            }
        ),
        video_as_link_only=False,  # sendVideo до 50 МБ — ролики уходят как есть
        supports_quote=True,
        renders_markdown=False,
        note="Telegram: текст до 4096 знаков, видео до 50 МБ вложением.",
    ),
}


def capabilities(channel: ChannelKind) -> ChannelCapabilities:
    """Строка таблицы возможностей канала."""
    return CHANNEL_CAPABILITIES[channel]


# Причины отказа. Стабильные snake_case-коды: уходят в логи и метрики, не в диалог.
DENY_CHANNEL: Final[str] = "channels_deny"
DENY_KIND: Final[str] = "kind_not_supported"
DENY_MEDIA: Final[str] = "media_not_supported"
DENY_MIME_UNKNOWN: Final[str] = "mime_unknown"
DENY_MIME: Final[str] = "mime_not_allowed"
DENY_SIZE: Final[str] = "file_too_large"
DENY_VIDEO: Final[str] = "video_link_only"
DENY_CANNOT_INITIATE: Final[str] = "channel_cannot_initiate"
DENY_WINDOW_EXPIRED: Final[str] = "service_window_expired"

#: Виды артефактов, которым нужен файл по ссылке (contentUri), а не текст.
#: Виды с файлом на диске: для них работают проверки mime, размера и видео-правила.
_MEDIA_KINDS: Final[frozenset[ArtifactKind]] = frozenset(
    {ArtifactKind.IMAGE, ArtifactKind.DOCUMENT, ArtifactKind.VIDEO}
)


def check_artifact_deliverable(
    *,
    channel: ChannelKind,
    kind: ArtifactKind,
    mime: str | None,
    size_bytes: int | None,
    allowed: bool,
) -> str | None:
    """``None`` — артефакт можно слать; иначе код причины отказа.

    Принимает примитивы, а не kb-модель: слой ``channels`` про KB не знает.
    Разворачивает ``Artifact`` в аргументы вызывающий — ``app/tools/content.py``.
    ``allowed`` — разрешение из самой KB (``artifacts[].channels``).
    """
    caps = capabilities(channel)
    if not allowed:
        return DENY_CHANNEL
    if kind not in caps.allowed_artifact_kinds:
        return f"{DENY_KIND}:{kind.value}"
    if kind not in _MEDIA_KINDS:
        # text_card / link / location_text уходят обычным текстом — файловых проверок нет.
        return None
    if not caps.allows_media:
        return DENY_MEDIA
    normalized_mime = (mime or "").strip().lower()
    if not normalized_mime:
        return DENY_MIME_UNKNOWN
    if caps.video_as_link_only and normalized_mime.startswith("video/"):
        return DENY_VIDEO
    if normalized_mime not in caps.allowed_mime:
        return f"{DENY_MIME}:{normalized_mime}"
    limit = min(caps.max_file_bytes, API_MAX_FILE_BYTES)
    if size_bytes is not None and size_bytes > limit:
        return f"{DENY_SIZE}:{size_bytes}>{limit}"
    return None


def check_send_allowed(
    *,
    channel: ChannelKind,
    last_inbound_at: datetime | None,
    now: datetime,
    kind: OutboundKind = OutboundKind.BOT_REPLY,
) -> str | None:
    """``None`` — писать можно; иначе код причины (окно закрыто, писать первым нельзя).

    Instagram: пока клиент не написал сам, отправлять нечего и незачем — первым писать
    нельзя вообще; после входящего есть 7 суток, каждое новое входящее продлевает окно.
    WhatsApp личный: окна нет, ограничение снимается сразу.
    """
    caps = capabilities(channel)
    if last_inbound_at is None:
        return None if caps.can_initiate else DENY_CANNOT_INITIATE
    if caps.service_window_hours is None:
        return None
    deadline = last_inbound_at + timedelta(hours=caps.service_window_hours)
    if now > deadline:
        return DENY_WINDOW_EXPIRED
    return None


def window_expires_at(channel: ChannelKind, last_inbound_at: datetime | None) -> datetime | None:
    """Когда закроется окно ответа. ``None`` — окна нет или оно ещё не открывалось."""
    caps = capabilities(channel)
    if last_inbound_at is None or caps.service_window_hours is None:
        return None
    return last_inbound_at + timedelta(hours=caps.service_window_hours)


def text_limits(
    channel: ChannelKind, *, soft_limit: int | None = None, hard_limit: int | None = None
) -> tuple[int, int]:
    """Эффективная пара «мягкий, жёсткий» предел: минимум из канала и запрошенного."""
    caps = capabilities(channel)
    hard = min(hard_limit or HARD_TEXT_CHARS, caps.max_text_chars, HARD_TEXT_CHARS)
    soft = min(soft_limit or caps.soft_text_chars, caps.soft_text_chars, hard)
    return max(soft, 1), max(hard, 1)


# --------------------------------------------------------------------------- #
# Санитайз
# --------------------------------------------------------------------------- #
_ZERO_WIDTH_RE: Final[re.Pattern[str]] = re.compile("[​‌⁠﻿]")
_CODE_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"```+")
_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
_BOLD_RE: Final[re.Pattern[str]] = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_MD_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\[([^\]]{1,120})\]\((https?://[^\s)]+)\)")
_LIST_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t]*(?:[-*+•]|\d{1,2}[.)])[ \t]+", re.MULTILINE)
_HR_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.MULTILINE)
_MANY_NEWLINES_RE: Final[re.Pattern[str]] = re.compile(r"\n{3,}")
_TRAILING_WS_RE: Final[re.Pattern[str]] = re.compile(r"[ \t]+$", re.MULTILINE)
_MANY_SPACES_RE: Final[re.Pattern[str]] = re.compile(r"[ \t]{2,}")

_EMOJI_CHARS: Final[str] = (
    "\U0001f000-\U0001faff"      # пиктограммы, эмодзи, флаги
    "☀-➿"              # разное + дингбаты
    "←-⇿"              # стрелки
    "⬀-⯿"              # доп. стрелки и фигуры
    "〰〽㊗㊙"
    "©®™"         # (c), (r), (tm)
)
#: Модификаторы: селекторы начертания и тон кожи.
_EMOJI_MODS: Final[str] = "[️︎\U0001f3fb-\U0001f3ff]*"
#: Кластер = базовый символ с модификаторами плюс сцепки через ZWJ (👨‍👩‍👧 — один кластер,
#: а 😀😃 — два, иначе «фейерверк» из подряд идущих эмодзи считался бы одним).
_EMOJI_CLUSTER_RE: Final[re.Pattern[str]] = re.compile(
    f"[{_EMOJI_CHARS}]{_EMOJI_MODS}(?:‍[{_EMOJI_CHARS}]{_EMOJI_MODS})*"
)


def sanitize(text: str) -> str:
    """Приводит ответ модели к тому, что мессенджеры реально показывают.

    WhatsApp и Instagram markdown не рендерят: ``**жирный**`` и ``### Заголовок``
    прилетают клиенту как есть. Убираем разметку, списки приводим к тире ``—``,
    режем эмодзи-фейерверк до :data:`MAX_EMOJI_PER_MESSAGE`.
    """
    if not text:
        return ""
    out = text.replace("\r\n", "\n").replace("\r", "\n")
    out = _ZERO_WIDTH_RE.sub("", out)
    out = out.replace(" ", " ")
    out = _CODE_FENCE_RE.sub("", out)
    out = out.replace("`", "")
    out = _HEADING_RE.sub("", out)
    out = _BOLD_RE.sub(r"\2", out)
    out = _MD_LINK_RE.sub(_replace_md_link, out)
    out = _HR_RE.sub("", out)
    out = _LIST_RE.sub("— ", out)
    out = _limit_emoji(out, MAX_EMOJI_PER_MESSAGE)
    out = _MANY_SPACES_RE.sub(" ", out)
    out = _TRAILING_WS_RE.sub("", out)
    out = _MANY_NEWLINES_RE.sub("\n\n", out)
    return out.strip()


def _replace_md_link(match: re.Match[str]) -> str:
    """``[текст](url)`` → ``текст: url``; если подпись совпадает с адресом — только адрес."""
    label, url = match.group(1).strip(), match.group(2).strip()
    if not label or label == url:
        return url
    return f"{label}: {url}"


def _limit_emoji(text: str, limit: int) -> str:
    """Оставляет первые ``limit`` эмодзи, остальные удаляет."""
    kept = 0
    out: list[str] = []
    last = 0
    for match in _EMOJI_CLUSTER_RE.finditer(text):
        out.append(text[last : match.start()])
        if kept < limit:
            out.append(match.group(0))
            kept += 1
        last = match.end()
    out.append(text[last:])
    return "".join(out)


# --------------------------------------------------------------------------- #
# Сплит длинного текста
# --------------------------------------------------------------------------- #
_PARAGRAPH_RE: Final[re.Pattern[str]] = re.compile(r"\n[ \t]*\n+")
_SENTENCE_END_RE: Final[re.Pattern[str]] = re.compile(r"[.!?…]+[\"»)\]]*[ \t]*\n?[ \t]*")
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\S+\s*")
_WORD_TAIL_RE: Final[re.Pattern[str]] = re.compile(r"([^\W\d_]+|\d+)[\"»)\]]*$", re.UNICODE)

#: Сокращения, после которых точка не заканчивает предложение.
_ABBREVIATIONS: Final[frozenset[str]] = frozenset(
    {
        "ул",
        "пр",
        "просп",
        "пер",
        "мкр",
        "обл",
        "г",
        "гг",
        "д",
        "кв",
        "каб",
        "стр",
        "им",
        "т",
        "тел",
        "др",
        "пр-т",
        "руб",
        "тг",
        "мин",
        "ч",
        "см",
        "км",
        "кг",
        "шт",
        "рис",
        "напр",
    }
)

#: Единицы измерения, которые нельзя оторвать от числа при переносе.
_UNITS: Final[frozenset[str]] = frozenset(
    {
        "тг",
        "тг.",
        "₸",
        "тенге",
        "kzt",
        "руб",
        "руб.",
        "мин",
        "мин.",
        "минут",
        "час",
        "часа",
        "часов",
        "ч",
        "ч.",
        "лет",
        "год",
        "года",
        "жыл",
        "жас",
        "теңге",
        "кг",
        "см",
        "м",
        "шт",
        "чел",
        "раз",
        "раза",
        "%",
    }
)


def split_text(
    text: str,
    *,
    channel: ChannelKind,
    soft_limit: int,
    hard_limit: int,
    max_parts: int | None = None,
) -> list[str]:
    """Режет длинный ответ на сообщения: сперва по абзацам, затем по предложениям.

    Никогда не рвёт слово, URL и число: ``1 500 тг`` и ``10 000`` остаются целыми
    вместе с единицей измерения. Кусков не больше ``max_parts``
    (по умолчанию ``Settings.max_messages_per_turn``) — остаток отбрасывается
    по границе предложения, без многоточий и обрывков.
    """
    if not text or not text.strip():
        return []
    soft, hard = text_limits(channel, soft_limit=soft_limit, hard_limit=hard_limit)
    limit = min(soft, hard)
    if max_parts is None:
        max_parts = _default_max_parts()
    max_parts = max(1, max_parts)

    pieces = _explode(text, limit)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
            continue
        if len((current + piece).strip()) <= limit:
            current += piece
        else:
            chunks.append(current)
            current = piece
    if current:
        chunks.append(current)

    result: list[str] = []
    for chunk in chunks[:max_parts]:
        clean = chunk.strip()
        if not clean:
            continue
        result.append(clean if len(clean) <= hard else _hard_cut(clean, hard))
    return result


def _default_max_parts() -> int:
    """``Settings.max_messages_per_turn``; при недоступных настройках — :data:`DEFAULT_MAX_PARTS`.

    Сплит вызывается в пути отправки ответа клиенту и падать там не имеет права:
    сломанный конфиг обязан валить старт процесса, а не одно сообщение.
    """
    try:
        return get_settings().max_messages_per_turn
    except Exception:  # pragma: no cover - настройки чинятся на старте, не здесь
        return DEFAULT_MAX_PARTS


def _explode(text: str, limit: int) -> list[str]:
    """Разбирает текст на минимальные куски ≤ ``limit``, сохраняя разделители."""
    out: list[str] = []
    for paragraph in _split_keep(text, [m.end() for m in _PARAGRAPH_RE.finditer(text)]):
        if len(paragraph.strip()) <= limit:
            out.append(paragraph)
            continue
        for sentence in _split_sentences(paragraph):
            if len(sentence.strip()) <= limit:
                out.append(sentence)
            else:
                out.extend(_split_atoms(sentence, limit))
    return out


def _split_keep(text: str, boundaries: list[int]) -> list[str]:
    """Режет строку по индексам, сохраняя разделители внутри кусков."""
    pieces: list[str] = []
    prev = 0
    for boundary in boundaries:
        if boundary <= prev or boundary >= len(text):
            continue
        pieces.append(text[prev:boundary])
        prev = boundary
    tail = text[prev:]
    if tail:
        pieces.append(tail)
    return pieces or ([text] if text else [])


def _split_sentences(text: str) -> list[str]:
    """Границы предложений. Сокращения (``ул.``, ``т.д.``) и числа не считаются концом."""
    boundaries: list[int] = []
    for match in _SENTENCE_END_RE.finditer(text):
        end = match.end()
        if end >= len(text) or end == match.start():
            continue
        if not match.group(0)[-1].isspace():
            continue  # «18.00» и «т.д.» без пробела после точки — не граница
        head = text[: match.start()]
        word = _WORD_TAIL_RE.search(head)
        if word is not None:
            token = word.group(1).lower()
            if token in _ABBREVIATIONS or token.isdigit() or len(token) == 1:
                continue
        nxt = text[end]
        if nxt.islower():
            continue  # предложение с маленькой буквы не начинается
        boundaries.append(end)
    return _split_keep(text, boundaries)


def _split_atoms(text: str, limit: int) -> list[str]:
    """Последний уровень: слова и числа. Атом не рвётся, даже если он длинный."""
    atoms = _atoms(text)
    out: list[str] = []
    current = ""
    for atom in atoms:
        if not current:
            current = atom
        elif len((current + atom).strip()) <= limit:
            current += atom
        else:
            out.append(current)
            current = atom
    if current:
        out.append(current)

    exploded: list[str] = []
    for piece in out:
        if len(piece.strip()) <= limit:
            exploded.append(piece)
        else:
            # Единственный атом длиннее лимита (гигантский URL) — режем как есть,
            # иначе Wazzup вернёт MESSAGE_TEXT_TOO_LONG и сообщение не уйдёт вовсе.
            stripped = piece.strip()
            exploded.extend(stripped[i : i + limit] for i in range(0, len(stripped), limit))
    return exploded


def _atoms(text: str) -> list[str]:
    """Токены с их хвостовыми пробелами; числа склеены с разрядами и единицей."""
    tokens = [m.group(0) for m in _TOKEN_RE.finditer(text)]
    atoms: list[str] = []
    for token in tokens:
        if atoms and _glues(atoms[-1], token):
            atoms[-1] += token
        else:
            atoms.append(token)
    return atoms


def _glues(previous: str, current: str) -> bool:
    """Нужно ли склеить два токена: ``1 500`` + ``тг`` не разрываются переносом."""
    left = previous.strip()
    right = current.strip()
    if not left or not right:
        return False
    if not left[-1].isdigit():
        return False
    if right[0].isdigit():
        return True  # разряды числа: «10 000»
    return right.lower().strip(".,;:!?") in _UNITS


def _hard_cut(text: str, limit: int) -> str:
    """Обрезает по границе слова, не оставляя половину слова или числа."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(" "), head.rfind("\n"))
    if cut > limit // 2:
        head = head[:cut]
    return head.rstrip(" \n\t,;:—-")


# --------------------------------------------------------------------------- #
# Тело запроса и backoff
# --------------------------------------------------------------------------- #
def build_send_request(msg: OutboundMessage) -> SendMessageRequest:
    """``OutboundMessage`` → тело ``POST /v3/message``.

    ``crmMessageId`` передаётся всегда — идемпотентность на стороне Wazzup живёт
    60 секунд и спасает от дублей при ретраях. ``clearUnanswered`` всегда ``False``:
    иначе автоответ гасит счётчик неотвеченных и менеджеры перестают видеть ждущих.
    """
    caps = capabilities(msg.channel)
    text = msg.text
    if text is not None:
        text = text.strip()
        if len(text) > caps.max_text_chars:
            # Последний рубеж: пайплайн обязан был порезать раньше через split_text.
            text = _hard_cut(text, caps.max_text_chars)
    return SendMessageRequest(
        channelId=msg.channel_id,
        chatType=msg.channel.value,
        chatId=msg.chat_id,
        text=text or None,
        contentUri=msg.content_uri,
        crmMessageId=str(msg.crm_message_id),
        refMessageId=msg.ref_message_id if caps.supports_quote else None,
        clearUnanswered=False,
    )


def next_backoff_ms(attempt: int, *, base_ms: int) -> int:
    """Задержка перед повтором: экспонента 1/2/4/8 × ``base_ms`` плюс джиттер до 25 %.

    ``attempt`` начинается с 1. Джиттер обязателен: без него все параллельные
    воркеры пойдут на повтор в одну и ту же миллисекунду.
    """
    safe_attempt = max(1, attempt)
    multiplier = min(2 ** (safe_attempt - 1), BACKOFF_MAX_MULTIPLIER)
    delay = max(0, base_ms) * multiplier
    jitter = random.uniform(0.0, BACKOFF_JITTER_RATIO) * delay
    return int(delay + jitter)


__all__ = [
    "API_MAX_FILE_BYTES",
    "BACKOFF_JITTER_RATIO",
    "BACKOFF_MAX_MULTIPLIER",
    "CHANNEL_CAPABILITIES",
    "ChannelCapabilities",
    "DENY_CANNOT_INITIATE",
    "DENY_CHANNEL",
    "DENY_KIND",
    "DENY_MEDIA",
    "DENY_MIME",
    "DENY_MIME_UNKNOWN",
    "DENY_SIZE",
    "DENY_VIDEO",
    "DENY_WINDOW_EXPIRED",
    "DEFAULT_MAX_PARTS",
    "HARD_TEXT_CHARS",
    "INSTAGRAM_WINDOW_HOURS",
    "MAX_EMOJI_PER_MESSAGE",
    "build_send_request",
    "capabilities",
    "check_artifact_deliverable",
    "check_send_allowed",
    "next_backoff_ms",
    "sanitize",
    "split_text",
    "text_limits",
    "window_expires_at",
]
