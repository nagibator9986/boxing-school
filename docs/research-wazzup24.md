# Wazzup24 HTTP API v3 — исчерпывающий разбор для двусторонней интеграции

**Проект:** AI-консультант «AINAZAROV TOP TEAM» (Костанай, KZ), Python 3.11 / FastAPI, Gemini.
**Каналы:** WhatsApp + Instagram Direct через агрегатор Wazzup24.
**Дата сбора:** 2026-08-09.

> **Про источники.** Домен `wazzup24.com` отдаёт `403 Forbidden` на автоматические запросы (проверено:
> `https://wazzup24.com/help/api-ru/` → 403, `https://wazzup24.com/help/api-en/webhooks/` → 403).
> Рабочее зеркало с идентичным содержимым — `wazzup24.ru`, а также казахстанское зеркало `wazzup-24.kz`
> (проверено: `https://wazzup-24.kz/help/api-ru/otpravka-soobshhenij/` → 200).
> Ниже все ссылки даны на `wazzup24.ru`; те же статьи доступны по путям `wazzup24.com/help/api-ru/...`
> и `wazzup-24.kz/help/api-ru/...` при открытии в браузере.

**Основные источники:**

| # | URL | Что оттуда взято |
|---|-----|------------------|
| S1 | https://wazzup24.ru/help/api-ru/ | оглавление раздела «Пользовательское API» |
| S2 | https://wazzup24.ru/help/api-ru/avtorizaciya/ | ключ API, заголовок |
| S3 | https://wazzup24.ru/help/api-ru/sposoby-podkljucheniya/ | 3 способа подключения, Sidecar-ключ |
| S4 | https://wazzup24.ru/help/api-ru/otpravka-soobshhenij/ | POST /v3/message, поля, лимиты, коды ошибок |
| S5 | https://wazzup24.ru/help/api-ru/webhooks-2/ | регистрация вебхуков, payload, таймаут |
| S6 | https://wazzup24.ru/help/api-ru/rabota-s-kanalami/ | GET /v3/channels, transport, state |
| S7 | https://wazzup24.ru/help/api-ru/obshhie-oshibki/ | rate limit, 401/403/429/500 |
| S8 | https://wazzup24.ru/help/api-ru/schetchik-neotvechennyh/ | счётчик неотвеченных (WS + REST) |
| S9 | https://wazzup24.ru/help/api-ru/sushhnosti-api-i-terminologiya/ | каналы, контакты, сделки, роли |
| S10 | https://wazzup24.ru/help/how-to-use/kommentarii-v-instagram/ | Instagram: 7-дневное окно, нельзя писать первым |
| S11 | https://wazzup24.ru/help/how-to-use/waba-limits/ | WABA: 24-часовое окно обслуживания, тиры |
| S12 | https://wazzup24.ru/help/how-to-use/requirements-for-attachments/ | типы и размеры вложений |
| S13 | https://wazzup24.ru/help/how-to-use/unanswered-counter/ | неотвеченные в UI, «Отвечать не нужно» |
| S14 | https://wazzup24.ru/help/api/webhooks/ | API техпартнёров (tech.wazzup24.com/v2) — рекомендации по ретраям |
| S15 | https://github.com/AlexR-eng/wazzup_bot (файл `main.py`) | перекрёстная проверка реального формата на живом боте |

---

## 1. Базовый URL и аутентификация

**Базовый URL:** `https://api.wazzup24.com/v3` — источник S4, S5, S6
(в документации все роуты пишутся полностью: `POST https://api.wazzup24.com/v3/message`,
`PATCH https://api.wazzup24.com/v3/webhooks`, `GET https://api.wazzup24.com/v3/channels`).

Отдельные хосты вне `api.wazzup24.com`:

| Хост | Назначение | Источник |
|------|-----------|----------|
| `https://api.wazzup24.com/v3` | основной REST API | S4, S5, S6 |
| `https://integrations.wazzup24.com/counters/ws_host/api_v3/:apiKey` | получить хост WS для счётчика неотвеченных | S8 |
| `ws-counters2.wazzup24.com` (пример) | socket.io для счётчика | S8 |
| `https://tech.wazzup24.com/v2` | **отдельный** API для техпартнёров, не для нас | S14 |

**Аутентификация — Bearer-токен в заголовке `Authorization`.** Дословно (S2):

> «Чтобы применить ключ, указывайте его как значение заголовка следующем виде:
> `Authorization: Bearer 33a817cbc1504bd5885574d8f0290cd3`»

Формат ключа: hex-строка 32 символа (по примерам в доках: `33a817cbc1504bd5885574d8f0290cd3` S2,
`c8cf90444023482f909520d454368d27` S6, `w11cf3444405648267f900520d454368d27` S5 — последний 35 символов,
т.е. **строгий формат ключа документацией не зафиксирован**, воспринимайте как непрозрачную строку).

**Где взять ключ в ЛК** (дословно S2 / S3):
1. Если ещё не подключён канал — добавьте его в разделе «Каналы».
2. Перейдите в раздел «Интеграция с CRM».
3. Выберите **API → Подключить**.
4. Скопируйте ключ API. После подключения интеграции ключ можно найти там же:
   «Интеграция с CRM» → вкладка **«Дополнительно»**.

**Три способа подключения** (S3):
1. **Ключ API** — «используют, чтобы отладить интеграцию перед публикацией в маркетплейсе интеграций
   или для непубличных интеграций». ← **это наш случай.**
2. **WAuth** — «вдохновлена OAuth2.0, но формально ей не является», обязательна для техпартнёров в маркетплейсе.
3. **Sidecar API-ключ** — генерируется при создании интеграции с amoCRM/Битрикс24; работает **только** в роутах:
   `GET /channels`, `POST /sendMessage`, `GET /webhooks`, `PATCH /webhooks`, `GET /v3/templates/whatsapp`.
   При Sidecar **не работает параметр `crmUserId`**.

> ✅ **Расхождение `/sendMessage` vs `/v3/message` — разрешено.** S3 называет роут отправки
> `POST /sendMessage`, S4 — `POST https://api.wazzup24.com/v3/message`. Ответ содержится в самой разметке
> S3: буллет «`POST /sendMessage` — отправка сообщения» является **гиперссылкой** на
> `href="/help/api/otpravka-soobshhenij/"`, то есть на ту же самую статью про `POST /v3/message`
> (аналогично «`GET /channels`» ссылается на `/help/api-ru/rabota-s-kanalami/`).
> Проверено в HTML-исходнике страницы S3.
> **Вывод:** `/sendMessage` — устаревшая подпись **того же самого роута** в перечне Sidecar-разрешений,
> а не отдельный endpoint. Отдельного URL `/v3/sendMessage` в документации нет — пробовать его не нужно.
> Использовать `POST https://api.wazzup24.com/v3/message`.

---

## 2. Отправка сообщения: `POST /v3/message`

**Полный URL:** `POST https://api.wazzup24.com/v3/message` (S4)

**Заголовки:**
```
Content-Type: application/json
Authorization: Bearer {apiKey|sidecarApiKey}
```

### 2.1. Структура тела запроса (дословно из S4)

```
POST /v3/message
├── channelId *
├── chatType *
├── chatId
├── text
├── contentUri
├── refMessageId
├── crmUserId
├── crmMessageId
├── username
├── phone
├── clearUnanswered
├── templateId
├── templateValues[]
└── buttonsObject
    ├── buttons[]
    │   ├── text
    │   ├── type
    │   ├── payload
    │   ├── url
    │   ├── callbackData
    │   └── intent
    ├── replyMarkup
    ├── removeKeyboard
    └── oneTimeKeyboard
```

> ⚠️ **Дерево выше неполно и справедливо не для всех каналов.** Оно скопировано из блока «Структура
> запроса» S4, но ниже на той же странице есть отдельная таблица «Для MAX Bot», где:
> - `buttons*` — **`array of arrays`**, «Кнопки в виде двумерного массива» (а не плоский массив, как в дереве);
> - у `buttonsObject` есть поле **`chatType`**, которого в дереве нет вообще — оно присутствует в примере
>   MAX Bot из доков (`"buttonsObject": { "chatType": "max", "buttons": [ [ … ] ] }`).
>
> Двумерный массив кнопок используется также для Telegram Bot (примеры инлайн- и кастомной клавиатуры в S4).
> Для нашего проекта (WhatsApp + Instagram) актуален только плоский вариант из блока
> «Для интерактивных сообщений WABA».

### 2.2. Поля (все — из S4)

| Поле | Тип | Обяз. | Описание |
|------|-----|-------|----------|
| `channelId` | String | **да** | Id канала (uuidv4), через который нужно отправить сообщение |
| `chatType` | String | **да** | Тип сущности в мессенджере/соцсети. Значения — см. §3 |
| `chatId` | String | условно | Идентификатор чата. Формат — см. §3.2 |
| `text` | String | условно | Текст. «Обязателен, если не указан `contentUri`. Одновременно передавать и `text`, и `contentUri` нельзя» |
| `contentUri` | String | условно | Ссылка на файл. «Обязателен, если не указан `text`». «Контент должен скачиваться по ссылке **без редиректов**. Попытка скачать контент будет сразу же после получения запроса, то есть можно делать короткоживущие ссылки» |
| `refMessageId` | String | нет | Id сообщения для цитирования |
| `crmUserId` | String | нет | Id пользователя CRM (из CRUD users). «Если указано, и если такой пользователь уже существует, покажем в iframe, кто из сотрудников отправил сообщение. **Не работает при подключении по Sidecar API**» |
| `crmMessageId` | String | нет | Id сообщения на стороне CRM. «Нужен для придания роуту идемпотентности» |
| `username` | String | нет | **Только Telegram.** Имя пользователя без `@`. Можно использовать, если неизвестен `chatId` |
| `phone` | String | нет | **Только Telegram, MAX.** «Телефон контакта в международном формате, без `+` и иных символов: только цифры с корректным кодом страны» |
| `clearUnanswered` | Boolean | нет | «Сбросить ли счетчик неотвеченных. Чтобы сообщение не сбрасывало счетчик, укажите `false`. Например, при автоматизации… Если ничего не указать — исходящее сообщение сбросит счетчик» |
| `templateId` | String | нет | Код шаблона WABA |
| `templateValues` | String (Array) | нет | Значения переменных шаблона WABA |
| `buttonsObject` | object | нет | Кнопки (WABA interactive / WABA-шаблоны / Telegram Bot / MAX Bot) |

