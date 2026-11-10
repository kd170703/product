#!/bin/sh
# A single entry point: always use the production file and its configuration.
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

if [ ! -f .env.prod ]; then
    echo "Сначала выполните: sh scripts/setup-server.sh monitoring.organization.ru" >&2
    exit 1
fi
if [ "$#" -eq 0 ]; then
    echo "Использование: sh scripts/server.sh <команда Docker Compose> [аргументы]" >&2
    echo "Примеры: pull | up -d | ps | logs --tail=100 web | exec web python manage.py createsuperuser" >&2
    exit 2
fi
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "Установите Docker Engine и плагин Docker Compose v2." >&2
    exit 1
fi

exec docker compose --env-file .env.prod -f compose.prod.yaml "$@"
