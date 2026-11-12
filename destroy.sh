#!/usr/bin/env bash
# Удаление сервиса одной командой.
#
#   ./destroy.sh                     остановить и удалить контейнер, данные сохранить
#   ./destroy.sh --purge             удалить контейнер, образ и данные (с резервной копией)
#   ./destroy.sh --purge --no-backup удалить всё без резервной копии
#   ./destroy.sh --yes               без запроса подтверждения (для сценариев)
#
# По умолчанию каталоги data/ и config/ остаются на месте: повторный ./deploy.sh
# поднимет сервис с той же базой и настройками. Полное удаление — только --purge.
set -euo pipefail
cd "$(dirname "$0")"

PURGE=0
BACKUP=1
ASSUME_YES=0
SERVICE="silant-assistant"

say() { printf '%s\n' "$*"; }
warn() { printf 'ВНИМАНИЕ: %s\n' "$*" >&2; }

usage() {
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --purge) PURGE=1; shift ;;
        --no-backup) BACKUP=0; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) warn "неизвестный параметр: $1"; usage; exit 2 ;;
    esac
done

if [ "$ASSUME_YES" -ne 1 ]; then
    if [ ! -t 0 ]; then
        warn "нет интерактивного ввода: запустите с параметром --yes"
        exit 2
    fi
    if [ "$PURGE" -eq 1 ]; then
        printf 'Удалить контейнер, образ, данные и настройки? [y/N] '
    else
        printf 'Остановить и удалить контейнер? Данные останутся. [y/N] '
    fi
    answer=""
    read -r answer || answer=""
    case "$answer" in
        y|Y|yes|YES) ;;
        *) say "Отменено"; exit 0 ;;
    esac
fi

# --- 1. Контейнеры --------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
    if docker compose version >/dev/null 2>&1; then
        docker compose down --remove-orphans || warn "docker compose down завершился с ошибкой"
    else
        docker rm -f "$SERVICE" >/dev/null 2>&1 || true
    fi
    say "Контейнер остановлен и удалён"
else
    warn "Docker не найден: контейнеры не удалялись"
fi

# --- 2. Полное удаление ---------------------------------------------------
if [ "$PURGE" -eq 1 ]; then
    if [ "$BACKUP" -eq 1 ]; then
        mkdir -p backups
        archive="backups/silant-backup-$(date -u +%Y%m%d-%H%M%S).tar.gz"
        if tar czf "$archive" data config/service.json .env 2>/dev/null; then
            say "Резервная копия: $archive"
        else
            warn "резервную копию создать не удалось (возможно, данных ещё нет)"
            rm -f "$archive"
        fi
    fi

    image_removed=0
    if command -v docker >/dev/null 2>&1; then
        if docker rmi silant-assistant:latest >/dev/null 2>&1; then
            image_removed=1
        fi
    fi
    [ "$image_removed" -eq 1 ] && say "Образ удалён"

    rm -f data/index.sqlite3 data/index.sqlite3-journal data/index.sqlite3-wal
    rm -f config/service.json config/service.json.bak config/service.json.tmp
    rm -f .env
    find data/documents -maxdepth 1 -type f ! -name 'README.md' ! -name '.gitkeep' \
        -delete 2>/dev/null || true

    left=""
    for leftover in data/*.sqlite3 config/service.json .env; do
        [ -e "$leftover" ] && left="$left $leftover"
    done
    if [ -n "$left" ]; then
        warn "не удалось удалить:$left — файлы созданы контейнером под другим uid, \
удалите их вручную: sudo rm -f$left"
    else
        say "Данные, настройки и переменные окружения удалены"
    fi
fi

say ""
if [ "$PURGE" -eq 1 ]; then
    say "Сервис удалён полностью. Для повторного развёртывания: ./deploy.sh"
else
    say "Сервис остановлен. Данные сохранены в data/ и config/."
    say "Поднять снова: ./deploy.sh"
    say "Удалить полностью: ./destroy.sh --purge"
fi
