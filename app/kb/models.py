"""Pydantic-схемы базы знаний AINAZAROV TOP TEAM.

База знаний — единственный источник фактов о школе. Ни цена, ни адрес, ни
расписание не живут в коде: код умеет считать и искать, промпт умеет
формулировать, **знать** умеет только KB.

Правила, общие для всех моделей этого модуля:

* ``frozen=True`` — снимок KB иммутабелен, менять его в рантайме нельзя;
* ``extra="forbid"`` — опечатка в имени поля YAML роняет загрузку, а не молча
  теряет данные;
* пустая строка ``""`` — **ошибка валидации**. «Нет данных» кодируется ``null``
  для скаляров и ``[]`` для списков (KB-SPEC §9.1);
* двуязычность обязательна: если поле заполнено, заполнены обе локали.

Модуль не читает окружение, не ходит в сеть и не открывает файлы: чтение и
сборка снимка — задача :mod:`app.kb.loader`.
"""

from __future__ import annotations

import re
import string
from datetime import date, datetime
from typing import Any, Final, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from app.types import (
    PHONE_E164_KZ_RE,
    SLUG_RE,
    ArtifactKind,
    ChannelKind,
    EscalationReason,
    FactSource,
    FollowupKind,
    GapRef,
    Gender,
    GymStatus,
    IntentHint,
    KBValidationError,
    Language,
    Plan,
    Scope,
)

# --------------------------------------------------------------------------- #
# Константы схемы
# --------------------------------------------------------------------------- #

#: Замороженный enum тем FAQ (INTERFACES §8.1). Из него строится enum `topic`
#: в JSON-схеме инструмента ``get_kb_fact``. Добавление темы = правка контракта.
FAQ_TOPICS: Final[tuple[str, ...]] = (
    "about",  # о школе: чем полезна, чем отличается, что даёт ребёнку
    "first_training",  # что происходит на первом занятии
    "fitness_level",  # не занимался раньше, слабый физически
    "experience",  # уже занимался другим видом спорта
    "parents",  # присутствие родителей, можно ли оставить ребёнка
    "siblings",  # брат и сестра вместе
    "group_change",  # перевод в другую группу
    "attendance",  # пропуск занятия, «можно прийти сегодня»
    "self_defense",  # бесплатный курс самообороны «Саламатты ұрпақ»
    "trial", "docs", "gear", "safety", "age_groups", "payment", "freeze", "coaches",
    "results", "girls", "adults", "instagram", "contacts", "offer",
    "sessions_count", "group_size", "competitions", "summer",
)

#: Закрытый список групп ключей ``i18n`` (INTERFACES §16.1). Группы `consent` нет.
#: ``card`` — подписи готовых карточек (список залов, прайс, расписание). Они
#: попадают клиенту дословно и обязаны быть на обоих языках, поэтому живут здесь,
#: а не в коде рендера.
I18N_GROUPS: Final[frozenset[str]] = frozenset(
    {"gap", "escalation", "error", "bridge", "followup", "lead_card", "greeting", "system", "card"}
)

#: Ключ i18n: ``<группа>.<slug>``.
I18N_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

#: Время в расписании — строго ``HH:MM``.
TIME_HHMM_RE: Final[re.Pattern[str]] = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

#: Плейсхолдер ``{snake_case}`` в строках i18n.
PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(r"\{([a-z][a-z0-9_]*)\}")

#: Эмодзи и пиктограммы: в текстах клиенту запрещены (тон бренда, INTERFACES §16.1).
EMOJI_RE: Final[re.Pattern[str]] = re.compile(
    "["
    "←-⇿"  # стрелки
    "⌀-⏿"  # технические знаки
    "①-⓿"  # кружочки
    "■-➿"  # геометрия и дингбаты
    "⬀-⯿"  # прочие стрелки и звёзды
    "️"          # variation selector
    "\U0001f000-\U0001faff"  # собственно эмодзи
    "]"
)

#: Markdown в мессенджерах не рендерится — только мусор в тексте.
MARKDOWN_RE: Final[re.Pattern[str]] = re.compile(r"(\*\*|__|```|^#{1,6}\s|\[[^\]]+\]\()", re.MULTILINE)

#: Максимальная длина готового ответа FAQ (KB-SPEC §3.1).
MAX_FAQ_ANSWER_CHARS: Final[int] = 600

#: Максимальная длина строки i18n и тела артефакта (INTERFACES §16.1, KB-SPEC §4.1).
MAX_I18N_CHARS: Final[int] = 900

#: Тарифные ключи, допустимые в ``city_plans`` / ``region_plans``.
PLAN_KEYS: Final[tuple[str, ...]] = (Plan.STANDARD.value, Plan.FLEXIBLE.value)

#: Дни недели в расписании.
WEEKDAYS: Final[tuple[str, ...]] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# --------------------------------------------------------------------------- #
# Служебные помощники валидации
# --------------------------------------------------------------------------- #
def _reject_blank(value: Any) -> Any:
    """Рекурсивно запрещает пустые и пробельные строки в любом поле.

    ``""`` означает «поле заполнили пробелами»: бот выдал бы пустоту вместо
    честного «уточню у администратора». Нет данных кодируется ``null`` / ``[]``.
    """
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("пустая строка запрещена: нет данных — пишите null (или [] для списка)")
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _reject_blank(item)
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_blank(key)
            _reject_blank(item)
        return value
    return value


def placeholders(text: str) -> frozenset[str]:
    """Множество плейсхолдеров ``{имя}`` в строке."""
    return frozenset(PLACEHOLDER_RE.findall(text))


