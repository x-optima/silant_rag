#!/usr/bin/env sh
# Точка входа контейнера: собрать индекс при необходимости и запустить сервис.
#
# Переменные окружения:
#   SILANT_DB         путь к файлу индекса (по умолчанию /app/data/index.sqlite3)
#   SILANT_CONFIG     путь к config/service.json
#   SILANT_DOCS_DIR   каталог с PDF руководств (по умолчанию /app/data/documents)
#   SILANT_AUTOINGEST 1 — переиндексировать документы при старте (по умолчанию),
#                     0 — запускать сервис с готовой базой
#   PORT              порт внутри контейнера (по умолчанию 8000)
#   SILANT_SERVER_ARGS дополнительные флаги app.server
set -eu

DB="${SILANT_DB:-/app/data/index.sqlite3}"
PORT="${PORT:-8000}"
DOCS_DIR="${SILANT_DOCS_DIR:-/app/data/documents}"

mkdir -p "$(dirname "$DB")" "$DOCS_DIR" 2>/dev/null || true

if [ "${SILANT_AUTOINGEST:-1}" = "1" ]; then
    files=$(find "$DOCS_DIR" -maxdepth 1 -type f \
        \( -iname '*.pdf' -o -iname '*.txt' -o -iname '*.md' \) 2>/dev/null | wc -l)
    if [ "$files" -gt 0 ]; then
        echo "entrypoint: документов в $DOCS_DIR — $files, обновляю индекс $DB"
        # ingest пропускает неизменившиеся файлы по sha256, поэтому повторный
        # запуск контейнера не переиндексирует базу целиком.
        python3 -m app.ingest --dir "$DOCS_DIR" --db "$DB" \
            || echo "entrypoint: индекс не обновлён, продолжаю со старой базой"
    else
        echo "entrypoint: в $DOCS_DIR нет документов — сервис запустится с пустой базой."
        echo "entrypoint: положите PDF руководств в ./data/documents на хосте и перезапустите контейнер."
    fi
fi

# shellcheck disable=SC2086
exec python3 -m app.server --host 0.0.0.0 --port "$PORT" --db "$DB" ${SILANT_SERVER_ARGS:-}
