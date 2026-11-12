# syntax=docker/dockerfile:1

# СИЛАНТ · сервисный ассистент.
# Образ содержит всё, что нужно для запуска: интерпретатор, poppler для PDF,
# зависимости из requirements.txt. Ничего доустанавливать на машине завода
# не требуется — только Docker.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SILANT_CONFIG=/app/config/service.json \
    SILANT_DB=/app/data/index.sqlite3 \
    SILANT_DOCS_DIR=/app/data/documents \
    PORT=8000

# poppler-utils даёт pdftotext — основной извлекатель текста из руководств.
# curl нужен для HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web
COPY config ./config
COPY eval ./eval
COPY tests ./tests
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# Непривилегированный пользователь: сервис не должен работать от root.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data/documents \
    && chown -R app:app /app \
    && chmod +x /usr/local/bin/entrypoint.sh

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
