# KB-SPEC.md — база знаний AINAZAROV TOP TEAM

Спецификация YAML-базы знаний бота. Каталог: `/Users/a1111/Desktop/ainazarov-bot/kb/`.
Парная спецификация системы — `docs/ARCHITECTURE.md`. Фактура — `docs/CONTENT-AUDIT.md` и первоисточник
владельца продукта.

---

## 0. Правило единственного источника правды

1. **KB — единственный источник фактов.** Ни один факт о школе (цена, адрес, район, тариф, скидка, условие
   пробного, ссылка, номер телефона, имя тренера) не живёт в коде и не живёт в системном промпте.
   Код умеет считать и искать; промпт умеет формулировать; **знать** умеет только KB.
2. **Редактирование KB не требует правки кода и промпта.** Изменение YAML → `POST /admin/kb/reload`
   (или рестарт) → новые факты в промпте, в детерминированных tools и в enum'ах схем tool-функций одновременно.
   Владелец школы (или администратор) правит YAML — разработчик не нужен.
3. **Enum'ы схем генерируются из KB.** `gym_id`, `artifact_id`, `topic` в JSON-схемах Gemini собираются
   из загруженного KB. Добавили зал в `gyms.yaml` — модель немедленно может на него сослаться; удалили — не может.
4. **Валидация обязательна и блокирующая.** `app/kb/loader.py` при старте и при hot-reload проверяет все файлы
   Pydantic-схемами. Ошибка при старте = приложение **не поднимается**. Ошибка при hot-reload = новая версия
   **не применяется**, продолжает работать предыдущая, в лог — `kb_load_failures_total` и алерт.
5. **Версия KB отслеживается.** `kb_hash` = sha256 от детерминированной сериализации всех файлов. Пишется в
   `kb_version`, в `Conversation.kb_hash_at_start`, в каждую строку `llm_call` и в логи. Любой разбор инцидента
   «бот сказал не то» начинается с ответа «какая была версия KB».
6. **Двуязычность обязательна.** Всякое поле, попадающее в текст клиенту, имеет `ru` и `kk`. Отсутствие
   `kk`-версии — ошибка валидации, а не предупреждение: реклама на территории РК должна распространяться на
   казахском языке (ст. 6 п. 2 Закона «О рекламе»), а машинный перевод модели даёт нестабильные формулировки.
7. **Заглушка ≠ пустая строка.** Неизвестное значение = `null` (для скаляров) или `[]` (для списков).
   Пустая строка `""` — **ошибка валидации**: она означает «поле заполнили пробелами», и бот выдал бы пустоту
   вместо честного «уточню у администратора».

**Файлы:**

| Файл | Содержимое | Кто правит |
|---|---|---|
| `kb/gyms.yaml` | 12 точек: адреса, районы, алиасы, тренеры, расписание, геоданные | владелец / администратор |
| `kb/pricing.yaml` | тарифы, скидки, правила расчёта | владелец |
| `kb/faq.yaml` | ответы на типовые вопросы по темам | владелец + контент-редактор |
| `kb/media.yaml` | реестр отправляемых артефактов | контент-редактор |
| `kb/policies.yaml` | реквизиты, согласие, оферта, ретеншн, эскалация, режим работы | владелец + юрист |
| `kb/i18n.yaml` | все системные строки бота (не-LLM), RU/KK | разработчик + редактор |
| `kb/lexicon.yaml` | *(вспомогательный)* сленг, опечатки, транслит, названия районов | разработчик |

Общие поля-«шапки» в каждом файле:

```yaml
schema_version: 1          # версия схемы; несовпадение с ожидаемой в коде → отказ загрузки
updated_at: "2026-08-09"   # дата последней правки, показывается в админке
updated_by: "owner"        # кто правил: owner | admin | dev
```

---

## 1. `kb/gyms.yaml`

### 1.1 Схема

| Поле | Тип | Обяз. | Описание |
|---|---|---|---|
| `defaults.timezone` | str | да | `Asia/Almaty` (UTC+5, Костанай) |
| `defaults.city_settlement` | str | да | `Костанай` — по нему `scope: city` |
| `gyms[].id` | str `^[a-z0-9_]+$` | да | стабильный slug, **никогда не меняется** (на него ссылаются `Lead.gym_id`, `media.yaml`, метрики) |
| `gyms[].scope` | enum `city\|region` | да | **управляет прайсом**: `city` → 25 000/30 000/3 200 ₸, `region` → 10 000/8 000 ₸ |
| `gyms[].settlement` | str | да | Костанай, Карабалык, Фёдоровка, … |
| `gyms[].is_head` | bool | да | головной зал (для ответа «а где основной?») |
| `gyms[].active` | bool | да | `false` — зал не показывается и не выбирается tools |
| `gyms[].status` | enum `open\|unresolved\|closed` | да | `unresolved` — запись-заглушка для нерешённого конфликта (см. КЖБИ ниже) |
| `gyms[].title.{ru,kk}` | str | да | короткое имя для списка, ≤ 40 знаков |
| `gyms[].address.{ru,kk}` | str \| null | да | улица и дом; `null` = адрес неизвестен (райцентры, пробел G-3) |
| `gyms[].landmark.{ru,kk}` | str \| null | да | ориентир: «школа №9, цоколь», «магазин Рахат» |
| `gyms[].district.{ru,kk}` | str \| null | да | район, как его называет клиент |
| `gyms[].district_aliases` | list[str] | да | разговорные формы в нижнем регистре для матчинга; **сюда же опечатки и транслит** |
| `gyms[].geo.lat` / `.lon` | float \| null | да | пробел G-15 |
| `gyms[].geo.map_url` | str \| null | да | ссылка на 2ГИС/Google Maps |
| `gyms[].phone` | str \| null | да | E.164; пробел G-2 |
| `gyms[].coaches` | list[obj] \| `[]` | да | `{name, credentials, groups, speaks:[ru,kk]}`; пробел G-8 |
| `gyms[].schedule` | list[obj] \| `[]` | да | `{age_from, age_to, days:[mon..sun], time_start, time_end, shift: first\|second\|any, note}`; пробел G-1 |
| `gyms[].capacity_note.{ru,kk}` | str \| null | нет | «в группе до 15 детей» — если владелец подтвердит |
| `gyms[].media` | list[str] | нет | id артефактов из `media.yaml`, привязанных к залу |
| `gyms[].gap_refs` | list[str] | нет | какие пробелы актуальны для этой записи: `["G-1","G-2"]` |
| `gyms[].internal_note` | str | нет | **не показывается клиенту и не попадает в промпт** — заметка для владельца |

Валидатор дополнительно проверяет: уникальность `id`; `scope: city` ⟺ `settlement == defaults.city_settlement`;
непересечение `district_aliases` между залами (иначе матчинг неоднозначен) — кроме случая, когда алиас
намеренно указывает на `status: unresolved`; согласованность `geo.lat/lon` (оба или ни одного).

### 1.2 Пример (фрагмент, заполнено по факту на 2026-08-09)