**Идемпотентность (дословно S4, без сокращений):**
> «Роут не идемпотентен! Повторные запросы с одним и тем же содержимым приведут к отправке нескольких
> одинаковых сообщений. Для защиты от возможного дублирования сообщений можно добавлять уникальное для
> сообщения свойство `crmMessageId`. Если оно было отправлено, то при поступлении другого запроса с этим же
> `crmMessageId`, сообщение не отправится, вернется ошибка 400 Bad Request,
> `{ error: 'repeatedCrmMessageId', description: 'You have already sent message with same crmMessageId' }`.
> **Проверка на `crmMessageId` длится 60 секунд.** Если юзер отправит дубль сообщения через 61 секунду
> и большее, оно уйдет.»

> ⚠️ **Расхождение в регистре кода ошибки внутри одной страницы S4.** В прозе (цитата выше) код написан
> camelCase — **`repeatedCrmMessageId`**; в разделе «Ошибки при отправке сообщений» на той же странице
> тот же случай показан как:
> ```json
> {
>     "status": 400,
>     "requestId": "c1005276e8a2b5aa23fcc94407d39f49",
>     "error": "REPEATED_CRM_MESSAGE_ID",
>     "description": "You have already sent message with same crmMessageId",
>     "data": { "crmMessageId": "1" }
> }
> ```
> **Практическое следствие:** обработчик дедупликации, сравнивающий `error == "REPEATED_CRM_MESSAGE_ID"`,
> может не сработать. **Сравнивайте коды ошибок нормализованно** (например,
> `error.replace("_", "").lower()`), а не точным равенством. Тот же класс расхождения есть в S5 для ошибок
> регистрации вебхука (`uriNotValid`, `testPostNotPassed` — camelCase, см. §4).
> Какой вариант приходит на практике — **⚠️ НЕ ПОДТВЕРЖДЕНО — проверить в личном кабинете/на практике**.

### 2.3. Ограничения на длину текста (таблица из S4)

| Канал | Лимит по таблице S4 |
|-------|---------------------|
| WhatsApp | до **10 000** символов |
| Instagram | до **1000** символов |
| WABA и Telegram | до **550** символов в шаблоне, **1024** в обычном сообщении |
| Telegram, MAX | **4096** символов |
| ВКонтакте и Авито | **1000** символов |

> ⚠️ **Внутреннее противоречие в S4.** Таблица лимитов и таблица кодов ошибок на одной и той же странице
> расходятся: `MESSAGES_TOO_LONG_INSTAGRAM` описан как «Текст сообщения Instagram* превышает **10 000**
> символов» (в таблице — 1000); `MESSAGES_TOO_LONG_VK` — «превышает **4096**» (в таблице — 1000);
> `MESSAGES_TOO_LONG_WABA` — «Максимум **1024** для заголовка и **4096** символов для основного текста»,
> при этом `MESSAGES_TOO_LONG_WABA_HEADER` — «заголовок превышает **60**», а
> `MESSAGES_TOO_LONG_WABA_TEMPLATE` — «текст шаблона превышает **1024**».
> **Практический вывод:** для нашего бота резать текст по самому строгому значению (Instagram — 1000),
> и всегда обрабатывать ошибку по коду, а не полагаться на цифру.

**Форматирование текста** (S4): для WhatsApp, WABA, Viber и Telegram Bot — `*bold*` / `_italic_` /
` ```Monospaced``` ` / `~strikethrough~`.
Дополнительно Telegram Bot: `<b>`, `<i>`, `<u>`, `<s>`.
Для WABA без `templateId` шаблон можно отправить через `text`:
`text: "@template: 6201005a-9a6f-486f-bdd5-e6cb86c76ddb { [[значение переменной]] }"`.

### 2.4. Вложения: типы и размеры

**Лимит для API — 10 МБ.** Дословно (S12):
> «Ограничение для всех вложений при отправке из чата CRM, **в том числе по API с помощью метода отправки
> сообщений — 10 МБ**.»

Соответствующий код ошибки (S4): `MESSAGES_CONTENT_SIZE_EXCEEDED` — «Контент превышает допустимый размер 10 MB».

Ограничения по типам/размерам **при отправке из виджета и мобильного приложения** (S12) — то есть выше
API-лимита в 10 МБ, для API они не применяются, но задают допустимые **форматы**:

| Канал | Форматы (S12) |
|-------|---------------|
| **WhatsApp** | Документы `.pdf .zip .gif .do* .xl .ppt`; изображения `.jpeg .jpg .png`; аудио `.aac .mp3 .amr .mpeg`; видео `.mp4`; голосовые `.ogg; codecs=opus` — все до 45 МБ (из виджета) |
| **Instagram\*** | «В директ можно отправить **только текст и изображения: jpg, png, bmp — 8 МБ**. Ответить на комментарий можно **только текстовым сообщением**. Сообщения с прикрепленными документами, фото, видео и аудио файлами не отправятся» |
| **WABA** | обычные сообщения: документы `.pdf .zip .docx .xl .ppt` 50 МБ; изображения `.jpeg .jpg .png` 5 МБ; видео `.mp4` 16 МБ; аудио `.aac .mp3 .amr .m4a .ogg` 16 МБ |
| **Telegram** | документы 20 МБ; изображения 5 МБ; аудио `.mp3 .m4a` 20 МБ; видео `.mp4` 20 МБ |
| **Viber / ВКонтакте / Авито / MAX / Циан** | 50 МБ (детали в S12) |

Ошибочный код по типам для Instagram (S4): `MESSAGES_UNSUPPORTED_CONTENT_TYPE_INSTAPI` —
«Поддерживаемые типы контента для Instagram*: **jpg, gif, png, ico, bmp**».

> ⚠️ Ещё одно расхождение: S12 для Instagram Direct даёт `jpg, png, bmp`, а код ошибки в S4 —
> `jpg, gif, png, ico, bmp`. Безопасное пересечение: **jpg, png, bmp**.

### 2.5. Ответ

Дословно (S4):
```
HTTP/1.1 201 OK
{
    "messageId": "f66c53a6-957a-46b2-b41b-5a2ef4844bcb",
    "chatId": "79999999999"
}
```
Поля: `messageId` (String, «Указывается только при code=OK»), `chatId` (String, то же).

> Примечание: **код успеха — 201**, не 200. Реальная реализация из S15 проверяет
> `if resp.status not in [200, 201]` — закладывайтесь на оба.

### 2.6. Примеры запросов

**Обычное текстовое сообщение (дословно из S4):**
```javascript
fetch("https://api.wazzup24.com/v3/message", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": "Bearer {apiKey|sidecarApiKey}",
  },
  body: {
    channelId: "e0629e11-0f67-4567-92a9-2237e91ec1b9",
    refMessageId: "61e5a375-1760-452f-ad73-5318844ffc4f",
    crmUserId: "string-user-id",
    crmMessageId: "string-crm-message-id",
    chatId: "string-chat-id",
    chatType: "whatsapp",
    text: "message text"
  },
});
```

**Что реально шлёт живой бот (S15, `main.py`), — перекрёстная проверка:**
```python
url = "https://api.wazzup24.com/v3/message"
headers = {"Authorization": f"Bearer {WAZZUP24_API_KEY}", ...}
payload = {
    "channelId": WAZZUP24_CHANNEL_ID,
    "chatId": chat_id,
    "chatType": "whatsapp",
    "text": message_text,
}
```
Формат совпадает с документацией.

**Шаблон WABA с кнопками (дословно S4):**
```javascript
body: {
    channelId: "24197d5f-06de-421f-8576-9f6e6cb67f28",
    chatType: "whatsapp",
    chatId: "79994621848",
    templateId: "6201005a-9a6f-486f-bdd5-e6cb86c76ddb",
    templateValues: ["value"],
    buttonsObject: {
      buttons: [
        { payload: "button_payload 1" },
        { payload: "button_payload 2" },
        { payload: "button_payload 3" }
      ]
    }
}
```
Обратите внимание: **`chatType` для WABA-канала — тоже `whatsapp`** (в enum нет значения `wapi`).

**Интерактивное сообщение WABA с кнопками (дословно S4, полное тело):**
```javascript
body: {
    channelId: "e0629e11-0f67-4567-92a9-2237e91ec1b9",
    refMessageId: "61e5a375-1760-452f-ad73-5318844ffc4f",
    crmUserId: "string-user-id",
    crmMessageId: "string-crm-message-id",
    chatId: "string-chat-id",
    chatType: "whatsapp",
    text: "message text",
    buttonsObject: {
      buttons: [
        {text: "Да", type: "text"},
        {text: "Нет", type: "text"},
        {text: "возможно", type: "text"}
      ]
    }
}
```
(`refMessageId`, `crmUserId`, `crmMessageId` опциональны, но в оригинальном примере S4 они присутствуют —
ровно как в примере «Обычное сообщение» выше.)

**Ограничения кнопок — по блокам таблицы `buttonsObject` в S4 (они разные для разных каналов):**

