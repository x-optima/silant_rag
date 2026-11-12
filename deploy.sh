#!/usr/bin/env bash
# Развёртывание сервиса одной командой.
#
#   ./deploy.sh                    рабочий корпус из data/documents, порт 8000
#   ./deploy.sh --port 8080        другой внешний порт
#   ./deploy.sh --token СЕКРЕТ     токен для правки настроек не с 127.0.0.1
#   ./deploy.sh --no-build         не пересобирать образ (быстрый перезапуск)
#   ./deploy.sh --wait 180         сколько секунд ждать готовности
#   ./deploy.sh --reindex          переиндексировать корпус с заменой изменённых
#
# Скрипт можно запускать повторно: он не удаляет базу, документы и настройки,
# а неизменившиеся PDF пропускаются при индексации по контрольной сумме.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
WAIT_SECONDS="${WAIT_SECONDS:-120}"
DO_BUILD=1
REINDEX=0
ADMIN_TOKEN="${SILANT_ADMIN_TOKEN:-}"
SERVICE="silant-assistant"

say() { printf '%s\n' "$*"; }
warn() { printf 'ВНИМАНИЕ: %s\n' "$*" >&2; }
fail() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --port)
            [ $# -ge 2 ] || fail "параметр --port требует значение"
            PORT="$2"; shift 2 ;;
        --token)
            [ $# -ge 2 ] || fail "параметр --token требует значение"
            ADMIN_TOKEN="$2"; shift 2 ;;
        --no-build)
            DO_BUILD=0; shift ;;
        --wait)
            [ $# -ge 2 ] || fail "параметр --wait требует значение"
            WAIT_SECONDS="$2"; shift 2 ;;
        --reindex)
            REINDEX=1; shift ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            warn "неизвестный параметр: $1"; usage; exit 2 ;;
    esac
done

case "$PORT" in
    ''|*[!0-9]*) fail "порт должен быть числом, получено: $PORT" ;;
esac
case "$WAIT_SECONDS" in
    ''|*[!0-9]*) fail "ожидание должно быть числом секунд, получено: $WAIT_SECONDS" ;;
esac

# --- 1. Проверка окружения ------------------------------------------------
command -v docker >/dev/null 2>&1 || fail \
    "Docker не найден. Установите Docker Engine: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || fail \
    "Не найден Docker Compose v2. Установите пакет docker-compose-plugin."
if ! docker info >/dev/null 2>&1; then
    fail "Docker-служба недоступна. Запустите её (sudo systemctl start docker) или \
добавьте пользователя в группу docker и войдите заново."
fi

# --- 2. Каталоги состояния ------------------------------------------------
mkdir -p data/documents config
say "Каталоги data/ и config/ готовы"

# --- 3. Файл переменных окружения ----------------------------------------
update_env() {
    local key="$1" value="$2" tmp
    tmp="$(mktemp)"
    if [ -f .env ] && grep -q "^${key}=" .env; then
        awk -v k="$key" -v v="$value" \
            'BEGIN { FS = OFS = "=" } $1 == k { print k "=" v; next } { print }' \
            .env > "$tmp"
    else
        { [ -f .env ] && cat .env; printf '%s=%s\n' "$key" "$value"; } > "$tmp"
    fi
    mv "$tmp" .env
    chmod 600 .env
}

env_value() {
    local key="$1"
    [ -f .env ] || return 0
    awk -F= -v k="$key" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' .env
}

if [ ! -f .env ]; then
    printf '# Создано deploy.sh %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > .env
    chmod 600 .env
    say "Создан файл .env"
fi
update_env PORT "$PORT"

update_env SILANT_DB /app/data/index.sqlite3
update_env SILANT_SERVER_ARGS ""

# Токен админ-панели: без него настройки разрешено менять только с самого
# сервера, поэтому при развёртывании в контейнере токен нужен сразу.
if [ -z "$ADMIN_TOKEN" ]; then
    ADMIN_TOKEN="$(env_value SILANT_ADMIN_TOKEN)"
fi
if [ -z "$ADMIN_TOKEN" ]; then
    if command -v openssl >/dev/null 2>&1; then
        ADMIN_TOKEN="$(openssl rand -hex 24)"
    else
        ADMIN_TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    fi
    update_env SILANT_ADMIN_TOKEN "$ADMIN_TOKEN"
    say "Создан токен админ-панели (сохранён в .env)"
else
    update_env SILANT_ADMIN_TOKEN "$ADMIN_TOKEN"
fi