```yaml
schema_version: 1
updated_at: "2026-08-09"
updated_by: "owner"

defaults:
  timezone: "Asia/Almaty"
  city_settlement: "Костанай"

gyms:
  - id: ksk_kairbekova_334
    scope: city
    settlement: "Костанай"
    is_head: true
    active: true
    status: open
    title:
      ru: "КСК — школа №9"
      kk: "КСК — №9 мектеп"
    address:
      ru: "Каирбекова 334, школа №9 (цоколь)"
      kk: "Қайырбеков көшесі 334, №9 мектеп (цокольде)"
    landmark:
      ru: "цокольный этаж школы №9"
      kk: "№9 мектептің цоколь қабаты"
    district:
      ru: "КСК"
      kk: "КСК"
    district_aliases: ["кск", "ksk", "каирбекова 334", "школа 9", "школа №9", "9 школа"]
    geo: { lat: null, lon: null, map_url: null }     # G-15
    phone: null                                       # G-2
    coaches: []                                       # G-8
    schedule: []                                      # G-1
    media: ["price_photo_city"]
    gap_refs: ["G-1", "G-2", "G-8", "G-15"]
    internal_note: "Головной зал по брифу владельца"

  - id: plaza_szm_70
    scope: city
    settlement: "Костанай"
    is_head: false
    active: true
    status: open
    title: { ru: "Костанай Плаза — AIMED", kk: "Қостанай Плаза — AIMED" }
    address:
      ru: "Северо-Западный микрорайон 70, участок 51, медцентр AIMED"
      kk: "Солтүстік-Батыс шағын ауданы 70, 51-учаске, AIMED медорталығы"
    landmark: { ru: "медицинский центр AIMED", kk: "AIMED медорталығы" }
    district: { ru: "Костанай Плаза", kk: "Қостанай Плаза" }
    district_aliases: ["плаза", "plaza", "костанай плаза", "северо-западный", "сзм", "аймед", "aimed"]
    geo: { lat: null, lon: null, map_url: null }
    phone: null
    coaches: []
    schedule: []
    gap_refs: ["G-1", "G-2", "G-8", "G-15"]

  - id: center_kasymkhanova_10
    scope: city
    settlement: "Костанай"
    is_head: false
    active: true
    status: open
    title: { ru: "Центр — Жана-Кала", kk: "Орталық — Жаңа Қала" }
    address: { ru: "Касымханова 10", kk: "Қасымханов көшесі 10" }
    landmark: { ru: "район Жана-Кала", kk: "Жаңа Қала ауданы" }
    district: { ru: "Центр / Жана-Кала", kk: "Орталық / Жаңа Қала" }
    district_aliases: ["жана кала", "жана-кала", "жаңа қала", "касымханова", "касымханова 10"]
    geo: { lat: null, lon: null, map_url: null }
    phone: null
    coaches: []
    schedule: []
    gap_refs: ["G-1", "G-2", "G-8", "G-15"]
    internal_note: "ВНИМАНИЕ: два зала заявлены как ЦЕНТР (этот и kairbekova_24). Различать по ориентиру."

  - id: center_kairbekova_24
    scope: city
    settlement: "Костанай"
    is_head: false
    active: true
    status: open
    title: { ru: "Центр — у магазина «Рахат»", kk: "Орталық — «Рахат» дүкені жанында" }
    address: { ru: "Каирбекова 24", kk: "Қайырбеков көшесі 24" }
    landmark: { ru: "магазин «Рахат»", kk: "«Рахат» дүкені" }
    district: { ru: "Центр", kk: "Орталық" }
    district_aliases: ["рахат", "каирбекова 24", "центр рахат"]
    geo: { lat: null, lon: null, map_url: null }
    phone: null
    coaches: []
    schedule: []
    gap_refs: ["G-1", "G-2", "G-8", "G-15"]

  - id: magazin15_voinov_8b
    scope: city
    settlement: "Костанай"
    is_head: false
    active: true
    status: open
    title: { ru: "15-й магазин — «Романтик»", kk: "15-дүкен — «Романтик»" }
    address: { ru: "Воинов-интернационалистов 8Б", kk: "Интернационалист-жауынгерлер көшесі 8Б" }
    landmark: { ru: "магазин «Романтик»", kk: "«Романтик» дүкені" }
    district: { ru: "район 15-го магазина", kk: "15-дүкен ауданы" }
    district_aliases: ["15 магазин", "15-й магазин", "пятнадцатый", "романтик", "воинов интернационалистов"]
    geo: { lat: null, lon: null, map_url: null }
    phone: null
    coaches: []
    schedule: []
    gap_refs: ["G-1", "G-2", "G-8", "G-15"]

  - id: mkr6_arystanbekova_6
    scope: city
    settlement: "Костанай"
    is_head: false
    active: true
    status: open
    title: { ru: "6-й микрорайон — у школы №10", kk: "6-шағын аудан — №10 мектеп жанында" }
    address: { ru: "Арыстанбекова 6", kk: "Арыстанбеков көшесі 6" }
    landmark: { ru: "возле школы №10", kk: "№10 мектептің жанында" }
    district: { ru: "6-й микрорайон", kk: "6-шағын аудан" }
    district_aliases: ["6 микрорайон", "шестой микрорайон", "6 мкр", "арыстанбекова", "школа 10"]
    geo: { lat: null, lon: null, map_url: null }
    phone: null
    coaches: []
    schedule: []
    gap_refs: ["G-1", "G-2", "G-8", "G-15"]
    internal_note: "Новый зал. Закреплённый креатив в Instagram говорит «5 залов» — конфликт C-1, креатив перевыпустить."

  # --- Райцентры: район известен, адрес НЕТ (пробел G-3) ---
  - id: region_karabalyk
    scope: region
    settlement: "Карабалык"
    is_head: false
    active: true
    status: open
    title: { ru: "Карабалык", kk: "Қарабалық" }
    address: { ru: null, kk: null }          # G-3
    landmark: { ru: null, kk: null }
    district: { ru: "Карабалык", kk: "Қарабалық" }
    district_aliases: ["карабалык", "қарабалық", "karabalyk"]
    geo: { lat: null, lon: null, map_url: null }
    phone: null
    coaches: []
    schedule: []
    gap_refs: ["G-1", "G-2", "G-3", "G-8"]
  # аналогично: region_fedorovka, region_sarykol, region_auliekol,
  #             region_uzynkol, region_zhitikara

  # --- Запись-заглушка для нерешённого конфликта C-3 ---
  - id: unresolved_kzhbi
    scope: city
    settlement: "Костанай"
    is_head: false
    active: false
    status: unresolved
    title: { ru: "КЖБИ — требует уточнения", kk: "КЖБИ — нақтылауды қажет етеді" }
    address: { ru: null, kk: null }
    landmark: { ru: null, kk: null }
    district: { ru: "КЖБИ", kk: "КЖБИ" }
    district_aliases: ["кжби", "кжби район", "kzhbi", "железобетон"]
    geo: { lat: null, lon: null, map_url: null }
    phone: null
    coaches: []
    schedule: []
    gap_refs: ["C-3"]
    internal_note: >
      В шапке Instagram заявлен район КЖБИ, в списке из 6 адресов его нет.
      До письменного ответа владельца бот НЕ утверждает ни что зал есть, ни что его нет:
      find_gym_by_district на алиас "кжби" возвращает status=needs_operator.
```

### 1.3 Поведение бота по этому файлу

- `get_gyms(scope)` отдаёт только `active: true, status: open`.
- `find_gym_by_district` матчит по `district_aliases` + нормализованному тексту клиента; попадание в запись со
  `status: unresolved` → `needs_operator` + фраза-заглушка (никаких догадок про КЖБИ).
- `address == null` (райцентры) → бот подтверждает наличие секции в населённом пункте и передаёт вопрос об
  адресе администратору.
- `schedule == []` → `get_schedule` возвращает `no_data`; пост-фильтр блокирует любой ответ с временем `HH:MM`.
- `phone == null` → бот не отдаёт номер и предлагает эскалацию внутри переписки.

---

## 2. `kb/pricing.yaml`

Питает **только** `tools/pricing.py`. Модель этот файл целиком в промпте не видит: в промпт рендерится лишь
краткая витрина («город: 25 000 / 30 000 / 3 200 ₸; райцентры: 10 000 / 8 000 ₸; считает инструмент») —
чтобы модель понимала, о чём речь, но не соблазнялась считать сама.

### 2.1 Схема

| Поле | Тип | Описание |
|---|---|---|
| `currency` | str | `KZT`. Цены только в тенге (ст. 6 п. 4-1 Закона «О рекламе») |
| `rounding.mode` | enum `half_up\|floor\|none` | правило округления итога |
| `rounding.to` | int | шаг округления в тенге |
| `city.subscription.sessions` | int | 12 |
| `city.subscription.validity_days` | int | 30 («действует в течение месяца») |
| `city.plans.<plan>.price` | int | цена в ₸ |
| `city.plans.<plan>.recalculation` | bool | есть ли перерасчёт за пропуски |
| `city.plans.<plan>.label.{ru,kk}` | str | как называть тариф клиенту |
| `city.plans.<plan>.note.{ru,kk}` | str \| null | обязательная оговорка (для `standard` — «перерасчёта и возврата нет») |
| `city.single.price` | int | разовая тренировка |
| `city.family_discount.type` | enum `percent_by_order\|fixed_per_child` | механика |
| `city.family_discount.rules[]` | list | `{child_index, percent}` |
| `city.family_discount.applies_to` | list[str] | к каким тарифам применяется |
| `city.family_discount.applies_to_status` | enum `confirmed\|unconfirmed` | **`unconfirmed` включает обязательную оговорку в ответе** (конфликт C-4) |
| `city.family_discount.base_rule` | enum `enrollment_order\|cheapest_first` | от чего считается «второй ребёнок» (конфликт C-5) |
| `city.family_discount.base_rule_status` | enum `confirmed\|unconfirmed` | |
| `city.family_discount.max_children` | int \| null | сверх этого числа правил нет → `needs_operator` |
| `region.plans.*` | те же поля | `null` = тарифа нет данных (пробел G-10) |
| `region.family.price_per_child` | int | 8 000 |
| `region.family.min_children` | int | 2 |
| `derived.enabled` | bool | разрешено ли боту произносить производные аргументы (выгода абонемента) |
| `payment_methods` | list \| `[]` | пробел G-9 |
| `freeze_policy.{ru,kk}` | str \| null | пробел G-13 |

### 2.2 Пример (заполнено по факту)

