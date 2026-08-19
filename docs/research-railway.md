# research-railway.md — деплой AINAZAROV TOP TEAM bot на Railway

**Дата сбора:** 2026-08-09. Всё проверено по актуальной документации `docs.railway.com`
(и по JSON-схеме `railway.schema.json`), а не по памяти. У каждого факта — URL источника.
Если факта в документации нет — стоит **НЕ ПОДТВЕРЖДЕНО**; такие места в коде и в инструкции
заказчику нужно обходить защитно.

Документ подчинён `docs/SCOPE-OVERRIDE.md` §2 (деплой — Railway, не VPS).

> **Как читать источники.** Документация Railway отдаётся и в markdown: к любому URL можно
> добавить `.md` (`https://docs.railway.com/deployments/healthchecks.md`) — это первоисточник,
> из него и цитировалось. Сайт был реструктурирован (старые пути `/guides/*`, `/reference/*`
> редиректят), поэтому ссылки ниже — на новые пути.

---

## 0. TL;DR для нетерпеливых

| Вопрос | Ответ |
|---|---|
| Переменная порта | `PORT`, инъектируется Railway; слушать **обязательно** `0.0.0.0`, не `127.0.0.1` |
| Формат `DATABASE_URL` | `postgresql://${{PGUSER}}:${{POSTGRES_PASSWORD}}@${{RAILWAY_PRIVATE_DOMAIN}}:5432/${{PGDATABASE}}` → в развёрнутом виде `postgresql://postgres:<pw>@postgres.railway.internal:5432/railway` |
| Формат `REDIS_URL` | `redis://${{REDISUSER}}:${{REDIS_PASSWORD}}@${{REDISHOST}}:${{REDISPORT}}` → `redis://default:<pw>@redis.railway.internal:6379` |
| Конфиг-файл | `railway.json` / `railway.toml` в корне; путь можно переопределить в настройках сервиса (абсолютный путь от корня репозитория) |
| Ссылка на чужую переменную | `${{ИмяСервиса.ПЕРЕМЕННАЯ}}`, напр. `DATABASE_URL=${{Postgres.DATABASE_URL}}` |
| Health check | HTTP GET по `healthcheckPath`, ждёт `200`, таймаут по умолчанию 300 с, **только при деплое**, не постоянно |
| Приватная сеть | `<service-name>.railway.internal`, только **runtime**, не при сборке |

---

## 1. Порт

**Имя переменной — `PORT`.**

> «Railway will inject a `PORT` environment variable that your application should listen on.
> This variable's value is also used when performing health checks on your deployments.»
> — <https://docs.railway.com/deployments/healthchecks>

**Слушать обязан `0.0.0.0`**, не `localhost`/`127.0.0.1`:

> «Your web server should bind to the host `0.0.0.0` and listen on the port specified by the
> `PORT` environment variable, which Railway automatically injects into your application.»
> — <https://docs.railway.com/networking/troubleshooting/application-failed-to-respond>

Там же приведён **дословный пример именно для uvicorn** (Railway отдельно отмечает, что uvicorn
без флагов слушает не то, что надо):

```bash
# Python / uvicorn
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Тот же рецепт повторяется в гайдах Railway по Python:
<https://docs.railway.com/deployments/troubleshooting/no-start-command-could-be-found>,
<https://docs.railway.com/guides/rag-pipeline-pgvector>,
<https://docs.railway.com/guides/langgraph-agent-backend>.

**Важные нюансы:**

- `PORT` **отсутствует** в официальной таблице «Railway-provided variables»
  (<https://docs.railway.com/variables/reference>). Он документирован только на страницах
  healthchecks и troubleshooting. Не ищите его в списке — его там нет, но он есть в контейнере.
- `PORT` можно задать руками как обычную переменную сервиса — тогда Railway использует ваше
  значение и для health check. Это нужно, если приложение слушает фиксированный порт при
  использовании target ports. Источник: <https://docs.railway.com/deployments/healthchecks>.
  **Для нашего API этого делать не надо** — пусть Railway выдаёт порт сам.
- «Магическое» определение порта: при генерации домена Railway сам определяет порт, на котором
  слушает приложение, и ставит его target port'ом домена; при нескольких портах предложит выбрать.
  — <https://docs.railway.com/networking/domains/working-with-domains#target-ports>
- **Грабли Dockerfile'а:** если сервис собран из Dockerfile, `startCommand` переопределяет
  `ENTRYPOINT` в **exec-форме**, а exec-форма **не раскрывает переменные окружения**. Значит
  `uvicorn ... --port $PORT` в `startCommand` без обёртки не сработает — `$PORT` уйдёт литералом:

  > «If you need to use environment variables in the start command for services deployed from a
  > Dockerfile or image you will need to wrap your command in a shell — `/bin/sh -c "exec python
  > main.py --port $PORT"` … This is because commands ran in exec form do not support variable expansion.»
  > — <https://docs.railway.com/deployments/start-command>,
  > <https://docs.railway.com/builds/build-and-start-commands>

  Отсюда: либо `CMD` в Dockerfile сам читает `PORT` (через shell-форму или через код),
  либо `startCommand` пишется как `/bin/sh -c "exec uvicorn ... --port $PORT"`.

---

## 2. PostgreSQL: переменные и точный формат URL

### 2.1. Что документация называет «выдаваемыми переменными»

> «Connect to the PostgreSQL server from another service in your project by referencing the
> environment variables made available in the PostgreSQL service: `PGHOST`, `PGPORT`, `PGUSER`,
> `PGPASSWORD`, `PGDATABASE`, `DATABASE_URL`»
> — <https://docs.railway.com/databases/postgresql>

### 2.2. Точные значения (из определения официального шаблона Postgres)

Документация не печатает шаблон строки подключения. Он есть в определении официального шаблона
Railway — страница **<https://railway.com/deploy/postgres>** (это официальный шаблон Railway,
разворачиваемый кнопкой `+ New → Database → PostgreSQL`). Значения по умолчанию:

| Переменная | Значение по умолчанию в шаблоне |
|---|---|
| `PGHOST` | `${{RAILWAY_PRIVATE_DOMAIN}}` (т.е. `postgres.railway.internal`) |
| `PGPORT` | `5432` |
| `PGUSER` | `${{POSTGRES_USER}}` |
| `PGPASSWORD` | `${{POSTGRES_PASSWORD}}` |
| `PGDATABASE` | `${{POSTGRES_DB}}` |
| `POSTGRES_USER` | `postgres` |
| `POSTGRES_DB` | `railway` |
| `POSTGRES_PASSWORD` | генерируется, `secret(32, …)` |
| `PGDATA` | `/var/lib/postgresql/data/pgdata` |
| `SSL_CERT_DAYS` | `820` |
| `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` | `60` |
| **`DATABASE_URL`** | **`postgresql://${{PGUSER}}:${{POSTGRES_PASSWORD}}@${{RAILWAY_PRIVATE_DOMAIN}}:5432/${{PGDATABASE}}`** |

Развёрнутое значение, которое реально придёт в контейнер:

```
postgresql://postgres:<сгенерированный_пароль>@postgres.railway.internal:5432/railway
```

