"""Отправка готового контента — инструмент ``send_content`` и кодек ссылок на медиа.

Реестр артефактов лежит в ``kb/media.yaml``: прайс-карточка, список залов,
геолокация зала, ссылка на Instagram, фото прайса. Инструмент выбирает запись
из реестра и **сам** кладёт её в outbox — до того, как модель сформулировала
текст. Отсюда ``render_hint=SILENT``: модель узнаёт факт отправки и пишет одну
короткую сопроводительную фразу, но содержимое артефакта не пересказывает
(пересказ — это ещё одна возможность соврать в цифре).

Кодек токена ``/media/{token}`` живёт здесь, а не в ``app/api/media.py``:
по правилу зависимостей ``api`` может импортировать ``tools``, обратное
направление запрещено (INTERFACES §18 п. 6).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Final

from app.config import get_settings
from app.kb.gaps import say_no_data
from app.kb.models import Artifact, KBSnapshot
from app.kb.render import render_artifact_body, schedule_heading
from app.types import (
    CHANNEL_LIMITS,
    MAX_MESSAGE_CHARS,
    ArtifactKind,
    ChannelKind,
    Language,
    OutboundKind,
    OutboundMessage,
    RenderHint,
    ToolContext,
    ToolResult,
)

#: Разделитель полезной нагрузки и подписи в токене ``/media/{token}``.
_TOKEN_SEP: Final[str] = "."

#: Длина подписи в байтах: 16 байт HMAC-SHA256 достаточно для ссылки с TTL 10 минут.
_SIG_BYTES: Final[int] = 16

#: Виды артефактов, которые уходят текстом, а не файлом.
_TEXT_KINDS: Final[frozenset[ArtifactKind]] = frozenset(
    {ArtifactKind.TEXT_CARD, ArtifactKind.LINK, ArtifactKind.LOCATION_TEXT}
)

_SAFE_REL_PATH_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9A-Za-z._\-/]+$")


# --------------------------------------------------------------------------- #
# Кодек токена /media/{token}
# --------------------------------------------------------------------------- #
def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _check_rel_path(rel_path: str) -> str:
    """Не выпускает за пределы каталога медиа: без ``..``, без абсолютных путей."""
    candidate = (rel_path or "").strip().replace("\\", "/")
    if not candidate:
        raise ValueError("пустой путь к файлу")
    if candidate.startswith("/") or candidate.startswith("~"):
        raise ValueError("абсолютный путь недопустим")
    if not _SAFE_REL_PATH_RE.match(candidate):
        raise ValueError("недопустимые символы в пути")
    parts = PurePosixPath(candidate).parts
    if any(part in ("..", ".") for part in parts):
        raise ValueError("выход за пределы каталога медиа запрещён")
    return candidate


def make_media_token(rel_path: str, *, ttl_s: int, secret: str) -> str:
    """HMAC-подписанный токен для /media/{token}. Живёт ttl_s (по умолчанию 10 минут)."""
    safe = _check_rel_path(rel_path)
    if not secret:
        raise ValueError("не задан секрет подписи ссылок на медиа")
    expires_at = int(datetime.now(UTC).timestamp()) + max(1, int(ttl_s))
    payload = _b64encode(f"{expires_at}|{safe}".encode())
    signature = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}{_TOKEN_SEP}{_b64encode(signature[:_SIG_BYTES])}"


def parse_media_token(token: str, *, secret: str, now: datetime) -> str:
    """Токен -> относительный путь. Просрочка/подделка -> ValueError."""
    if not token or _TOKEN_SEP not in token:
        raise ValueError("токен повреждён")
    if not secret:
        raise ValueError("не задан секрет подписи ссылок на медиа")
    payload, _, signature = token.partition(_TOKEN_SEP)
    expected = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64encode(expected[:_SIG_BYTES]), signature):
        raise ValueError("подпись токена не совпала")
    try:
        decoded = _b64decode(payload).decode("utf-8")
        expires_raw, _, rel_path = decoded.partition("|")
        expires_at = int(expires_raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("токен повреждён") from exc
    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    if moment.timestamp() > expires_at:
        raise ValueError("срок действия ссылки истёк")
    return _check_rel_path(rel_path)


def build_media_url(rel_path: str) -> str:
    """``{public_base_url}/media/{token}``. Ссылка отдаёт файл напрямую, без редиректов."""
    settings = get_settings()
    token = make_media_token(
        rel_path, ttl_s=settings.media_token_ttl_s, secret=settings.media_signing_key
    )
    return f"{settings.public_base_url.rstrip('/')}/media/{token}"


def media_rel_path_of(url: str, *, secret: str) -> str | None:
    """Путь к файлу из НАШЕЙ ссылки на медиа. Подпись проверяется, срок — нет.

    Нужна ровно одному вызывающему — воркеру отправки, который перед фактическим
    уходом сообщения в Wazzup обязан пере-подписать ссылку. Токен живёт
    ``MEDIA_TOKEN_TTL_S`` (10 минут) и выпускается в момент решения пайплайна, а
    реальная отправка может отстать на час: строка outbox ждёт снятия спам-блока
    канала (``CHANNEL_RETRY_DELAY_S`` × до ``CHANNEL_STOP_TTL_S``) или лежит до
    рестарта и подбирается сметкой. Клиенту при этом уходит протухшая ссылка, и
    Wazzup получает на ней 404.

    Срок здесь намеренно не проверяется — проверять его будет ``/media/{token}``
    уже на свежем токене. Подпись проверяется обязательно: пере-подписывать
    можно только то, что выпустили мы сами, иначе ручка медиа превращается в
    произвольное чтение файлов.

    Возвращает ``None``, если это не наша ссылка на медиа или подпись не сошлась.
    """
    if not url or not secret:
        return None
    _, sep, token = url.partition("/media/")
    if not sep or not token or "/" in token:
        return None
    payload, _, signature = token.partition(_TOKEN_SEP)
    if not payload or not signature:
        return None
    expected = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64encode(expected[:_SIG_BYTES]), signature):
        return None
    try:
        decoded = _b64decode(payload).decode("utf-8")
        _, _, rel_path = decoded.partition("|")
        return _check_rel_path(rel_path)
    except (ValueError, UnicodeDecodeError):
        return None


def refresh_media_url(url: str) -> str:
    """Свежий токен для той же нашей ссылки. Чужую строку возвращает как есть."""
    settings = get_settings()
    rel_path = media_rel_path_of(url, secret=settings.media_signing_key)
    if rel_path is None:
        return url
    return build_media_url(rel_path)


# --------------------------------------------------------------------------- #
# Проверки доставляемости
# --------------------------------------------------------------------------- #
def _clip(text: str, limit: int) -> str:
    """Обрезает текст по границе слова, чтобы не порвать фразу посередине."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + "…"