```yaml
schema_version: 1
updated_at: "2026-08-09"
updated_by: "owner"

currency: "KZT"
rounding: { mode: half_up, to: 10 }

city:
  settlement: "Костанай"
  subscription:
    sessions: 12
    validity_days: 30
    validity_note:
      ru: "Абонемент — 12 занятий, действует в течение месяца."
      kk: "Абонемент — 12 сабақ, бір ай ішінде жарамды."
  plans:
    standard:
      price: 25000
      recalculation: false
      label: { ru: "Стандартный", kk: "Стандартты" }
      note:
        ru: "Перерасчёта и возврата за пропущенные тренировки нет."
        kk: "Жіберілген жаттығулар үшін қайта есептеу және қайтару жоқ."
    flexible:
      price: 30000
      recalculation: true
      label: { ru: "Гибкий", kk: "Икемді" }
      note:
        ru: "С перерасчётом за пропущенные тренировки."
        kk: "Жіберілген жаттығулар қайта есептеледі."
  single:
    price: 3200
    label: { ru: "Разовая тренировка", kk: "Бір реттік жаттығу" }
  family_discount:
    type: percent_by_order
    rules:
      - { child_index: 2, percent: 10 }
      - { child_index: 3, percent: 15 }
    applies_to: ["standard", "flexible"]
    applies_to_status: unconfirmed          # C-4 — ждём письменного ответа владельца
    base_rule: enrollment_order
    base_rule_status: unconfirmed           # C-5
    max_children: 3
    label:
      ru: "Скидка для детей из одной семьи: 10% на второго, 15% на третьего."
      kk: "Бір отбасының балаларына жеңілдік: екіншісіне 10%, үшіншісіне 15%."

region:
  settlements: ["Карабалык", "Фёдоровка", "Сарыколь", "Аулиеколь", "Узынколь", "Житикара"]
  plans:
    standard:
      price: 10000
      recalculation: false
      label: { ru: "Стандартный", kk: "Стандартты" }
      note: { ru: "Без перерасчёта.", kk: "Қайта есептеусіз." }
    flexible: null        # G-10 — есть ли гибкий тариф в райцентрах, неизвестно
  single: null            # G-10
  family:
    price_per_child: 8000
    min_children: 2
    label:
      ru: "Семейный тариф: 8 000 ₸ за ребёнка, если занимаются двое и более детей из одной семьи."
      kk: "Отбасылық тариф: бір отбасынан екі және одан көп бала болса, әр балаға 8 000 ₸."

derived:
  enabled: true
  facts:
    - id: subscription_vs_single
      ru: "12 разовых тренировок — 38 400 ₸, абонемент — 25 000 ₸: экономия 13 400 ₸."
      kk: "12 бір реттік жаттығу — 38 400 ₸, абонемент — 25 000 ₸: 13 400 ₸ үнемдейсіз."
    - id: price_per_session
      ru: "По абонементу одно занятие выходит примерно 2 083 ₸ вместо 3 200 ₸ разово."
      kk: "Абонемент бойынша бір сабақ шамамен 2 083 ₸, бір реттік — 3 200 ₸."
    - id: flexible_break_even
      ru: "Гибкий тариф дороже на 5 000 ₸ — окупается, если ребёнок стабильно пропускает 2+ занятия в месяц."
      kk: "Икемді тариф 5 000 ₸ қымбат — бала айына 2+ сабақ жіберсе, тиімді болады."

payment_methods: []            # G-9: Kaspi / перевод / наличные / рассрочка — неизвестно
payment_details: null          # G-9: реквизиты
freeze_policy: { ru: null, kk: null }   # G-13
```

### 2.3 Алгоритм расчёта (зафиксирован здесь, реализуется в `tools/pricing.py`)

```
вход: scope, plan, children_count, single_sessions?

если scope == city:
    если plan == single: total = single.price * single_sessions; скидки НЕ применяются
    иначе:
        base = city.plans[plan].price
        для i в 1..children_count:
            pct = rules[child_index == i].percent  (для i=1 → 0)
            если i > family_discount.max_children → status = needs_operator, стоп
            price_i = round(base * (100 - pct) / 100)
        total = sum(price_i)
        если plan == flexible и applies_to_status == unconfirmed → caveat C-4
        если children_count >= 2 и base_rule_status == unconfirmed → caveat C-5 (тихий, во внутренний лог)

если scope == region:
    если plan in (flexible, single) и region.plans[plan] is null → status = no_data (G-10)
    если children_count == 1 → total = region.plans.standard.price
    если children_count >= region.family.min_children → total = family.price_per_child * children_count

дети на разных тарифах → status = needs_operator (правила нет)
scope неизвестен → НЕ считать; вернуть обе витрины и потребовать уточнения города/райцентра
```

**Почему это не может считать модель:** одно и то же слово «абонемент» стоит 25 000 ₸ или 10 000 ₸ в
зависимости от географии (разница в 2,5 раза), а механика семейной скидки в городе и в райцентрах разная
по природе (проценты vs фиксированная цена за ребёнка). Это конфликт C-2 из `CONTENT-AUDIT.md`.
Плюс требование ГК РК ст. 387: цена публичного договора одинакова для всех — скидка обязана быть
объективным правилом, а не результатом переговоров в чате.

---

## 3. `kb/faq.yaml`

Питает tool `get_kb_fact(topic, scope)`. В системный промпт рендерится **дайджест** (тема → одна строка
«о чём»), полные тексты отдаёт tool — так промпт остаётся компактным и стабильным для кэша.

### 3.1 Схема

| Поле | Тип | Описание |
|---|---|---|
| `entries[].id` | str | стабильный slug |
| `entries[].topic` | enum | `trial, docs, gear, safety, age_groups, payment, freeze, coaches, results, girls, adults, instagram, contacts, offer, sessions_count, group_size, competitions, summer` — **этот же enum идёт в схему tool** |
| `entries[].scope` | enum `city\|region\|any` | если ответ различается по географии |
| `entries[].question_variants.{ru,kk}` | list[str] | как спрашивают клиенты (для метрик и тестов, **не** для матчинга — матчит модель) |
| `entries[].answer.{ru,kk}` | str \| null | **готовый текст**, ≤ 600 знаков, 2–4 строки, без markdown и эмодзи |
| `entries[].source` | enum `owner_confirmed\|derived\|generic` | `owner_confirmed` — со слов владельца; `derived` — выведено из прайса/адресов; `generic` — общеотраслевое, без цифр и обещаний |
| `entries[].gap_ref` | str \| null | `G-4`, `G-6`, … если ответ отсутствует |
| `entries[].escalate_if_empty` | bool | при пустом `answer` сразу предлагать менеджера |
| `entries[].requires_tool` | str \| null | если ответ обязан сопровождаться вызовом другого tool (`calculate_price`, `get_gyms`) |
| `entries[].forbidden_claims` | list[str] | формулировки, которые нельзя произносить по этой теме |

### 3.2 Пример