# --- 4. Проверка настроек и документов -----------------------------------
if [ -f config/service.json ] && command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys; json.load(open("config/service.json", encoding="utf-8"))' \
        || fail "config/service.json содержит некорректный JSON. Исправьте файл или удалите его: \
сервис создаст настройки по умолчанию."
fi

DOCS_COUNT=$(find data/documents -maxdepth 1 -type f \
    \( -iname '*.pdf' -o -iname '*.txt' -o -iname '*.md' \) \
    ! -name 'README.md' ! -name '.gitkeep' 2>/dev/null | wc -l | tr -d ' ')

if [ "$DOCS_COUNT" -eq 0 ]; then
    warn "в data/documents нет документов: сервис запустится с пустой базой и будет \
отвечать «в руководствах ответа нет». Положите PDF и запустите ./deploy.sh снова."
fi

# --- 5. Сборка и запуск ---------------------------------------------------
compose_args=(up -d)
if [ "$DO_BUILD" -eq 1 ]; then
    say "Собираю образ (при первом запуске это занимает несколько минут)"
    docker compose build
fi
say "Запускаю контейнер"
docker compose "${compose_args[@]}"

# --- 6. Ожидание готовности ----------------------------------------------
READY=0
DEADLINE=$((SECONDS + WAIT_SECONDS))
while [ $SECONDS -lt $DEADLINE ]; do
    status=$(docker inspect --format \
        '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        "$SERVICE" 2>/dev/null || echo missing)
    case "$status" in
        healthy) READY=1; break ;;
        missing|exited|dead)
            warn "контейнер в состоянии '$status'"
            break ;;
    esac
    sleep 3
done

if [ "$READY" -ne 1 ]; then
    say "--- последние строки журнала ---"
    docker compose logs --tail 40 "$SERVICE" || true
    fail "сервис не перешёл в состояние healthy за ${WAIT_SECONDS} с. \
Причина обычно указана в журнале выше."
fi
say "Контейнер готов (healthy)"

# --- 7. Индексация --------------------------------------------------------
# Индексация выполняется внутри контейнера: на хосте не нужен ни Python,
# ни poppler. Путь /app/data/documents — это смонтированный ./data/documents.
if [ "$DOCS_COUNT" -gt 0 ]; then
    if [ "$REINDEX" -eq 1 ]; then
        say "Переиндексирую корпус с заменой изменённых документов"
        ingest_args=(--dir /app/data/documents --db /app/data/index.sqlite3 --replace)
    else
        say "Индексирую новые документы из data/documents"
        ingest_args=(--dir /app/data/documents --db /app/data/index.sqlite3)
    fi
    if ! docker compose exec -T "$SERVICE" python3 -m app.ingest "${ingest_args[@]}"; then
        warn "индексация завершилась с ошибкой: проверьте журнал и формат PDF"
    fi
elif [ "$REINDEX" -eq 1 ]; then
    warn "переиндексация запрошена, но в data/documents нет документов"
fi

# --- 8. Итог --------------------------------------------------------------
health=$(curl -s --max-time 5 "http://127.0.0.1:${PORT}/api/health" || true)
summary=""
if command -v python3 >/dev/null 2>&1 && [ -n "$health" ]; then
    summary=$(printf '%s' "$health" | python3 -c '
import json
import sys

try:
    stats = json.load(sys.stdin)["stats"]
except Exception:
    raise SystemExit(0)

print("документов: {docs}, фрагментов: {chunks}, таблиц: {tables}".format(
    docs=stats["documents"], chunks=stats["chunks"], tables=stats["tables"],
))
' 2>/dev/null || true)
fi

say ""
say "Сервис запущен"
say "  Чат:        http://127.0.0.1:${PORT}/"
say "  Настройки:  http://127.0.0.1:${PORT}/admin.html"
if [ -n "$ADMIN_TOKEN" ]; then
    say "  Токен панели: ${ADMIN_TOKEN}"
    say "               введите его в поле «Токен доступа» на странице настроек"
fi
say "  Проверка:   http://127.0.0.1:${PORT}/api/health"
[ -n "$summary" ] && say "  В индексе:  $summary"
lan_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
if [ -n "$lan_ip" ]; then
    say "  В сети:     http://${lan_ip}:${PORT}/"
fi
say ""
say "Журнал:      docker compose logs -f ${SERVICE}"
say "Остановка:   ./destroy.sh"
say "Обновление:  ./deploy.sh   (пересоберёт образ и проиндексирует новые PDF)"
say "Замена ред.: ./deploy.sh --reindex   (заменит изменённые документы)"