def _channel_verdict(artifact: Artifact, channel: ChannelKind) -> str | None:
    """Почему артефакт нельзя отправить в этот канал; ``None`` — можно.

    Ограничения берутся из :data:`app.types.CHANNEL_LIMITS`, а не зашиты в код:
    для Instagram список разрешённых типов файлов сводится к картинкам, поэтому
    документ туда не уйдёт сам собой, без отдельного «если инстаграм».
    """
    if artifact.channels.get(channel, "allow") == "deny":
        return "артефакт помечен в media.yaml как недоставляемый в этот канал"
    if artifact.kind in _TEXT_KINDS:
        return None
    limits = CHANNEL_LIMITS[channel]
    if not limits.allows_media:
        return "канал не принимает вложения"
    if artifact.file_mime and artifact.file_mime not in limits.allowed_mime:
        return f"канал не принимает файлы типа {artifact.file_mime}"
    if artifact.file_bytes is not None and artifact.file_bytes > limits.max_file_bytes:
        return "файл больше лимита канала"
    return None


def _schedule_sent_this_turn(ctx: ToolContext, gym_id: str | None) -> bool:
    """Ушло ли расписание этого зала клиенту на текущем ходу.

    Смотрит сообщения, собранные за ход: за его пределами список пуст, поэтому
    «прислали вчера» и «прислали только что» не путаются. Сравнение идёт по
    заголовку карточки — он собирается тем же рендером, что и сама карточка
    (:func:`app.kb.render.schedule_heading`), поэтому не разойдётся с ней.
    """
    if not gym_id:
        return False
    try:
        heading = schedule_heading(ctx.kb, gym_id=gym_id, lang=ctx.lang)
    except Exception:  # noqa: BLE001 - подпись не имеет права уронить отправку
        return False
    collected = getattr(ctx.services, "messages", None) or ()
    return any(heading in (getattr(message, "text", None) or "") for message in collected)