```yaml
schema_version: 1
updated_at: "2026-08-09"
updated_by: "owner"

entries:
  - id: trial_free
    topic: trial
    scope: any
    question_variants:
      ru: ["пробное бесплатное?", "правда первое занятие бесплатно", "можно прийти посмотреть"]
      kk: ["сынақ сабақ тегін бе?", "бірінші сабақ тегін бе"]
    answer:
      ru: "Да, первое пробное занятие бесплатное и ни к чему не обязывает. Приходите, посмотрите зал и тренера, решение примете после."
      kk: "Иә, бірінші сынақ сабақ тегін және ешқандай міндеттеме жоқ. Келіп, залды және жаттықтырушыны көріңіз, шешімді кейін қабылдайсыз."
    source: owner_confirmed
    gap_ref: null
    escalate_if_empty: false
    requires_tool: null
    forbidden_claims: ["скидка только сегодня", "осталось 2 места"]

  - id: trial_conditions
    topic: trial
    scope: any
    question_variants:
      ru: ["сколько раз бесплатно", "что взять на первое занятие", "нужна ли запись заранее"]
      kk: ["неше рет тегін", "бірінші сабаққа не керек"]
    answer: { ru: null, kk: null }          # G-4 — критический пробел
    source: owner_confirmed
    gap_ref: "G-4"
    escalate_if_empty: true
    requires_tool: null
    forbidden_claims: []

  - id: safety_kids
    topic: safety
    scope: any
    question_variants:
      ru: ["не опасно ли", "по голове бить будут", "а если нос сломает"]
      kk: ["қауіпті емес пе", "басқа ұра ма"]
    answer:
      ru: "Это нормальный страх, его называет почти каждый родитель. В младших группах основа — общая физподготовка, координация, техника и работа на лапах; жёстких спаррингов нет. Контактная работа появляется позже, в полной защите и под контролем тренера."
      kk: "Бұл — қалыпты алаңдаушылық, оны кез келген ата-ана айтады. Кіші топтарда негізі — дене дайындығы, координация, техника және лапамен жұмыс; қатты спарринг жоқ. Контакт кейін, толық қорғаныс құралдарымен және жаттықтырушының бақылауымен енгізіледі."
    source: generic
    gap_ref: null
    escalate_if_empty: false
    requires_tool: null
    forbidden_claims:
      - "это абсолютно безопасно"
      - "травм не бывает"
      - "мы гарантируем безопасность"

  - id: age_min
    topic: age_groups
    scope: any
    question_variants:
      ru: ["со скольки лет", "сыну 5, не рано"]
      kk: ["неше жастан", "5 жаста, ерте емес пе"]
    answer:
      ru: "Принимаем детей школьного возраста, от 5 лет. Скажите возраст ребёнка — подскажу, что дальше."
      kk: "Мектеп жасындағы балаларды, 5 жастан бастап қабылдаймыз. Балаңыздың жасын айтыңыз — әрі қарай айтамын."
    source: owner_confirmed
    gap_ref: null
    escalate_if_empty: false

  - id: age_upper_and_girls
    topic: girls
    scope: any
    question_variants:
      ru: ["девочке можно", "а взрослым", "до скольки лет берёте"]
      kk: ["қызды қабылдайсыздар ма", "ересектерге бола ма"]
    answer: { ru: null, kk: null }          # G-7
    source: owner_confirmed
    gap_ref: "G-7"
    escalate_if_empty: true

  - id: docs_medical
    topic: docs
    scope: any
    answer: { ru: null, kk: null }          # G-6
    source: owner_confirmed
    gap_ref: "G-6"
    escalate_if_empty: true
    forbidden_claims: ["нужна справка 075", "справка не нужна"]   # номер формы бот НЕ называет

  - id: gear_first_lesson
    topic: gear
    scope: any
    answer: { ru: null, kk: null }          # G-5
    source: owner_confirmed
    gap_ref: "G-5"
    escalate_if_empty: true

  - id: payment_methods
    topic: payment
    scope: any
    answer: { ru: null, kk: null }          # G-9
    source: owner_confirmed
    gap_ref: "G-9"
    escalate_if_empty: true
    requires_tool: "calculate_price"

  - id: instagram_profile
    topic: instagram
    scope: any
    answer:
      ru: "Наш Instagram — @ainazarovtopteam, там тренировки, соревнования и жизнь залов."
      kk: "Біздің Instagram — @ainazarovtopteam, онда жаттығулар, жарыстар және залдардың өмірі."
    source: owner_confirmed
    gap_ref: null
    escalate_if_empty: false

  - id: coaches_info
    topic: coaches
    scope: any
    answer: { ru: null, kk: null }          # G-8
    source: owner_confirmed
    gap_ref: "G-8"
    escalate_if_empty: true
    forbidden_claims: ["тренер — мастер спорта", "чемпион Казахстана"]  # без подтверждения — нельзя
```

**Правило `forbidden_claims`:** список подмешивается в системный промпт как явный запрет **и** проверяется
пост-фильтром по нормализованному тексту ответа. Это защищает от двух конкретных рисков: обещание безопасности
(убивает доверие и юридически опасно) и приписывание тренерам несуществующих регалий.

---

## 4. `kb/media.yaml`

Реестр всего, что бот умеет «отправить по требованию». Питает tool `send_content` и генерирует enum
`artifact_id`.

### 4.1 Схема

| Поле | Тип | Описание |
|---|---|---|
| `artifacts[].id` | str | стабильный slug, попадает в enum схемы tool |
| `artifacts[].kind` | enum `text_card\|image\|document\|link\|location_text` | как доставляется |
| `artifacts[].enabled` | bool | `false` → артефакт не попадает в enum и не показывается модели |
| `artifacts[].scope` | enum `city\|region\|any` | |
| `artifacts[].gym_id` | str \| null | привязка к конкретному залу |
| `artifacts[].title.{ru,kk}` | str | как бот назовёт вложение в сопроводительном тексте |
| `artifacts[].when_to_send.{ru}` | str | 1 строка **для промпта**: когда этот артефакт уместен |
| `artifacts[].body.{ru,kk}` | str \| null | для `text_card` / `location_text` / `link` — готовый текст ≤ 900 знаков |
| `artifacts[].file.path` | str \| null | для `image`/`document`, относительно `MEDIA_DIR` |
| `artifacts[].file.mime` | str \| null | `image/jpeg`, `image/png`, `application/pdf` |
| `artifacts[].file.bytes` | int \| null | проверяется при загрузке |
| `artifacts[].file.sha256` | str \| null | проверяется при загрузке; расхождение → `enabled=false` + алерт |
| `artifacts[].channels.whatsapp` | enum `allow\|deny` | |
| `artifacts[].channels.instagram` | enum `allow\|deny` | Instagram Direct: только текст и jpg/png/bmp, ≤ 8 МБ |
| `artifacts[].max_send_per_dialog` | int | защита от спама вложениями |
| `artifacts[].gap_ref` | str \| null | почему выключен |
| `artifacts[].render_from` | str \| null | `gyms` / `pricing` — текст собирается кодом из другого файла, а не из `body` |

### 4.2 Пример

```yaml
schema_version: 1
updated_at: "2026-08-09"
updated_by: "dev"

artifacts:
  - id: price_card_city
    kind: text_card
    enabled: true
    scope: city
    gym_id: null
    title: { ru: "Цены, Костанай", kk: "Бағалар, Қостанай" }
    when_to_send: { ru: "когда просят прайс по городу целиком, а не расчёт под конкретную семью" }
    body:
      ru: |
        Костанай, абонемент — 12 занятий на месяц.
        — Стандартный: 25 000 ₸, без перерасчёта за пропуски
        — Гибкий: 30 000 ₸, с перерасчётом
        — Разовая тренировка: 3 200 ₸
        Второй ребёнок из семьи — 10%, третий — 15%.
      kk: |
        Қостанай, абонемент — айына 12 сабақ.
        — Стандартты: 25 000 ₸, жіберілген сабақтар қайта есептелмейді
        — Икемді: 30 000 ₸, қайта есептеумен
        — Бір реттік жаттығу: 3 200 ₸
        Отбасынан екінші бала — 10%, үшінші — 15%.
    file: { path: null, mime: null, bytes: null, sha256: null }
    channels: { whatsapp: allow, instagram: allow }
    max_send_per_dialog: 1
    gap_ref: null
    render_from: pricing

  - id: price_card_region
    kind: text_card
    enabled: true
    scope: region
    title: { ru: "Цены, райцентры", kk: "Бағалар, аудан орталықтары" }
    when_to_send: { ru: "когда клиент из района области спрашивает стоимость" }
    body:
      ru: |
        Райцентры области, абонемент:
        — Стандартный: 10 000 ₸, без перерасчёта
        — Семейный: 8 000 ₸ за ребёнка, если занимаются двое и более из одной семьи
      kk: |
        Облыс аудан орталықтары, абонемент:
        — Стандартты: 10 000 ₸, қайта есептеусіз
        — Отбасылық: бір отбасынан екі және одан көп бала болса, әр балаға 8 000 ₸
    channels: { whatsapp: allow, instagram: allow }
    max_send_per_dialog: 1
    render_from: pricing

  - id: price_photo_city
    kind: image
    enabled: true
    scope: city
    title: { ru: "Фото прайса", kk: "Прайс фотосы" }
    when_to_send: { ru: "когда просят «скиньте прайс картинкой» или «фото прайса»" }
    body: { ru: null, kk: null }
    file:
      path: "price_city.jpg"
      mime: "image/jpeg"
      bytes: 412000
      sha256: "<заполнить при добавлении файла>"
    channels: { whatsapp: allow, instagram: allow }   # jpg ≤ 8 МБ — проходит в Instagram
    max_send_per_dialog: 1

  - id: gyms_list_city
    kind: text_card
    enabled: true
    scope: city
    title: { ru: "Адреса залов в Костанае", kk: "Қостанайдағы залдардың мекенжайлары" }
    when_to_send: { ru: "когда просят список адресов или «где вы находитесь»" }
    body: { ru: null, kk: null }
    render_from: gyms          # собирается кодом из gyms.yaml, всегда актуален
    channels: { whatsapp: allow, instagram: allow }
    max_send_per_dialog: 2

  - id: gym_location_ksk_kairbekova_334
    kind: location_text
    enabled: true
    scope: city
    gym_id: ksk_kairbekova_334
    title: { ru: "Как найти зал на КСК", kk: "КСК-дағы залды қалай табуға болады" }
    when_to_send: { ru: "когда просят локацию/геометку конкретного зала" }
    body: { ru: null, kk: null }
    render_from: gyms
    channels: { whatsapp: allow, instagram: allow }
    max_send_per_dialog: 1
    gap_ref: "G-15"            # пока geo.map_url == null отправляется только адрес + ориентир

  - id: schedule_card
    kind: image
    enabled: false             # ВЫКЛЮЧЕН
    scope: any
    title: { ru: "Расписание", kk: "Кесте" }
    when_to_send: { ru: "нет данных" }
    body: { ru: null, kk: null }
    file: { path: null, mime: null, bytes: null, sha256: null }
    channels: { whatsapp: allow, instagram: allow }
    max_send_per_dialog: 1
    gap_ref: "G-1"

  - id: payment_details
    kind: text_card
    enabled: false             # ВЫКЛЮЧЕН
    scope: any
    title: { ru: "Как оплатить", kk: "Қалай төлеуге болады" }
    when_to_send: { ru: "нет данных" }
    body: { ru: null, kk: null }
    channels: { whatsapp: allow, instagram: allow }
    max_send_per_dialog: 1
    gap_ref: "G-9"

  - id: instagram_link
    kind: link
    enabled: true
    scope: any
    title: { ru: "Instagram школы", kk: "Мектептің Instagram парақшасы" }
    when_to_send: { ru: "когда просят соцсети, отзывы, «покажите тренировки»" }
    body:
      ru: "Наш Instagram: https://instagram.com/ainazarovtopteam — там тренировки, соревнования и жизнь залов."
      kk: "Біздің Instagram: https://instagram.com/ainazarovtopteam — онда жаттығулар, жарыстар және залдардың өмірі."
    channels: { whatsapp: allow, instagram: allow }
    max_send_per_dialog: 1

  - id: offer_and_policy
    kind: link
    enabled: false             # ВЫКЛЮЧЕН до публикации документов
    scope: any
    title: { ru: "Оферта и политика", kk: "Оферта және саясат" }
    when_to_send: { ru: "consent gate и по запросу «где почитать условия»" }
    body: { ru: null, kk: null }
    channels: { whatsapp: allow, instagram: allow }
    max_send_per_dialog: 3
    gap_ref: "G-14"
```

