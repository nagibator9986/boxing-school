# Makefile AINAZAROV TOP TEAM — локальная разработка и проверки.
# В проде ничего из этого не выполняется: там Railway и start-команды из railway.*.json.

SHELL := /bin/bash

# Интерпретатор: локальный .venv, если он есть, иначе системный python3.
PY      := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
COMPOSE ?= docker compose
BASE_URL ?= http://localhost:8000
PORT    ?= 8000

.DEFAULT_GOAL := help
.PHONY: help venv install up dev down logs ps run worker crm bot migrate revision \
        kb-validate kb-check test lint fmt smoke docker-build clean

help: ## список целей
	@echo "AINAZAROV TOP TEAM — доступные цели:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------------- #
# Окружение
# --------------------------------------------------------------------------- #
venv: ## создать .venv и поставить зависимости
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt ruff

install: ## переустановить зависимости в текущий интерпретатор
	$(PY) -m pip install -r requirements.txt

# --------------------------------------------------------------------------- #
# Инфраструктура (docker-compose — только локально; postgres и redis не нужны)
# --------------------------------------------------------------------------- #
up: ## поднять бота в контейнере (хранилище — SQLite в ./data)
	$(COMPOSE) up -d --wait bot

dev: migrate ## локальный запуск API: миграции + uvicorn --reload
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port $(PORT) --reload

down: ## остановить контейнеры (данные в volume остаются)
	$(COMPOSE) down

logs: ## логи контейнеров, follow
	$(COMPOSE) logs -f --tail=100

ps: ## что сейчас поднято
	$(COMPOSE) ps

# --------------------------------------------------------------------------- #
# Приложение
# --------------------------------------------------------------------------- #
run: ## только API (uvicorn с автоперезагрузкой)
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port $(PORT) --reload

worker: ## только фоновый воркер (arq)
	$(PY) -m arq app.workers.queue.WorkerSettings

migrate: ## накатить миграции (alembic upgrade head)
	$(PY) -m alembic upgrade head

revision: ## новая миграция: make revision m="что изменилось"
	$(PY) -m alembic revision --autogenerate -m "$(m)"

# --------------------------------------------------------------------------- #
# Проверки
# --------------------------------------------------------------------------- #
kb-validate: ## проверить базу знаний (kb/*.yaml)
	$(PY) -m app.kb.loader --check

kb-check: kb-validate ## синоним kb-validate

crm: ## поднять CRM: управление ботом, база знаний, клиенты (http://127.0.0.1:8000)
	$(PY) scripts/crm.py

bot: ## запустить бота в Telegram
	$(PY) scripts/telegram_bot.py

test: ## прогнать тесты
	$(PY) -m pytest -q

lint: ## статические проверки (ruff)
	$(PY) -m ruff check app tests

fmt: ## отформатировать код (ruff format)
	$(PY) -m ruff format app tests

smoke: ## проверка живости: make smoke BASE_URL=https://<домен>.up.railway.app
	@echo "== $(BASE_URL)/healthz =="
	@curl -fsS -m 10 "$(BASE_URL)/healthz" && echo "" || { echo "healthz НЕ отвечает"; exit 1; }
	@echo "== $(BASE_URL)/readyz =="
	@curl -sS -m 10 -o /tmp/readyz.json -w "HTTP %{http_code}\n" "$(BASE_URL)/readyz" || true
	@cat /tmp/readyz.json 2>/dev/null && echo "" || true
	@echo "Вебхук Wazzup: $(BASE_URL)/wazzup/webhook/<WAZZUP_WEBHOOK_SECRET>"

docker-build: ## собрать продовый образ локально (как это сделает Railway)
	docker build -t ainazarov-bot:local .

clean: ## убрать кэши инструментов
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