async def _prefer_route_video(ctx: ToolContext, artifact: Artifact) -> Artifact:
    """Подменяет карточку адреса видео дороги, если канал его принимает.

    Возвращает исходный артефакт, если подменять нечем или незачем: видео нет,
    оно выключено, канал его не тянет (WhatsApp, Instagram) или его уже
    отправляли в этом диалоге.
    """
    if not artifact.id.startswith("gym_location_") or not artifact.gym_id:
        return artifact
    route = ctx.kb.artifact(f"route_{artifact.gym_id}")
    if route is None or not route.enabled:
        return artifact
    if _channel_verdict(route, ctx.channel) is not None:
        return artifact
    sent = await ctx.services.count_artifact_sends(ctx.conversation_id, route.id)
    if sent >= route.max_send_per_dialog:
        return artifact
    return route


def _resolve(kb: KBSnapshot, artifact_id: str, gym_id: str | None) -> Artifact | None:
    """Артефакт по id; при заданном ``gym_id`` — его вариант для конкретного зала."""
    if gym_id:
        specific = kb.artifact(f"{artifact_id}_{gym_id}")
        if specific is not None:
            return specific
    return kb.artifact(artifact_id)


# --------------------------------------------------------------------------- #
# Инструмент
# --------------------------------------------------------------------------- #
async def send_content(ctx: ToolContext, *, artifact_id: str, gym_id: str | None = None) -> ToolResult:
    """Ставит артефакт в outbox САМ, до того как модель сформулировала текст.
    render_hint=SILENT: модель узнаёт факт постановки и пишет одно короткое сопроводительное
    сообщение, содержимое артефакта не пересказывает.
    Проверки: доставляемость в канале, max_send_per_dialog, наличие файла и sha256."""
    kb = ctx.kb
    lang: Language = ctx.lang

    artifact = _resolve(kb, str(artifact_id or "").strip(), gym_id)
    if artifact is None:
        return ToolResult.invalid_input(f"в kb/media.yaml нет артефакта '{artifact_id}'")

    if gym_id and artifact.gym_id is not None and artifact.gym_id != gym_id:
        return ToolResult.invalid_input(
            f"артефакт '{artifact.id}' привязан к залу '{artifact.gym_id}', а запрошен '{gym_id}'"
        )
    if gym_id and kb.gym(gym_id) is None:
        return ToolResult.invalid_input(f"в kb/gyms.yaml нет зала '{gym_id}'")

    # Выключенный артефакт = данных нет (фото прайса, расписание, реквизиты).
    if not artifact.enabled:
        gap = artifact.gap_ref
        say = say_no_data(kb, gap) if gap is not None else kb.bilingual_text("gap.generic")
        return ToolResult.no_data(
            say=say,
            gap_ref=gap,
            data={"status": "disabled", "artifact_id": artifact.id, "queued": []},
        )

    # Адрес зала школа всегда отправляет вместе с видео дороги — так это делает
    # сам владелец, и клиенту так понятнее: на видео видно вход. Подпись к видео
    # собирается из той же базы знаний и уже содержит адрес, ориентир, ссылку на
    # 2ГИС и расписание, поэтому карточка адреса после него была бы повтором.
    artifact = await _prefer_route_video(ctx, artifact)

    verdict = _channel_verdict(artifact, ctx.channel)
    if verdict is not None:
        # Это НЕ «данных нет»: материал существует, просто канал его не тянет —
        # видео не уходит в WhatsApp и Instagram. Возвращать сюда фразу-заглушку
        # «уточню у администратора» было бы враньём: адрес бот знает и обязан
        # назвать его текстом. Поэтому отдаём успех с подсказкой и запасным
        # материалом, а не ошибку.
        fallback = None
        if artifact.gym_id:
            candidate = kb.artifact(f"gym_location_{artifact.gym_id}")
            if candidate is not None and candidate.enabled:
                fallback = candidate.id
        return ToolResult.success(
            data={
                "status": "channel_unsupported",
                "artifact_id": artifact.id,
                "channel": ctx.channel.value,
                "reason": verdict,
                "fallback_artifact_id": fallback,
                "queued": [],
            },
            caveats=[
                f"В канале {ctx.channel.value} этот материал не отправляется ({verdict}). "
                + (
                    f"Отправь вместо него «{fallback}» и ответь текстом."
                    if fallback
                    else "Ответь текстом, не упоминая, что что-то не отправилось."
                )
            ],
        )

    already_sent = await ctx.services.count_artifact_sends(ctx.conversation_id, artifact.id)
    if already_sent >= artifact.max_send_per_dialog:
        return ToolResult.success(
            data={
                "status": "already_sent",
                "artifact_id": artifact.id,
                "sent_before": already_sent,
                "queued": [],
                "note": "лимит отправок этого артефакта в диалоге исчерпан",
            },
            render_hint=RenderHint.SILENT,
            caveats=["Этот материал уже отправлялся — не дублируй его, ответь словами."],
            meta={"artifact_id": artifact.id},
        )

    limits = CHANNEL_LIMITS[ctx.channel]
    text: str | None = None
    content_uri: str | None = None
    caption_text: str | None = None

    if artifact.kind in _TEXT_KINDS:
        body = render_artifact_body(kb, artifact_id=artifact.id, lang=lang)
        if not body or not body.strip():
            return ToolResult.no_data(
                say=say_no_data(kb, artifact.gap_ref) if artifact.gap_ref is not None
                else kb.bilingual_text("gap.generic"),
                gap_ref=artifact.gap_ref,
                data={"status": "empty_body", "artifact_id": artifact.id, "queued": []},
            )
        text = _clip(body.strip(), min(MAX_MESSAGE_CHARS, limits.max_text_chars))
    else:
        if not artifact.file_path or not artifact.file_sha256:
            return ToolResult.no_data(
                say=say_no_data(kb, artifact.gap_ref) if artifact.gap_ref is not None
                else kb.bilingual_text("gap.generic"),
                gap_ref=artifact.gap_ref,
                data={"status": "no_file", "artifact_id": artifact.id, "queued": []},
            )
        try:
            content_uri = build_media_url(artifact.file_path)
        except Exception as exc:  # секрет подписи не настроен или путь битый
            return ToolResult.failure(f"не удалось собрать ссылку на медиа: {exc}")

        # Подпись к файлу идёт ОТДЕЛЬНЫМ сообщением: Wazzup запрещает передавать
        # text и contentUri в одном запросе. Без подписи клиент получает ролик
        # без единого слова и не понимает, что ему прислали и зачем.
        # Подпись берём тем же рендером, что и текстовые артефакты: у видео
        # маршрута она собирается из базы знаний (адрес, ориентир, ссылка на
        # карту, расписание), а не лежит статикой. Читать здесь только
        # artifact.body значило бы отправить видео совсем без подписи.
        # Расписание в подписи повторялось: модель звала get_schedule (карточка
        # расписания ушла клиенту) и следом send_content, а подпись к видео
        # собирается из адреса И расписания — так эти ролики рассылает школа
        # вручную. В одном ходу клиент получал одно и то же дважды подряд.
        raw_caption = render_artifact_body(
            kb,
            artifact_id=artifact.id,
            lang=lang,
            with_schedule=not _schedule_sent_this_turn(ctx, artifact.gym_id),
        )
        if raw_caption and raw_caption.strip():
            caption_text = _clip(
                raw_caption.strip(), min(MAX_MESSAGE_CHARS, limits.max_text_chars)
            )

    message = OutboundMessage(
        conversation_id=ctx.conversation_id,
        channel_id=ctx.channel_id,
        channel=ctx.channel,
        chat_id=ctx.chat_id,
        lang=lang,
        kind=OutboundKind.ARTIFACT,
        text=text,
        content_uri=content_uri,
        artifact_id=artifact.id,
    )
    outbox_id = await ctx.services.enqueue_outbound(message)

    if content_uri is not None and caption_text:
        await ctx.services.enqueue_outbound(
            OutboundMessage(
                conversation_id=ctx.conversation_id,
                channel_id=ctx.channel_id,
                channel=ctx.channel,
                chat_id=ctx.chat_id,
                lang=lang,
                kind=OutboundKind.ARTIFACT,
                text=caption_text,
                artifact_id=artifact.id,
            )
        )

    title = artifact.title.get(lang) or artifact.title.ru or artifact.id
    caveats = ["Материал уже отправлен отдельным сообщением — не пересказывай его содержимое."]
    if artifact.gap_ref is not None:
        caveats.append(
            "В этом материале часть данных отсутствует: не дополняй его сведениями от себя."
        )
    return ToolResult.success(
        data={
            "status": "queued",
            "queued": [
                {
                    "kind": artifact.kind.value,
                    "artifact_id": artifact.id,
                    "outbox_id": str(outbox_id),
                    "title": title,
                }
            ],
            "note": "артефакт поставлен в очередь отправки",
            "channel": ctx.channel.value,
            "title": title,
        },
        render_hint=RenderHint.SILENT,
        caveats=caveats,
        meta={"artifact_id": artifact.id, "sent_before": already_sent},
    )


__all__ = [
    "build_media_url",
    "make_media_token",
    "media_rel_path_of",
    "parse_media_token",
    "refresh_media_url",
    "send_content",
]