**Как модель выбирает артефакт.** В промпт попадает только компактный каталог `id — when_to_send.ru`
(без `body`), а enum `artifact_id` в схеме `send_content` строится из `enabled: true`. Содержимое собирается и
отправляется кодом. Модель не может ни выдумать артефакт, ни изменить его текст.

**Важное ограничение канала.** Instagram Direct принимает только текст и изображения (jpg/png/bmp); видео,
PDF и аудио туда не доставляются. Артефакты с `kind: document` для Instagram автоматически подменяются на
текст со ссылкой; правило зашито в `channels/outbound.py`, а не в промпт.

---

## 5. `kb/policies.yaml`

Организационно-правовой слой: реквизиты, согласие, документы, ретеншн, режим работы, правила эскалации и
follow-up. Правится владельцем совместно с юристом. Часть полей блокирует запуск (см. §9).

### 5.1 Схема

| Поле | Тип | Описание |
|---|---|---|
| `org.legal_form` | enum `ИП\|ТОО` \| null | форма собственности |
| `org.legal_name` | str \| null | наименование как в документах |
| `org.bin_iin` | str \| null | **БИН/ИИН — обязательный реквизит текста согласия** (ст. 8 п. 4 пп. 1 закона 94-V) |
| `org.brand` | str | `AINAZAROV TOP TEAM` |
| `org.city` | str | `Костанай` |
| `org.legal_address` | str \| null | |
| `org.contact_phone` / `org.contact_email` | str \| null | канал для отзыва согласия и обращений — пробел G-2 |
| `org.responsible_person` | str \| null | ответственный за обработку ПДн (обязателен только для ТОО) |
| `documents.consent.version` | str | напр. `consent-v1` |
| `documents.consent.text.{ru,kk}` | str \| null | полный текст короткого экрана согласия |
| `documents.consent.sha256` | str \| null | считается загрузчиком, пишется в `ConsentRecord` |
| `documents.consent.valid_months` | int | срок действия согласия (в тексте — «1 год с последнего обращения») |
| `documents.consent.scope` | list[str] | `collect, process, cross_border, third_party` |
| `documents.consent.third_parties` | list[str] | поимённо: `Google`, `Wazzup24`, `Meta (WhatsApp/Instagram)`, хостинг |
| `documents.policy_url` / `policy_version` | str \| null | политика обработки ПДн |
| `documents.offer_url` / `offer_version` | str \| null | публичная оферта |
| `documents.ai_terms_url` | str \| null | пользовательское соглашение системы ИИ (ст. 15 п. 2 пп. 5 Закона об ИИ) |
| `documents.marketing_optin.text.{ru,kk}` | str \| null | **отдельный** чекбокс: маркетинг — иная цель (ст. 14) |
| `ai_disclosure.{ru,kk}` | str | «диалог ведёт программа (ИИ)» — требование ст. 21 п. 1 Закона об ИИ |
| `audience.adults_only` | bool | `true` — бот для родителей/законных представителей 18+ |
| `audience.minor_detected_action` | enum `escalate\|stop` | что делать, если пишет ребёнок |
| `work_hours.{ru,kk}` | str \| null | часы работы администратора — пробел |
| `sla.reply_minutes` | int \| null | что бот обещает при эскалации |
| `escalation.triggers` | list[str] | машиночитаемые причины (совпадают с enum tool `escalate_to_manager`) |
| `escalation.manager_channel` | enum `telegram\|whatsapp\|email` | куда уходит карточка |
| `escalation.pause_minutes` | int | длительность паузы бота |
| `followup.policy` | list[obj] | `{event, delay_hours, only_work_hours, template_id, max_times}` |
| `followup.stop_words` | list[str] | после них follow-up выключается навсегда |
| `retention.lead_months` | int | несконвертированный лид |
| `retention.dialog_months` | int | переписка |
| `retention.consent_years` | int | аудит согласий (доказательство, ст. 25 п. 2 пп. 5) |
| `rights.export_days` | int | 3 рабочих дня |
| `rights.revoke_days` | int | 15 рабочих дней |
| `rights.modify_block_delete_days` | int | 1 рабочий день |
| `rights.objection_days` | int | 3 рабочих дня (ст. 19-1 п. 3) |
| `forbidden_behaviour` | list[str] | запреты для промпта: манипуляции, давление, искусственный дефицит, обращения к ребёнку «попроси родителей купить» |

### 5.2 Пример

```yaml
schema_version: 1
updated_at: "2026-08-09"
updated_by: "owner"

org:
  legal_form: null           # БЛОКЕР запуска
  legal_name: null           # БЛОКЕР
  bin_iin: null              # БЛОКЕР — без БИН/ИИН текст согласия неполон
  brand: "AINAZAROV TOP TEAM"
  city: "Костанай"
  legal_address: null
  contact_phone: null        # G-2
  contact_email: null
  responsible_person: null

documents:
  consent:
    version: "consent-v1"
    valid_months: 12
    scope: ["collect", "process", "cross_border", "third_party"]
    third_parties: ["Google (Gemini API)", "Wazzup24", "Meta (WhatsApp, Instagram)", "хостинг-провайдер в РК"]
    text:
      ru: null               # БЛОКЕР — заполняется после утверждения юристом, шаблон в research-kz-legal §3.3
      kk: null               # БЛОКЕР — шаблон в research-kz-legal §3.4, обязательна вычитка носителем
    sha256: null
  policy_url: null           # БЛОКЕР (G-14)
  policy_version: null
  offer_url: null            # G-14
  offer_version: null
  ai_terms_url: null         # G-14
  marketing_optin:
    text:
      ru: "Можно присылать вам новости школы и информацию об акциях? Ответьте «ДА» — это отдельное согласие, отказ ни на что не влияет."
      kk: "Мектеп жаңалықтары мен акциялар туралы хабарлама жіберуге бола ма? «ИӘ» деп жауап беріңіз — бұл бөлек келісім, бас тарту ештеңеге әсер етпейді."

ai_disclosure:
  ru: "Диалог ведёт программа (искусственный интеллект). Живого администратора можно позвать в любой момент — напишите «менеджер»."
  kk: "Диалогты бағдарлама (жасанды интеллект) жүргізеді. Тірі әкімшіні кез келген уақытта шақыруға болады — «менеджер» деп жазыңыз."

audience:
  adults_only: true
  minor_detected_action: escalate

work_hours: { ru: null, kk: null }     # пробел
sla:
  reply_minutes: null                  # пробел

escalation:
  triggers: ["user_request", "no_data", "complaint", "medical", "price_off_list",
             "installments", "age_out_of_range", "foreign_language", "repeated_miss"]
  manager_channel: telegram
  pause_minutes: 60

followup:
  policy:
    - { event: "answered_no_reply", delay_hours: 2,  only_work_hours: true,  template_id: "fu_soft",     max_times: 1 }
    - { event: "will_think",        delay_hours: 48, only_work_hours: true,  template_id: "fu_value",    max_times: 1 }
    - { event: "slot_unconfirmed",  delay_hours: 3,  only_work_hours: true,  template_id: "fu_remind",   max_times: 1 }
    - { event: "trial_booked",      delay_hours: -20, only_work_hours: false, template_id: "fu_before",  max_times: 2 }
    - { event: "no_show",           delay_hours: 24, only_work_hours: true,  template_id: "fu_reschedule", max_times: 1 }
  stop_words: ["не пишите", "не звоните", "отстаньте", "спасибо, не надо",
               "уже записались", "жазбаңыз", "керек емес", "стоп", "стоп рассылка"]

retention:
  lead_months: 12
  dialog_months: 12
  consent_years: 3

rights:
  export_days: 3
  revoke_days: 15
  modify_block_delete_days: 1
  objection_days: 3

forbidden_behaviour:
  - "давление и искусственная срочность («последнее место», «только сегодня»)"
  - "манипуляции на возрасте и уязвимости ребёнка"
  - "обращение к ребёнку с призывом уговорить родителей купить"
  - "обещание спортивных результатов и побед"
  - "медицинские советы, оценка веса и телосложения ребёнка"
  - "утверждение, что бокс безопасен на 100%"
  - "индивидуальный торг по цене вне публичных условий"
```