Схема — **`postgresql://`** (не легаси `postgres://`), хост — **приватный домен**, порт `5432`,
БД — `railway`. Форма подтверждается и документацией:

> «postgresql://postgres:password@postgres.railway.internal:5432/railway»
> — <https://docs.railway.com/databases/build-a-database-service>

Том смонтирован в `/var/lib/postgresql/data`; образ — SSL-enabled форк официального
(<https://github.com/railwayapp-templates/postgres-ssl>), см.
<https://docs.railway.com/databases/postgresql>.

### 2.3. Публичный доступ (`DATABASE_PUBLIC_URL`)

> «Databases are deployed private by default — to expose one, open the service's
> **Settings → Networking** and add **Public Access**. This creates a TCP Proxy and populates a
> `DATABASE_PUBLIC_URL` variable with the external connection string.»
> — <https://docs.railway.com/databases/postgresql>

Формат внешнего адреса — прокси-домен и **внешний** порт (не 5432):

```
postgresql://postgres:<PGPASSWORD>@shuttle.proxy.rlwy.net:15140/railway
```
— <https://docs.railway.com/guides/migrate-database-minimal-downtime>,
переменные `RAILWAY_TCP_PROXY_DOMAIN` (пример `roundhouse.proxy.rlwy.net`) и `RAILWAY_TCP_PROXY_PORT`
(пример `11105`) — <https://docs.railway.com/variables/reference>,
<https://docs.railway.com/networking/tcp-proxy>.

**Разница публичного и внутреннего хоста:**

| | Внутренний (`DATABASE_URL`) | Публичный (`DATABASE_PUBLIC_URL`) |
|---|---|---|
| Хост | `postgres.railway.internal` | `<имя>.proxy.rlwy.net` |
| Порт | `5432` | случайный внешний (напр. `15140`) |
| Доступен откуда | только из того же проекта **и** окружения, только в runtime | из любого места интернета |
| Трафик | Wireguard-туннель внутри Railway, egress не тарифицируется | тарифицируется как Network Egress |
| Включается | по умолчанию | вручную: Settings → Networking → Public Access |

Источники: <https://docs.railway.com/networking/private-networking/how-it-works>,
<https://docs.railway.com/databases/postgresql>, <https://docs.railway.com/pricing/plans>.

### 2.4. Что это значит для нашего кода

`DATABASE_URL` придёт со схемой `postgresql://`. SQLAlchemy async требует
`postgresql+asyncpg://`. Нормализацию делаем в `app/config.py` (см. `SCOPE-OVERRIDE.md` §2 п.2),
обрабатывая и легаси-префикс `postgres://`. Переменную из Railway **не редактируем руками** —
иначе при ротации пароля она разъедется с реальностью.

---

## 3. Redis: переменные и формат URL

> «Connect to the Redis server from another service in your project by referencing the environment
> variables made available in the Redis service: `REDISHOST`, `REDISUSER`, `REDISPORT`,
> `REDISPASSWORD`, `REDIS_URL`»
> — <https://docs.railway.com/databases/redis>

Значения по умолчанию из определения официального шаблона (**<https://railway.com/deploy/redis>**):

| Переменная | Значение по умолчанию |
|---|---|
| `REDISHOST` | `${{RAILWAY_PRIVATE_DOMAIN}}` (т.е. `redis.railway.internal`) |
| `REDISPORT` | `6379` |
| `REDISUSER` | `default` |
| `REDISPASSWORD` | `${{REDIS_PASSWORD}}` |
| `REDIS_PASSWORD` | генерируется, `secret(32, …)` |
| **`REDIS_URL`** | **`redis://${{REDISUSER}}:${{REDIS_PASSWORD}}@${{REDISHOST}}:${{REDISPORT}}`** |

Развёрнуто:

```
redis://default:<сгенерированный_пароль>@redis.railway.internal:6379
```

Схема `redis://` (не `rediss://` — TLS внутри приватной сети не нужен, трафик и так в Wireguard).
Номера БД в URL нет → по умолчанию `db=0`. Том монтируется в `/data`.

Публичный вариант — `REDIS_PUBLIC_URL`, появляется после включения Public Access
(<https://docs.railway.com/databases/redis>).

**Про `?family=0`:** это чисто Node-специфика (`ioredis`/`bullmq` по умолчанию делают только
A-lookup и падают в legacy IPv6-only окружениях) —
<https://docs.railway.com/networking/private-networking/library-configuration>,
<https://docs.railway.com/databases/troubleshooting/enotfound-redis-railway-internal>.
Для Python (`redis-py` / `arq`) такой параметр в URL **не нужен и не поддерживается** — не тащите
его в `REDIS_URL`. В новых окружениях (созданных после 16.10.2025) DNS отдаёт и A, и AAAA.

---

## 4. Связывание переменных между сервисами (reference variables)

Синтаксис — `${{NAMESPACE.VAR}}`:

> «`NAMESPACE` — … For a shared variable, the namespace is "shared". For a variable defined in
> another service, the namespace is the name of the service, e.g. "Postgres" or "backend-api".»
> — <https://docs.railway.com/variables/reference>

Три формы (<https://docs.railway.com/variables>):

```bash
# 1. переменная другого сервиса
DATABASE_URL=${{ Postgres.DATABASE_URL }}
REDIS_URL=${{ Redis.REDIS_URL }}

# 2. общая (shared) переменная проекта: Project Settings → Shared Variables
GEMINI_API_KEY=${{ shared.GEMINI_API_KEY }}

# 3. переменная того же сервиса + конкатенация с текстом
PUBLIC_BASE_URL=https://${{ RAILWAY_PUBLIC_DOMAIN }}
API_URL=https://${{ backend.RAILWAY_PUBLIC_DOMAIN }}
BACKEND_URL=http://${{ api.RAILWAY_PRIVATE_DOMAIN }}:${{ api.PORT }}
```

Ровно такие примеры для нашего стека есть в гайдах Railway:
`DATABASE_URL=${{Postgres.DATABASE_URL}}` и `REDIS_URL=${{Redis.REDIS_URL}}` —
<https://docs.railway.com/guides/fullstack-nextjs>,
<https://docs.railway.com/guides/langgraph-agent-backend>.

Факты, которые легко пропустить:

- Ссылки **не создаются автоматически**: новый сервис в проекте с Postgres **не получает**
  `DATABASE_URL` сам собой, переменную-ссылку надо добавить вручную (или через autocomplete в UI).
  Явная инструкция «Reference `DATABASE_URL` from Postgres» —
  <https://docs.railway.com/guides/ai-agent-workers>.
- Любое добавление/изменение переменной создаёт **staged changes**, которые надо явно задеплоить:
  <https://docs.railway.com/variables>, <https://docs.railway.com/deployments/staged-changes>.
- **Sealed variables** — значение отдаётся в build/deploy, но не показывается в UI и не читается
  через API. Каветы: расшифровать обратно нельзя, не отдаются в `railway variables`/`railway run`,
  не копируются в PR-окружения и при дублировании окружения/сервиса
  (<https://docs.railway.com/variables#sealed-variables>). Для `GEMINI_API_KEY` / `WAZZUP_API_KEY`
  это уместно, но заказчик должен понимать, что локальная разработка через `railway run` их не увидит.
- Переменные доступны и на этапе **сборки**; в Dockerfile их нужно объявлять через `ARG`
  в нужной стадии — <https://docs.railway.com/builds/dockerfiles#using-variables-at-build-time>.
- Многострочные значения поддерживаются (`Cmd/Ctrl + Enter` в поле) — <https://docs.railway.com/variables>.
- **НЕ ПОДТВЕРЖДЕНО:** обновляет ли Railway автоматически ссылки `${{Postgres.*}}` при
  переименовании сервиса. Вывод по здравому смыслу: не переименовывайте сервисы `Postgres`/`Redis`
  после того, как на них навешаны ссылки, либо проверьте переменные после переименования.

---

## 5. Приватная сеть

**Домен:** `<service-name>.railway.internal`; корень `railway.internal` менять нельзя, изменение
имени сервиса меняет и DNS-имя.
— <https://docs.railway.com/networking/domains/working-with-domains#private-domains>

**IPv4/IPv6:**

> «**New environments** (created after October 16, 2025): DNS names resolve to both internal IPv4
> and IPv6 addresses. **Legacy environments**: DNS names resolve to IPv6 addresses only.»
> — <https://docs.railway.com/networking/private-networking/how-it-works>

То есть **IPv6-only — это только легаси-окружения**; новые проекты дуалстек. Разрешён любой
валидный IPv4/IPv6-трафик: TCP, UDP, HTTP/HTTPS. Внутри сети используем `http://`, не `https://`
(трафик и так шифруется Wireguard).

**Когда доступна:**

> «Private networking is only available at **runtime**, not during the build phase. … Database
> migrations that require internal connectivity should run as part of the start command, not the build.»
> — <https://docs.railway.com/networking/private-networking/how-it-works>

Это прямо влияет на нас: **`alembic upgrade head` нельзя запускать на этапе сборки** — БД по
`postgres.railway.internal` в build-контейнере не резолвится. Миграции — либо
`preDeployCommand` (он «execute within your private network and have access to your application's
environment variables» — <https://docs.railway.com/deployments/pre-deploy-command>), либо start-команда.

**Изоляция:** сервисы из разных проектов и из разных окружений одного проекта по приватной сети
**не видят друг друга**; из браузера клиента приватная сеть недоступна.
— <https://docs.railway.com/networking/private-networking/how-it-works>,
<https://docs.railway.com/networking/domains/working-with-domains#private-domain-scope>

**Задержка инициализации приватной сети при старте контейнера — НЕ ПОДТВЕРЖДЕНО.**
В актуальной документации такого предупреждения нет (в старых версиях доки оно было).
На Help Station встречаются рекомендации «sleep 3 перед стартом процесса» и упоминание, что
проблема закрыта в runtime v2 (<https://station.railway.com/questions/reliability-internal-networking-7daff8bd>) —
это **сообщество, не документация**. Косвенное подтверждение существования переключателя:
в JSON-схеме конфига есть поле `deploy.runtime` со значениями `UNSPECIFIED | LEGACY | V2`
(<https://railway.com/railway.schema.json>), но текстового описания в докax нет.
**Практический вывод для нашего кода:** подключения к Postgres/Redis делать лениво и с ретраями
(это и так требование правил проекта — никаких сетевых вызовов на импорте), а `/healthz`
не должен зависеть от БД (см. §11).

---

## 6. Файл конфигурации: `railway.json` / `railway.toml`

**Где ищется:** «By default, we will look for a `railway.toml` or `railway.json` file»
в корне репозитория (точнее — в корне source-директории сервиса).
— <https://docs.railway.com/config-as-code>

**JSON-схема:** `https://railway.com/railway.schema.json` (редиректит на
`https://backboard.railway.app/railway.schema.json`). Ниже — разбор **самой схемы**, не пересказ.

### 6.1. Полная схема (по railway.schema.json, 2026-08-09)

Верхний уровень: `$schema`, `build`, `deploy`, `environments`. `additionalProperties: false` —
посторонние ключи схема отвергнет.

**`build`:**

| Поле | Тип / значения |
|---|---|
| `builder` | enum: `NIXPACKS`, `DOCKERFILE`, `RAILPACK`, `HEROKU`, `PAKETO` (или `null`) |
| `buildCommand` | string \| null |
| `watchPatterns` | array of string \| null |
| `dockerfilePath` | string \| null |
| `nixpacksConfigPath`, `nixpacksPlan`, `nixpacksVersion` | легаси-Nixpacks, string/object \| null |
| `railpackVersion` | string \| null |

**`deploy`:**

| Поле | Тип / значения |
|---|---|
| `startCommand` | string \| null |
| `preDeployCommand` | string **или** массив строк с `maxItems: 1` \| null |
| `numReplicas` | integer, **1…200** \| null |
| `healthcheckPath` | string \| null |
| `healthcheckTimeout` | number (секунды) \| null |
| `sleepApplication` | boolean \| null (это Serverless / App Sleep) |
| `runtime` | enum: `UNSPECIFIED`, `LEGACY`, `V2` \| null |
| `registryCredentials` | `{username, password}` \| null |
| `restartPolicyType` | enum: `ON_FAILURE`, `ALWAYS`, `NEVER` \| null |
| `restartPolicyMaxRetries` | number, min 1 \| null |
| `cronSchedule` | string (cron) \| null |
| `region` | string \| null |
| `multiRegionConfig` | object: `{ "<region>": { "numReplicas": N, … } }` \| null |
| `limitOverride` | `{ containers: { cpu, memoryBytes, diskBytes } }` \| null |
| `requiredMountPath` | string \| null |
| `overlapSeconds` | number ≥ 0 \| null |
| `drainingSeconds` | number ≥ 0 \| null |
| `ipv6EgressEnabled` | boolean \| null |

**`environments`:** объект `{"<имя окружения>": {"build": {…}, "deploy": {…}}}` — те же поля.

> Расхождение, которое стоит знать: страница
> <https://docs.railway.com/config-as-code/reference> документирует далеко не все поля схемы
> (например, `numReplicas` показан только внутри `multiRegionConfig`, а `runtime`, `region`,
> `limitOverride`, `sleepApplication`, `ipv6EgressEnabled` не описаны вовсе). Схема — первоисточник;
> недокументированные поля используйте только осознанно.

### 6.2. TOML-эквивалент

Формат — дело вкуса, поведение идентично (<https://docs.railway.com/config-as-code>):

```toml
[build]
builder = "railpack"
buildCommand = "echo building!"

[deploy]
preDeployCommand = ["npm run db:migrate"]
startCommand = "echo starting!"
healthcheckPath = "/"
healthcheckTimeout = 100
restartPolicyType = "never"
```

⚠️ В официальном TOML-примере значения enum написаны **строчными** (`"railpack"`, `"never"`),
а JSON-схема требует **ПРОПИСНЫХ** (`RAILPACK`, `NEVER`). Мы используем JSON — пишем прописными,
как в JSON-примерах той же страницы.

### 6.3. Приоритеты

> «Configuration defined in code will always override values from the dashboard.» Настройки
> дашборда при этом **не переписываются**, конфиг применяется только к текущему деплою.
> — <https://docs.railway.com/config-as-code>

Порядок разрешения: (1) `environments.<name>` в коде → (2) базовый конфиг в коде →
(3) настройки сервиса в дашборде. Для PR-окружений добавляются ещё два уровня
(имя ephemeral-окружения, затем хардкод-имя `pr`).
— <https://docs.railway.com/config-as-code/reference#setting-environment-overrides>

---

## 7. Два сервиса из одного репозитория с разными start-командами

**Штатный путь (это ровно наш случай — API + worker):**

> «Add a new service in the same project, pointing at the same repo with a different start command
> (e.g. `npm run worker` or `python worker.py`). … No public domain is needed. The worker
> communicates with Redis and Postgres over private networking.»
> — <https://docs.railway.com/guides/ai-agent-workers>

Что настраивается **в UI**:
- Service Source → Connect Repo (тот же репозиторий для обоих сервисов) —
  <https://docs.railway.com/services#deploying-from-a-github-repo>;
- Start Command (если не задан в конфиг-файле) — <https://docs.railway.com/deployments/start-command>;
- Root Directory (нам не нужен — у нас не монорепо) — <https://docs.railway.com/builds/build-configuration>;
- **Railway Config File** — путь к конфигу для этого сервиса;
- Networking → Generate Domain — только для `api`.

Что настраивается **в файле**: всё из `build`/`deploy` (см. §6).

### 7.1. Ключевая ловушка: один `railway.json` на два сервиса

Railway ищет `railway.json`/`railway.toml` в корне **для каждого сервиса**, а конфиг из кода
**перекрывает дашборд**. Значит, если положить в корень один `railway.json` со
`startCommand: uvicorn …`, то worker, для которого в UI прописан `arq …`, **всё равно запустится
как uvicorn** — код важнее дашборда.

Решение — разные файлы + явный путь на каждый сервис:

> «You can use a custom config file by setting it on the service settings page. You should provide
> the absolute path to the file in your repository, for example: `/backend/railway.toml`»
> — <https://docs.railway.com/config-as-code#using-a-custom-config-as-code-file>

Поэтому:

- в репозитории лежат `railway.api.json` и `railway.worker.json`
  (имена **не** `railway.json` — чтобы автодетект ничего не подхватил случайно);
- в сервисе `api` в Settings указываем Railway Config File = `/railway.api.json`;
- в сервисе `worker` — `/railway.worker.json`.

Дополнительно: «The **Railway Config File** does not follow the **Root Directory** path. You have to
specify the absolute path» — <https://docs.railway.com/deployments/monorepo>.

### 7.2. Вариант «один сервис» (`INLINE_WORKER=true`)

Разрешён `SCOPE-OVERRIDE.md` §2 п.4 и никак не противоречит Railway: просто одна служба `api`,
которая внутри себя поднимает воркера. Со стороны Railway это обычный сервис.
Переезд на две службы позже = создать второй сервис из того же репо, указать
`/railway.worker.json` и выставить `INLINE_WORKER=false` у `api`.

---

## 8. Сборка: Dockerfile vs Railpack

- **Dockerfile побеждает автоматически:** «Railway will always build with a Dockerfile if it finds
  one. New services default to Railpack unless otherwise specified.»
  — <https://docs.railway.com/config-as-code/reference#specify-the-builder>
- Ищется файл **строго** с именем `Dockerfile` (с заглавной D) в корне source-директории;
  в логах появляется `Using detected Dockerfile!`
  — <https://docs.railway.com/builds/dockerfiles>
- Нестандартное имя/путь: переменная `RAILWAY_DOCKERFILE_PATH` (по умолчанию `Dockerfile`)
  либо `build.dockerfilePath` в конфиге —
  <https://docs.railway.com/builds/dockerfiles>, <https://docs.railway.com/variables/reference>
- **Форсировать явно:** `"build": {"builder": "DOCKERFILE"}`. Мы так и делаем — чтобы никакой
  Railpack не подхватился, если Dockerfile однажды переименуют.
- Railpack (дефолт для новых сервисов) поддерживает Python из коробки —
  <https://docs.railway.com/builds/railpack>; нам он не нужен.
- Кэш-mount'ы в Dockerfile поддерживаются в формате
  `--mount=type=cache,id=s/<service id>-<target path>,target=<target path>`; для Python пример
  таргета — `/root/.cache/pip`. Переменные окружения в `id` использовать нельзя.
  — <https://docs.railway.com/builds/dockerfiles#cache-mounts>
- Кэш слоёв не гарантирован: «Since Railway's build system scales up and down in response to
  demand, cache hit on builds is not guaranteed» — <https://docs.railway.com/builds/build-configuration>.
  Отключить кэш: `NO_CACHE=1`.
- `watchPatterns` (gitignore-style) — чтобы правка `docs/**` не пересобирала оба сервиса.
  Паттерны считаются от `/` даже при заданном Root Directory; негативные правила работают только
  после позитивных. — <https://docs.railway.com/builds/build-configuration#configure-watch-paths>

---

## 9. Публичный домен и URL для вебхука

**Домен не выдаётся автоматически:**

> «Railway services don't obtain a domain automatically, but it is easy to set one up. To assign a
> domain to your service, go to your service's settings, find the Networking → Public Networking
> section, and choose `Generate Domain`.»
> — <https://docs.railway.com/networking/domains/working-with-domains>

**Формат:** поддомен в зоне `*.up.railway.app`; переменная `RAILWAY_PUBLIC_DOMAIN` —
«The public service or customer domain, of the form `example.up.railway.app`»
(<https://docs.railway.com/variables/reference>).
**Точный шаблон генерируемого имени (типа `<service>-<environment>-<hash>.up.railway.app`) —
НЕ ПОДТВЕРЖДЕНО**: документация фиксирует только зону. Ориентируйтесь на то, что покажет UI.

**HTTPS/TLS:** сертификаты выпускаются и обновляются автоматически (Let's Encrypt, ECDSA, 90 дней,
автообновление за 30 дней до конца) — <https://docs.railway.com/networking/domains/working-with-domains#ssl-certificates>,
<https://docs.railway.com/networking/public-networking/specs-and-limits>. Отдельная работа по TLS
не нужна — что и требовалось для Wazzup (`PATCH /v3/webhooks` требует рабочий HTTPS).

**Как получить URL для вебхука:**
1. Settings → Networking → Public Networking → **Generate Domain** у сервиса `api`.
2. Внутри приложения тот же адрес доступен как `RAILWAY_PUBLIC_DOMAIN` (без схемы), поэтому
   `PUBLIC_BASE_URL=https://${{ RAILWAY_PUBLIC_DOMAIN }}` — рабочая ссылка-переменная.
   *(Инференс: переменная имеет смысл только после генерации домена; до этого значение пустое —
   в докax это прямо не написано, **НЕ ПОДТВЕРЖДЕНО**. В коде читать защитно.)*
3. Итоговый вебхук: `https://<домен>.up.railway.app/wazzup/webhook/<секрет>` — укладывается
   в лимит Wazzup «`webhooksUri` ≤ 200 символов» (см. `research-wazzup24.md`).

**Полезные лимиты edge-прокси** (<https://docs.railway.com/networking/public-networking/specs-and-limits>):
HTTP/1.1 и HTTP/2, websockets; idle HTTP/1.1-соединение закрывается через 60 с; суммарный размер
заголовков ≤ 32 КБ; запрос живёт до 15 минут при активной передаче данных и закрывается через
5 минут простоя; тело запроса должно догрузиться за 5 минут; 10 000 одновременных соединений,
~11 000 RPS на домен. Заголовки от прокси: `X-Real-IP`, `X-Forwarded-Proto` (всегда `https`),
`X-Forwarded-Host`, `X-Railway-Edge`, `X-Request-Start`, `X-Railway-Request-Id`.
`X-Railway-Request-Id` стоит писать в лог — по нему матчатся сетевые логи.

Кастомный домен (если школа захочет `bot.ainazarov.kz`): CNAME **и** TXT обязательны, иначе 404;
Trial — 1 кастомный домен, Hobby — 2 на сервис, Pro — 20.
— <https://docs.railway.com/networking/domains/working-with-domains#custom-domains>

---

## 10. Volumes — нужны ли нам

**Нет, не нужны.** Медиафайлы лежат в репозитории (`media/`) и попадают в образ — как и решено
в `SCOPE-OVERRIDE.md` §2 п.8. Том нужен только для данных, которые **пишутся** и должны пережить
редеплой.

Факты (<https://docs.railway.com/volumes>, <https://docs.railway.com/volumes/reference>):

- Монтируется по указанному абсолютному пути; для относительных путей приложения помните, что код
  лежит в `/app` → `./data` = `/app/data`.
- Тома монтируются **только при старте контейнера**, не при сборке и **не при pre-deploy**.
- Даёт переменные `RAILWAY_VOLUME_NAME`, `RAILWAY_VOLUME_MOUNT_PATH`.
- Монтируется от `root`; для non-root образов (а у нас non-root!) потребовалась бы
  `RAILWAY_RUN_UID=0` — ещё один аргумент не заводить том.
- **Один том на сервис**; **реплики с томом несовместимы**; при редеплое сервиса с томом
  неизбежен короткий даунтайм даже при настроенном health check.
- Размеры по тарифам: Free/Trial 0.5 ГБ, Hobby 5 ГБ, Pro 50 ГБ (до 1 ТБ self-serve).
  Лимит числа томов на проект: Free 1, Trial 3, Hobby 10, Pro 20.
- Цена: $0.15 / ГБ / месяц.

**Эфемерный диск** (без тома): 1 ГБ на Free, 100 ГБ на платных; превышение → сервис может быть
принудительно остановлен и передеплоен — <https://docs.railway.com/deployments/reference#ephemeral-storage>.
Наши временные файлы (например, скачанные медиа) обязаны быть маленькими и чиститься.

---

## 11. Health check

**Как работает:**

> «When a new deployment is triggered for a service, if a healthcheck endpoint is configured,
> Railway will query the endpoint until it receives an HTTP `200` response. Only then will the new
> deployment be made active and the previous deployment inactive.»
> «**Note:** Railway does not monitor the healthcheck endpoint after the deployment has gone live.»
> — <https://docs.railway.com/deployments/healthchecks>

- **Порт проверки** — значение `PORT` (то же, что слушает приложение).
- **Таймаут** — 300 секунд по умолчанию; не уложился → деплой помечается **Failed**
  (старый деплой при этом остаётся активным — трафик не переключается). Меняется полем
  `healthcheckTimeout` или переменной `RAILWAY_HEALTHCHECK_TIMEOUT_SEC`.
- **Hostname проверки — `healthcheck.railway.app`.** Если приложение фильтрует Host
  (FastAPI + `TrustedHostMiddleware`, ALLOWED_HOSTS и т. п.), этот хост надо разрешить, иначе
  «failed with service unavailable» / «failed with status 400».
- **Непрерывного мониторинга нет** — эндпоинт зовётся только при деплое. Для аптайм-мониторинга
  нужен внешний сервис.
- Влияние на zero-downtime: с health check'ом деплой становится `Active` только после `200`
  (<https://docs.railway.com/deployments/reference#deployment-states>); без него — сразу после
  старта контейнера. Плюс `overlapSeconds` (сколько старый деплой живёт параллельно) и
  `drainingSeconds` (SIGTERM → SIGKILL); **по умолчанию оба 0** —
  <https://docs.railway.com/deployments/deployment-teardown>,
  <https://docs.railway.com/variables/reference>.
  Для нас: `drainingSeconds` нужен, чтобы воркер успел доработать задачу, а API — дослать ответ.
- Сервис с томом ломает zero-downtime независимо от health check (см. §10).

**Решение для проекта:** `healthcheckPath: "/healthz"` (живость процесса, без внешних
зависимостей). `/readyz` (БД + Redis + KB + активность канала Wazzup) в health check **не ставить**:
если Wazzup-канал временно `disabled`, деплой упадёт по таймауту, хотя код исправен.

Restart policy: дефолт — `ON_FAILURE`, максимум 10 рестартов
(<https://docs.railway.com/deployments/restart-policy>). На Free/Trial `ALWAYS` **недоступен**,
а `ON_FAILURE` ограничен 10 попытками. Для платного тарифа ставим `ON_FAILURE` с явным
`restartPolicyMaxRetries`.

---

## 12. Логи и переменные окружения — эксплуатация

**Логи** (<https://docs.railway.com/observability/logs>):

- Три места: панель деплоя (build/deploy отдельно), **Log Explorer** (Observability — логи всего
  окружения сразу), CLI `railway logs`.
- Всё, что уходит в stdout/stderr, попадает в логи; `stderr` автоматически становится `level=error`.
- **Структурные логи**: одна строка = один JSON, поля `message`, `level` (`debug|info|warn|error`)
  + произвольные атрибуты; фильтрация `@attribute:value`, `@level:error`, `"key phrase"`,
  булевы `AND`/`OR`/`-`, числовые сравнения `>`, `>=`, `<`, `<=`, `..`.
  → в `app/logging_conf.py` имеет смысл писать JSON-строки в одну линию.
- HTTP-логи с атрибутами `@path`, `@httpStatus`, `@responseTime`, `@srcIp`, `@requestId` — удобно
  ловить провалившиеся вебхуки Wazzup: `@path:/wazzup/webhook AND @httpStatus:>=400`.
- DNS-логи (вкладка Network → DNS) — видно резолвы `*.railway.internal`, полезно при отладке
  приватной сети: `@zone:internal AND @status:failed`.
- **Лимит 500 строк логов в секунду на реплику** — сверх этого строки отбрасываются с предупреждением.
- Ретеншн логов: Hobby/Trial 7 дней, Pro 30 дней, Enterprise до 90.
- Log drain как настройка отсутствует; для внешнего хранения — форвардер или SDK.

**Переменные** (<https://docs.railway.com/variables>):

- Задаются на вкладке Variables сервиса; есть RAW-редактор (вставка `.env`-текста целиком) — самый
  быстрый способ залить сразу весь список из §14.
- Railway предлагает импорт переменных, найденных в `.env`/`.env.example` в корне репо
  (значения из `.env.example` пустые — удобно как чек-лист).
- **Ограничение на размер значения переменной — НЕ ПОДТВЕРЖДЕНО.** В документации лимита нет.
  Исторический контекст: в блоге Railway про envelope-шифрование упоминается, что старый предел
  шёл от GCP KMS и составлял 64 KiB, а переход на envelope-шифрование это ограничение снял
  (<https://blog.railway.com/p/envelope-encryption>). Для нас неактуально — самое большое значение
  это ключ API.

---

## 13. Типовые грабли Python / FastAPI / uvicorn на Railway

**Топ-5 (в порядке вероятности наступить):**

1. **Порт и хост.** `uvicorn app.main:app` без `--host 0.0.0.0 --port $PORT` → edge-прокси не
   достучится, клиент увидит `502 Application failed to respond`. Отдельный подвох: при сборке из
   **Dockerfile** `startCommand` идёт в exec-форме и `$PORT` **не раскрывается** — нужна обёртка
   `/bin/sh -c "exec uvicorn … --port $PORT"` (или `CMD` в shell-форме).
   — <https://docs.railway.com/networking/troubleshooting/application-failed-to-respond>,
   <https://docs.railway.com/deployments/start-command>

2. **`DATABASE_URL` и миграции.** (а) Схема `postgresql://` — asyncpg-драйвер сам не появится,
   нормализуем в `app/config.py`. (б) Хост в этом URL — приватный (`postgres.railway.internal`),
   а приватная сеть **недоступна при сборке** → `alembic upgrade head` в `RUN`-слое Dockerfile
   или в `buildCommand` гарантированно падает. Миграции — только `preDeployCommand` или start-команда.
   — <https://docs.railway.com/networking/private-networking/how-it-works>,
   <https://docs.railway.com/deployments/pre-deploy-command>

3. **Один `railway.json` на два сервиса.** Конфиг из кода перекрывает дашборд, поэтому worker
   получит start-команду API и «молча» поднимет второй uvicorn, который ничего не обрабатывает.
   Лечится раздельными файлами `railway.api.json` / `railway.worker.json` и полем
   **Railway Config File** у каждого сервиса.
   — <https://docs.railway.com/config-as-code>

4. **Health check.** Три способа провалить деплой на ровном месте: (а) поставить `/readyz`,
   зависящий от Wazzup/Gemini/миграций; (б) не пустить хост `healthcheck.railway.app` через
   TrustedHost-мидлварь; (в) уложиться дольше 300 с (холодный старт + миграции + прогрев KB).
   И помните: после успешного деплоя эндпоинт больше **не опрашивается** — «зелёный» сервис может
   быть мёртвым, внешний мониторинг обязателен.
   — <https://docs.railway.com/deployments/healthchecks>

5. **Serverless / App Sleep на API.** Соблазн сэкономить: «первый запрос разбудит». Но
   «The first request sent to a slept service may return a **502 Bad Gateway** response»
   (<https://docs.railway.com/deployments/serverless>) — а первым запросом будет **вебхук Wazzup**,
   и сообщение клиента потеряется. На `api` App Sleep **не включать**. На `worker` он всё равно
   бесполезен: воркер постоянно ходит в Redis, а исходящий трафик сбрасывает счётчик простоя.

**Остальные грабли, которые стоит знать:**

6. **Холодный старт воркера / очередь задач.** Worker без health check становится `Active` сразу
   после старта контейнера (<https://docs.railway.com/deployments/reference#deployment-states>).
   Если он падает на подключении к Redis — увидите только рестарты по `ON_FAILURE`; закладывайте
   ретраи с backoff и понятный лог первой строкой.

7. **Graceful shutdown.** По умолчанию старому деплою даётся **0 секунд** между SIGTERM и SIGKILL
   (<https://docs.railway.com/deployments/reference#singleton-deploys>). Для воркера, который может
   держать задачу, ставим `drainingSeconds` (и, при желании, `overlapSeconds` для API).
   Никаких других сигналов Railway не шлёт.

8. **Тарифы.** Free: 0.5 ГБ RAM / 1 vCPU / 1 реплика, $1 кредита в месяц, `ALWAYS` restart policy
   недоступен, **деплой запрещён в пиковые часы региона** (08:00–20:00 по локальному времени
   региона); Trial: 30 дней, разовые $5, 1 ГБ RAM, shared vCPU, до 5 сервисов на проект, при
   неверифицированном аккаунте — **ограниченный исходящий сетевой доступ и ограниченный набор
   портов** (это убьёт вызовы Gemini/Wazzup). Hobby $5/мес c включёнными $5 usage — минимально
   вменяемый тариф для продакшена этого бота.
   — <https://docs.railway.com/pricing/plans>, <https://docs.railway.com/pricing/free-trial>,
   <https://docs.railway.com/deployments/reference#free-tier-peak-hours-restriction>,
   <https://docs.railway.com/deployments/restart-policy>

9. **Ротация образов / откат.** Образы хранятся 24 ч (Free/Trial), 72 ч (Hobby), 120 ч (Pro) —
   старше этого Rollback недоступен, останется только Redeploy с пересборкой.
   — <https://docs.railway.com/pricing/plans#image-retention-policy>

10. **Railway сам может передеплоить сервис** (миграция между хостами, обновления платформы);
    отказаться нельзя. Значит, приложение обязано переживать внезапный рестарт без потери данных:
    задачи — в Redis/БД, никакого состояния в памяти процесса.
    — <https://docs.railway.com/deployments/reference#railway-initiated-deployments>

11. **Реплики и дедуп.** При `numReplicas > 1` у API вебхуки Wazzup поедут в разные реплики —
    дедуп по `messageId` в Redis/БД обязателен (он и так в архитектуре). Sticky sessions Railway
    не поддерживает. Для воркера несколько реплик безопасны, только если очередь атомарна.
    — <https://docs.railway.com/deployments/scaling>

12. **`preDeployCommand` — не универсальный хук:** выполняется в **отдельном контейнере**, тома не
    примонтированы, изменения ФС не сохраняются, при ненулевом коде возврата деплой **не
    продолжится** и повтора не будет, и он занимает слот в очереди сборок. Для `alembic upgrade head`
    подходит идеально, для «скачать медиа в volume» — нет.
    — <https://docs.railway.com/deployments/pre-deploy-command>

13. **Миграции при двух сервисах.** `preDeployCommand` должен быть **только у `api`**. Если
    прописать его обоим, две `alembic upgrade head` могут стартовать одновременно и подраться
    за блокировку.

---

## 14. Готовые файлы конфигурации для этого проекта

Оба файла кладутся в корень репозитория. **Имена намеренно не `railway.json`** — чтобы автодетект
не подхватил один конфиг сразу для двух сервисов (§7.1). Путь к нужному файлу прописывается в
Settings каждого сервиса (`Railway Config File`).

### 14.1. `railway.api.json` — сервис `api`

```json
{
  "$schema": "https://railway.com/railway.schema.json",

  "build": {
    "builder": "DOCKERFILE",
    "dockerfilePath": "Dockerfile",
    "watchPatterns": [
      "app/**",
      "kb/**",
      "media/**",
      "alembic/**",
      "Dockerfile",
      "requirements.txt",
      "pyproject.toml",
      "railway.api.json"
    ]
  },

  "deploy": {
    "startCommand": "/bin/sh -c \"exec uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips='*'\"",
    "preDeployCommand": ["/bin/sh -c \"alembic upgrade head\""],
    "healthcheckPath": "/healthz",
    "healthcheckTimeout": 300,
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10,
    "numReplicas": 1,
    "overlapSeconds": 20,
    "drainingSeconds": 15
  }
}
```

Комментарии (в JSON их нельзя, поэтому здесь):

- `builder: DOCKERFILE` — форсируем Dockerfile явно, не полагаемся на автодетект.
- `startCommand` обёрнут в `/bin/sh -c`, иначе `$PORT` не раскроется (§1).
- `--workers 1` — воркеры uvicorn это отдельные процессы; горизонтальное масштабирование на Railway
  делается репликами, а не форками (плюс на Free это просто не влезет в 0.5 ГБ).
- `--proxy-headers` + `--forwarded-allow-ips='*'` — за edge-прокси Railway;
  `X-Forwarded-Proto` всегда `https` (<https://docs.railway.com/networking/public-networking/specs-and-limits>).
- `preDeployCommand` — миграции **только здесь**, не в worker'е (§13 п.13).
- `healthcheckPath: /healthz` — живость, без внешних зависимостей (§11).
- `overlapSeconds`/`drainingSeconds` — чтобы вебхук, пойманный старым деплоем, успел доехать.
- `numReplicas: 1` — на старте; поднимать только после дедупа и нагрузочной проверки.

### 14.2. `railway.worker.json` — сервис `worker`

```json
{
  "$schema": "https://railway.com/railway.schema.json",

  "build": {
    "builder": "DOCKERFILE",
    "dockerfilePath": "Dockerfile",
    "watchPatterns": [
      "app/**",
      "kb/**",
      "Dockerfile",
      "requirements.txt",
      "pyproject.toml",
      "railway.worker.json"
    ]
  },

  "deploy": {
    "startCommand": "/bin/sh -c \"exec arq app.workers.queue.WorkerSettings\"",
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10,
    "numReplicas": 1,
    "drainingSeconds": 30
  }
}
```

- **Нет** `healthcheckPath` — воркер не слушает HTTP; health check ждал бы `200` и завалил бы деплой.
- **Нет** `preDeployCommand` — миграции уже прогнал `api`.
- `drainingSeconds: 30` — дать текущей задаче доработать до SIGKILL.
- Домен воркеру **не генерировать**.

### 14.3. Вариант «одна служба» (INLINE_WORKER)

Тогда используется только `railway.api.json`, а у сервиса `api` ставится `INLINE_WORKER=true`.
Файл `railway.worker.json` остаётся в репозитории неиспользованным — он понадобится при разделении.

---

## 15. Полный список переменных окружения, которые заказчик вбивает руками

Легенда столбца «Сервис»: **A** — `api`, **W** — `worker`, **A+W** — обоим (при `INLINE_WORKER=true`
всё ставится одному сервису `api`).

### 15.1. Выдаёт Railway — руками НЕ трогать

| Переменная | Сервис | Комментарий |
|---|---|---|
| `PORT` | A | инъектируется платформой; ручная установка только при target ports |
| `RAILWAY_PUBLIC_DOMAIN` | A | появляется после Generate Domain |
| `RAILWAY_PRIVATE_DOMAIN`, `RAILWAY_SERVICE_NAME`, `RAILWAY_ENVIRONMENT_NAME`, `RAILWAY_REPLICA_ID`, `RAILWAY_GIT_COMMIT_SHA` и пр. | A+W | полный список — <https://docs.railway.com/variables/reference> |

### 15.2. Ссылки на плагины (создаются как reference variables)

| Переменная | Значение | Сервис |
|---|---|---|
| `DATABASE_URL` | `${{ Postgres.DATABASE_URL }}` | A+W |
| `REDIS_URL` | `${{ Redis.REDIS_URL }}` | A+W |

*(Если сервисы БД названы иначе — подставить их реальные имена: namespace = имя сервиса.)*

### 15.3. Задаются руками

| Переменная | Значение / где взять | Сервис |
|---|---|---|
| `APP_ENV` | `production` | A+W |
| `LOG_LEVEL` | `INFO` | A+W |
| `TZ` | `Asia/Almaty` (для читаемых таймстемпов в логах) | A+W |
| `PUBLIC_BASE_URL` | `https://${{ RAILWAY_PUBLIC_DOMAIN }}` — база для `/media/{token}` | A+W |
| `INLINE_WORKER` | `false` при двух службах; `true` при одной | A |
| `WAZZUP_API_KEY` | ЛК Wazzup24 → «Интеграция с CRM» → API → «Дополнительно» | A+W |
| `WAZZUP_WEBHOOK_SECRET` | 32 случайных байта base64url, генерирует заказчик; попадает в путь вебхука | A |
| `WAZZUP_CHANNEL_ID_WHATSAPP` | из `GET /v3/channels` | A+W |
| `WAZZUP_CHANNEL_ID_INSTAGRAM` | из `GET /v3/channels` | A+W |
| `WAZZUP_CLEAR_UNANSWERED` | `false` | A+W |
| `GEMINI_API_KEY` | Google AI Studio, **платный** тариф (Tier 1+) | A+W |
| `GEMINI_MODEL_PRIMARY` | `gemini-3.5-flash-lite` | A+W |
| `GEMINI_MODEL_FALLBACK` | `gemini-3.1-flash-lite` | A+W |
| `GEMINI_THINKING_LEVEL` | `minimal` (задаём явно) | A+W |
| `GEMINI_TIMEOUT_MS` | `30000` | A+W |
| `LLM_MAX_TOOL_LOOPS` | `5` | A+W |
| `LLM_DAILY_BUDGET_USD` | напр. `2` — бюджетный предохранитель | A+W |
| `KB_DIR` | `/app/kb` | A+W |
| `MEDIA_DIR` | `/app/media` | A+W |
| `KB_HOT_RELOAD` | `false` в проде | A+W |
| `MANAGER_NOTIFY_CHANNEL` | канал доставки лид-карточки менеджеру | A+W |
| `MANAGER_NOTIFY_TARGET` | адрес/номер получателя лид-карточки | A+W |
| `WORK_HOURS` | напр. `09:00-20:00` — текст обещания клиенту | A+W |
| `SLA_MINUTES` | напр. `15` — текст обещания клиенту | A+W |

**Отменено `SCOPE-OVERRIDE.md` §1 — НЕ заводить:** `PII_ENCRYPTION_KEY`, `CONSENT_TEXT_VERSION`,
`POLICY_URL`, `POLICY_VERSION`, `RETENTION_LEAD_MONTHS`, `RETENTION_DIALOG_MONTHS`.

**Рекомендация по секретам:** `WAZZUP_API_KEY`, `GEMINI_API_KEY`, `WAZZUP_WEBHOOK_SECRET` — пометить
как **sealed** (<https://docs.railway.com/variables#sealed-variables>), помня о каветах: значение
нельзя посмотреть и вернуть, оно не отдаётся в `railway run`/`railway variables` и не копируется
в PR-окружения. Если заказчик разрабатывает локально через `railway run` — sealed не подойдёт.

**Дублирование между службами:** переменные, помеченные A+W, удобно вынести в
Project Settings → Shared Variables и подключить обоим сервисам через `${{ shared.ИМЯ }}`
(<https://docs.railway.com/variables#shared-variables>) — тогда ключ Gemini правится в одном месте.

---

## 16. Порядок первого деплоя (для `docs/DEPLOY-RAILWAY.md`)

Кратко — подробную русскую пошаговую инструкцию заказчику писать отдельным документом.

1. New Project → Deploy from GitHub repo → выбрать репозиторий. Появится сервис — переименовать в `api`.
2. `+ New → Database → PostgreSQL`; `+ New → Database → Redis` (в том же проекте и окружении —
   иначе приватная сеть не свяжет их, §5).
3. В сервисе `api`: Settings → **Railway Config File** = `/railway.api.json`.
4. Variables у `api`: залить RAW-редактором список из §15 (включая `DATABASE_URL=${{Postgres.DATABASE_URL}}`
   и `REDIS_URL=${{Redis.REDIS_URL}}`), задеплоить staged changes.
5. Settings → Networking → **Generate Domain**. Записать выданный `*.up.railway.app`.
6. Дождаться, пока деплой станет `Active` (health check `/healthz` вернёт `200`).
7. `+ New → GitHub Repo` → тот же репозиторий → сервис переименовать в `worker` →
   Settings → Railway Config File = `/railway.worker.json` → те же переменные (кроме доменных) →
   домен **не** генерировать.
   *(При старте одной службой шаг 7 пропускается, у `api` ставится `INLINE_WORKER=true`.)*
8. Зарегистрировать вебхук в Wazzup: `PATCH /v3/webhooks` с
   `https://<домен>.up.railway.app/wazzup/webhook/<WAZZUP_WEBHOOK_SECRET>` и флагами из
   `ARCHITECTURE.md` §14 (`contactsAndDealsCreation: false`). Wazzup пришлёт тестовый
   `POST {"test": true}` и ждёт `200`.
9. Smoke: написать себе в WhatsApp → в Log Explorer проверить приём вебхука
   (`@path:/wazzup/webhook`), эхо-ветку и `sentFromApp`.

---

## 17. Сводка «НЕ ПОДТВЕРЖДЕНО» — проверить на проде

| # | Что не подтверждено документацией | Как проверить |
|---|---|---|
| 1 | Точный шаблон генерируемого поддомена `*.up.railway.app` | посмотреть значение после Generate Domain |
| 2 | Значение `RAILWAY_PUBLIC_DOMAIN` до генерации домена (пустое?) | залогировать на старте, читать через `.get` с дефолтом |
| 3 | Задержка инициализации приватной сети при старте контейнера | ретраи на первом коннекте к Postgres/Redis + лог времени успеха |
| 4 | Текущий лимит размера значения переменной окружения | не приближаться; исторический потолок был 64 KiB |
| 5 | Автообновление reference-переменных при переименовании сервиса | не переименовывать `Postgres`/`Redis` после настройки |
| 6 | Поведение `deploy.runtime: V2` (в схеме есть, в докax не описано) | не задавать без нужды |
| 7 | Регистр enum-значений в `railway.toml` (в примере строчные, в схеме прописные) | мы используем JSON и прописные |
| 8 | Запускается ли `preDeployCommand` через shell (раскрываются ли `$VAR`) | обёрнут в `/bin/sh -c` на всякий случай |

---

## 18. Реестр источников

**Официальная документация Railway** (все страницы доступны и в `.md`, добавив расширение к URL):

- Порт, хост, 502: <https://docs.railway.com/networking/troubleshooting/application-failed-to-respond>
- Health checks и `PORT`: <https://docs.railway.com/deployments/healthchecks>
- Start command / exec-форма: <https://docs.railway.com/deployments/start-command>, <https://docs.railway.com/builds/build-and-start-commands>
- Pre-deploy command: <https://docs.railway.com/deployments/pre-deploy-command>
- Config as code: <https://docs.railway.com/config-as-code>, <https://docs.railway.com/config-as-code/reference>
- JSON-схема конфига: <https://railway.com/railway.schema.json> (→ <https://backboard.railway.app/railway.schema.json>)
- Переменные: <https://docs.railway.com/variables>, <https://docs.railway.com/variables/reference>
- PostgreSQL: <https://docs.railway.com/databases/postgresql>; шаблон: <https://railway.com/deploy/postgres>
- Redis: <https://docs.railway.com/databases/redis>; шаблон: <https://railway.com/deploy/redis>
- Своя БД / формат строки подключения: <https://docs.railway.com/databases/build-a-database-service>
- Приватная сеть: <https://docs.railway.com/networking/private-networking>, <https://docs.railway.com/networking/private-networking/how-it-works>, <https://docs.railway.com/networking/private-networking/library-configuration>
- Домены: <https://docs.railway.com/networking/domains/working-with-domains>, <https://docs.railway.com/networking/public-networking>
- Лимиты edge: <https://docs.railway.com/networking/public-networking/specs-and-limits>
- TCP proxy: <https://docs.railway.com/networking/tcp-proxy>
- Сборка: <https://docs.railway.com/builds/dockerfiles>, <https://docs.railway.com/builds/build-configuration>, <https://docs.railway.com/builds/railpack>
- Деплой и его состояния: <https://docs.railway.com/deployments/reference>, <https://docs.railway.com/deployments/deployment-teardown>, <https://docs.railway.com/deployments/restart-policy>, <https://docs.railway.com/deployments/scaling>, <https://docs.railway.com/deployments/serverless>
- Монорепо и раздельные конфиги: <https://docs.railway.com/deployments/monorepo>
- Тома: <https://docs.railway.com/volumes>, <https://docs.railway.com/volumes/reference>
- Логи: <https://docs.railway.com/observability/logs>
- Тарифы: <https://docs.railway.com/pricing/plans>, <https://docs.railway.com/pricing/free-trial>
- Прод-чеклист и практики: <https://docs.railway.com/overview/production-readiness-checklist>, <https://docs.railway.com/overview/best-practices>
- Паттерн API + async worker (наш случай): <https://docs.railway.com/guides/ai-agent-workers>
- FastAPI/uvicorn: <https://docs.railway.com/guides/fastapi>, <https://docs.railway.com/deployments/troubleshooting/no-start-command-could-be-found>, <https://docs.railway.com/guides/rag-pipeline-pgvector>

**Не документация (помечено в тексте как таковое):**

- Блог Railway про envelope-шифрование переменных (исторический лимит 64 KiB): <https://blog.railway.com/p/envelope-encryption>
- Help Station про инициализацию приватной сети (сообщество, не гарантия): <https://station.railway.com/questions/reliability-internal-networking-7daff8bd>