| Блок таблицы S4 | Ограничения (дословно) |
|---|---|
| **Для интерактивных сообщений WABA** | `buttons` — «Массив объектов с кнопками. **Не более 10.** Если кнопок больше 10, возьмем первые 10»; `buttons.text` — «Ограничение для кнопки WABA — **20 символов**»; `buttons.type` — «Тип кнопки. **Сейчас поддерживаем только текстовый формат — указывайте тип `text`**»; `buttons.payload` — «Полезная нагрузка кнопок в шаблонах и интерактивных сообщениях WABA» |
| **Для сообщений Telegram Bot** | `buttons.text` — «Ограничение для кнопки Telegram Bot — **64 символа**»; плюс `replyMarkup` (`inline`/`reply`), `url`, `callbackData`, `removeKeyboard`, `oneTimeKeyboard`; кнопки — двумерный массив |
| **Для MAX Bot** | `buttons*` — «**array of arrays.** Кнопки в виде двумерного массива»; `buttons.type*` — «Тип кнопки. Возможные значения: **`callback`, `link`, `message`**»; `buttons.text*` — «Текст кнопки для всех типов»; `buttons.payload*` — «Только для типа `callback`»; `buttons.intent` — «Возможные значения: **`default`, `positive`, `negative`**»; `buttons.url*` — «Только для типа `link`» |

> ⚠️ Ограничение «`buttons.type` поддерживается только `text`» относится **только к интерактивным
> сообщениям WABA**, а не к полю `buttons.type` вообще: для MAX Bot документация явно перечисляет
> `callback`, `link`, `message`. Для нашего канала (WhatsApp/Instagram) действует именно WABA-строка.

### 2.7. Редактирование и удаление

| Операция | Метод (S4) |
|----------|-----------|
| Редактировать | `PATCH https://api.wazzup24.com/v3/message/:messageId` — поля `text` **или** `contentUri` (не одновременно), плюс `crmUserId` |
| Удалить | `DELETE https://api.wazzup24.com/v3/message/:messageId` |

Ограничены по времени: `MESSAGES_EDITING_TIME_EXPIRED`, `MESSAGES_DELETION_TIME_EXPIRED`.
Конкретные окна в минутах — **НЕ ПОДТВЕРЖДЕНО** (в S4 сказано только «в течение установленного времени»).

---

## 3. `chatType` и формирование `chatId`

### 3.1. Точные допустимые значения `chatType`

Enum одинаков и в отправке (S4), и во входящем вебхуке (S5). **Полный список — 10 значений:**

```
whatsapp    — для индивидуальных чатов в WhatsApp
whatsgroup  — для групповых чатов в WhatsApp
viber       — для чатов Viber
instagram   — для чатов в Instagram*
telegram    — для индивидуальных чатов в Telegram
telegroup   — для групповых чатов в Telegram
vk          — для чатов ВКонтакте
avito       — для чатов Авито
max         — для индивидуальных чатов MAX
maxgroup    — для групповых чатов MAX
```
(дословный список из S4; в S5 тот же перечень: «Доступные значения: whatsapp, whatsgroup, viber,
instagram*, telegram, telegroup, vk, avito, max, maxgroup»)

**Значений `wapi`, `tgapi`, `maxbot` в `chatType` НЕТ.** Это значения другого поля — `transport`
(тип канала, см. §10).

> ⚠️ **Вся таблица ниже, кроме строки `wapi`, — НАШ ВЫВОД, а не цитата.** Связывающей таблицы
> «`transport` → `chatType`» в первоисточниках **нет нигде**: S6 (`rabota-s-kanalami`) даёт только список
> значений `transport`, S4/S5 — только список значений `chatType`. Единственная строка, подтверждённая
> примером из документации, — `wapi` → `whatsapp`. Остальные девять строк выведены из совпадения имён.
> Косвенно механизм соответствия подтверждается существованием ошибки `WRONG_TRANSPORT` (S4), но она
> доказывает лишь **факт проверки**, а не саму таблицу.
> **⚠️ НЕ ПОДТВЕРЖДЕНО — проверить в личном кабинете/на практике** (для нашего проекта достаточно
> проверить строки `whatsapp` и `instagram` реальным `GET /v3/channels` + тестовой отправкой).

| `transport` (канал, S6) | `chatType` (чат/сообщение, S4/S5) | Статус |
|---|---|---|
| `whatsapp` | `whatsapp` / `whatsgroup` | вывод по совпадению имён |
| `wapi` (WABA) | `whatsapp` | ✅ **подтверждено** примером WABA-шаблона в S4 (`chatType: "whatsapp"` + `templateId`) |
| `instagram` | `instagram` | вывод по совпадению имён |
| `tgapi` (Telegram-клиент) | `telegram` / `telegroup` | вывод (в enum `chatType` значения `tgapi` нет) |
| `telegram` (Telegram Bot) | `telegram` / `telegroup` | вывод по совпадению имён |
| `max` / `maxbot` | `max` / `maxgroup` | вывод (в enum `chatType` значения `maxbot` нет) |
| `vk` | `vk` | вывод по совпадению имён |
| `avito` | `avito` | вывод по совпадению имён |
| `viber` | `viber` | вывод по совпадению имён |

Несоответствие транспорта и `chatType` даёт ошибку `WRONG_TRANSPORT` (S4):
```json
{
    "status": 400,
    "requestId": "21a9be7692d378b0270e7fc1d993381a",
    "error": "WRONG_TRANSPORT",
    "description": "You can't send message to vk chat from whatsapp transport",
    "data": {
        "channelId": "dffa1c7b-6db8-4b8f-b559-91166aba879e",
        "transport": "whatsapp",
        "chatType": "vk"
    }
}
```

### 3.2. Формат `chatId` (дословно из S4)

> «Идентификатор сущности в мессенджере, соцсети:
> - для **whatsapp, viber** — только цифры, без пробелов и специальных символов **в формате `79011112233`**,
> - для **instagram\*** — **аккаунт без `@` вначале**,
> - для **whatsgroup** — приходит в вебхуках входящих сообщений,
> - для **telegram, max** — приходит в вебхуках входящих сообщений и в ответ на запрос при отправке
>   исходящего с параметрами `phone`, а также `username` для Telegram,
> - для **avito и vk** приходит в вебхуках входящих сообщений»

**WhatsApp / KZ-номера.** Формат — номер в международном виде **без `+`, без пробелов, скобок и дефисов**,
только цифры, начиная с кода страны. Для Казахстана: `77012345678`.
Пример из доков: `79011112233`, `79994621848`, `79999999999` (все — S4).
Явного упоминания казахстанских номеров в API-документации **нет** — правило общее: только цифры с кодом страны.

**Instagram.** Документация S4 говорит «аккаунт без `@` вначале». **Однако** в списке кодов ошибок вебхука
(S5) присутствует `CHATID_IGSID_MISMATCH`, а `chatId` во входящих Instagram-вебхуках на практике —
IGSID (числовой Instagram-scoped ID). **Практическая рекомендация: НЕ конструировать `chatId` для Instagram
вручную — всегда брать его из входящего вебхука** (тем более что писать первым в Instagram нельзя, см. §9).
Точное соотношение «username vs IGSID» в поле `chatId` — **НЕ ПОДТВЕРЖДЕНО** документацией.

---

## 4. Регистрация вебхука

**Endpoint (S5):** `PATCH https://api.wazzup24.com/v3/webhooks`

**Структура запроса (дословно S5):**
```
PATCH /v3/webhooks
├── webhooksUri
└── subscriptions
    ├── messagesAndStatuses
    ├── contactsAndDealsCreation
    ├── channelsUpdates
    └── templateStatus
```

| Параметр | Тип | Описание (дословно S5) |
|----------|-----|------------------------|
| `webhooksUri` | String | «Адрес для получения вебхуков. **Не более 200 символов**» |
| `subscriptions` | object | Настройки вебхуков |
| `subscriptions.messagesAndStatuses` | Boolean | «Вебхук о новых сообщениях и вебхук об изменении статуса исходящих» |
| `subscriptions.contactsAndDealsCreation` | Boolean | «Вебхук о том, что нужно создать новый контакт или сделку» |
| `subscriptions.channelsUpdates` | Boolean | «Вебхук об изменении статуса канала» |
| `subscriptions.templateStatus` | Boolean | «Вебхук об изменении статуса модерации шаблона WABA» |

**Пример запроса (дословно S5):**
```bash
curl --location --request PATCH 'https://api.wazzup24.com/v3/webhooks' \
--header 'Authorization: Bearer w11cf3444405648267f900520d454368d27' \
--header 'Content-Type: application/json' \
--data-raw '{
"webhooksUri": "https://example.com/webhooks",
"subscriptions": {
"messagesAndStatuses": true,
"contactsAndDealsCreation": true
}
}'
```
Ответ: `{ ok }` (так в документации).

**Проверка текущего адреса:** `GET https://api.wazzup24.com/v3/webhooks` (S5)
```
HTTP/1.1 200 OK
{
"webhooksUri": "https://example.com/webhooks",
"subscriptions": {
"messagesAndStatuses": "true",
"contactsAndDealsCreation": "true"
}
}
```

**Ошибки регистрации (дословно S5):**

| Код | Тело |
|-----|------|
| 400 Bad Request | `{ error: 'uriNotValid', description: 'Provided URI is not valid URI' }` — при неверном по формальным признакам URI |
| 400 Bad Request | `{ error: 'testPostNotPassed', description: 'URI does not return 200 OK on test request', data: { '${код ответа}' } }` — если получена ошибка при отправке тестового запроса |

Важно (S5): «Вебхуки отправляем методом POST на указанный URI. **Он может включать в себя query string.**»
— это разрешает трюк с секретом в query (см. §7).

---

## 5. Структура входящего payload

### 5.1. Общее (дословно S5)

> «Вебхуки содержат в теле JSON и соответствующий заголовок `Content-Type: application/json; charset-utf-8`.
> В JSON закодирован объект со свойствами, которые соответствуют типам вебхуков.
> Вебхуки о новых сообщениях и изменении их статуса **могут содержать объекты `messages` и `statuses`
> одновременно**. Вебхук о необходимости создать контакт или сделку содержит только один объект:
> `createContact` или `createDeal`.»