Смысл `forbidden_behaviour` не косметический: манипулятивные техники и эксплуатация уязвимости по возрасту
прямо запрещены Законом РК «Об искусственном интеллекте» (ст. 17 п. 3), а призыв к несовершеннолетнему
уговорить родителей — Законом «О рекламе» (ст. 15). Список рендерится в системный промпт дословно.

---

## 6. `kb/i18n.yaml`

Все строки, которые бот произносит **без участия модели**: consent gate, заглушки при отсутствии данных,
эскалация, ошибки, follow-up, шаблоны лид-карточек. Это гарантирует, что критичные формулировки
(юридические и «уточню у администратора») стабильны от диалога к диалогу.

### 6.1 Схема

Плоский словарь `key → {ru, kk}`, ключи в `dot.notation`, плейсхолдеры в `{фигурных_скобках}` подставляются кодом.
Валидатор проверяет: наличие обеих локалей, отсутствие пустых строк, совпадение множества плейсхолдеров в `ru`
и `kk` (иначе на казахском потеряется подстановка), длину ≤ 900 знаков.

### 6.2 Пример

```yaml
schema_version: 1
updated_at: "2026-08-09"
updated_by: "dev"

strings:
  consent.gate:
    ru: "{ai_disclosure}\n\nБот предназначен для родителей и законных представителей (18+).\nЧтобы продолжить, нужно ваше согласие на сбор и обработку персональных данных: {consent_summary}\nПолный текст и политика: {policy_url}\nСогласны? Ответьте «СОГЛАСЕН»."
    kk: "{ai_disclosure}\n\nБот ата-аналарға және заңды өкілдерге арналған (18+).\nЖалғастыру үшін дербес деректерді жинауға және өңдеуге келісіміңіз қажет: {consent_summary}\nТолық мәтін және саясат: {policy_url}\nКелісесіз бе? «КЕЛІСЕМІН» деп жауап беріңіз."
  consent.accepted:
    ru: "Спасибо. Чем могу помочь?"
    kk: "Рахмет. Немен көмектесе аламын?"
  consent.declined:
    ru: "Понимаю. Без согласия я не могу собирать данные, но общие вопросы задать можно — отвечу без записи."
    kk: "Түсінемін. Келісімсіз деректерді жинай алмаймын, бірақ жалпы сұрақтар қоюға болады — жазбай жауап беремін."

  gap.schedule:
    ru: "Точное расписание по этому залу подскажет администратор — угадывать не буду. Передать ему ваш вопрос?"
    kk: "Бұл залдың нақты кестесін әкімші айтады — мен болжамаймын. Сұрағыңызды оған жеткізейін бе?"
  gap.contacts:
    ru: "Номер администратора я пока не могу дать. Напишите здесь — передам ваш вопрос, он ответит в этой же переписке."
    kk: "Әкімшінің нөмірін әзірге бере алмаймын. Осында жазыңыз — сұрағыңызды жеткіземін, ол осы хат алмасуда жауап береді."
  gap.trial_conditions:
    ru: "Условия бесплатного пробного уточнит администратор — не хочу сказать неточность. Записать ребёнка, чтобы он связался и всё подтвердил?"
    kk: "Тегін сынақ сабақтың шарттарын әкімші нақтылайды — қате айтқым келмейді. Балаңызды жазып қояйын ба, ол хабарласып, бәрін растайды?"
  gap.docs:
    ru: "Про справку скажет администратор — конкретную форму называть не буду, чтобы вы не съездили зря."
    kk: "Анықтама туралы әкімші айтады — бекер бармауыңыз үшін нақты форманы атамаймын."
  gap.gear:
    ru: "Что именно взять на тренировку, уточнит администратор. Обычно этого спрашивают вместе с расписанием — передам оба вопроса."
    kk: "Жаттығуға нақты не алу керектігін әкімші нақтылайды. Әдетте мұны кестемен бірге сұрайды — екі сұрақты да жеткіземін."
  gap.payment:
    ru: "Способы оплаты подтвердит администратор. Стоимость я назвать могу — сказать точную сумму под ваш случай?"
    kk: "Төлем тәсілдерін әкімші растайды. Құнын айта аламын — жағдайыңызға нақты соманы есептеп берейін бе?"
  gap.coaches:
    ru: "Про тренера конкретного зала расскажет администратор. Могу передать ваш вопрос ему."
    kk: "Нақты залдың жаттықтырушысы туралы әкімші айтады. Сұрағыңызды оған жеткізе аламын."
  gap.region_address:
    ru: "Секция в этом райцентре есть, точный адрес подскажет администратор — передам ваш вопрос."
    kk: "Бұл аудан орталығында секция бар, нақты мекенжайды әкімші айтады — сұрағыңызды жеткіземін."
  gap.kzhbi:
    ru: "По КЖБИ я уточню у администратора и не буду гадать. Скажите, где вам удобнее — подберу ближайший из тех, что точно работают."
    kk: "КЖБИ бойынша әкімшіден нақтылаймын, болжамаймын. Сізге қай жер ыңғайлы екенін айтыңыз — нақты жұмыс істейтіндерінен жақынын таңдап беремін."

  escalation.handoff:
    ru: "Здесь лучше ответит администратор, чтобы не сказать вам неточность. Передаю ваш вопрос — он напишет сюда же."
    kk: "Мұнда әкімші жақсы жауап береді, қате айтпау үшін. Сұрағыңызды жеткіздім — ол осында жазады."
  escalation.child_writing:
    ru: "Классно, что хочешь заниматься. Для записи нужно согласие родителей — покажи это сообщение маме или папе, пусть напишут сюда."
    kk: "Айналысқың келгені жақсы. Жазылу үшін ата-ананың келісімі қажет — осы хабарламаны анаңа немесе әкеңе көрсет, олар осында жазсын."

  error.generic:
    ru: "Секунду, у меня сбой на стороне сервиса. Передаю администратору, он ответит здесь же."
    kk: "Бір сәт, сервис жағында ақау. Әкімшіге жеткіздім, ол осында жауап береді."
  error.voice_message:
    ru: "Голосовое пока не слушаю. Напишите, пожалуйста, текстом — отвечу сразу."
    kk: "Дауыстық хабарламаны әзірге тыңдамаймын. Мәтінмен жазыңызшы — бірден жауап беремін."
  error.too_many_messages:
    ru: "Вижу несколько сообщений подряд — отвечу на всё по порядку."
    kk: "Қатарынан бірнеше хабарлама көрдім — бәріне ретімен жауап беремін."

  bridge.kk_offer:
    ru: "Қазақша жазсаңыз, қазақша жауап беремін."
    kk: "Қазақша жазсаңыз, қазақша жауап беремін."

  followup.fu_soft:
    ru: "Добрый день. Остались вопросы по секции? Если удобнее — могу сразу записать на бесплатное пробное."
    kk: "Қайырлы күн. Секция бойынша сұрақтар қалды ма? Қаласаңыз, тегін сынақ сабаққа бірден жазып қоямын."
  followup.fu_value:
    ru: "Добрый день. Если ещё актуально — запишу на бесплатное пробное, время подтвердит администратор. Если нет, больше не пишу."
    kk: "Қайырлы күн. Әлі өзекті болса — тегін сынақ сабаққа жазамын, уақытын әкімші растайды. Жоқ болса, енді жазбаймын."

  lead_card.trial_booked:
    ru: "НОВАЯ ЗАПИСЬ НА ПРОБНОЕ\n\nРебёнок: {child}\nРодитель: {parent}\nТелефон: {phone}\nЯзык: {lang}\nЗал: {gym}\nКогда: {when}\nМотив: {motivation}\nТревога: {objection}\nКанал: {channel}, диалог от {dt}"
    kk: "НОВАЯ ЗАПИСЬ НА ПРОБНОЕ\n\nРебёнок: {child}\nРодитель: {parent}\nТелефон: {phone}\nЯзык: {lang}\nЗал: {gym}\nКогда: {when}\nМотив: {motivation}\nТревога: {objection}\nКанал: {channel}, диалог от {dt}"
  lead_card.escalation:
    ru: "НУЖЕН ЖИВОЙ ОТВЕТ\n\nТелефон: {phone}\nВопрос: {question}\nЯзык: {lang}\nПричина: {reason}\nКанал: {channel}, {dt}"
    kk: "НУЖЕН ЖИВОЙ ОТВЕТ\n\nТелефон: {phone}\nВопрос: {question}\nЯзык: {lang}\nПричина: {reason}\nКанал: {channel}, {dt}"
```

