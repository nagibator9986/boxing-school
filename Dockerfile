# syntax=docker/dockerfile:1
#
# Образ AINAZAROV TOP TEAM: бот в Telegram и CRM управления в одном контейнере.
#
# Почему в одном. Обе части работают с общими файлами: база знаний kb/*.yaml и
# базы SQLite. Диск на Railway принадлежит одной службе и между службами не
# разделяется, поэтому разнесённые бот и CRM получили бы каждый свою копию
# данных — владелец правил бы цену в CRM, а бот отвечал бы по старой.
#
# Порт НЕ хардкодится: Railway передаёт его в PORT, слушаем 0.0.0.0:$PORT.
# EXPOSE намеренно нет — фиксированный порт был бы неправдой.
# HEALTHCHECK намеренно нет — живость проверяет Railway по healthcheckPath: /healthz.
# Миграции не нужны: таблицы создаёт сам бот при старте, схема одна на SQLite.

# --------------------------------------------------------------------------- #
# Слой 1: сборка зависимостей в изолированный venv
# --------------------------------------------------------------------------- #
FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# Компиляторы нужны только здесь; в финальный образ они не попадают.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Отдельным слоем: пока requirements.txt не менялся, слой берётся из кэша.
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# --------------------------------------------------------------------------- #
# Слой 2: рантайм. Ни pip-кэша, ни компиляторов, ни исходников сборки.
# --------------------------------------------------------------------------- #
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=Asia/Almaty \
    APP_ENV=prod \
    DATA_DIR=/data

# tzdata обязателен: в slim базы часовых поясов нет, и ZoneInfo("Asia/Almaty")
# упал бы прямо в рантайме (тихие часы напоминаний, рабочее время, дата правок).
# ca-certificates — для TLS к generativelanguage.googleapis.com и api.telegram.org.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Что попадает в образ, определяет .dockerignore (docs, tests, .venv, .env — нет).
COPY . /app

# Каталог данных создаётся на случай запуска без тома: тогда данные живут внутри
# контейнера и теряются при передеплое — об этом громко пишет scripts/serve.py.
RUN mkdir -p /data /app/kb /app/media

# Процесс работает под root намеренно. Том Railway монтируется с правами root,
# и непривилегированный процесс не смог бы записать в него ни историю диалогов,
# ни заявки — служба падала бы на первом же сообщении. Контейнер здесь
# однопользовательский, и это меньшее из двух зол; при переезде на другую
# площадку с настраиваемыми правами тома стоит вернуть отдельного пользователя.

# Значение по умолчанию только для локального `docker run` без -e PORT.
ENV PORT=8000

# Надзиратель поднимает CRM (gunicorn) и бота, следит за обоими и отдаёт сигналы.
CMD ["python", "scripts/serve.py"]