### 5.2. `messages` — полная структура (дословно S5)

```json
{
  "messages": [
    {
      "messageId": "String (uuid4)",
      "channelId": "String (uuid4)",
      "chatType": "String",
      "chatId": "String",
      "avitoProfileId": "String",
      "dateTime": "String",
      "type": "String",
      "isEcho": "Boolean",
      "contact": {
        "name": "String",
        "avatarUri": "String",
        "username": "String",
        "phone": "String"
      },
      "text": "String",
      "contentUri": "String",
      "status": "String",
      "error": {
        "error": "String",
        "description": "String"
      },
      "authorName": "String",
      "authorId": "String",
      "instPost": { },
      "interactive": [ ],
      "quotedMessage": { },
      "sentFromApp": "Boolean",
      "isEdited": "Boolean",
      "isDeleted": "Boolean",
      "oldInfo": {
        "oldText": "String",
        "oldAuthorId": "String",
        "oldAuthorName": "String"
      }
    }
  ]
}
```

**Поля (дословные описания из S5):**

| Поле | Тип | Описание |
|------|-----|----------|
| `messageId` | String (uuid4) | guid сообщения в Wazzup |
| `channelId` | String (uuid4) | ID канала |
| `chatType` | String | «Тип сущности в мессенджере, соцсети. Доступные значения: whatsapp, whatsgroup, viber, instagram*, telegram, telegroup, vk, avito, max, maxgroup» |
| `chatId` | String | Идентификатор сущности в мессенджере, соцсети |
| `avitoProfileId` | String | «Id профиля Авито. Не то же, что chatId» |
| `dateTime` | String | «Время отправки сообщения в формате `yyyy-mm-ddThh:mm:ss.ms`» (в примерах — ISO-8601 с `Z`, напр. `2025-05-06T14:16:00.002Z`) |
| `type` | String | «Тип сообщения: `text`, `image`, `audio`, `video`, `document`, `vcard`, `geo`, `wapi_template`, `unsupported`, `missing_call`, `system` (системное), `unknown`» |
| `isEcho` | Boolean | «**Если сообщение входящее — `false`. Если исходящее — `true`**» |
| `contact` | object | Информация о контакте |
| `text` | String | «Текст сообщения. Может отсутствовать, если сообщение с контентом» |
| `contentUri` | String | «Ссылка на контент сообщения. Может отсутствовать, если сообщение не содержит контента» |
| `status` | String | «Содержит только значение из ENUM из вебхука statuses: `sent`, `delivered`, `read`, `error`, `inbound`» |
| `error` | object | «Приходит, если `status: error`» |
| `authorName` | String | «Имя пользователя, отправившего сообщение. **Может быть только при `isEcho == true`**» |
| `authorId` | String | Идентификатор пользователя CRM |
| `instPost` | object | «Информация о посте из Instagram*. Прикладывается к комментарию в Instagram*» |
| `interactive` | Interactive | «Массив объектов с кнопками Salesbot amoCRM» |
| `quotedMessage` | Object | «Объект с параметрами цитируемого сообщения» |
| `sentFromApp` | Boolean | «**`true`, если отправлено из нативного чата Wazzup**» |
| `isEdited` | Boolean | «Показывает, что сообщение отредактировано» |
| `isDeleted` | Boolean | «Показывает, что сообщение удалено» |
| `oldInfo` | object | «Содержит информацию об измененном или удаленном сообщении» |
| `advert` | object | «Объявление Авито, по которому написал клиент» |

**Вложенные объекты (S5):**

`contact`: `name` (String, имя контакта), `avatarUri` (String, URI аватарки),
`username` (String, «**Только для Telegram.** username без @»),
`phone` (String, «**Только для Telegram, MAX.** Телефон в международном формате»).

> ⚠️ Для WhatsApp `contact.phone` **не документирован** — там сам `chatId` и есть номер.
> В примерах S5 у WhatsApp приходит `contact: {"name": "79999999999", "avatarUri": "..."}` — имя
> подставляется как номер, если у контакта нет push-name.

`error`: `error` (String, «Код ошибки (`BAD_CONTACT`, `CHATID_IGSID_MISMATCH`, `TOO_LONG_TEXT`, `SPAM` и др.)»),
`description` (String).

`instPost`: `id`, `src`, `author`, `description`. Реальный пример объекта (S5) содержит больше полей:
```json
{
  "id": "2430659146657243411_41370968890",
  "src": "https://www.instagram.com/p/CG7b52ejyET",
  "sha1": "dc8c036b4a0122bb238fc38dcb0391c125e916f2",
  "likes": 0,
  "author": "wztestdlv",
  "comments": 22,
  "timestamp": 1603977171000,
  "updatedAt": 1608905897958,
  "authorName": "",
  "authorId": "78596324",
  "description": "Красота",
  "previewSha1": "3a55c2920912de4b6a66d24568470dd4ad367c34",
  "imageSrc": "https://store.dev-wazzup24.com/dc8c...",
  "previewSrc": "https://store.dev-wazzup24.com/3a55..."
}
```

`oldInfo`: `oldText`, `oldAuthorId`, `oldAuthorName`.

> `quotedMessage` и `interactive` в документации показаны как `{ ... }` / `[ ... ]` без раскрытия полей —
> **структура НЕ ПОДТВЕРЖДЕНА**.

### 5.3. Реальные примеры входящих (дословно S5)

**Стикер WhatsApp** — приходит как `type: "image"` со ссылкой на `.was`:
```json
{
  "messages": [
    {
      "messageId": "6a2087e8-e0f4-9999-b968-9d9999933c81",
      "dateTime": "2025-05-06T14:16:00.002Z",
      "channelId": "b96a353b-9999-4cac-8413-ba99999f981",
      "chatType": "whatsapp",
      "chatId": "79999999999",
      "type": "image",
      "isEcho": false,
      "contact": {
        "name": "79999999999",
        "avatarUri": "https://store.wazzup24.com/0e999997ae07d2083c687253b8baed9999a26fa"
      },
      "contentUri": "https://store.wazzup24.com/e51159999e0046d628b3924161d411e5812d2546/?filename=f9ebe1b1-3ed5-4ec2-97fb-03f0c25e413f.was",
      "status": "inbound"
    }
  ]
}
```

**Опрос WhatsApp** — приходит как `type: "text"`, варианты склеены в текст:
```json
{
  "messages": [
    {
      "messageId": "caa9999-cce3-424c-86cd-05f99995073",
      "dateTime": "2025-05-06T14:18:00.001Z",
      "channelId": "b96a999e-06f5-4cac-8413-ba999993f981",
      "chatType": "whatsapp",
      "chatId": "79999999999",
      "type": "text",
      "isEcho": false,
      "contact": { "name": "79999999999", "avatarUri": "https://store.wazzup24.com/0e82ead..." },
      "text": "Тестовый\n• Вариант1\n• Вариант2",
      "status": "inbound"
    }
  ]
}
```

### 5.4. `statuses` — обновление статусов исходящих (дословно S5)

```json
{
  "statuses": [
    {
      "messageId": "String",
      "timestamp": "String",
      "status": "String",
      "error": {
        "error": "String",
        "description": "String",
        "[data]": "String"
      }
    }
  ]
}
```
| Поле | Описание |
|------|----------|
| `messageId` | guid сообщения в Wazzup |
| `timestamp` | «Время получения информации об обновлении статуса» |
| `status` | «Обновленный статус сообщения: `sent`, `delivered`, `read`, `error`, `edited`» |
| `error` | «Приходит, если `status: error`»; внутри `error`, `description`, опциональный `data` |

Пример (S5):
```json
{
  "statuses": [
    {
      "messageId": "be3dc577-60c4-4fc8-83a5-8c358e0bfe15",
      "timestamp": "2025-02-05T06:01:07.499Z",
      "status": "delivered"
    }
  ]
}
```

> Обратите внимание: enum статусов **различается**: в `messages.status` есть `inbound`, но нет `edited`;
> в `statuses.status` есть `edited`, но нет `inbound`.

### 5.5. Как отличить входящее от эха собственной отправки

**Основной признак — `isEcho`** (S5, дословно): «Если сообщение входящее — `false`. Если исходящее — `true`».

Дублирующий признак — `status`: у входящего `status: "inbound"` (видно в обоих примерах S5);
у исходящих — `sent` / `delivered` / `read` / `error`.

Поле называется именно **`isEcho`**. Варианта `isEchoMessage` в документации v3 **нет** — **НЕ ПОДТВЕРЖДЕНО**.

Практическое правило для бота:
```python
if msg.get("isEcho") is True:
    return          # наше собственное/операторское исходящее — не отвечать
if msg.get("status") != "inbound":
    return          # подстраховка
```

> ⚠️ Реальный open-source бот (S15) **не фильтрует `isEcho`** — он обрабатывает все элементы
> `data["messages"]` подряд. Это прямой путь к бесконечному эхо-циклу. Не повторяйте.

### 5.6. `createContact` / `createDeal` (дословно S5)

Отправляются, когда CRM должна создать сущность. Три кейса описаны в S5 (первый ответивший менеджер /
очередь / кнопка «+» в списке «Сделки»).

```json
{
  "createContact": {
    "responsibleUserId": "1",
    "name": "contacts.name",
    "contactData": [{ "chatType": "...", "chatId": "..." }],
    "source": "auto"
  }
}
```
```json
{
  "createDeal": {
    "responsibleUserId": "1",
    "contacts": ["1"],
    "source": "auto"
  }
}
```
`source` = `'auto' | 'byUser'`.
> «После этого CRM должна создать контакт и вернуть в ответе **200 OK с JSON-объектом новой сущности**,
> соответствующим сигнатуре CRUD-роутов контактов.»
> «Если сделка контакт, сделка уже созданы, то Wazzup не отправит повторно вебхук, даже если сделка
> закрыта: `"close": "true"`»