Карточки для администратора намеренно одинаковы в обеих локалях: их читает сотрудник школы, а поле
`Язык: {lang}` сообщает, на каком языке звонить родителю.

---

## 7. `kb/lexicon.yaml` *(вспомогательный файл)*

Не входит в шесть обязательных, но необходим: препроцессинг сленга и опечаток выполняется **до** обращения к
модели (§5.3 `research-funnel.md`) и питает `find_gym_by_district`.

```yaml
schema_version: 1
updated_at: "2026-08-09"

intents:
  price:    ["абик", "абонимент", "абонимнт", "абанимент", "абон", "скок", "скока", "скоко",
             "сколко", "почем", "по чем", "ценник", "прайс", "стоимость", "баға", "бага",
             "канша", "қанша", "канша турады", "narx"]
  signup:   ["запишите", "запишите ребенка", "хочу записать", "записаца", "запись",
             "жазыныз", "жазыңызшы", "жазайык", "жазылу", "zapis"]
  schedule: ["распис", "расписание", "график", "когда занятия", "во сколько", "в скока",
             "кесте", "кестени жиберыныз", "уакыты", "уақыты", "raspisanie"]
  location: ["адрес", "адрес киньте", "где вы", "где находитесь", "гдэ", "локация",
             "геолокацию скиньте", "мекенжай", "кай жерде", "қай жерде", "adres"]
  manager:  ["менеджер", "оператор", "живой", "человек", "администратор", "әкімші"]
  erase:    ["удалить", "удалите мои данные", "жою", "деректерімді жойыңыз"]
  stop:     ["стоп", "не пишите", "отстаньте", "керек емес", "жазбаңыз"]

language_markers:
  kk_graphemes: ["ә", "ғ", "қ", "ң", "ө", "ұ", "ү", "һ", "і"]
  kk_words: ["қанша", "баға", "сабақ", "бала", "жазыңыз", "кесте", "мекенжай", "жас", "тегін", "керек"]
  kk_translit: ["salemetsiz", "kalai", "balam", "jaste", "kansha", "kalay", "rahmet"]
  ru_translit: ["skolko", "stoit", "adres", "raspisanie", "zapishite"]

age_patterns:
  - "(\\d{1,2})\\s*(лет|года|год|л\\.?|жаста|жас)"
  - "(сыну|дочке|ұлым|қызым)\\s*(\\d{1,2})"
  - "(\\d{4})\\s*(года|г\\.?р\\.?|жылы)"     # год рождения → возраст от текущей даты

gender_markers:
  m: ["сын", "сыну", "мальчик", "пацан", "ұлым", "бала (ер)"]
  f: ["дочь", "дочка", "дочке", "девочка", "қызым"]

districts_extra:                 # разговорные названия, не привязанные к конкретному залу
  - "наримановка"
  - "юбилейный"
  - "борки"
  - "хбк"
  - "васильевка"
  - "дружба"
```

`districts_extra` — районы, которые клиенты называют, но зала там нет. Их наличие в файле позволяет боту
распознать район и **честно** сказать «ближайший к вам — такой-то», а не переспрашивать по кругу.

---

## 8. Как KB попадает в промпт и в детерминированные tools

### 8.1 Два потребителя одной базы

```
kb/*.yaml
   │
   ├─► app/kb/loader.py ──► KBSnapshot (иммутабельный объект в памяти, kb_hash)
   │                          │
   │                          ├─► app/kb/render.py ──► текст system_instruction (стабильный префикс)
   │                          │
   │                          ├─► app/llm/tools_schema.py ──► enum'ы: gym_id, artifact_id, topic
   │                          │
   │                          └─► app/tools/*.py ──► ФАКТЫ для расчёта и поиска (полные данные)
```

Промпт и tools питаются **из одного снимка**, поэтому рассинхрон невозможен по построению: модель не может
знать о зале, которого нет в enum, и не может получить цену, отличную от той, что посчитал `pricing.py`.

### 8.2 Что именно рендерится в системный промпт

Порядок фиксирован, сериализация детерминирована (сортировка ключей, `ensure_ascii=False`, единый перевод строк) —
префикс обязан быть **байт-в-байт одинаковым** между запросами, иначе не срабатывает implicit-кэш Gemini.

| Блок | Источник | Объём | Зачем |
|---|---|---|---|
| 1. Роль, тон, правила диалога (A1Q1, длины, запреты) | статический текст + `policies.forbidden_behaviour` | ~700 токенов | поведение |
| 2. Реестр залов, компактно: `id — район — ориентир — scope` | `gyms.yaml` (только `active+open`) | ~500 | чтобы модель понимала географию и правильно звала tools |
| 3. Витрина цен, без расчётов | `pricing.yaml` (только числа-ориентиры) | ~150 | понимать, о чём речь |
| 4. Дайджест FAQ: `topic — о чём` | `faq.yaml` | ~300 | маршрутизация в `get_kb_fact` |
| 5. Каталог артефактов: `id — when_to_send` | `media.yaml` (`enabled: true`) | ~200 | выбор в `send_content` |
| 6. **Манифест пробелов**: чего в базе нет + дословная фраза отказа | `gaps.py` + `i18n.yaml` | ~350 | анти-галлюцинация |
| 7. Правила эскалации и раскрытие ИИ | `policies.yaml` | ~200 | закон + воронка |
| 8. 2–3 эталонных диалога (few-shot) | статический текст (§3.6 `research-funnel.md`) | ~600 | длина и стиль |

Итого ≈ **8 000 токенов** — это и есть цифра, на которой построена оценка стоимости в `ARCHITECTURE.md` §13.

**Чего в промпте нет намеренно:** полных текстов FAQ и артефактов (их отдают tools), расписания (его нет),
телефонов и реквизитов (их нет), `internal_note` (заметки владельца), `lexicon.yaml` (используется кодом до модели).

### 8.3 Динамика — только в конец

Всё изменяемое (язык диалога, текущая дата, имя ребёнка, выбранный зал, статус лида, признак
`injection_suspected`) добавляется **последним** элементом `contents` как служебная заметка, а не в
`system_instruction`. Любая динамика в начале промпта убивает попадание в кэш.

### 8.4 Перезагрузка

`POST /admin/kb/reload` → чтение файлов → Pydantic-валидация → проверка файлов из `media.yaml` (существование,
размер, sha256) → сборка нового `KBSnapshot` и нового `kb_hash` → **атомарный swap** ссылки.
Диалоги, начатые на прежней версии, продолжаются на новой (история не переписывается), в лог пишется
`kb_switch old→new`. Ошибка валидации → старый снимок остаётся, HTTP 422 с перечнем полей.
Смена `kb_hash` инвалидирует implicit-кэш Gemini — это нормально при редких правках; массовое редактирование
KB в часы пик не рекомендуется.

---

## 9. Поля-заглушки и поведение при пустом поле

### 9.1 Соглашение о заглушках

| Что записано | Что это значит | Как ведёт себя бот |
|---|---|---|
| `null` (скаляр) | данных нет | tool → `status: no_data` + `say_if_no_data`; бот произносит фразу из `i18n.gap.*` и предлагает эскалацию |
| `[]` (список) | данных нет | то же |
| `""` (пустая строка) | **ошибка** | KB не проходит валидацию, версия не применяется |
| `enabled: false` | артефакт временно отключён | не попадает в enum, модель о нём не знает |
| `status: unresolved` | конфликт данных не решён | tool → `needs_operator`; бот не утверждает ни «да», ни «нет» |
| `*_status: unconfirmed` | правило есть, но не подтверждено владельцем | tool считает, но добавляет обязательную оговорку в ответ |

### 9.2 Полный реестр заглушек на 2026-08-09

