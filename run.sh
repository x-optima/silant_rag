#!/usr/bin/env bash
# Локальный запуск без Docker: виртуальное окружение, зависимости, индекс, сервер.
#
#   ./run.sh              # рабочий корпус из data/documents
#   PORT=8080 ./run.sh    # другой порт
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8000}"

if [ ! -d .venv ]; then
    echo "Создаю виртуальное окружение .venv"
    "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate

echo "Устанавливаю зависимости"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

if find data/documents -maxdepth 1 -type f \
    \( -iname '*.pdf' -o -iname '*.txt' -o -iname '*.md' \) 2>/dev/null | grep -q .; then
    echo "Обновляю индекс по документам из data/documents"
    python -m app.ingest
else
    echo "data/documents пуст — сервис запустится с пустой базой."
    echo "Положите PDF руководств в data/documents и запустите ./run.sh снова."
fi

exec python -m app.server --port "$PORT" --open "$@"