**Для нашего проекта это НЕ нужно** — мы не CRM, лид-карточку ведём у себя. Подписку
`contactsAndDealsCreation` ставьте `false`, иначе придётся отвечать корректными объектами.

### 5.7. `channelsUpdates` (дословно S5)

```json
{
  "channelsUpdates": [
    {
      "channelId": "d9e5721c-ce2b-444f-9627-60a8129d7e1f",
      "state": "qr",
      "timestamp": 1603977171000,
      "qr": "data:image/png;base64,iVBORw0KGgoAAAANS"
    }
  ]
}
```
Поля: `channelId`, `state`, `tier` («Только для каналов WABA (TIER_0, TIER_1K, TIER_10K и т.д.)»),
`qr` («QR-код в формате base64, присутствует только при state 'qr'»), `qridle`
(«канал разавторизован или QR-код протух»), `timestamp` (Integer, «Время установки статуса в мс»).

Значения `state` в **этом вебхуке** (таблица S5 — всего 7): `active`, `disabled`, `qr` (whatsapp и tgapi),
`phoneUnavailable` (whatsapp), `openElsewhere` (whatsapp), `notEnoughMoney`, `unauthorized`.

> ⚠️ Список и **регистр** значений `state` в вебхуке (S5) и в `GET /channels` (S6) **не совпадают**:
> в S5 — `qr` и `openElsewhere`; в S6 — `qridle` и `openelsewhere` (в нижнем регистре), плюс ещё
> `init`, `foreignphone`, `waitForPassword`, `blocked`, `onModeration`, `rejected`.
> Сравнивайте статусы **регистронезависимо** и обрабатывайте неизвестные значения как «не active».

### 5.8. `templateStatus` (дословно S5)

```json
{
  "templateStatus": {
    "templateGuid": "8d255e5d-aefd-44dc-8131-c3ad6c3ab28c",
    "name": "Test",
    "status": "approved"
  }
}
```
Значения `status`: `APPROVED` («Одобрен Meta*, можно использовать»), `PENDING` («На модерации Meta*»),
`REJECTED` («Отклонен Meta*»), `PAUSED` («На шаблон жаловались, Meta* его проверяет»),
`DISABLED` («Шаблон заблокирован после жалоб»).
> В примере из доков значение написано строчными (`approved`), в таблице — прописными. Сравнивайте
> регистронезависимо.

---

## 6. Требования к вебхук-эндпоинту

Всё дословно из S5, если не указано иное.

| Требование | Значение |
|------------|----------|
| Метод | `POST` на указанный URI. «Он может включать в себя query string» |
| Content-Type входящего | `application/json; charset-utf-8` (так в оригинале, с дефисом вместо `=`) |
| **Ожидаемый ответ** | **`200 OK`.** «В ответ ожидаем код 200 OK. В некоторых случаях ждем определенную информацию в теле ответа» |
| **Таймаут** | **30 секунд** («Таймаут — 30 с») |
| Заголовок Authorization на входящем | «Если у нас есть ваш `crmKey`, мы добавляем заголовок `Authorization: Bearer ${crmKey}`. Если нет — **не добавляем заголовок Authorization вообще**» |
| **Тестовый запрос при регистрации** | «При подключении на указанный url будет отправлен тестовый POST запрос с телом `{test: true}`. В ответ сервер должен вернуть 200 при успешном подключении вебхуков. Иначе вернется ошибка: `Webhooks request not valid. Response status must be 200.`» |
| Ретраи | **В документации Пользовательского API (S5) политика ретраев НЕ ОПИСАНА — НЕ ПОДТВЕРЖДЕНО.** В документации техпартнёров (S14, другой API `tech.wazzup24.com/v2`) сказано: «Ретраи. При временных ошибках доставки мы **можем** повторить отправку (**количество попыток и интервалы могут меняться**). Делайте обработку идемпотентной» |
| Отключение вебхука при сбоях | **НЕ ПОДТВЕРЖДЕНО.** Ни в S5, ни в S14 не описано автоматическое отключение подписки после N неудач |

**Рекомендации самого Wazzup (S14):**
> «Отвечайте быстро 200 OK, остальную обработку делайте асинхронно.»
> «Защита. Размещайте endpoint за HTTPS.»
> «Лимиты. Не выполняйте тяжёлую работу синхронно в обработчике вебхука — используйте очередь/фоновую обработку.»

**Практический шаблон FastAPI:**
```python
@app.post("/wazzup/webhook/{secret}")
async def wazzup_webhook(secret: str, request: Request, bg: BackgroundTasks):
    if not hmac.compare_digest(secret, settings.WAZZUP_WEBHOOK_SECRET):
        raise HTTPException(404)
    body = await request.json()
    if body.get("test") is True:           # тестовый POST при регистрации
        return JSONResponse({"ok": True}, status_code=200)
    bg.add_task(process_wazzup_payload, body)   # вся работа — асинхронно
    return JSONResponse({"ok": True}, status_code=200)   # отвечаем < 30 c
```

---

## 7. Проверка подлинности вебхука

**Подписи (HMAC/signature header) в Wazzup24 v3 НЕТ.** В статье S5 не упоминается ни один заголовок с
подписью, ни секрет, ни алгоритм верификации. Единственный упомянутый заголовок аутентификации —
`Authorization: Bearer ${crmKey}`, и он приходит **только если у Wazzup есть ваш `crmKey`**
(`crmKey` — сущность из сценария техпартнёра/WAuth; в статьях Пользовательского API v3 способ его задать
**не описан** — **НЕ ПОДТВЕРЖДЕНО**, что он вообще появляется при подключении обычным ключом API).

**Альтернативы, которые реально доступны:**

1. **Секретный path / query string.** Прямо разрешено документацией (S5): «Вебхуки отправляем методом POST
   на указанный URI. **Он может включать в себя query string**». То есть регистрируем
   `https://bot.example.kz/wazzup/webhook/<32-байтный-случайный-токен>` и сверяем через
   `hmac.compare_digest`. Ограничение: `webhooksUri` — **не более 200 символов** (S5).
2. **Allowlist IP.** Список исходящих IP Wazzup24 в документации **НЕ ОПУБЛИКОВАН — НЕ ПОДТВЕРЖДЕНО.**
   Нужно запрашивать у поддержки Wazzup (**support@wazzup24.com** / мессенджеры на сайте). Не закладывайтесь
   на allowlist как на единственный барьер.
   *(Адрес восстановлен из `data-cfemail` в футере wazzup24.ru — на страницах он скрыт Cloudflare Email
   Obfuscation и при скрейпинге без JS выглядит как «[email protected]».)*
3. **HTTPS + строгая валидация тела** (Pydantic-схема), отбрасывание всего, что не проходит схему.
4. **Дедупликация по `messageId`** — на случай ретраев (S14 прямо просит идемпотентность).

---

## 8. Rate limits и коды ошибок

### 8.1. Rate limit (дословно S7)

> «**429 Too Many Requests. Лимит — не более 500 запросов каждые 5 секунд. Счетчик сбрасывается каждые
> 5 секунд**, после чего можно отправлять новые запросы.»

Это ≈100 rps в среднем. Для нашего бота потолок недостижим, но при массовых рассылках — учитывать.

### 8.2. Общие HTTP-коды (S7)

| Код | Значение |
|-----|----------|
| 401 Unauthorized | «Не передали ключ API или передали неправильный ключ» |
| 403 Forbidden | «Запросы на роуты, неразрешенные к использованию с sidecar API KEY, могут приводить к ошибкам `TOO_MANY_ENTITIES`» |
| 429 Too Many Requests | превышен лимит 500 req / 5 s |
| 500 Internal server error | внутренняя ошибка |

Лимит сущностей за запрос — **100 штук** для `POST /contacts`, `POST /users`, `POST /deals`,
`PATCH /contacts/bulk_delete`, `PATCH /deals/bulk_delete` (S7).

### 8.3. Формат ошибки (дословно S7)

> «Когда возникает ошибка, вернется код статуса 4ХХ, а тело ответа будет либо пустым, либо будет содержать JSON вида:»
```json
{
  "error": "КОД_ОШИБКИ",
  "description": "CHANNEL_BLOCKED",
  "[data]": {}
}
```
`description` — «краткое описание ошибки на английском»; `data` — «объект с дополнительной информацией.
Не обязателен. Предназначен для анализа разработчиками».

Расширенный формат при отправке сообщений включает также `status` и `requestId` (S4):
```json
{
    "status": 400,
    "requestId": "7ca68797d127735e72b066b0080e2cc0",
    "error": "INVALID_MESSAGE_DATA",
    "description": "Message data is invalid",
    "data": { "fields": ["channelId"] }
}
```

### 8.4. Коды ошибок отправки / редактирования / удаления (полная таблица, дословно S4)