class _SafeDict(dict):
    """Словарь для ``format_map``: неизвестный плейсхолдер остаётся как есть."""

    def __missing__(self, key: str) -> str:  # pragma: no cover - тривиально
        return "{" + key + "}"


def _format_safe(template: str, params: Mapping[str, object]) -> str:
    """Подстановка плейсхолдеров, не падающая на нехватке параметров.

    Живой диалог не должен обрываться из-за забытого параметра: недостающий
    плейсхолдер остаётся видимым в тексте и ловится тестом, а не клиентом.
    """
    try:
        return string.Formatter().vformat(template, (), _SafeDict(params))
    except (IndexError, KeyError, ValueError):
        return template


class _Base(BaseModel):
    """Общая база: иммутабельность, запрет лишних полей, запрет пустых строк."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def _no_blank_strings(cls, value: Any) -> Any:
        return _reject_blank(value)


# --------------------------------------------------------------------------- #
# Двуязычная пара
# --------------------------------------------------------------------------- #
class Bilingual(_Base):
    """Пара RU/KK. Обе локали обязательны, если поле вообще заполнено."""

    ru: str | None = None
    kk: str | None = None

    @model_validator(mode="after")
    def _both_or_neither(self) -> "Bilingual":
        if (self.ru is None) != (self.kk is None):
            missing = "kk" if self.ru is not None else "ru"
            raise ValueError(f"нет перевода '{missing}': заполните обе локали или обе оставьте null")
        return self

    @property
    def filled(self) -> bool:
        """Заполнена ли пара (обе локали)."""
        return self.ru is not None and self.kk is not None

    def get(self, lang: Language) -> str | None:
        """Текст на нужном языке; ``None``, если данных нет."""
        return self.kk if lang is Language.KK else self.ru


EMPTY_BILINGUAL: Final[Bilingual] = Bilingual()


def _max_len(value: Bilingual, limit: int, what: str) -> None:
    for lang_code, text in (("ru", value.ru), ("kk", value.kk)):
        if text is not None and len(text) > limit:
            raise ValueError(f"{what}: {lang_code} длиннее {limit} знаков ({len(text)})")


def _no_markup(value: Bilingual, what: str) -> None:
    for lang_code, text in (("ru", value.ru), ("kk", value.kk)):
        if text is None:
            continue
        if EMOJI_RE.search(text):
            raise ValueError(f"{what}: {lang_code} содержит эмодзи — в текстах клиенту они запрещены")
        if MARKDOWN_RE.search(text):
            raise ValueError(f"{what}: {lang_code} содержит markdown — мессенджер его не отрисует")


# --------------------------------------------------------------------------- #
# gyms.yaml
# --------------------------------------------------------------------------- #
class Coach(_Base):
    """Тренер зала. Пробел G-8: пока список пуст у всех залов."""

    name: str
    credentials: str | None = None
    groups: list[str] = Field(default_factory=list)
    speaks: list[Language] = Field(default_factory=list)


class ScheduleSlot(_Base):
    """Одна строка расписания зала.

    ``discipline`` обязательна: в одном зале бокс и кикбоксинг идут в разные дни,
    и родителю это важно — он записывает ребёнка на конкретный вид.

    ``age_from``/``age_to`` НЕОБЯЗАТЕЛЬНЫ. В расписании, которое передал владелец,
    возрастных групп нет вовсе, а придумывать их нельзя: бот скажет «в это время
    занимается группа, возраст уточнит администратор». Как только группы появятся,
    поля заполнятся и фильтр по возрасту заработает сам.
    """

    discipline: Literal["boxing", "kickboxing"]
    age_from: int | None = Field(default=None, ge=3, le=99)
    age_to: int | None = Field(default=None, ge=3, le=99)
    days: list[Weekday]
    time_start: str
    time_end: str
    shift: Literal["first", "second", "any"] = "any"
    note: Bilingual | None = None

    @field_validator("time_start", "time_end")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        if not TIME_HHMM_RE.match(value):
            raise ValueError(f"время '{value}' не в формате HH:MM")
        return value

    @field_validator("days")
    @classmethod
    def _days_unique(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("дни занятий не указаны")
        if len(set(value)) != len(value):
            raise ValueError("день недели повторяется")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "ScheduleSlot":
        if self.age_from is not None and self.age_to is not None and self.age_from > self.age_to:
            raise ValueError("age_from больше age_to")
        if (self.age_from is None) != (self.age_to is None):
            raise ValueError("age_from и age_to указываются вместе либо не указываются вовсе")
        if self.time_start >= self.time_end:
            raise ValueError("time_start не раньше time_end")
        return self

    @property
    def age_known(self) -> bool:
        """Известны ли границы возраста. ``False`` — бот отправит к администратору."""
        return self.age_from is not None and self.age_to is not None


class Gym(_Base):
    """Точка школы: зал в Костанае или секция в райцентре."""

    id: str
    scope: Scope
    settlement: str
    is_head: bool = False
    active: bool = True
    status: GymStatus = GymStatus.OPEN
    title: Bilingual
    address: Bilingual = EMPTY_BILINGUAL
    landmark: Bilingual = EMPTY_BILINGUAL
    district: Bilingual = EMPTY_BILINGUAL
    district_aliases: list[str] = Field(default_factory=list)
    #: Соседние районы, откуда в этот зал реально ездят и ходят пешком.
    #: НЕ то же самое, что district_aliases: там — как называют район САМОГО зала,
    #: здесь — районы, где зала нет. Смешивать нельзя: по алиасу бот скажет
    #: «наш зал в Аэропорту», а это неправда. По этому полю он скажет честно:
    #: «в Аэропорту зала нет, ближайший — на Полевой, оттуда пешком».
    serves_districts: list[str] = Field(default_factory=list)
    geo_lat: float | None = None
    geo_lon: float | None = None
    map_url: str | None = None
    phone: str | None = None
    coaches: list[Coach] = Field(default_factory=list)
    schedule: list[ScheduleSlot] = Field(default_factory=list)
    capacity_note: Bilingual | None = None
    media: list[str] = Field(default_factory=list)
    gap_refs: list[GapRef] = Field(default_factory=list)
    internal_note: str | None = None

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError(f"id '{value}' не подходит под ^[a-z0-9_]+$")
        return value

    @field_validator("scope")
    @classmethod
    def _scope_city_or_region(cls, value: Scope) -> Scope:
        if value not in (Scope.CITY, Scope.REGION):
            raise ValueError("scope зала — только city или region")
        return value

    @field_validator("district_aliases")
    @classmethod
    def _aliases(cls, value: list[str]) -> list[str]:
        for alias in value:
            if alias != alias.lower():
                raise ValueError(f"алиас '{alias}' должен быть в нижнем регистре")
        if len(set(value)) != len(value):
            raise ValueError("алиас района повторяется внутри одного зала")
        return value

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        if value is not None and not PHONE_E164_KZ_RE.match(value):
            raise ValueError(f"телефон '{value}' не в формате +7XXXXXXXXXX")
        return value

    @field_validator("media")
    @classmethod
    def _media_slugs(cls, value: list[str]) -> list[str]:
        for artifact_id in value:
            if not SLUG_RE.match(artifact_id):
                raise ValueError(f"artifact_id '{artifact_id}' не подходит под ^[a-z0-9_]+$")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "Gym":
        if (self.geo_lat is None) != (self.geo_lon is None):
            raise ValueError("geo_lat и geo_lon задаются оба или ни одного")
        if self.geo_lat is not None and not (-90.0 <= self.geo_lat <= 90.0):
            raise ValueError("geo_lat вне диапазона -90..90")
        if self.geo_lon is not None and not (-180.0 <= self.geo_lon <= 180.0):
            raise ValueError("geo_lon вне диапазона -180..180")
        if self.status is GymStatus.UNRESOLVED and self.active:
            raise ValueError("зал со status=unresolved обязан быть active=false")
        _no_markup(self.title, f"gyms[{self.id}].title")
        return self

    @property
    def has_schedule(self) -> bool:
        """Есть ли хоть одна строка расписания (для зеркала в БД и отчёта)."""
        return bool(self.schedule)


class GymsFile(_Base):
    """Файл ``kb/gyms.yaml`` целиком."""

    schema_version: int
    updated_at: date
    updated_by: Literal["owner", "admin", "dev"] = "owner"
    timezone: str = "Asia/Almaty"
    city_settlement: str
    gyms: list[Gym]

    @model_validator(mode="after")
    def _registry_rules(self) -> "GymsFile":
        if not self.gyms:
            raise ValueError("в gyms.yaml нет ни одного зала")
        seen: set[str] = set()
        for gym in self.gyms:
            if gym.id in seen:
                raise ValueError(f"дубликат gym_id '{gym.id}'")
            seen.add(gym.id)
            if (gym.scope is Scope.CITY) != (gym.settlement == self.city_settlement):
                raise ValueError(
                    f"зал '{gym.id}': scope=city обязан совпадать с settlement == '{self.city_settlement}'"
                )
        heads = [gym.id for gym in self.gyms if gym.is_head]
        if len(heads) > 1:
            raise ValueError(f"головным помечено больше одного зала: {', '.join(sorted(heads))}")
        # Непересечение алиасов: иначе матчинг района неоднозначен. Исключение —
        # алиас, ведущий на запись со status=unresolved (нерешённый конфликт C-3).
        owners: dict[str, str] = {}
        for gym in self.gyms:
            if gym.status is GymStatus.UNRESOLVED:
                continue
            for alias in gym.district_aliases:
                if alias in owners:
                    raise ValueError(
                        f"алиас района '{alias}' указан и у '{owners[alias]}', и у '{gym.id}'"
                    )
                owners[alias] = gym.id
        return self


# --------------------------------------------------------------------------- #
# pricing.yaml
# --------------------------------------------------------------------------- #
class PlanPrice(_Base):
    """Тариф: цена, наличие перерасчёта и как его называть клиенту."""

    price: int = Field(gt=0)
    recalculation: bool = False
    label: Bilingual
    note: Bilingual | None = None

    @model_validator(mode="after")
    def _texts(self) -> "PlanPrice":
        if not self.label.filled:
            raise ValueError("label тарифа обязан быть заполнен на ru и kk")
        _no_markup(self.label, "plan.label")
        if self.note is not None:
            _no_markup(self.note, "plan.note")
        return self


class FamilyDiscount(_Base):
    """Семейная скидка города: проценты по порядку зачисления детей (C-4, C-5)."""

    type: Literal["percent_by_order", "fixed_per_child"]
    rules: list[tuple[int, int]] = Field(default_factory=list)
    applies_to: list[str] = Field(default_factory=list)
    applies_to_status: Literal["confirmed", "unconfirmed"] = "unconfirmed"
    base_rule: Literal["enrollment_order", "cheapest_first"] = "enrollment_order"
    base_rule_status: Literal["confirmed", "unconfirmed"] = "unconfirmed"
    max_children: int | None = None
    label: Bilingual

    @field_validator("rules", mode="before")
    @classmethod
    def _accept_dicts(cls, value: Any) -> Any:
        """Разрешает владельцу писать ``{child_index: 2, percent: 10}``.

        Внутри контракта правило — пара ``(child_index, percent)``; словарь в YAML
        читается человеком куда лучше, поэтому принимаем обе формы.
        """
        if isinstance(value, list):
            converted: list[Any] = []
            for item in value:
                if isinstance(item, dict):
                    unknown = set(item) - {"child_index", "percent"}
                    if unknown:
                        raise ValueError(f"неизвестные ключи правила скидки: {sorted(unknown)}")
                    if "child_index" not in item or "percent" not in item:
                        raise ValueError("в правиле скидки нужны child_index и percent")
                    converted.append((item["child_index"], item["percent"]))
                else:
                    converted.append(item)
            return converted
        return value

    @field_validator("applies_to")
    @classmethod
    def _known_plans(cls, value: list[str]) -> list[str]:
        allowed = {plan.value for plan in Plan if plan is not Plan.UNKNOWN}
        for plan in value:
            if plan not in allowed:
                raise ValueError(f"скидка ссылается на неизвестный тариф '{plan}'")
        return value

    @model_validator(mode="after")
    def _rules_sane(self) -> "FamilyDiscount":
        seen: set[int] = set()
        for child_index, percent in self.rules:
            if child_index < 1:
                raise ValueError(f"child_index={child_index}: нумерация детей начинается с 1")
            if child_index in seen:
                raise ValueError(f"правило для ребёнка №{child_index} задано дважды")
            seen.add(child_index)
            if not 0 <= percent <= 100:
                raise ValueError(f"скидка {percent}% вне диапазона 0..100")
        if self.max_children is not None:
            if self.max_children < 1:
                raise ValueError("max_children меньше 1")
            for child_index, _ in self.rules:
                if child_index > self.max_children:
                    raise ValueError(f"правило для ребёнка №{child_index} превышает max_children")
        if not self.label.filled:
            raise ValueError("label семейной скидки обязан быть заполнен на ru и kk")
        _no_markup(self.label, "family_discount.label")
        return self

    def percent_for(self, child_index: int) -> int:
        """Процент скидки для ребёнка по порядку (нет правила — 0)."""
        for index, percent in self.rules:
            if index == child_index:
                return percent
        return 0


class DerivedFact(_Base):
    """Производный аргумент («выгода абонемента»).

    Произносится, только если ``derived_enabled``. Числа внутри обязаны быть
    следствием прайса — их пересчитывает валидатор прайса вручную при правке.
    """

    id: str
    ru: str
    kk: str

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError(f"id факта '{value}' не подходит под ^[a-z0-9_]+$")
        return value


class PricingFile(_Base):
    """Файл ``kb/pricing.yaml``: питает только детерминированный калькулятор."""

    schema_version: int
    updated_at: date | None = None
    updated_by: Literal["owner", "admin", "dev"] = "owner"
    currency: Literal["KZT"] = "KZT"
    rounding_mode: Literal["half_up", "floor", "none"] = "half_up"
    rounding_to: int = 10
    city_settlement: str
    city_sessions: int = Field(gt=0)
    city_validity_days: int = Field(gt=0)
    city_validity_note: Bilingual
    city_plans: dict[str, PlanPrice | None] = Field(default_factory=dict)
    city_single: PlanPrice | None = None
    city_family_discount: FamilyDiscount
    region_settlements: list[str] = Field(default_factory=list)
    region_plans: dict[str, PlanPrice | None] = Field(default_factory=dict)
    region_single: PlanPrice | None = None
    region_family_price_per_child: int | None = None
    region_family_min_children: int = 2
    region_family_label: Bilingual
    derived_enabled: bool = True
    derived_facts: list[DerivedFact] = Field(default_factory=list)
    payment_methods: list[str] = Field(default_factory=list)
    payment_details: Bilingual | None = None
    freeze_policy: Bilingual | None = None

    @field_validator("city_plans", "region_plans")
    @classmethod
    def _plan_keys(cls, value: dict[str, PlanPrice | None]) -> dict[str, PlanPrice | None]:
        unknown = set(value) - set(PLAN_KEYS)
        if unknown:
            raise ValueError(f"неизвестные тарифы: {sorted(unknown)}; допустимы {list(PLAN_KEYS)}")
        return value

    @field_validator("rounding_to")
    @classmethod
    def _rounding(cls, value: int) -> int:
        if value < 1:
            raise ValueError("rounding_to меньше 1")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "PricingFile":
        if self.region_family_price_per_child is not None and self.region_family_price_per_child <= 0:
            raise ValueError("region_family_price_per_child обязан быть больше нуля")
        if self.region_family_min_children < 1:
            raise ValueError("region_family_min_children меньше 1")
        if not self.city_validity_note.filled:
            raise ValueError("city_validity_note обязан быть заполнен на ru и kk")
        if not self.region_family_label.filled:
            raise ValueError("region_family_label обязан быть заполнен на ru и kk")
        if self.city_plans.get(Plan.STANDARD.value) is None:
            raise ValueError("city_plans.standard не может быть пустым: это базовый городской тариф")
        seen: set[str] = set()
        for fact in self.derived_facts:
            if fact.id in seen:
                raise ValueError(f"дубликат derived_facts.id '{fact.id}'")
            seen.add(fact.id)
        for note in (self.payment_details, self.freeze_policy):
            if note is not None:
                _no_markup(note, "pricing.note")
        return self

    def plan(self, scope: Scope, plan: str) -> PlanPrice | None:
        """Тариф по географии; ``None`` — данных нет (пробел G-10)."""
        plans = self.city_plans if scope is Scope.CITY else self.region_plans
        return plans.get(plan)


# --------------------------------------------------------------------------- #
# faq.yaml
# --------------------------------------------------------------------------- #
class FaqEntry(_Base):
    """Готовый ответ на типовой вопрос. Полный текст отдаёт tool, не промпт."""

    id: str
    topic: str
    scope: Scope = Scope.ANY
    question_variants: dict[Language, list[str]] = Field(default_factory=dict)
    answer: Bilingual = EMPTY_BILINGUAL
    source: FactSource = FactSource.OWNER_CONFIRMED
    gap_ref: GapRef | None = None
    escalate_if_empty: bool = True
    requires_tool: str | None = None
    forbidden_claims: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError(f"faq.id '{value}' не подходит под ^[a-z0-9_]+$")
        return value

    @field_validator("topic")
    @classmethod
    def _known_topic(cls, value: str) -> str:
        if value not in FAQ_TOPICS:
            raise ValueError(f"тема '{value}' вне замороженного enum FAQ_TOPICS")
        return value

    @model_validator(mode="after")
    def _answer_rules(self) -> "FaqEntry":
        _max_len(self.answer, MAX_FAQ_ANSWER_CHARS, f"faq[{self.id}].answer")
        _no_markup(self.answer, f"faq[{self.id}].answer")
        if not self.answer.filled and not self.escalate_if_empty:
            raise ValueError(
                f"faq[{self.id}]: ответа нет, значит escalate_if_empty обязан быть true — "
                "иначе бот промолчит вместо честной передачи администратору"
            )
        return self

    @property
    def answered(self) -> bool:
        """Есть ли готовый ответ на обеих локалях."""
        return self.answer.filled


class FaqFile(_Base):
    """Файл ``kb/faq.yaml``."""

    schema_version: int
    updated_at: date | None = None
    updated_by: Literal["owner", "admin", "dev"] = "owner"
    entries: list[FaqEntry]

    @model_validator(mode="after")
    def _unique_ids(self) -> "FaqFile":
        seen: set[str] = set()
        for entry in self.entries:
            if entry.id in seen:
                raise ValueError(f"дубликат faq.id '{entry.id}'")
            seen.add(entry.id)
        return self


# --------------------------------------------------------------------------- #
# media.yaml
# --------------------------------------------------------------------------- #
#: Виды артефактов, у которых есть файл на диске и обязательны поля ``file_*``.
#: Видео сюда входит, но отправляется вложением только в Telegram — в остальных
#: каналах его подменяет ссылка (``video_as_link_only``).
FILE_ARTIFACT_KINDS: Final[frozenset[ArtifactKind]] = frozenset(
    {ArtifactKind.IMAGE, ArtifactKind.DOCUMENT, ArtifactKind.VIDEO}
)


class Artifact(_Base):
    """Единица контента, которую бот умеет «отправить по требованию»."""

    id: str
    kind: ArtifactKind
    enabled: bool = True
    scope: Scope = Scope.ANY
    gym_id: str | None = None
    title: Bilingual
    when_to_send_ru: str
    body: Bilingual | None = None
    file_path: str | None = None
    file_mime: str | None = None
    file_bytes: int | None = None
    file_sha256: str | None = None
    channels: dict[ChannelKind, Literal["allow", "deny"]] = Field(default_factory=dict)
    max_send_per_dialog: int = 1
    gap_ref: GapRef | None = None
    render_from: Literal["gyms", "pricing", "route"] | None = None

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError(f"artifact.id '{value}' не подходит под ^[a-z0-9_]+$")
        return value

    @field_validator("max_send_per_dialog")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_send_per_dialog меньше 1")
        return value

    @field_validator("file_bytes")
    @classmethod
    def _bytes(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("file_bytes обязан быть больше нуля")
        return value

    @field_validator("file_sha256")
    @classmethod
    def _sha(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("file_sha256 — 64 знака нижнего регистра [0-9a-f]")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "Artifact":
        if not self.title.filled:
            raise ValueError(f"artifact[{self.id}]: title обязан быть заполнен на ru и kk")
        _no_markup(self.title, f"artifact[{self.id}].title")
        if self.body is not None:
            _max_len(self.body, MAX_I18N_CHARS, f"artifact[{self.id}].body")
        file_fields = (self.file_path, self.file_mime, self.file_bytes, self.file_sha256)
        if self.kind in FILE_ARTIFACT_KINDS:
            if self.enabled and any(field is None for field in file_fields):
                raise ValueError(
                    f"artifact[{self.id}]: для image/document/video нужны все поля file_* "
                    "(или enabled: false, пока файла нет)"
                )
        else:
            if any(field is not None for field in file_fields):
                raise ValueError(
                    f"artifact[{self.id}]: поля file_* только у image/document/video"
                )
            if self.render_from is None and self.enabled and not (self.body and self.body.filled):
                raise ValueError(
                    f"artifact[{self.id}]: без render_from нужен готовый body на ru и kk"
                )
        if self.render_from == "route" and self.gym_id is None:
            raise ValueError(f"artifact[{self.id}]: render_from: route требует gym_id")
        if self.render_from is not None and self.kind in FILE_ARTIFACT_KINDS:
            # Файловому артефакту render_from задаёт ПОДПИСЬ, а не тело: видео
            # маршрута подписывается адресом и расписанием зала, собранными из
            # базы знаний. Статичная подпись жила бы своей жизнью: владелец
            # поменял время занятий в CRM, а под видео годами висит старое.
            if self.render_from not in ("route", "gyms"):
                raise ValueError(
                    f"artifact[{self.id}]: у файлового артефакта подпись собирается "
                    "только из gyms или route"
                )
        if self.gym_id is not None and not SLUG_RE.match(self.gym_id):
            raise ValueError(f"artifact[{self.id}]: gym_id '{self.gym_id}' не подходит под ^[a-z0-9_]+$")
        return self


class MediaFile(_Base):
    """Файл ``kb/media.yaml``."""

    schema_version: int
    updated_at: date | None = None
    updated_by: Literal["owner", "admin", "dev"] = "dev"
    artifacts: list[Artifact]

    @model_validator(mode="after")
    def _unique_ids(self) -> "MediaFile":
        seen: set[str] = set()
        for artifact in self.artifacts:
            if artifact.id in seen:
                raise ValueError(f"дубликат artifact.id '{artifact.id}'")
            seen.add(artifact.id)
        return self


# --------------------------------------------------------------------------- #
# policies.yaml
# --------------------------------------------------------------------------- #
class FollowupRule(_Base):
    """Строка политики follow-up. Продуктовое правило, согласия не требует."""

    event: FollowupKind
    delay_hours: int
    only_work_hours: bool = True
    template_id: str
    max_times: int = Field(ge=0)

    @field_validator("template_id")
    @classmethod
    def _i18n_key(cls, value: str) -> str:
        if not I18N_KEY_RE.match(value) or not value.startswith("followup."):
            raise ValueError(f"template_id '{value}' обязан быть ключом i18n группы followup")
        return value


class PoliciesFile(_Base):
    """Организационный слой. Юридических полей НЕТ — см. SCOPE-OVERRIDE §1."""

    schema_version: int
    updated_at: date | None = None
    updated_by: Literal["owner", "admin", "dev"] = "owner"
    org_brand: str
    org_city: str
    audience_adults_only: bool = True
    audience_child_detected_action: Literal["escalate", "stop"] = "escalate"
    work_hours: Bilingual | None = None
    sla_reply_minutes: int | None = None
    escalation_triggers: list[EscalationReason] = Field(default_factory=list)
    escalation_pause_minutes: int = Field(default=60, ge=1)
    followup_policy: list[FollowupRule] = Field(default_factory=list)
    followup_stop_words: list[str] = Field(default_factory=list)
    forbidden_behaviour: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> "PoliciesFile":
        if self.sla_reply_minutes is not None and self.sla_reply_minutes < 1:
            raise ValueError("sla_reply_minutes меньше 1")
        if self.work_hours is not None and not self.work_hours.filled and self.work_hours != EMPTY_BILINGUAL:
            raise ValueError("work_hours: нужны обе локали")
        seen: set[FollowupKind] = set()
        for rule in self.followup_policy:
            if rule.event in seen:
                raise ValueError(f"дубликат правила follow-up '{rule.event.value}'")
            seen.add(rule.event)
        lowered = [word.lower() for word in self.followup_stop_words]
        if len(set(lowered)) != len(lowered):
            raise ValueError("стоп-слово follow-up повторяется")
        return self


# --------------------------------------------------------------------------- #
# i18n.yaml
# --------------------------------------------------------------------------- #
class I18nFile(_Base):
    """Все строки, которые бот произносит без участия модели."""

    schema_version: int
    updated_at: date | None = None
    updated_by: Literal["owner", "admin", "dev"] = "dev"
    strings: dict[str, Bilingual]

    @model_validator(mode="after")
    def _keys_and_texts(self) -> "I18nFile":
        for key, value in self.strings.items():
            if not I18N_KEY_RE.match(key):
                raise ValueError(f"ключ i18n '{key}' не подходит под <группа>.<slug>")
            group = key.split(".", 1)[0]
            if group not in I18N_GROUPS:
                raise ValueError(
                    f"группа '{group}' вне закрытого списка: {sorted(I18N_GROUPS)}"
                )
            if not value.filled:
                raise ValueError(f"i18n '{key}': обязательны обе локали ru и kk")
            _max_len(value, MAX_I18N_CHARS, f"i18n[{key}]")
            _no_markup(value, f"i18n[{key}]")
            if placeholders(value.ru or "") != placeholders(value.kk or ""):
                raise ValueError(
                    f"i18n '{key}': множества плейсхолдеров ru и kk различаются — "
                    "на казахском потеряется подстановка"
                )
        return self


# --------------------------------------------------------------------------- #
# lexicon.yaml
# --------------------------------------------------------------------------- #
class LexiconFile(_Base):
    """Разговорный слой Костаная: сленг, опечатки, транслит, районы."""

    schema_version: int
    updated_at: date | None = None
    updated_by: Literal["owner", "admin", "dev"] = "dev"
    intents: dict[IntentHint, list[str]] = Field(default_factory=dict)
    kk_graphemes: list[str] = Field(default_factory=list)
    kk_words: list[str] = Field(default_factory=list)
    kk_translit: list[str] = Field(default_factory=list)
    ru_translit: list[str] = Field(default_factory=list)
    age_patterns: list[str] = Field(default_factory=list)
    gender_markers: dict[Gender, list[str]] = Field(default_factory=dict)
    districts_extra: list[str] = Field(default_factory=list)

    @field_validator("age_patterns")
    @classmethod
    def _compilable(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:  # pragma: no cover - защита от опечатки владельца
                raise ValueError(f"age_patterns: регулярное выражение '{pattern}' не компилируется: {exc}")
        return value

    @model_validator(mode="after")
    def _lowercase(self) -> "LexiconFile":
        for name, words in (
            ("intents", [word for words_ in self.intents.values() for word in words_]),
            ("kk_words", self.kk_words),
            ("kk_translit", self.kk_translit),
            ("ru_translit", self.ru_translit),
            ("districts_extra", self.districts_extra),
        ):
            for word in words:
                if word != word.lower():
                    raise ValueError(f"{name}: '{word}' должно быть в нижнем регистре")
        return self


# --------------------------------------------------------------------------- #
# Снимок
# --------------------------------------------------------------------------- #
def cross_reference_errors(
    *,
    gyms: GymsFile,
    pricing: PricingFile,
    faq: Sequence[FaqEntry],
    media: Sequence[Artifact],
    policies: PoliciesFile,
    i18n: I18nFile,
) -> list[str]:
    """Проверяет ссылки между файлами KB. Возвращает **все** ошибки, а не первую.

    Ловит именно то, что валидатор одного файла увидеть не может: ссылку на
    несуществующий ``gym_id`` / ``artifact_id`` и отсутствующий ключ i18n.
    """
    errors: list[str] = []
    gym_ids = {gym.id for gym in gyms.gyms}
    artifact_ids = {artifact.id for artifact in media}
    i18n_keys = set(i18n.strings)

    for gym in gyms.gyms:
        for artifact_id in gym.media:
            if artifact_id not in artifact_ids:
                errors.append(
                    f"gyms.yaml: зал '{gym.id}' ссылается на несуществующий артефакт '{artifact_id}'"
                )
    for artifact in media:
        if artifact.gym_id is not None and artifact.gym_id not in gym_ids:
            errors.append(
                f"media.yaml: артефакт '{artifact.id}' ссылается на несуществующий зал '{artifact.gym_id}'"
            )
        # Префикс gym_location_<gym_id> зафиксирован контрактом (INTERFACES §16.2).
        if artifact.id.startswith("gym_location_"):
            suffix = artifact.id[len("gym_location_"):]
            if suffix not in gym_ids:
                errors.append(
                    f"media.yaml: '{artifact.id}' — суффикс обязан совпадать с существующим gyms[].id"
                )
    for rule in policies.followup_policy:
        if rule.template_id not in i18n_keys:
            errors.append(
                f"policies.yaml: follow-up '{rule.event.value}' ссылается на "
                f"отсутствующий ключ i18n '{rule.template_id}'"
            )
    if pricing.city_settlement != gyms.city_settlement:
        errors.append(
            f"pricing.yaml: city_settlement '{pricing.city_settlement}' не совпадает с "
            f"gyms.yaml '{gyms.city_settlement}'"
        )
    region_settlements = {
        gym.settlement for gym in gyms.gyms if gym.scope is Scope.REGION and gym.active
    }
    for settlement in sorted(region_settlements - set(pricing.region_settlements)):
        errors.append(
            f"pricing.yaml: райцентр '{settlement}' есть в gyms.yaml, но отсутствует в region_settlements"
        )
    for entry in faq:
        if entry.gap_ref is not None and entry.answered:
            errors.append(
                f"faq.yaml: '{entry.id}' одновременно имеет ответ и ссылку на пробел "
                f"{entry.gap_ref.value} — уберите gap_ref после заполнения"
            )
    return errors


class KBSnapshot(BaseModel):
    """Иммутабельный снимок базы знаний. Живёт в памяти, меняется атомарным swap."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kb_hash: str
    loaded_at: datetime
    gyms: GymsFile
    pricing: PricingFile
    faq: list[FaqEntry]
    media: list[Artifact]
    policies: PoliciesFile
    i18n: I18nFile
    lexicon: LexiconFile

    _gym_index: dict[str, Gym] = PrivateAttr(default_factory=dict)
    _artifact_index: dict[str, Artifact] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        self._gym_index = {gym.id: gym for gym in self.gyms.gyms}
        self._artifact_index = {artifact.id: artifact for artifact in self.media}

    @model_validator(mode="after")
    def _cross_references(self) -> "KBSnapshot":
        errors = cross_reference_errors(
            gyms=self.gyms,
            pricing=self.pricing,
            faq=self.faq,
            media=self.media,
            policies=self.policies,
            i18n=self.i18n,
        )
        if errors:
            raise ValueError("; ".join(errors))
        return self

    # --- залы ------------------------------------------------------------- #
    def gym(self, gym_id: str) -> Gym | None:
        """Зал по идентификатору (в том числе неактивный)."""
        return self._gym_index.get(gym_id)

    def active_gyms(self, scope: Scope = Scope.ALL) -> tuple[Gym, ...]:
        """Только ``active=True`` и ``status=open``, отсортированы по ``id``."""
        selected = [
            gym
            for gym in self.gyms.gyms
            if gym.active
            and gym.status is GymStatus.OPEN
            and (scope in (Scope.ALL, Scope.ANY) or gym.scope is scope)
        ]
        return tuple(sorted(selected, key=lambda gym: gym.id))

    def gym_ids(self) -> tuple[str, ...]:
        """Enum для схем инструментов: id активных и открытых залов, отсортированы."""
        return tuple(gym.id for gym in self.active_gyms(Scope.ALL))

    def head_gym(self) -> Gym | None:
        """Головной зал, если он помечен и работает."""
        for gym in self.active_gyms(Scope.CITY):
            if gym.is_head:
                return gym
        return None

    def unresolved_gyms(self) -> tuple[Gym, ...]:
        """Записи-заглушки нерешённых конфликтов (C-3, КЖБИ), отсортированы по id."""
        selected = [gym for gym in self.gyms.gyms if gym.status is GymStatus.UNRESOLVED]
        return tuple(sorted(selected, key=lambda gym: gym.id))

    # --- артефакты -------------------------------------------------------- #
    def artifact(self, artifact_id: str) -> Artifact | None:
        """Артефакт по идентификатору (в том числе выключенный)."""
        return self._artifact_index.get(artifact_id)

    def artifact_ids(self) -> tuple[str, ...]:
        """Только ``enabled=True``, отсортированы. Из них строится enum ``artifact_id``."""
        return tuple(sorted(artifact.id for artifact in self.media if artifact.enabled))

    # --- FAQ -------------------------------------------------------------- #
    def faq_topics(self) -> tuple[str, ...]:
        """Отсортированное множество тем FAQ — enum ``topic`` в ``get_kb_fact``."""
        return tuple(sorted({entry.topic for entry in self.faq}))

    def faq_entries(self, topic: str, scope: Scope = Scope.ANY) -> tuple[FaqEntry, ...]:
        """Все записи темы, подходящие под географию. Порядок детерминирован.

        Сначала записи с готовым ответом, затем по ``id``. Точное совпадение
        ``scope`` идёт раньше записи с ``scope: any``.
        """
        selected = [
            entry
            for entry in self.faq
            if entry.topic == topic
            and (
                scope in (Scope.ANY, Scope.ALL)
                or entry.scope is Scope.ANY
                or entry.scope is scope
            )
        ]
        return tuple(
            sorted(
                selected,
                key=lambda entry: (
                    0 if entry.answered else 1,
                    0 if entry.scope is scope else 1,
                    entry.id,
                ),
            )
        )

    def faq_entry(self, topic: str, scope: Scope) -> FaqEntry | None:
        """Лучшая запись темы под географию (см. :meth:`faq_entries`)."""
        entries = self.faq_entries(topic, scope)
        return entries[0] if entries else None

    # --- строки ----------------------------------------------------------- #
    def text(self, key: str, lang: Language, **params: object) -> str:
        """Строка i18n с подстановкой плейсхолдеров. Нет ключа -> KBValidationError."""
        value = self.i18n.strings.get(key)
        if value is None:
            raise KBValidationError(
                f"в kb/i18n.yaml нет ключа '{key}'",
                errors=(f"i18n.yaml: отсутствует ключ '{key}'",),
            )
        template = value.get(lang) or value.ru or ""
        return _format_safe(template, params)

    def bilingual_text(self, key: str, **params: object) -> dict[str, str]:
        """Пара ``{'ru': ..., 'kk': ...}`` — готовый ``say_if_no_data``."""
        return {
            Language.RU.value: self.text(key, Language.RU, **params),
            Language.KK.value: self.text(key, Language.KK, **params),
        }

    # --- пробелы ---------------------------------------------------------- #
    def gaps(self) -> tuple[GapRef, ...]:
        """Все пробелы, на которые ссылается KB, отсортированы по коду."""
        refs: set[GapRef] = set()
        for gym in self.gyms.gyms:
            refs.update(gym.gap_refs)
        for entry in self.faq:
            if entry.gap_ref is not None:
                refs.add(entry.gap_ref)
        for artifact in self.media:
            if artifact.gap_ref is not None:
                refs.add(artifact.gap_ref)
        if self.pricing.city_family_discount.applies_to_status == "unconfirmed":
            refs.add(GapRef.C4)
        if self.pricing.city_family_discount.base_rule_status == "unconfirmed":
            refs.add(GapRef.C5)
        if not self.pricing.payment_methods:
            refs.add(GapRef.G9)
        if self.pricing.freeze_policy is None or not self.pricing.freeze_policy.filled:
            refs.add(GapRef.G13)
        if self.pricing.region_plans.get(Plan.FLEXIBLE.value) is None or self.pricing.region_single is None:
            refs.add(GapRef.G10)
        return tuple(sorted(refs, key=_gap_sort_key))

    # --- прочее ----------------------------------------------------------- #
    def payment_methods(self) -> tuple[str, ...]:
        """Способы оплаты; пусто — пробел G-9."""
        return tuple(self.pricing.payment_methods)

    def district_alias_map(self) -> dict[str, str]:
        """``алиас района -> gym_id`` по всем залам, включая ``unresolved``."""
        mapping: dict[str, str] = {}
        for gym in sorted(self.gyms.gyms, key=lambda item: item.id):
            for alias in gym.district_aliases:
                mapping.setdefault(alias, gym.id)
        return mapping


def _gap_sort_key(gap: GapRef) -> tuple[str, int]:
    """Сортировка ``G-2`` раньше ``G-10``, конфликты ``C-*`` — после пробелов."""
    letter, _, number = gap.value.partition("-")
    return (letter, int(number))


def iter_bilinguals(model: BaseModel) -> Iterable[tuple[str, Bilingual]]:
    """Обходит все поля-:class:`Bilingual` модели. Нужен отчёту validate_kb."""
    for name, value in model:
        if isinstance(value, Bilingual):
            yield name, value


__all__ = [
    "Artifact",
    "Bilingual",
    "Coach",
    "DerivedFact",
    "EMPTY_BILINGUAL",
    "FAQ_TOPICS",
    "FamilyDiscount",
    "FaqEntry",
    "FaqFile",
    "FollowupRule",
    "Gym",
    "GymsFile",
    "I18N_GROUPS",
    "I18nFile",
    "KBSnapshot",
    "LexiconFile",
    "MediaFile",
    "PlanPrice",
    "PoliciesFile",
    "PricingFile",
    "ScheduleSlot",
    "cross_reference_errors",
    "placeholders",
]
