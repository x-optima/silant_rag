# СИЛАНТ · сервисный ассистент — типовые команды.
# Контейнерный запуск: make up;  локальный: make install && make serve

PYTHON ?= python3
COMPOSE ?= docker compose
SERVICE ?= silant-assistant

.PHONY: help install ingest reindex list-docs ingest-local serve test eval deploy up down destroy logs restart shell clean

help:
	@echo "make ingest        — проиндексировать data/documents внутри контейнера"
	@echo "make reindex       — переиндексировать с заменой изменённых (контейнер)"
	@echo "make list-docs     — показать содержимое базы (контейнер)"
	@echo "make ingest-local  — собрать индекс локально (разработка, нужен Python)"
	@echo "make install       — виртуальное окружение и зависимости (локальная разработка)"
	@echo "make serve         — запустить сервис локально (http://127.0.0.1:8000)"
	@echo "make test          — модульные тесты"
	@echo "make eval          — прогон тестового набора на рабочей базе (локально)"
	@echo "make deploy        — развернуть в Docker (./deploy.sh)"
	@echo "make up            — то же, что make deploy"
	@echo "make destroy       — удалить контейнер, данные сохранить (./destroy.sh)"
	@echo "make down          — то же, что make destroy"
	@echo "make logs          — логи контейнера"
	@echo "make restart       — перезапуск контейнера"
	@echo "make shell         — оболочка внутри контейнера"

install:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt

ingest:
	$(COMPOSE) exec $(SERVICE) python3 -m app.ingest --dir /app/data/documents --db /app/data/index.sqlite3

reindex:
	$(COMPOSE) exec $(SERVICE) python3 -m app.ingest --dir /app/data/documents --db /app/data/index.sqlite3 --replace

list-docs:
	$(COMPOSE) exec $(SERVICE) python3 -m app.ingest --list

ingest-local:
	$(PYTHON) -m app.ingest

serve:
	$(PYTHON) -m app.server --open

test:
	$(PYTHON) -m unittest discover -s tests -t .

eval:
	$(PYTHON) -m eval.run_eval --db data/index.sqlite3 --questions eval/questions_real.jsonl --corpus real

deploy up:
	./deploy.sh

destroy down:
	./destroy.sh --yes

logs:
	$(COMPOSE) logs -f $(SERVICE)

restart:
	$(COMPOSE) restart $(SERVICE)

shell:
	$(COMPOSE) exec $(SERVICE) sh

clean:
	rm -rf .venv __pycache__ app/__pycache__ tests/__pycache__ eval/__pycache__