| Код | Описание |
|-----|----------|
| `INVALID_MESSAGE_DATA` | Некорректные значения параметров; в `data.fields` — имя поля |
| `WRONG_TRANSPORT` | `chatType` не соответствует транспорту канала |
| `REPEATED_CRM_MESSAGE_ID` | Повторный `crmMessageId` в течение 60 секунд. ⚠️ **В прозе той же страницы S4 этот же код написан camelCase — `repeatedCrmMessageId`** (см. §2.2). Сравнивать нормализованно |
| `BALANCE_IS_EMPTY` | Закончились деньги на балансе подписки WABA |
| `MESSAGE_WRONG_CONTENT_TYPE` | Неверный/неопределимый тип контента |
| `MESSAGE_ONLY_TEXT_OR_CONTENT` | Нельзя одновременно текст и вложение |
| `MESSAGE_NOTHING_TO_SEND` | Текст сообщения не найден |
| `MESSAGE_TEXT_TOO_LONG` | Текст WhatsApp превышает 10 000 символов |
| `MESSAGES_TOO_LONG_INSTAGRAM` | Текст Instagram* превышает 10 000 символов *(см. противоречие в §2.3)* |
| `MESSAGES_TOO_LONG_TELEGRAM` | Текст Telegram превышает 4096 символов |
| `MESSAGES_TOO_LONG_WABA` | Максимум 1024 для заголовка и 4096 для основного текста |
| `MESSAGES_TOO_LONG_VK` | Текст ВКонтакте превышает 4096 символов |
| `MESSAGES_TOO_LONG_AVITO` | Максимум 1000 для подписи и 1000 для текстового сообщения |
| `MESSAGES_TOO_LONG_VIBER` | Текст Viber превышает 6999 символов |
| `MESSAGES_TOO_LONG_WABA_HEADER` | Заголовок шаблона WABA превышает 60 символов |
| `MESSAGES_TOO_LONG_WABA_TEMPLATE` | Текст шаблона WABA превышает 1024 символов |
| `MESSAGES_UNSUPPORTED_CONTENT_TYPE_INSTAPI` | Поддерживаемые типы для Instagram*: jpg, gif, png, ico, bmp |
| `MESSAGES_CONTENT_CAN_NOT_BE_BLANK` | Нетекстовое сообщение без медиа |
| `MESSAGES_CONTENT_SIZE_EXCEEDED` | **Контент превышает допустимый размер 10 MB** |
| `MESSAGES_CONTENT_SIZE_EXCEEDED_WABA` | Для Telegram макс. фото 5 МБ, другой контент 16 МБ *(так в оригинале)* |
| `MESSAGES_CONTENT_SIZE_EXCEEDED_TELEGRAM` | Для Telegram — 5 МБ фото и 20 МБ прочего |
| `MESSAGES_TEXT_CAN_NOT_BE_BLANK` | Текстовое сообщение не может быть пустым |
| `CHANNEL_NOT_FOUND` | Канал не найден в интеграции |
| `CHANNEL_BLOCKED` | Канал выключен |
| `CHANNEL_WAPI_REJECTED` | Канал WABA заблокирован |
| `CHANNEL_NO_MONEY` | Канал не оплачен: не в подписке и не на тестовом периоде |
| `CHANNEL_LIMIT_EXCEEDED` | Превышен лимит активных диалогов для канала |
| `MESSAGE_CHANNEL_UNAVAILABLE` | Канал недоступен: статус «Телефон недоступен» или «Подождите минутку» |
| `MESSAGE_DOWNLOAD_CONTENT_ERROR` | Не удалось скачать контент по указанной ссылке |
| `MESSAGES_NOT_TEXT_FIRST` | На тарифе «Inbox» нельзя написать первым |
| `MESSAGES_IS_SPAM` | **WhatsApp оценил это сообщение как спам** |
| `MESSAGES_ABNORMAL_SEND` | Тип чата не соответствует источнику контакта (напр. WhatsApp→Instagram*) |
| `MESSAGES_INVALID_CONTACT_TYPE` | Тип чата не соответствует источнику контакта Instagram* |
| `MESSAGES_CAN_NOT_ADD` | Непредвиденная серверная ошибка |
| `REFERENCE_MESSAGE_NOT_FOUND` | Не найдено цитируемое сообщение (`refMessageId`) |
| `VALIDATION_ERROR` | Валидационная ошибка параметра |
| `MESSAGES_EDITING_TIME_EXPIRED` | Истекло время редактирования |
| `MESSAGES_DELETION_TIME_EXPIRED` | Истекло время удаления |
| `MESSAGES_CONTAIN_BUTTONS` | Сообщение с кнопками нельзя отредактировать |
| `MESSAGES_ONLY_TEXT_OR_CONTENT` | Только текст **или** вложение |
| `CHANNEL_INVALID_TRANSPORT_FOR_EDITING` | Канал не поддерживает редактирование |
| `CHANNEL_INVALID_TRANSPORT_FOR_CONTENT_EDITING` | Канал не поддерживает редактирование вложений |
| `CHANNEL_INVALID_TRANSPORT_FOR_DELETION` | Канал не поддерживает удаление |
| `CHAT_NO_ACCESS` | Нет доступа к указанному чату |
| `MESSAGES_NOT_FOUND` | Сообщение не найдено или не содержит контента |
| `TEMPLATE_REJECTED` | Шаблон отклонен. Попробуйте другой или дождитесь входящего |
| `BAD_CONTACT` | **«Не удалось отправить. Номера нет в WhatsApp или версия устарела»** |
| `UNKNOWN_ERROR` | Неизвестная ошибка |
| `UNKNOWN_ERROR_WITH_TRACE_ID` | Неизвестная ошибка с trace id |

**Коды ошибок, приходящие внутри вебхука** (`messages[].error.error`, S5):
`BAD_CONTACT`, `CHATID_IGSID_MISMATCH`, `TOO_LONG_TEXT`, `SPAM` «и др.» — полный список **НЕ ОПУБЛИКОВАН**.

---

## 9. Специфика Instagram Direct и WhatsApp

### 9.1. Instagram Direct через Wazzup — **окно 7 дней, а не 24 часа**

Дословно (S10):
> «**Можно ли писать первым** — Нет. Написать первым в Direct можно с приложения на телефоне.
> Когда клиент ответит на ваше сообщение — откроется **7-дневное диалоговое окно**. В течение этого
> времени вы можете писать клиенту с канала Instagram*.
> Каждое входящее от клиента сообщение **продлевает диалоговое окно еще на 7 дней**. Если 7 дней прошли,
> вы не сможете отправить клиенту сообщение.»

> ⚠️ Это важная поправка к распространённому мифу про «24 часа в Instagram». В документации **Wazzup**
> для Instagram Direct фигурирует именно **7 дней**. (Meta исторически даёт 24 ч standard messaging +
> расширение до 7 дней через human_agent tag; Wazzup описывает конечное наблюдаемое поведение как 7 дней.)

**Комментарии к постам (S10):**
- Входящие комментарии приходят «со ссылкой на сам пост» (объект `instPost` в вебхуке, см. §5.2).
- Ответ **с цитированием** (`refMessageId`) → уйдёт как ответ на комментарий к посту.
  «Ответить на комментарий с цитированием можно **только из чатов Wazzup**» — т.е. через API это,
  вероятно, недоступно; **НЕ ПОДТВЕРЖДЕНО**, работает ли `refMessageId` на комментарий Instagram через API.
- Ответ **без цитирования** → уйдёт в Direct, но «только в течение семи дней с момента публикации
  комментария. Причем для каждого комментария можно отправить **только одно сообщение в Direct**,
  после чего надо ждать ответа клиента».

**Вложения Instagram (S12):** «только текст и изображения: jpg, png, bmp — 8 МБ».
На комментарий — «только текстовым сообщением».

**Практический вывод для нашего бота:** любой сценарий «отправь мне видео тренировки» в Instagram
Direct — **невозможен**. Только текст + картинка. Видео/PDF придётся отдавать ссылкой в тексте.

### 9.2. WhatsApp

**Личный WhatsApp (`transport: whatsapp`)** — 24-часового окна и шаблонов **нет**; писать первым можно
(если тариф не «Inbox», иначе `MESSAGES_NOT_TEXT_FIRST`). Главный риск — бан за спам:
код ошибки `MESSAGES_IS_SPAM` — «WhatsApp оценил это сообщение, как спам» (S4).

**WABA (`transport: wapi`)** — дословно (S11):
> «**Начать переписку = отправить шаблон WABA вне окна обслуживания клиента.**
> **Окно обслуживания — это 24-часовая сессия, которая начинается, когда клиент отправил вам входящее
> сообщение.** Если от контакта не было входящих сообщений за последние 24 часа, у вас нет открытого
> окна обслуживания.
> Если вы отправили клиенту шаблон WABA, а он вам не ответил — окно обслуживания **не откроется**.
> Если клиент написал первым и вы отвечаете на его входящее в течение 24 часов, Meta* посчитает,
> что переписку начал клиент, а не вы. Поэтому такая переписка не будет засчитана в лимитах.»

**Тиры WABA (S11):** Уровень 0 — 250 переписок; Уровень 1 — 2000; Уровень 2 — 10 000;
Уровень 3 — 100 000; Уровень 4 — без ограничений. «Лимит распространяется на всё бизнес-портфолио Meta*».
Текущий тир виден в ЛК: «Каналы» → настройки канала → «Текущее ограничение Facebook*»,
и приходит в вебхуке `channelsUpdates` в поле `tier` (`TIER_0`, `TIER_1K`, `TIER_10K` и т.д., S5).

Отправка шаблона: `templateId` + `templateValues` (S4), `chatType: "whatsapp"`.
Список шаблонов: `GET /v3/templates/whatsapp` (упомянут в S3 как один из Sidecar-роутов).
Статусы модерации приходят вебхуком `templateStatus` (§5.8).

**Для нашего проекта:** если канал — **личный WhatsApp** (а не WABA), 24-часового окна и шаблонов нет;
бот может отвечать в любой момент. Это самый вероятный сценарий для школы бокса в Костанае.

---

## 10. `GET /channels` — channelId и статус канала

**Endpoint (S6):** `GET https://api.wazzup24.com/v3/channels`

```bash
curl --location --request GET 'https://api.wazzup24.com/v3/channels' \
--header 'Authorization: Bearer c8cf90444023482f909520d454368d27'
```

**Ответ (дословно S6):**
```
HTTP/1.1 200 OK

[
  {
    "channelId": "string",
    "transport": "whatsapp",
    "plainId": "79865784457",
    "state": "active"
  }
]
```

| Поле | Тип | Описание |
|------|-----|----------|
| `channelId` | String | Id канала (uuidv4) — **это и есть то, что кладём в `channelId` при отправке** |
| `transport` | String | Тип канала (см. ниже) |
| `plainId` | String | «Номер телефона, юзернейм в ID в мессенджере» |
| `state` | String | Состояние канала |