| Ключ пробела | Поле в KB | Вопрос клиента, остающийся без ответа | Фраза бота |
|---|---|---|---|
| **G-1** | `gyms[].schedule: []` | «Во сколько тренировки?», «есть утренняя группа?» | `i18n.gap.schedule` |
| **G-2** | `gyms[].phone: null`, `policies.org.contact_phone: null` | «Как с вами связаться?», «дайте номер» | `i18n.gap.contacts` |
| **G-3** | `gyms[].address: null` у 6 райцентров | «Где именно в Житикаре?» | `i18n.gap.region_address` |
| **G-4** | `faq: trial_conditions.answer: null` | «Сколько раз бесплатно?», «нужна ли запись заранее?» | `i18n.gap.trial_conditions` |
| **G-5** | `faq: gear_first_lesson.answer: null` | «Что взять на первое занятие?», «сколько стоит экипировка?» | `i18n.gap.gear` |
| **G-6** | `faq: docs_medical.answer: null` | «Нужна справка? Какая?» | `i18n.gap.docs` |
| **G-7** | `faq: age_upper_and_girls.answer: null` | «Дочке 9, возьмёте?», «а мне 30?» | `get_kb_fact` → эскалация |
| **G-8** | `gyms[].coaches: []`, `faq: coaches_info` | «Кто тренер? Какой опыт?» | `i18n.gap.coaches` |
| **G-9** | `pricing.payment_methods: []`, `media: payment_details.enabled=false` | «Каспи принимаете?», «как оплатить?» | `i18n.gap.payment` |
| **G-10** | `pricing.region.plans.flexible: null`, `region.single: null` | «А разово в Фёдоровке можно?» | `calculate_price` → `no_data` |
| **G-13** | `pricing.freeze_policy: null` | «Ребёнок заболел — что с абонементом?» | `get_kb_fact` → эскалация |
| **G-14** | `policies.documents.policy_url/offer_url: null` | ссылка в consent gate | **блокирует запуск** |
| **G-15** | `gyms[].geo.lat/lon/map_url: null` | «Скиньте локацию» | адрес + ориентир текстом |
| **C-3** | `gyms: unresolved_kzhbi` | «У вас есть зал на КЖБИ?» | `i18n.gap.kzhbi` |
| **C-4** | `family_discount.applies_to_status: unconfirmed` | «Скидка на гибкий тариф действует?» | расчёт + обязательная оговорка |
| **C-5** | `family_discount.base_rule_status: unconfirmed` | «От какого тарифа считается скидка?» | `needs_operator` при разных тарифах |

**Блокеры запуска** (без них бот не поднимается): `policies.org.bin_iin`, `policies.org.legal_name`,
`policies.documents.consent.text.{ru,kk}`, `policies.documents.policy_url`. Причина — сбор ПДн без
надлежащего согласия незаконен (ст. 7 п. 1 и ст. 8 закона 94-V), а текст согласия обязан содержать
БИН/ИИН оператора (ст. 8 п. 4 пп. 1). Валидатор `loader.py` проверяет их отдельным правилом `startup_blockers`.

---

## 10. Чек-лист владельцу школы: что дозаполнить

Порядок = (частота вопроса) × (потеря от отсутствия ответа). Пока поле пусто, бот работает корректно, но
теряет темп и эскалирует.

### Уровень 0 — без этого бот не запускается (юридические блокеры)

| # | Что дать | Куда ляжет | Что не работает без этого |
|---|---|---|---|
| 0.1 | Форма (ИП/ТОО), полное наименование, **БИН/ИИН**, юридический адрес | `policies.org.*` | текст согласия неполон → сбор ПДн незаконен, бот не стартует |
| 0.2 | Утверждённые юристом тексты: согласие (RU+KK), политика обработки ПДн, публичная оферта, пользовательское соглашение ИИ + публичные ссылки | `policies.documents.*` | consent gate не собрать; штраф по ст. 79 КоАП — до 300 МРП за несоблюдение мер защиты |
| 0.3 | Контакт для отзыва согласия и обращений (телефон и/или e-mail) | `policies.org.contact_*` | нельзя реализовать права субъекта (ст. 24) |

### Уровень 1 — критично для конверсии

| # | Что дать | Куда ляжет | Какой вопрос останется без ответа |
|---|---|---|---|
| 1.1 | **Расписание по всем 12 точкам**: возрастная группа × дни × время, отдельно под 1-ю и 2-ю смену | `gyms[].schedule` (G-1) | «Во сколько тренировки?», «есть группа для 6-летнего утром?» — самый частый вопрос, хайлайт «ГРАФИК» это доказывает |
| 1.2 | **Условия бесплатного пробного**: сколько раз, нужна ли предварительная запись, нужна ли справка на пробное | `faq: trial_conditions` (G-4) | «Пробное правда бесплатное? Что нужно?» — это точка конверсии, отказ здесь дороже всего |
| 1.3 | **Телефон/WhatsApp администратора** — общий и/или по залам | `gyms[].phone`, `policies.org.contact_phone` (G-2) | «Как с вами связаться?» — бот не может отдать номер |
| 1.4 | **Что взять на первое занятие**, нужно ли сразу покупать экипировку и сколько она стоит | `faq: gear_first_lesson` (G-5) | «Сколько ещё придётся потратить?» — скрытая стоимость, типовое возражение |
| 1.5 | **Способы оплаты** (Kaspi QR / перевод / наличные / рассрочка) и реквизиты | `pricing.payment_methods`, `media: payment_details` (G-9) | «Как оплатить?» — блокирует закрытие сделки в переписке |

### Уровень 2 — письменные решения по конфликтам

| # | Вопрос владельцу | Куда ляжет | Пока нет ответа |
|---|---|---|---|
| 2.1 | **КЖБИ**: это зал на Воинов-интернационалистов 8Б, закрытая точка или седьмой зал, не попавший в бриф? | `gyms: unresolved_kzhbi` (C-3) | бот не подтверждает и не отрицает наличие зала, эскалирует |
| 2.2 | **Семейная скидка 10/15 % распространяется на тариф ГИБКИЙ (30 000 ₸) или только на СТАНДАРТНЫЙ?** | `family_discount.applies_to_status` (C-4) | бот считает по обоим тарифам, но обязан добавлять оговорку «уточнит администратор» |
| 2.3 | **От чего считается «второй ребёнок»** — от порядка зачисления или от более дешёвого абонемента, если дети на разных тарифах? | `family_discount.base_rule_status` (C-5) | при разных тарифах бот отказывается считать и передаёт менеджеру |
| 2.4 | Есть ли в райцентрах гибкий тариф и разовые тренировки? | `pricing.region.plans` (G-10) | «А разово в Фёдоровке можно?» — `no_data` |
| 2.5 | Верхняя возрастная граница; есть ли группы для девочек; есть ли взрослые группы | `faq: age_upper_and_girls` (G-7) | «Дочке 9, возьмёте?», «а мне 30?» — эскалация |

### Уровень 3 — качество и доверие

| # | Что дать | Куда ляжет | Какой вопрос |
|---|---|---|---|
| 3.1 | Адреса секций внутри 6 райцентров | `gyms[].address` (G-3) | «Где именно в Житикаре?» |
| 3.2 | Имена тренеров по залам, регалии, стаж, на каком языке говорят | `gyms[].coaches` (G-8) | «Кто тренер?» — без этого слоган «сделаем чемпиона» ничем не подкреплён |
| 3.3 | Нужна ли медсправка, какая именно, в какой срок | `faq: docs_medical` (G-6) | «Нужна справка 075?» — номер формы бот не назовёт без подтверждения |
| 3.4 | Политика заморозки абонемента (болезнь, отпуск) | `pricing.freeze_policy` (G-13) | «Ребёнок заболел на две недели — что с абонементом?» |
| 3.5 | Геокоординаты или ссылки на карту для 6 городских залов | `gyms[].geo` (G-15) | «Скиньте локацию» — сейчас только текстовый адрес |
| 3.6 | Часы работы администратора и SLA ответа | `policies.work_hours`, `policies.sla` | «Когда мне ответят?» — бот обещает срок только если он задан |
| 3.7 | Размер группы, длительность тренировки, сколько раз в неделю по абонементу | `faq: group_size`, `sessions_count` | «Сколько детей в группе?», «сколько длится?» |
| 3.8 | Достижения учеников (турниры, медали, разряды) — 5–10 проверяемых фактов | `faq: results` (G-12) | «А результаты есть?» |
| 3.9 | Идёт ли набор сейчас, есть ли места | `faq: sessions_count` / отдельная тема (G-11) | «Сейчас набор идёт?» |
| 3.10 | Фото прайса в jpg ≤ 8 МБ + расписание картинкой | `media: price_photo_city`, `schedule_card` | «Скиньте прайс/расписание картинкой» |

### Как сдавать данные

Владелец правит YAML напрямую (или присылает таблицу, администратор переносит) → `POST /admin/kb/reload`.
**Правки кода и системного промпта не требуются ни в одном пункте этого чек-листа.** Единственное исключение —
появление принципиально новой сущности (например, отдельных цен для взрослых групп): тогда добавляется поле
в схему `pricing.yaml` и ветка в `tools/pricing.py`.

Приоритет закрытия измеряется метрикой `kb_gap_hits{topic}` — топ-10 отказов «уточню у администратора»
за неделю и есть фактический список того, что дозаполнять следующим.

