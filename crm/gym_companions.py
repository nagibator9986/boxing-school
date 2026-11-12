"""Всё, что нужно залу кроме записи в ``gyms.yaml``, — CRM доделывает сама.

Владелец 12.11.2026: «чтобы все данные — расписание, новые залы — легко менялись в CRM и бот
подхватывал». Запись зала сама по себе не делала зал рабочим, и сохранение упиралось в
правила, которых на странице зала не видно:

* районный зал в новом посёлке не сохранялся: посёлка нет в районных ценах (``pricing.yaml``);
* зал «Костанай» в пригороде не сохранялся: пригород не отмечен частью города;
* у нового зала не было карточки адреса (``gym_location_<id>``) — бот не мог её отправить;
* удалить зал с карточкой адреса или видео дороги было нельзя: материалы ссылались на зал,
  и проверка базы откатывала удаление.

Функции работают с документами, прочитанными ``KBEditor.load`` (с комментариями), и только
готовят правку: сохраняет её вызывающий одним ``KBEditor.save`` — проверка базы и откат общие.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from app.admin.media_store import slugify
from app.types import CHANNEL_LIMITS, ChannelKind

#: Префиксы материалов зала зафиксированы контрактом: по ним бот находит карточку адреса и
#: видео дороги (``app.tools.content``, ``app.tools.schedule``).
LOCATION_PREFIX: Final[str] = "gym_location_"
ROUTE_PREFIX: Final[str] = "route_"

#: Когда модель отправляет карточку адреса и видео дороги. Те же слова, что у материалов,
#: заведённых вручную; правятся потом на странице материала.
_LOCATION_WHEN: Final[str] = "когда просят адрес, локацию или как найти зал «{title}»"
_ROUTE_WHEN: Final[str] = (
    "как только разговор дошёл до зала «{title}»: назвали его адрес, клиент выбрал этот зал, "
    "спрашивает где находится, как добраться, где вход или просит показать дорогу. Видео с "
    "подписью заменяет объяснение словами"
)


@dataclass(slots=True)
class Prepared:
    """Правка, готовая к сохранению: документы по именам файлов и что CRM сделала сама."""

    documents: dict[str, Any]
    notes: list[str] = field(default_factory=list)


def find_gym(document: Any, gym_id: str) -> Any | None:
    """Запись зала в документе ``gyms.yaml``."""
    for gym in document.get("gyms") or ():
        if gym.get("id") == gym_id:
            return gym
    return None


def new_gym_id(title: str, settlement: str, taken: set[str]) -> str:
    """Идентификатор нового зала из названия: латиницей, без пробелов, не занятый."""
    base = slugify(title or settlement or "zal", limit=40)
    if base == "material":
        base = "zal"
    candidate, index = base, 2
    while candidate in taken:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def _text(value: Any, lang: str) -> str:
    return str((value or {}).get(lang) or "").strip()


def prepare_gym_save(editor: Any, gyms_doc: Any, gym_id: str) -> Prepared:
    """Доводит до рабочего состояния всё, что связано с залом ``gym_id``.

    Ничего не сохраняет: возвращает документы, которые нужно сохранить вместе.
    """
    prepared = Prepared(documents={"gyms.yaml": gyms_doc})
    gym = find_gym(gyms_doc, gym_id)
    if gym is None:
        return prepared
    title = _text(gym.get("title"), "ru") or gym_id
    settlement = str(gym.get("settlement") or "").strip()
    city = str(gyms_doc.get("city_settlement") or "").strip()

    if gym.get("scope") == "city" and settlement and settlement != city:
        suburbs = gyms_doc.get("city_suburbs")
        if suburbs is None:
            gyms_doc["city_suburbs"] = suburbs = []
        if settlement not in suburbs:
            suburbs.append(settlement)
            prepared.notes.append(
                f"«{settlement}» отмечен частью города: там действуют цены {city or 'города'}."
            )

    if gym.get("scope") == "region" and gym.get("active", True) and settlement:
        pricing = editor.load("pricing.yaml")
        regions = pricing.get("region_settlements")
        if regions is None:
            pricing["region_settlements"] = regions = []
        if settlement not in regions:
            regions.append(settlement)
            prepared.documents["pricing.yaml"] = pricing
            prepared.notes.append(f"Посёлок «{settlement}» добавлен в районные цены.")

    media_doc = editor.load("media.yaml")
    artifacts = media_doc.get("artifacts")
    if artifacts is None:
        media_doc["artifacts"] = artifacts = []
    by_id = {artifact.get("id"): artifact for artifact in artifacts}
    location_id = f"{LOCATION_PREFIX}{gym_id}"
    if _text(gym.get("address"), "ru") and location_id not in by_id:
        artifacts.append(_location_artifact(gym, gym_id, title))
        by_id[location_id] = artifacts[-1]
        prepared.documents["media.yaml"] = media_doc
        prepared.notes.append("Создана карточка адреса — бот отправит её, когда спросят, где зал.")

    gym_media = gym.get("media")
    if gym_media is None:
        gym["media"] = gym_media = []
    for own in (location_id, f"{ROUTE_PREFIX}{gym_id}"):
        if own in by_id and own not in gym_media:
            gym_media.append(own)
    return prepared


def _location_artifact(gym: Any, gym_id: str, title: str) -> dict[str, Any]:
    """Карточка адреса: текст собирает бот из адреса, ориентира и ссылки на карту зала."""
    title_kk = _text(gym.get("title"), "kk") or title
    return {
        "id": f"{LOCATION_PREFIX}{gym_id}",
        "kind": "location_text",
        "enabled": True,
        "scope": gym.get("scope") or "any",
        "gym_id": gym_id,
        "title": {"ru": f"Как найти зал — {title}", "kk": f"Залды қалай табуға болады — {title_kk}"},
        "when_to_send_ru": _LOCATION_WHEN.format(title=title),
        "body": None,
        "file_path": None,
        "file_mime": None,
        "file_bytes": None,
        "file_sha256": None,
        "channels": {"telegram": "allow", "whatsapp": "allow", "instagram": "allow"},
        "max_send_per_dialog": 1,
        "gap_ref": None,
        "render_from": "gyms",
    }


def route_video_artifact(
    gym: Any, gym_id: str, *, file_name: str, size: int, sha256: str, current: Any | None
) -> dict[str, Any]:
    """Запись видео дороги зала: прежние подписи и настройки сохраняются, файл — новый.

    WhatsApp принимает вложение до предела канала (10 МБ у Wazzup): видео больше уходит
    только в Telegram, и CRM прямо говорит об этом при загрузке.
    """
    title = _text(gym.get("title"), "ru") or gym_id
    title_kk = _text(gym.get("title"), "kk") or title
    fits_whatsapp = size <= CHANNEL_LIMITS[ChannelKind.WHATSAPP].max_file_bytes
    base: dict[str, Any] = dict(current or {})
    base.update(
        {
            "id": f"{ROUTE_PREFIX}{gym_id}",
            "kind": "video",
            "enabled": True,
            "scope": base.get("scope") or "any",
            "gym_id": gym_id,
            "title": base.get("title") or {"ru": f"Как добраться — {title}", "kk": f"Қалай жетуге болады — {title_kk}"},
            "when_to_send_ru": base.get("when_to_send_ru") or _ROUTE_WHEN.format(title=title),
            "body": base.get("body") or {"ru": None, "kk": None},
            "file_path": file_name,
            "file_mime": "video/mp4",
            "file_bytes": size,
            "file_sha256": sha256,
            "channels": {
                "telegram": "allow",
                "whatsapp": "allow" if fits_whatsapp else "deny",
                "instagram": "deny",
            },
            "max_send_per_dialog": base.get("max_send_per_dialog") or 1,
            "gap_ref": None,
            "render_from": "route",
        }
    )
    return base


def attach_route_video(
    editor: Any, gym_id: str, *, file_name: str, size: int, sha256: str
) -> Prepared:
    """Ставит видео дороги залу: заводит или обновляет ``route_<id>`` и привязывает к залу."""
    gyms_doc = editor.load("gyms.yaml")
    gym = find_gym(gyms_doc, gym_id)
    if gym is None:
        raise KeyError(gym_id)
    media_doc = editor.load("media.yaml")
    artifacts = media_doc.get("artifacts")
    if artifacts is None:
        media_doc["artifacts"] = artifacts = []
    route_id = f"{ROUTE_PREFIX}{gym_id}"
    position = next((i for i, item in enumerate(artifacts) if item.get("id") == route_id), -1)
    current = artifacts[position] if position >= 0 else None
    artifact = route_video_artifact(gym, gym_id, file_name=file_name, size=size, sha256=sha256, current=current)
    if position >= 0:
        for key, value in artifact.items():
            artifacts[position][key] = value
    else:
        artifacts.append(artifact)
    gym_media = gym.get("media")
    if gym_media is None:
        gym["media"] = gym_media = []
    if route_id not in gym_media:
        gym_media.append(route_id)
    notes = [] if artifact["channels"]["whatsapp"] == "allow" else [
        "Видео больше 10 МБ — в WhatsApp оно не уйдёт, только в Telegram. Сожмите и загрузите снова."
    ]
    return Prepared(documents={"gyms.yaml": gyms_doc, "media.yaml": media_doc}, notes=notes)


def companions(media_doc: Any, gym_id: str) -> list[str]:
    """Материалы, привязанные к залу: без зала они не проходят проверку базы."""
    return [
        str(artifact.get("id"))
        for artifact in media_doc.get("artifacts") or ()
        if artifact.get("gym_id") == gym_id
    ]


def remove_gym(editor: Any, gym_id: str) -> Prepared:
    """Удаляет зал вместе с его материалами. Файлы видео на диске остаются."""
    gyms_doc = editor.load("gyms.yaml")
    gyms = gyms_doc.get("gyms") or []
    position = next((i for i, gym in enumerate(gyms) if gym.get("id") == gym_id), -1)
    if position < 0:
        raise KeyError(gym_id)
    del gyms[position]
    prepared = Prepared(documents={"gyms.yaml": gyms_doc})
    media_doc = editor.load("media.yaml")
    owned = set(companions(media_doc, gym_id))
    if owned:
        _drop(media_doc["artifacts"], owned)
        prepared.documents["media.yaml"] = media_doc
        # Другие залы могли ссылаться на эти материалы — ссылки тоже снимаются.
        for gym in gyms:
            if any(item in owned for item in gym.get("media") or ()):
                gym["media"] = [item for item in gym.get("media") or () if item not in owned]
        prepared.notes.append("Вместе с залом убраны его материалы: " + ", ".join(sorted(owned)) + ".")
    return prepared


def _drop(sequence: Any, ids: set[str]) -> Any:
    """Удаляет элементы из списка ruamel на месте — комментарии соседей остаются."""
    for index in range(len(sequence) - 1, -1, -1):
        if sequence[index].get("id") in ids:
            del sequence[index]
    return sequence


__all__ = [
    "LOCATION_PREFIX",
    "Prepared",
    "ROUTE_PREFIX",
    "attach_route_video",
    "companions",
    "find_gym",
    "new_gym_id",
    "prepare_gym_save",
    "remove_gym",
    "route_video_artifact",
]