**`transport` — точные значения (дословно S6):**
```
whatsapp   — WhatsApp
instagram  — Instagram*
tgapi      — Telegram
max        — MAX
maxbot     — MAX Bot
wapi       — WABA
telegram   — Telegram Bot
vk         — ВКонтакте
avito      — Авито
viber      — Viber
```

**`state` — точные значения (дословно S6):**
| Значение | Описание |
|----------|----------|
| `active` | канал активен |
| `init` | канал запускается |
| `disabled` | канал выключен: его убрали из подписки или удалили с сохранением сообщений |
| `phoneUnavailable` | нет связи с телефоном |
| `qridle` | необходимо отсканировать QR-код |
| `openelsewhere` | канал авторизован в другом аккаунте Wazzup |
| `notEnoughMoney` | канал не оплачен |
| `foreignphone` | QR канала отсканирован не тем аккаунтом в мессенджере |
| `unauthorized` | не авторизован |
| `waitForPassword` | нужно ввести пароль для двухфакторной аутентификации |
| `blocked` | Facebook* заблокировал канал |
| `onModeration` | канал WABA находится на модерации |
| `rejected` | канал WABA отклонен |

> См. предупреждение в §5.7 о расхождении регистра/набора значений между `GET /channels` и
> вебхуком `channelsUpdates`.

---

## 11. Вход живого оператора в диалог / передача оператору

### Короткий ответ: **выделенного события «в диалог вошёл оператор» в Wazzup24 API v3 НЕТ.**

Проверено по всему разделу «Пользовательское API» (S1): статьи
`webhooks-2`, `otpravka-soobshhenij`, `sushhnosti-api-i-terminologiya`, `schetchik-neotvechennyh`,
`rabota-s-kanalami`, `rabota-s-kontaktami`, `obshhie-oshibki`.
Ни `agentJoined`, ни `handover`, ни `takeover`, ни «передача оператору», ни флага «бот на паузе»
в документации **не существует — НЕ ПОДТВЕРЖДЕНО**.

### Что реально есть и как из этого собрать паузу бота

**1. Эхо исходящего + признак «отправлено из нативного чата Wazzup».**
Когда оператор пишет клиенту руками из чатов Wazzup (веб/десктоп/мобильное приложение), вам придёт
вебхук `messages` с (S5):
- `isEcho: true` — «Если исходящее — true»,
- `sentFromApp: true` — «**true, если отправлено из нативного чата Wazzup**»,
- `authorName` — «Имя пользователя, отправившего сообщение. Может быть только при `isEcho == true`»,
- `authorId` — «Идентификатор пользователя CRM».

Это **единственный** доступный сигнал «человек вмешался». Логика паузы:
```python
if msg.get("isEcho") and msg.get("sentFromApp"):
    # оператор ответил вручную из чатов Wazzup
    await pause_bot(chat_key=(msg["channelId"], msg["chatType"], msg["chatId"]),
                    minutes=30)
```
> ⚠️ **НЕ ПОДТВЕРЖДЕНО**, приходит ли `sentFromApp: false` (или отсутствует ли поле) для сообщений,
> отправленных через API. Логически — да, но документация этого не утверждает.
> **Обязательно проверить эмпирически на боевом канале перед релизом.** Запасной вариант —
> дедуплицировать по `messageId`: сохранять `messageId`, который вернул `POST /v3/message`, и считать
> «операторским» любой эхо-вебхук с `messageId`, которого нет в вашей базе отправленных.

**2. Счётчик неотвеченных.** Это не «передача оператору», а метрика «клиенты ждут ответа» (S8, S13):
- WebSocket: `GET https://integrations.wazzup24.com/counters/ws_host/api_v3/:apiKey` → `{"host": "..."}`,
  далее socket.io **версии 4.1.3**, опции `{path: '/ws-counters/', transports: ['websocket','polling']}`,
  событие `counterConnecting` с `{type: "api_v3", apiKey, userId}`, слушать `counterUpdate` →
  `{counter, counterV2, type}`, где `type` = `'red' | 'grey' | null`.
- REST: `GET https://api.wazzup24.com/v3/unanswered/{user_id}` → «количество неотвеченных сообщений
  **за последние 7 дней**»:
```json
{ "counterV2": 7, "type": "red", "lastMsgDateTime": "2023-05-25T12:30:46.000Z" }
```
Счётчик **пер-пользователь** (`user_id` — id сотрудника в CRM), а не пер-чат, и зависит от роли
(«Менеджер» / «Руководитель» / «Контроль качества»). Для паузы бота в конкретном чате **не годится**.

**3. `clearUnanswered: false`** (S4) — обратная задача: чтобы автоответ бота **не гасил** красный счётчик
у менеджера. Дословно: «Например, при автоматизации. Тогда пользователь CRM увидит уведомление о новом
входящем, даже если его клиенту ушел автоматический ответ».
**Ставьте `clearUnanswered: false` на все автоответы бота** — иначе менеджеры перестанут видеть,
что клиент реально ждёт человека.

**4. В UI есть кнопка «Отвечать не нужно»** (S13) — снимает неотвеченное без ответа.
Соответствующего события в API/вебхуках **НЕТ — НЕ ПОДТВЕРЖДЕНО**.

### Рекомендуемая схема эскалации для нашего бота
Раз API не даёт handover, эскалацию делаем на своей стороне:
1. Бот определил интент «хочу человека» / не смог ответить N раз → ставит флаг `paused_until` в Redis
   по ключу `(channelId, chatType, chatId)` на 30–60 мин.
2. Бот шлёт клиенту «передаю менеджеру» с `clearUnanswered: false`, чтобы у менеджера остался красный счётчик.
3. Параллельно — уведомление менеджеру своим каналом (Telegram-бот школы / email), потому что
   Wazzup сам «эскалацию» не понимает.
4. Любой вебхук с `isEcho: true` + `sentFromApp: true` продлевает паузу (оператор в диалоге).
5. Пауза снимается по таймауту.

---

## 12. Прочие роуты v3 (для полноты, из S1)

| Раздел | URL статьи |
|--------|-----------|
| Работа с контактами (CRUD `/v3/contacts`) | https://wazzup24.ru/help/api-ru/rabota-s-kontaktami/ |
| Работа с сущностью пользователя (`/v3/users`) | https://wazzup24.ru/help/api-ru/rabota-s-sushhnostju-polzovatelya/ |
| Работа со списком сделок (`/v3/deals`) | https://wazzup24.ru/help/api-ru/rabota-so-spiskom-sdelok/ |
| Загрузка воронок продаж | https://wazzup24.ru/help/api-ru/zagruzka-voronok-prodazh/ |
| Окно чатов (iFrame) | https://wazzup24.ru/help/api-ru/okno-chatov-iframe/ |
| Шаблоны WABA | https://wazzup24.ru/help/api-ru/shablony-whatsapp-business-api/ |
| WAuth | https://wazzup24.ru/help/api-ru/wauth/ |
| Миграция v2 → v3 | https://wazzup24.ru/help/api-ru/migraciya-polzovatelej-api-v2-na-api-v3/ |
| Схемы интеграций | https://wazzup24.ru/help/api-ru/shemy-integracij/ |

Для нашего бота из этого списка не нужно ничего: контакты/сделки/воронки/iframe — это интерфейс для CRM,
а мы ведём лид-карточки у себя.

---

## 13. Чек-лист интеграции для ainazarov-bot

1. Получить API-ключ: ЛК → «Интеграция с CRM» → API → Подключить → «Дополнительно».
2. `GET /v3/channels` → сохранить `channelId` для WhatsApp и для Instagram; проверить `state == "active"`.
3. Поднять `POST /wazzup/webhook/<secret>` на HTTPS, отвечающий 200 за <30 с, обрабатывающий `{"test": true}`.
4. `PATCH /v3/webhooks` с `{"messagesAndStatuses": true, "contactsAndDealsCreation": false,
   "channelsUpdates": true, "templateStatus": false}`.
5. В обработчике: отбросить `isEcho == true` (кроме ветки «оператор вмешался»), дедуплицировать по `messageId`,
   маршрутизировать по `chatType`.
6. Отправка: `POST /v3/message` с `channelId`, `chatType`, `chatId`, `text` **или** `contentUri`,
   `crmMessageId` (uuid4) и `clearUnanswered: false` для автоответов.
7. Ретраи на 429 (лимит 500/5 с) и на 5xx — с экспоненциальным backoff.
8. Резать текст по 1000 символов (общий безопасный минимум), картинки ≤8 МБ jpg/png для Instagram, ≤10 МБ для API вообще.

---

## Открытые вопросы / НЕ ПОДТВЕРЖДЕНО

| # | Вопрос | Статус |
|---|--------|--------|
| 1 | **Подпись/секрет вебхука** | **Отсутствует.** Никакого HMAC/signature header в документации v3 нет (S5). Единственный механизм — `Authorization: Bearer ${crmKey}`, и он применяется, «если у нас есть ваш crmKey». Как задать `crmKey` при подключении обычным API-ключом — **не описано**. Обходной путь — секрет в path/query (разрешено S5) |
| 2 | **Список исходящих IP Wazzup для allowlist** | **НЕ ОПУБЛИКОВАН.** Запрашивать у поддержки |
| 3 | **Политика ретраев вебхуков в v3** | **НЕ ОПИСАНА** в S5. В API техпартнёров (S14, другой продукт): «можем повторить, количество попыток и интервалы могут меняться» |
| 4 | **Автоотключение вебхука при N сбоях** | **НЕ ОПИСАНО** ни в S5, ни в S14 |
| 5 | **Точный лимит текста для Instagram** | Противоречие внутри S4: таблица — 1000, код ошибки `MESSAGES_TOO_LONG_INSTAGRAM` — 10 000 |
| 6 | **Точные лимиты текста для VK/WABA** | Противоречия внутри S4 (VK 1000 vs 4096; WABA 550/1024 vs 1024/4096 vs header 60) |
| 7 | **`chatId` для Instagram: username или IGSID?** | S4 говорит «аккаунт без @», но существует ошибка `CHATID_IGSID_MISMATCH` (S5). Однозначного ответа в доках нет. Практика: брать `chatId` только из вебхука |
| 8 | **Соответствие `transport` → `chatType` (вся таблица §3.1)** | Связывающей таблицы нет ни в одном источнике: S6 даёт только список `transport`, S4/S5 — только список `chatType`. Подтверждена **только** строка `wapi` → `whatsapp` (пример в S4). Все остальные 9 строк — вывод по совпадению имён. **⚠️ НЕ ПОДТВЕРЖДЕНО — проверить на практике** |
| 9 | **Структура `quotedMessage` и `interactive`** | В S5 показаны как `{ ... }` / `[ ... ]` без раскрытия полей |
| 10 | **Полный список кодов в `messages[].error.error`** | S5 перечисляет только `BAD_CONTACT`, `CHATID_IGSID_MISMATCH`, `TOO_LONG_TEXT`, `SPAM` «и др.» |
| 11 | **Окна времени для редактирования/удаления сообщений** | В S4 только «в течение установленного времени»; минуты не указаны |
| 12 | **Приходит ли `sentFromApp: false` для API-отправленных сообщений** | Логически да, но **явно не сказано**. Критично для логики паузы бота — проверить эмпирически |
| 13 | **Событие «оператор вошёл в диалог» / handover** | **Не существует.** Ни события, ни флага, ни роута |
| 14 | **Событие «Отвечать не нужно» (кнопка в UI)** | В API не отражено |
| 15 | ~~**Работает ли legacy `POST /sendMessage`**~~ | ✅ **ЗАКРЫТО.** В HTML S3 буллет «`POST /sendMessage`» — гиперссылка на `/help/api/otpravka-soobshhenij/`, т.е. на статью про `POST /v3/message`. Это устаревшая подпись того же роута, а не отдельный endpoint. Отдельного `/v3/sendMessage` не существует |
| 16 | **Строгий формат API-ключа** | В примерах доков встречаются строки длиной 32 и 35 символов — формат не зафиксирован |
| 17 | **Ответ `PATCH /v3/webhooks`** | В доках написан как `{ ok }` — это не валидный JSON, реальное тело ответа не уточнено |
| 18 | **Rate limit — глобальный на аккаунт или на ключ** | S7 говорит «не более 500 запросов каждые 5 секунд» без уточнения области действия |
| 19 | **Instagram: работает ли `refMessageId` через API для ответа на комментарий** | S10 говорит, что цитирование комментария доступно «только из чатов Wazzup» — про API не сказано |
| 20 | **Доступность `wazzup24.com` для сервер-сайд запросов** | Отдаёт 403 на автоматические клиенты. `api.wazzup24.com` — отдельный хост, к докам отношения не имеет; проблем с API не ожидается, но проверить с боевого сервера |
| 21 | **Регистр кодов ошибок: `REPEATED_CRM_MESSAGE_ID` или `repeatedCrmMessageId`?** | Внутри одной страницы S4 оба варианта: проза — camelCase, пример JSON и таблица кодов — UPPER_SNAKE. Тот же класс расхождения в S5 (`uriNotValid`, `testPostNotPassed`). **⚠️ НЕ ПОДТВЕРЖДЕНО — проверить на практике**; до проверки сравнивать коды нормализованно (`code.replace("_","").lower()`) |
| 22 | **`buttonsObject.chatType` — обязателен ли для MAX Bot и игнорируется ли для WABA** | Поля нет в дереве «Структура запроса» S4, но оно есть в примере MAX Bot. Для нашего канала не нужно. **⚠️ НЕ ПОДТВЕРЖДЕНО** |

---

## Результаты независимой верификации

Дата верификации: **2026-08-09**. Проверка проводилась по HTML-исходникам первоисточников
(`curl` + разбор разметки), а не по текстовому рендеру страниц — часть расхождений видна только в HTML.
Всего проверено 6 утверждений: **4 WRONG** (исправлены), **2 UNVERIFIED** (одно закрыто, одно
переклассифицировано).

| # | Утверждение (как было в документе) | Вердикт | Что на самом деле |
|---|---|---|---|
| 1 | §2.6: «Ограничения кнопок (S4): не более 10, текст кнопки WABA — до 20 символов, `buttons.type` сейчас поддерживается только `text`» — подано как общее свойство поля | **WRONG** | Ограничение `type: text` верно **только для интерактивных сообщений WABA**. В блоке «Для MAX Bot» на той же странице S4 дословно: «`buttons.type*` \| string \| Тип кнопки. Возможные значения: **callback, link, message**» и «`buttons.intent` \| string \| Возможные значения: default, positive, negative». Там же «`buttons*` \| **array of arrays** \| Кнопки в виде двумерного массива» — для MAX Bot (и Telegram Bot) `buttons` **не плоский массив**, как в дереве §2.1, а двумерный; в примере MAX Bot присутствует поле `buttonsObject.chatType`, которого в дереве §2.1 нет вообще. Также был опущен лимит текста кнопки Telegram Bot — **64 символа**. **Исправлено:** §2.1 дополнено предупреждением, §2.6 переписан таблицей по трём блокам |
| 2 | §2.2, «дословная» цитата про идемпотентность с многоточием + §8.4 `REPEATED_CRM_MESSAGE_ID` | **WRONG** | Многоточие вырезало ровно ту часть, которая противоречит таблице §8.4. В S4 дословно: «вернется ошибка 400 Bad Request, `{ error: 'repeatedCrmMessageId', description: 'You have already sent message with same crmMessageId' }`» — в прозе код в **camelCase**, а в таблице кодов и в примере JSON на той же странице — **UPPER_SNAKE**. Документ приводил только UPPER_SNAKE и, в отличие от десятка других расхождений, это не помечал. Следствие: обработчик дедупликации со сравнением `error == "REPEATED_CRM_MESSAGE_ID"` может не сработать. Тот же класс расхождения уже есть в S5 (`uriNotValid`, `testPostNotPassed`). **Исправлено:** цитата восстановлена полностью, добавлено предупреждение о нормализованном сравнении, добавлен открытый вопрос №21 |
| 3 | §7, п.2: «Нужно запрашивать у поддержки Wazzup (\[email protected\] / мессенджеры на сайте)» | **WRONG** | Адреса `[email protected]` не существует — это артефакт **Cloudflare Email Obfuscation**, подставляемый вместо реального адреса при скрейпинге без выполнения JS. В футере каждой страницы wazzup24.ru лежит `<a data-cfemail="4f3c3a3f3f203d3b0f382e35353a3f7d7b612c2022">`; после XOR-дешифровки первым байтом (0x4f) получается **`support@wazzup24.com`**. **Исправлено:** адрес заменён, добавлено примечание о происхождении артефакта |
| 4 | §2.6, «Интерактивное сообщение WABA с кнопками (дословно S4)» — тело только с `channelId`, `chatId`, `chatType`, `text`, `buttonsObject` | **WRONG** | Помечено «дословно», но сокращено. В S4 пример содержит ещё три поля: `refMessageId: "61e5a375-1760-452f-ad73-5318844ffc4f"`, `crmUserId: "string-user-id"`, `crmMessageId: "string-crm-message-id"` — ровно как в примере «Обычное сообщение». Само по себе безвредно (поля опциональны), но метка «дословно» в документе, который специально противопоставляет цитаты выводам, не должна стоять на отредактированном фрагменте. **Исправлено:** пример приведён полностью |
| 5 | Открытый вопрос №15 + §1: «НЕ ПОДТВЕРЖДЕНО, работает ли `/sendMessage` в v3» | **UNVERIFIED → закрыто** | Вопрос разрешается прямо в первоисточнике, но не по тексту, а по разметке. В HTML S3 буллет «`POST /sendMessage` — отправка сообщения» — это **гиперссылка** с `href="/help/api/otpravka-soobshhenij/"`, т.е. на ту же статью про `POST https://api.wazzup24.com/v3/message` (аналогично «`GET /channels`» → `/help/api-ru/rabota-s-kanalami/`). Значит `/sendMessage` — устаревшая подпись **того же роута** в перечне Sidecar-разрешений, а не отдельный endpoint; отдельного URL `/v3/sendMessage` в документации нет, пробовать его не нужно. **Исправлено:** §1 переписан, вопрос №15 закрыт |
| 6 | §3.1: строка `tgapi` помечена «НЕ ПОДТВЕРЖДЕНО явно», остальные строки `transport → chatType` — без оговорок | **UNVERIFIED (непоследовательная строгость)** | Соответствие `transport → chatType` не описано в первоисточниках **нигде**: S6 даёт только список `transport` (whatsapp, instagram, tgapi, max, maxbot, wapi, telegram, vk, avito, viber), S4/S5 — только список `chatType`; связывающей таблицы нет. Единственная строка, подтверждённая примером из S4, — `wapi` → `whatsapp` (шаблон WABA с `chatType: "whatsapp"`), и документ это честно отмечал. Остальные девять строк — такой же вывод из совпадения имён, как и помеченная `tgapi`. Косвенное подтверждение общего механизма даёт лишь ошибка `WRONG_TRANSPORT` (S4), которая доказывает факт проверки, но не саму таблицу. **Исправлено:** весь блок помечен как вывод, у каждой строки проставлен статус, обновлён открытый вопрос №8 |

**Что НЕ изменилось по итогам верификации** (перепроверено и подтверждено дословно в S4):
лимит «не более 10 кнопок, лишние отбрасываются»; «Ограничение для кнопки WABA — 20 символов»;
«Проверка на `crmMessageId` длится 60 секунд»; код успеха `HTTP/1.1 201 OK`.
